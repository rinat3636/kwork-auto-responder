from playwright.sync_api import sync_playwright
import json, re
CDP="http://localhost:29229"
with sync_playwright() as pw:
    b=pw.chromium.connect_over_cdp(CDP)
    ctx=b.contexts[0]
    page=ctx.pages[0] if ctx.pages else ctx.new_page()
    page.bring_to_front()
    page.goto("https://kwork.ru/projects?c=11", wait_until="networkidle")
    page.wait_for_timeout(1500)
    print("URL:", page.url, "TITLE:", page.title())
    ids=page.evaluate("""() => {
        const set=new Set();
        document.querySelectorAll('a[href*="/projects/"]').forEach(a=>{
            const m=a.getAttribute('href').match(/\\/projects\\/(\\d+)/);
            if(m) set.add(m[1]);
        });
        return Array.from(set);
    }""")
    print("COUNT:", len(ids))
    print("IDS:", ids[:20])
