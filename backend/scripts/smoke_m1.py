# /// script
# requires-python = ">=3.11"
# ///
"""M1 冒烟：起服务全链路（项目→上传→拆解→素材→检索→涉密→绑定→阶段）。"""

import io
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
    "第三章 格式清单\n"
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

# 6 涉密打标
r = c.patch(f"{base}/documents/{bid_id}/secret", json={"is_secret": False})
step("涉密解除", r.json().get("is_secret") is False)
r = c.patch(f"{base}/documents/{bid_id}/secret", json={"is_secret": True})
step("涉密恢复", r.json().get("is_secret") is True)

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

print(f"\n冒烟结果：{ok} 过 / {fail} 败")
raise SystemExit(1 if fail else 0)
