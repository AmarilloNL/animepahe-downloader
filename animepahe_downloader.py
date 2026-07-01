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

import os
import sys
# On Windows, stdout/stderr default to cp1252 and crash on Unicode like → or ✓.
# Force UTF-8 so logging and status messages can never raise UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# PyInstaller-frozen Windows builds: this MUST run before anything that can
# spawn a subprocess, or child processes relaunch the whole GUI in a loop.
import multiprocessing
multiprocessing.freeze_support()

# WebKitGTK on Linux (esp. Arch/CachyOS) often renders a blank window due to a
# DMABUF/GPU-compositing bug. Disabling those renderers forces software paint,
# which fixes the blank-window symptom. Must be set BEFORE importing webview.
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

import webview
import threading, queue
from bs4 import BeautifulSoup
import re, time, json
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
# ── Synthwave / neon palette ──────────────────────────────────────────────────
# Variable names kept stable; values remapped to a purple+magenta neon theme.
BG       = "#0a0a14"   # near-black with a blue-violet tint
SURFACE  = "#11111f"   # panels / bars
CARD     = "#1a1a2e"   # inputs, list rows, buttons
BORDER   = "#2d2b50"   # violet-tinted separators
ROSE     = "#e94aff"   # primary accent — neon magenta
ROSE_DIM = "#6d28d9"   # deep purple (logo bg, button base, progress)
TEAL     = "#22d3ee"   # secondary accent — electric cyan
ACCENT2  = "#a855f7"   # mid purple (hover / highlights)
TEXT     = "#f5f3ff"   # near-white with a faint violet warmth
SUBTEXT  = "#9d9bc4"   # muted lavender-grey
MUTED    = "#56547e"   # dim violet-grey
SUCCESS  = "#34d399"   # neon green
ERROR    = "#fb7185"   # neon red/pink
WARN     = "#fcd34d"   # amber
SELECT   = "#241a3d"   # selected list-row background (purple glow)
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
    """Append a timestamped line to the engine log AND print it.

    Must never raise: on Windows the console/file default to cp1252, which can't
    encode characters like → or ✓ and would otherwise crash the calling job with
    UnicodeEncodeError. We force UTF-8 and swallow any encoding errors."""
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        # cp1252 console can't encode some chars — fall back to ascii-safe.
        try:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

_engine: "BrowserEngine | None" = None


def _cleanup_playwright_artifacts():
    """
    Playwright/patchright writes per-session scratch files named
    'playwright-artifacts-*' into the system temp dir. Normally they're removed
    when the browser closes cleanly, but a crash or hard exit can orphan them —
    and they can grow to many GB over repeated launches. Sweep away any stale
    ones at startup so they never accumulate.
    """
    import glob, shutil, tempfile, time as _t
    try:
        tmp = tempfile.gettempdir()
        now = _t.time()
        removed = 0
        for d in glob.glob(os.path.join(tmp, "playwright-artifacts-*")):
            try:
                # Only remove ones not touched in the last 5 minutes, so we never
                # delete the artifacts of a browser that's actively running.
                if now - os.path.getmtime(d) < 300:
                    continue
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    os.remove(d)
                removed += 1
            except Exception:
                pass
        if removed:
            _log(f"CLEANUP: removed {removed} stale playwright-artifacts dir(s)")
    except Exception as e:
        _log(f"CLEANUP: artifact sweep skipped ({type(e).__name__})")


def _chromium_present():
    import glob
    if os.name == "nt":
        base = os.path.join(os.environ.get("USERPROFILE", ""),
                            "AppData", "Local", "ms-playwright")
    else:
        base = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
    return any(os.path.isdir(c) for c in glob.glob(os.path.join(base, "chromium-*")))


