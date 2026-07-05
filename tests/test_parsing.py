"""
Unit tests for the pure parsing / naming layer of the AnimePahe downloader.

These cover the parts most likely to break when AnimePahe or Kwik change their
markup — the HTML/JSON parsers and the file-naming logic — WITHOUT touching the
network or a browser. The engine and pw_get are monkeypatched with canned
responses, so `pytest` runs offline and fast (no webview/GUI import needed).

Run:  python -m pytest tests/ -q
"""
import os
import sys

import pytest

# Make the app modules importable when pytest runs from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pahe_engine as engine  # noqa: E402  (settings live here)
import pahe_scrape as app     # noqa: E402  (parsing/naming live here)


# ── season detection ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("title,expected", [
    ("Naruto", 1),
    ("One Piece", 1),
    ("Demon Slayer: Kimetsu no Yaiba", 1),        # no season marker
    ("Kaguya-sama: Love is War Season 2", 2),
    ("Attack on Titan 3rd Season", 3),
    ("Overlord IV", 4),
    ("Mushoku Tensei II", 2),
    ("Some Show Part 2", 2),
    ("Bleach 2", 2),
    ("", 1),
])
def test_detect_season(title, expected):
    assert app.detect_season(title) == expected


# ── filename sanitising / paths ──────────────────────────────────────────────
def test_sanitize_title_strips_illegal_chars():
    assert app.sanitize_title('A/B:C*?"<>|D') == "ABCD"


def test_sanitize_title_empty_falls_back():
    assert app.sanitize_title("   ") == "Unknown"
    assert app.sanitize_title(None) == "Unknown"


def test_episode_paths_jellyfin():
    series, path = app.episode_paths("/dl", "My Show", 2, "5", "1080", jellyfin=True)
    assert series == os.path.join("/dl", "My Show")
    assert path == os.path.join("/dl", "My Show", "My Show - S02E05 [1080p].mp4")


def test_episode_paths_flat():
    series, path = app.episode_paths("/dl", "My Show", 1, "5", "720", jellyfin=False)
    assert series == "/dl"
    assert path == os.path.join("/dl", "My Show - Ep005 [720p].mp4")


def test_episode_paths_strips_nonnumeric_episode():
    _series, path = app.episode_paths("/dl", "Show", 1, "12v2", "1080", jellyfin=True)
    assert "S01E12" in path


# ── skip-existing detection ──────────────────────────────────────────────────
def test_existing_episode_matches_any_quality(tmp_path):
    show_dir = tmp_path / "My Show"
    show_dir.mkdir()
    f = show_dir / "My Show - S01E03 [720p].mp4"
    f.write_bytes(b"0" * 2_000_000)  # over the 1MB threshold
    # Asked for 1080p but a 720p file already exists → should still count.
    found = app.existing_episode(str(tmp_path), "My Show", 1, "3", jellyfin=True)
    assert found == str(f)


def test_existing_episode_ignores_tiny_files(tmp_path):
    show_dir = tmp_path / "My Show"
    show_dir.mkdir()
    (show_dir / "My Show - S01E03 [1080p].mp4").write_bytes(b"0" * 100)  # partial
    assert app.existing_episode(str(tmp_path), "My Show", 1, "3", jellyfin=True) is None


def test_existing_episode_none_when_absent(tmp_path):
    assert app.existing_episode(str(tmp_path), "My Show", 1, "99", jellyfin=True) is None


# ── download-option picking ──────────────────────────────────────────────────
def _opts():
    return [
        {"url": "u1080e", "quality": "1080", "audio": "eng", "size": "", "label": ""},
        {"url": "u1080j", "quality": "1080", "audio": "jpn", "size": "", "label": ""},
        {"url": "u720j",  "quality": "720",  "audio": "jpn", "size": "", "label": ""},
    ]


def test_pick_exact_quality_and_audio():
    assert app.pick_download_option(_opts(), "1080", "jpn")["url"] == "u1080j"


def test_pick_right_quality_any_audio():
    opts = [o for o in _opts() if o["audio"] == "eng"]  # only eng available
    assert app.pick_download_option(opts, "1080", "jpn")["quality"] == "1080"


def test_pick_falls_back_to_highest_of_preferred_audio():
    opts = [
        {"url": "a", "quality": "480", "audio": "jpn"},
        {"url": "b", "quality": "720", "audio": "jpn"},
    ]
    # 1080 not present → pick highest jpn (720)
    assert app.pick_download_option(opts, "1080", "jpn")["url"] == "b"


def test_pick_empty_returns_none():
    assert app.pick_download_option([], "1080", "jpn") is None


# ── #pickDownload HTML scraping ──────────────────────────────────────────────
PLAY_HTML = """
<html><body>
  <h1>Berserk of Gluttony - AnimePahe</h1>
  <div id="pickDownload">
    <a href="https://pahe.win/aaa">SubsPlease · 360p (80MB)</a>
    <a href="https://pahe.win/bbb">SubsPlease · 720p (150MB)</a>
    <a href="https://pahe.win/ccc">SubsPlease · 1080p eng (300MB)</a>
    <a href="https://pahe.win/ddd">SubsPlease · 1080p (280MB)</a>
  </div>
</body></html>
"""


class _FakeEngine:
    def __init__(self, status, body):
        self._r = (status, body)

    def get(self, url):
        return self._r


