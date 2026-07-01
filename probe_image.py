#!/usr/bin/env python3
"""
Probe: figure out how to fetch a poster from i.animepahe.pw without a 403.
Run this on your own machine (same one where the app runs), let it clear the
Cloudflare check in the browser window if it appears, and it will try several
methods and print which ones succeed. Paste the output back.
"""
import sys, os, time, base64
from pathlib import Path

try:
    from patchright.sync_api import sync_playwright
except Exception:
    from playwright.sync_api import sync_playwright

BASE_URL = "https://animepahe.pw"
PROFILE_DIR = Path.home() / ".config" / "animepahe-dl" / "chromium-profile"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def main():
    pw = sync_playwright().start()
    ctx = None
    for channel in ("chrome", "chromium"):
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR), headless=False, channel=channel,
                args=["--disable-blink-features=AutomationControlled"])
            print(f"launched channel={channel}")
            break
        except Exception as e:
            print(f"channel {channel} failed: {e}")
    if not ctx:
        print("could not launch browser"); return

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    print("opening animepahe, solve the check if the window shows one…")
    page.goto(BASE_URL, wait_until="domcontentloaded")
    # give you time to solve any challenge
    for _ in range(30):
        if "Just a moment" not in (page.title() or ""):
            break
        time.sleep(1)
    print("page title:", page.title())

    # Grab a real poster URL from the homepage / search.
    page.goto(BASE_URL + "/api?m=search&q=demon slayer", wait_until="domcontentloaded")
    import json, re
    body = page.evaluate("() => document.body.innerText")
    try:
        data = json.loads(body)
        poster = data["data"][0]["poster"]
    except Exception:
        m = re.search(r'https://i\.animepahe\.pw/uploads/posters/\S+?\.(?:jpg|webp|png)', body)
        poster = m.group(0) if m else None
    print("test poster:", poster)
    if not poster:
        print("could not find a poster url to test"); ctx.close(); return

    # Collect cookies
    cookies = ctx.cookies()
    cookie_hdr = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    print(f"\n{len(cookies)} cookies in context; domains:",
          sorted({c['domain'] for c in cookies}))

    def report(name, ok, detail=""):
        print(f"  [{'OK ' if ok else 'FAIL'}] {name}  {detail}")

    print("\n=== METHOD TESTS ===")

    # 1) context.request.get WITHOUT cookies/referer
    try:
        r = ctx.request.get(poster, timeout=15000)
        report("ctx.request bare", r.ok, f"status={r.status}")
    except Exception as e:
        report("ctx.request bare", False, str(e)[:60])

    # 2) context.request.get WITH referer only
    try:
        r = ctx.request.get(poster, headers={"Referer": BASE_URL + "/"}, timeout=15000)
        report("ctx.request +referer", r.ok, f"status={r.status}")
    except Exception as e:
        report("ctx.request +referer", False, str(e)[:60])

    # 3) context.request.get WITH referer + cookies + UA
    try:
        r = ctx.request.get(poster, headers={
            "Referer": BASE_URL + "/", "User-Agent": UA,
            "Cookie": cookie_hdr}, timeout=15000)
        report("ctx.request +referer+cookies+UA", r.ok, f"status={r.status}")
    except Exception as e:
        report("ctx.request +referer+cookies+UA", False, str(e)[:60])

    # 4) navigate the page directly to the image (browsers can view images)
    try:
        resp = page.goto(poster, wait_until="domcontentloaded", timeout=15000)
        report("page.goto image", bool(resp and resp.ok),
               f"status={resp.status if resp else '?'}")
        # 4a) after navigating, the response body IS the image bytes — read them
        if resp and resp.ok:
            try:
                body = resp.body()
                report("  -> resp.body() bytes", bool(body) and len(body) > 100,
                       f"len={len(body) if body else 0}")
            except Exception as e:
                report("  -> resp.body() bytes", False, str(e)[:60])
            # 4b) read the rendered <img> via canvas (same-origin now, so no CORS)
            try:
                res = page.evaluate("""async () => {
                    const img = document.querySelector('img');
                    if (!img) return {ok:false, err:'no img element'};
                    await img.decode().catch(()=>{});
                    const c = document.createElement('canvas');
                    c.width = img.naturalWidth; c.height = img.naturalHeight;
                    c.getContext('2d').drawImage(img,0,0);
                    return {ok:true, len:c.toDataURL('image/png').length,
                            w:img.naturalWidth, h:img.naturalHeight};
                }""")
                report("  -> rendered <img> canvas", res.get("ok"), str(res))
            except Exception as e:
                report("  -> rendered <img> canvas", False, str(e)[:60])
        page.goto(BASE_URL, wait_until="domcontentloaded")  # back
    except Exception as e:
        report("page.goto image", False, str(e)[:60])

    # 5) in-page fetch (expected to fail on CORP, but let's confirm the error)
    try:
        res = page.evaluate("""async (u) => {
            try { const r = await fetch(u); return {ok:r.ok, status:r.status}; }
            catch(e){ return {ok:false, err:String(e)}; }
        }""", poster)
        report("in-page fetch()", res.get("ok"), str(res))
    except Exception as e:
        report("in-page fetch()", False, str(e)[:60])

    # 6) open image in a NEW tab via <img> and read via canvas (needs CORS)
    try:
        res = page.evaluate("""async (u) => {
            return await new Promise(res => {
                const img = new Image();
                img.crossOrigin = 'anonymous';
                img.onload = () => {
                    try {
                        const c = document.createElement('canvas');
                        c.width = img.width; c.height = img.height;
                        c.getContext('2d').drawImage(img,0,0);
                        res({ok:true, len:c.toDataURL('image/png').length});
                    } catch(e){ res({ok:false, err:'canvas '+String(e)}); }
                };
                img.onerror = () => res({ok:false, err:'img onerror'});
                img.src = u;
                setTimeout(()=>res({ok:false, err:'timeout'}), 8000);
            });
        }""", poster)
        report("<img> crossOrigin+canvas", res.get("ok"), str(res))
    except Exception as e:
        report("<img> crossOrigin+canvas", False, str(e)[:60])

    print("\nDone with image tests.")

    # ============ MP4 DOWNLOAD TEST ============
    print("\n=== MP4 DOWNLOAD TEST (resolve one episode) ===")
    try:
        import urllib.request
        page.goto(BASE_URL + "/api?m=search&q=demon slayer", wait_until="domcontentloaded")
        d = json.loads(page.evaluate("() => document.body.innerText"))
        sess = d["data"][0]["session"]
        page.goto(f"{BASE_URL}/api?m=release&id={sess}&sort=episode_asc&page=1",
                  wait_until="domcontentloaded")
        rel = json.loads(page.evaluate("() => document.body.innerText"))
        ep_session = rel["data"][0]["session"]
        play_url = f"{BASE_URL}/play/{sess}/{ep_session}"
        print("play page:", play_url)
        page.goto(play_url, wait_until="domcontentloaded")
        html = page.content()
        pm = re.search(r'https://pahe\.win/\w+', html)
        if not pm:
            print("  no pahe.win link found, skipping mp4 test")
        else:
            print("  pahe.win:", pm.group(0))
            page.goto(pm.group(0), wait_until="domcontentloaded", timeout=20000)
            time.sleep(1.5)
            # embedded-kwik extraction (like the real app)
            h2 = page.content()
            km = re.search(r'https://kwik\.\w+/f/\w+', h2)
            if km:
                print("  kwik:", km.group(0))
                page.goto(km.group(0), wait_until="domcontentloaded", timeout=20000)
                time.sleep(1)
            else:
                print("  no embedded kwik link found in pahe.win page")

            # METHOD A: Playwright browser download via save_as (real browser nav)
            import tempfile
            got_url = None
            try:
                with page.expect_download(timeout=30000) as dl:
                    page.evaluate("(document.querySelector('form')||{submit(){}}).submit()")
                got_url = dl.value.url
                print("  mp4 url:", got_url[:80])
                # save a small part to confirm it actually downloads via browser
                test_path = os.path.join(tempfile.gettempdir(), "probe_test.mp4")
                dl.value.save_as(test_path)
                sz = os.path.getsize(test_path) if os.path.exists(test_path) else 0
                report("Playwright save_as (full browser download)", sz > 100000, f"bytes={sz}")
                try: os.remove(test_path)
                except Exception: pass
            except Exception as e:
                report("Playwright save_as", False, str(e)[:70])

            # If we got the url, also test urllib routes for comparison
            if got_url:
                cookie_hdr = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
                try:
                    req = urllib.request.Request(got_url, headers={"User-Agent": UA,
                        "Referer": "https://kwik.cx/"})
                    r = urllib.request.urlopen(req, timeout=20); r.read(65536); r.close()
                    report("urllib +referer", True, f"status={r.status}")
                except Exception as e:
                    report("urllib +referer", False, str(e)[:70])
                try:
                    req = urllib.request.Request(got_url, headers={"User-Agent": UA,
                        "Referer": "https://kwik.cx/", "Cookie": cookie_hdr})
                    r = urllib.request.urlopen(req, timeout=20); r.read(65536); r.close()
                    report("urllib +referer+cookies", True, f"status={r.status}")
                except Exception as e:
                    report("urllib +referer+cookies", False, str(e)[:70])
    except Exception as e:
        print("  mp4 test error:", str(e)[:120])

    print("\nDone. Paste all of the above (from METHOD TESTS) back.")
    ctx.close()
    pw.stop()


if __name__ == "__main__":
    main()
