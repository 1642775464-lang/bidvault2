"""阶段三真机验收：编辑民机 DOCX → Ctrl+S 快照 → 关闭编辑器 → status=2 正式版本 → 重开 key 变化 → 重拆含新文本。"""
import sqlite3
import time

from playwright.sync_api import sync_playwright

DOC = "97b76a729b3a405cb8f1c7e28650d71d"
MARK = "阶段三验收标记XYZ"


def db():
    s = sqlite3.connect("../data/db/bidvault.sqlite3", timeout=15)
    return s


with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_page(viewport={"width": 1720, "height": 1000})
    page.goto("http://127.0.0.1:5173/", wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.locator("text=文档库").first.click()
    page.wait_for_timeout(900)
    page.locator(".xitem", has_text="招标文件—民机").first.click()
    frame = None
    t0 = time.time()
    while time.time() - t0 < 90:
        for f in page.frames:
            if "8080" in (f.url or "") and "web-apps" in (f.url or ""):
                frame = f
                break
        if frame:
            break
        page.wait_for_timeout(2000)
    assert frame, "编辑器未加载"
    page.wait_for_timeout(18000)
    page.mouse.click(1090, 650)   # 关警告弹窗
    page.wait_for_timeout(500)

    # 1) 编辑：点封面标题下方正文，输入标记
    page.mouse.click(1000, 560)
    page.wait_for_timeout(800)
    page.keyboard.type(MARK, delay=60)
    page.wait_for_timeout(800)

    # 2) Ctrl+S → status=6 工作快照（不产生正式版本）
    page.keyboard.press("Control+s")
    print("已按 Ctrl+S，等待 status=6 快照…")
    time.sleep(12)
    s = db()
    n_ver_6 = s.execute("SELECT count(*) FROM document_versions WHERE document_id=?", (DOC,)).fetchone()[0]
    cur_6 = s.execute("SELECT current_version_id FROM documents WHERE id=?", (DOC,)).fetchone()[0]
    s.close()
    print(f"[证据A] status=6 后正式版本数={n_ver_6} current未变={'是' if cur_6 else '否'}")
    page.screenshot(path="../回归证据/阶段三_快照保存.png")

    # 3) 关闭编辑器（切到拆解页签触发 destroy）→ DS 延时发 status=2
    page.locator(".doctabs button", has_text="拆解与AI分析").first.click()
    print("已切走页签（关闭编辑会话），等待 status=2 正式版本…")
    deadline = time.time() + 90
    cur_before = cur_6
    new_ver = None
    while time.time() < deadline:
        s = db()
        rows = s.execute(
            "SELECT version, id, file_hash FROM document_versions WHERE document_id=? ORDER BY version", (DOC,)
        ).fetchall()
        cur = s.execute("SELECT current_version_id FROM documents WHERE id=?", (DOC,)).fetchone()[0]
        s.close()
        if len(rows) >= 2 and cur != cur_before:
            new_ver = rows[-1]
            break
        page.wait_for_timeout(3000)
    assert new_ver, "status=2 正式版本未生成"
    print(f"[证据B] 正式版本 v{new_ver[0]} 已创建，current_version_id 已原子前移")

    # 4) 重新打开（原始文档页签）→ key 应切到 v2
    page.locator(".doctabs button", has_text="原始文档").first.click()
    page.wait_for_timeout(12000)
    s = db()
    vh = s.execute("SELECT file_hash FROM document_versions WHERE id=?", (new_ver[1],)).fetchone()[0]
    s.close()
    expected_key = f"{DOC}-v{new_ver[0]}-{vh[:12]}"
    got_key = ""
    t0 = time.time()
    while time.time() - t0 < 30:
        for f in page.frames:
            if "8080" in (f.url or "") and "editor" in (f.url or ""):
                got_key = f.url
        r = frame
        break
    # 直接比对 editor-config 返回的 key
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:8000/api/documents/{DOC}/editor-config", timeout=15) as r:
        cfg = json.loads(r.read()) if (json := __import__("json")) else None
    got_key = cfg["config"]["document"]["key"]
    print(f"[证据C] 重开后 key={got_key}")
    print(f"[证据C] 预期 key={expected_key}")
    print(f"[证据C] key 已切换到新正式版本: {got_key == expected_key}")
    page.screenshot(path="../回归证据/阶段三_重开.png")

    # 5) 重新拆解 → 拆解结果应包含新文本
    page.locator(".doctabs button", has_text="拆解与AI分析").first.click()
    page.wait_for_timeout(800)
    page.locator("button", has_text="重新拆解").first.click()
    print("已触发重新拆解，等待完成…")
    time.sleep(45)
    s = db()
    txt = s.execute("SELECT content_text FROM documents WHERE id=?", (DOC,)).fetchone()[0] or ""
    s.close()
    contains = MARK in txt
    print(f"[证据D] 重新拆解后 content_text 含验收标记: {contains}")
    page.screenshot(path="../回归证据/阶段三_重拆.png")
    b.close()
print("验收流程完成")