def test_get_download_options_parses_dropdown(monkeypatch):
    monkeypatch.setattr(app, "get_engine", lambda: _FakeEngine(200, PLAY_HTML))
    options, title = app.get_download_options("anime-id", "ep-session")
    assert title == "Berserk of Gluttony"
    assert len(options) == 4
    q1080 = [o for o in options if o["quality"] == "1080"]
    assert {o["audio"] for o in q1080} == {"eng", "jpn"}
    eng_opt = next(o for o in q1080 if o["audio"] == "eng")
    assert eng_opt["size"] == "300MB"
    # end-to-end: pick 1080 jpn from the scraped options
    chosen = app.pick_download_option(options, "1080", "jpn")
    assert chosen["url"] == "https://pahe.win/ddd"


def test_get_download_options_empty_on_non_200(monkeypatch):
    monkeypatch.setattr(app, "get_engine", lambda: _FakeEngine(403, "blocked"))
    options, title = app.get_download_options("x", "y")
    assert options == [] and title == ""


# ── JSON API parsers (search / latest / episodes) ────────────────────────────
def test_search_anime_parses_json(monkeypatch):
    import json
    payload = json.dumps({
        "data": [
            {"session": "s1", "title": "Show One", "year": 2021, "status": "Finished",
             "episodes": 12, "type": "TV", "poster": "p1"},
            {"session": "s2", "title": "Show Two", "year": 2022,
             "episodes": 24, "poster": "p2"},
        ],
        "last_page": 1,
    })
    monkeypatch.setattr(app, "pw_get", lambda url, params=None: payload)
    results = app.search_anime("show")
    assert [r["id"] for r in results] == ["s1", "s2"]
    assert results[0]["title"] == "Show One"
    assert results[0]["year"] == "2021"


def test_search_anime_dedupes(monkeypatch):
    import json
    payload = json.dumps({"data": [
        {"session": "dup", "title": "A"},
        {"session": "dup", "title": "A again"},
    ], "last_page": 1})
    monkeypatch.setattr(app, "pw_get", lambda url, params=None: payload)
    assert len(app.search_anime("x")) == 1


def test_fetch_latest_dedupes_and_shapes(monkeypatch):
    import json
    payload = json.dumps({"data": [
        {"anime_session": "a", "anime_title": "Airing One", "episode": 3, "snapshot": "s"},
        {"anime_session": "a", "anime_title": "Airing One", "episode": 4},  # dup anime
        {"anime_session": "b", "anime_title": "Airing Two", "episode": 1, "fansub": "Group"},
    ], "last_page": 1})
    monkeypatch.setattr(app, "pw_get", lambda url, params=None: payload)
    results, has_more = app.fetch_latest(page=1)
    assert [r["id"] for r in results] == ["a", "b"]
    assert has_more is False
    assert "Group" in results[1]["status"]


def test_fetch_episodes_parses_pages(monkeypatch):
    import json
    payload = json.dumps({"data": [
        {"episode": 1, "title": "Ep one", "session": "e1", "snapshot": "s1"},
        {"episode": 2, "title": "Ep two", "session": "e2", "snapshot": "s2"},
    ], "last_page": 1})
    monkeypatch.setattr(app, "pw_get", lambda url, params=None: payload)
    eps = app.fetch_episodes("anime-id")
    assert [e["episode"] for e in eps] == [1, 2]
    assert eps[0]["session"] == "e1"
    assert eps[0]["anime_id"] == "anime-id"


# ── catalog-widened search ───────────────────────────────────────────────────
def _catalog():
    return [
        {"id": "1", "title": "Demon Slayer", "poster": ""},
        {"id": "2", "title": "Demon King Daimao", "poster": ""},
        {"id": "3", "title": "The Rising of the Shield Hero", "poster": ""},
        {"id": "4", "title": "demon lord, retry!", "poster": ""},  # lower-case
    ]


def test_filter_catalog_case_insensitive_substring():
    hits = app.filter_catalog(_catalog(), "demon")
    assert {h["id"] for h in hits} == {"1", "2", "4"}


def test_filter_catalog_excludes_ids_already_shown():
    # id "1" already came back from the API search → don't duplicate it.
    hits = app.filter_catalog(_catalog(), "demon", exclude_ids={"1"})
    assert {h["id"] for h in hits} == {"2", "4"}


def test_filter_catalog_empty_query_or_catalog():
    assert app.filter_catalog(_catalog(), "") == []
    assert app.filter_catalog([], "demon") == []


# ── settings round-trip ──────────────────────────────────────────────────────
def test_settings_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "SETTINGS_PATH", tmp_path / "settings.json")
    engine.save_settings({
        "quality": "720", "audio": "eng", "jellyfin": False,
        "skip_existing": False, "concurrency": 2, "folder": "/some/dir",
        "ignored_key": "nope",
    })
    got = engine.load_settings()
    assert got == {
        "quality": "720", "audio": "eng", "jellyfin": False,
        "skip_existing": False, "concurrency": 2, "folder": "/some/dir",
    }
    assert "ignored_key" not in got  # non-whitelisted keys are dropped


def test_load_settings_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "SETTINGS_PATH", tmp_path / "nope.json")
    assert engine.load_settings() == {}
