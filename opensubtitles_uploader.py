import base64
import hashlib
import json
import os
import re
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import urllib.parse
import urllib.request
import webbrowser
import xmlrpc.client
import zlib

XMLRPC_URL = "https://api.opensubtitles.org/xml-rpc"
DEFAULT_USER_AGENT = "os-uploader"

# Common ISO 639-2B language codes accepted by OpenSubtitles XML-RPC
LANGUAGES = [
    "eng", "spa", "fre", "ger", "ita", "por", "pob", "pol",
    "rus", "tur", "dut", "ara", "chi", "jpn", "kor", "swe",
    "nor", "dan", "fin", "cze", "hun", "rum", "gre", "heb", "ukr"
]

AD_PATTERNS = re.compile(
    r"(advertise your product|opensubtitles\.org|opensubtitles\.com|www\.|http://|https://|"
    r"support us and become vip|subscene|addic7ed|yify|yts\.|podnapisi|divxsubtitles|"
    r"downloaded from|synced and corrected by|subtitles by)",
    re.IGNORECASE,
)

TIME_LINE_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2})[.,](\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2})[.,](\d{3})"
)


def normalize_for_dedup(text: str) -> str:
    """Normalize subtitle cue text to detect repetitive consecutive cues."""
    t = text.lower()
    t = re.sub(r"^[a-z0-9\s]+(\[.*?\])?\s*:", " ", t, flags=re.MULTILINE)
    t = re.sub(r"\[.*?\]", lambda m: "sdh_scream" if "scream" in m.group(0) else m.group(0), t)
    words = re.findall(r"[a-z0-9]+", t)
    # Collapse consecutive identical words so 'Marcus.' and 'Marcus, Marcus.' normalize the same
    collapsed = []
    for w in words:
        if not collapsed or collapsed[-1] != w:
            collapsed.append(w)
    return " ".join(collapsed)


def clean_cue_line(line: str) -> str:
    """Clean spam-like punctuation or 3x repeated words inside a line."""
    # Replace 4+ dots with standard 3-dot ellipsis
    line = re.sub(r"\.{4,}", "...", line)
    # Collapse 3x identical repeated short words/phrases (e.g. 'Wait, wait, wait.' -> 'Wait, wait.')
    line = re.sub(r"\b(\w+)([,!?\s]+)\1\2\1\b", r"\1\2\1", line, flags=re.IGNORECASE)
    return line


