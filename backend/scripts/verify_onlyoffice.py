"""阶段二真机验收：ONLYOFFICE 打开民机真实 DOCX + Heading 导航 + 截图。"""
import time

from playwright.sync_api import sync_playwright

OUT = "../回归证据"
DOC_NAME = "招标文件—民机"

with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_page(viewport={"width": 1720, "height": 1000})
    page.goto("http://127.0.0.1:5173/", wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.locator("text=文档库").first.click()
    page.wait_for_timeout(900)
    page.locator(".xitem", has_text=DOC_NAME).first.click()
    page.wait_for_timeout(1200)
    # 原始文档页签（默认），等待 ONLYOFFICE iframe
    t0 = time.time()
    frame = None
    while time.time() - t0 < 120:
        for f in page.frames:
            if "8080" in (f.url or "") and "web-apps" in (f.url or ""):
                frame = f
                break
        if frame:
            break
        page.wait_for_timeout(2000)
    print("编辑器 iframe:", "已出现" if frame else "120秒未出现")
    if frame:
        # 等文档转换渲染（编辑器内部出现画布/骨架）
        page.wait_for_timeout(20000)
        page.screenshot(path=f"{OUT}/验收_编辑器打开.png")
        print("截图1: 编辑器打开")
        # 找导航/标题面板按钮（zh-CN 界面）
        opened = False
        for label in ["导航", "标题", "heading", "Headings"]:
            try:
                btn = frame.locator(f"[title*='{label}'], [aria-label*='{label}'], [data-tooltip*='{label}']").first
                btn.click(timeout=2500)
                opened = True
                print(f"导航按钮({label}): 已点击")
                break
            except Exception:
                continue
        page.wait_for_timeout(3500)
        # 读取标题条目
        texts = []
        try:
            items = frame.locator(".region-view .item, .navigation-panel .item, [class*='navigation'] div, [class*='heading'] .item")
            n = items.count()
            for i in range(min(n, 60)):
                t = (items.nth(i).inner_text() or "").strip()
                if t:
                    texts.append(t)
        except Exception:
            pass
        if not texts:
            # 兜底：抓取面板可见文本
            try:
                body_text = frame.locator("body").inner_text()
                texts.extend(f"{kw}: {'出现' if kw in body_text else '未出现'}" for kw in ["招标公告", "第二章", "第三章", "第四章", "第五章", "第六章"])
            except Exception:
                pass
        print("标题导航条目样本:", texts[:14])
        page.screenshot(path=f"{OUT}/验收_标题导航.png")
        print("截图2: 标题导航")
        # 点击第三章跳转
        try:
            target = None
            for sel in ["text=第三章评标办法", "text=第三章 评标办法", "[title*='第三章']"]:
                loc = frame.locator(sel).first
                try:
                    if loc.is_visible(timeout=1200):
                        target = loc
                        break
                except Exception:
                    continue
            if target:
                target.click(timeout=2500)
                page.wait_for_timeout(2500)
                page.screenshot(path=f"{OUT}/验收_标题跳转.png")
                print("截图3: 点击第三章跳转后")
            else:
                print("第三章条目未在面板中定位到")
        except Exception as e:
            print("跳转点击异常:", str(e)[:80])
    b.close()
print("完成")
