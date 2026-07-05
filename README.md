# AnimePahe Downloader

A desktop app to search, browse, and batch-download anime episodes from AnimePahe. It has a clean web-style interface (a synthwave card grid with cover art), drives a stealth browser to get past the bot protection, then streams clean MP4 files straight to disk — with optional Jellyfin/Plex-friendly naming.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-GPLv3-green) ![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20macOS-lightgrey)

> [!IMPORTANT]
> **For educational and personal use only.** This tool is intended to help you access content you have a legal right to view. You are responsible for complying with AnimePahe's Terms of Service and the copyright laws in your country. The author does not host, distribute, or own any of the content this tool accesses, and takes no responsibility for how it is used. If you enjoy an anime, please support the creators through official channels.

## Features

- **Browse the full catalog** — the whole AnimePahe library as a fast, scrollable title list
- **Latest releases** — a card grid of what's newly aired, with episode thumbnails
- **Search** — find anime by name, shown as a card grid with cover art
- **Batch download** — select individual episodes, a range, or a whole season at once
- **Quality and audio selection** — choose 1080p/720p/480p/360p and sub/dub when available
- **Skip what you already have** — re-point the app at a series and it only fetches the missing episodes (matches any quality, so a 720p file already on disk won't be re-grabbed in 1080p)
- **Remembers your setup** — quality, audio, naming, folder, and parallelism are restored on the next launch
- **Live progress** — a real per-file percentage and MB counter, not just a batch position
- **Optional parallel downloads** — pull 2–3 episodes at once for faster batches (off by default; higher values raise the risk of a Cloudflare/Kwik block)
- **Self-test** — one button walks the whole pipeline (API → episodes → links → resolve) and tells you exactly which step broke when the site changes
- **Background operation** — the browser minimizes and downloads run quietly while you do other things
- **Jellyfin/Plex naming** — optional `Series/Series - S01E01` folder structure that media servers recognize automatically, with the season number auto-guessed from the title
- **Resilient** — backs off and retries on rate limits, throttling, and transient failures, and resumes stalled downloads

## How it works

AnimePahe and its file host (Kwik) sit behind Cloudflare, which blocks ordinary scripts and automation tools. This downloader uses [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (a stealth-patched fork of Playwright) to drive a real Chromium browser that clears the Cloudflare check. It keeps one persistent browser session alive with a saved profile, so you typically only solve the "Verify you are human" check once per run.

The interface itself is a local HTML/CSS/JS front-end rendered in a native window via [pywebview](https://pywebview.flowrl.com/) — the Python backend exposes search/browse/download functions to the page through pywebview's JS bridge.

The download flow for each episode is:

1. Scrape the play page for the real download links (`pahe.win` redirects, with quality/audio labels)
2. Follow `pahe.win` → `kwik.cx` → the direct MP4 URL on the CDN
3. Stream that MP4 straight to disk with the correct headers

### Project layout

The code is split into small modules so the parts that break when the site
changes are easy to find:

| File | Responsibility |
|------|----------------|
| `animepahe_downloader.py` | App entry point — the pywebview window and the JS↔Python API bridge (`class Api`) |
| `pahe_engine.py` | The persistent stealth-Chromium session, network/stream layer, and settings/log paths |
| `pahe_scrape.py` | Search/browse/episode parsing, file naming, and link resolving |
| `pahe_ui.py` | The HTML/CSS/JS frontend (`INDEX_HTML`) |
| `tests/` | Offline unit tests for the parsers and naming logic |

Run it exactly as before — `python animepahe_downloader.py`. The extra modules
are imported automatically, and PyInstaller bundles them into the same single
`.exe`.

## Requirements

- Python 3.10 or newer
- A Chromium browser (installed automatically by Patchright)
- The Python packages and system libraries listed below

## Installation

### Arch Linux / CachyOS

```bash
# System packages: BeautifulSoup + the GTK WebKit engine pywebview needs
sudo pacman -S python-beautifulsoup4 webkit2gtk-4.1

# Python packages
python -m pip install patchright pywebview --break-system-packages
python -m patchright install chromium
```

### Debian / Ubuntu

```bash
sudo apt install python3-bs4 python3-gi gir1.2-webkit2-4.1
python3 -m pip install patchright pywebview
python3 -m patchright install chromium
```

### Windows — prebuilt .exe (easiest)

Grab `AnimePaheDownloader.exe` from the [Releases page](../../releases) and run it. On first launch it downloads its Chromium browser automatically (one-time), then opens. No Python install needed.

### Windows / macOS — from source

```bash
pip install -r requirements.txt
python -m patchright install chromium
```

(pywebview uses the built-in system webview on Windows/macOS, so no extra GTK package is needed there.)

## Usage

```bash
python animepahe_downloader.py
```

1. When the browser window appears, solve the **"Verify you are human"** check if shown. The window then minimizes itself.
2. The **full catalog** loads automatically. Use **Browse all**, **Latest**, or **Search** to find a title.
3. **Click** a title to load its episodes.
4. **Tick** the episodes you want — or use the range box, or the **All** button.
5. Set your **quality**, **audio**, **season number**, and a download **folder**.
6. Click **Download**. Episodes download in the background to your chosen folder.

### Jellyfin / Plex naming

With **"Jellyfin naming"** enabled (the default), files are saved as:

```
<your folder>/
└── Berserk of Gluttony/
    ├── Berserk of Gluttony - S01E01 [1080p].mp4
    ├── Berserk of Gluttony - S01E02 [1080p].mp4
    └── ...
```

Point your media server's library at the parent folder and it will recognize the series and episodes automatically.

> [!NOTE]
> AnimePahe provides an absolute episode number but no season number, so the **Season** field defaults to `1`. For multi-season shows, set the season manually before downloading. A few long-running shows use absolute numbering that won't line up with TVDB's per-season numbering; for those you may need to rename or add a metadata file for your media server.

## Troubleshooting

**Blank window on Linux.** WebKitGTK can render a blank window due to a GPU-compositing bug. The app already sets `WEBKIT_DISABLE_DMABUF_RENDERER` and `WEBKIT_DISABLE_COMPOSITING_MODE` to work around it. If it still happens, confirm `webkit2gtk-4.1` is installed.

**The browser keeps showing the Cloudflare check.** Make sure Patchright is installed into the *same* Python that runs the app, and that you ran `python -m patchright install chromium`. If a plain Playwright is used by mistake, Cloudflare will loop forever.

**A download stalls or an episode is skipped.** Transient network and CDN hiccups are normal; the tool backs off and retries automatically. A single failure that recovers is nothing to worry about. Repeated failures across many episodes suggest a connection problem or a temporary block — try splitting very long batches into smaller chunks.

**"Sorry, you have been blocked" from Kwik.** This is a server-side IP block, not a bug. Wait a while or try a different connection/VPN.

The app logs everything to `~/.config/animepahe-dl/engine.log` and to the terminal — check there first when something goes wrong.

## Development

The parsing and naming logic has offline unit tests (no browser or network
needed — the engine and fetch layer are monkeypatched with canned responses):

```bash
pip install pytest
python -m pytest tests/ -q
```

These are the fastest way to confirm a scraping change still produces the right
data shapes. When AnimePahe/Kwik change their markup, update the parser in
`pahe_scrape.py` and adjust the matching fixture in `tests/test_parsing.py`.

## Contributing

Issues and pull requests are welcome. Because AnimePahe and Kwik change their page structure and bot protection periodically, the scraping/resolve logic occasionally needs updating — the detailed logs make it straightforward to see which step broke, and the **Self-test** button pinpoints the failing stage.

## License

This project is licensed under the **GNU General Public License v3.0** — see the [LICENSE](LICENSE) file for details. In short: you're free to use, modify, and redistribute it, but any distributed forks must also remain open-source under the same license.
