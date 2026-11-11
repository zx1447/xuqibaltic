"""MCWorldHost AFK 页勘探: 元素清单 + 网络请求流 + console 捕获。

目的: 站点改版后「挂着页面 +4min 一刷」不再涨币（20:37 run 52 分钟 +0），
需要确认记账机制——如果 AFK 页在记币，一定存在周期性网络请求（server
action / api 调用），从请求流即可定位真正的领取链路。
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcworldhost_cf import open_session, sign_in  # noqa: E402

BASE = "https://mcworldhost.de"
AFK_PAGE = "/dashboard/afk"


def log(m):
    print(m, flush=True)


INVENTORY_JS = """() => {
  const out = [];
  const sel = 'button,a,[role=button],input,select,textarea,iframe,[class*=countdown],[class*=timer],[class*=afk],[class*=earn],[class*=coin],[class*=reward],[data-testid]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName,
      txt: (el.innerText || el.value || el.getAttribute('aria-label') || '').slice(0, 90).replace(/\\s+/g, ' ').trim(),
      href: (el.href || '').slice(0, 120),
      cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 90),
      id: el.id || '',
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)].join(',')
    });
  }
  return out;
}"""


def main() -> int:
    email = os.environ.get("MCWH_EMAIL", "").strip()
    password = os.environ.get("MCWH_PASSWORD", "")
    if not email or not password:
        log("Missing MCWH secrets.")
        return 2

    log("Solving Cloudflare challenge (headed Chromium on Xvfb)...")
    pw, browser, page = open_session(BASE)
    reqs, console = [], []

    def on_resp(r):
        try:
            reqs.append("%s %s %s" % (r.status, r.request.method, r.url[:200]))
        except Exception:
            pass

    page.on("response", on_resp)
    page.on("console", lambda m: console.append("%s: %s" % (m.type, m.text[:220])))
    page.on("pageerror", lambda e: console.append("PAGEERROR: %s" % str(e)[:220]))

    try:
        sign_in(page, BASE, email, password)
        log("Signed in.")
        page.goto(f"{BASE}{AFK_PAGE}", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("text=Balance", timeout=25000)
        except Exception:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("text=Balance", timeout=20000)
        page.wait_for_timeout(10000)  # hydration

        inv = page.evaluate(INVENTORY_JS)
        body = page.locator("body").inner_text()
        m = re.search(r"Balance\s+(\d+)", body)
        b0 = int(m.group(1)) if m else None
        with open("mcwh_afk_inventory.txt", "w") as f:
            f.write("URL: %s\nUSER-AGENT: %s\n\n== BODY TEXT ==\n%s\n\n== ELEMENTS (%d) ==\n" %
                    (page.url, page.evaluate("() => navigator.userAgent"), body[:8000], len(inv)))
            for e in inv:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        page.screenshot(path="mcwh_afk_page.png", full_page=True)
        log(f"inventory done: {len(inv)} elements, balance={b0}")
        print("===== BODY TEXT =====")
        print(body[:2500])

        # —— 空闲 180s: 抓记账网络流 ——
        t0 = time.time()
        while time.time() - t0 < 180:
            page.wait_for_timeout(5000)
        body2 = page.locator("body").inner_text()
        m = re.search(r"Balance\s+(\d+)", body2)
        b1 = int(m.group(1)) if m else None
        log(f"balance after 180s idle: {b1} (delta={None if (b0 is None or b1 is None) else b1 - b0})")
        with open("mcwh_afk_network.txt", "w") as f:
            f.write("== requests during 180s idle (status method url) ==\n")
            f.write("\n".join(reqs) + "\n\n== CONSOLE ==\n" + "\n".join(console[-100:]))
        log(f"network capture: {len(reqs)} responses")

        # —— reload 一次再观察 90s ——
        reqs.clear()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
        body3 = page.locator("body").inner_text()
        m = re.search(r"Balance\s+(\d+)", body3)
        b2 = int(m.group(1)) if m else None
        page.wait_for_timeout(90000)
        body4 = page.locator("body").inner_text()
        m = re.search(r"Balance\s+(\d+)", body4)
        b3 = int(m.group(1)) if m else None
        log(f"reload: balance {b2} -> after 90s {b3}")
        with open("mcwh_afk_network2.txt", "w") as f:
            f.write("== requests during reload+90s ==\n")
            f.write("\n".join(reqs) + "\n\n== CONSOLE ==\n" + "\n".join(console[-100:]))
        page.screenshot(path="mcwh_afk_after.png", full_page=True)
        log("EXPLORE_DONE")
        return 0
    finally:
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
