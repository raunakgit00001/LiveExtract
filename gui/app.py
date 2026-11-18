"""
LiveExtract — Minimal, modern desktop launcher for zero-disk streaming extraction.

Design goals:
- Clean, distraction-free interface for everyday users (paste link -> see name & size -> Download)
- Collapsible Advanced drawer for power users (parallel connections, buffer size, verification, logs)
- Lightweight native Tk rendering with throttled updates so it runs smoothly on low-end PCs
- Completely decoupled from the `liveextract` core library
"""

import ctypes
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from liveextract import __version__
from liveextract.client.controller import StreamController
from liveextract.client.link_inspector import format_bytes, inspect_url_preflight
from liveextract.client.stream_client import StreamClient, StreamProgress
from liveextract.core.verifier import verify_destination


# Neutral Dark Slate palette (calm, classic, high-contrast)
C_BG = "#121316"
C_SURFACE = "#1A1C21"
C_INPUT = "#21242B"
C_BORDER = "#2B2F38"
C_HOVER = "#2A2E38"
C_TEXT = "#F3F4F6"
C_MUTED = "#9CA3AF"
C_HINT = "#6B7280"
C_ACCENT = "#3B82F6"
C_ACCENT_HOVER = "#2563EB"
C_SUCCESS = "#22C55E"
C_WARN = "#F59E0B"
C_DANGER = "#EF4444"
C_TRACK = "#252932"


