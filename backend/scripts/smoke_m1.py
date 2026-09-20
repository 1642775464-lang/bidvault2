# /// script
# requires-python = ">=3.11"
# ///
"""M1 冒烟：起服务全链路（项目→上传→拆解→素材→检索→检测→导出）。"""

import io
import os
import time

import httpx
from docx import Document as Docx


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    d.add_paragraph(text)
    d.save(buf)
    return buf.getvalue()


TENDER = (
    "第一章 投标人须知前附表\n"
    "项目名称：智慧园区数据中心项目\n"
    "招标编号：HTC-HW-2026-0891\n"
    "投标人资质要求：具备电子与智能化工程专业承包一级资质\n"
    "投标截止时间：2026-09-30 09:30\n"
    "第二章 评标办法前附表\n"
    "评标办法：综合评估法，技术分 60 分，商务分 20 分，价格分 20 分\n"
    "第三章 投标文件格式\n"
    "投标函、开标一览表、法定代表人身份证明、授权委托书\n"
)
BID = (
    "第一章 投标函\n"
    "致：南青云网络技术有限公司\n"
    "第二章 法定代表人身份证明\n"
    "兹证明张建国为本公司法定代表人\n"
    "第三章 授权委托书\n"
    "本人张建国授权代理人王志强参加本项目投标活动\n"
    "委托代理人：王志强\n"
    "第四章 技术方案\n"
    "机房按 B 级标准建设\n"
    "第五章 类似项目业绩\n"
    "云启数据中心机房改造项目（2024 年，合同额 1,280 万元）\n"
    "第六章 项目团队成员及相关人员简历\n"
    "项目经理：王志强（一级建造师机电工程）\n"
    "技术负责人：李慧敏（高级工程师，暖通方向）\n"
)

base = "http://127.0.0.1:8000/api"
c = httpx.Client(timeout=30)

ok = fail = 0


