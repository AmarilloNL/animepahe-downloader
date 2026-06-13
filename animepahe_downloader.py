#!/usr/bin/env python3
"""
AnimePahe Batch Downloader
Search by name → pick a result → select episodes → download.

Copyright (C) 2026  AmarilloNL

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version. See the LICENSE file for details.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

For educational and personal use only. Respect AnimePahe's Terms of
Service and the copyright laws in your country.

Requires:
  sudo pacman -S python-beautifulsoup4 tk
  python -m pip install patchright --break-system-packages
  python -m patchright install chromium

How it works:
  AnimePahe and Kwik are behind Cloudflare, which detects normal Playwright and
  makes the "Verify you are human" checkbox loop forever. This tool uses
  Patchright (a stealth-patched Playwright) so the challenge clears on a real
  human click. It drives one persistent Chromium window with a saved profile,
  so you usually only solve the check once. When the window shows the Cloudflare
  checkbox, click it; the app continues automatically once you're through.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
from bs4 import BeautifulSoup
import re, os, time, json
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL = "https://animepahe.pw"
API_URL  = f"{BASE_URL}/api"
HEADERS  = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
    "Referer":         BASE_URL,
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With":"XMLHttpRequest",
}

# ── Palette ───────────────────────────────────────────────────────────────────
BG       = "#0b0c10"
SURFACE  = "#13141a"
CARD     = "#1c1d26"
BORDER   = "#2a2b38"
ROSE     = "#f472b6"
ROSE_DIM = "#9d174d"
TEAL     = "#2dd4bf"
TEXT     = "#f1f5f9"
SUBTEXT  = "#94a3b8"
MUTED    = "#475569"
SUCCESS  = "#34d399"
ERROR    = "#fb7185"
WARN     = "#fbbf24"
FONT_UI  = ("Segoe UI", 10)
FONT_MONO= ("JetBrains Mono", 9) if os.path.exists("/usr/share/fonts/JetBrains") \
           else ("Consolas", 9)

# ── Persistent headed-browser engine ─────────────────────────────────────────
# AnimePahe (DDoS-Guard) and Kwik (Cloudflare) require a real browser fingerprint.
# We keep ONE persistent Chromium context alive for the whole session and route
# every request through it. The profile is stored on disk so challenges only need
# solving once. All Playwright calls run on a single dedicated thread, because
# the sync API is not safe to call from arbitrary threads.

import queue
from urllib.parse import urlencode, urlparse

PROFILE_DIR = Path.home() / ".config" / "animepahe-dl" / "chromium-profile"
LOG_PATH    = Path.home() / ".config" / "animepahe-dl" / "engine.log"

def _log(msg: str):
    """Append a timestamped line to the engine log AND print it."""
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

_engine: "BrowserEngine | None" = None


class RateLimited(Exception):
    """Raised when AnimePahe returns HTTP 429 (too many requests)."""
    pass


class BrowserEngine:
    """
    Owns a persistent Playwright Chromium context on its own thread.
    Public methods are thread-safe: they post a job to the engine thread
    and block for the result.
    """

    def __init__(self, headless: bool = False, status_cb=None, on_challenge=None):
        self._headless  = headless
        self._status_cb = status_cb
        self._on_challenge = on_challenge   # called when a visible re-solve is needed
        self._current_headless = False      # we always start visible
        self._minimized = False             # window minimized state
        self._jobs:    "queue.Queue" = queue.Queue()
        self._ready    = threading.Event()
        self._err      = None
        self._ctx      = None
        self._pw       = None
        self._page     = None
        self._stealth  = False
        self._thread   = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- engine thread ----
    def _run(self):
        _log("ENGINE: thread started")
        try:
            try:
                from patchright.sync_api import sync_playwright
                self._stealth = True
                _log("ENGINE: using patchright")
            except ImportError:
                from playwright.sync_api import sync_playwright
                self._stealth = False
                _log("ENGINE: using plain playwright (patchright not found)")

            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                p = PROFILE_DIR / lock
                try:
                    if p.exists() or p.is_symlink():
                        p.unlink()
                        _log(f"ENGINE: removed stale lock {lock}")
                except OSError:
                    pass

            _log("ENGINE: starting sync_playwright()")
            self._pw = sync_playwright().start()
            _log("ENGINE: sync_playwright started")

            # Start visible so the user can solve the captcha.
            self._launch_ctx(headless=False)
            _log(f"ENGINE: context ready, {len(self._ctx.pages)} page(s) open")
        except Exception as e:
            _log(f"ENGINE: LAUNCH FAILED: {type(e).__name__}: {e}")
            self._err = e
            self._ready.set()
            return

        self._ready.set()
        _log("ENGINE: ready, entering job loop")
        # Job loop
        while True:
            fn, args, kwargs, result_q = self._jobs.get()
            if fn is None:  # shutdown sentinel
                _log("ENGINE: shutdown sentinel received")
                break
            _log(f"ENGINE: running job {fn.__name__}")
            try:
                result_q.put(("ok", fn(*args, **kwargs)))
                _log(f"ENGINE: job {fn.__name__} returned OK")
            except Exception as e:
                _log(f"ENGINE: job {fn.__name__} raised {type(e).__name__}: {str(e)[:150]}")
                result_q.put(("err", e))
        try:
            self._ctx.close()
            self._pw.stop()
        except Exception:
            pass

    def _call(self, fn, *args, **kwargs):
        """Run fn on the engine thread, block for its result."""
        self._ready.wait()
        if self._err:
            raise self._err
        result_q: "queue.Queue" = queue.Queue()
        self._jobs.put((fn, args, kwargs, result_q))
        status, value = result_q.get()
        if status == "err":
            raise value
        return value

    # ---- operations (run on engine thread) ----
    def _challenge_active(self, page) -> bool:
        """Heuristic: is a Cloudflare/DDoS-Guard challenge currently showing?"""
        # pahe.win shows its own "Just a moment… / Continue" gate that looks like
        # a challenge by title but is just a clickable redirect — not Cloudflare.
        # Treat it as NOT a challenge so the resolve logic clicks through it.
        try:
            if "pahe.win" in (page.url or ""):
                return False
        except Exception:
            pass
        t = (page.title() or "").lower()
        if any(k in t for k in ("just a moment", "ddos", "checking", "attention required",
                                 "verify", "moment…", "access denied")):
            return True
        # DOM markers for Cloudflare interstitial / Turnstile widget
        try:
            if page.query_selector(
                "#challenge-form, #challenge-running, #cf-challenge-running, "
                "iframe[src*='challenges.cloudflare'], div.cf-turnstile, "
                "iframe[title*='Cloudflare'], #turnstile-wrapper"):
                return True
        except Exception:
            pass
        # A Cloudflare 'Just a moment' page often has very little body text.
        try:
            body_txt = (page.inner_text("body") or "").strip().lower()
            if "verify you are human" in body_txt or "needs to review the security" in body_txt:
                return True
        except Exception:
            pass
        return False

    def _is_blocked(self, status: int, page) -> bool:
        """True if we got a 403/503 challenge wall rather than real content."""
        if status in (403, 503):
            return True
        return self._challenge_active(page)

    def _page_alive(self) -> bool:
        p = getattr(self, "_page", None)
        if p is None:
            return False
        try:
            return not p.is_closed()
        except Exception:
            return False

    def _main_page(self):
        """
        Return the persistent main page, recreating it if the user (or a crash)
        closed the tab. If the whole context is gone, relaunch it.
        """
        if self._page_alive():
            return self._page

        # Page is closed/missing — try to reuse or open a fresh tab.
        _log("MAINPAGE: page not alive, (re)creating")
        try:
            # Is the context still usable?
            pages = self._ctx.pages
            if pages:
                self._page = pages[0]
                if self._page.is_closed():
                    self._page = self._ctx.new_page()
            else:
                self._page = self._ctx.new_page()
            _log(f"MAINPAGE: page ready url={self._page.url!r}")
            return self._page
        except Exception as e:
            # Context itself is dead — relaunch the whole browser.
            _log(f"MAINPAGE: context dead ({type(e).__name__}), relaunching browser")
            self._relaunch()
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            return self._page

    def _launch_ctx(self, headless: bool):
        """(Re)launch the persistent context in the given headed/headless mode.
        The profile on disk carries Cloudflare clearance across relaunches."""
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass

        stable_args = ["--disable-blink-features=AutomationControlled"]
        if self._stealth:
            last_err = None
            for channel in ("chrome", "chromium", None):
                try:
                    _log(f"LAUNCH: channel={channel!r} headless={headless}")
                    kwargs = dict(user_data_dir=str(PROFILE_DIR), headless=headless,
                                  viewport={"width": 1280, "height": 800}, args=stable_args)
                    if channel:
                        kwargs["channel"] = channel
                    self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
                    last_err = None
                    _log(f"LAUNCH: OK channel={channel!r} headless={headless}")
                    break
                except Exception as e:
                    last_err = e
                    _log(f"LAUNCH: channel={channel!r} failed {type(e).__name__}")
            if last_err:
                raise last_err
        else:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR), headless=headless,
                viewport={"width": 1280, "height": 800},
                user_agent=HEADERS["User-Agent"], args=stable_args)
            self._ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self._current_headless = headless
        self._page = None

    def _relaunch(self):
        """Relaunch in the current mode (used when the context died)."""
        self._launch_ctx(self._current_headless)
        _log("MAINPAGE: relaunched context")

    def _window_state(self, state: str):
        """Set the browser window state via CDP: 'minimized' or 'normal'.
        Keeps the SAME browser alive (no relaunch), so Cloudflare clearance
        is never lost."""
        try:
            page = self._main_page()
            session = self._ctx.new_cdp_session(page)
            ids = session.send("Browser.getWindowForTarget")
            win_id = ids["windowId"]
            session.send("Browser.setWindowBounds", {
                "windowId": win_id,
                "bounds": {"windowState": state},
            })
            _log(f"WINDOW: set state={state}")
            try: session.detach()
            except Exception: pass
            return True
        except Exception as e:
            _log(f"WINDOW: set state={state} failed {type(e).__name__}: {str(e)[:100]}")
            return False

    def _go_headless(self):
        """Hide the browser by minimizing it (same process stays alive)."""
        if self._window_state("minimized"):
            self._minimized = True

    def _go_visible(self):
        """Restore + focus the browser window so the user can re-solve."""
        self._window_state("normal")
        self._minimized = False
        try:
            self._main_page().bring_to_front()
        except Exception:
            pass

    def _goto(self, page, url, attempts=3):
        """Navigate with retries; raise the last error if all fail.

        Uses wait_until='commit' (fires as soon as the response starts) instead
        of 'domcontentloaded', which can hang on the engine worker thread when
        the page keeps loading sub-resources or a challenge script spins.
        """
        last = None
        for i in range(attempts):
            try:
                _log(f"GOTO[{i}]: page.goto({url}) start")
                resp = page.goto(url, wait_until="commit", timeout=30000)
                _log(f"GOTO[{i}]: committed status={resp.status if resp else '?'} url={page.url!r}")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                    _log(f"GOTO[{i}]: domcontentloaded ok")
                except Exception as e:
                    _log(f"GOTO[{i}]: dcl wait skipped {type(e).__name__}")
                return resp
            except Exception as e:
                last = e
                _log(f"GOTO[{i}]: FAILED {type(e).__name__}: {str(e)[:120]}")
                page.wait_for_timeout(1500)
        if last:
            raise last

    def _solve(self, url: str) -> None:
        """
        Warm up: navigate to the site. If Cloudflare blocks us (403 / challenge
        page), surface the window so the user can click 'Verify you are human',
        and poll until the page clears. Returns once we have real access.
        """
        _log("SOLVE: start")
        page = self._main_page()
        try:
            page.bring_to_front()
        except Exception:
            pass

        resp = self._goto(page, url)
        status = resp.status if resp else 0
        blocked = self._is_blocked(status, page)
        _log(f"SOLVE: after goto status={status} blocked={blocked} title={page.title()!r}")

        # If we're blocked, wait for the user to solve the visible challenge.
        if blocked:
            try:
                page.bring_to_front()
            except Exception:
                pass
            deadline = time.time() + 180  # up to 3 minutes
            while time.time() < deadline:
                page.wait_for_timeout(1500)
                if not self._challenge_active(page):
                    _log("SOLVE: challenge cleared, confirming with reload")
                    try:
                        r = self._goto(page, url)
                        if not self._is_blocked(r.status if r else 0, page):
                            _log("SOLVE: confirmed cleared ✓")
                            return
                    except Exception as e:
                        _log(f"SOLVE: confirm reload err {type(e).__name__}")
            _log("SOLVE: timed out")
        else:
            _log("SOLVE: not blocked")
        # Either cleared, or timed out — let downstream calls report status.

    def _ensure_clear(self, page):
        """If a challenge appears mid-session, surface the window and wait."""
        if not self._challenge_active(page):
            return
        try: page.bring_to_front()
        except Exception: pass
        deadline = time.time() + 180
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            if not self._challenge_active(page):
                return

    def _get(self, url: str) -> tuple[int, str]:
        page = self._main_page()
        resp = self._goto(page, url)
        status = resp.status if resp else 0
        # HTTP 429 = rate limited by AnimePahe. Surface it clearly so the
        # download loop can back off instead of hammering and getting the
        # browser context killed.
        if status == 429:
            _log("GET: 429 rate limited")
            raise RateLimited("AnimePahe rate limit (HTTP 429)")
        self._ensure_clear(page)
        body = page.content()
        pre = page.query_selector("pre")
        if pre:
            body = pre.inner_text()
        return status, body

    def _api_fetch(self, url: str) -> tuple[int, str]:
        """Fetch via in-page fetch() on the cleared main page (carries cookies)."""
        for attempt in range(3):
            page = self._main_page()
            try:
                # The fetch() must run from a cleared animepahe origin.
                if not (page.url or "").startswith(BASE_URL):
                    r = self._goto(page, BASE_URL)
                    if self._is_blocked(r.status if r else 0, page):
                        self._handle_block()
                        continue
                else:
                    self._ensure_clear(page)

                result = page.evaluate(
                    """async (u) => {
                        const r = await fetch(u, {headers: {'X-Requested-With':'XMLHttpRequest'}});
                        return {status: r.status, text: await r.text()};
                    }""", url)

                # A 403 here means Cloudflare re-challenged (common after going
                # headless). Surface the browser so the user can re-solve.
                if result["status"] in (403, 503):
                    _log(f"API_FETCH: got {result['status']} — challenge returned")
                    self._handle_block()
                    continue
                return result["status"], result["text"]
            except Exception as e:
                name = type(e).__name__
                if "TargetClosed" in name or "closed" in str(e).lower():
                    _log(f"API_FETCH: page closed ({name}), recovering (attempt {attempt})")
                    self._page = None
                    continue
                raise
        raise Exception("api_fetch: could not get past the challenge after retries.")

    def _handle_block(self):
        """Cloudflare is blocking us. Restore the window, signal the GUI, wait
        for the user to clear it, then minimize again."""
        if self._on_challenge:
            try: self._on_challenge()
            except Exception: pass
        was_minimized = self._minimized
        self._go_visible()
        page = self._main_page()
        try:
            self._goto(page, BASE_URL)
            page.bring_to_front()
        except Exception:
            pass
        # Wait for the user to clear it (up to 3 min)
        deadline = time.time() + 180
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            if not self._challenge_active(page):
                try:
                    r = self._goto(page, BASE_URL)
                    if not self._is_blocked(r.status if r else 0, page):
                        break
                except Exception:
                    pass
        # Re-minimize if it was minimized before.
        if was_minimized:
            self._go_headless()

    def _reassert_minimized(self):
        """If the window is supposed to be minimized, push it back. New tabs and
        navigations tend to raise/focus the window on Linux; this snaps it back
        so resolve tabs don't steal focus during background downloads."""
        if self._minimized:
            try:
                self._window_state("minimized")
            except Exception:
                pass

    def _resolve_download(self, start_url: str) -> "tuple[str, dict] | None":
        """
        Follow a pahe.win download link through to the final direct MP4.
        Chain: pahe.win/XXX  →  kwik.cx/f/<token>  →  (submit form)  →  MP4 CDN URL.
        Runs in a background tab; the window is kept minimized unless a Cloudflare
        check genuinely needs solving.
        """
        page = self._ctx.new_page()
        self._reassert_minimized()   # new tab tried to raise the window — push back
        try:
            _log(f"DLRES: opening {start_url}")
            opened = False
            for attempt in range(2):
                try:
                    page.goto(start_url, wait_until="commit", timeout=20000)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    opened = True
                    break
                except Exception as e:
                    _log(f"DLRES: pahe nav attempt {attempt} failed {type(e).__name__}")
                    page.wait_for_timeout(1500)
            if not opened:
                _log("DLRES: pahe.win navigation failed after retries")
                return None
            self._reassert_minimized()
            page.wait_for_timeout(800)

            # pahe.win shows a "Just a moment… / Continue" gate (NOT Cloudflare —
            # it just needs a click). Click through it first; only after we reach
            # kwik do we check for a real Cloudflare challenge.
            cur = page.url or ""
            if "pahe.win" in cur:
                for _ in range(4):
                    if "kwik." in (page.url or ""):
                        break
                    clicked = False
                    for sel in ("text=Continue", "a:has-text('Continue')",
                                "button:has-text('Continue')", "input[type=submit]",
                                "a.btn", "button.btn", "a[href*='kwik.']", "form button"):
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                _log(f"DLRES: clicking pahe.win '{sel}'")
                                el.click()
                                clicked = True
                                break
                        except Exception:
                            continue
                    # Wait for the redirect to kwik after clicking.
                    try:
                        page.wait_for_url(re.compile(r"kwik\."), timeout=12000)
                    except Exception:
                        page.wait_for_timeout(2000)
                    self._reassert_minimized()
                    if not clicked:
                        # No button found this round; maybe a meta-refresh is
                        # pending, or the kwik link is embedded in the HTML.
                        html = page.content()
                        km = re.search(r'https://kwik\.[a-z]+/f/[A-Za-z0-9]+', html)
                        if km:
                            _log(f"DLRES: pahe→kwik (embedded) {km.group(0)}")
                            try:
                                page.goto(km.group(0), wait_until="commit", timeout=20000)
                                page.wait_for_load_state("domcontentloaded", timeout=8000)
                            except Exception:
                                pass
                            break
                        page.wait_for_timeout(2000)

                if "pahe.win" in (page.url or ""):
                    _log("DLRES: could not get past pahe.win gate")
                    return None

            # Now we should be on kwik — handle its Cloudflare check if any.
            self._reassert_minimized()
            self._wait_clear(page)
            page.wait_for_timeout(800)

            page.wait_for_timeout(1000)
            kwik_url = page.url
            parsed = urlparse(kwik_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            _log(f"DLRES: now at {kwik_url}")

            # On the kwik /f/ page: submit the download form to get the MP4.
            direct = None
            try:
                page.wait_for_selector("form", timeout=15000)
            except Exception:
                pass

            # The MP4 may be revealed by submitting the form. Capture whichever
            # of these fires: a download event, a navigation, or a popup. Use a
            # generous timeout since Kwik can respond slowly when throttling.
            try:
                with page.expect_download(timeout=25000) as dl:
                    page.evaluate("(document.querySelector('form')||{submit(){}}).submit()")
                direct = dl.value.url
                _log(f"DLRES: got download url {direct[:80]}")
                self._reassert_minimized()
            except Exception:
                pass

            if not direct:
                # Look for a direct mp4 link in the page.
                html = page.content()
                mm = re.search(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', html)
                if mm:
                    direct = mm.group(0)
                    _log(f"DLRES: found mp4 in page {direct[:80]}")

            if not direct:
                # Check any popup tabs.
                for pg in self._ctx.pages:
                    if pg is not page and ".mp4" in (pg.url or ""):
                        direct = pg.url
                        break

            if direct and ".mp4" in direct:
                return direct, {"Referer": origin + "/"}
            _log("DLRES: no mp4 found")
            return None
        finally:
            try: page.close()
            except Exception: pass
            self._reassert_minimized()   # closing the tab can also refocus

    def _wait_clear(self, page, timeout=180):
        """
        Handle a Cloudflare check. Patchright usually clears the interstitial
        automatically within a few seconds without any user action, so we wait
        silently first and only surface the window if it's STILL blocked after
        the grace period. This keeps normal downloads fully in the background.
        """
        if not self._challenge_active(page):
            return

        # Grace period: wait for Patchright to auto-clear (no window popup).
        grace_deadline = time.time() + 8
        while time.time() < grace_deadline:
            page.wait_for_timeout(1000)
            if not self._challenge_active(page):
                _log("DLRES: challenge auto-cleared (no popup needed)")
                return

        # Still blocked — now we need the user. Surface the window.
        _log("DLRES: challenge persists, surfacing for user")
        if self._on_challenge:
            try: self._on_challenge()
            except Exception: pass
        self._window_state("normal")
        try: page.bring_to_front()
        except Exception: pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            if not self._challenge_active(page):
                # Re-minimize once cleared so we stay in the background.
                if self._minimized:
                    self._window_state("minimized")
                return

    def _download(self, url: str, dest_path: str, extra_headers: dict,
                  progress_cb, stop_event) -> bool:
        """Kept for compatibility; real downloading now happens in
        stream_download() off the engine thread. This just delegates."""
        return stream_download(url, dest_path, extra_headers, progress_cb, stop_event)

    # ---- public, thread-safe API ----
    def solve(self, url):                 return self._call(self._solve, url)
    def get(self, url):                   return self._call(self._get, url)
    def api_fetch(self, url):             return self._call(self._api_fetch, url)
    def resolve_download(self, url):      return self._call(self._resolve_download, url)
    def download(self, url, dest, hdrs, progress_cb=None, stop_event=None):
        return self._call(self._download, url, dest, hdrs, progress_cb, stop_event)
    def go_headless(self):                return self._call(self._go_headless)
    def go_visible(self):                 return self._call(self._go_visible)
    def is_headless(self):                return self._current_headless

    def shutdown(self):
        self._jobs.put((None, None, None, None))


def get_engine(headless: bool = False, status_cb=None, on_challenge=None) -> BrowserEngine:
    global _engine
    if _engine is None:
        if status_cb:
            status_cb("Launching browser… solve the 'Verify you are human' check in the window if it appears.")
        _engine = BrowserEngine(headless=headless, status_cb=status_cb, on_challenge=on_challenge)
        # Warm up: open AnimePahe and wait for the user to clear any challenge.
        _engine.solve(BASE_URL)
    return _engine


def pw_get(url: str, params: dict | None = None) -> str:
    if params:
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    status, text = get_engine().api_fetch(url)
    if status >= 400:
        raise Exception(f"HTTP {status} for {url}")
    return text


def stream_download(url: str, dest_path: str, extra_headers: dict,
                    progress_cb=None, stop_event=None) -> bool:
    """
    Stream a large file to disk in chunks. The CDN host (e.g. owocdn.top) is a
    plain file server needing only the right Referer/User-Agent. On a stalled
    connection (read timeout), reconnect and RESUME from the bytes already
    written using an HTTP Range request, rather than restarting the whole file.
    """
    import urllib.request

    referer = extra_headers.get("Referer", "https://kwik.cx/")
    tmp_path = dest_path + ".part"
    _log(f"DL: streaming {url[:80]}")

    max_attempts = 4
    total = 0
    for attempt in range(max_attempts):
        # How many bytes do we already have from a previous (stalled) attempt?
        have = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0

        headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Referer": referer,
            "Accept": "*/*",
        }
        if have:
            headers["Range"] = f"bytes={have}-"
            _log(f"DL: resuming from byte {have} (attempt {attempt})")

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                # Work out the full size. With a 206 partial response the
                # Content-Length is just the remaining bytes, so add what we have.
                clen = int(resp.headers.get("Content-Length", 0))
                if resp.status == 206:
                    total = have + clen
                elif clen:
                    total = clen
                    if have and resp.status == 200:
                        # Server ignored Range — restart from scratch.
                        have = 0
                _log(f"DL: status={resp.status} total={total}")

                mode = "ab" if (have and resp.status == 206) else "wb"
                done = have if mode == "ab" else 0
                with open(tmp_path, mode) as f:
                    while True:
                        if stop_event and stop_event.is_set():
                            _log("DL: stopped by user")
                            try: os.remove(tmp_path)
                            except OSError: pass
                            return False
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            progress_cb(min(1.0, done / total))

            # Completed a clean read. Verify size if we know the total.
            final = os.path.getsize(tmp_path)
            if total and final < total:
                _log(f"DL: short read {final}/{total}, will resume")
                continue   # connection ended early — resume
            os.replace(tmp_path, dest_path)
            _log(f"DL: done ({final} bytes)")
            if progress_cb:
                progress_cb(1.0)
            return True

        except Exception as e:
            _log(f"DL: attempt {attempt} failed {type(e).__name__}: {str(e)[:120]}")
            # Keep the .part file so the next attempt can resume from it.
            if attempt == max_attempts - 1:
                try: os.remove(tmp_path)
                except OSError: pass
                raise
            time.sleep(2)
    return False


def pw_download(url: str, dest_path: str, extra_headers: dict,
                progress_cb=None, stop_event=None) -> bool:
    """
    Download the direct MP4 from the CDN. Streams off the engine thread so the
    browser stays responsive and large files don't blow up memory.
    """
    return stream_download(url, dest_path, extra_headers,
                           progress_cb=progress_cb, stop_event=stop_event)

# ── API helpers ───────────────────────────────────────────────────────────────

def search_anime(query: str, status_cb=None) -> list[dict]:
    # Primary: JSON API via in-page fetch
    text = pw_get(API_URL, params={"m": "search", "q": query})
    print(f"[SEARCH] api response (first 300): {text[:300]!r}")
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped[:1] in ("{", "["):
        data  = json.loads(stripped)
        items = data if isinstance(data, list) else data.get("data", [])
        if items:
            return [{
                "id":       i.get("session", i.get("id", "")),
                "title":    i.get("title", "Unknown"),
                "year":     str(i.get("year", "")),
                "status":   i.get("status", ""),
                "episodes": i.get("episodes", "?"),
                "type":     i.get("type", ""),
            } for i in items]
        # Valid JSON but no matches — that's a genuine empty result
        return []

    # The API didn't return JSON (HTML challenge page, redirect, etc.).
    # Fall back to scraping the /anime search page.
    print(f"[SEARCH] API was not JSON, trying scrape fallback…")
    text = pw_get(f"{BASE_URL}/anime", params={"q": query})
    soup = BeautifulSoup(text, "html.parser")
    results = []
    for card in soup.select("div.col-sm-6.col-md-4.col-lg-3"):
        a = card.select_one("a[href*='/anime/']")
        if not a: continue
        m = re.search(r"/anime/([a-f0-9-]{36})", a.get("href",""))
        if not m: continue
        results.append({
            "id": m.group(1),
            "title": a.get("title") or a.get_text(strip=True),
            "year": "", "status": "", "episodes": "?", "type": "",
        })
    return results


def fetch_episodes(anime_id: str, progress_cb=None) -> list[dict]:
    episodes, page, total_pages = [], 1, 1
    while page <= total_pages:
        text = pw_get(API_URL, params={"m":"release","id":anime_id,"sort":"episode_asc","page":page})
        stripped = text.lstrip("\ufeff \t\r\n")
        data = json.loads(stripped) if stripped[:1] == "{" else {}
        total_pages = data.get("last_page", 1)
        for ep in data.get("data", []):
            episodes.append({
                "episode":  ep.get("episode", "?"),
                "title":    ep.get("title", ""),
                "session":  ep.get("session", ""),
                "anime_id": anime_id,
                "snapshot": ep.get("snapshot", ""),
            })
        if progress_cb: progress_cb(page, total_pages)
        page += 1
        time.sleep(0.3)
    return episodes


def get_download_options(anime_id: str, ep_session: str) -> tuple[list[dict], str]:
    """
    Scrape the play page's #pickDownload dropdown for the real download links.
    Returns (options, title) where each option is:
        {"url": pahe.win link, "quality": "1080", "audio": "eng"|"jpn",
         "size": "172MB", "label": full text}
    """
    eng = get_engine()
    title = ""
    play_url = f"{BASE_URL}/play/{anime_id}/{ep_session}"
    status, body = eng.get(play_url)
    options: list[dict] = []
    if status == 200:
        soup = BeautifulSoup(body, "html.parser")
        el = soup.select_one("h1")
        if el:
            raw = re.sub(r"\s*[-|–]\s*AnimePahe.*$", "", el.get_text(strip=True))
            if raw:
                title = raw

        # The download links live in #pickDownload as <a> dropdown-items.
        container = soup.select_one("#pickDownload")
        anchors = container.select("a") if container else []
        # Fallback: any anchor pointing at pahe.win / kwik
        if not anchors:
            anchors = [a for a in soup.select("a") if re.search(r"pahe\.win|kwik\.", a.get("href",""))]

        for a in anchors:
            href = a.get("href", "")
            if not re.search(r"pahe\.win|kwik\.", href):
                continue
            label = a.get_text(" ", strip=True)
            qm = re.search(r"(\d{3,4})p", label)
            sm = re.search(r"\(([\d.]+\s*[MG]B)\)", label)
            audio = "eng" if re.search(r"\beng\b", label, re.I) else "jpn"
            options.append({
                "url": href,
                "quality": qm.group(1) if qm else "?",
                "audio": audio,
                "size": sm.group(1) if sm else "",
                "label": label,
            })
    return options, title


def pick_download_option(options: list[dict], quality_pref: str = "1080",
                         audio_pref: str = "jpn") -> dict | None:
    """Choose the best matching download option for the user's prefs."""
    if not options:
        return None
    # Exact quality + audio
    for o in options:
        if o["quality"] == quality_pref and o["audio"] == audio_pref:
            return o
    # Right quality, any audio
    for o in options:
        if o["quality"] == quality_pref:
            return o
    # Right audio, highest quality available
    aud = [o for o in options if o["audio"] == audio_pref]
    pool = aud or options
    def q(o):
        try: return int(o["quality"])
        except ValueError: return 0
    return max(pool, key=q)


def resolve_download(pahe_or_kwik_url: str) -> tuple[str, dict] | None:
    """
    Follow a pahe.win (or direct kwik) download link through to the final MP4.
    Runs in the persistent browser so Cloudflare/clearance is handled.
    Returns (mp4_url, {headers}) or None.
    """
    return get_engine().resolve_download(pahe_or_kwik_url)


# ── Custom checkbox list widget ───────────────────────────────────────────────

class CheckList(tk.Frame):
    """Scrollable list of checkboxes with episode data."""

    ROW_H   = 32
    CB_SIZE = 14

    def __init__(self, master, **kw):
        super().__init__(master, bg=CARD, **kw)
        self._items: list[dict] = []   # {"ep":…, "title":…, "var": BooleanVar, …}
        self._row_frames: list[tk.Frame] = []

        self._canvas = tk.Canvas(self, bg=CARD, highlightthickness=0, bd=0)
        self._sb     = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._sb.set)
        self._sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._inner = tk.Frame(self._canvas, bg=CARD)
        self._win   = self._canvas.create_window((0,0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", self._on_inner_cfg)
        self._canvas.bind("<Configure>", self._on_canvas_cfg)
        self._canvas.bind("<MouseWheel>", self._on_scroll)
        self._canvas.bind("<Button-4>",   lambda e: self._canvas.yview_scroll(-1,"units"))
        self._canvas.bind("<Button-5>",   lambda e: self._canvas.yview_scroll( 1,"units"))

    def _on_inner_cfg(self, _=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_cfg(self, e):
        self._canvas.itemconfig(self._win, width=e.width)

    def _on_scroll(self, e):
        self._canvas.yview_scroll(int(-1*(e.delta/120)), "units")

    def clear(self):
        for f in self._row_frames: f.destroy()
        self._row_frames.clear()
        self._items.clear()

    def add_episode(self, ep_num, title, session, anime_id):
        var = tk.BooleanVar(value=False)
        row = tk.Frame(self._inner, bg=CARD, cursor="hand2")
        row.pack(fill="x", padx=4, pady=1)
        self._row_frames.append(row)

        # Custom checkbox canvas
        cb_cnv = tk.Canvas(row, width=self.CB_SIZE, height=self.CB_SIZE,
                           bg=CARD, highlightthickness=0, bd=0)
        cb_cnv.pack(side="left", padx=(8,6), pady=8)

        ep_lbl = tk.Label(row, text=f"Episode {ep_num}",
                          font=("Segoe UI", 9, "bold"),
                          bg=CARD, fg=ROSE, width=12, anchor="w")
        ep_lbl.pack(side="left", padx=(0,8))

        title_lbl = tk.Label(row, text=title if title else "",
                             font=FONT_UI, bg=CARD, fg=TEXT, anchor="w")
        title_lbl.pack(side="left", fill="x", expand=True)

        item = {"ep": ep_num, "title": title, "session": session,
                "anime_id": anime_id, "var": var, "cb": cb_cnv, "row": row}
        self._items.append(item)

        def draw_cb(item=item):
            c = item["cb"]
            c.delete("all")
            if item["var"].get():
                c.create_rectangle(1,1,self.CB_SIZE-1,self.CB_SIZE-1,
                                   fill=ROSE, outline=ROSE)
                c.create_line(3,7,6,10,11,4, fill="white", width=2, capstyle="round", joinstyle="round")
            else:
                c.create_rectangle(1,1,self.CB_SIZE-1,self.CB_SIZE-1,
                                   fill="", outline=BORDER, width=1)

        def toggle(event=None, item=item):
            item["var"].set(not item["var"].get())
            draw_cb(item)
            self._update_row_bg(item)

        draw_cb(item)

        for widget in (row, cb_cnv, ep_lbl, title_lbl):
            widget.bind("<Button-1>", toggle)
            widget.bind("<Enter>",
                lambda e, r=row: r.config(bg="#212232") or
                    [w.config(bg="#212232") for w in r.winfo_children()])
            widget.bind("<Leave>",
                lambda e, r=row: r.config(bg=CARD) or
                    [w.config(bg=CARD) for w in r.winfo_children()])

    def _update_row_bg(self, item):
        bg = "#1a1b2e" if item["var"].get() else CARD
        item["row"].config(bg=bg)
        for w in item["row"].winfo_children():
            try: w.config(bg=bg)
            except Exception: pass

    def select_all(self):
        for item in self._items:
            item["var"].set(True)
            item["cb"].delete("all")
            item["cb"].create_rectangle(1,1,self.CB_SIZE-1,self.CB_SIZE-1, fill=ROSE, outline=ROSE)
            item["cb"].create_line(3,7,6,10,11,4, fill="white", width=2, capstyle="round", joinstyle="round")
            self._update_row_bg(item)

    def select_none(self):
        for item in self._items:
            item["var"].set(False)
            item["cb"].delete("all")
            item["cb"].create_rectangle(1,1,self.CB_SIZE-1,self.CB_SIZE-1, fill="", outline=BORDER, width=1)
            self._update_row_bg(item)

    def select_range(self, lo: float, hi: float):
        self.select_none()
        for item in self._items:
            try:
                if lo <= float(item["ep"]) <= hi:
                    item["var"].set(True)
                    c = item["cb"]
                    c.delete("all")
                    c.create_rectangle(1,1,self.CB_SIZE-1,self.CB_SIZE-1, fill=ROSE, outline=ROSE)
                    c.create_line(3,7,6,10,11,4, fill="white", width=2, capstyle="round", joinstyle="round")
                    self._update_row_bg(item)
            except (ValueError, TypeError):
                pass

    def get_selected(self) -> list[dict]:
        return [i for i in self._items if i["var"].get()]

    def count_selected(self) -> int:
        return sum(1 for i in self._items if i["var"].get())


# ── Main App ──────────────────────────────────────────────────────────────────

class AnimePaheApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AnimePahe Downloader")
        self.geometry("1120x720")
        self.minsize(900, 560)
        self.configure(bg=BG)

        self._search_results: list[dict] = []
        self._episodes:       list[dict] = []
        self._stop_event = threading.Event()
        self._engine_ready = False
        # Download preferences (changeable in the UI)
        self._quality_var = tk.StringVar(value="1080")
        self._audio_var   = tk.StringVar(value="jpn")
        self._season_var  = tk.StringVar(value="1")
        self._jellyfin_var = tk.BooleanVar(value=True)

        self._setup_styles()
        self._build_ui()
        # Warm up the browser engine in the background on startup
        self.after(300, self._startup_engine)

    def _setup_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TScrollbar", troughcolor=SURFACE, background=BORDER,
                    bordercolor=SURFACE, arrowcolor=SUBTEXT)
        s.configure("Rose.Horizontal.TProgressbar",
                    troughcolor=SURFACE, background=ROSE,
                    bordercolor=SURFACE, lightcolor=ROSE, darkcolor=ROSE_DIM)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ── Top bar ───────────────────────────────────────────────────────────
        top = tk.Frame(self, bg=SURFACE, pady=0)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        # Logo / title
        logo = tk.Frame(top, bg=ROSE_DIM, padx=16, pady=14)
        logo.grid(row=0, column=0)
        tk.Label(logo, text="🌸 pahe", font=("Segoe UI", 13, "bold"),
                 bg=ROSE_DIM, fg="white").pack()

        # Engine status indicator in top bar
        btn_wrap = tk.Frame(top, bg=SURFACE)
        btn_wrap.grid(row=0, column=2, padx=(0,8))

        self._engine_dot = tk.Label(btn_wrap, text="● starting…",
                                    bg=SURFACE, fg=WARN,
                                    font=("Segoe UI", 9), padx=10, pady=14)
        self._engine_dot.pack(side="left")

        self._show_browser_btn = tk.Button(btn_wrap, text="🔍 Show Browser",
                                           bg=SURFACE, fg=SUBTEXT,
                                           activebackground=CARD, activeforeground=ROSE,
                                           relief="flat", padx=10, pady=14,
                                           font=("Segoe UI", 9), cursor="hand2",
                                           command=lambda: self._show_browser())
        self._show_browser_btn.pack(side="left")

        # Search area
        search_wrap = tk.Frame(top, bg=SURFACE, padx=16, pady=10)
        search_wrap.grid(row=0, column=1, sticky="ew")
        search_wrap.columnconfigure(0, weight=1)

        entry_frame = tk.Frame(search_wrap, bg=CARD, padx=10, pady=6)
        entry_frame.grid(row=0, column=0, sticky="ew")
        entry_frame.columnconfigure(0, weight=1)

        self._search_var = tk.StringVar()
        tk.Entry(entry_frame, textvariable=self._search_var,
                 bg=CARD, fg=TEXT, insertbackground=ROSE,
                 relief="flat", font=("Segoe UI", 11), bd=0).grid(
                     row=0, column=0, sticky="ew")
        self._search_var.trace_add("write", lambda *_: None)

        self._search_entry = entry_frame.winfo_children()[0]
        self._search_entry.bind("<Return>", lambda _: self._do_search())

        self._search_btn = tk.Button(search_wrap, text="Search",
                                     bg=ROSE_DIM, fg="white",
                                     activebackground=ROSE,
                                     relief="flat", padx=16, pady=7,
                                     font=("Segoe UI", 10, "bold"),
                                     cursor="hand2",
                                     command=self._do_search)
        self._search_btn.grid(row=0, column=1, padx=(8,0))

        # ── Main content ──────────────────────────────────────────────────────
        content = tk.Frame(self, bg=BG)
        content.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        content.columnconfigure(0, weight=2, minsize=320)
        content.columnconfigure(1, weight=3)
        content.rowconfigure(0, weight=1)

        # ── Left: search results ──────────────────────────────────────────────
        left = tk.Frame(content, bg=SURFACE, bd=0)
        left.grid(row=0, column=0, sticky="nsew", padx=(0,6))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        tk.Label(left, text="RESULTS", font=("Segoe UI", 8, "bold"),
                 bg=SURFACE, fg=MUTED, anchor="w", padx=10, pady=8).grid(
                     row=0, column=0, sticky="ew")

        res_frame = tk.Frame(left, bg=CARD)
        res_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0,6))
        res_frame.rowconfigure(0, weight=1)
        res_frame.columnconfigure(0, weight=1)

        self._res_listbox = tk.Listbox(
            res_frame, selectmode="single",
            bg=CARD, fg=TEXT,
            selectbackground=ROSE_DIM, selectforeground="white",
            relief="flat", bd=0, font=("Segoe UI", 10),
            activestyle="none", highlightthickness=0, cursor="hand2")
        self._res_listbox.grid(row=0, column=0, sticky="nsew")
        # Use ButtonRelease so the event carries a y-coordinate we can validate
        # against real rows (avoids selecting blank space below the last item).
        self._res_listbox.bind("<ButtonRelease-1>", self._on_result_select)

        res_sb = ttk.Scrollbar(res_frame, orient="vertical",
                               command=self._res_listbox.yview)
        res_sb.grid(row=0, column=1, sticky="ns")
        self._res_listbox.configure(yscrollcommand=res_sb.set)

        # Horizontal scrollbar so long titles are reachable without maximizing.
        res_hsb = ttk.Scrollbar(res_frame, orient="horizontal",
                                command=self._res_listbox.xview)
        res_hsb.grid(row=1, column=0, sticky="ew")
        self._res_listbox.configure(xscrollcommand=res_hsb.set)

        self._anime_info = tk.Label(left, text="", font=("Segoe UI", 8),
                                    bg=SURFACE, fg=SUBTEXT, wraplength=300,
                                    justify="left", padx=10, pady=4)
        self._anime_info.grid(row=2, column=0, sticky="ew")

        # ── Right: episode list ───────────────────────────────────────────────
        right = tk.Frame(content, bg=SURFACE)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # Episode header row
        ep_hdr = tk.Frame(right, bg=SURFACE)
        ep_hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(8,4))
        ep_hdr.columnconfigure(0, weight=1)

        self._ep_label = tk.Label(ep_hdr, text="EPISODES",
                                  font=("Segoe UI", 8, "bold"),
                                  bg=SURFACE, fg=MUTED, anchor="w")
        self._ep_label.grid(row=0, column=0, sticky="w")

        btn_bar = tk.Frame(ep_hdr, bg=SURFACE)
        btn_bar.grid(row=0, column=1, sticky="e")

        for label, cmd in [("All", lambda: self._checklist.select_all()),
                           ("None", lambda: self._checklist.select_none())]:
            tk.Button(btn_bar, text=label, bg=CARD, fg=SUBTEXT,
                      activebackground=BORDER, activeforeground=TEXT,
                      relief="flat", padx=10, pady=3,
                      font=("Segoe UI", 8), cursor="hand2",
                      command=cmd).pack(side="left", padx=2)

        # Range selector
        rng_bar = tk.Frame(ep_hdr, bg=SURFACE)
        rng_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4,0))

        tk.Label(rng_bar, text="Range:", bg=SURFACE, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left")

        self._range_from = tk.Entry(rng_bar, width=5, bg=CARD, fg=TEXT,
                                    insertbackground=ROSE, relief="flat",
                                    font=("Segoe UI", 9), bd=4)
        self._range_from.pack(side="left", padx=(4,2))

        tk.Label(rng_bar, text="–", bg=SURFACE, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")

        self._range_to = tk.Entry(rng_bar, width=5, bg=CARD, fg=TEXT,
                                  insertbackground=ROSE, relief="flat",
                                  font=("Segoe UI", 9), bd=4)
        self._range_to.pack(side="left", padx=(2,6))

        tk.Button(rng_bar, text="Apply", bg=ROSE_DIM, fg="white",
                  relief="flat", padx=8, pady=2, font=("Segoe UI", 8),
                  cursor="hand2", command=self._apply_range).pack(side="left")

        self._sel_count = tk.Label(rng_bar, text="", bg=SURFACE, fg=ROSE,
                                   font=("Segoe UI", 8))
        self._sel_count.pack(side="left", padx=(12,0))

        # CheckList
        self._checklist = CheckList(right)
        self._checklist.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0,6))

        # ── Bottom bar ────────────────────────────────────────────────────────
        bot = tk.Frame(self, bg=SURFACE, padx=12, pady=10)
        bot.grid(row=2, column=0, sticky="ew")
        bot.columnconfigure(1, weight=1)

        tk.Label(bot, text="Save to", bg=SURFACE, fg=SUBTEXT,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", padx=(0,8))

        self._dir_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        tk.Entry(bot, textvariable=self._dir_var,
                 bg=CARD, fg=TEXT, insertbackground=ROSE,
                 relief="flat", font=("Segoe UI", 9), bd=6).grid(
                     row=0, column=1, sticky="ew")

        tk.Button(bot, text="…", bg=CARD, fg=ROSE,
                  relief="flat", padx=10, pady=3,
                  font=("Segoe UI", 9), cursor="hand2",
                  command=self._browse).grid(row=0, column=2, padx=(6,0))

        # Quality + audio preference row
        pref = tk.Frame(bot, bg=SURFACE)
        pref.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8,0))
        tk.Label(pref, text="Quality", bg=SURFACE, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0,4))
        q_menu = tk.OptionMenu(pref, self._quality_var, "1080", "720", "480", "360")
        q_menu.config(bg=CARD, fg=TEXT, relief="flat", highlightthickness=0,
                      activebackground=BORDER, font=("Segoe UI", 9), width=6)
        q_menu["menu"].config(bg=CARD, fg=TEXT)
        q_menu.pack(side="left", padx=(0,16))
        tk.Label(pref, text="Audio", bg=SURFACE, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0,4))
        a_menu = tk.OptionMenu(pref, self._audio_var, "jpn", "eng")
        a_menu.config(bg=CARD, fg=TEXT, relief="flat", highlightthickness=0,
                      activebackground=BORDER, font=("Segoe UI", 9), width=6)
        a_menu["menu"].config(bg=CARD, fg=TEXT)
        a_menu.pack(side="left")

        # Season number (for Jellyfin SxxEyy naming)
        tk.Label(pref, text="Season", bg=SURFACE, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(16,4))
        season_entry = tk.Entry(pref, textvariable=self._season_var, width=4,
                                bg=CARD, fg=TEXT, insertbackground=ROSE,
                                relief="flat", font=("Segoe UI", 9), justify="center")
        season_entry.pack(side="left")

        # Jellyfin-friendly naming toggle
        jf = tk.Checkbutton(pref, text="Jellyfin naming (S01E01 + folder)",
                            variable=self._jellyfin_var,
                            bg=SURFACE, fg=SUBTEXT, selectcolor=CARD,
                            activebackground=SURFACE, activeforeground=TEXT,
                            relief="flat", font=("Segoe UI", 9),
                            highlightthickness=0, bd=0)
        jf.pack(side="left", padx=(16,0))

        self._progress = ttk.Progressbar(bot, style="Rose.Horizontal.TProgressbar",
                                         mode="determinate")
        self._progress.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8,4))

        self._status_var = tk.StringVar(value="Search for an anime above to get started.")
        self._status_lbl = tk.Label(bot, textvariable=self._status_var,
                                    bg=SURFACE, fg=SUBTEXT,
                                    font=("Segoe UI", 9), anchor="w")
        self._status_lbl.grid(row=2, column=0, columnspan=2, sticky="ew")

        action_bar = tk.Frame(bot, bg=SURFACE)
        action_bar.grid(row=1, column=3, rowspan=2, sticky="se")

        self._dl_btn = tk.Button(action_bar, text="⬇  Download",
                                 bg=ROSE_DIM, fg="white",
                                 activebackground=ROSE,
                                 relief="flat", padx=18, pady=8,
                                 font=("Segoe UI", 10, "bold"),
                                 cursor="hand2",
                                 command=self._start_download)
        self._dl_btn.pack(side="left", padx=(0,6))

        self._stop_btn = tk.Button(action_bar, text="■ Stop",
                                   bg=CARD, fg=ERROR,
                                   activebackground=BORDER,
                                   relief="flat", padx=12, pady=8,
                                   font=("Segoe UI", 10, "bold"),
                                   cursor="hand2", state="disabled",
                                   command=self._stop_download)
        self._stop_btn.pack(side="left")

    # ── Search ────────────────────────────────────────────────────────────────

    def _do_search(self):
        query = self._search_var.get().strip()
        if not query: return

        if not self._engine_ready:
            self._set_status("Browser engine still starting — try again in a moment…", WARN)
            return

        self._search_btn.config(state="disabled", text="…")
        self._res_listbox.delete(0, "end")
        self._checklist.clear()
        self._search_results = []
        self._anime_info.config(text="")
        self._set_status(f'Searching for "{query}"…', WARN)

        def run():
            try:
                results = search_anime(query)
                self.after(0, lambda: self._populate_results(results, query))
            except Exception as e:
                self.after(0, lambda err=e: self._set_status(f"Search error: {err}", ERROR))
            finally:
                self.after(0, lambda: self._search_btn.config(state="normal", text="Search"))

        threading.Thread(target=run, daemon=True).start()

    def _startup_engine(self):
        """Start the persistent browser engine in the background."""
        self._engine_ready = False
        self._engine_dot.config(text="● starting…", fg=WARN)
        self._set_status("Opening browser — if you see 'Verify you are human', click it in the browser window.", WARN)

        def run():
            try:
                get_engine(headless=False,
                           status_cb=lambda m: self.after(0, lambda msg=m: self._set_status(msg, WARN)),
                           on_challenge=lambda: self.after(0, self._on_challenge_needed))
                self.after(0, self._engine_ok)
            except Exception as e:
                self.after(0, lambda err=e: self._engine_fail(err))

        threading.Thread(target=run, daemon=True).start()

    def _engine_ok(self):
        self._engine_ready = True
        self._engine_dot.config(text="● ready", fg=SUCCESS)
        self._set_status("Browser ready ✓  Minimizing it — search away. Use 'Show Browser' if a check reappears.", SUCCESS)
        # Minimize the browser now that the challenge is solved.
        def hide():
            try:
                get_engine().go_headless()
                self.after(0, lambda: self._engine_dot.config(text="● ready (minimized)", fg=SUCCESS))
            except Exception as e:
                _log(f"GUI: minimize failed {type(e).__name__}")
        threading.Thread(target=hide, daemon=True).start()

    def _on_challenge_needed(self):
        """Engine signalled it needs a visible re-solve."""
        self._engine_dot.config(text="● solve in browser", fg=WARN)
        self._set_status("Cloudflare check reappeared — solve it in the browser window; it'll hide again after.", WARN)

    def _show_browser(self):
        """Manually bring the browser back to solve a challenge."""
        if not self._engine_ready:
            self._set_status("Engine still starting…", WARN)
            return
        self._set_status("Bringing the browser forward… solve any check, then it hides again.", WARN)
        def run():
            try:
                get_engine().go_visible()
            except Exception as e:
                _log(f"GUI: go_visible failed {type(e).__name__}")
        threading.Thread(target=run, daemon=True).start()

    def _engine_fail(self, err):
        self._engine_ready = False
        self._engine_dot.config(text="● error", fg=ERROR)
        self._set_status(f"Engine failed to start: {err}  "
                         "Make sure Playwright + Chromium are installed.", ERROR)

    def _populate_results(self, results, query):
        self._search_results = results
        if not results:
            self._set_status(f'No results for "{query}". The site returned an empty list — check spelling or try another title.', WARN)
            return
        for r in results:
            year = f" ({r['year']})" if r.get("year") else ""
            kind = f" [{r['type']}]"  if r.get("type")  else ""
            self._res_listbox.insert("end", f" {r['title']}{year}{kind}")
        self._set_status(f"{len(results)} result(s) — click one to load episodes.", SUCCESS)

    def _on_result_select(self, event=None):
        # Guard against clicks on blank space below the last item. Tkinter's
        # Listbox still fires a selection for empty-area clicks; verify the
        # click y-coordinate actually falls on a real row.
        if event is not None and hasattr(event, "y"):
            idx = self._res_listbox.nearest(event.y)
            bbox = self._res_listbox.bbox(idx)
            if not bbox:
                return
            y0, height = bbox[1], bbox[3]
            if event.y > y0 + height:        # clicked below the last row
                self._res_listbox.selection_clear(0, "end")
                return
            # Force the selection to the item actually under the cursor.
            self._res_listbox.selection_clear(0, "end")
            self._res_listbox.selection_set(idx)

        sel = self._res_listbox.curselection()
        if not sel: return
        anime = self._search_results[sel[0]]

        parts = [p for p in [
            f"Status: {anime['status']}" if anime.get("status") else "",
            f"Eps: {anime['episodes']}"  if anime.get("episodes") else "",
            anime.get("type", ""),
        ] if p]
        self._anime_info.config(text="  ·  ".join(parts))

        self._checklist.clear()
        self._ep_label.config(text="EPISODES — loading…")
        # Don't stomp on the download status line if a download is in progress.
        downloading = not self._stop_event.is_set() and self._dl_btn["state"] == "disabled"
        if not downloading:
            self._set_status(f"Loading episodes for {anime['title']}…", WARN)

        def run():
            try:
                def page_cb(p, t):
                    if not (not self._stop_event.is_set() and self._dl_btn["state"] == "disabled"):
                        self.after(0, lambda pg=p, tot=t:
                            self._set_status(f"Fetching page {pg}/{tot}…", WARN))
                eps = fetch_episodes(anime["id"], progress_cb=page_cb)
                self.after(0, lambda: self._populate_episodes(eps, anime["title"]))
            except Exception as e:
                self.after(0, lambda err=e: self._set_status(f"Episode load error: {err}", ERROR))

        threading.Thread(target=run, daemon=True).start()

    def _populate_episodes(self, episodes, title):
        self._episodes = episodes
        self._current_anime_title = title   # clean title for filenames
        self._checklist.clear()
        for ep in episodes:
            self._checklist.add_episode(
                ep["episode"],
                ep["title"],       # show whatever the API gives; blank → "—"
                ep["session"],
                ep["anime_id"],
            )
        count = len(episodes)
        self._ep_label.config(text=f"EPISODES — {count} total")
        downloading = not self._stop_event.is_set() and self._dl_btn["state"] == "disabled"
        if not downloading:
            self._set_status(f"Loaded {count} episodes for {title}. Select episodes below.", SUCCESS)
        self._update_sel_count()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_sel_count(self):
        n = self._checklist.count_selected()
        self._sel_count.config(text=f"{n} selected" if n else "")

    def _apply_range(self):
        try:
            lo = float(self._range_from.get().strip())
            hi = float(self._range_to.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid range", "Enter valid episode numbers.")
            return
        self._checklist.select_range(lo, hi)
        n = self._checklist.count_selected()
        self._set_status(f"Selected {n} episode(s) in range {lo}–{hi}.", TEAL)
        self._update_sel_count()

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self._dir_var.get())
        if d: self._dir_var.set(d)

    # ── Download ──────────────────────────────────────────────────────────────

    def _start_download(self):
        selected = self._checklist.get_selected()
        if not selected:
            messagebox.showwarning("Nothing selected", "Tick at least one episode.")
            return
        dest = self._dir_var.get().strip()
        if not dest or not os.path.isdir(dest):
            messagebox.showerror("Bad directory", "Choose a valid download folder.")
            return

        self._stop_event.clear()
        self._dl_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._progress["value"] = 0
        # Snapshot the title NOW so clicking another result mid-download can't
        # change the folder/filename of the in-progress batch.
        batch_title = getattr(self, "_current_anime_title", "")

        def run():
            import traceback
            total = len(selected)
            idx = 0
            rl_strikes = 0     # consecutive rate-limit hits
            ctx_strikes = 0    # consecutive dead-context hits
            res_strikes = 0    # consecutive resolve failures (Kwik throttling)
            while idx < len(selected):
                item = selected[idx]
                if self._stop_event.is_set():
                    self.after(0, lambda: self._set_status("Stopped.", WARN))
                    break

                ep_num = item["ep"]
                print(f"\n[DL] Starting Ep {ep_num} | anime_id={item['anime_id']} | session={item['session'][:16]}…")
                self.after(0, lambda n=ep_num, i=idx:
                    self._set_status(f"[{i+1}/{total}] Finding download link for Ep {n}…", WARN))

                try:
                    print(f"[DL] Calling get_download_options…")
                    options, scraped_title = get_download_options(item["anime_id"], item["session"])
                    print(f"[DL] {len(options)} option(s): " +
                          ", ".join(f"{o['quality']}p/{o['audio']}" for o in options))

                    if not options:
                        msg = f"Ep {ep_num}: No download links found — skipping."
                        print(f"[DL] {msg}")
                        self.after(0, lambda n=ep_num, m=msg:
                            self._set_status(m, ERROR))
                        idx += 1
                        continue

                    chosen = pick_download_option(options, self._quality_var.get(), self._audio_var.get())
                    print(f"[DL] chosen: {chosen['label']!r} -> {chosen['url']}")

                    print(f"[DL] Resolving {chosen['url']}…")
                    result = resolve_download(chosen["url"])
                    if not result:
                        # Transient kwik/pahe hiccup — re-scrape (links rotate)
                        # and try once more before giving up on this episode.
                        print(f"[DL] resolve failed, retrying once…")
                        self.after(0, lambda n=ep_num:
                            self._set_status(f"Ep {n}: link slow, retrying…", WARN))
                        options2, _ = get_download_options(item["anime_id"], item["session"])
                        if options2:
                            chosen2 = pick_download_option(options2, self._quality_var.get(), self._audio_var.get())
                            result = resolve_download(chosen2["url"])
                            if result:
                                chosen = chosen2
                    print(f"[DL] resolve result={result!r}")

                    if not result:
                        # Repeated resolve failures usually mean Kwik is throttling
                        # us after many rapid downloads. Back off (progressively),
                        # then RETRY the same episode rather than skipping it.
                        res_strikes += 1
                        if res_strikes <= 3:
                            wait_s = min(120, 30 * res_strikes)
                            print(f"[DL] resolve throttled (strike {res_strikes}); cooling down {wait_s}s then retrying Ep {ep_num}.")
                            for remaining in range(wait_s, 0, -1):
                                if self._stop_event.is_set():
                                    break
                                self.after(0, lambda r=remaining, n=ep_num:
                                    self._set_status(f"Links throttled — waiting {r}s before retrying Ep {n}…", WARN))
                                time.sleep(1)
                            continue   # retry same episode (idx not advanced)
                        # Gave it several tries with cooldowns — skip and move on.
                        msg = f"Ep {ep_num}: Could not resolve after retries — skipping."
                        print(f"[DL] {msg}")
                        self.after(0, lambda n=ep_num, m=msg:
                            self._set_status(m, ERROR))
                        res_strikes = 0
                        idx += 1
                        continue

                    # Resolve succeeded — reset the throttle counter.
                    res_strikes = 0

                    direct_url, extra_hdrs = result
                    print(f"[DL] direct_url={direct_url!r}")

                    # Build filename. Jellyfin wants "Series SxxEyy" and, ideally,
                    # each series in its own folder.
                    anime_title = batch_title or item["title"] or scraped_title
                    safe_title = re.sub(r'[\\/:*?"<>|]', "", anime_title).strip() or "Unknown"
                    season = int(self._season_var.get() or 1)
                    ep_int = int(re.sub(r"\D", "", str(ep_num)) or 0)
                    se_tag = f"S{season:02d}E{ep_int:02d}"

                    if self._jellyfin_var.get():
                        # Jellyfin layout: <dest>/<Series>/<Series> SxxEyy.mp4
                        series_dir = os.path.join(dest, safe_title)
                        os.makedirs(series_dir, exist_ok=True)
                        fname = f"{safe_title} - {se_tag} [{chosen['quality']}p].mp4"
                        dest_path = os.path.join(series_dir, fname)
                    else:
                        fname = f"{safe_title} - Ep{str(ep_num).zfill(3)} [{chosen['quality']}p].mp4"
                        dest_path = os.path.join(dest, fname)
                    print(f"[DL] Saving to: {dest_path}")

                    self.after(0, lambda n=ep_num, i=idx:
                        self._set_status(f"[{i+1}/{total}] Downloading Ep {n}…", TEXT))

                    def file_prog(frac, ep_done=idx):
                        overall = (ep_done + frac) / total * 100
                        self.after(0, lambda v=overall: self._progress.configure(value=v))

                    print(f"[DL] Starting file download…")
                    ok = pw_download(direct_url, dest_path, extra_hdrs,
                                       progress_cb=file_prog,
                                       stop_event=self._stop_event)
                    print(f"[DL] pw_download returned: {ok}")
                    if not ok:
                        break

                    # success — reset the strike counters and advance
                    rl_strikes = 0
                    ctx_strikes = 0
                    idx += 1

                except RateLimited:
                    # AnimePahe is rate-limiting us. Wait a while, then RETRY the
                    # same episode (don't advance idx). Back off progressively.
                    rl_strikes += 1
                    wait_s = min(120, 30 * rl_strikes)
                    print(f"[DL] RATE LIMITED (429). Cooling down {wait_s}s, then retrying Ep {ep_num}.")
                    for remaining in range(wait_s, 0, -1):
                        if self._stop_event.is_set():
                            break
                        self.after(0, lambda r=remaining, n=ep_num:
                            self._set_status(f"Rate limited — waiting {r}s before retrying Ep {n}…", WARN))
                        time.sleep(1)
                    # do NOT increment idx — retry the same episode
                    continue

                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"[DL] EXCEPTION for Ep {ep_num}:\n{tb}")
                    # A dead browser context (TargetClosedError) often follows a
                    # rate-limit or network drop. Pause so the relaunched browser
                    # can settle, then retry the SAME episode once before skipping.
                    if "TargetClosedError" in tb or "Target page" in str(e):
                        ctx_strikes += 1
                        if ctx_strikes <= 3:
                            print(f"[DL] context died, cooldown 15s then retry (strike {ctx_strikes})")
                            for remaining in range(15, 0, -1):
                                if self._stop_event.is_set():
                                    break
                                self.after(0, lambda r=remaining, n=ep_num:
                                    self._set_status(f"Browser reset — waiting {r}s before retrying Ep {n}…", WARN))
                                time.sleep(1)
                            continue   # retry same episode
                    self.after(0, lambda n=ep_num, err=str(e):
                        self._set_status(f"Ep {n} error: {err}", ERROR))
                    idx += 1
                    continue

                overall = idx / total * 100
                self.after(0, lambda v=overall: self._progress.configure(value=v))

                # Small polite pause between episodes on long batches to avoid
                # tripping AnimePahe's rate limiter (HTTP 429).
                if idx < len(selected) and not self._stop_event.is_set():
                    time.sleep(1.5)
                self.after(0, lambda: (
                    self._set_status(f"Done! {total} episode(s) saved to {dest}", SUCCESS),
                    self._progress.configure(value=100),
                ))
            self.after(0, lambda: (
                self._dl_btn.config(state="normal"),
                self._stop_btn.config(state="disabled"),
            ))

        threading.Thread(target=run, daemon=True).start()

    def _stop_download(self):
        self._stop_event.set()
        self._stop_btn.config(state="disabled")
        self._set_status("Stopping after current file…", WARN)

    def _set_status(self, msg, color=SUBTEXT):
        self._status_var.set(msg)
        self._status_lbl.config(fg=color)

    def _on_close(self):
        """Shut the browser engine down cleanly, then close the window."""
        try:
            if _engine is not None:
                _engine.shutdown()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = AnimePaheApp()
    app.protocol("WM_DELETE_WINDOW", app._on_close)
    app.mainloop()
