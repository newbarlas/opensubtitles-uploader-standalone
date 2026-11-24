# OpenSubtitles Uploader (Standalone Windows `.exe` & Python App)

A lightweight, zero-bloat standalone replacement for the broken legacy `vankasteelj/opensubtitles-uploader` (v2.8.0 NW.js app) that works cleanly on modern Windows 10/11.

Built entirely with Python's standard library (`tkinter`, `xmlrpc.client`, `zlib`, `urllib`) and packaged into a single portable Windows `.exe` (`dist/OpenSubtitlesUploader.exe`).

---

## Why This Exists

The classic `opensubtitles-uploader.exe` (v2.8.0) fails on modern Windows with blank screens, NW.js profile errors, or `401 Unauthorized` connection failures—and even when it connects, OpenSubtitles' server-side anti-spam filter frequently rejects subtitles containing legacy watermarks, WebVTT headers, or repetitive Hearing Impaired (SDH) cues.

This standalone uploader solves all of those issues out of the box:

- **Zero Node.js / NW.js Bloat:** Pure Python standard library GUI (`tkinter`) compiled to a single portable `.exe`.
- **Working XML-RPC Authentication:** Uses the `os-uploader` User-Agent accepted by `https://api.opensubtitles.org/xml-rpc`, with explicit **Log In** and **Use Guest (Anonymous)** modes (and automatic Guest fallback).
- **Built-in IMDb Search & Auto-Match:**
  - Automatically checks the 64-bit OpenSubtitles video hash (`CheckMovieHash2`).
  - If the video hash isn't in OpenSubtitles yet, automatically cleans the video filename (e.g. `final-destination-2-2003[1080p].mp4` → `final destination 2 2003`) and queries IMDb's public JSON suggestion endpoint to fill in the exact IMDb ID (`tt0309593`).
  - Includes an in-app **Search / Browse IMDb...** window to search and pick any movie or TV show without leaving the app.
- **Built-in Auto-Clean (Anti-Spam & Format Repair):**
  - Automatically removes ad/watermark cues (`Advertise your product or brand here`, `www.OpenSubtitles.org`, `Subscene`, `YIFY`, `Addic7ed`, URLs) that trigger OpenSubtitles' anti-spam blocker.
  - Merges consecutive duplicate cues (e.g. back-to-back `NO!` → `NO!`, `[SCREAMS]` → `[SCREAMING]`, `Marcus.` → `Marcus, Marcus.`) and removes repeated identical lines inside a single cue.
  - Strips accidental `WEBVTT FILE` headers inside `.srt` files, converts dot timestamps (`00:00:17.893`) to standard `.srt` comma timestamps (`00:00:17,893`), and renumbers cues `1..N`.
  - Auto-detects **Hearing Impaired (SDH)** subtitles when sound/speaker tags (`[SCREAMING]`, `[LAUGHING]`, `SPEAKER:`) are present.
- **Auto-Reset After Upload:** Automatically clears the file and movie fields after each successful upload while keeping your account logged in for the next movie.

---

## Quick Start

### Option A: Run the Standalone Windows Executable
Double-click **`dist/OpenSubtitlesUploader.exe`** — no Python or dependencies required.

### Option B: Run from Python Source
Requires Python 3.10+ (uses only standard library modules):
```powershell
python opensubtitles_uploader.py
```

### Build the Single-File `.exe` Yourself
```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name OpenSubtitlesUploader opensubtitles_uploader.py
```
The compiled executable will be generated at `dist\OpenSubtitlesUploader.exe`.

---

## How to Use
1. **Account (Optional):** Enter your **OpenSubtitles.org** username and password and click **Log In** (or leave blank / click **Use Guest** to upload anonymously).
2. **Select Video & Subtitle:** Click **Browse...** to select your video file—if a matching `.srt` file is in the same folder, it will be selected automatically, and the movie's **IMDb ID** will auto-detect.
3. **Verify IMDb Match:** If needed, click **Search / Browse IMDb...** to search by title and double-click the matching movie.
4. **Upload:** Click **Check & Upload Subtitle**. Auto-Clean will sanitize the subtitle and upload it to OpenSubtitles.