def clean_subtitle_bytes(raw_bytes: bytes) -> tuple[bytes, dict]:
    """
    Clean an SRT/WebVTT subtitle file to prevent OpenSubtitles anti-spam rejections:
    - Removes WEBVTT header and converts dot timestamps (.000) to standard SRT commas (,000)
    - Removes advertisement / watermark / URL cues
    - Deduplicates identical repeated lines within a single cue
    - Merges consecutive cues with identical normalized text
    - Renumbers all cues sequentially 1..N
    - Detects whether the subtitle contains Hearing Impaired (SDH) tags
    """
    text = raw_bytes.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    # Strip WEBVTT header lines at top
    idx = 0
    removed_webvtt = False
    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped or stripped.upper().startswith("WEBVTT"):
            if stripped.upper().startswith("WEBVTT"):
                removed_webvtt = True
            idx += 1
        else:
            break

    body = "\n".join(lines[idx:])
    raw_blocks = re.split(r"\n\s*\n", body.strip())

    parsed_cues = []
    ads_removed = 0
    intra_cue_deduped = 0

    for block in raw_blocks:
        blines = [ln.strip() for ln in block.strip().split("\n") if ln.strip()]
        if not blines:
            continue

        # Find timestamp line
        time_idx = -1
        time_match = None
        for i, ln in enumerate(blines):
            m = TIME_LINE_RE.match(ln)
            if m:
                time_idx = i
                time_match = m
                break

        if time_idx == -1 or not time_match:
            continue

        start_ts = f"{time_match.group(1)},{time_match.group(2)}"
        end_ts = f"{time_match.group(3)},{time_match.group(4)}"
        cue_lines = blines[time_idx + 1 :]
        if not cue_lines:
            continue

        full_cue_text = "\n".join(cue_lines)
        if AD_PATTERNS.search(full_cue_text):
            ads_removed += 1
            continue

        # Deduplicate consecutive identical lines inside the same cue
        deduped_lines = []
        for raw_ln in cue_lines:
            ln = clean_cue_line(raw_ln)
            if deduped_lines and normalize_for_dedup(deduped_lines[-1]) == normalize_for_dedup(ln):
                intra_cue_deduped += 1
                continue
            deduped_lines.append(ln)

        parsed_cues.append(
            {
                "start": start_ts,
                "end": end_ts,
                "text": "\n".join(deduped_lines),
            }
        )

    # Merge consecutive cues that have identical normalized text
    merged_cues = []
    consecutive_merged = 0
    for cue in parsed_cues:
        if merged_cues:
            prev = merged_cues[-1]
            norm_prev = normalize_for_dedup(prev["text"])
            norm_curr = normalize_for_dedup(cue["text"])
            if norm_prev and norm_prev == norm_curr:
                prev["end"] = cue["end"]
                consecutive_merged += 1
                continue
        merged_cues.append(cue)

    # Check if subtitle has SDH / Hearing Impaired cues like [LAUGHING], [SCREAMING], SPEAKER:
    full_out_text = "\n".join(c["text"] for c in merged_cues)
    sdh_tags = len(re.findall(r"\[[A-Z0-9\s\-]{3,}\]|^[A-Z]{3,}\s*:", full_out_text, flags=re.MULTILINE))
    is_sdh_detected = sdh_tags >= 10

    out_blocks = []
    for i, cue in enumerate(merged_cues, start=1):
        out_blocks.append(f"{i}\r\n{cue['start']} --> {cue['end']}\r\n{cue['text'].replace(chr(10), chr(13) + chr(10))}")

    cleaned_str = "\r\n\r\n".join(out_blocks) + "\r\n"
    stats = {
        "removed_webvtt": removed_webvtt,
        "ads_removed": ads_removed,
        "intra_cue_deduped": intra_cue_deduped,
        "consecutive_merged": consecutive_merged,
        "total_cues": len(merged_cues),
        "is_sdh_detected": is_sdh_detected,
    }
    return cleaned_str.encode("utf-8"), stats


class OpenSubtitlesTransport(xmlrpc.client.SafeTransport):
    """HTTPS XML-RPC transport that sends the required OpenSubtitles User-Agent."""
    def __init__(self, user_agent: str = DEFAULT_USER_AGENT):
        super().__init__()
        self.user_agent = user_agent


