#!/usr/bin/env python3
"""
Persistent browser engine + network layer for the AnimePahe downloader.

Owns the single stealth-Chromium session (BrowserEngine), the config/log/settings
file paths, the settings load/save helpers, and the low-level fetch/stream
functions. No GUI or scraping logic lives here — see pahe_scrape and the app
entry point (animepahe_downloader.py).

For educational and personal use only. GPLv3 — see LICENSE.
"""
import os
import sys
import re
import time
import json
import threading
import queue
from pathlib import Path
from urllib.parse import urlencode, urlparse

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

PROFILE_DIR   = Path.home() / ".config" / "animepahe-dl" / "chromium-profile"
LOG_PATH      = Path.home() / ".config" / "animepahe-dl" / "engine.log"
SETTINGS_PATH = Path.home() / ".config" / "animepahe-dl" / "settings.json"

# Which UI fields we persist between runs. Season is deliberately excluded — it's
# per-show and auto-detected from the title each time an anime is opened.
_SETTINGS_KEYS = ("quality", "audio", "jellyfin", "skip_existing",
                  "concurrency", "folder")


def load_settings() -> dict:
    """Read persisted UI settings; return {} if none/unreadable."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: data[k] for k in _SETTINGS_KEYS if k in data}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    """Persist the whitelisted UI settings to disk (best-effort)."""
    try:
        clean = {k: data[k] for k in _SETTINGS_KEYS if k in (data or {})}
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
    except Exception as e:
        _log(f"SETTINGS: save failed {type(e).__name__}")

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


def _playwright_partial_size(since_ts: float) -> int:
    """
    Size of the file Playwright is currently downloading. A browser download in
    progress is written into a temp 'playwright-artifacts-*' dir before save_as
    copies it out, so the growing file there IS the live download. We report the
    largest recently-touched file, which lets the UI show real byte progress even
    though save_as itself has no per-byte callback. Returns 0 if none found.
    """
    import glob, tempfile
    best = 0
    try:
        for d in glob.glob(os.path.join(tempfile.gettempdir(), "playwright-artifacts-*")):
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    try:
                        st = os.stat(os.path.join(root, fn))
                        if st.st_mtime >= since_ts - 2 and st.st_size > best:
                            best = st.st_size
                    except OSError:
                        pass
    except Exception:
        pass
    return best


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

    def _win_hide_browser(self):
        """
        On Windows the CDP 'minimized' state is unreliable — the window pops back
        on every new tab/navigation, causing a visible flash. We use the Win32
        API to HIDE the Chromium window entirely (SW_HIDE). A hidden window can't
        flash when tabs open behind it. The "Show Browser" button un-hides it.
        Returns True if it hid a window.
        """
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            SW_HIDE = 0
            hidden = [False]
            self._win_hwnds = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _enum(hwnd, lparam):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value == "Chrome_WidgetWin_1" and user32.IsWindowVisible(hwnd):
                    tlen = user32.GetWindowTextLengthW(hwnd)
                    if tlen > 0:
                        user32.ShowWindow(hwnd, SW_HIDE)
                        self._win_hwnds.append(hwnd)
                        hidden[0] = True
                return True

            user32.EnumWindows(_enum, 0)
            return hidden[0]
        except Exception as e:
            _log(f"WINDOW: win32 hide failed {type(e).__name__}")
            return False

    def _win_show_browser(self):
        """Un-hide the Chromium window(s) we hid with SW_HIDE (for 'Show Browser'
        and for Cloudflare re-solves)."""
        if os.name != "nt":
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            SW_SHOW = 5
            SW_RESTORE = 9
            shown = False
            for hwnd in getattr(self, "_win_hwnds", []):
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.ShowWindow(hwnd, SW_SHOW)
                user32.SetForegroundWindow(hwnd)
                shown = True
            # Also scan for any hidden Chrome windows we may have missed.
            from ctypes import wintypes
            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _enum(hwnd, lparam):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value == "Chrome_WidgetWin_1" and not user32.IsWindowVisible(hwnd):
                    if user32.GetWindowTextLengthW(hwnd) > 0:
                        user32.ShowWindow(hwnd, SW_RESTORE)
                        user32.ShowWindow(hwnd, SW_SHOW)
                return True
            user32.EnumWindows(_enum, 0)
            return shown
        except Exception as e:
            _log(f"WINDOW: win32 show failed {type(e).__name__}")
            return False

    def _window_state(self, state: str):
        """Set the browser window state via CDP: 'minimized' or 'normal'.
        Keeps the SAME browser alive (no relaunch), so Cloudflare clearance
        is never lost. On Windows, prefer the native Win32 minimize (the CDP
        one is unreliable and flashes); fall back to off-screen elsewhere."""
        # On Windows, use the native minimize which actually sticks.
        if state == "minimized" and os.name == "nt":
            if self._win_hide_browser():
                _log("WINDOW: minimized (win32)")
                return True
            # else fall through to CDP attempt below
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

    def _move_offscreen(self) -> bool:
        """
        Park the window far off-screen with window state 'normal' (NOT minimized).
        On Linux/mac a *minimized* window gets restored — flashed briefly on
        screen — every time we open a background tab (poster/resolve fetches),
        which is the flicker users see. A window that's merely off-screen never
        becomes visible when a tab opens, so there's nothing to flash.
        """
        try:
            page = self._main_page()
            session = self._ctx.new_cdp_session(page)
            win_id = session.send("Browser.getWindowForTarget")["windowId"]
            # Must be 'normal' before explicit bounds are accepted.
            session.send("Browser.setWindowBounds",
                         {"windowId": win_id, "bounds": {"windowState": "normal"}})
            session.send("Browser.setWindowBounds", {
                "windowId": win_id,
                "bounds": {"left": -32000, "top": -32000, "width": 1100, "height": 760},
            })
            try: session.detach()
            except Exception: pass
            return True
        except Exception as e:
            _log(f"WINDOW: offscreen move failed {type(e).__name__}")
            return False

    def _go_headless(self):
        """Hide the browser (same process stays alive)."""
        if os.name == "nt":
            if self._window_state("minimized"):
                self._minimized = True
        else:
            # Linux/mac: park off-screen instead of minimizing, so opening a
            # background tab can't flash the window back into view.
            if self._move_offscreen() or self._window_state("minimized"):
                self._minimized = True

    def _go_visible(self):
        """Restore + focus the browser window so the user can re-solve."""
        # On Windows the window is SW_HIDE-hidden; un-hide it via Win32 first.
        if os.name == "nt":
            self._win_show_browser()
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
            # Opening a tab un-minimizes the window on Windows. Re-hide it
            # immediately (before navigation) to keep the flash as short as
            # possible.
            if self._minimized:
                self._win_hide_browser()
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

    def _get_html_via_tab(self, url: str) -> "tuple[int, str]":
        """
        Fetch a page's HTML in a throwaway background tab, WITHOUT navigating the
        main page. Used for cheap side lookups (e.g. a catalog title's poster)
        where routing through the main page would surface/flash the window and
        disturb the current view. Keeps the window minimized throughout.
        Returns (status, html) or (0, "").
        """
        if not url:
            return 0, ""
        tab = None
        try:
            tab = self._ctx.new_page()
            # Opening a tab tries to raise the window — re-hide it immediately.
            if self._minimized:
                self._win_hide_browser()
            self._reassert_minimized()
            resp = tab.goto(url, wait_until="commit", timeout=15000)
            try:
                tab.wait_for_load_state("domcontentloaded", timeout=6000)
            except Exception:
                pass
            self._reassert_minimized()
            status = resp.status if resp else 0
            html = tab.content()
            return status, html
        except Exception as e:
            _log(f"TABGET: failed {type(e).__name__} {url[:55]}")
            return 0, ""
        finally:
            if tab is not None:
                try: tab.close()
                except Exception: pass
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
        """If the window is supposed to be minimized/hidden, push it back. New
        tabs and navigations tend to raise/focus the window; this snaps it back
        so resolve tabs don't steal focus during background downloads."""
        if self._minimized:
            try:
                if os.name == "nt":
                    # Direct SW_HIDE is faster than the CDP round-trip and keeps
                    # the window from lingering visibly.
                    self._win_hide_browser()
                else:
                    # Re-park off-screen (not minimize) so a freshly-opened tab
                    # can't flash the window on screen. See _move_offscreen.
                    self._move_offscreen()
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
        # Opening a tab un-hides the window on Windows; re-hide immediately.
        if self._minimized and os.name == "nt":
            self._win_hide_browser()
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
                    # The submit can raise the window right as the download
                    # starts; hide it again immediately (before we block on
                    # save_as) so it doesn't flash during the download.
                    if self._minimized and os.name == "nt":
                        self._win_hide_browser()
                download = dl.value
                direct = download.url
                _log(f"DLRES: got download url {direct[:80]}")
                self._reassert_minimized()
                if self._minimized and os.name == "nt":
                    self._win_hide_browser()

                if self._pending_dest:
                    # Save directly to the destination via the browser download.
                    dest_path = self._pending_dest
                    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                    _log(f"DLRES: saving via browser to {dest_path}")

                    # Learn the total size (best-effort) so the bar can be a real
                    # percentage. The API request shares the browser's cookies; if
                    # the CDN refuses a HEAD we just fall back to byte-only display.
                    total_bytes = 0
                    try:
                        head = self._ctx.request.head(direct, timeout=8000)
                        total_bytes = int(head.headers.get("content-length", 0) or 0)
                    except Exception:
                        total_bytes = 0

                    # save_as has no per-byte callback, so poll the temp file
                    # Playwright is writing and report genuine progress from it.
                    poll_start = time.time()
                    stop_poll = threading.Event()

                    def _pump():
                        while not stop_poll.wait(0.7):
                            sz = _playwright_partial_size(poll_start)
                            if not sz or not self._pending_progress:
                                continue
                            if total_bytes:
                                frac = min(0.98, sz / total_bytes)
                            else:
                                # Unknown total → asymptotic ramp so the bar still
                                # advances without ever claiming to be finished.
                                frac = min(0.9, 0.1 + sz / (sz + 60_000_000))
                            try:
                                self._pending_progress(frac, sz, total_bytes)
                            except Exception:
                                pass

                    poller = threading.Thread(target=_pump, daemon=True)
                    try:
                        if self._pending_progress:
                            self._pending_progress(0.05, 0, total_bytes)
                        poller.start()
                        download.save_as(dest_path)
                        stop_poll.set()
                        size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                        if self._pending_progress:
                            self._pending_progress(1.0, size, total_bytes or size)
                        _log(f"DLRES: browser download complete ({size} bytes)")
                        # Clean up Playwright's temp copy now that it's saved.
                        try: download.delete()
                        except Exception: pass
                        return ("SAVED", {"path": dest_path, "size": size})
                    except Exception as e:
                        stop_poll.set()
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
    def get_html_via_tab(self, url):      return self._call(self._get_html_via_tab, url)
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

    def _trigger_download(self, page, start_url):
        """
        Navigate a fresh tab from a pahe.win/kwik link to the point where a
        browser download has STARTED, and return the Playwright Download object
        (which is now transferring in Chromium's background). Returns None on
        failure. This mirrors the navigation in _resolve_download but stops at
        the download trigger — used by the parallel batch path so several
        downloads can transfer at once. The single-episode path is untouched.
        """
        opened = False
        for _ in range(2):
            try:
                page.goto(start_url, wait_until="commit", timeout=20000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                opened = True
                break
            except Exception:
                page.wait_for_timeout(1500)
        if not opened:
            return None
        self._reassert_minimized()
        page.wait_for_timeout(800)

        if "pahe.win" in (page.url or ""):
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
                    try:
                        page.goto(km.group(0), wait_until="commit", timeout=20000)
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    self._reassert_minimized()
                    break
                clicked = False
                for sel in ("a[href*='kwik.']", "a:has-text('Continue')",
                            "button:has-text('Continue')",
                            "form[action*='kwik'] button", "form button[type=submit]"):
                    try:
                        for el in page.query_selector_all(sel):
                            if not el.is_visible():
                                continue
                            label = (el.inner_text() or "").strip().lower()
                            if any(bad in label for bad in
                                   ("install", "robot", "adblock", "update", "download app")):
                                continue
                            el.click(); clicked = True; break
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
                return None

        self._reassert_minimized()
        self._wait_clear(page)
        page.wait_for_timeout(800)
        try:
            page.wait_for_selector("form", timeout=15000)
        except Exception:
            pass
        try:
            with page.expect_download(timeout=25000) as dl:
                page.evaluate("(document.querySelector('form')||{submit(){}}).submit()")
                if self._minimized and os.name == "nt":
                    self._win_hide_browser()
            return dl.value
        except Exception:
            return None

    def _resolve_and_save_parallel(self, jobs):
        """
        Download several episodes at once. Each job is {url, dest, ep}. We first
        TRIGGER every browser download (they then transfer concurrently inside
        Chromium), then save each to disk. Returns a list of
        {ep, path, size, ok}. Runs on the engine thread.
        """
        triggered, results = [], []
        for job in jobs:
            ep = job.get("ep"); dest = job["dest"]; url = job["url"]
            tab = self._ctx.new_page()
            if self._minimized and os.name == "nt":
                self._win_hide_browser()
            self._reassert_minimized()
            dl = None
            try:
                dl = self._trigger_download(tab, url)
            except Exception as e:
                _log(f"PAR: trigger ep {ep} failed {type(e).__name__}")
            if dl is None:
                try: tab.close()
                except Exception: pass
                self._reassert_minimized()
                results.append({"ep": ep, "ok": False})
            else:
                triggered.append((ep, dest, dl, tab))

        for ep, dest, dl, tab in triggered:
            try:
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                _log(f"PAR: saving ep {ep} to {dest}")
                dl.save_as(dest)
                size = os.path.getsize(dest) if os.path.exists(dest) else 0
                results.append({"ep": ep, "path": dest, "size": size, "ok": size > 0})
                try: dl.delete()
                except Exception: pass
            except Exception as e:
                _log(f"PAR: save ep {ep} failed {type(e).__name__}: {str(e)[:80]}")
                results.append({"ep": ep, "ok": False})
            finally:
                try: tab.close()
                except Exception: pass
                self._reassert_minimized()
        return results

    def resolve_and_save_parallel(self, jobs):
        return self._call(self._resolve_and_save_parallel, jobs)

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


def shutdown_engine():
    """Shut down the persistent engine if one was started (called on window
    close). Never creates one — unlike get_engine — so closing an app that never
    booted the engine is a no-op."""
    if _engine is not None:
        try:
            _engine.shutdown()
        except Exception:
            pass


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
