"""Načte sdílený odkaz na trasu (Mapy.com / Mapy.cz) v neviditelném prohlížeči a vypíše spojení jako JSON.

Mapy.com při sdílení posílají jen odkaz; spojení si stránka počítá až v prohlížeči (JavaScript),
proto ji tu otevřeme stejně jako telefon a přečteme zobrazené spojení (a několik dalších).

Použití:  python tools/render_route.py URL [počet_spojení]
Spouští se jako samostatný proces – prohlížeč po skončení uvolní paměť a hlavní služba zůstane malá.
"""
import json
import sys
import time

EXTRACT_JS = r"""
() => {
  const box = document.querySelector('.route-item-pubt');
  if (!box) return null;
  const h2 = box.querySelector('h2');
  const legs = [];
  box.querySelectorAll('ul > li').forEach(li => {
    const cls = (li.className || '').trim();
    if (/pedestrian|walk/.test(cls)) return;
    const h3 = li.querySelector('h3');
    const dep = li.querySelector('p.departure'), arr = li.querySelector('p.arrival');
    const st = p => p ? ((p.querySelector('a, .arrival-departure') || p).textContent || '').trim() : '';
    const tm = p => { const t = p && p.querySelector('.time'); return t ? [t.textContent.trim(), t.getAttribute('title') || ''] : ['', '']; };
    legs.push({kind: cls, line: h3 ? (h3.getAttribute('title') || h3.textContent || '').trim() : '',
               from: st(dep), to: st(arr), dep: tm(dep)[0], dep_full: tm(dep)[1], arr: tm(arr)[0], arr_full: tm(arr)[1]});
  });
  return {title: h2 ? (h2.getAttribute('title') || h2.textContent || '').trim() : '', legs};
}
"""


def main():
    url = sys.argv[1]
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    deadline = time.time() + float(sys.argv[3] if len(sys.argv) > 3 else 35)
    from playwright.sync_api import sync_playwright

    out = {"url": url, "connections": [], "error": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                                          "--js-flags=--max-old-space-size=256"])
        ctx = browser.new_context(locale="cs-CZ", timezone_id="Europe/Prague", viewport={"width": 420, "height": 900},
                                  user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")
        # obrázky, písma a mapové dlaždice nepotřebujeme – rychlejší načtení a méně paměti
        ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            out["final_url"] = page.url
            # souhlas s cookies (Seznam CMP) – když se objeví, odsouhlasíme ho, jinak může blokovat stránku
            for sel in ('[data-testid="cw-button-agree-with-ads"]', 'button:has-text("Souhlasím")',
                        'button:has-text("Accept")', 'button:has-text("Akzeptieren")'):
                try:
                    page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    pass
            page.wait_for_selector(".route-item-pubt ul > li", timeout=max(3000, int((deadline - time.time()) * 1000)))
            last = None
            while len(out["connections"]) < want and time.time() < deadline:
                con = page.evaluate(EXTRACT_JS)
                if not con or not con["legs"] or (last and con["title"] == last):
                    break
                out["connections"].append(con)
                last = con["title"]
                nxt = page.locator(".route-item-pubt .paging-pt button.next")
                if not nxt.count():
                    break
                nxt.first.click(timeout=3000)
                try:
                    page.wait_for_function(
                        "t => { const h = document.querySelector('.route-item-pubt h2');"
                        " return h && (h.getAttribute('title') || h.textContent).trim() !== t; }",
                        arg=last, timeout=max(1000, int(min(8, deadline - time.time()) * 1000)))
                except Exception:
                    break
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            browser.close()
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
