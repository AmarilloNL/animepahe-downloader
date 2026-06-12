# AnimePahe Downloader

A desktop GUI tool to search, browse, and batch-download anime episodes from AnimePahe. Built with Python and Tkinter, it drives a stealth browser to get past the bot protection, then streams clean MP4 files straight to disk — with optional Jellyfin/Plex-friendly naming.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-GPLv3-green) ![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20macOS-lightgrey)

> [!IMPORTANT]
> **For educational and personal use only.** This tool is intended to help you access content you have a legal right to view. You are responsible for complying with AnimePahe's Terms of Service and the copyright laws in your country. The author does not host, distribute, or own any of the content this tool accesses, and takes no responsibility for how it is used. If you enjoy an anime, please support the creators through official channels.

## Features

- **Search and browse** — find anime by name and see all available episodes
- **Batch download** — select individual episodes, a range, or a whole season at once
- **Quality and audio selection** — choose 1080p/720p/480p/360p and sub/dub when available
- **Background operation** — the browser minimizes and downloads run quietly while you do other things
- **Jellyfin/Plex naming** — optional `Series/Series - S01E01` folder structure that media servers recognize automatically
- **Resilient** — automatically retries transient failures and resumes stalled downloads from where they left off

## How it works

AnimePahe and its file host (Kwik) sit behind Cloudflare, which blocks ordinary scripts and automation tools. This downloader uses [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (a stealth-patched fork of Playwright) to drive a real Chromium browser that clears the Cloudflare check. It keeps one persistent browser session alive with a saved profile, so you typically only solve the "Verify you are human" check once per run.

The download flow for each episode is:

1. Scrape the play page for the real download links (`pahe.win` redirects, with quality/audio labels)
2. Follow `pahe.win` → `kwik.cx` → the direct MP4 URL on the CDN
3. Stream that MP4 straight to disk with the correct headers

## Requirements

- Python 3.10 or newer
- A Chromium browser (installed automatically by Patchright)
- The Python packages and system libraries listed below

## Installation

### Arch Linux / CachyOS

```bash
# System packages (Tkinter + BeautifulSoup)
sudo pacman -S python-beautifulsoup4 tk

# Patchright (stealth Playwright) + its Chromium
python -m pip install patchright --break-system-packages
python -m patchright install chromium
```

### Debian / Ubuntu

```bash
sudo apt install python3-tk python3-bs4
python3 -m pip install patchright
python3 -m patchright install chromium
```

### Windows / macOS

```bash
pip install -r requirements.txt
python -m patchright install chromium
```

(Tkinter ships with the standard Python installer on Windows and macOS.)

## Usage

```bash
python animepahe_downloader.py
```

1. When the browser window appears, solve the **"Verify you are human"** check if shown. The window then minimizes itself.
2. **Search** for an anime by name and click a result to load its episodes.
3. **Tick** the episodes you want — or use the range box, or the **All** button.
4. Set your **quality**, **audio**, **season number**, and download folder.
5. Click **Download**. Episodes download in the background to your chosen folder.

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

**The browser keeps showing the Cloudflare check.** Make sure Patchright is installed into the *same* Python that runs the app, and that you ran `python -m patchright install chromium`. If a plain Playwright is used by mistake, Cloudflare will loop forever.

**A download stalls or an episode is skipped.** Transient network and CDN hiccups are normal; the tool retries resolves and resumes stalled downloads automatically. A single failure that recovers is nothing to worry about. Repeated failures across many episodes suggest a connection problem or a temporary block.

**"Sorry, you have been blocked" from Kwik.** This is a server-side IP block, not a bug. Wait a while or try a different connection/VPN.

The app logs everything to `~/.config/animepahe-dl/engine.log` and to the terminal — check there first when something goes wrong.

## Contributing

Issues and pull requests are welcome. Because AnimePahe and Kwik change their page structure and bot protection periodically, the scraping/resolve logic occasionally needs updating — the detailed logs make it straightforward to see which step broke.

## License

This project is licensed under the **GNU General Public License v3.0** — see the [LICENSE](LICENSE) file for details. In short: you're free to use, modify, and redistribute it, but any distributed forks must also remain open-source under the same license.