def step(name: str, cond: bool, detail: str = ""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS {name} {detail}")
    else:
        fail += 1
        print(f"  FAIL {name} {detail}")


# 1 项目
r = c.post(f"{base}/projects", json={"name": "智慧园区数据中心项目", "role": "主标", "industry": "数据中心 / ICT", "open_at": "2026-09-30", "cust": "南青云网络技术有限公司"})
pid = r.json()["id"]
step("新建项目", r.status_code == 200, pid)

r = c.get(f"{base}/projects/meta")
step("项目元数据", "数据中心 / ICT" in r.json()["industries"])

# 2 上传（招标+投标）
r = c.post(
    f"{base}/documents/upload",
    files=[("files", ("招标文件_HTC-HW-2026-0891.docx", _docx(TENDER), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
    data={"kind": "tender", "project_id": pid, "is_secret": "true"},
)
tid = r.json()["created"][0]["id"]
r = c.post(
    f"{base}/documents/upload",
    files=[("files", ("投标文件_智慧园区数据中心项目.docx", _docx(BID), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
    data={"kind": "bid", "project_id": pid, "is_secret": "true"},
)
bid_id = r.json()["created"][0]["id"]
step("上传招标+投标", bool(tid and bid_id))


def wait_status(doc_id: str, want: int = 2, timeout: float = 60) -> int:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"{base}/documents", params={"q": ""}).json()
        for d in s["items"]:
            if d["id"] == doc_id and d["status"] >= want:
                return d["status"]
        time.sleep(1)
    return -1


st = wait_status(bid_id)
step("队列拆解完成", st == 2, f"status={st}")

# 3 拆解结构
r = c.get(f"{base}/documents/{tid}/extract")
g = r.json()
names = [i["name"] for grp in g["groups"] for i in grp["items"]]
step("招标抽取组", r.status_code == 200 and len(g["groups"]) > 0, str(names[:4]))

# 4 素材自动入库
r = c.get(f"{base}/materials")
mats = r.json()["items"]
step("素材自动入库", r.json()["total"] >= 2, f"total={r.json()['total']}")

# 5 检索
r = c.get(f"{base}/search", params={"q": "数据中心"})
hits = r.json()["hits"]
step("全文检索", r.status_code == 200 and len(hits) > 0, f"hits={len(hits)} top={hits[0]['filename'] if hits else '-'}")

# 6 重新拆解（原样复制制：LLM 不参与结构拆解，重拆幂等）
r = c.post(f"{base}/documents/{bid_id}/extract")
ex = c.get(f"{base}/documents/{bid_id}/extract").json()
n_items = sum(len(g["items"]) for g in ex["groups"])
r = c.post(f"{base}/documents/{bid_id}/extract")
ex2 = c.get(f"{base}/documents/{bid_id}/extract").json()
n_items2 = sum(len(g["items"]) for g in ex2["groups"])
step("重新拆解稳定", r.status_code == 200 and n_items >= 1 and n_items == n_items2, f"items={n_items}")

# 7 正文预览
r = c.get(f"{base}/documents/{bid_id}/content")
step("正文预览", r.status_code == 200 and "授权委托书" in r.json()["text"])

# 8 阶段推进 + 绑定 + 归档链路
r = c.post(f"{base}/projects/{pid}/stage", json={"stage": 2})
step("阶段推进", r.status_code == 200)
r = c.post(f"{base}/documents/{bid_id}/bind", json={"project_id": None})
step("解除绑定", r.json().get("project_id") is None)
r = c.post(f"{base}/documents/{bid_id}/bind", json={"project_id": pid})
step("重新绑定", r.json().get("project_id") == pid)

# 9 health
r = c.get(f"{base}/health")
cnt = r.json()["counts"]
step("health 计数", cnt["documents"] >= 2 and cnt["materials"] >= 2, str(cnt))

# 10 F2.2 NL 检索（语义通道）
r = c.get(f"{base}/search", params={"q": "投标需要交多少钱", "mode": "nl"})
step("NL 检索", r.status_code == 200 and len(r.json()["hits"]) > 0, f"hits={len(r.json()['hits'])}")

# 11 F2.5 人员档案卡
r = c.get(f"{base}/people", params={"project_id": pid})
cards = r.json()["items"]
step("人员档案卡", r.status_code == 200 and any("王志强" in p["name"] or "张建国" in p["name"] for p in cards), str([p['name'] + '/' + p['role'] for p in cards][:3]))

# 12 前端可达
r = httpx.get("http://127.0.0.1:5173/", timeout=10)
step("前端页面", r.status_code == 200 and "标库" in r.text)

# 13 M3 图片证据链：构造含内嵌图的 PDF 上传
def _random_png(w=400, h=300):

    import struct as _s
    import zlib as _z
    rows = b"".join(b"\x00" + os.urandom(w * 3) for _ in range(h))
    def chunk(tag, data):
        return _s.pack(">I", len(data)) + tag + data + _s.pack(">I", _z.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = _s.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", _z.compress(rows)) + chunk(b"IEND", b"")



pdf = __import__("fitz").open()
page = pdf.new_page(width=595, height=842)
page.insert_text((72, 90), "营业执照复印件", fontsize=15, fontname="china-s")
page.insert_image(__import__("fitz").Rect(72, 120, 400, 350), stream=_random_png())
buf = io.BytesIO()
pdf.save(buf)
r = c.post(f"{base}/documents/upload", files=[("files", ("资质文件_图片版.pdf", buf.getvalue(), "application/pdf"))], data={"kind": "bid", "project_id": pid, "is_secret": "true"})
img_doc_id = r.json()["created"][0]["id"]


def _wait_img(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        mats_ = c.get(f"{base}/materials", params={"q": "图片证据"}).json()["items"]
        if mats_:
            return mats_
        time.sleep(1)
    return []

imgmats = _wait_img()
step("图片素材自动入库", len(imgmats) >= 1 and imgmats[0]["source_page"] == 1, str([(m["title"], m["source_page"]) for m in imgmats[:2]]))

# 14 页面预览
pc = c.get(f"{base}/documents/{img_doc_id}/page_count").json()
r = c.get(f"{base}/documents/{img_doc_id}/pages", params={"page_no": 1})
step("页面预览 PNG", pc["pages"] == 1 and r.status_code == 200 and r.headers["content-type"] == "image/png")

# 15 证据图片文件直读
r = c.get(f"{base}/files/{imgmats[0]['file_name']}")
step("证据图片直读", r.status_code == 200 and r.headers["content-type"].startswith("image/"))


# 17 M4 废标检测：主投标件缺保证金/有效期响应 → 规则触发
r = c.post(f"{base}/detect/{pid}")
body = r.json()
titles = " ".join(x["title"] for x in body["items"])
step("废标检测触发", r.status_code == 200 and "资质等级" in titles and body["risk_index"] > 0,
     f"index={body['risk_index']} items={[x['title'] for x in body['items']][:3]}")

# 18 已处理闭环
rid = body["items"][0]["id"]
r = c.patch(f"{base}/detect/risks/{rid}/done", json={"done": True})
step("风险处理闭环", r.json()["ok"] and r.json()["risk_index"] < body["risk_index"])  # 处理后指数下降（新语义：越高越危险）

# 19 检测报告 Excel
r = c.get(f"{base}/detect/{pid}/report.xlsx")
step("检测报告 Excel", r.status_code == 200 and "spreadsheetml" in r.headers["content-type"] and len(r.content) > 4000)

# 20 评分对照联动
step("评分对照联动", len(body["score_rows"]) > 0, str(body["score_rows"][:1]))

# 21 内置规则清单
r = c.get(f"{base}/detect/rules")
step("内置规则清单", r.status_code == 200 and len(r.json()["rules"]) >= 7)

r = c.get(f"{base}/documents/{bid_id}/download")
step("成品导出下载", r.status_code == 200 and "attachment" in r.headers.get("content-disposition", ""))

r = httpx.get("http://127.0.0.1:8000/")
step("生产模式托管", r.status_code == 200 and 'id="app"' in r.text)
r = httpx.get("http://127.0.0.1:8000/some/route")
step("SPA fallback", r.status_code == 200 and 'id="app"' in r.text)

# 22 第2期 组合筛选
r = c.get(f"{base}/documents", params={"kind": "bid", "status": "2"})
step("组合筛选 kind+status", r.status_code == 200 and r.json()["total"] >= 1, f"total={r.json()['total']}")
png2 = _random_png(120, 90)
r = c.post(f"{base}/documents/upload", files=[("files", ("资质证书_扫描.png", png2, "image/png"))], data={"kind": "bid", "project_id": pid, "is_secret": "true"})
step("图片文档上传", r.status_code == 200 and len(r.json().get("created", [])) == 1)
r = c.get(f"{base}/documents", params={"file_type": "img"})
step("组合筛选 图片类型", r.status_code == 200 and r.json()["total"] >= 1, f"total={r.json()['total']}")
r = c.get(f"{base}/documents", params={"project_id": "none"})
step("组合筛选 未绑定", r.status_code == 200 and r.json()["total"] == 0, f"total={r.json()['total']}")

# 23 第2期 素材真实上传（图片 OCR）
png1 = _random_png(200, 120)
r = c.post(f"{base}/materials/upload", files=[("file", ("营业执照_新.png", png1, "image/png"))], data={"cat": "资质文件"})
up_ok = r.status_code == 200 and r.json().get("type") == "图片"
step("素材图片上传", up_ok, str(r.json()) if up_ok else r.text[:80])
r = c.get(f"{base}/materials")
step("上传素材入库", any("营业执照_新" in m["title"] for m in r.json()["items"]))

# 24 第2期 正文编辑保存 + 成品稿导出
edited = BID + chr(10) + "本人承诺：本投标文件所有内容真实有效。" + chr(10)
r = c.put(f"{base}/documents/{bid_id}/content", json={"text": edited})
step("正文编辑保存", r.status_code == 200 and r.json().get("ok"), str(r.text[:60]))
r = c.get(f"{base}/documents/{bid_id}/content")
step("编辑后预览", "真实有效" in r.json()["text"])
r = c.get(f"{base}/documents/{bid_id}/content.docx")
step("成品稿导出", r.status_code == 200 and len(r.content) > 4000)
c.put(f"{base}/documents/{bid_id}/content", json={"text": BID})

# 25 第2期 检测规则维护 + 留痕
r = c.get(f"{base}/detect/rules")
rules0 = r.json()["rules"]
step("规则清单含状态", r.status_code == 200 and all("enabled" in x for x in rules0))
r = c.patch(f"{base}/detect/rules/copies", json={"enabled": False})
step("规则停用", r.status_code == 200 and r.json().get("changed"))
r = c.get(f"{base}/detect/rules")
step("停用生效", next(x["enabled"] for x in r.json()["rules"] if x["id"] == "copies") is False)
c.patch(f"{base}/detect/rules/copies", json={"enabled": True})
r = c.get(f"{base}/detect/rules/history")
step("规则留痕", r.status_code == 200 and len(r.json()["items"]) >= 1, f"n={len(r.json()['items'])}")

# 26 第2期 备份与还原
r = c.post(f"{base}/backup")
bk_ok = r.status_code == 200 and "name" in r.json()
step("手动备份", bk_ok, str(r.json()) if bk_ok else r.text[:60])
r = c.get(f"{base}/backup")
step("备份列表", r.status_code == 200 and len(r.json()["items"]) >= 1 and r.json()["keep"] == 7)
r = c.get(f"{base}/detect/{pid}/risks")
step("缺项风险带引用", all(x.get("quote") for x in r.json()["items"]), str([x["title"] for x in r.json()["items"]][:2]))

# 27 第3期 招标公告独立类型
r = c.post(
    f"{base}/documents/upload",
    files=[("files", ("招标公告_HTC-HW-2026-0891.txt", "智慧园区数据中心项目招标公告（公开招标）".encode(), "text/plain"))],
    data={"kind": "notice", "project_id": pid, "is_secret": "false"},
)
step("公告上传", r.status_code == 200 and len(r.json().get("created", [])) == 1, str(r.text[:60]))
r = c.get(f"{base}/documents", params={"kind": "notice"})
step("公告筛选", r.status_code == 200 and r.json()["total"] >= 1, f"total={r.json()['total']}")

# 28 第3期 客户历史合作案例（归档登记中标 → 案例推荐）
r = c.post(f"{base}/projects", json={"name": "智慧园区二期数据中心项目", "role": "主标", "industry": "数据中心 / ICT", "cust": "南青云网络技术有限公司"})
pid2 = r.json()["id"]
r = c.post(f"{base}/projects/{pid}/archive", json={"result": "中标", "result_note": "第三次合作", "result_amount": "1,280 万元"})
step("归档登记中标", r.status_code == 200 and r.json().get("ok"))
r = c.get(f"{base}/projects/cases", params={"project_id": pid2})
c0 = r.json()["items"][0] if r.json()["items"] else {}
step("案例推荐真实化", r.status_code == 200 and len(r.json()["items"]) >= 1 and c0.get("match", 0) >= 75 and c0.get("amount") == "1,280 万元",
     f"match={c0.get('match')} name={c0.get('name', '')[:12]}")
r = c.get(f"{base}/projects/cases", params={"project_id": pid})
step("案例排除当前项目", all(x["id"] != pid for x in r.json()["items"]))

# 29 第3期 磁盘空间水位
r = c.get(f"{base}/health")
disk = r.json().get("disk") or {}
step("磁盘水位", r.status_code == 200 and disk.get("free_gb", 0) > 0 and "warn" in disk, str(disk))

print(f"\n冒烟结果：{ok} 过 / {fail} 败")
raise SystemExit(1 if fail else 0)