def _ensure_chromium_installed(status_cb=None):
    """
    Make sure Patchright's Chromium is present. In a packaged build (the Windows
    .exe) the user never ran `patchright install chromium`, so the browser won't
    exist on first launch. Detect that and install it automatically, once,
    reporting progress to the UI via status_cb instead of a separate console.

    NOTE: in a PyInstaller --onefile build, sys.executable is the .exe itself, so
    we must NOT spawn `sys.executable -m patchright …` — that would relaunch the
    whole app in a loop. We invoke patchright's install routine in-process.
    """
    try:
        if _chromium_present():
            return
        msg = "First run: downloading the browser engine (~180 MB, one-time)…"
        _log("ENGINE: " + msg)
        if status_cb:
            status_cb(msg)

        # Hide the child console the patchright driver (node.exe) spawns on
        # Windows, so no black terminal pops up.
        if os.name == "nt":
            os.environ.setdefault("PLAYWRIGHT_NODEJS_NO_WINDOW", "1")

        old_argv = sys.argv
        try:
            sys.argv = ["patchright", "install", "chromium"]
            try:
                from patchright.__main__ import main as _pr_main
            except Exception:
                from playwright.__main__ import main as _pr_main
            try:
                _pr_main()
            except SystemExit:
                pass   # the CLI calls sys.exit() on completion
        finally:
            sys.argv = old_argv

        _log("ENGINE: Chromium install finished.")
        if status_cb:
            status_cb("Browser engine installed.")
    except Exception as e:
        _log(f"ENGINE: chromium auto-install skipped ({type(e).__name__})")
        if status_cb:
            status_cb("Couldn't auto-install the browser — see engine.log.")


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
        self._pending_dest = None           # dest path for browser save_as download
        self._pending_progress = None       # progress callback for the save
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
            _cleanup_playwright_artifacts()
            _ensure_chromium_installed(status_cb=self._status_cb)
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
        # Final sweep so this session leaves no scratch files behind.
        _cleanup_playwright_artifacts()

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
        is never lost. If 'minimized' is rejected (some Windows Chromium builds
        refuse it), fall back to moving the window off-screen."""
        try:
            page = self._main_page()
            session = self._ctx.new_cdp_session(page)
            ids = session.send("Browser.getWindowForTarget")
            win_id = ids["windowId"]
            try:
                session.send("Browser.setWindowBounds", {
                    "windowId": win_id,
                    "bounds": {"windowState": state},
                })
                _log(f"WINDOW: set state={state}")
            except Exception as e1:
                # Windows sometimes rejects windowState:minimized. Fall back to
                # shoving the window off-screen (works everywhere).
                if state == "minimized":
                    try:
                        # must be 'normal' before setting explicit bounds
                        session.send("Browser.setWindowBounds",
                                     {"windowId": win_id, "bounds": {"windowState": "normal"}})
                        session.send("Browser.setWindowBounds", {
                            "windowId": win_id,
                            "bounds": {"left": -32000, "top": -32000,
                                       "width": 1100, "height": 760},
                        })
                        _log("WINDOW: minimized rejected; moved off-screen instead")
                    except Exception as e2:
                        _log(f"WINDOW: offscreen fallback failed {type(e2).__name__}")
                        raise
                else:
                    raise
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
        # Explicitly bring it back on-screen (in case we hid it off-screen).
        try:
            page = self._main_page()
            session = self._ctx.new_cdp_session(page)
            win_id = session.send("Browser.getWindowForTarget")["windowId"]
            session.send("Browser.setWindowBounds",
                         {"windowId": win_id, "bounds": {"windowState": "normal"}})
            session.send("Browser.setWindowBounds", {
                "windowId": win_id,
                "bounds": {"left": 80, "top": 80, "width": 1100, "height": 760},
            })
            try: session.detach()
            except Exception: pass
        except Exception:
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
                            _log("SOLVE: confirmed cleared")
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

    def _fetch_image(self, url: str) -> bytes | None:
        """
        Fetch a poster/snapshot. The image host (i.animepahe.pw) rejects every
        API/fetch route with 403 (bare, +referer, +cookies all fail) and CORP
        blocks in-page fetch. The ONLY thing it accepts is a real browser
        navigation to the image URL. So we open a throwaway tab, navigate to the
        image, and read the raw response bytes (which ARE the image). Verified by
        probe: page.goto(image) -> 200, resp.body() -> image bytes.
        Returns raw image bytes, or None.
        """
        if not url:
            return None
        tab = None
        try:
            # Make sure the main page has cleared Cloudflare so the session is warm.
            main = self._main_page()
            if not (main.url or "").startswith(BASE_URL):
                self._goto(main, BASE_URL)
            tab = self._ctx.new_page()
            resp = tab.goto(url, wait_until="commit", timeout=15000)
            if not resp or not resp.ok:
                _log(f"IMG: goto HTTP {resp.status if resp else '?'} {url[:55]}")
                return None
            data = resp.body()
            if not data or len(data) < 64:
                _log(f"IMG: empty body {url[:55]}")
                return None
            return data
        except Exception as e:
            _log(f"IMG: fetch failed {type(e).__name__} {url[:55]}")
            return None
        finally:
            if tab is not None:
                try: tab.close()
                except Exception: pass
            # Opening/closing a tab can re-raise the window; keep it hidden.
            if self._minimized:
                try: self._reassert_minimized()
                except Exception: pass

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
                # pahe.win injects fake "Adblocker / Install / I'm not a robot"
                # ad-overlays. Clicking buttons risks hitting an ad. So FIRST try
                # to pull the real kwik link straight out of the page HTML and
                # navigate to it directly — this skips the ad gauntlet entirely.
                for _ in range(4):
                    if "kwik." in (page.url or ""):
                        break
                    html = ""
                    try:
                        html = page.content()
                    except Exception:
                        pass
                    km = re.search(r'https://kwik\.[a-z]+/f/[A-Za-z0-9]+', html)
                    if km:
                        _log(f"DLRES: pahe to kwik (embedded) {km.group(0)}")
                        try:
                            page.goto(km.group(0), wait_until="commit", timeout=20000)
                            page.wait_for_load_state("domcontentloaded", timeout=8000)
                        except Exception:
                            pass
                        self._reassert_minimized()
                        break

                    # No embedded link yet — click ONLY a real continue/kwik
                    # control, never generic ad buttons. We explicitly skip
                    # anything that looks like an ad ("Install", "robot", etc.).
                    clicked = False
                    for sel in ("a[href*='kwik.']",
                                "a:has-text('Continue')",
                                "button:has-text('Continue')",
                                "form[action*='kwik'] button",
                                "form button[type=submit]"):
                        try:
                            for el in page.query_selector_all(sel):
                                if not el.is_visible():
                                    continue
                                label = (el.inner_text() or "").strip().lower()
                                # Skip obvious ad buttons.
                                if any(bad in label for bad in
                                       ("install", "robot", "adblock", "update", "download app")):
                                    continue
                                _log(f"DLRES: clicking pahe.win '{sel}' ({label[:20]!r})")
                                el.click()
                                clicked = True
                                break
                            if clicked:
                                break
                        except Exception:
                            continue
                    try:
                        page.wait_for_url(re.compile(r"kwik\."), timeout=12000)
                    except Exception:
                        page.wait_for_timeout(2000)
                    self._reassert_minimized()

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

            # Submit the form to trigger the download. The vault CDN now rejects
            # plain urllib/API requests with 403 (verified by probe) — the ONLY
            # thing it accepts is a real browser download. So we let Playwright
            # download it and save_as() straight to the destination. That also
            # means no leftover copy in temp (fixes the disk-fill problem).
            try:
                with page.expect_download(timeout=25000) as dl:
                    page.evaluate("(document.querySelector('form')||{submit(){}}).submit()")
                download = dl.value
                direct = download.url
                _log(f"DLRES: got download url {direct[:80]}")
                self._reassert_minimized()

                if self._pending_dest:
                    # Save directly to the destination via the browser download.
                    dest_path = self._pending_dest
                    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                    _log(f"DLRES: saving via browser to {dest_path}")
                    try:
                        # Report indeterminate progress; Playwright save_as is
                        # atomic (no per-byte callback), so we show a spinner-ish
                        # state and jump to 100% when it completes.
                        if self._pending_progress:
                            self._pending_progress(0.05)
                        download.save_as(dest_path)
                        if self._pending_progress:
                            self._pending_progress(1.0)
                        size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                        _log(f"DLRES: browser download complete ({size} bytes)")
                        # Clean up Playwright's temp copy now that it's saved.
                        try: download.delete()
                        except Exception: pass
                        return ("SAVED", {"path": dest_path, "size": size})
                    except Exception as e:
                        _log(f"DLRES: save_as failed {type(e).__name__}: {str(e)[:80]}")
                        try: download.delete()
                        except Exception: pass
                        return None
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

            # Fallback path (rare): return the URL for the old urllib downloader.
            if direct and ".mp4" in direct:
                cookie_hdr = ""
                try:
                    host = urlparse(direct).hostname or ""
                    parts = []
                    for c in self._ctx.cookies():
                        dom = (c.get("domain") or "").lstrip(".")
                        if dom and (dom in host or host.endswith(dom)
                                    or "uwucdn" in dom or "kwik" in dom):
                            parts.append(f"{c['name']}={c['value']}")
                    cookie_hdr = "; ".join(parts)
                except Exception:
                    pass
                hdrs = {"Referer": origin + "/", "User-Agent": HEADERS["User-Agent"]}
                if cookie_hdr:
                    hdrs["Cookie"] = cookie_hdr
                return direct, hdrs
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
    def fetch_image(self, url):           return self._call(self._fetch_image, url)
    def resolve_download(self, url):      return self._call(self._resolve_download, url)

    def _resolve_and_save(self, url, dest_path, progress_cb, stop_event):
        """Resolve the link AND download the MP4 via the browser (save_as),
        which is the only method the CDN accepts now. Runs on the engine thread
        so it shares the cleared browser session."""
        self._pending_dest = dest_path
        self._pending_progress = progress_cb
        try:
            return self._resolve_download(url)
        finally:
            self._pending_dest = None
            self._pending_progress = None

    def resolve_and_save(self, url, dest_path, progress_cb=None, stop_event=None):
        return self._call(self._resolve_and_save, url, dest_path, progress_cb, stop_event)

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
        eng = BrowserEngine(headless=headless, status_cb=status_cb, on_challenge=on_challenge)
        # Warm up: open AnimePahe and wait for the user to clear any challenge.
        try:
            eng.solve(BASE_URL)
        except Exception:
            # Don't cache a half-initialized engine; tear it down and re-raise so
            # the caller can retry cleanly instead of reusing a dead browser.
            try: eng.shutdown()
            except Exception: pass
            raise
        _engine = eng
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
            "User-Agent": extra_headers.get("User-Agent", HEADERS["User-Agent"]),
            "Referer": referer,
            "Accept": "*/*",
        }
        if extra_headers.get("Cookie"):
            headers["Cookie"] = extra_headers["Cookie"]
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

def fetch_latest(page: int = 1) -> tuple[list[dict], bool]:
    """
    Browse the 'latest releases' (airing) feed. Returns (results, has_more).
    Each show may appear more than once as new episodes air, so we dedupe by
    anime within this page. Results use the same shape as search_anime.
    """
    text = pw_get(API_URL, params={"m": "airing", "page": page})
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped[:1] not in ("{", "["):
        return [], False
    data = json.loads(stripped)
    items = data.get("data", []) if isinstance(data, dict) else data
    results, seen = [], set()
    for i in items:
        sid = i.get("anime_session") or i.get("session", "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        results.append({
            "id":       sid,
            "title":    i.get("anime_title", "Unknown"),
            "year":     "",
            "status":   f"Ep {i.get('episode', '?')}" + (f" · {i['fansub']}" if i.get("fansub") else ""),
            "episodes": "?",
            "type":     "airing",
            "poster":   i.get("snapshot", ""),
        })
    last_page = data.get("last_page", 1) if isinstance(data, dict) else 1
    has_more = page < last_page
    return results, has_more


def fetch_catalog(status_cb=None) -> list[dict]:
    """
    Browse the full A-Z catalog from the /anime index page. This is one large
    page listing every title on the site, so we fetch it once and parse out
    each title + its session id. Results use the same shape as search_anime.
    """
    if status_cb:
        status_cb("Loading full catalog (this can take a few seconds)…")
    text = pw_get(f"{BASE_URL}/anime")
    soup = BeautifulSoup(text, "html.parser")
    results, seen = [], set()
    # The index lists each anime as <a href="/anime/<session>" title="Name">.
    for a in soup.select("a[href*='/anime/']"):
        href = a.get("href", "")
        m = re.search(r"/anime/([a-f0-9-]{36})", href)
        if not m:
            continue
        sid = m.group(1)
        if sid in seen:
            continue
        seen.add(sid)
        title = (a.get("title") or a.get_text(strip=True)).strip()
        if not title:
            continue
        results.append({
            "id": sid, "title": title,
            "year": "", "status": "", "episodes": "?", "type": "catalog", "poster": "",
        })
    # Present them alphabetically for easy browsing.
    results.sort(key=lambda r: r["title"].lower())
    return results


def search_anime(query: str, status_cb=None) -> list[dict]:
    # Primary: JSON API via in-page fetch. The API returns 8 results per page,
    # so we page through all of them (up to a sane cap) to show every match.
    text = pw_get(API_URL, params={"m": "search", "q": query})
    print(f"[SEARCH] api response (first 300): {text[:300]!r}")
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped[:1] in ("{", "["):
        data  = json.loads(stripped)
        items = data if isinstance(data, list) else data.get("data", [])
        results = []
        seen = set()

        def _add(lst):
            """Add results, skipping any anime we've already collected. Returns
            how many NEW items were added."""
            added = 0
            for i in lst:
                sid = i.get("session", i.get("id", ""))
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                added += 1
                results.append({
                    "id":       sid,
                    "title":    i.get("title", "Unknown"),
                    "year":     str(i.get("year", "")),
                    "status":   i.get("status", ""),
                    "episodes": i.get("episodes", "?"),
                    "type":     i.get("type", ""),
                    "poster":   i.get("poster", ""),
                })
            return added

        _add(items)
        # Follow pagination if the API reports more pages. AnimePahe's search
        # sometimes ignores &page and re-returns page 1, so we dedupe and stop
        # the moment a page contributes nothing new.
        if isinstance(data, dict):
            last_page = int(data.get("last_page", 1) or 1)
            for page in range(2, min(last_page, 15) + 1):
                if status_cb:
                    status_cb(f"Loading results… page {page}/{last_page}")
                t2 = pw_get(API_URL, params={"m": "search", "q": query, "page": page})
                s2 = t2.lstrip("\ufeff \t\r\n")
                if s2[:1] != "{":
                    break
                d2 = json.loads(s2)
                if _add(d2.get("data", [])) == 0:
                    break   # page added nothing new — pagination isn't working
                time.sleep(0.25)
        return results

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
            "year": "", "status": "", "episodes": "?", "type": "", "poster": "",
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


def resolve_and_save(pahe_or_kwik_url: str, dest_path: str,
                     progress_cb=None, stop_event=None):
    """
    Resolve the link and download the MP4 straight to dest_path via the browser
    (the CDN rejects urllib with 403). Returns ("SAVED", {path,size}) on success,
    a (url, headers) tuple if only resolving worked, or None.
    """
    return get_engine().resolve_and_save(pahe_or_kwik_url, dest_path,
                                         progress_cb, stop_event)



# ── Frontend (HTML/CSS/JS served into the pywebview window) ──────────────────
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PAHE DL</title>
<style>
  :root{
    --bg:#0a0a14; --bg2:#0d0d1c; --surface:#12122150; --card:#16162a;
    --card2:#1c1c34; --border:#2d2b50; --border2:#3a3866;
    --magenta:#e94aff; --magenta-dim:#a21caf; --purple:#a855f7;
    --deep:#6d28d9; --cyan:#22d3ee; --text:#f5f3ff; --sub:#b6b3da;
    --muted:#6e6c9c; --success:#34d399; --error:#fb7185; --warn:#fcd34d;
  }
  *{box-sizing:border-box; margin:0; padding:0}
  html,body{height:100%}
  body{
    font-family:"Inter","Segoe UI",system-ui,sans-serif;
    background:
      radial-gradient(1200px 600px at 80% -10%, #2a0b4e55, transparent 60%),
      radial-gradient(900px 500px at -10% 110%, #0a3b4a55, transparent 60%),
      var(--bg);
    color:var(--text); overflow:hidden; user-select:none;
  }
  /* faint synthwave grid floor */
  body::before{
    content:""; position:fixed; inset:0; pointer-events:none; opacity:.05;
    background-image:linear-gradient(var(--magenta) 1px,transparent 1px),
      linear-gradient(90deg,var(--magenta) 1px,transparent 1px);
    background-size:42px 42px;
    mask-image:linear-gradient(transparent 55%, #000 130%);
  }
  .app{display:flex; flex-direction:column; height:100vh}

  /* ── top bar ─────────────────────────────────────────── */
  .topbar{
    display:flex; align-items:center; gap:16px; padding:14px 18px;
    background:linear-gradient(180deg,#13132488,#0d0d1c88);
    backdrop-filter:blur(8px);
    border-bottom:1px solid var(--border);
    box-shadow:0 1px 0 #e94aff33, 0 8px 30px #00000060;
  }
  .logo{display:flex; align-items:center; gap:9px; font-weight:800; font-size:18px;
    letter-spacing:.5px; padding:8px 16px; border-radius:12px;
    background:linear-gradient(135deg,var(--deep),var(--magenta-dim));
    box-shadow:0 0 18px #e94aff55, inset 0 0 12px #ffffff15;}
  .logo .dot{color:var(--cyan); text-shadow:0 0 10px var(--cyan)}
  .logo .dl{color:var(--cyan); text-shadow:0 0 8px #22d3ee99}

  .searchwrap{flex:1; display:flex; gap:8px; align-items:center}
  .searchbox{flex:1; position:relative; display:flex; align-items:center;
    background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:0 14px; transition:.2s; box-shadow:inset 0 1px 2px #00000040;}
  .searchbox:focus-within{border-color:var(--magenta);
    box-shadow:0 0 0 3px #e94aff22, 0 0 22px #e94aff44;}
  .searchbox svg{width:17px; height:17px; color:var(--muted); flex:none}
  .searchbox input{flex:1; background:none; border:none; outline:none;
    color:var(--text); font-size:14px; padding:12px 10px; user-select:text}
  .searchbox input::placeholder{color:var(--muted)}

  .btn{border:none; cursor:pointer; border-radius:11px; font-size:13px;
    font-weight:600; padding:11px 16px; transition:.18s; white-space:nowrap;
    font-family:inherit;}
  .btn-primary{background:linear-gradient(135deg,var(--deep),var(--magenta));
    color:#fff; box-shadow:0 0 16px #e94aff44;}
  .btn-primary:hover{filter:brightness(1.12); box-shadow:0 0 22px #e94aff77;
    transform:translateY(-1px)}
  .btn-ghost{background:var(--card); color:var(--magenta);
    border:1px solid var(--border)}
  .btn-ghost.cyan{color:var(--cyan)}
  .btn-ghost:hover{border-color:var(--magenta); color:#fff;
    box-shadow:0 0 14px #e94aff33}
  .btn-ghost.cyan:hover{border-color:var(--cyan); box-shadow:0 0 14px #22d3ee33}
  .btn-ghost.active{background:#241a3d; border-color:var(--magenta); color:#fff}
  .btn-ghost.cyan.active{border-color:var(--cyan)}

  .engine{display:flex; align-items:center; gap:10px; flex:none}
  .dot{display:flex; align-items:center; gap:6px; font-size:12px; color:var(--sub)}
  .dot .led{width:8px; height:8px; border-radius:50%; background:var(--warn);
    box-shadow:0 0 8px currentColor; animation:pulse 1.6s infinite}
  .dot.ready .led{background:var(--success); animation:none}
  .dot.error .led{background:var(--error); animation:none}
  @keyframes pulse{50%{opacity:.35}}

  /* ── main split ──────────────────────────────────────── */
  .main{flex:1; display:grid; grid-template-columns:1.4fr 1fr; gap:14px;
    padding:14px 18px; min-height:0}
  .panel{background:linear-gradient(180deg,#14142688,#10101e88);
    border:1px solid var(--border); border-radius:16px; display:flex;
    flex-direction:column; min-height:0; overflow:hidden;
    box-shadow:0 10px 40px #00000050}
  .panel-head{display:flex; align-items:center; justify-content:space-between;
    padding:13px 16px; border-bottom:1px solid var(--border);
    font-size:11px; font-weight:700; letter-spacing:1.5px}
  .panel-head .magenta{color:var(--magenta); text-shadow:0 0 10px #e94aff66}
  .panel-head .cyan{color:var(--cyan); text-shadow:0 0 10px #22d3ee66}
  .panel-head .meta{color:var(--muted); font-weight:500; letter-spacing:.3px}

  /* ── results card grid ──────────────────────────────── */
  .grid{flex:1; overflow-y:auto; padding:14px; display:grid;
    grid-template-columns:repeat(auto-fill,minmax(132px,1fr));
    gap:13px; align-content:start}
  .card{position:relative; border-radius:13px; overflow:hidden; cursor:pointer;
    background:var(--card); border:1px solid var(--border);
    transition:.18s; aspect-ratio:2/3; display:flex; flex-direction:column}
  .card:hover{border-color:var(--magenta); transform:translateY(-3px);
    box-shadow:0 8px 26px #00000070, 0 0 18px #e94aff44}
  .card.sel{border-color:var(--cyan); box-shadow:0 0 0 2px #22d3ee55,0 0 18px #22d3ee44}
  .card .thumb{flex:1; background-size:cover; background-position:center;
    background-color:var(--card2); position:relative}
  .card .thumb.fallback{display:flex; align-items:center; justify-content:center;
    background:linear-gradient(135deg,#1c1c34,#241a3d)}
  .card .thumb.fallback span{font-size:30px; opacity:.5;
    filter:drop-shadow(0 0 8px #e94aff88)}
  .card .cap{padding:8px 9px; font-size:11.5px; line-height:1.3; font-weight:600;
    background:linear-gradient(180deg,#0d0d1cd0,#0d0d1c);
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
    overflow:hidden}
  .card .badge{position:absolute; top:7px; left:7px; font-size:9.5px;
    font-weight:700; padding:3px 7px; border-radius:7px;
    background:#0a0a14cc; color:var(--cyan); border:1px solid #22d3ee55;
    backdrop-filter:blur(4px)}

  /* ── catalog list view (no artwork — dense title list) ── */
  .listview{flex:1; overflow-y:auto; padding:8px; display:flex;
    flex-direction:column; gap:2px}
  .list-row{display:flex; align-items:center; gap:11px; padding:9px 13px;
    border-radius:9px; cursor:pointer; transition:.12s; font-size:13.5px;
    border:1px solid transparent}
  .list-row:hover{background:var(--card); border-color:var(--border2)}
  .list-row.sel{background:#241a3d; border-color:var(--cyan);
    box-shadow:0 0 12px #22d3ee33}
  .list-row .li-dot{color:var(--deep); font-size:11px; flex:none;
    transition:.12s}
  .list-row:hover .li-dot{color:var(--magenta)}
  .list-row.sel .li-dot{color:var(--cyan)}
  .list-row .li-title{color:var(--text); overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; font-weight:500}

  .empty{margin:auto; text-align:center; color:var(--muted); font-size:13px;
    padding:40px}
  .empty .big{font-size:34px; opacity:.4; margin-bottom:10px}

  /* ── episodes / download panel ──────────────────────── */
  .rightpanel{display:flex; flex-direction:column; min-height:0}
  .ep-meta{padding:8px 16px; font-size:11px; color:var(--sub);
    border-bottom:1px solid var(--border); min-height:30px}
  .ep-list{flex:1; overflow-y:auto; padding:8px 6px}
  .ep-row{display:flex; align-items:center; gap:10px; padding:8px 11px;
    border-radius:9px; cursor:pointer; transition:.12s; font-size:13px}
  .ep-row:hover{background:var(--border)}
  .ep-row.sel{background:#241a3d}
  .ep-check{width:17px; height:17px; border-radius:5px; flex:none;
    border:1.5px solid var(--border2); display:flex; align-items:center;
    justify-content:center; transition:.12s}
  .ep-row.sel .ep-check{background:var(--magenta); border-color:var(--magenta);
    box-shadow:0 0 8px #e94aff88}
  .ep-check svg{width:11px; height:11px; color:#fff; opacity:0}
  .ep-row.sel .ep-check svg{opacity:1}
  .ep-num{color:var(--cyan); font-weight:700; min-width:46px; font-size:12px}
  .ep-title{color:var(--sub); overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap}

  /* ── controls ───────────────────────────────────────── */
  .controls{border-top:1px solid var(--border); padding:12px 14px;
    display:flex; flex-direction:column; gap:10px;
    background:linear-gradient(180deg,transparent,#0d0d1c80)}
  .ctl-row{display:flex; gap:8px; align-items:center; flex-wrap:wrap}
  .seg{display:flex; gap:4px; align-items:center}
  .seg label{font-size:11px; color:var(--muted); font-weight:600}
  select,.mini-input{background:var(--card); color:var(--text);
    border:1px solid var(--border); border-radius:8px; padding:6px 9px;
    font-size:12px; outline:none; font-family:inherit;
    -webkit-appearance:none; appearance:none; cursor:pointer;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%239d9bc4' stroke-width='3'><path d='M6 9l6 6 6-6'/></svg>");
    background-repeat:no-repeat; background-position:right 8px center; padding-right:26px}
  select option{background:#16162a; color:var(--text)}
  select:focus,.mini-input:focus{border-color:var(--magenta)}
  .mini-input{width:54px; text-align:center; user-select:text}
  .chk{display:flex; align-items:center; gap:7px; cursor:pointer; font-size:12px;
    color:var(--sub)}
  .chk input{accent-color:var(--magenta); width:15px; height:15px}
  .range-in{width:48px}
  .quick{font-size:11px; color:var(--cyan); cursor:pointer; padding:5px 9px;
    border:1px solid var(--border); border-radius:7px}
  .quick:hover{border-color:var(--cyan)}

  .dlbar{display:flex; gap:8px; align-items:center}
  .btn-dl{flex:1; background:linear-gradient(135deg,var(--deep),var(--magenta));
    color:#fff; font-weight:700; padding:13px; border-radius:11px;
    box-shadow:0 0 18px #e94aff55}
  .btn-dl:hover{filter:brightness(1.12); box-shadow:0 0 26px #e94aff88}
  .btn-dl:disabled{opacity:.4; cursor:default; filter:none; box-shadow:none}
  .btn-stop{background:#2a1322; color:var(--error); border:1px solid #fb718555;
    padding:13px 16px; border-radius:11px; font-weight:700}
  .btn-stop:disabled{opacity:.35; cursor:default}

  .folder{display:flex; gap:8px; align-items:center}
  .folder .path{flex:1; font-size:11.5px; color:var(--text); background:var(--card);
    border:1px solid var(--border); border-radius:8px; padding:9px 11px;
    white-space:nowrap; user-select:text; outline:none; font-family:inherit;
    transition:.15s}
  .folder .path::placeholder{color:var(--muted)}
  .folder .path:focus{border-color:var(--magenta); box-shadow:0 0 0 2px #e94aff22}
  .folder .path.ok{border-color:var(--success)}
  .folder .path.bad{border-color:var(--error)}
  .folder-hint{font-size:10.5px; line-height:1.5; white-space:pre-wrap;
    color:var(--muted); padding:0 2px; max-height:0; overflow:hidden;
    transition:max-height .2s}
  .folder-hint.ok{color:var(--success); max-height:60px}
  .folder-hint.bad{color:var(--warn); max-height:140px;
    font-family:ui-monospace,monospace; user-select:text}

  /* ── progress + status ──────────────────────────────── */
  .prog{height:8px; background:var(--card); border-radius:6px; overflow:hidden;
    border:1px solid var(--border)}
  .prog .fill{height:100%; width:0%;
    background:linear-gradient(90deg,var(--deep),var(--magenta),var(--cyan));
    box-shadow:0 0 12px #e94aff88; transition:width .3s; border-radius:6px}
  .statusbar{padding:9px 18px; font-size:12px; border-top:1px solid var(--border);
    background:#0d0d1c; display:flex; align-items:center; gap:9px; min-height:34px}
  .statusbar .sled{width:7px;height:7px;border-radius:50%;background:var(--sub);flex:none}
  .statusbar.info .sled{background:var(--cyan)} .statusbar.info{color:var(--text)}
  .statusbar.warn .sled{background:var(--warn)} .statusbar.warn{color:var(--warn)}
  .statusbar.success .sled{background:var(--success)} .statusbar.success{color:var(--success)}
  .statusbar.error .sled{background:var(--error)} .statusbar.error{color:var(--error)}

  ::-webkit-scrollbar{width:10px; height:10px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--border2); border-radius:6px;
    border:2px solid transparent; background-clip:content-box}
  ::-webkit-scrollbar-thumb:hover{background:var(--purple)}
</style>
</head>
<body>
<div class="app">
  <!-- top bar -->
  <div class="topbar">
    <div class="logo"><span class="dot">◆</span>PAHE<span class="dl">DL</span></div>
    <div class="searchwrap">
      <div class="searchbox">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        <input id="q" placeholder="Search anime…  (empty = Latest)" autocomplete="off">
      </div>
      <button class="btn btn-primary" onclick="doSearch()">Search</button>
      <button class="btn btn-ghost active" id="btnCatalog" onclick="loadCatalog()">Browse all</button>
      <button class="btn btn-ghost cyan" id="btnLatest" onclick="loadLatest()">Latest</button>
    </div>
    <div class="engine">
      <div class="dot" id="engineDot"><span class="led"></span><span id="engineTxt">starting…</span></div>
      <button class="btn btn-ghost" onclick="showBrowser()">Show Browser</button>
    </div>
  </div>

  <!-- main -->
  <div class="main">
    <!-- results -->
    <div class="panel">
      <div class="panel-head">
        <span class="magenta" id="resTitle">RESULTS</span>
        <span class="meta" id="resMeta"></span>
      </div>
      <div class="grid" id="grid">
        <div class="empty"><div class="big">◆</div>Waiting for the browser engine…</div>
      </div>
    </div>

    <!-- episodes + download -->
    <div class="panel rightpanel">
      <div class="panel-head">
        <span class="cyan" id="epHead">EPISODES</span>
        <span class="meta" id="epSel"></span>
      </div>
      <div class="ep-meta" id="epMeta">Pick an anime on the left.</div>
      <div class="ep-list" id="epList"></div>

      <div class="controls">
        <div class="ctl-row">
          <div class="seg"><label>Range</label>
            <input class="mini-input range-in" id="rFrom" placeholder="1">
            <span style="color:var(--muted)">–</span>
            <input class="mini-input range-in" id="rTo" placeholder="12"></div>
          <span class="quick" onclick="applyRange()">Apply</span>
          <span class="quick" onclick="selectAll(true)">All</span>
          <span class="quick" onclick="selectAll(false)">None</span>
        </div>
        <div class="ctl-row">
          <div class="seg"><label>Quality</label>
            <select id="quality"><option>1080</option><option>720</option><option>480</option><option>360</option></select></div>
          <div class="seg"><label>Audio</label>
            <select id="audio"><option value="jpn">jpn</option><option value="eng">eng</option></select></div>
          <div class="seg"><label>Season</label>
            <input class="mini-input" id="season" value="1"></div>
          <label class="chk"><input type="checkbox" id="jellyfin" checked>Jellyfin naming</label>
        </div>
        <div class="folder">
          <input class="path" id="folderPath" placeholder="Paste a folder path or smb:// share, or use Folder…">
          <button class="btn btn-ghost" onclick="chooseFolder()">Folder…</button>
        </div>
        <div class="folder-hint" id="folderHint"></div>
        <div class="prog"><div class="fill" id="progFill"></div></div>
        <div class="dlbar">
          <button class="btn btn-dl" id="btnDl" onclick="startDownload()" disabled>Download</button>
          <button class="btn btn-stop" id="btnStop" onclick="stopDownload()" disabled>Stop</button>
        </div>
      </div>
    </div>
  </div>

  <!-- status -->
  <div class="statusbar warn" id="status"><span class="sled"></span><span id="statusTxt">Starting…</span></div>
</div>

<script>
  let RESULTS=[], EPISODES=[], SELECTED=new Set(), FOLDER="", CUR_TITLE="";
  let apiReady=false;

  function api(){ return window.pywebview.api; }

  window.addEventListener('pywebviewready', ()=>{ apiReady=true; });

  // ── rendering ────────────────────────────────────────
  window.renderResults = (data)=>{
    RESULTS = data.results || [];
    document.getElementById('resMeta').textContent =
      RESULTS.length ? RESULTS.length+" titles" : "";
    const v = data.view;
    document.getElementById('btnCatalog').classList.toggle('active', v==='catalog');
    document.getElementById('btnLatest').classList.toggle('active', v==='latest');
    const g = document.getElementById('grid');
    if(!RESULTS.length){
      g.className='grid';
      g.innerHTML = '<div class="empty"><div class="big">∅</div>Nothing to show.</div>';
      return;
    }
    if(v==='catalog'){
      // Catalog has no artwork — render a clean, dense title list instead of
      // image cards. With thousands of entries, putting every row in the DOM at
      // once makes scrolling sluggish, so we render in chunks and append more as
      // the user scrolls near the bottom (infinite scroll).
      g.className='listview';
      g.innerHTML='';
      _catalogShown = 0;
      renderCatalogChunk(g);
      g.onscroll = ()=>{
        if(g.scrollTop + g.clientHeight >= g.scrollHeight - 400){
          renderCatalogChunk(g);
        }
      };
      return;
    }
    g.onscroll = null;
    // Search / Latest → poster card grid
    g.className='grid';
    g.innerHTML = '';
    RESULTS.forEach((r,i)=>{
      const card = document.createElement('div');
      card.className='card'; card.onclick=()=>pickAnime(i);
      const badge = r.type==='airing' && r.status ? `<div class="badge">${escapeHtml(r.status)}</div>` : '';
      // Start with a fallback tile; fetch the real image via Python (the site's
      // image host blocks direct cross-origin loads), then swap it in.
      card.innerHTML =
        `<div class="thumb fallback" data-i="${i}"><span>◆</span>${badge}</div>`+
        `<div class="cap">${escapeHtml(r.title)}</div>`;
      g.appendChild(card);
    });
    lazyLoadPosters();
  };

  let _posterQueue=[];
  let _catalogShown=0;
  const CATALOG_CHUNK=300;
  function renderCatalogChunk(g){
    const end = Math.min(_catalogShown + CATALOG_CHUNK, RESULTS.length);
    const frag = document.createDocumentFragment();
    for(let i=_catalogShown; i<end; i++){
      const r=RESULTS[i];
      const row=document.createElement('div');
      row.className='list-row'; row.dataset.i=i; row.onclick=()=>pickAnime(i);
      row.innerHTML=`<span class="li-dot">◆</span><span class="li-title">${escapeHtml(r.title)}</span>`;
      frag.appendChild(row);
    }
    g.appendChild(frag);
    _catalogShown = end;
  }
  function lazyLoadPosters(){
    // Build a queue of cards that have a poster URL, load a few at a time so we
    // don't hammer the engine thread.
    _posterQueue = RESULTS.map((r,i)=>({i, url:r.poster}))
                          .filter(x=>x.url && x.url.length>4);
    pumpPosters();
  }
  async function pumpPosters(){
    if(!apiReady || !_posterQueue.length) return;
    const batch = _posterQueue.splice(0, 4);
    await Promise.all(batch.map(async ({i,url})=>{
      try{
        const data = await api().get_image(url);
        if(data){
          const thumb = document.querySelector(`.thumb[data-i='${i}']`);
          if(thumb){
            thumb.classList.remove('fallback');
            thumb.style.backgroundImage = `url('${data}')`;
            const span = thumb.querySelector('span'); if(span) span.remove();
          }
        }
      }catch(e){}
    }));
    if(_posterQueue.length) setTimeout(pumpPosters, 60);
  }

  window.renderEpisodes = (data)=>{
    EPISODES = data.episodes || [];
    CUR_TITLE = data.title || "";
    SELECTED.clear();
    document.getElementById('epHead').textContent = 'EPISODES';
    document.getElementById('epMeta').textContent =
      CUR_TITLE + " · " + EPISODES.length + " episodes";
    const l = document.getElementById('epList');
    l.innerHTML='';
    EPISODES.forEach((ep,i)=>{
      const row=document.createElement('div');
      row.className='ep-row'; row.dataset.i=i; row.onclick=()=>toggleEp(i);
      row.innerHTML =
        `<div class="ep-check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 13l4 4L19 7"/></svg></div>`+
        `<div class="ep-num">EP ${ep.episode}</div>`+
        `<div class="ep-title">${escapeHtml(ep.title||'—')}</div>`;
      l.appendChild(row);
    });
    updateSel(); updateDlState();
  };

  // ── actions ──────────────────────────────────────────
  function doSearch(){ if(!apiReady)return; api().search(document.getElementById('q').value); }
  function loadLatest(){ if(!apiReady)return; api().load_latest(); }
  function loadCatalog(){ if(!apiReady)return; api().load_catalog(); }
  function showBrowser(){ if(!apiReady)return; api().show_browser(); }

  document.getElementById('q').addEventListener('keydown',e=>{ if(e.key==='Enter')doSearch(); });

  function pickAnime(i){
    const r=RESULTS[i]; if(!r)return;
    document.querySelectorAll('.card,.list-row').forEach((c,j)=>c.classList.toggle('sel',j===i));
    document.getElementById('epMeta').textContent='Loading episodes…';
    api().get_episodes(r.id, r.title);
  }

  function toggleEp(i){
    if(SELECTED.has(i))SELECTED.delete(i); else SELECTED.add(i);
    document.querySelector(`.ep-row[data-i='${i}']`).classList.toggle('sel',SELECTED.has(i));
    updateSel(); updateDlState();
  }
  function selectAll(on){
    SELECTED.clear();
    if(on)EPISODES.forEach((_,i)=>SELECTED.add(i));
    document.querySelectorAll('.ep-row').forEach(r=>r.classList.toggle('sel',on));
    updateSel(); updateDlState();
  }
  function applyRange(){
    const lo=parseFloat(document.getElementById('rFrom').value);
    const hi=parseFloat(document.getElementById('rTo').value);
    if(isNaN(lo)||isNaN(hi))return;
    SELECTED.clear();
    EPISODES.forEach((ep,i)=>{ const n=parseFloat(ep.episode);
      if(!isNaN(n)&&n>=lo&&n<=hi)SELECTED.add(i); });
    document.querySelectorAll('.ep-row').forEach(r=>{
      const i=+r.dataset.i; r.classList.toggle('sel',SELECTED.has(i)); });
    updateSel(); updateDlState();
  }
  function updateSel(){
    document.getElementById('epSel').textContent = SELECTED.size? SELECTED.size+" selected":"";
  }
  function updateDlState(){
    document.getElementById('btnDl').disabled = SELECTED.size===0 || !FOLDER;
  }

  async function chooseFolder(){
    if(!apiReady)return;
    const f = await api().choose_folder();
    if(f){
      document.getElementById('folderPath').value = f;
      setFolder(f);
    }
  }

  // Validate / resolve a typed/pasted/picked path. Handles smb:// URLs by
  // finding their mount (or showing guidance).
  let _folderTimer=null;
  async function setFolder(path){
    const el = document.getElementById('folderPath');
    const hintEl = document.getElementById('folderHint');
    const raw = (path||'').trim();
    if(!raw){ FOLDER=''; el.classList.remove('ok','bad'); hintEl.textContent=''; hintEl.className='folder-hint'; updateDlState(); return; }
    if(!apiReady){ updateDlState(); return; }
    try{
      const res = await api().resolve_folder(raw);
      FOLDER = res.ok ? res.path : '';
      el.classList.toggle('ok', res.ok);
      el.classList.toggle('bad', !res.ok);
      // If we resolved an smb:// URL to a real mounted path, show it in the box.
      if(res.ok && res.path && res.path!==raw){ el.value = res.path; }
      hintEl.textContent = res.hint || '';
      hintEl.className = 'folder-hint' + (res.ok ? ' ok' : (res.hint ? ' bad' : ''));
    }catch(e){}
    updateDlState();
  }
  // debounce typing on the folder field (script runs at end of <body>, so the
  // element already exists)
  (function(){
    const el=document.getElementById('folderPath');
    if(el) el.addEventListener('input',()=>{
      clearTimeout(_folderTimer);
      _folderTimer=setTimeout(()=>setFolder(el.value), 350);
    });
  })();

  function startDownload(){
    if(!apiReady || SELECTED.size===0 || !FOLDER) return;
    const eps=[...SELECTED].sort((a,b)=>a-b).map(i=>({
      ep:EPISODES[i].episode, title:CUR_TITLE,
      session:EPISODES[i].session, anime_id:EPISODES[i].anime_id }));
    api().start_download({
      episodes:eps, dest:FOLDER,
      quality:document.getElementById('quality').value,
      audio:document.getElementById('audio').value,
      season:parseInt(document.getElementById('season').value)||1,
      jellyfin:document.getElementById('jellyfin').checked
    });
    document.getElementById('btnDl').disabled=true;
    document.getElementById('btnStop').disabled=false;
  }
  function stopDownload(){ if(apiReady)api().stop_download();
    document.getElementById('btnStop').disabled=true; }

  function escapeHtml(s){ return (s||'').replace(/[&<>"']/g,c=>(
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  // ── polling for status / progress / engine ───────────
  async function poll(){
    if(apiReady){
      try{
        const s=await api().poll_status();
        const bar=document.getElementById('status');
        bar.className='statusbar '+(s.kind||'info');
        document.getElementById('statusTxt').textContent=s.msg;

        const p=await api().poll_progress();
        document.getElementById('progFill').style.width=(p.value||0)+'%';

        const e=await api().engine_state();
        const dot=document.getElementById('engineDot');
        dot.className='dot'+(e.ready?' ready':'')+(e.dot==='error'?' error':'');
        document.getElementById('engineTxt').textContent=
          e.dot==='minimized'?'ready':e.dot;
        // re-enable download button when a run finishes
        if(p.value>=100 || (s.msg&&s.msg.startsWith('Done'))){
          document.getElementById('btnStop').disabled=true;
          updateDlState();
        }
      }catch(err){}
    }
    setTimeout(poll, 600);
  }
  poll();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Web UI  —  pywebview frontend + Python API bridge
#  (The backend above — engine, search/browse/episode fetch, resolve, download —
#   is unchanged. This layer replaces the old Tkinter GUI with an HTML/CSS/JS
#   window driven through pywebview's JS↔Python bridge.)
# ══════════════════════════════════════════════════════════════════════════════


class Api:
    """
    Exposed to JavaScript as window.pywebview.api.<method>.
    Only basic types (str/int/dict/list/bool) cross the bridge. Long-running
    work (engine start, searches, downloads) runs in background threads and the
    UI polls poll_status()/poll_progress() for live updates.
    """

    def __init__(self):
        self._window = None
        self._engine_ready = False
        self._stop_event = threading.Event()
        self._downloading = False
        # Thread-safe channels the JS side polls.
        self._status = {"msg": "Starting…", "kind": "warn"}
        self._progress = {"value": 0.0, "label": ""}
        self._results = []            # last results shown (for episode lookups)
        self._current_title = ""
        self._engine_dot = "starting"

    # ── wiring ────────────────────────────────────────────────────────────────
    def _set_status(self, msg, kind="info"):
        self._status = {"msg": msg, "kind": kind}
        _log(f"UI[{kind}]: {msg}")

    def _set_progress(self, value, label=""):
        self._progress = {"value": round(value, 1), "label": label}

    def poll_status(self):
        return self._status

    def poll_progress(self):
        return self._progress

    def engine_state(self):
        return {"ready": self._engine_ready, "dot": self._engine_dot}

    # ── engine lifecycle ────────────────────────────────────────────────────────
    def start_engine(self):
        """Kick off the persistent browser engine in the background."""
        # Guard against being called more than once (boot + UI poll could both
        # fire it, which would spawn multiple browser windows).
        if getattr(self, "_engine_starting", False) or self._engine_ready:
            return True
        self._engine_starting = True
        self._engine_dot = "starting"
        self._set_status("Opening browser — if you see 'Verify you are human', "
                         "click it in the browser window.", "warn")

        def run():
            try:
                get_engine(headless=False,
                           status_cb=lambda m: self._set_status(m, "warn"),
                           on_challenge=lambda: self._on_challenge())
                self._engine_ready = True
                self._engine_dot = "ready"
                self._set_status("Browser ready. Minimizing it — browse away.", "success")
                try:
                    get_engine().go_headless()
                    self._engine_dot = "minimized"
                except Exception as e:
                    _log(f"UI: minimize failed {type(e).__name__}")
                # Auto-load the full catalog once ready.
                self.load_catalog()
            except Exception as e:
                self._engine_dot = "error"
                self._engine_starting = False   # allow a retry
                self._set_status(f"Engine failed to start: {e}", "error")

        threading.Thread(target=run, daemon=True).start()
        return True

    def _on_challenge(self):
        self._engine_dot = "challenge"
        self._set_status("Cloudflare check reappeared — solve it in the browser "
                         "window; it hides again after.", "warn")

    def show_browser(self):
        if not self._engine_ready:
            self._set_status("Engine still starting…", "warn")
            return False
        self._set_status("Bringing the browser forward… solve any check, then it hides.", "warn")
        threading.Thread(target=lambda: get_engine().go_visible(), daemon=True).start()
        return True

    # ── browse / search ──────────────────────────────────────────────────────────
    def _guard(self):
        if not self._engine_ready:
            self._set_status("Browser engine still starting — try again in a moment…", "warn")
            return False
        return True

    def search(self, query):
        query = (query or "").strip()
        if not query:
            return self.load_latest()
        if not self._guard():
            return False
        self._set_status(f'Searching for "{query}"…', "warn")

        def run():
            try:
                results = search_anime(query)
                self._results = results
                self._push_results(results, f'{len(results)} result(s) for "{query}"' if results
                                   else f'No results for "{query}".')
            except Exception as e:
                self._set_status(f"Search error: {e}", "error")
        threading.Thread(target=run, daemon=True).start()
        return True

    def load_latest(self):
        if not self._guard():
            return False
        self._set_status("Loading latest releases…", "warn")

        def run():
            try:
                results, _ = fetch_latest(page=1)
                self._results = results
                self._push_results(results, "Latest releases", view="latest")
            except Exception as e:
                self._set_status(f"Browse error: {e}", "error")
        threading.Thread(target=run, daemon=True).start()
        return True

    def load_catalog(self):
        if not self._guard():
            return False
        self._set_status("Loading full catalog — this can take a few seconds…", "warn")

        def run():
            try:
                results = fetch_catalog(status_cb=lambda m: self._set_status(m, "warn"))
                self._results = results
                self._push_results(results, f"Full catalog · {len(results)} titles", view="catalog")
            except Exception as e:
                self._set_status(f"Catalog error: {e}", "error")
        threading.Thread(target=run, daemon=True).start()
        return True

    def _push_results(self, results, status_msg, view="search"):
        """Send results to the JS side by calling a global render function."""
        payload = json.dumps({"results": results, "view": view, "status": status_msg})
        self._set_status(status_msg, "success" if results else "warn")
        if self._window:
            # escape for safe embedding in evaluate_js
            safe = payload.replace("\\", "\\\\").replace("`", "\\`")
            try:
                self._window.evaluate_js(f"window.renderResults(JSON.parse(`{safe}`))")
            except Exception as e:
                _log(f"UI: push_results evaluate_js failed {e}")

    # ── episodes ──────────────────────────────────────────────────────────────────
    def get_image(self, url):
        """
        Return a poster/snapshot as an inline base64 data: URL.

        The image host is behind Cloudflare, so the bytes are fetched through the
        engine's authenticated browser context (which holds clearance cookies),
        cached to a temp file, then returned as a data: URI. We use a data URI
        (not a file:// path) because WebKitGTK refuses to render a file:// image
        that lives outside the page's own folder. Posters are small webps, so the
        data URL crosses the bridge fine.
        """
        if not url or not self._engine_ready:
            return ""
        import hashlib, tempfile, base64, mimetypes
        try:
            cache_dir = os.path.join(tempfile.gettempdir(), "pahe_dl_thumbs")
            os.makedirs(cache_dir, exist_ok=True)
            ext = ".webp"
            m = re.search(r"\.(webp|jpg|jpeg|png|gif)(?:\?|$)", url, re.I)
            if m:
                ext = "." + m.group(1).lower()
            fpath = os.path.join(cache_dir, hashlib.md5(url.encode()).hexdigest() + ext)
            if os.path.exists(fpath):
                data = open(fpath, "rb").read()
            else:
                data = get_engine().fetch_image(url)
                if not data:
                    return ""
                with open(fpath, "wb") as f:
                    f.write(data)
            ctype = mimetypes.guess_type(fpath)[0] or "image/webp"
            return f"data:{ctype};base64," + base64.b64encode(data).decode("ascii")
        except Exception as e:
            _log(f"UI: get_image failed {type(e).__name__} {url[:70]}")
            return ""

    def get_episodes(self, anime_id, title):
        if not self._guard():
            return False
        self._current_title = title
        self._set_status(f"Loading episodes for {title}…", "warn")

        def run():
            try:
                eps = fetch_episodes(anime_id)
                payload = json.dumps({"episodes": eps, "title": title})
                safe = payload.replace("\\", "\\\\").replace("`", "\\`")
                if self._window:
                    self._window.evaluate_js(f"window.renderEpisodes(JSON.parse(`{safe}`))")
                self._set_status(f"Loaded {len(eps)} episodes for {title}.", "success")
            except Exception as e:
                self._set_status(f"Episode load error: {e}", "error")
        threading.Thread(target=run, daemon=True).start()
        return True

    # ── download ────────────────────────────────────────────────────────────────
    def choose_folder(self):
        """Open a native folder picker, return the chosen path (or '')."""
        if not self._window:
            return ""
        # pywebview renamed the folder-dialog constant; prefer the new one,
        # fall back to the old for older pywebview versions.
        try:
            folder_mode = webview.FileDialog.FOLDER
        except AttributeError:
            folder_mode = webview.FOLDER_DIALOG
        dirs = self._window.create_file_dialog(folder_mode)
        if dirs:
            return dirs[0] if isinstance(dirs, (list, tuple)) else dirs
        return ""

    def check_folder(self, path):
        """True if `path` is an existing, writable directory. Works for mounted
        SMB/NAS shares since the OS presents them as ordinary paths."""
        try:
            path = (path or "").strip()
            return bool(path) and os.path.isdir(path) and os.access(path, os.W_OK)
        except Exception:
            return False

    def resolve_folder(self, raw):
        """
        Turn whatever the user typed into a usable local path.

        - A normal path is validated as-is.
        - An smb:// URL can't be written to directly (it's a network address,
          not a filesystem path). We try to find where the OS has already
          mounted it (GVFS, or a CIFS mount), and if we can't, we return a hint
          telling the user how to mount it and what to paste instead.

        Returns {path, ok, hint}. `path` is the resolved local path to actually
        use (may equal the input); `ok` is whether it's writable now.
        """
        raw = (raw or "").strip()
        if not raw:
            return {"path": "", "ok": False, "hint": ""}

        # Plain filesystem path → validate directly.
        if not raw.lower().startswith("smb://"):
            ok = self.check_folder(raw)
            hint = "" if ok else "Folder not found or not writable."
            return {"path": raw, "ok": ok, "hint": hint}

        # smb:// URL → parse out host + share, then look for a mount.
        import urllib.parse, getpass
        u = urllib.parse.urlparse(raw)
        host = u.hostname or ""
        parts = [p for p in (u.path or "").split("/") if p]
        share = parts[0] if parts else ""
        subpath = "/".join(parts[1:]) if len(parts) > 1 else ""
        if not host or not share:
            return {"path": raw, "ok": False,
                    "hint": "Couldn't read that smb:// address. Expected smb://host/Share/…"}

        candidates = []
        # 1) GVFS (what a file-manager "Connect to Server" produces)
        try:
            uid = os.getuid()
            gvfs = f"/run/user/{uid}/gvfs"
            if os.path.isdir(gvfs):
                for entry in os.listdir(gvfs):
                    # entry looks like: smb-share:server=192.168.2.116,share=anime
                    el = entry.lower()
                    if "smb-share:" in el and f"server={host.lower()}" in el \
                       and f"share={share.lower()}" in el:
                        candidates.append(os.path.join(gvfs, entry))
        except Exception:
            pass
        # 2) Common CIFS mount points
        for base in ("/mnt", "/media", os.path.expanduser("~/mnt")):
            try:
                if os.path.isdir(base):
                    for entry in os.listdir(base):
                        if entry.lower() == share.lower():
                            candidates.append(os.path.join(base, entry))
            except Exception:
                pass

        for c in candidates:
            full = os.path.join(c, subpath) if subpath else c
            if self.check_folder(full):
                return {"path": full, "ok": True,
                        "hint": f"Found mounted share → {full}"}

        # Not mounted anywhere we can see → guide the user.
        user = getpass.getuser()
        hint = ("That SMB share isn't mounted yet. Either open it once in your file "
                f"manager (Connect to Server → {raw}), then paste the path that "
                f"appears under /run/user/{os.getuid()}/gvfs/ — or mount it properly:\n"
                f"  sudo mkdir -p /mnt/{share}\n"
                f"  sudo mount -t cifs //{host}/{share} /mnt/{share} "
                f"-o username={user},uid=$(id -u),gid=$(id -g)\n"
                f"…then paste  /mnt/{share}")
        return {"path": raw, "ok": False, "hint": hint}

    def start_download(self, payload):
        """
        payload: {episodes:[{ep,title,session,anime_id}], dest, quality, audio,
                  season, jellyfin(bool)}
        """
        if self._downloading:
            self._set_status("A download is already running.", "warn")
            return False
        selected = payload.get("episodes", [])
        if not selected:
            self._set_status("Tick at least one episode.", "error")
            return False
        dest = (payload.get("dest") or "").strip()
        if not dest or not os.path.isdir(dest):
            self._set_status("Choose a valid download folder.", "error")
            return False

        quality = str(payload.get("quality", "1080"))
        audio   = str(payload.get("audio", "jpn"))
        season  = int(payload.get("season", 1) or 1)
        jellyfin = bool(payload.get("jellyfin", True))
        batch_title = self._current_title

        self._stop_event.clear()
        self._downloading = True
        self._set_progress(0, "")

        def run():
            import traceback
            total = len(selected)
            idx = 0
            rl_strikes = ctx_strikes = res_strikes = 0
            try:
                while idx < len(selected):
                    item = selected[idx]
                    if self._stop_event.is_set():
                        self._set_status("Stopped.", "warn")
                        break
                    ep_num = item["ep"]
                    print(f"\n[DL] Starting Ep {ep_num} | anime_id={item['anime_id']} | session={item['session'][:16]}…")
                    self._set_status(f"[{idx+1}/{total}] Finding download link for Ep {ep_num}…", "warn")
                    try:
                        options, scraped_title = get_download_options(item["anime_id"], item["session"])
                        if not options:
                            self._set_status(f"Ep {ep_num}: No download links — skipping.", "error")
                            idx += 1; continue
                        chosen = pick_download_option(options, quality, audio)

                        # Work out the destination path up-front (we need it to
                        # save the browser download straight to disk).
                        anime_title = batch_title or item.get("title") or scraped_title
                        safe_title = re.sub(r'[\\/:*?"<>|]', "", anime_title).strip() or "Unknown"
                        ep_int = int(re.sub(r"\D", "", str(ep_num)) or 0)
                        se_tag = f"S{season:02d}E{ep_int:02d}"
                        if jellyfin:
                            series_dir = os.path.join(dest, safe_title)
                            os.makedirs(series_dir, exist_ok=True)
                            fname = f"{safe_title} - {se_tag} [{chosen['quality']}p].mp4"
                            dest_path = os.path.join(series_dir, fname)
                        else:
                            fname = f"{safe_title} - Ep{str(ep_num).zfill(3)} [{chosen['quality']}p].mp4"
                            dest_path = os.path.join(dest, fname)

                        # save_as gives no per-byte progress, so we drive the bar
                        # by batch position: show it partway while this episode
                        # downloads, then step it to the next slot when done.
                        def file_prog(frac, ep_done=idx):
                            # frac is 0.05 at start, 1.0 at finish → nudge the bar
                            # a little within this episode's slot for feedback.
                            overall = (ep_done + min(frac, 0.9)) / total * 100
                            self._set_progress(overall, f"Ep {ep_num} of {total}")

                        self._set_status(f"[{idx+1}/{total}] Downloading Ep {ep_num}…", "info")

                        # Resolve AND download via the browser (the CDN rejects
                        # urllib with 403; only a real browser download works).
                        result = resolve_and_save(chosen["url"], dest_path,
                                                  progress_cb=file_prog,
                                                  stop_event=self._stop_event)
                        if not (result and isinstance(result, tuple) and result[0] == "SAVED"):
                            # Retry once with a fresh link if the browser download
                            # didn't complete.
                            self._set_status(f"Ep {ep_num}: link slow, retrying…", "warn")
                            options2, _ = get_download_options(item["anime_id"], item["session"])
                            if options2:
                                chosen2 = pick_download_option(options2, quality, audio)
                                result = resolve_and_save(chosen2["url"], dest_path,
                                                          progress_cb=file_prog,
                                                          stop_event=self._stop_event)
                        if not (result and isinstance(result, tuple) and result[0] == "SAVED"):
                            res_strikes += 1
                            if res_strikes <= 3:
                                wait_s = min(120, 30 * res_strikes)
                                for r in range(wait_s, 0, -1):
                                    if self._stop_event.is_set(): break
                                    self._set_status(f"Links throttled — waiting {r}s before retrying Ep {ep_num}…", "warn")
                                    time.sleep(1)
                                continue
                            self._set_status(f"Ep {ep_num}: Could not download after retries — skipping.", "error")
                            res_strikes = 0; idx += 1; continue
                        res_strikes = 0
                        self._set_progress((idx + 1) / total * 100, f"Ep {ep_num} done")
                        rl_strikes = ctx_strikes = 0
                        idx += 1
                    except RateLimited:
                        rl_strikes += 1
                        wait_s = min(120, 30 * rl_strikes)
                        for r in range(wait_s, 0, -1):
                            if self._stop_event.is_set(): break
                            self._set_status(f"Rate limited — waiting {r}s before retrying Ep {ep_num}…", "warn")
                            time.sleep(1)
                        continue
                    except Exception as e:
                        tb = traceback.format_exc()
                        print(f"[DL] EXCEPTION for Ep {ep_num}:\n{tb}")
                        if "TargetClosedError" in tb or "Target page" in str(e):
                            ctx_strikes += 1
                            if ctx_strikes <= 3:
                                for r in range(15, 0, -1):
                                    if self._stop_event.is_set(): break
                                    self._set_status(f"Browser reset — waiting {r}s before retrying Ep {ep_num}…", "warn")
                                    time.sleep(1)
                                continue
                        self._set_status(f"Ep {ep_num} error: {e}", "error")
                        idx += 1; continue

                    self._set_progress(idx / total * 100, "")
                    if idx < len(selected) and not self._stop_event.is_set():
                        time.sleep(1.5)

                if not self._stop_event.is_set():
                    self._set_status(f"Done! {total} episode(s) saved to {dest}", "success")
                    self._set_progress(100, "Complete")
            finally:
                self._downloading = False
                # Reclaim disk space after the batch: sweep orphaned browser
                # scratch files. The sweep skips anything modified in the last
                # few minutes, so the live browser's own files are never touched.
                _cleanup_playwright_artifacts()

        threading.Thread(target=run, daemon=True).start()
        return True

    def stop_download(self):
        self._stop_event.set()
        self._set_status("Stopping after current file…", "warn")
        return True


def _on_closed():
    try:
        if _engine is not None:
            _engine.shutdown()
    except Exception:
        pass


def main():
    api = Api()

    # WebKitGTK (Linux) can fail to paint a large inline html= string, leaving a
    # blank window. Writing the page to a temp file and loading it by URL is the
    # reliable path across backends.
    import tempfile
    html_path = os.path.join(tempfile.gettempdir(), "pahe_dl_ui.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(INDEX_HTML)
    _log(f"UI: wrote frontend to {html_path}")

    window = webview.create_window(
        "PAHE DL",
        url=html_path,
        js_api=api,
        width=1180, height=780, min_size=(940, 600),
        background_color="#0a0a14",
    )
    api._window = window
    window.events.closed += _on_closed

    def boot():
        # Wait until the bridge is ready, then start the engine.
        api.start_engine()

    # Use the bundled icon if we can find it (works in both source and frozen
    # builds). Not all pywebview backends honour it, so it's best-effort.
    icon_path = None
    for cand in ("app_icon.ico", "icon.png"):
        # When frozen, PyInstaller unpacks data next to sys._MEIPASS.
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(base, cand)
        if os.path.exists(p):
            icon_path = p
            break
    try:
        if icon_path:
            webview.start(boot, debug=False, icon=icon_path)
        else:
            webview.start(boot, debug=False)
    except TypeError:
        # Older pywebview without the icon kwarg
        webview.start(boot, debug=False)


if __name__ == "__main__":
    main()
