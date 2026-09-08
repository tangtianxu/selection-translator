"""A small Windows selection/clipboard translation utility.

The app intentionally uses only the Python standard library so the source can
also be launched directly.  A packaged executable is built with PyInstaller.
"""

from __future__ import annotations

import ctypes
import faulthandler
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import pystray
from matplotlib.mathtext import math_to_image
from PIL import Image, ImageDraw, ImageFont, ImageTk


APP_NAME = "划词翻译（ttx的vibe coding小工具）"
LOGGER = logging.getLogger("SelectionTranslator")
FAULT_LOG_HANDLE = None
HOTKEY_ID = 0xB071
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_T = 0x54
VK_Q = 0x51
VK_D = 0x44
VK_C = 0x43
KEYEVENTF_KEYUP = 0x0002
MAX_TEXT_LENGTH = 5000
MAX_HISTORY_ITEMS = 200
GMEM_MOVEABLE = 0x0002
SAFE_STANDARD_CLIPBOARD_FORMATS = {1, 7, 8, 13, 15, 16, 17}
SAFE_REGISTERED_CLIPBOARD_FORMATS = {
    "HTML Format",
    "Rich Text Format",
    "Rich Text Format Without Objects",
    "CSV",
    "text/html",
    "text/rtf",
    "UniformResourceLocator",
    "UniformResourceLocatorW",
    "PNG",
    "image/png",
}

MATH_PATTERN = re.compile(
    r"(\\begin\{[^{}]+\}.*?\\end\{[^{}]+\}|\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|(?<!\$)\$(?!\$)[^$\n]+?\$)",
    re.DOTALL,
)
MATH_FONT_SHORTHAND_PATTERN = re.compile(
    r"\\(?P<command>mathbf|mathcal|mathrm|mathit|mathsf|mathtt|mathbb|mathfrak|mathscr)"
    r"\s+(?P<argument>\\[A-Za-z]+|[A-Za-z0-9])"
)

LANGUAGES = {
    "简体中文": "zh-CN",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "法语": "fr",
    "德语": "de",
    "西班牙语": "es",
    "俄语": "ru",
}

KEY_CODES = {
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{chr(code): code for code in range(ord("0"), ord("9") + 1)},
    **{f"F{number}": 0x6F + number for number in range(1, 13)},
}


def parse_hotkey(name: str) -> tuple[int, int] | None:
    parts = name.split("+")
    if len(parts) < 2:
        return None
    key = KEY_CODES.get(parts[-1].upper())
    if key is None:
        return None
    modifier_map = {"Ctrl": MOD_CONTROL, "Alt": MOD_ALT, "Shift": MOD_SHIFT, "Win": MOD_WIN}
    modifiers = 0
    for part in parts[:-1]:
        if part not in modifier_map:
            return None
        modifiers |= modifier_map[part]
    if not modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        return None
    return modifiers, key


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


@dataclass
class ClipboardBackup:
    formats: list[tuple[int, bytes]]
    skipped_formats: int = 0


def _open_clipboard_with_retry(attempts: int = 8) -> bool:
    user32 = ctypes.windll.user32
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.025)
    return False


def capture_clipboard() -> ClipboardBackup | None:
    """Snapshot HGLOBAL-backed clipboard formats using only Win32 APIs."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    if not _open_clipboard_with_retry():
        return None
    captured: list[tuple[int, bytes]] = []
    skipped = 0
    try:
        clipboard_format = 0
        while True:
            clipboard_format = user32.EnumClipboardFormats(clipboard_format)
            if not clipboard_format:
                break
            safe_format = clipboard_format in SAFE_STANDARD_CLIPBOARD_FORMATS
            if clipboard_format >= 0xC000:
                name_buffer = ctypes.create_unicode_buffer(256)
                if user32.GetClipboardFormatNameW(clipboard_format, name_buffer, len(name_buffer)):
                    safe_format = name_buffer.value in SAFE_REGISTERED_CLIPBOARD_FORMATS
            if not safe_format:
                skipped += 1
                continue
            handle = user32.GetClipboardData(clipboard_format)
            size = kernel32.GlobalSize(handle) if handle else 0
            if not handle or not size:
                skipped += 1
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                skipped += 1
                continue
            try:
                captured.append((clipboard_format, ctypes.string_at(pointer, size)))
            finally:
                kernel32.GlobalUnlock(handle)
        return ClipboardBackup(captured, skipped)
    finally:
        user32.CloseClipboard()


def restore_clipboard(backup: ClipboardBackup) -> bool:
    """Restore captured formats; Windows owns each allocated block on success."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    if not _open_clipboard_with_retry():
        return False
    all_restored = True
    try:
        if not user32.EmptyClipboard():
            return False
        for clipboard_format, data in backup.formats:
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                all_restored = False
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                kernel32.GlobalFree(handle)
                all_restored = False
                continue
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(clipboard_format, handle):
                kernel32.GlobalFree(handle)
                all_restored = False
        return all_restored
    finally:
        user32.CloseClipboard()


def normalize_latex_text(text: str) -> str:
    """Normalize common Markdown/JSON escaping around LaTeX copied as text."""
    return (
        text.replace(r"\\(", r"\(")
        .replace(r"\\)", r"\)")
        .replace(r"\\[", r"\[")
        .replace(r"\\]", r"\]")
    )


def normalize_math_body_for_renderer(body: str) -> str:
    """Adapt valid TeX shorthand to the stricter local MathText parser."""
    normalized = body.replace(r"\_", "_")
    return MATH_FONT_SHORTHAND_PATTERN.sub(
        lambda match: f"\\{match.group('command')}{{{match.group('argument')}}}",
        normalized,
    ).strip()


def split_math_segments(text: str) -> list[tuple[bool, str, bool]]:
    """Return (is_math, content, is_display) segments."""
    normalized = normalize_latex_text(text)
    segments: list[tuple[bool, str, bool]] = []
    position = 0
    for match in MATH_PATTERN.finditer(normalized):
        if match.start() > position:
            segments.append((False, normalized[position : match.start()], False))
        raw = match.group(0)
        display = raw.startswith(("$$", r"\[", r"\begin"))
        if raw.startswith("$$"):
            body = raw[2:-2]
        elif raw.startswith("$"):
            body = raw[1:-1]
        elif raw.startswith(r"\(") or raw.startswith(r"\["):
            body = raw[2:-2]
        else:
            body = raw
        # Markdown copies often escape subscripts, while MathText also requires
        # braces around font commands that full TeX accepts as shorthand.
        body = normalize_math_body_for_renderer(body)
        segments.append((True, body, display))
        position = match.end()
    if position < len(normalized):
        segments.append((False, normalized[position:], False))
    return segments or [(False, normalized, False)]


