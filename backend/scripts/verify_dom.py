"""页面级验证：正文段落渲染为 <p class=wp-p>、真实表格渲染为 table.realtable。"""
from playwright.sync_api import sync_playwright

TARGET = "1.1.2 招标人：见投标人须知前附表。"

with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_page(viewport={"width": 1600, "height": 950})
    page.goto("http://127.0.0.1:5173/", wait_until="networkidle")
    page.wait_for_timeout(1200)
    # 进入文档库
    page.locator("text=文档库").first.click()
    page.wait_for_timeout(800)
    # 选中招标文件
    page.locator(".xitem", has_text="招标文件—民机").first.click()
    page.wait_for_selector(".wordpage", timeout=15000)
    page.wait_for_timeout(1500)
    stats = page.evaluate(
        """() => {
        const ps = document.querySelectorAll('.wordpage p.wp-p').length;
        const tbls = document.querySelectorAll('.wordpage table.realtable').length;
        let target = null;
        for (const p of document.querySelectorAll('.wordpage p.wp-p')) {
            if ((p.innerText || '').includes('1.1.2 招标人')) { target = 'p.wp-p'; break; }
        }
        if (!target) {
            for (const td of document.querySelectorAll('.wordpage td')) {
                if ((td.innerText || '').includes('1.1.2 招标人')) { target = 'td'; break; }
            }
        }
        let plainInTd = false;
        for (const td of document.querySelectorAll('.wordpage td')) {
            const t = (td.innerText || '').trim();
            if (t === '1. 总则' || t === '1.1 招标项目概况') { plainInTd = true; break; }
        }
        return { paras: ps, tables: tbls, targetLoc: target, plainInTd };
    }"""
    )
    print("DOM 验证：", stats)
    ok = stats["paras"] > 0 and stats["tables"] > 0 and stats["targetLoc"] == "p.wp-p" and not stats["plainInTd"]
    print("结论：", "PASS——正文为段落、表格为表格" if ok else "FAIL")
    b.close()
