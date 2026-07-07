#!/usr/bin/env python3
"""
AnimePahe Batch Downloader — application entry point.

Search by name (or browse the catalog / latest) -> pick a title -> select
episodes -> download. This module wires the pywebview window to the Python API
bridge (class Api) and boots the browser engine. The heavy lifting lives in:

  * pahe_engine  — persistent stealth-Chromium session + network/stream layer
  * pahe_scrape  — search/browse/episode parsing, naming, and link resolve
  * pahe_ui      — the HTML/CSS/JS frontend (INDEX_HTML)

Copyright (C) 2026  AmarilloNL.  GNU GPL v3 — see the LICENSE file.
For educational and personal use only. Respect AnimePahe's Terms of Service and
the copyright laws in your country.
"""
import os
import sys
# On Windows, stdout/stderr default to cp1252 and crash on Unicode like -> or check
# marks. Force UTF-8 so logging and status messages can never raise.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# PyInstaller-frozen Windows builds: this MUST run before anything that can spawn
# a subprocess, or child processes relaunch the whole GUI in a loop.
import multiprocessing
multiprocessing.freeze_support()

# WebKitGTK on Linux often renders a blank window due to a DMABUF/GPU-compositing
# bug. Disabling those renderers forces software paint. Must be set BEFORE
# importing webview.
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

import webview
import threading
import re, time, json