def protect_math_for_translation(text: str) -> tuple[str, list[str]]:
    """Replace formulas with stable tokens before sending prose to Google."""
    formulas: list[str] = []
    normalized = normalize_latex_text(text)

    def replace(match: re.Match) -> str:
        formulas.append(match.group(0))
        return f"ZXQMATHTOKEN{len(formulas) - 1}QXZ"

    return MATH_PATTERN.sub(replace, normalized), formulas


def restore_protected_math(text: str, formulas: list[str]) -> str:
    restored = text
    for index, formula in enumerate(formulas):
        token = f"ZXQMATHTOKEN{index}QXZ"
        restored = re.sub(re.escape(token), lambda _match, value=formula: value, restored, flags=re.IGNORECASE)
    return restored


def app_data_folder() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    folder = base / "SelectionTranslator"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def app_data_path() -> Path:
    return app_data_folder() / "settings.json"


def history_data_path() -> Path:
    return app_data_path().with_name("history.json")


def log_data_path() -> Path:
    return app_data_folder() / "app.log"


def configure_logging() -> None:
    global FAULT_LOG_HANDLE
    log_path = log_data_path()
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    LOGGER.propagate = False
    try:
        FAULT_LOG_HANDLE = open(app_data_folder() / "fatal.log", "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=FAULT_LOG_HANDLE, all_threads=True)
    except OSError:
        FAULT_LOG_HANDLE = None

    def log_unhandled(exc_type, exc_value, exc_traceback) -> None:
        LOGGER.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = log_unhandled

    def log_thread_exception(args) -> None:
        LOGGER.critical(
            "Unhandled thread exception in %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = log_thread_exception


def translate_google(text: str, target: str) -> tuple[str, str]:
    """Translate with Google's public web endpoint (no API key required)."""
    payload = urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single",
        data=payload,
        headers={
            "User-Agent": "Mozilla/5.0 SelectionTranslator/1.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))
    translated = "".join(segment[0] for segment in data[0] if segment and segment[0])
    detected = data[2] if len(data) > 2 and data[2] else "auto"
    if not translated:
        raise ValueError("翻译服务返回了空结果")
    return translated, detected


class SelectionTranslator:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.user32 = ctypes.windll.user32
        self.results: queue.Queue[tuple[int, str, str, str, str, str]] = queue.Queue()
        self.hotkey_events: queue.Queue[tuple[int, str]] = queue.Queue()
        self.tray_events: queue.Queue[str] = queue.Queue()
        self.request_id = 0
        self.copy_sequence = 0
        self.selection_copy_in_progress = False
        self.pending_clipboard_backup: ClipboardBackup | None = None
        self.last_clipboard = self._read_clipboard()
        self.settings = self._load_settings()
        self.history = self._load_history()
        self.history_panel: ttk.Frame | None = None
        self.history_panel_visible = False
        self.formula_panel: ttk.Frame | None = None
        self.formula_panel_visible = False
        self.formula_images: list[ImageTk.PhotoImage] = []
        self.output_formula_images: list[ImageTk.PhotoImage] = []
        self.history_formula_images: list[ImageTk.PhotoImage] = []
        self.history_formula_rendered = False
        self.current_translation_text = ""
        self.compact_geometry: tuple[int, int, int, int] | None = None
        self.full_mode_height = 500
        self.hotkey_registered = False
        self.hotkey_thread_id = 0
        self.hotkey_thread: threading.Thread | None = None
        self.hotkey_generation = 0
        self.tray_icon: pystray.Icon | None = None
        self.tray_hint_shown = False

        self.root.title(APP_NAME)
        self.root.geometry("680x500")
        self.root.minsize(540, 390)
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.root.report_callback_exception = self._report_callback_exception
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        self.target_name = tk.StringVar(value=self.settings.get("target", "简体中文"))
        self.hotkey_name = tk.StringVar(value=self.settings.get("hotkey", "Ctrl+Shift+T"))
        if parse_hotkey(self.hotkey_name.get()) is None:
            self.hotkey_name.set("Ctrl+Shift+T")
        self.zh_en_mode = tk.BooleanVar(value=self.settings.get("zh_en_mode", False))
        self.watch_clipboard = tk.BooleanVar(value=self.settings.get("watch_clipboard", False))
        self.always_on_top = tk.BooleanVar(value=self.settings.get("always_on_top", False))
        self.source_collapsed = tk.BooleanVar(value=self.settings.get("source_collapsed", False))
        self.translation_only = tk.BooleanVar(value=self.settings.get("translation_only", False))
        self.auto_math_render = tk.BooleanVar(value=self.settings.get("auto_math_render", False))
        self.source_toggle_text = tk.StringVar(value="展开" if self.source_collapsed.get() else "收起")
        self.status = tk.StringVar(value="就绪")
        self.detected = tk.StringVar(value="")

        self._build_ui()
        self._update_language_control()
        self._apply_display_mode(initial=True)
        self._apply_topmost_setting()
        self._register_hotkey()
        self._start_tray_icon()
        self._poll_hotkey_events()
        self._poll_tray_events()
        self._poll_clipboard()
        self._poll_results()

    def _build_ui(self) -> None:
        self.root.configure(bg="#f5f6f8")
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        self.shell = ttk.Panedwindow(self.root, orient="horizontal")
        self.shell.pack(fill="both", expand=True)
        frame = ttk.Frame(self.shell, padding=16)
        self.content_frame = frame
        self.main_frame = frame
        self.shell.add(frame, weight=0)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.rowconfigure(5, weight=1)

        header = ttk.Frame(frame)
        self.header_frame = header
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(4, weight=1)
        ttk.Label(header, text="源语言：自动检测").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(header, text="目标语言：").grid(row=0, column=2, sticky="e", padx=(18, 0))
        self.language_box = ttk.Combobox(
            header,
            textvariable=self.target_name,
            values=list(LANGUAGES),
            width=12,
            state="readonly",
        )
        self.language_box.grid(row=0, column=3, sticky="w")
        self.language_box.bind("<<ComboboxSelected>>", lambda _event: self._save_settings())
        ttk.Checkbutton(
            header,
            text="中英互翻",
            variable=self.zh_en_mode,
            command=self._translation_mode_changed,
        ).grid(row=0, column=4, sticky="e", padx=(18, 0))

        ttk.Label(header, text="全局快捷键：").grid(row=1, column=0, sticky="w", pady=(10, 0))
        hotkey_box = ttk.Entry(
            header,
            textvariable=self.hotkey_name,
            width=15,
            state="readonly",
        )
        hotkey_box.grid(row=1, column=1, sticky="w", pady=(10, 0))
        ttk.Button(header, text="设置…", command=self._open_hotkey_capture).grid(
            row=1, column=2, sticky="w", padx=(6, 0), pady=(10, 0)
        )
        ttk.Checkbutton(
            header,
            text="窗口保持置顶",
            variable=self.always_on_top,
            command=self._topmost_changed,
        ).grid(row=1, column=3, sticky="e", padx=(18, 0), pady=(10, 0))
        ttk.Checkbutton(
            header,
            text="复制后自动翻译",
            variable=self.watch_clipboard,
            command=self._clipboard_mode_changed,
        ).grid(row=1, column=4, sticky="e", pady=(10, 0))

        source_header = ttk.Frame(frame)
        self.source_header = source_header
        source_header.grid(row=1, column=0, sticky="ew")
        source_header.columnconfigure(0, weight=1)
        ttk.Label(source_header, text="原文").grid(row=0, column=0, sticky="w")
        ttk.Button(source_header, textvariable=self.source_toggle_text, command=self._toggle_source).grid(
            row=0, column=1, sticky="e"
        )
        self.source = tk.Text(
            frame,
            height=7,
            wrap="word",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=8,
            undo=True,
        )
        self.source.grid(row=2, column=0, sticky="nsew", pady=(5, 10))
        self.source.bind("<Control-Return>", lambda _event: self.translate())

        buttons = ttk.Frame(frame)
        self.action_buttons = buttons
        buttons.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(buttons, text="翻译  Ctrl+Enter", style="Primary.TButton", command=self.translate).pack(
            side="left"
        )
        ttk.Button(buttons, text="复制译文", command=self.copy_translation).pack(side="left", padx=8)
        ttk.Button(buttons, text="清空", command=self.clear).pack(side="left")
        self.history_button_text = tk.StringVar(value="历史记录 ▶")
        ttk.Button(buttons, textvariable=self.history_button_text, command=self.show_history).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(buttons, text="退出", command=self._on_close).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="日志", command=self._open_log).pack(side="left", padx=(8, 0))
        ttk.Label(buttons, textvariable=self.detected, foreground="#667085").pack(side="right")

        translation_header = ttk.Frame(frame)
        self.translation_header = translation_header
        translation_header.grid(row=4, column=0, sticky="ew")
        translation_header.columnconfigure(0, weight=1)
        ttk.Label(translation_header, text="译文").grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            translation_header,
            text="自动公式",
            variable=self.auto_math_render,
            command=self._auto_math_changed,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.formula_button_text = tk.StringVar(value="公式转换 ▶")
        ttk.Button(translation_header, textvariable=self.formula_button_text, command=self.show_formula_preview).grid(
            row=0, column=2, sticky="e", padx=(8, 0)
        )
        ttk.Checkbutton(
            translation_header,
            text="仅译文",
            variable=self.translation_only,
            command=self._translation_only_changed,
        ).grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.output = tk.Text(
            frame,
            height=7,
            wrap="word",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=8,
            background="#fbfcff",
        )
        self.output.grid(row=5, column=0, sticky="nsew", pady=(5, 10))
        self.output.configure(state="disabled")

        footer = ttk.Frame(frame)
        footer.grid(row=6, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status, foreground="#475467").grid(row=0, column=0, sticky="w")
        self.hotkey_hint = ttk.Label(footer, foreground="#475467")
        self.hotkey_hint.grid(row=0, column=1, sticky="e")
        self._update_hotkey_hint()

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        LOGGER.error("Tk callback exception", exc_info=(exc_type, exc_value, exc_traceback))
        self.status.set("程序遇到错误但仍在运行；详情已写入日志")

    def _open_log(self) -> None:
        try:
            path = log_data_path()
            path.touch(exist_ok=True)
            os.startfile(path)
            self.status.set("已打开日志文件")
        except OSError as exc:
            LOGGER.exception("Unable to open log file")
            self.status.set(f"无法打开日志：{exc}")

    def _toggle_source(self) -> None:
        self.source_collapsed.set(not self.source_collapsed.get())
        self._apply_display_mode()
        self._save_settings()

    def _translation_only_changed(self) -> None:
        if self.translation_only.get() and self.history_panel_visible:
            self._close_history_panel()
        if self.translation_only.get() and self.formula_panel_visible:
            self._close_formula_panel()
        self._apply_display_mode()
        self._save_settings()
        self.status.set("已开启仅译文模式" if self.translation_only.get() else "已恢复完整界面")

    def _auto_math_changed(self) -> None:
        self._save_settings()
        rendered = failed = 0
        if self.current_translation_text:
            rendered, failed = self._set_output(self.current_translation_text)
        if self.history_panel_visible:
            item = self._selected_history_record()
            if item:
                translation = item.get("translation", "")
                self.history_formula_rendered = self.auto_math_render.get() and self._has_math(translation)
                self._set_history_preview(item.get("source", ""), translation)
        if self.auto_math_render.get():
            if failed:
                self.status.set(f"自动公式：成功渲染 {rendered} 个，失败 {failed} 个（已保留源码）")
            elif rendered:
                self.status.set(f"自动公式：已成功渲染 {rendered} 个公式")
            else:
                self.status.set("已开启自动公式：检测到 LaTeX 时直接在译文区域渲染")
        else:
            self.status.set("已关闭自动公式；可点击“公式转换”按需查看")

    def _apply_display_mode(self, initial: bool = False) -> None:
        only_translation = self.translation_only.get()
        if only_translation:
            self.header_frame.grid_remove()
            self.source_header.grid_remove()
            self.source.grid_remove()
            self.action_buttons.grid_remove()
            self.content_frame.rowconfigure(2, weight=0)
            self.source_toggle_text.set("展开")
        else:
            self.header_frame.grid()
            self.source_header.grid()
            self.action_buttons.grid()
            if self.source_collapsed.get():
                self.source.grid_remove()
                self.content_frame.rowconfigure(2, weight=0)
                self.source_toggle_text.set("展开")
            else:
                self.source.grid()
                self.content_frame.rowconfigure(2, weight=1)
                self.source_toggle_text.set("收起")

        self.root.update_idletasks()
        if self.root.state() not in ("normal", "zoomed"):
            return
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if only_translation:
            if not initial and height > 340:
                self.full_mode_height = height
            self.root.minsize(540, 240)
            self.root.geometry(f"{width}x300+{x}+{y}")
        else:
            self.root.minsize(540, 390)
            if not initial and height < 340:
                self.root.geometry(f"{width}x{max(390, self.full_mode_height)}+{x}+{y}")

    def _register_hotkey(self) -> None:
        self.hotkey_generation += 1
        generation = self.hotkey_generation
        parsed = parse_hotkey(self.hotkey_name.get()) or parse_hotkey("Ctrl+Shift+T")
        assert parsed is not None
        modifiers, key = parsed
        self.hotkey_thread = threading.Thread(
            target=self._hotkey_message_loop,
            args=(generation, modifiers, key),
            daemon=True,
        )
        self.hotkey_thread.start()

    def _hotkey_message_loop(self, generation: int, modifiers: int, key: int) -> None:
        """Own the Windows hotkey queue on a dedicated thread.

        Tk has its own Windows message pump. Registering a thread hotkey on the
        Tk thread lets Tk consume WM_HOTKEY before Python sees it, so this
        listener deliberately owns a separate queue.
        """
        thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self.hotkey_thread_id = thread_id
        registered = bool(self.user32.RegisterHotKey(None, HOTKEY_ID, modifiers | MOD_NOREPEAT, key))
        self.hotkey_events.put((generation, "registered" if registered else "failed"))
        if not registered:
            return
        message = MSG()
        while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.wParam == HOTKEY_ID:
                self.hotkey_events.put((generation, "pressed"))
        self.user32.UnregisterHotKey(None, HOTKEY_ID)

    def _poll_hotkey_events(self) -> None:
        try:
            while True:
                generation, event = self.hotkey_events.get_nowait()
                if generation != self.hotkey_generation:
                    continue
                if event == "failed":
                    self.hotkey_registered = False
                    self.status.set("快捷键注册失败（可能被其他软件占用），仍可使用剪贴板模式")
                elif event == "registered":
                    self.hotkey_registered = True
                    self.status.set(f"快捷键已改为 {self.hotkey_name.get()}")
                elif event == "pressed":
                    # Let the user release Ctrl+Shift before synthesizing Ctrl+C.
                    self.root.after(180, self._copy_current_selection)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_hotkey_events)

    def _stop_hotkey_listener(self) -> None:
        old_thread_id = self.hotkey_thread_id
        old_thread = self.hotkey_thread
        self.hotkey_generation += 1  # Ignore events from the old listener immediately.
        if old_thread_id:
            self.user32.PostThreadMessageW(old_thread_id, WM_QUIT, 0, 0)
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=0.25)
        self.hotkey_thread_id = 0
        self.hotkey_registered = False

    def _apply_new_hotkey(self, name: str) -> None:
        self.hotkey_name.set(name)
        self._save_settings()
        self._update_hotkey_hint()
        self._register_hotkey()

    def _open_hotkey_capture(self) -> None:
        self._stop_hotkey_listener()
        dialog = tk.Toplevel(self.root)
        dialog.title("设置全局快捷键")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="请直接按下新的快捷键组合", font=("Microsoft YaHei UI", 11, "bold")).pack()
        capture_status = tk.StringVar(value="例如：Ctrl+Shift+T、Alt+Q、Win+F2")
        ttk.Label(body, textvariable=capture_status, foreground="#667085").pack(pady=(10, 4))
        ttk.Label(body, text="组合中必须包含 Ctrl、Alt 或 Win；按 Esc 取消").pack()

        closed = False

        def cancel() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            dialog.grab_release()
            dialog.destroy()
            self._register_hotkey()
            self.status.set("已取消修改快捷键")

        def capture(event) -> str:
            nonlocal closed
            key_name = event.keysym.upper()
            if key_name == "ESCAPE":
                cancel()
                return "break"
            if key_name in {
                "CONTROL_L", "CONTROL_R", "ALT_L", "ALT_R", "SHIFT_L", "SHIFT_R", "WIN_L", "WIN_R",
                "SUPER_L", "SUPER_R",
            }:
                capture_status.set("请在按住修饰键的同时，再按一个字母、数字或 F1–F12")
                return "break"
            if key_name not in KEY_CODES:
                capture_status.set("该按键暂不支持，请使用字母、数字或 F1–F12")
                return "break"

            pressed = lambda vk: bool(self.user32.GetAsyncKeyState(vk) & 0x8000)
            ctrl = pressed(0x11)
            alt = pressed(0x12)
            shift = pressed(0x10)
            win = pressed(0x5B) or pressed(0x5C)
            if not (ctrl or alt or win):
                capture_status.set("为避免影响正常输入，组合中必须包含 Ctrl、Alt 或 Win")
                return "break"

            parts: list[str] = []
            if ctrl:
                parts.append("Ctrl")
            if alt:
                parts.append("Alt")
            if shift:
                parts.append("Shift")
            if win:
                parts.append("Win")
            parts.append(key_name)
            new_name = "+".join(parts)
            closed = True
            dialog.grab_release()
            dialog.destroy()
            self._apply_new_hotkey(new_name)
            self.status.set(f"快捷键已设置为 {new_name}")
            return "break"

        dialog.bind("<KeyPress>", capture)
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.focus_force()

    def _update_hotkey_hint(self) -> None:
        self.hotkey_hint.configure(text=f"在任意软件选中文字后按 {self.hotkey_name.get()}")

    def _copy_current_selection(self) -> None:
        if self.selection_copy_in_progress:
            return
        self.selection_copy_in_progress = True
        self.pending_clipboard_backup = None
        if not self.watch_clipboard.get():
            self.pending_clipboard_backup = capture_clipboard()
            if self.pending_clipboard_backup is None:
                self.selection_copy_in_progress = False
                self._show_window()
                self.status.set("剪贴板正被其他程序占用；为避免覆盖原内容，本次翻译已取消")
                LOGGER.warning("Selection capture cancelled: clipboard unavailable")
                return
            if not self.pending_clipboard_backup.formats and self.pending_clipboard_backup.skipped_formats:
                self.pending_clipboard_backup = None
                self.selection_copy_in_progress = False
                self._show_window()
                self.status.set("剪贴板只有无法安全备份的特殊格式；为避免覆盖，本次翻译已取消")
                LOGGER.warning("Selection capture cancelled: clipboard contains only unsupported formats")
                return
        self.copy_sequence = self.user32.GetClipboardSequenceNumber()
        self.user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
        self.user32.keybd_event(VK_C, 0, 0, 0)
        self.user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
        self.user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)
        self._wait_for_selection_copy(0)

    def _wait_for_selection_copy(self, attempt: int) -> None:
        sequence = self.user32.GetClipboardSequenceNumber()
        if sequence != self.copy_sequence:
            text = self._read_clipboard().strip()
            if text:
                restore_note = self._finish_selection_copy()
                self._set_source(text)
                self._show_window()
                self.translate(restore_note)
                return
        if attempt < 8:
            self.root.after(70, lambda: self._wait_for_selection_copy(attempt + 1))
        else:
            restore_note = self._finish_selection_copy()
            self._show_window()
            message = "没有复制到文字；请确认已选中文字，或先按 Ctrl+C"
            self.status.set(message + restore_note)

    def _finish_selection_copy(self) -> str:
        backup = self.pending_clipboard_backup
        self.pending_clipboard_backup = None
        self.selection_copy_in_progress = False
        if backup is None:
            self.last_clipboard = self._read_clipboard()
            return ""
        restored = restore_clipboard(backup)
        self.last_clipboard = self._read_clipboard()
        if not restored:
            LOGGER.error("Clipboard restoration failed")
            return "；原剪贴板恢复失败"
        if backup.skipped_formats:
            LOGGER.warning("Clipboard restored without %s unsupported private formats", backup.skipped_formats)
            return "；原剪贴板已恢复（部分特殊格式无法保留）"
        return "；原剪贴板已恢复"

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        if not self.always_on_top.get():
            self.root.after(700, self._release_transient_topmost)
        self.root.focus_force()

    def _release_transient_topmost(self) -> None:
        if not self.always_on_top.get():
            self.root.attributes("-topmost", False)

    def _topmost_changed(self) -> None:
        self._apply_topmost_setting()
        self._save_settings()
        self.status.set("窗口已保持置顶" if self.always_on_top.get() else "已取消窗口保持置顶")

    def _apply_topmost_setting(self) -> None:
        self.root.attributes("-topmost", self.always_on_top.get())

    def _clipboard_mode_changed(self) -> None:
        self.last_clipboard = self._read_clipboard()
        self._save_settings()
        self.status.set("已开启剪贴板自动翻译" if self.watch_clipboard.get() else "已关闭剪贴板自动翻译")

    def _translation_mode_changed(self) -> None:
        self._update_language_control()
        self._save_settings()
        if self.zh_en_mode.get():
            self.status.set("已开启中英互翻：中文→英文，英文→中文")
        else:
            self.status.set("已切换为手动选择目标语言")

    def _update_language_control(self) -> None:
        self.language_box.configure(state="disabled" if self.zh_en_mode.get() else "readonly")

    def _poll_clipboard(self) -> None:
        if self.watch_clipboard.get():
            text = self._read_clipboard().strip()
            if text and text != self.last_clipboard:
                self.last_clipboard = text
                # Respect a deliberate minimize/hide action. Consume the new
                # clipboard value so it will not pop up later on restore.
                if self.root.state() not in ("iconic", "withdrawn"):
                    self._set_source(text)
                    self._show_window()
                    self.translate()
        self.root.after(550, self._poll_clipboard)

    def _read_clipboard(self) -> str:
        try:
            return self.root.clipboard_get()
        except (tk.TclError, UnicodeError):
            return ""

    def _set_source(self, text: str) -> None:
        self.source.delete("1.0", "end")
        self.source.insert("1.0", text)

    def _set_output(self, text: str) -> tuple[int, int]:
        self.current_translation_text = text
        if self.auto_math_render.get() and self._has_math(text):
            return self._render_document(self.output, text, self.output_formula_images)
        self.output_formula_images.clear()
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")
        return 0, 0

    @staticmethod
    def _has_math(text: str) -> bool:
        return any(is_math for is_math, _content, _display in split_math_segments(text))

    @staticmethod
    def _formula_image(body: str) -> ImageTk.PhotoImage:
        buffer = BytesIO()
        math_to_image(f"${body}$", buffer, dpi=105, format="png", color="#111827")
        buffer.seek(0)
        image = Image.open(buffer).convert("RGBA")
        if image.width <= 1 or image.height <= 1:
            raise ValueError("公式渲染结果为空")
        return ImageTk.PhotoImage(image)

    def _render_document(
        self, widget: tk.Text, text: str, image_store: list[ImageTk.PhotoImage]
    ) -> tuple[int, int]:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        image_store.clear()
        rendered = 0
        failed = 0
        widget.tag_configure("formula_error", foreground="#b42318")
        for is_math, content, display in split_math_segments(text):
            if not is_math:
                widget.insert("end", content)
                continue
            try:
                photo = self._formula_image(content)
                image_store.append(photo)
                if display and widget.index("end-1c") != "1.0":
                    widget.insert("end", "\n")
                widget.image_create("end", image=photo, align="center", padx=3, pady=2)
                if display:
                    widget.insert("end", "\n")
                rendered += 1
            except Exception as exc:
                LOGGER.info("Local formula rendering failed for %r: %s", content, exc)
                delimiter_left, delimiter_right = ("\\[", "\\]") if display else ("\\(", "\\)")
                widget.insert("end", delimiter_left + content + delimiter_right, "formula_error")
                failed += 1
        widget.configure(state="disabled")
        return rendered, failed

    def translate(self, completion_note: str = "") -> None:
        text = self.source.get("1.0", "end-1c").strip()
        if not text:
            self.status.set("请先输入、复制或选中要翻译的文字")
            return
        if len(text) > MAX_TEXT_LENGTH:
            self.status.set(f"文本过长：当前 {len(text)} 字符，单次最多 {MAX_TEXT_LENGTH} 字符")
            return

        target = LANGUAGES.get(self.target_name.get(), "zh-CN")
        mutual = self.zh_en_mode.get()
        self.request_id += 1
        current_id = self.request_id
        self.status.set("正在翻译…")
        self.detected.set("")
        threading.Thread(
            target=self._translate_worker,
            args=(current_id, text, target, mutual, completion_note),
            daemon=True,
        ).start()

    def _translate_worker(
        self, request_id: int, text: str, target: str, mutual: bool, completion_note: str
    ) -> None:
        try:
            protected_text, protected_formulas = protect_math_for_translation(text)
            if mutual:
                english_result, detected = translate_google(protected_text, "en")
                if detected.lower().startswith("zh"):
                    translated = english_result
                    direction = "中文 → 英文"
                else:
                    translated, detected = translate_google(protected_text, "zh-CN")
                    direction = "英文 → 中文" if detected.lower().startswith("en") else f"{detected} → 中文"
                detected_label = f"{detected}｜{direction}"
            else:
                translated, detected = translate_google(protected_text, target)
                detected_label = detected
            translated = restore_protected_math(translated, protected_formulas)
            self.results.put((request_id, "ok", translated, detected_label, text, completion_note))
        except urllib.error.HTTPError as exc:
            LOGGER.warning("Google translation HTTP error: %s", exc.code)
            self.results.put((request_id, "error", f"翻译服务返回错误：HTTP {exc.code}", "", text, completion_note))
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            LOGGER.warning("Google translation network error: %s", reason)
            self.results.put((request_id, "error", f"网络连接失败：{reason}", "", text, completion_note))
        except Exception as exc:  # keep the UI alive on malformed service responses
            LOGGER.exception("Translation worker failed")
            self.results.put((request_id, "error", f"翻译失败：{exc}", "", text, completion_note))

    def _poll_results(self) -> None:
        try:
            while True:
                request_id, kind, payload, detected, source_text, completion_note = self.results.get_nowait()
                if request_id != self.request_id:
                    continue
                if kind == "ok":
                    rendered, failed = self._set_output(payload)
                    self.detected.set(f"检测语言：{detected}")
                    self._record_history(source_text, payload, detected)
                    formula_note = ""
                    if failed:
                        formula_note = f"；公式成功 {rendered} 个，失败 {failed} 个（已保留源码）"
                    elif rendered:
                        formula_note = f"；已成功渲染 {rendered} 个公式"
                    self.status.set("翻译完成" + completion_note + formula_note)
                else:
                    self.status.set(payload + completion_note)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_results)

    def copy_translation(self) -> None:
        text = self.current_translation_text.strip()
        if not text:
            self.status.set("当前没有可复制的译文")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.last_clipboard = text
        self.status.set("译文已复制")

    def clear(self) -> None:
        self.source.delete("1.0", "end")
        self._set_output("")
        self.detected.set("")
        self.status.set("已清空")

    def _record_history(self, source: str, translation: str, detected: str) -> None:
        """Save one successful result and keep the newest 200 unique results."""
        self.history = [
            item
            for item in self.history
            if not (item.get("source") == source and item.get("translation") == translation)
        ]
        mode = "中英互翻" if self.zh_en_mode.get() else f"自动检测 → {self.target_name.get()}"
        self.history.insert(
            0,
            {
                "id": uuid.uuid4().hex,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": source,
                "translation": translation,
                "detected": detected,
                "mode": mode,
            },
        )
        del self.history[MAX_HISTORY_ITEMS:]
        self._save_history()
        if self.history_panel_visible:
            self._refresh_history_tree()

    def show_formula_preview(self) -> None:
        if self.formula_panel_visible:
            self._close_formula_panel()
            self.status.set("已收起公式转换")
            return
        text = self.current_translation_text.strip()
        if not text:
            self.status.set("当前没有可转换的译文")
            return
        if not self._has_math(text):
            self.status.set("当前译文中没有检测到 LaTeX 公式")
            return
        if self.history_panel_visible:
            self._close_history_panel()
        if self.formula_panel is None or not self.formula_panel.winfo_exists():
            self._build_formula_panel()
        if not self.formula_panel_visible:
            self.root.update_idletasks()
            self.compact_geometry = (
                self.root.winfo_width(),
                self.root.winfo_height(),
                self.root.winfo_x(),
                self.root.winfo_y(),
            )
            self.shell.add(self.formula_panel, weight=1)
            self.shell.pane(self.main_frame, weight=0)
            self.formula_panel_visible = True
            self.formula_button_text.set("公式转换 ◀")
            width, height, x, y = self.compact_geometry
            expanded_width = width + 620
            screen_width = self.root.winfo_screenwidth()
            expanded_x = max(0, min(x, screen_width - expanded_width)) if expanded_width <= screen_width else 0
            minimum_height = 300 if self.translation_only.get() else 450
            self.root.minsize(width + 480, minimum_height)
            self.root.geometry(f"{expanded_width}x{max(height, minimum_height)}+{expanded_x}+{y}")
            self.root.update_idletasks()
            self.shell.sashpos(0, width)
            self.root.after(50, lambda: self._keep_main_pane_width(width))
        rendered, failed = self._render_document(self.formula_preview, text, self.formula_images)
        if failed:
            self.formula_preview_status.set(f"已渲染 {rendered} 个公式，{failed} 个不受支持并保留源码")
        else:
            self.formula_preview_status.set(f"已渲染 {rendered} 个公式")
        self.status.set("公式转换结果已在右侧显示")

    def _build_formula_panel(self) -> None:
        frame = ttk.Frame(self.shell, padding=(12, 14, 14, 14))
        self.formula_panel = frame
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="公式转换结果", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(header, text="收起转换", command=self._close_formula_panel).grid(row=0, column=1, sticky="e")
        preview_frame = ttk.Frame(frame)
        preview_frame.grid(row=1, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.formula_preview = tk.Text(
            preview_frame,
            wrap="word",
            state="disabled",
            padx=12,
            pady=10,
            background="#fbfcff",
            relief="solid",
            borderwidth=1,
        )
        scrollbar = ttk.Scrollbar(preview_frame, orient="vertical", command=self.formula_preview.yview)
        self.formula_preview.configure(yscrollcommand=scrollbar.set)
        self.formula_preview.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.formula_preview_status = tk.StringVar()
        ttk.Label(frame, textvariable=self.formula_preview_status, foreground="#667085").grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )

    def _close_formula_panel(self) -> None:
        if not self.formula_panel_visible or not self.formula_panel:
            return
        self.shell.forget(self.formula_panel)
        self.formula_panel_visible = False
        self.formula_button_text.set("公式转换 ▶")
        self.formula_images.clear()
        self.root.minsize(540, 240 if self.translation_only.get() else 390)
        if self.compact_geometry:
            width, height, x, y = self.compact_geometry
            self.root.geometry(f"{width}x{height}+{x}+{y}")

    def show_history(self) -> None:
        if self.formula_panel_visible:
            self._close_formula_panel()
        if self.translation_only.get():
            self.translation_only.set(False)
            self._apply_display_mode()
        if self.history_panel_visible:
            self._close_history_panel()
            return

        self.root.update_idletasks()
        self.compact_geometry = (
            self.root.winfo_width(),
            self.root.winfo_height(),
            self.root.winfo_x(),
            self.root.winfo_y(),
        )
        if self.history_panel is None or not self.history_panel.winfo_exists():
            self._build_history_panel()
        self.shell.add(self.history_panel, weight=1)
        self.shell.pane(self.main_frame, weight=0)
        self.history_panel_visible = True
        self.history_button_text.set("历史记录 ◀")

        width, height, x, y = self.compact_geometry
        expanded_width = width + 620
        screen_width = self.root.winfo_screenwidth()
        expanded_x = max(0, min(x, screen_width - expanded_width)) if expanded_width <= screen_width else 0
        self.root.minsize(width + 480, 450)
        self.root.geometry(f"{expanded_width}x{max(height, 500)}+{expanded_x}+{y}")
        self.root.update_idletasks()
        self.shell.sashpos(0, width)
        self.root.after(50, lambda: self._keep_main_pane_width(width))
        self._refresh_history_tree()
        self.history_search_entry.focus_set()

    def _keep_main_pane_width(self, width: int) -> None:
        if self.history_panel_visible or self.formula_panel_visible:
            self.shell.sashpos(0, width)

    def _build_history_panel(self) -> None:
        frame = ttk.Frame(self.shell, padding=(12, 14, 14, 14))
        self.history_panel = frame
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=3)
        frame.rowconfigure(3, weight=2)

        search_row = ttk.Frame(frame)
        search_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="历史记录　查找：").grid(row=0, column=0, sticky="w")
        self.history_search = tk.StringVar()
        self.history_search_entry = ttk.Entry(search_row, textvariable=self.history_search)
        self.history_search_entry.grid(row=0, column=1, sticky="ew")
        self.history_count = tk.StringVar(value=f"共 {len(self.history)} 条")
        ttk.Label(search_row, textvariable=self.history_count, foreground="#667085").grid(
            row=0, column=2, sticky="e", padx=(12, 0)
        )
        self.history_search.trace_add("write", lambda *_args: self._refresh_history_tree())

        tree_frame = ttk.Frame(frame)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.history_tree = ttk.Treeview(
            tree_frame,
            columns=("source", "translation"),
            show="headings",
            selectmode="browse",
        )
        self.history_tree.heading("source", text="原文")
        self.history_tree.heading("translation", text="翻译结果")
        self.history_tree.column("source", width=440, minwidth=220)
        self.history_tree.column("translation", width=440, minwidth=220)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.history_tree.bind("<<TreeviewSelect>>", self._history_selection_changed)
        self.history_tree.bind("<Double-1>", lambda _event: self._load_history_entry())

        ttk.Label(frame, text="完整内容（双击记录可载入主窗口）").grid(
            row=2, column=0, sticky="w", pady=(10, 5)
        )
        preview = ttk.Panedwindow(frame, orient="horizontal")
        preview.grid(row=3, column=0, sticky="nsew")
        source_box = ttk.Labelframe(preview, text="原文", padding=5)
        translation_box = ttk.Labelframe(preview, text="译文", padding=5)
        self.history_source_preview = tk.Text(source_box, wrap="word", height=7, state="disabled")
        self.history_translation_preview = tk.Text(
            translation_box, wrap="word", height=7, state="disabled", background="#fbfcff"
        )
        self.history_source_preview.pack(fill="both", expand=True)
        self.history_translation_preview.pack(fill="both", expand=True)
        preview.add(source_box, weight=1)
        preview.add(translation_box, weight=1)

        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="载入所选", command=self._load_history_entry).pack(side="left")
        ttk.Button(actions, text="复制译文", command=self._copy_history_translation).pack(side="left", padx=8)
        self.history_formula_button_text = tk.StringVar(value="公式转换")
        ttk.Button(
            actions,
            textvariable=self.history_formula_button_text,
            command=self._toggle_history_formula,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="删除所选", command=self._delete_history_entry).pack(side="left")
        ttk.Button(actions, text="清空历史", command=self._clear_history).pack(side="right")


    @staticmethod
    def _history_preview(text: str, limit: int = 90) -> str:
        compact = " ".join(text.split())
        return compact if len(compact) <= limit else compact[: limit - 1] + "…"

    def _refresh_history_tree(self) -> None:
        if not self.history_panel or not self.history_panel.winfo_exists():
            return
        query = self.history_search.get().strip().casefold()
        self.history_tree.delete(*self.history_tree.get_children())
        matched = 0
        for item in self.history:
            searchable = " ".join(
                str(item.get(key, "")) for key in ("source", "translation", "detected", "mode", "time")
            ).casefold()
            if query and query not in searchable:
                continue
            matched += 1
            self.history_tree.insert(
                "",
                "end",
                iid=item["id"],
                values=(
                    self._history_preview(item.get("source", "")),
                    self._history_preview(item.get("translation", "")),
                ),
            )
        self.history_count.set(f"找到 {matched} 条｜共 {len(self.history)} 条" if query else f"共 {len(self.history)} 条")
        self._set_history_preview("", "")

    def _selected_history_record(self) -> dict | None:
        selected = self.history_tree.selection()
        if not selected:
            return None
        record_id = selected[0]
        return next((item for item in self.history if item.get("id") == record_id), None)

    def _history_selection_changed(self, _event=None) -> None:
        item = self._selected_history_record()
        if item:
            translation = item.get("translation", "")
            self.history_formula_rendered = self.auto_math_render.get() and self._has_math(translation)
            self._set_history_preview(item.get("source", ""), translation)

    def _set_history_preview(self, source: str, translation: str) -> None:
        self.history_source_preview.configure(state="normal")
        self.history_source_preview.delete("1.0", "end")
        self.history_source_preview.insert("1.0", source)
        self.history_source_preview.configure(state="disabled")

        self.history_formula_images.clear()
        if translation and self.history_formula_rendered and self._has_math(translation):
            rendered, failed = self._render_document(
                self.history_translation_preview, translation, self.history_formula_images
            )
            self.history_formula_button_text.set("显示源码")
            if failed:
                self.status.set(f"历史译文：公式成功 {rendered} 个，失败 {failed} 个（已保留源码）")
        else:
            self.history_translation_preview.configure(state="normal")
            self.history_translation_preview.delete("1.0", "end")
            self.history_translation_preview.insert("1.0", translation)
            self.history_translation_preview.configure(state="disabled")
            self.history_formula_button_text.set("公式转换")

    def _toggle_history_formula(self) -> None:
        item = self._selected_history_record()
        if not item:
            self.status.set("请先选择一条历史记录")
            return
        translation = item.get("translation", "")
        if not self._has_math(translation):
            self.status.set("所选历史译文中没有检测到 LaTeX 公式")
            return
        self.history_formula_rendered = not self.history_formula_rendered
        self._set_history_preview(item.get("source", ""), translation)
        if self.history_formula_rendered:
            self.status.set("历史译文已转换为公式")
        else:
            self.status.set("历史译文已切换为源码")

    def _load_history_entry(self) -> None:
        item = self._selected_history_record()
        if not item:
            self.status.set("请先选择一条历史记录")
            return
        self._set_source(item.get("source", ""))
        self._set_output(item.get("translation", ""))
        self.detected.set(f"历史记录：{item.get('detected', '')}")
        self._show_window()
        self.status.set("已载入历史记录")

    def _copy_history_translation(self) -> None:
        item = self._selected_history_record()
        if not item:
            self.status.set("请先选择一条历史记录")
            return
        text = item.get("translation", "")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self.last_clipboard = text
        self.status.set("历史译文已复制")

    def _delete_history_entry(self) -> None:
        item = self._selected_history_record()
        if not item:
            self.status.set("请先选择一条历史记录")
            return
        self.history = [record for record in self.history if record.get("id") != item.get("id")]
        self._save_history()
        self._refresh_history_tree()
        self.status.set("已删除所选历史记录")

    def _clear_history(self) -> None:
        if not self.history:
            return
        if not messagebox.askyesno("清空历史", "确定删除全部翻译历史吗？此操作无法撤销。", parent=self.root):
            return
        self.history.clear()
        self._save_history()
        self._refresh_history_tree()
        self.status.set("翻译历史已清空")

    def _close_history_panel(self) -> None:
        if not self.history_panel_visible or not self.history_panel:
            return
        self.shell.forget(self.history_panel)
        self.history_panel_visible = False
        self.history_button_text.set("历史记录 ▶")
        self.root.minsize(540, 390)
        if self.compact_geometry:
            width, height, x, y = self.compact_geometry
            self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _load_history(self) -> list[dict]:
        try:
            data = json.loads(history_data_path().read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            valid: list[dict] = []
            for item in data[:MAX_HISTORY_ITEMS]:
                if not isinstance(item, dict) or not item.get("source") or "translation" not in item:
                    continue
                item.setdefault("id", uuid.uuid4().hex)
                valid.append(item)
            return valid
        except (OSError, ValueError):
            return []

    def _save_history(self) -> None:
        path = history_data_path()
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.history, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_settings(self) -> dict:
        try:
            return json.loads(app_data_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        settings = {
            "target": self.target_name.get(),
            "hotkey": self.hotkey_name.get(),
            "zh_en_mode": self.zh_en_mode.get(),
            "watch_clipboard": self.watch_clipboard.get(),
            "always_on_top": self.always_on_top.get(),
            "source_collapsed": self.source_collapsed.get(),
            "translation_only": self.translation_only.get(),
            "auto_math_render": self.auto_math_render.get(),
        }
        try:
            app_data_path().write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _create_tray_image() -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=13, fill="#1677ff", outline="#0d4fa6", width=2)
        try:
            font = ImageFont.truetype("arialbd.ttf", 38)
        except OSError:
            font = ImageFont.load_default()
        bounds = draw.textbbox((0, 0), "T", font=font)
        text_width = bounds[2] - bounds[0]
        text_height = bounds[3] - bounds[1]
        draw.text(
            ((64 - text_width) / 2, (64 - text_height) / 2 - bounds[1]),
            "T",
            font=font,
            fill="white",
        )
        return image

    def _start_tray_icon(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("显示翻译窗口", lambda _icon, _item: self.tray_events.put("show"), default=True),
            pystray.MenuItem("打开日志", lambda _icon, _item: self.tray_events.put("log")),
            pystray.MenuItem("退出程序", lambda _icon, _item: self.tray_events.put("exit")),
        )
        self.tray_icon = pystray.Icon("SelectionTranslator", self._create_tray_image(), APP_NAME, menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _poll_tray_events(self) -> None:
        try:
            while True:
                event = self.tray_events.get_nowait()
                if event == "show":
                    self._show_window()
                elif event == "log":
                    self._open_log()
                elif event == "exit":
                    self._on_close()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_tray_events)

    def _hide_to_tray(self) -> None:
        self.status.set(f"已隐藏到系统托盘；按 {self.hotkey_name.get()} 可再次唤起")
        self.root.withdraw()
        if self.tray_icon and not self.tray_hint_shown:
            self.tray_hint_shown = True
            try:
                self.tray_icon.notify("程序仍在后台运行；双击托盘图标可显示，右键可退出。", "已隐藏到系统托盘")
            except (NotImplementedError, OSError):
                pass

    def _on_close(self) -> None:
        LOGGER.info("Application exiting")
        self._save_settings()
        if self.hotkey_thread_id:
            self.user32.PostThreadMessageW(self.hotkey_thread_id, WM_QUIT, 0, 0)
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()


def self_test() -> int:
    sample = r"Clock noise is \(q_{\theta,b}=10^{-6}\)."
    protected, formulas = protect_math_for_translation(sample)
    translated, detected = translate_google(protected, "zh-CN")
    restored = restore_protected_math(translated, formulas)
    buffer = BytesIO()
    math_to_image(r"$q_{\theta,b}=10^{-6}$", buffer, dpi=120, format="png")
    formula_ok = len(buffer.getvalue()) > 100 and formulas[0] in restored
    print(json.dumps({"translated": restored, "detected": detected, "formula_ok": formula_ok}, ensure_ascii=False))
    return 0 if translated and formula_ok else 1


def main() -> int:
    if sys.platform != "win32":
        print("This app currently supports Windows only.", file=sys.stderr)
        return 2
    configure_logging()
    LOGGER.info("Application starting; executable=%s", sys.executable)
    if "--self-test" in sys.argv:
        try:
            result = self_test()
            LOGGER.info("Self-test completed with code %s", result)
            return result
        except Exception:
            LOGGER.exception("Self-test failed")
            return 1
    root = tk.Tk()
    SelectionTranslator(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