def _enable_windows_polish(root: tk.Tk) -> None:
    """Enable Windows DPI awareness and native dark titlebar when running on Windows 10/11."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        val = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(val), ctypes.sizeof(val)
        )
    except Exception:
        pass


def _default_output_dir() -> str:
    downloads = Path.home() / "Downloads"
    if downloads.exists():
        return str((downloads / "LiveExtract").resolve())
    return str((Path.cwd() / "LiveExtract_Output").resolve())


class FlatButton(tk.Label):
    """Lightweight flat button with smooth hover transitions and disabled state."""

    def __init__(
        self,
        parent,
        text: str,
        command,
        bg: str = C_INPUT,
        hover_bg: str = C_HOVER,
        fg: str = C_TEXT,
        font=("Segoe UI", 9, "bold"),
        padx: int = 16,
        pady: int = 8,
        border_color: Optional[str] = None,
    ):
        super().__init__(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=font,
            padx=padx,
            pady=pady,
            cursor="hand2",
            highlightthickness=1 if border_color else 0,
            highlightbackground=border_color or bg,
        )
        self._base_bg = bg
        self._hover_bg = hover_bg
        self._base_fg = fg
        self._command = command
        self._enabled = True

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _on_enter(self, _e=None):
        if self._enabled:
            self.configure(bg=self._hover_bg)

    def _on_leave(self, _e=None):
        if self._enabled:
            self.configure(bg=self._base_bg)

    def _on_click(self, _e=None):
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if enabled:
            self.configure(bg=self._base_bg, fg=self._base_fg, cursor="hand2")
        else:
            self.configure(bg="#1E2026", fg="#555A66", cursor="arrow")

    def set_style(self, text: Optional[str] = None, bg: Optional[str] = None, hover_bg: Optional[str] = None, fg: Optional[str] = None):
        if text is not None:
            self.configure(text=text)
        if bg is not None:
            self._base_bg = bg
            self.configure(bg=bg)
        if hover_bg is not None:
            self._hover_bg = hover_bg
        if fg is not None:
            self._base_fg = fg
            self.configure(fg=fg)


class SmoothProgressBar(tk.Canvas):
    """Slim, low-CPU 6px progress bar rendered on a lightweight Tk Canvas."""

    def __init__(self, parent, height: int = 6):
        super().__init__(parent, height=height, bg=C_BG, highlightthickness=0, bd=0)
        self._percent = 0.0
        self._bar_color = C_ACCENT
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_progress(self, percent: float, color: Optional[str] = None) -> None:
        self._percent = max(0.0, min(100.0, percent))
        if color is not None:
            self._bar_color = color
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())
        self.create_rectangle(0, 0, w, h, fill=C_TRACK, outline="")
        fill_w = int(w * (self._percent / 100.0))
        if fill_w > 0:
            self.create_rectangle(0, 0, fill_w, h, fill=self._bar_color, outline="")


class LiveExtractApp:
    COMPACT_HEIGHT = 400
    ADVANCED_HEIGHT = 640
    WINDOW_WIDTH = 600

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("LiveExtract")
        self.root.configure(bg=C_BG)
        self.root.geometry(f"{self.WINDOW_WIDTH}x{self.COMPACT_HEIGHT}")
        self.root.minsize(540, self.COMPACT_HEIGHT)
        self.root.resizable(True, False)

        # Runtime state
        self.controller = StreamController()
        self._worker_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._advanced_open = False

        # Link catcher & debouncing state
        self._last_clipboard = ""
        self._last_inspected_url = ""
        self._inspect_after_id: Optional[str] = None
        self._preflight: Optional[Dict[str, Any]] = None
        self._placeholder_active = True

        # Settings
        self.url_var = tk.StringVar(value="")
        self.dest_var = tk.StringVar(value=_default_output_dir())
        self.workers_var = tk.IntVar(value=4)
        self.chunk_var = tk.StringVar(value="256 KB")
        self.clipboard_catch_var = tk.BooleanVar(value=True)
        self.resume_var = tk.BooleanVar(value=True)
        self.verify_var = tk.BooleanVar(value=True)

        self._setup_ttk()
        self._build_ui()
        _enable_windows_polish(self.root)
        self._start_clipboard_watcher()

    def _setup_ttk(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Dark.TCombobox",
            fieldbackground=C_INPUT,
            background=C_SURFACE,
            foreground=C_TEXT,
            bordercolor=C_BORDER,
            arrowcolor=C_MUTED,
        )

    def _build_ui(self) -> None:
        container = tk.Frame(self.root, bg=C_BG, padx=24, pady=20)
        container.pack(fill=tk.BOTH, expand=True)

        # ── Header ───────────────────────────────────────────────
        hdr = tk.Frame(container, bg=C_BG)
        hdr.pack(fill=tk.X, pady=(0, 16))

        tk.Label(
            hdr,
            text="LiveExtract",
            font=("Segoe UI", 16, "bold"),
            bg=C_BG,
            fg=C_TEXT,
        ).pack(side=tk.LEFT)

        tk.Label(
            hdr,
            text="Direct stream extractor",
            font=("Segoe UI", 9),
            bg=C_BG,
            fg=C_HINT,
        ).pack(side=tk.LEFT, padx=(10, 0), pady=(5, 0))

        # ── Link Input Row ───────────────────────────────────────
        url_frame = tk.Frame(
            container,
            bg=C_INPUT,
            highlightthickness=1,
            highlightbackground=C_BORDER,
            highlightcolor=C_ACCENT,
        )
        url_frame.pack(fill=tk.X, pady=(0, 12))

        self.url_entry = tk.Entry(
            url_frame,
            textvariable=self.url_var,
            font=("Segoe UI", 10),
            bg=C_INPUT,
            fg=C_HINT,
            insertbackground=C_TEXT,
            relief=tk.FLAT,
            bd=0,
        )
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 8), pady=10)
        self._set_placeholder()

        self.url_entry.bind("<FocusIn>", self._on_url_focus_in)
        self.url_entry.bind("<FocusOut>", self._on_url_focus_out)
        self.url_entry.bind("<KeyRelease>", self._on_url_change)

        btn_paste = FlatButton(
            url_frame,
            text="Paste",
            command=self._on_paste_click,
            bg=C_SURFACE,
            hover_bg=C_HOVER,
            fg=C_MUTED,
            font=("Segoe UI", 9),
            padx=12,
            pady=6,
        )
        btn_paste.pack(side=tk.RIGHT, padx=(0, 6), pady=4)

        # ── Minimal File Details Card ────────────────────────────
        info_card = tk.Frame(
            container,
            bg=C_SURFACE,
            highlightthickness=1,
            highlightbackground=C_BORDER,
            padx=16,
            pady=13,
        )
        info_card.pack(fill=tk.X, pady=(0, 12))

        self.lbl_name = tk.Label(
            info_card,
            text="No archive selected",
            font=("Segoe UI", 11, "bold"),
            bg=C_SURFACE,
            fg=C_TEXT,
            anchor=tk.W,
        )
        self.lbl_name.pack(fill=tk.X)

        self.lbl_meta = tk.Label(
            info_card,
            text="Paste or copy a .zip, .rar, or .7z link to inspect name and size",
            font=("Segoe UI", 9),
            bg=C_SURFACE,
            fg=C_MUTED,
            anchor=tk.W,
        )
        self.lbl_meta.pack(fill=tk.X, pady=(3, 0))

        # ── Save Location Row ────────────────────────────────────
        dest_row = tk.Frame(container, bg=C_BG)
        dest_row.pack(fill=tk.X, pady=(0, 16))

        tk.Label(
            dest_row,
            text="Save to",
            font=("Segoe UI", 9),
            bg=C_BG,
            fg=C_MUTED,
        ).pack(side=tk.LEFT, padx=(0, 10))

        dest_box = tk.Frame(
            dest_row,
            bg=C_INPUT,
            highlightthickness=1,
            highlightbackground=C_BORDER,
        )
        dest_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        tk.Entry(
            dest_box,
            textvariable=self.dest_var,
            font=("Segoe UI", 9),
            bg=C_INPUT,
            fg=C_TEXT,
            insertbackground=C_TEXT,
            relief=tk.FLAT,
            bd=0,
        ).pack(fill=tk.X, padx=10, pady=6)

        FlatButton(
            dest_row,
            text="Change",
            command=self._on_browse_dest,
            bg=C_SURFACE,
            hover_bg=C_HOVER,
            fg=C_TEXT,
            font=("Segoe UI", 9),
            padx=12,
            pady=5,
            border_color=C_BORDER,
        ).pack(side=tk.RIGHT)

        # ── Progress Bar & Live Status ───────────────────────────
        self.progress = SmoothProgressBar(container, height=6)
        self.progress.pack(fill=tk.X, pady=(0, 8))

        status_row = tk.Frame(container, bg=C_BG)
        status_row.pack(fill=tk.X, pady=(0, 16))

        self.lbl_status = tk.Label(
            status_row,
            text="Ready",
            font=("Segoe UI", 9),
            bg=C_BG,
            fg=C_MUTED,
        )
        self.lbl_status.pack(side=tk.LEFT)

        self.lbl_speed = tk.Label(
            status_row,
            text="",
            font=("Segoe UI", 9),
            bg=C_BG,
            fg=C_MUTED,
        )
        self.lbl_speed.pack(side=tk.RIGHT)

        # ── Primary Actions Row (Clean, context-aware) ───────────
        self.action_row = tk.Frame(container, bg=C_BG)
        self.action_row.pack(fill=tk.X, pady=(0, 14))

        self.btn_primary = FlatButton(
            self.action_row,
            text="Download & Extract",
            command=self._on_primary_action,
            bg=C_ACCENT,
            hover_bg=C_ACCENT_HOVER,
            fg="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            padx=20,
            pady=9,
        )
        self.btn_primary.pack(side=tk.LEFT)

        self.btn_secondary = FlatButton(
            self.action_row,
            text="Cancel",
            command=self._on_secondary_action,
            bg=C_SURFACE,
            hover_bg=C_HOVER,
            fg=C_MUTED,
            font=("Segoe UI", 9),
            padx=14,
            pady=9,
            border_color=C_BORDER,
        )
        # Hidden until active or completed
        self._secondary_mode = "hidden"

        self.btn_open_folder = FlatButton(
            self.action_row,
            text="Open Folder",
            command=self._on_open_folder,
            bg=C_SURFACE,
            hover_bg=C_HOVER,
            fg=C_TEXT,
            font=("Segoe UI", 9),
            padx=14,
            pady=9,
            border_color=C_BORDER,
        )

        # ── Footer (Advanced Drawer Toggle) ──────────────────────
        footer = tk.Frame(container, bg=C_BG)
        footer.pack(fill=tk.X, pady=(2, 0))

        self.lbl_adv_toggle = tk.Label(
            footer,
            text="Advanced ▸",
            font=("Segoe UI", 9),
            bg=C_BG,
            fg=C_MUTED,
            cursor="hand2",
        )
        self.lbl_adv_toggle.pack(side=tk.LEFT)
        self.lbl_adv_toggle.bind("<Button-1>", self._toggle_advanced)
        self.lbl_adv_toggle.bind("<Enter>", lambda _e: self.lbl_adv_toggle.configure(fg=C_TEXT))
        self.lbl_adv_toggle.bind("<Leave>", lambda _e: self.lbl_adv_toggle.configure(fg=C_MUTED))

        tk.Label(
            footer,
            text=f"v{__version__}",
            font=("Segoe UI", 8),
            bg=C_BG,
            fg=C_HINT,
        ).pack(side=tk.RIGHT)

        # ── Collapsible Advanced Drawer (Hidden by Default) ──────
        self.adv_panel = tk.Frame(
            container,
            bg=C_SURFACE,
            highlightthickness=1,
            highlightbackground=C_BORDER,
            padx=14,
            pady=12,
        )
        self._build_advanced_panel()

    def _build_advanced_panel(self) -> None:
        # Row 1: Connections & Chunk Buffer
        r1 = tk.Frame(self.adv_panel, bg=C_SURFACE)
        r1.pack(fill=tk.X, pady=(0, 8))

        tk.Label(r1, text="Connections", font=("Segoe UI", 9), bg=C_SURFACE, fg=C_MUTED).pack(side=tk.LEFT)
        tk.Spinbox(
            r1,
            from_=1,
            to=16,
            textvariable=self.workers_var,
            width=4,
            font=("Segoe UI", 9),
            bg=C_INPUT,
            fg=C_TEXT,
            buttonbackground=C_SURFACE,
            relief=tk.FLAT,
        ).pack(side=tk.LEFT, padx=(8, 20))

        tk.Label(r1, text="Buffer", font=("Segoe UI", 9), bg=C_SURFACE, fg=C_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(
            r1,
            textvariable=self.chunk_var,
            values=["64 KB", "128 KB", "256 KB", "512 KB", "1 MB"],
            width=8,
            state="readonly",
            style="Dark.TCombobox",
        ).pack(side=tk.LEFT, padx=(8, 0))

        # Utility buttons on the right
        FlatButton(
            r1,
            text="Verify Files",
            command=self._on_verify,
            bg=C_INPUT,
            hover_bg=C_HOVER,
            fg=C_TEXT,
            font=("Segoe UI", 8),
            padx=10,
            pady=4,
            border_color=C_BORDER,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        FlatButton(
            r1,
            text="Local File...",
            command=self._on_pick_local_file,
            bg=C_INPUT,
            hover_bg=C_HOVER,
            fg=C_TEXT,
            font=("Segoe UI", 8),
            padx=10,
            pady=4,
            border_color=C_BORDER,
        ).pack(side=tk.RIGHT)

        # Row 2: Toggles
        r2 = tk.Frame(self.adv_panel, bg=C_SURFACE)
        r2.pack(fill=tk.X, pady=(0, 10))

        for label, var in [
            ("Auto-catch clipboard links", self.clipboard_catch_var),
            ("Auto-resume", self.resume_var),
            ("CRC-32 verify", self.verify_var),
        ]:
            tk.Checkbutton(
                r2,
                text=label,
                variable=var,
                font=("Segoe UI", 8),
                bg=C_SURFACE,
                fg=C_MUTED,
                selectcolor=C_INPUT,
                activebackground=C_SURFACE,
                activeforeground=C_TEXT,
            ).pack(side=tk.LEFT, padx=(0, 14))

        # Row 3: Compact Activity Log
        self.log_box = tk.Text(
            self.adv_panel,
            height=6,
            bg=C_INPUT,
            fg=C_MUTED,
            font=("Consolas", 8),
            relief=tk.FLAT,
            bd=0,
            padx=8,
            pady=6,
            state=tk.DISABLED,
        )
        self.log_box.pack(fill=tk.BOTH, expand=True)

    # ── Placeholder & Input Handling ─────────────────────────────
    def _set_placeholder(self) -> None:
        self._placeholder_active = True
        self.url_var.set("Paste a .zip, .rar, or .7z link...")
        self.url_entry.configure(fg=C_HINT)

    def _get_url(self) -> str:
        if self._placeholder_active:
            return ""
        return self.url_var.get().strip()

    def _set_url(self, value: str) -> None:
        self._placeholder_active = False
        self.url_entry.configure(fg=C_TEXT)
        self.url_var.set(value)

    def _on_url_focus_in(self, _e=None) -> None:
        if self._placeholder_active:
            self._placeholder_active = False
            self.url_var.set("")
            self.url_entry.configure(fg=C_TEXT)

    def _on_url_focus_out(self, _e=None) -> None:
        if not self.url_var.get().strip():
            self._set_placeholder()

    def _on_paste_click(self) -> None:
        try:
            text = self.root.clipboard_get().strip()
        except Exception:
            return
        if text:
            self._last_clipboard = text
            self._set_url(text)
            self._schedule_inspect(delay_ms=50, force=True)

    def _on_url_change(self, _e=None) -> None:
        url = self._get_url()
        if not url or url == self._last_inspected_url:
            return
        if len(url) > 8 and (url.startswith("http://") or url.startswith("https://") or Path(url).exists()):
            self._schedule_inspect(delay_ms=550, force=False)

    def _schedule_inspect(self, delay_ms: int = 550, force: bool = False) -> None:
        if self._inspect_after_id:
            try:
                self.root.after_cancel(self._inspect_after_id)
            except Exception:
                pass
        self._inspect_after_id = self.root.after(delay_ms, lambda: self._run_preflight(force=force))

    def _start_clipboard_watcher(self) -> None:
        def _poll():
            if self.clipboard_catch_var.get() and not self._is_running:
                try:
                    clip = self.root.clipboard_get().strip()
                    if clip and clip != self._last_clipboard:
                        if clip.startswith("http://") or clip.startswith("https://"):
                            self._last_clipboard = clip
                            self._set_url(clip)
                            self._log("Link detected from clipboard.")
                            self._run_preflight(force=True)
                except Exception:
                    pass
            self.root.after(1000, _poll)

        self.root.after(1000, _poll)

    def _run_preflight(self, force: bool = False) -> None:
        url = self._get_url()
        if not url or (not force and url == self._last_inspected_url):
            return
        self._last_inspected_url = url

        self.lbl_name.configure(text="Inspecting link...", fg=C_MUTED)
        self.lbl_meta.configure(text="Fetching archive name and size")

        def _bg():
            try:
                info = inspect_url_preflight(url)
                self._preflight = info

                def _apply():
                    self.lbl_name.configure(text=info["filename"], fg=C_TEXT)
                    meta_line = f"{info['formatted_size'].strip()}  ·  {info['format']}  ·  Zero archive disk space"
                    self.lbl_meta.configure(text=meta_line, fg=C_MUTED)
                    self.lbl_status.configure(text="Ready", fg=C_MUTED)
                    self._log(f"Ready: {info['filename']} ({info['formatted_size'].strip()}, {info['format']})")

                self.root.after(0, _apply)
            except Exception as ex:
                def _err():
                    self.lbl_name.configure(text="Could not inspect link", fg=C_WARN)
                    self.lbl_meta.configure(text=str(ex)[:90], fg=C_HINT)
                    self._log(f"Inspect warning: {ex}")

                self.root.after(0, _err)

        threading.Thread(target=_bg, daemon=True).start()

    # ── UI Controls & Drawer ─────────────────────────────────────
    def _toggle_advanced(self, _e=None) -> None:
        self._advanced_open = not self._advanced_open
        if self._advanced_open:
            self.lbl_adv_toggle.configure(text="Advanced ▾")
            self.adv_panel.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
            self.root.geometry(f"{self.WINDOW_WIDTH}x{self.ADVANCED_HEIGHT}")
        else:
            self.lbl_adv_toggle.configure(text="Advanced ▸")
            self.adv_panel.pack_forget()
            self.root.geometry(f"{self.WINDOW_WIDTH}x{self.COMPACT_HEIGHT}")

    def _log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}\n"

        def _append():
            self.log_box.configure(state=tk.NORMAL)
            self.log_box.insert(tk.END, line)
            # Keep memory tiny on low-end PCs (cap at 200 lines)
            line_count = int(self.log_box.index("end-1c").split(".")[0])
            if line_count > 200:
                self.log_box.delete("1.0", f"{line_count - 200}.0")
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)

        self.root.after(0, _append)

    def _set_action_state(self, state: str) -> None:
        """Cleanly toggle visible buttons for idle / running / paused / done states."""
        self.btn_secondary.pack_forget()
        self.btn_open_folder.pack_forget()

        if state == "idle":
            self.btn_primary.set_style("Download & Extract", bg=C_ACCENT, hover_bg=C_ACCENT_HOVER, fg="#FFFFFF")
            self.btn_primary.set_enabled(True)
            self._secondary_mode = "hidden"
        elif state == "running":
            self.btn_primary.set_style("Pause", bg=C_SURFACE, hover_bg=C_HOVER, fg=C_WARN)
            self.btn_primary.set_enabled(True)
            self.btn_secondary.set_style("Cancel", bg=C_SURFACE, hover_bg=C_HOVER, fg=C_DANGER)
            self.btn_secondary.pack(side=tk.LEFT, padx=(10, 0))
            self._secondary_mode = "cancel"
        elif state == "paused":
            self.btn_primary.set_style("Resume", bg=C_ACCENT, hover_bg=C_ACCENT_HOVER, fg="#FFFFFF")
            self.btn_primary.set_enabled(True)
            self.btn_secondary.set_style("Cancel", bg=C_SURFACE, hover_bg=C_HOVER, fg=C_DANGER)
            self.btn_secondary.pack(side=tk.LEFT, padx=(10, 0))
            self._secondary_mode = "cancel"
        elif state == "done":
            self.btn_primary.set_style("Download & Extract", bg=C_ACCENT, hover_bg=C_ACCENT_HOVER, fg="#FFFFFF")
            self.btn_primary.set_enabled(True)
            self.btn_open_folder.pack(side=tk.LEFT, padx=(10, 0))
            self.btn_secondary.set_style("Verify", bg=C_SURFACE, hover_bg=C_HOVER, fg=C_TEXT)
            self.btn_secondary.pack(side=tk.LEFT, padx=(10, 0))
            self._secondary_mode = "verify"

    def _on_browse_dest(self) -> None:
        folder = filedialog.askdirectory(title="Select Destination Folder")
        if folder:
            self.dest_var.set(folder)

    def _on_pick_local_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Local Archive",
            filetypes=[("Archives", "*.zip *.rar *.7z"), ("All Files", "*.*")],
        )
        if path:
            self._set_url(path)
            self._run_preflight(force=True)

    def _on_open_folder(self) -> None:
        dest = self.dest_var.get().strip()
        if dest and Path(dest).exists():
            os.startfile(dest)

    def _on_primary_action(self) -> None:
        if self._is_running:
            if self.controller.is_paused:
                self.controller.resume()
                self._set_action_state("running")
                self.lbl_status.configure(text="Resuming...", fg=C_TEXT)
                self._log("Resumed.")
            else:
                self.controller.pause()
                self._set_action_state("paused")
                self.lbl_status.configure(text="Paused", fg=C_WARN)
                self.progress.set_progress(self.progress._percent, color=C_WARN)
                self._log("Paused.")
            return

        self._start_extraction()

    def _on_secondary_action(self) -> None:
        if self._secondary_mode == "cancel":
            self.controller.cancel()
            self.lbl_status.configure(text="Cancelling...", fg=C_DANGER)
            self._log("Cancelling...")
        elif self._secondary_mode == "verify":
            self._on_verify()

    def _start_extraction(self) -> None:
        source = self._get_url()
        dest = self.dest_var.get().strip()
        if not source:
            messagebox.showinfo("LiveExtract", "Paste a download link first.")
            return
        if not dest:
            messagebox.showinfo("LiveExtract", "Choose a destination folder first.")
            return

        chunk_map = {
            "64 KB": 65536,
            "128 KB": 131072,
            "256 KB": 262144,
            "512 KB": 524288,
            "1 MB": 1048576,
        }
        chunk_size = chunk_map.get(self.chunk_var.get(), 262144)
        workers = max(1, min(16, self.workers_var.get()))

        self._is_running = True
        self.controller.reset()
        self._set_action_state("running")
        self.progress.set_progress(0.0, color=C_ACCENT)
        self.lbl_status.configure(text="Connecting...", fg=C_TEXT)
        self.lbl_speed.configure(text="")
        self._log(f"Starting stream ({workers} connections, {self.chunk_var.get()} buffer)...")

        client = StreamClient(
            chunk_size=chunk_size,
            max_workers=workers,
            verify_crc=self.verify_var.get(),
            resume=self.resume_var.get(),
            controller=self.controller,
        )

        last_ui = [0.0]

        def progress_cb(p: StreamProgress):
            now = time.time()
            if (now - last_ui[0]) < 0.1 and p.percent < 100.0:
                return
            last_ui[0] = now

            def _update():
                if self.controller.is_paused:
                    return
                self.progress.set_progress(p.percent, color=C_ACCENT)
                self.lbl_status.configure(text=f"Extracting  {p.percent:.1f}%", fg=C_TEXT)
                net_mb = (p.net_bytes_per_second or p.bytes_per_second) / (1024 * 1024)
                done_str = format_bytes(p.uncompressed_bytes_done).strip()
                tot_str = format_bytes(p.total_uncompressed_bytes).strip()
                self.lbl_speed.configure(text=f"{net_mb:.2f} MB/s  ·  {done_str} / {tot_str}")

            self.root.after(0, _update)

        def _run():
            try:
                res = client.extract_from_url(source, dest, progress_cb=progress_cb)
                status = res.get("status")

                def _finish():
                    self._is_running = False
                    if status == "completed":
                        self.progress.set_progress(100.0, color=C_SUCCESS)
                        self.lbl_status.configure(text=f"Done  ·  {res['total_files']} files extracted", fg=C_SUCCESS)
                        self.lbl_speed.configure(text=f"{format_bytes(res['uncompressed_bytes']).strip()}  ·  0 B temp archive")
                        self._set_action_state("done")
                        self._log(f"Completed: {res['total_files']} files ({format_bytes(res['uncompressed_bytes']).strip()})")
                    else:
                        self.lbl_status.configure(text="Cancelled", fg=C_MUTED)
                        self.lbl_speed.configure(text="")
                        self._set_action_state("idle")
                        self._log("Cancelled by user.")

                self.root.after(0, _finish)
            except Exception as ex:
                def _fail():
                    self._is_running = False
                    self.progress.set_progress(self.progress._percent, color=C_DANGER)
                    self.lbl_status.configure(text="Failed", fg=C_DANGER)
                    self._set_action_state("idle")
                    self._log(f"Error: {ex}")
                    messagebox.showerror("Extraction Error", str(ex))

                self.root.after(0, _fail)

        self._worker_thread = threading.Thread(target=_run, daemon=True)
        self._worker_thread.start()

    def _on_verify(self) -> None:
        dest = self.dest_var.get().strip()
        if not dest or not Path(dest).exists():
            messagebox.showinfo("LiveExtract", "Destination folder does not exist yet.")
            return

        self.lbl_status.configure(text="Verifying files...", fg=C_MUTED)
        self._log("Running CRC-32 integrity check...")

        def _bg_verify():
            report = verify_destination(Path(dest), archive_source=self._get_url() or None)

            def _show():
                if report.total_expected == 0:
                    self.lbl_status.configure(text="No manifest found", fg=C_WARN)
                    messagebox.showinfo("Verify", "No extraction manifest (.extract_state.json) found in this folder.")
                    return

                if report.is_valid:
                    self.lbl_status.configure(text=f"Verified  ·  {report.total_expected} files intact", fg=C_SUCCESS)
                    self._log(f"Verification passed ({report.total_expected} files intact).")
                    messagebox.showinfo(
                        "Verified",
                        f"All {report.total_expected} files are intact ({format_bytes(report.total_verified_bytes).strip()}).",
                    )
                else:
                    self.lbl_status.configure(
                        text=f"Missing: {len(report.missing_files)}, Corrupted: {len(report.corrupted_files)}",
                        fg=C_DANGER,
                    )
                    self._log(report.failure_summary)
                    if messagebox.askyesno(
                        "Integrity Check Failed",
                        f"{report.failure_summary}\n\nRe-download missing/corrupted files now?",
                    ):
                        self._start_extraction()

            self.root.after(0, _show)

        threading.Thread(target=_bg_verify, daemon=True).start()


def launch_gui() -> None:
    """Launch LiveExtract desktop window."""
    root = tk.Tk()
    LiveExtractApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