from pahe_engine import (
    _log, _cleanup_playwright_artifacts, get_engine, shutdown_engine, pw_get,
    BASE_URL, API_URL, APP_VERSION, GITHUB_REPO, RateLimited,
    load_settings, save_settings, check_for_update,
)
from pahe_scrape import (
    search_anime, filter_catalog, fetch_latest, fetch_catalog, fetch_episodes,
    get_download_options, pick_download_option, resolve_download,
    resolve_and_save, detect_season, existing_episode, episode_paths,
)
from pahe_ui import INDEX_HTML

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
        self._catalog = []            # full A-Z catalog, cached to widen search
        self._current_title = ""
        self._engine_dot = "starting"
        # Per-episode batch state the JS polls to render the queue panel.
        self._batch = []              # [{ep, status}]
        self._batch_anime = ""        # anime_id the batch belongs to
        self._batch_bytes = 0         # cumulative bytes saved this batch
        self._batch_est = 0           # projected total bytes for the batch
        self._last_dl = None          # last batch options, for retrying failures
        self._update_info = None      # {latest, url, newer} once checked

    # ── wiring ────────────────────────────────────────────────────────────────
    def _set_status(self, msg, kind="info"):
        self._status = {"msg": msg, "kind": kind}
        _log(f"UI[{kind}]: {msg}")

    def _set_progress(self, value, label=""):
        self._progress = {"value": round(value, 1), "label": label}

    def _set_ep_status(self, ep, status):
        for item in self._batch:
            if item["ep"] == ep:
                item["status"] = status
                break

    def poll_status(self):
        return self._status

    def poll_progress(self):
        return self._progress

    def poll_batch(self):
        """Per-episode queue state + running size totals for the batch panel."""
        return {"items": self._batch, "anime": self._batch_anime,
                "bytes": self._batch_bytes, "estimate": self._batch_est,
                "downloading": self._downloading}

    def app_info(self):
        """Version + update banner info for the UI."""
        return {"version": APP_VERSION, "update": self._update_info}

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
                # AnimePahe's search API caps at 8 hits, so widen it by also
                # matching the full catalog cached at startup — appending anything
                # the API didn't already return.
                have = {r.get("id") for r in results}
                extra = filter_catalog(self._catalog, query, have)
                combined = results + extra
                self._results = combined
                if combined:
                    msg = f'{len(combined)} result(s) for "{query}"'
                    if extra:
                        msg += f" (+{len(extra)} from catalog)"
                else:
                    msg = f'No results for "{query}".'
                self._push_results(combined, msg)
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
                self._catalog = results   # cache so search can widen past the API's 8
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

    def get_poster(self, anime_id):
        """
        Fetch cover art for a catalog result. The A-Z index the catalog is built
        from has no images, so those tiles start blank; the JS lazily calls this
        for the ones on screen. We read the anime page's og:image and return it
        as a data: URL (cached on disk by get_image). Returns '' on failure.
        """
        if not self._engine_ready or not anime_id:
            return ""
        try:
            # Fetch in a background tab so the main page/window is never disturbed
            # (routing through the main page flashes the browser on every poster).
            status, body = get_engine().get_html_via_tab(f"{BASE_URL}/anime/{anime_id}")
            if status != 200:
                return ""
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                          body) or \
                re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                          body)
            if not m:
                return ""
            return self.get_image(m.group(1))
        except Exception as e:
            _log(f"UI: get_poster failed {type(e).__name__} {anime_id[:12]}")
            return ""

    def get_episodes(self, anime_id, title):
        if not self._guard():
            return False
        self._current_title = title
        self._set_status(f"Loading episodes for {title}…", "warn")

        def run():
            try:
                eps = fetch_episodes(anime_id)
                # Guess the season from the title so multi-season shows get the
                # right SxxExx tag without the user editing the field by hand.
                season = detect_season(title)
                payload = json.dumps({"episodes": eps, "title": title, "season": season})
                safe = payload.replace("\\", "\\\\").replace("`", "\\`")
                if self._window:
                    self._window.evaluate_js(f"window.renderEpisodes(JSON.parse(`{safe}`))")
                self._set_status(f"Loaded {len(eps)} episodes for {title}.", "success")
            except Exception as e:
                self._set_status(f"Episode load error: {e}", "error")
        threading.Thread(target=run, daemon=True).start()
        return True

    def downloaded_episodes(self, payload):
        """
        Given the current folder/season/naming and a list of {ep, title}, return
        the episode numbers already present on disk. The UI uses this to grey out
        episodes you already have and to power the "Select missing" button.
        """
        dest = (payload.get("dest") or "").strip()
        if not dest or not os.path.isdir(dest):
            return []
        season = int(payload.get("season", 1) or 1)
        jellyfin = bool(payload.get("jellyfin", True))
        title = payload.get("title") or self._current_title
        have = []
        for ep in payload.get("episodes", []):
            try:
                if existing_episode(dest, title, season, ep, jellyfin):
                    have.append(ep)
            except Exception:
                pass
        return have

    # ── update check ─────────────────────────────────────────────────────────────
    def check_update(self):
        """Look up the latest GitHub release in the background; the UI reads the
        result via app_info() and shows a banner if a newer version exists."""
        def run():
            info = check_for_update()
            if info:
                self._update_info = info
                if info.get("newer"):
                    self._set_status(
                        f"Update available: v{info['latest']} "
                        f"(you have v{APP_VERSION}).", "warn")
        threading.Thread(target=run, daemon=True).start()
        return True

    def open_release(self):
        """Open the latest release page in the user's default browser."""
        url = (self._update_info or {}).get("url") \
            or f"https://github.com/{GITHUB_REPO}/releases/latest"
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            _log(f"UI: open_release failed {type(e).__name__}")
        return True

    # ── notifications ────────────────────────────────────────────────────────────
    def _notify(self, title, message):
        """Best-effort desktop notification (batch complete). Never raises."""
        try:
            if sys.platform.startswith("linux"):
                import shutil, subprocess
                if shutil.which("notify-send"):
                    subprocess.Popen(["notify-send", "-a", "PAHE DL", title, message])
            elif sys.platform == "darwin":
                import subprocess
                safe_m = message.replace('"', "'"); safe_t = title.replace('"', "'")
                subprocess.Popen(["osascript", "-e",
                                  f'display notification "{safe_m}" with title "{safe_t}"'])
            elif os.name == "nt":
                import subprocess
                # Windows toast via PowerShell + WinRT. Best-effort; if the shell
                # or WinRT isn't available it simply no-ops.
                ps = (
                    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                    "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                    "$x=$t.GetElementsByTagName('text');"
                    f"$x.Item(0).AppendChild($t.CreateTextNode('{title}')) > $null;"
                    f"$x.Item(1).AppendChild($t.CreateTextNode('{message}')) > $null;"
                    "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
                    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PAHE DL').Show($n);"
                )
                subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                                  "-Command", ps],
                                 creationflags=0x08000000)  # CREATE_NO_WINDOW
        except Exception as e:
            _log(f"NOTIFY: failed {type(e).__name__}")

    # ── settings persistence ─────────────────────────────────────────────────────
    def load_settings(self):
        """Return the persisted UI settings (empty dict if none)."""
        return load_settings()

    def save_settings(self, data):
        """Persist the UI settings the JS side sends when a download starts."""
        save_settings(data or {})
        return True

    # ── diagnostics ──────────────────────────────────────────────────────────────
    def run_diagnostics(self):
        """
        Walk the scrape→resolve pipeline stage by stage and report which step
        (if any) is broken. AnimePahe/Kwik change their markup periodically, so
        this pinpoints the failing stage instead of a vague 'download failed'.
        Progress is reported through the normal status channel.
        """
        if not self._guard():
            return False
        if self._downloading:
            self._set_status("Finish or stop the current download first.", "warn")
            return False

        def run():
            state = {"sid": None, "eps": None, "opts": None}
            report = []

            def stage(name, fn):
                self._set_status(f"Self-test: {name}…", "warn")
                try:
                    ok, detail = fn()
                except Exception as e:
                    ok, detail = False, f"{type(e).__name__}: {e}"
                report.append((name, ok, detail))
                _log(f"SELFTEST: {name}: {'OK' if ok else 'FAIL'} — {detail}")
                self._set_status(f"Self-test: {name} — {'OK' if ok else 'FAILED'} ({detail})",
                                 "success" if ok else "error")
                time.sleep(1.0)
                return ok

            def s_api():
                txt = pw_get(API_URL, params={"m": "search", "q": "one piece"})
                data = json.loads(txt.lstrip("\ufeff \t\r\n"))
                items = data.get("data", []) if isinstance(data, dict) else data
                if not items:
                    return False, "no results returned"
                state["sid"] = items[0].get("session") or items[0].get("id")
                return True, f"{len(items)} results"

            def s_eps():
                if not state["sid"]:
                    return False, "no anime id from search"
                state["eps"] = fetch_episodes(state["sid"])
                return bool(state["eps"]), f"{len(state['eps'])} episodes"

            def s_opts():
                if not state["eps"]:
                    return False, "no episodes"
                opts, _ = get_download_options(state["sid"], state["eps"][0]["session"])
                state["opts"] = opts
                return bool(opts), f"{len(opts)} download link(s)"

            def s_resolve():
                if not state["opts"]:
                    return False, "no download links"
                chosen = pick_download_option(state["opts"])
                r = resolve_download(chosen["url"])
                return bool(r), "reached the CDN" if r else "could not resolve the link"

            ok = stage("AnimePahe API", s_api)
            ok = stage("Episode list", s_eps) if ok else False
            ok = stage("Download links", s_opts) if ok else False
            if ok:
                stage("Link resolve", s_resolve)

            passed = sum(1 for _, o, _ in report if o)
            kind = "success" if passed == len(report) else "warn"
            self._set_status(
                f"Self-test complete: {passed}/{len(report)} stage(s) passed. "
                f"Full details in engine.log.", kind)

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
                  season, jellyfin(bool), skip_existing(bool), concurrency(int)}
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
        skip_existing = bool(payload.get("skip_existing", True))
        # Cap concurrency: more than a few parallel browser downloads invites a
        # Cloudflare/Kwik IP block, which defeats the point.
        concurrency = max(1, min(3, int(payload.get("concurrency", 1) or 1)))
        batch_title = self._current_title

        # Batch queue state (for the panel) + a snapshot of options so failed
        # episodes can be retried with the same settings.
        self._batch = [{"ep": it["ep"], "status": "queued"} for it in selected]
        self._batch_anime = (selected[0].get("anime_id") or "") if selected else ""
        self._batch_bytes = 0
        self._batch_est = 0
        self._last_dl = {"episodes": list(selected), "dest": dest,
                         "quality": quality, "audio": audio, "season": season,
                         "jellyfin": jellyfin, "skip_existing": skip_existing,
                         "concurrency": concurrency, "title": batch_title}

        self._stop_event.clear()
        self._downloading = True
        self._set_progress(0, "")

        def run():
            import traceback
            total = len(selected)

            def record_done(ep_num, size):
                """Mark an episode done and refresh the batch's size totals."""
                self._set_ep_status(ep_num, "done")
                if size and size > 0:
                    self._batch_bytes += size
                done = sum(1 for b in self._batch if b["status"] == "done")
                if done:
                    self._batch_est = int(self._batch_bytes / done * total)

            def already_have(item, ep_num):
                """True if this episode is already on disk (skip-existing on)."""
                title = batch_title or item.get("title") or ""
                return bool(title) and skip_existing and bool(
                    existing_episode(dest, title, season, ep_num, jellyfin))

            # ── sequential path (one episode at a time — the default) ──────────
            def sequential():
                idx = 0
                rl_strikes = ctx_strikes = res_strikes = 0
                while idx < len(selected):
                    item = selected[idx]
                    if self._stop_event.is_set():
                        self._set_status("Stopped.", "warn")
                        break
                    ep_num = item["ep"]
                    if already_have(item, ep_num):
                        self._set_ep_status(ep_num, "skipped")
                        self._set_status(f"[{idx+1}/{total}] Ep {ep_num}: already downloaded — skipping.", "info")
                        self._set_progress((idx + 1) / total * 100, f"Ep {ep_num} skipped")
                        idx += 1; continue
                    self._set_ep_status(ep_num, "downloading")
                    print(f"\n[DL] Starting Ep {ep_num} | anime_id={item['anime_id']} | session={item['session'][:16]}…")
                    self._set_status(f"[{idx+1}/{total}] Finding download link for Ep {ep_num}…", "warn")
                    try:
                        options, scraped_title = get_download_options(item["anime_id"], item["session"])
                        if not options:
                            self._set_ep_status(ep_num, "failed")
                            self._set_status(f"Ep {ep_num}: No download links — skipping.", "error")
                            idx += 1; continue
                        chosen = pick_download_option(options, quality, audio)

                        # Work out the destination path up-front (we need it to
                        # save the browser download straight to disk).
                        anime_title = batch_title or item.get("title") or scraped_title
                        _series_dir, dest_path = episode_paths(
                            dest, anime_title, season, ep_num, chosen["quality"], jellyfin)

                        # Real per-byte progress comes from the engine (which polls
                        # the file it's writing); we map it onto this episode's
                        # slot of the overall bar and show live MB, speed and ETA.
                        spd = {"t": time.time(), "b": 0, "rate": 0.0}
                        def file_prog(frac, downloaded=None, total_b=None, ep_done=idx):
                            overall = (ep_done + min(frac, 0.99)) / total * 100
                            if downloaded:
                                now = time.time()
                                dt = now - spd["t"]
                                if dt >= 0.4 and downloaded >= spd["b"]:
                                    inst = (downloaded - spd["b"]) / dt
                                    # Smooth the rate a little so it doesn't jump.
                                    spd["rate"] = inst if spd["rate"] == 0 else spd["rate"] * 0.6 + inst * 0.4
                                    spd["t"], spd["b"] = now, downloaded
                                extra = ""
                                if spd["rate"] > 0:
                                    extra = f"  {spd['rate']/1_048_576:.1f} MB/s"
                                    if total_b and total_b > downloaded:
                                        eta = int((total_b - downloaded) / spd["rate"])
                                        extra += f"  ETA {eta//60:d}:{eta%60:02d}"
                                if total_b:
                                    lbl = (f"Ep {ep_num}: {downloaded/1_048_576:.0f}/"
                                           f"{total_b/1_048_576:.0f} MB{extra}  ({ep_done+1}/{total})")
                                else:
                                    lbl = f"Ep {ep_num}: {downloaded/1_048_576:.0f} MB{extra}  ({ep_done+1}/{total})"
                            else:
                                lbl = f"Ep {ep_num} of {total}"
                            self._set_progress(overall, lbl)

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
                            self._set_ep_status(ep_num, "failed")
                            self._set_status(f"Ep {ep_num}: Could not download after retries — skipping.", "error")
                            res_strikes = 0; idx += 1; continue
                        res_strikes = 0
                        try:
                            saved_size = int(result[1].get("size", 0))
                        except Exception:
                            saved_size = 0
                        record_done(ep_num, saved_size)
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
                        self._set_ep_status(ep_num, "failed")
                        self._set_status(f"Ep {ep_num} error: {e}", "error")
                        idx += 1; continue

                    self._set_progress(idx / total * 100, "")
                    if idx < len(selected) and not self._stop_event.is_set():
                        time.sleep(1.5)

            # ── parallel path (opt-in: N browser downloads at once) ────────────
            def parallel(nworkers):
                done = 0
                i = 0
                while i < len(selected) and not self._stop_event.is_set():
                    group = selected[i:i + nworkers]
                    i += nworkers
                    jobs = []
                    for item in group:
                        ep_num = item["ep"]
                        if already_have(item, ep_num):
                            self._set_ep_status(ep_num, "skipped")
                            self._set_status(f"Ep {ep_num}: already downloaded — skipping.", "info")
                            done += 1; continue
                        self._set_ep_status(ep_num, "downloading")
                        self._set_status(f"Finding download link for Ep {ep_num}…", "warn")
                        try:
                            options, scraped = get_download_options(item["anime_id"], item["session"])
                        except RateLimited:
                            self._set_status("Rate limited — pausing 30s…", "warn")
                            for r in range(30, 0, -1):
                                if self._stop_event.is_set(): break
                                time.sleep(1)
                            options, scraped = [], ""
                        if not options:
                            self._set_ep_status(ep_num, "failed")
                            self._set_status(f"Ep {ep_num}: No download links — skipping.", "error")
                            done += 1; continue
                        chosen = pick_download_option(options, quality, audio)
                        atitle = batch_title or item.get("title") or scraped
                        _dir, dpath = episode_paths(dest, atitle, season, ep_num,
                                                    chosen["quality"], jellyfin)
                        jobs.append({"url": chosen["url"], "dest": dpath, "ep": ep_num})

                    if jobs and not self._stop_event.is_set():
                        self._set_status(f"Downloading {len(jobs)} episode(s) in parallel…", "info")
                        try:
                            results = get_engine().resolve_and_save_parallel(jobs)
                        except Exception as e:
                            _log(f"PAR: batch failed {type(e).__name__}: {str(e)[:100]}")
                            results = [{"ep": j["ep"], "ok": False} for j in jobs]
                        for r in results:
                            done += 1
                            if r.get("ok"):
                                record_done(r["ep"], int(r.get("size", 0)))
                                self._set_status(f"Ep {r['ep']} done.", "success")
                            else:
                                self._set_ep_status(r["ep"], "failed")
                                self._set_status(f"Ep {r['ep']}: download failed.", "error")
                    self._set_progress(done / total * 100, f"{done}/{total} done")

            try:
                if concurrency > 1:
                    parallel(concurrency)
                else:
                    sequential()

                if not self._stop_event.is_set():
                    ok = sum(1 for b in self._batch if b["status"] in ("done", "skipped"))
                    failed = sum(1 for b in self._batch if b["status"] == "failed")
                    summary = f"Done! {ok}/{total} episode(s) saved to {dest}"
                    if failed:
                        summary += f" · {failed} failed"
                    self._set_status(summary, "success" if not failed else "warn")
                    self._set_progress(100, "Complete")
                    gb = self._batch_bytes / 1_073_741_824
                    self._notify("Download complete",
                                 f"{ok}/{total} episodes"
                                 + (f", {failed} failed" if failed else "")
                                 + (f" · {gb:.1f} GB" if gb >= 0.05 else ""))
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

    def retry_failed(self):
        """Re-run just the failed episodes from the last batch, with the same
        settings. Used by the Retry button in the queue panel."""
        if self._downloading:
            self._set_status("A download is already running.", "warn")
            return False
        if not self._last_dl:
            return False
        failed_eps = {b["ep"] for b in self._batch if b["status"] == "failed"}
        items = [it for it in self._last_dl["episodes"] if it["ep"] in failed_eps]
        if not items:
            self._set_status("No failed episodes to retry.", "info")
            return False
        payload = dict(self._last_dl)
        payload["episodes"] = items
        # Retry the actual files even if skip-existing is on (they're missing).
        payload["skip_existing"] = False
        # Restore the batch's series title so naming stays correct even if the
        # user has since navigated to a different anime.
        self._current_title = self._last_dl.get("title") or self._current_title
        self._set_status(f"Retrying {len(items)} failed episode(s)…", "warn")
        return self.start_download(payload)


def _on_closed():
    shutdown_engine()


def main():
    api = Api()

    # WebKitGTK (Linux) can fail to paint a large inline html= string, leaving a
    # blank window. Writing the page to a temp file and loading it by URL is the
    # reliable path across backends.
    #
    # IMPORTANT: use a UNIQUE filename per launch. WebKitGTK aggressively caches
    # file:// URLs, so re-using one path made it serve a stale UI after updates
    # (CSS/JS changes appeared not to take effect until the cache happened to
    # clear). A fresh name every run guarantees the current frontend is loaded.
    import tempfile, glob
    tmpdir = tempfile.gettempdir()
    for old in glob.glob(os.path.join(tmpdir, "pahe_dl_ui*.html")):
        try: os.remove(old)
        except OSError: pass
    html_path = os.path.join(tmpdir, f"pahe_dl_ui_{os.getpid()}_{int(time.time())}.html")
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
        # Check GitHub for a newer release (background, best-effort).
        api.check_update()

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
