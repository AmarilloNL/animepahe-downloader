#!/usr/bin/env python3
"""
Scraping / parsing layer for the AnimePahe downloader.

Turns AnimePahe's JSON API and HTML pages into plain dicts, handles file naming
and skip-existing detection, and drives the engine to resolve + download links.
The parser and naming functions are pure and covered by tests/ ; the resolve
wrappers just delegate to the browser engine.
"""
import os
import re
import time
import json
from bs4 import BeautifulSoup

from pahe_engine import BASE_URL, API_URL, get_engine, pw_get

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


def filter_catalog(catalog: list[dict], query: str, exclude_ids=(),
                   limit: int = 300) -> list[dict]:
    """
    Substring-match the locally-cached A-Z catalog against a search query.

    AnimePahe's search API only ever returns its top 8 hits, so a query like
    "demon" hides most of the library. Since the whole catalog is already loaded
    at startup, we widen the results by scanning it here. Case-insensitive; skips
    any ids the API search already returned (exclude_ids). The matches are
    catalog-shaped dicts (no poster/year — those aren't in the index), so they
    render as titled placeholder tiles. Capped at `limit` so a 1–2 character
    query can't dump thousands of cards into the DOM.
    """
    q = (query or "").strip().lower()
    if not q or not catalog:
        return []
    exclude = set(exclude_ids)
    matches = []
    for item in catalog:
        if item.get("id") in exclude:
            continue
        if q in (item.get("title") or "").lower():
            matches.append(item)
            if len(matches) >= limit:
                break
    return matches


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


def sanitize_title(title: str) -> str:
    """Strip characters that are illegal in filenames on Windows/macOS/Linux."""
    return re.sub(r'[\\/:*?"<>|]', "", title or "").strip() or "Unknown"


def detect_season(title: str) -> int:
    """
    Best-effort guess of a season number from an anime title, so multi-season
    shows get the right Jellyfin/Plex SxxExx tag without manual entry.
    AnimePahe gives no season field, so we parse common patterns. Returns 1
    when nothing season-like is found (the safe default).
    """
    if not title:
        return 1
    t = title.lower()
    # "Season 2", "2nd Season", "Part 2", "Cour 2"
    m = re.search(r'\b(?:season|cour|part)\s+(\d{1,2})\b', t)
    if m:
        return int(m.group(1))
    m = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)\s+season\b', t)
    if m:
        return int(m.group(1))
    # Trailing Roman numeral (e.g. "Overlord IV", "Mushoku Tensei II")
    roman = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8}
    m = re.search(r'\b([ivx]{2,5})\s*$', t)
    if m and m.group(1) in roman:
        return roman[m.group(1)]
    # Trailing bare single digit (e.g. "Kaguya-sama 2") — only a single digit,
    # so we never mistake a year or a numbered title for a season.
    m = re.search(r'\s(\d)\s*$', title)
    if m:
        return int(m.group(1))
    return 1


def episode_paths(dest: str, title: str, season: int, ep_num, quality,
                  jellyfin: bool = True) -> tuple[str, str]:
    """
    Work out where an episode should be saved.
    Returns (series_dir, dest_path). For Jellyfin naming the file goes in a
    per-series subfolder; otherwise it goes straight in `dest`.
    """
    safe = sanitize_title(title)
    ep_int = int(re.sub(r"\D", "", str(ep_num)) or 0)
    if jellyfin:
        series_dir = os.path.join(dest, safe)
        fname = f"{safe} - S{season:02d}E{ep_int:02d} [{quality}p].mp4"
        return series_dir, os.path.join(series_dir, fname)
    fname = f"{safe} - Ep{str(ep_num).zfill(3)} [{quality}p].mp4"
    return dest, os.path.join(dest, fname)


def existing_episode(dest: str, title: str, season: int, ep_num,
                     jellyfin: bool = True, min_bytes: int = 1_000_000) -> str | None:
    """
    Return the path of an already-downloaded file for this episode (any quality),
    or None. Used to skip episodes on a re-run/resume. Matches on the SxxExx (or
    EpNNN) tag so a 720p file counts as 'already have it' even if 1080p is picked.
    """
    safe = sanitize_title(title)
    ep_int = int(re.sub(r"\D", "", str(ep_num)) or 0)
    if jellyfin:
        folder = os.path.join(dest, safe)
        prefix = f"{safe} - S{season:02d}E{ep_int:02d} ["
    else:
        folder = dest
        prefix = f"{safe} - Ep{str(ep_num).zfill(3)} ["
    # Scan the directory rather than glob: the '[1080p]' quality tag contains
    # brackets, which glob would treat as a character class and mis-match.
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        if name.startswith(prefix) and name.endswith(".mp4"):
            p = os.path.join(folder, name)
            try:
                if os.path.getsize(p) >= min_bytes:
                    return p
            except OSError:
                pass
    return None


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