def compute_movie_hash(filepath: str) -> tuple[str, int]:
    """Compute the standard OpenSubtitles 64-bit video file hash and byte size."""
    chunk_size = 65536
    fmt = "<Q"
    step = struct.calcsize(fmt)
    filesize = os.path.getsize(filepath)

    if filesize < chunk_size * 2:
        raise ValueError("Video file must be at least 128 KB to compute an OpenSubtitles hash.")

    filehash = filesize
    with open(filepath, "rb") as f:
        for _ in range(chunk_size // step):
            (val,) = struct.unpack(fmt, f.read(step))
            filehash = (filehash + val) & 0xFFFFFFFFFFFFFFFF

        f.seek(filesize - chunk_size, os.SEEK_SET)
        for _ in range(chunk_size // step):
            (val,) = struct.unpack(fmt, f.read(step))
            filehash = (filehash + val) & 0xFFFFFFFFFFFFFFFF

    return f"{filehash:016x}", filesize


def compute_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def clean_filename_for_search(filename: str) -> str:
    """Convert a release filename like 'final-destination-2-2003[1080p].mp4' into 'final destination 2 2003'."""
    name = os.path.splitext(os.path.basename(filename))[0]
    name = re.sub(r"\[.*?\]|\(.*?\)", " ", name)
    name = re.sub(
        r"\b(1080p|720p|2160p|480p|4k|uhd|hdr|bluray|brrip|bdrip|webrip|web-dl|webdl|dvdrip|hdtv|x264|x265|h264|h265|hevc|aac|ac3|dts|ddp5|yts|yify|rarbg|extended|proper|repack)\b",
        " ",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"[._\-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def search_imdb_suggestions(query: str) -> list[dict]:
    """Query IMDb's public JSON suggestion endpoint and return matching titles."""
    cleaned = query.strip()
    if not cleaned:
        return []
    url = f"https://v3.sg.media-imdb.com/suggestion/x/{urllib.parse.quote(cleaned)}.json"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))

    results = []
    for item in data.get("d", []):
        imdb_id = str(item.get("id", ""))
        if not imdb_id.startswith("tt"):
            continue
        results.append(
            {
                "id": imdb_id,
                "title": item.get("l", "Unknown Title"),
                "year": item.get("y", ""),
                "kind": item.get("qid") or item.get("q") or "title",
                "cast": item.get("s", ""),
            }
        )
    return results


class IMDbSearchDialog(tk.Toplevel):
    """Popup window to search & browse IMDb titles and pick an IMDb ID (tt...)."""

    def __init__(self, parent: "UploaderApp", initial_query: str = ""):
        super().__init__(parent)
        self.parent_app = parent
        self.title("Search & Browse IMDb")
        self.geometry("620x380")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self.query_var = tk.StringVar(value=initial_query)
        self.results: list[dict] = []

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Movie / Show Title:").pack(side="left", padx=(0, 6))
        self.search_entry = ttk.Entry(top, textvariable=self.query_var, width=42)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.search_entry.bind("<Return>", lambda _e: self._run_search())

        self.search_btn = ttk.Button(top, text="Search IMDb", command=self._run_search)
        self.search_btn.pack(side="left", padx=(0, 6))

        ttk.Button(top, text="Open IMDb.com", command=self._open_in_browser).pack(side="left")

        # Treeview of results
        list_frame = ttk.Frame(self, padding=(10, 0, 10, 6))
        list_frame.pack(fill="both", expand=True)

        cols = ("imdb_id", "title", "year", "type", "cast")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=11)
        self.tree.heading("imdb_id", text="IMDb ID")
        self.tree.heading("title", text="Title")
        self.tree.heading("year", text="Year")
        self.tree.heading("type", text="Type")
        self.tree.heading("cast", text="Stars / Notes")

        self.tree.column("imdb_id", width=85, anchor="w")
        self.tree.column("title", width=210, anchor="w")
        self.tree.column("year", width=55, anchor="center")
        self.tree.column("type", width=80, anchor="w")
        self.tree.column("cast", width=150, anchor="w")

        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda _e: self._use_selected())
        self.tree.bind("<Return>", lambda _e: self._use_selected())

        # Bottom bar
        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")

        self.status_lbl = ttk.Label(bottom, text="Tip: Double-click any movie in the list to select it.")
        self.status_lbl.pack(side="left")

        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="Use Selected Movie", command=self._use_selected).pack(side="right")

        self.search_entry.focus_set()
        if initial_query.strip():
            self._run_search()

    def _run_search(self):
        query = self.query_var.get().strip()
        if not query:
            return
        self.status_lbl.configure(text=f"Searching IMDb for '{query}'...")
        self.search_btn.configure(state="disabled")
        for row in self.tree.get_children():
            self.tree.delete(row)

        def worker():
            try:
                items = search_imdb_suggestions(query)
                self.after(0, lambda: self._populate(items))
            except Exception as exc:
                self.after(0, lambda: self.status_lbl.configure(text=f"Search failed: {exc}"))
            finally:
                self.after(0, lambda: self.search_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _populate(self, items: list[dict]):
        self.results = items
        for idx, item in enumerate(items):
            self.tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(item["id"], item["title"], item["year"], item["kind"], item["cast"]),
            )
        if items:
            self.tree.selection_set("0")
            self.tree.focus("0")
            self.status_lbl.configure(
                text=f"Found {len(items)} result(s). Double-click a movie or click 'Use Selected Movie'."
            )
        else:
            self.status_lbl.configure(text="No matches found. Try a simpler title (e.g. 'Final Destination 2').")

    def _use_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a Movie", "Click a movie in the list first.", parent=self)
            return
        idx = int(sel[0])
        chosen = self.results[idx]
        self.parent_app.set_selected_imdb(chosen)
        self.destroy()

    def _open_in_browser(self):
        sel = self.tree.selection()
        if sel:
            chosen = self.results[int(sel[0])]
            webbrowser.open(f"https://www.imdb.com/title/{chosen['id']}/")
            return
        q = urllib.parse.quote(self.query_var.get().strip())
        webbrowser.open(f"https://www.imdb.com/find/?q={q}" if q else "https://www.imdb.com/")


class UploaderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OpenSubtitles Uploader (Standalone)")
        self.geometry("710x640")
        self.resizable(False, False)

        self.video_path = tk.StringVar()
        self.sub_path = tk.StringVar()
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.account_status = tk.StringVar(value="Mode: Guest / Anonymous (or enter OpenSubtitles.org username & click Log In)")
        self.imdb_id = tk.StringVar()
        self.matched_movie_label = tk.StringVar(value="No movie selected yet")
        self.language = tk.StringVar(value="eng")
        self.release_name = tk.StringVar()
        self.comment = tk.StringVar()
        self.is_hd = tk.BooleanVar(value=True)
        self.is_hi = tk.BooleanVar(value=False)
        self.auto_clean_sub = tk.BooleanVar(value=True)

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # Credentials
        cred_frame = ttk.LabelFrame(self, text="OpenSubtitles.org Account (Optional — Leave blank for Guest Upload)")
        cred_frame.pack(fill="x", padx=10, pady=6)

        ttk.Label(cred_frame, text="Username:").grid(row=0, column=0, sticky="w", **pad)
        user_entry = ttk.Entry(cred_frame, textvariable=self.username, width=20)
        user_entry.grid(row=0, column=1, sticky="w", **pad)
        user_entry.bind("<Return>", lambda _e: self._start_test_login())

        ttk.Label(cred_frame, text="Password:").grid(row=0, column=2, sticky="w", **pad)
        pass_entry = ttk.Entry(cred_frame, textvariable=self.password, show="*", width=20)
        pass_entry.grid(row=0, column=3, sticky="w", **pad)
        pass_entry.bind("<Return>", lambda _e: self._start_test_login())

        self.login_btn = ttk.Button(cred_frame, text="Log In", command=self._start_test_login)
        self.login_btn.grid(row=0, column=4, sticky="w", padx=6, pady=4)

        ttk.Button(cred_frame, text="Use Guest", command=self._use_guest).grid(
            row=0, column=5, sticky="w", padx=4, pady=4
        )

        ttk.Label(cred_frame, textvariable=self.account_status, foreground="#1a5fb4").grid(
            row=1, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 4)
        )

        # Files
        file_frame = ttk.LabelFrame(self, text="Files")
        file_frame.pack(fill="x", padx=10, pady=6)

        ttk.Label(file_frame, text="Video File:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(file_frame, textvariable=self.video_path, width=56).grid(row=0, column=1, **pad)
        ttk.Button(file_frame, text="Browse...", command=self._pick_video).grid(row=0, column=2, **pad)

        ttk.Label(file_frame, text="Subtitle File:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(file_frame, textvariable=self.sub_path, width=56).grid(row=1, column=1, **pad)
        ttk.Button(file_frame, text="Browse...", command=self._pick_sub).grid(row=1, column=2, **pad)

        # Metadata
        meta_frame = ttk.LabelFrame(self, text="Movie & Subtitle Details")
        meta_frame.pack(fill="x", padx=10, pady=6)

        ttk.Label(meta_frame, text="IMDb ID (e.g. tt0309593):").grid(row=0, column=0, sticky="w", **pad)

        imdb_row = ttk.Frame(meta_frame)
        imdb_row.grid(row=0, column=1, columnspan=2, sticky="w", padx=8, pady=4)

        imdb_entry = ttk.Entry(imdb_row, textvariable=self.imdb_id, width=16)
        imdb_entry.pack(side="left", padx=(0, 6))
        imdb_entry.bind("<Return>", lambda _e: self._open_imdb_search())

        ttk.Button(imdb_row, text="Search / Browse IMDb...", command=self._open_imdb_search).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(imdb_row, text="Auto-Detect Movie", command=self._start_detect).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(imdb_row, text="View on IMDb.com", command=self._open_current_imdb_web).pack(side="left")

        ttk.Label(meta_frame, text="Matched Movie:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Label(meta_frame, textvariable=self.matched_movie_label, foreground="#26a269").grid(
            row=1, column=1, columnspan=2, sticky="w", **pad
        )

        ttk.Label(meta_frame, text="Language (ISO 639-2B):").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(meta_frame, textvariable=self.language, values=LANGUAGES, width=17).grid(
            row=2, column=1, sticky="w", **pad
        )

        ttk.Label(meta_frame, text="Release Name:").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(meta_frame, textvariable=self.release_name, width=52).grid(
            row=3, column=1, columnspan=2, sticky="w", **pad
        )

        ttk.Label(meta_frame, text="Author Comment:").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(meta_frame, textvariable=self.comment, width=52).grid(
            row=4, column=1, columnspan=2, sticky="w", **pad
        )

        flags = ttk.Frame(meta_frame)
        flags.grid(row=5, column=1, columnspan=2, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(flags, text="High Definition (720p/1080p/4K)", variable=self.is_hd).pack(
            side="left", padx=(0, 12)
        )
        ttk.Checkbutton(flags, text="Hearing Impaired (SDH)", variable=self.is_hi).pack(side="left")

        clean_row = ttk.Frame(meta_frame)
        clean_row.grid(row=6, column=1, columnspan=2, sticky="w", padx=8, pady=2)
        ttk.Checkbutton(
            clean_row,
            text="Auto-Clean Subtitle (remove ads, fix WEBVTT/.srt & merge duplicate cues)",
            variable=self.auto_clean_sub,
        ).pack(side="left")

        # Actions
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=6)
        ttk.Button(btn_frame, text="Clean & Save Subtitle File", command=self._clean_sub_file_on_disk).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btn_frame, text="Reset Form", command=self._reset_form).pack(side="left")
        self.upload_btn = ttk.Button(btn_frame, text="Check & Upload Subtitle", command=self._start_upload)
        self.upload_btn.pack(side="right")

        # Log output
        self.log_box = scrolledtext.ScrolledText(self, height=10, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log("Ready! Auto-Clean is enabled to remove ads and fix anti-spam triggers automatically.")

    def log(self, msg: str):
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, _append)

    def set_selected_imdb(self, item: dict):
        self.imdb_id.set(item["id"])
        year_part = f" ({item['year']})" if item.get("year") else ""
        self.matched_movie_label.set(f"{item['title']}{year_part} [{item['id']}]")
        self.log(f"Selected IMDb movie: {item['title']}{year_part} — {item['id']}")

    def _reset_form(self):
        """Reset file and movie fields after a successful upload while keeping login credentials."""
        self.video_path.set("")
        self.sub_path.set("")
        self.imdb_id.set("")
        self.matched_movie_label.set("No movie selected yet")
        self.release_name.set("")
        self.comment.set("")
        self.is_hd.set(True)
        self.is_hi.set(False)
        self.log("Form reset and ready for the next subtitle upload.")

    def _use_guest(self):
        self.username.set("")
        self.password.set("")
        self.account_status.set("Mode: Guest / Anonymous (Ready to upload without account)")
        self.log("Switched to Guest / Anonymous mode.")

    def _start_test_login(self):
        self.login_btn.configure(state="disabled")
        threading.Thread(target=self._test_login_worker, daemon=True).start()

    def _test_login_worker(self):
        proxy = None
        token = None
        try:
            proxy, token, logged_user = self._connect_and_login(allow_guest_fallback=False)
            label = f"Logged in as: {logged_user}"
            self.after(0, lambda: self.account_status.set(label))
        except Exception as exc:
            self.log(f"LOGIN ERROR: {exc}")
            self.after(
                0,
                lambda: self.account_status.set(
                    "Login failed (Check credentials or click 'Use Guest')"
                ),
            )
        finally:
            if proxy and token:
                try:
                    proxy.LogOut(token)
                except Exception:
                    pass
            self.after(0, lambda: self.login_btn.configure(state="normal"))

    def _pick_video(self):
        path = filedialog.askopenfilename(
            title="Select Video File",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.wmv *.m4v"), ("All files", "*.*")]
        )
        if path:
            self.video_path.set(path)
            base_release = os.path.splitext(os.path.basename(path))[0]
            self.release_name.set(base_release)
            if not self.sub_path.get():
                folder = os.path.dirname(path)
                for ext in (".srt", ".ass", ".ssa", ".sub", ".vtt"):
                    candidate = os.path.join(folder, base_release + ext)
                    if os.path.isfile(candidate):
                        self.sub_path.set(candidate)
                        self.log(f"Auto-selected matching subtitle file: {os.path.basename(candidate)}")
                        break
            self._start_detect()

    def _pick_sub(self):
        path = filedialog.askopenfilename(
            title="Select Subtitle File",
            filetypes=[("Subtitles", "*.srt *.ass *.ssa *.sub *.vtt *.txt"), ("All files", "*.*")]
        )
        if path:
            self.sub_path.set(path)

    def _clean_sub_file_on_disk(self):
        spath = self.sub_path.get().strip()
        if not spath or not os.path.isfile(spath):
            messagebox.showwarning("Missing Subtitle", "Select a subtitle file first.")
            return
        try:
            with open(spath, "rb") as f:
                raw_bytes = f.read()
            cleaned_bytes, stats = clean_subtitle_bytes(raw_bytes)
            with open(spath, "wb") as f:
                f.write(cleaned_bytes)
            if stats["is_sdh_detected"]:
                self.is_hi.set(True)
            self.log(
                f"Cleaned & saved '{os.path.basename(spath)}': "
                f"removed {stats['ads_removed']} ad cue(s), "
                f"merged {stats['consecutive_merged']} consecutive duplicate(s), "
                f"deduped {stats['intra_cue_deduped']} repeated line(s), "
                f"{stats['total_cues']} cues ready."
            )
        except Exception as exc:
            self.log(f"Error cleaning subtitle: {exc}")

    def _open_imdb_search(self):
        initial = clean_filename_for_search(self.release_name.get() or self.video_path.get())
        IMDbSearchDialog(self, initial_query=initial)

    def _open_current_imdb_web(self):
        raw_id = self.imdb_id.get().strip().lower()
        if raw_id:
            digits = raw_id.lstrip("t")
            if digits.isdigit():
                webbrowser.open(f"https://www.imdb.com/title/tt{digits.zfill(7)}/")
                return
        initial = clean_filename_for_search(self.release_name.get() or self.video_path.get())
        q = urllib.parse.quote(initial)
        webbrowser.open(f"https://www.imdb.com/find/?q={q}" if q else "https://www.imdb.com/")

    def _connect_and_login(self, allow_guest_fallback: bool = True):
        transport = OpenSubtitlesTransport(DEFAULT_USER_AGENT)
        proxy = xmlrpc.client.ServerProxy(XMLRPC_URL, transport=transport, allow_none=True)

        user = self.username.get().strip()
        raw_pass = self.password.get()
        pwd_md5 = hashlib.md5(raw_pass.encode("utf-8")).hexdigest() if raw_pass else ""

        if user and raw_pass:
            self.log(f"Logging in to OpenSubtitles.org as '{user}'...")
            resp = proxy.LogIn(user, pwd_md5, "en", DEFAULT_USER_AGENT)
            status = resp.get("status", "")
            if status.startswith("200"):
                token = resp.get("token")
                self.log(f"Logged in successfully as '{user}'.")
                self.after(0, lambda: self.account_status.set(f"Logged in as: {user}"))
                return proxy, token, user

            if not allow_guest_fallback:
                raise RuntimeError(f"{status}")

            self.log(
                f"Account login returned '{status}'. Falling back to Guest / Anonymous connection automatically..."
            )

        self.log("Connecting to OpenSubtitles.org as Guest (anonymous)...")
        resp = proxy.LogIn("", "", "en", DEFAULT_USER_AGENT)
        status = resp.get("status", "")
        if not status.startswith("200"):
            raise RuntimeError(f"Connection failed: {status}")

        token = resp.get("token")
        self.log("Connected as Guest (anonymous).")
        return proxy, token, "Guest (Anonymous)"

    def _start_detect(self):
        if not self.video_path.get() and not self.release_name.get():
            messagebox.showwarning("Missing Video", "Select a video file first.")
            return
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self):
        proxy = None
        token = None
        try:
            vpath = self.video_path.get()
            if vpath and os.path.isfile(vpath):
                mhash, msize = compute_movie_hash(vpath)
                self.log(f"Video hash: {mhash} ({msize} bytes) — checking OpenSubtitles DB...")
                proxy, token, _ = self._connect_and_login(allow_guest_fallback=True)
                res = proxy.CheckMovieHash2(token, [mhash])
                data_field = res.get("data", {})
                matches = data_field.get(mhash) if isinstance(data_field, dict) else None
                if matches and isinstance(matches, list):
                    best = matches[0]
                    imdb = str(best.get("MovieImdbID", ""))
                    title = best.get("MovieName", "Unknown")
                    year = best.get("MovieYear", "")
                    if imdb:
                        item = {"id": f"tt{imdb.zfill(7)}", "title": title, "year": year}
                        self.after(0, lambda: self.set_selected_imdb(item))
                        return

            query = clean_filename_for_search(self.release_name.get() or vpath)
            if query:
                self.log(f"Searching IMDb directly for '{query}'...")
                imdb_results = search_imdb_suggestions(query)
                if imdb_results:
                    best = imdb_results[0]
                    self.after(0, lambda: self.set_selected_imdb(best))
                    return

            self.log("Could not auto-detect movie. Click 'Search / Browse IMDb...' to pick it.")
        except Exception as exc:
            self.log(f"Auto-detect note: {exc}")
        finally:
            if proxy and token:
                try:
                    proxy.LogOut(token)
                except Exception:
                    pass

    def _start_upload(self):
        if not self.video_path.get() or not self.sub_path.get():
            messagebox.showwarning("Missing Files", "Select both a video file and a subtitle file.")
            return
        self.upload_btn.configure(state="disabled")
        threading.Thread(target=self._upload_worker, daemon=True).start()

    def _upload_worker(self):
        proxy = None
        token = None
        try:
            vpath = self.video_path.get()
            spath = self.sub_path.get()

            mhash, msize = compute_movie_hash(vpath)
            with open(spath, "rb") as f:
                sub_bytes = f.read()

            if self.auto_clean_sub.get():
                sub_bytes, stats = clean_subtitle_bytes(sub_bytes)
                with open(spath, "wb") as f:
                    f.write(sub_bytes)
                if stats["is_sdh_detected"]:
                    self.after(0, lambda: self.is_hi.set(True))
                self.log(
                    f"Auto-cleaned subtitle: removed {stats['ads_removed']} ad(s), "
                    f"merged {stats['consecutive_merged']} consecutive duplicate(s), "
                    f"{stats['total_cues']} clean cues."
                )

            sub_md5 = compute_md5(sub_bytes)

            self.log(f"Video Hash : {mhash} | Size: {msize}")
            self.log(f"Sub MD5    : {sub_md5} | File: {os.path.basename(spath)}")

            proxy, token, _ = self._connect_and_login(allow_guest_fallback=True)

            cd1 = {
                "subhash": sub_md5,
                "subfilename": os.path.basename(spath),
                "moviehash": mhash,
                "moviebytesize": str(msize),
                "moviefilename": os.path.basename(vpath),
            }

            self.log("Checking if subtitle is already in OpenSubtitles database...")
            try_res = proxy.TryUploadSubtitles(token, {"cd1": cd1})
            status = try_res.get("status", "")
            if not status.startswith("200"):
                raise RuntimeError(f"TryUploadSubtitles failed: {status}")

            if str(try_res.get("alreadyindb", "0")) == "1":
                self.log(f"Subtitle is already in OpenSubtitles database! Info: {try_res.get('data')}")
                return

            imdb_digits = self.imdb_id.get().strip().lower().lstrip("t")
            if not imdb_digits:
                data_block = try_res.get("data", {})
                if isinstance(data_block, dict):
                    imdb_digits = str(data_block.get("IDMovieImdb", "")).lstrip("t")
                elif isinstance(data_block, list) and data_block and isinstance(data_block[0], dict):
                    imdb_digits = str(data_block[0].get("IDMovieImdb", "")).lstrip("t")

            if not imdb_digits:
                query = clean_filename_for_search(self.release_name.get() or vpath)
                if query:
                    imdb_results = search_imdb_suggestions(query)
                    if imdb_results:
                        best = imdb_results[0]
                        imdb_digits = best["id"].lstrip("t")
                        self.after(0, lambda: self.set_selected_imdb(best))

            if not imdb_digits:
                raise ValueError("IMDb ID is required. Click 'Search / Browse IMDb...' to pick the movie.")

            # OpenSubtitles XML-RPC expects zlib-compressed (RFC 1950 / PHP gzcompress) + base64
            encoded_sub = base64.b64encode(zlib.compress(sub_bytes)).decode("ascii")

            payload = {
                "baseinfo": {
                    "idmovieimdb": imdb_digits,
                    "sublanguageid": self.language.get().strip(),
                    "moviereleasename": self.release_name.get().strip()
                    or os.path.splitext(os.path.basename(vpath))[0],
                    "highdefinition": "1" if self.is_hd.get() else "0",
                    "hearingimpaired": "1" if self.is_hi.get() else "0",
                    "subauthorcomment": self.comment.get().strip(),
                },
                "cd1": {
                    **cd1,
                    "subcontent": encoded_sub,
                },
            }

            self.log(f"Uploading subtitle for IMDb tt{imdb_digits.zfill(7)}...")
            up_res = proxy.UploadSubtitles(token, payload)
            up_status = up_res.get("status", "")
            if up_status.startswith("200"):
                sub_url = up_res.get("data", "")
                self.log(f"SUCCESS ({up_status})! Uploaded subtitle URL: {sub_url}")
                def _on_success():
                    messagebox.showinfo(
                        "Upload Successful!",
                        f"Your subtitle was uploaded to OpenSubtitles!\n\n{sub_url}",
                    )
                    self._reset_form()
                self.after(0, _on_success)
            else:
                self.log(f"UPLOAD REJECTED ({up_status}): {up_res}")

        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            if proxy and token:
                try:
                    proxy.LogOut(token)
                except Exception:
                    pass
            self.after(0, lambda: self.upload_btn.configure(state="normal"))


if __name__ == "__main__":
    app = UploaderApp()
    app.mainloop()
