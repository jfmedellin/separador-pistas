"""Tkinter/customtkinter studio workspace for separating and auditioning Windows stems."""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from tkinterdnd2 import DND_FILES, TkinterDnD

from SeparationWorker.demucs_adapter import frozen_worker_path
from SeparationWorker.engine import stem_cache
from SeparationWorker.engine.mixer import MixerSnapshot
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.gui_controller import GuiState, SeparationController
from SeparationWorker.mixer_controller import MixerController, MixerState


# Kanagawa Dragon palette (https://github.com/rebelot/kanagawa.nvim).
COLORS = {
    "window": "#0D0C0C",
    "surface": "#161514",
    "surface_alt": "#131211",
    "field": "#1D1C19",
    "line": "#282727",
    "line_strong": "#625E5A",
    "text": "#DCD7BA",
    "muted": "#9E9B93",
    "muted2": "#7A756F",
    "disabled": "#5A5750",
    "accent": "#C4B28A",
    "accent_hover": "#D8C69D",
    "accent_text": "#0D0C0C",
    "error": "#C4746E",
    "success": "#87A987",
}

STEMS = (
    ("VOCALS", "#A292A3"),
    ("DRUMS", "#B6927B"),
    ("BASS", "#87A987"),
    ("OTHER", "#8BA4B0"),
)
STEM_ROWS = tuple((name.lower() + ".wav", name, color) for name, color in STEMS)
STEM_ICONS = {"vocals.wav": "mic", "drums.wav": "drum", "bass.wav": "bass", "other.wav": "layers"}
APP_NAME = "Stemslayer"

NAV_HEIGHT = 60


def parse_drop_paths(data: str, splitlist: Callable[[str], tuple[str, ...]]) -> tuple[str, ...]:
    """Parse a Tcl/Tk file-list payload without breaking paths that contain spaces."""
    if not data:
        return ()
    try:
        return tuple(path for path in splitlist(data) if path)
    except (TypeError, tk.TclError):
        return ()


def format_time(seconds: float) -> str:
    """Format a timeline value without exposing audio implementation details."""
    seconds = max(0.0, float(seconds))
    minutes, remainder = divmod(seconds, 60.0)
    return f"{int(minutes):02d}:{remainder:05.2f}"


def _draw_icon(canvas: tk.Canvas, kind: str, size: int, color: str) -> None:
    """Draw a small stroke-based glyph on a bare canvas (no icon-font dependency)."""
    canvas.delete("icon")
    pad = size * 0.18
    if kind == "play":
        canvas.create_polygon(pad, pad, pad, size - pad, size - pad, size / 2, fill=color, outline="", tags="icon")
    elif kind == "pause":
        bar_w, gap = size * 0.2, size * 0.16
        canvas.create_rectangle(size / 2 - gap / 2 - bar_w, pad, size / 2 - gap / 2, size - pad, fill=color, outline="", tags="icon")
        canvas.create_rectangle(size / 2 + gap / 2, pad, size / 2 + gap / 2 + bar_w, size - pad, fill=color, outline="", tags="icon")
    elif kind == "audio":
        canvas.create_line(size * 0.4, size * 0.78, size * 0.4, size * 0.2, fill=color, width=2, tags="icon")
        canvas.create_line(size * 0.4, size * 0.2, size * 0.78, size * 0.1, fill=color, width=2, tags="icon")
        canvas.create_line(size * 0.78, size * 0.1, size * 0.78, size * 0.56, fill=color, width=2, tags="icon")
        canvas.create_oval(size * 0.26, size * 0.68, size * 0.44, size * 0.86, outline=color, width=2, tags="icon")
        canvas.create_oval(size * 0.64, size * 0.54, size * 0.82, size * 0.72, outline=color, width=2, tags="icon")
    elif kind == "mic":
        canvas.create_rectangle(size * 0.4, size * 0.08, size * 0.6, size * 0.55, outline=color, width=2, tags="icon")
        canvas.create_arc(size * 0.24, size * 0.32, size * 0.76, size * 0.72, start=180, extent=180, style="arc", outline=color, width=2, tags="icon")
        canvas.create_line(size / 2, size * 0.72, size / 2, size * 0.9, fill=color, width=2, tags="icon")
        canvas.create_line(size * 0.33, size * 0.9, size * 0.67, size * 0.9, fill=color, width=2, tags="icon")
    elif kind == "drum":
        canvas.create_oval(size * 0.12, size * 0.12, size * 0.88, size * 0.48, outline=color, width=2, tags="icon")
        canvas.create_arc(size * 0.12, size * 0.32, size * 0.88, size * 0.84, start=180, extent=180, style="arc", outline=color, width=2, tags="icon")
        canvas.create_line(size * 0.12, size * 0.3, size * 0.12, size * 0.58, fill=color, width=2, tags="icon")
        canvas.create_line(size * 0.88, size * 0.3, size * 0.88, size * 0.58, fill=color, width=2, tags="icon")
    elif kind == "bass":
        canvas.create_oval(size * 0.08, size * 0.54, size * 0.5, size * 0.96, outline=color, width=2, tags="icon")
        canvas.create_line(size * 0.42, size * 0.62, size * 0.92, size * 0.08, fill=color, width=2, tags="icon")
    elif kind == "layers":
        canvas.create_rectangle(size * 0.16, size * 0.55, size * 0.34, size * 0.9, fill=color, outline="", tags="icon")
        canvas.create_rectangle(size * 0.42, size * 0.28, size * 0.6, size * 0.9, fill=color, outline="", tags="icon")
        canvas.create_rectangle(size * 0.68, size * 0.65, size * 0.86, size * 0.9, fill=color, outline="", tags="icon")
    elif kind == "check":
        canvas.create_oval(pad, pad, size - pad, size - pad, outline=color, width=2, tags="icon")
        canvas.create_line(size * 0.3, size * 0.52, size * 0.44, size * 0.66, size * 0.72, size * 0.34, fill=color, width=2, tags="icon")
    elif kind == "alert":
        canvas.create_oval(pad, pad, size - pad, size - pad, outline=color, width=2, tags="icon")
        canvas.create_line(size / 2, size * 0.32, size / 2, size * 0.58, fill=color, width=2, tags="icon")
        canvas.create_oval(size / 2 - 1.5, size * 0.68, size / 2 + 1.5, size * 0.68 + 3, fill=color, outline="", tags="icon")
    elif kind == "dot":
        canvas.create_oval(size * 0.35, size * 0.35, size * 0.65, size * 0.65, fill=color, outline="", tags="icon")


def _icon(parent: tk.Widget, kind: str, size: int, color: str, bg: str) -> tk.Canvas:
    canvas = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
    _draw_icon(canvas, kind, size, color)
    return canvas


class _RoundButton:
    """A circular, icon-only transport button (plain Tk/CTk has no built-in one)."""

    def __init__(self, parent: tk.Widget, *, diameter: int, fg: str, fg_disabled: str, icon_color: str, command: Callable[[], None]):
        self._fg = fg
        self._fg_disabled = fg_disabled
        self._icon_color = icon_color
        self._icon_kind = "play"
        self._command = command
        self._enabled = True
        self.frame = ctk.CTkFrame(parent, width=diameter, height=diameter, corner_radius=diameter // 2, fg_color=fg)
        self.frame.pack_propagate(False)
        self._size = diameter
        self.canvas = tk.Canvas(self.frame, width=diameter, height=diameter, bg=fg, highlightthickness=0, cursor="hand2")
        self.canvas.pack(expand=True)
        self.canvas.bind("<Button-1>", self._on_click)
        self._redraw()

    def _redraw(self) -> None:
        color = self._icon_color if self._enabled else COLORS["muted2"]
        _draw_icon(self.canvas, self._icon_kind, self._size, color)

    def set_icon(self, kind: str) -> None:
        self._icon_kind = kind
        self._redraw()

    def _on_click(self, _event=None) -> None:
        if self._enabled:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        color = self._fg if enabled else self._fg_disabled
        self.frame.configure(fg_color=color)
        self.canvas.configure(bg=color, cursor="hand2" if enabled else "arrow")
        self._redraw()


@dataclass(frozen=True)
class MixerViewModel:
    """Headless presentation state shared by the Tk view and portable tests."""

    stem_names: tuple[str, ...] = STEM_NAMES
    position: int = 0
    frame_count: int = 0
    sample_rate: int = 0
    playing: bool = False
    settings: MixerSnapshot = MixerSnapshot(tuple())
    preview_frame: int | None = None

    @classmethod
    def from_controller_state(cls, state: MixerState, *, preview_frame: int | None = None):
        session = state.session
        sample_rate = int(getattr(session, "sample_rate", 0) or 0)
        frame_count = max(0, int(state.frame_count))
        position = max(0, min(int(state.position), frame_count))
        if preview_frame is not None:
            preview_frame = max(0, min(int(preview_frame), frame_count))
        return cls(
            position=position,
            frame_count=frame_count,
            sample_rate=sample_rate,
            playing=bool(state.playing),
            settings=state.settings,
            preview_frame=preview_frame,
        )

    @property
    def lane_names(self) -> tuple[str, ...]:
        return self.stem_names

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate if self.sample_rate else 0.0

    @property
    def position_seconds(self) -> float:
        return self.position / self.sample_rate if self.sample_rate else 0.0

    @property
    def playhead_frame(self) -> int:
        return self.preview_frame if self.preview_frame is not None else self.position

    @property
    def playhead_ratio(self) -> float:
        return self.position / self.frame_count if self.frame_count else 0.0

    @property
    def preview_ratio(self) -> float:
        return self.playhead_frame / self.frame_count if self.frame_count else 0.0

    @property
    def volume_percent(self) -> dict[str, float]:
        return {setting.name: setting.gain * 100.0 for setting in self.settings.settings}

    @property
    def muted_names(self) -> tuple[str, ...]:
        return tuple(setting.name for setting in self.settings.settings if setting.muted)

    @property
    def soloed_names(self) -> tuple[str, ...]:
        return tuple(setting.name for setting in self.settings.settings if setting.solo)

    @property
    def excluded_controls(self) -> tuple[str, ...]:
        # Explicitly empty: the MVP does not model controls that are out of scope.
        return ()

    def frame_for_pixel(self, pixel: float, width: float) -> int:
        if self.frame_count <= 0 or width <= 0:
            return 0
        ratio = max(0.0, min(float(pixel) / float(width), 1.0))
        return int(round(ratio * self.frame_count))

    def with_preview(self, frame: int | float | None):
        if frame is None:
            return replace(self, preview_frame=None)
        return replace(self, preview_frame=max(0, min(int(frame), self.frame_count)))


class StemslayerApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.configure(fg_color=COLORS["window"])
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._events: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._view = "separation"
        self._closed = False
        self._preview_frame: int | None = None
        self._seek_dragging = False
        self._waveform_session = None
        self._waveform_canvases: dict[str, tk.Canvas] = {}
        self._lane_widgets: dict[str, dict[str, object]] = {}
        self._mixer_model = MixerViewModel()
        self._syncing_controls = False
        self._cache_directories: list[Path] = []
        self.export_overlay: ctk.CTkFrame | None = None

        self.status_headline = tk.StringVar()
        self.status_detail = tk.StringVar()
        self.mixer_headline = tk.StringVar(value="Choose a four-stem folder")
        self.mixer_detail = tk.StringVar(value="Load vocals.wav, drums.wav, bass.wav, and other.wav to begin.")
        self.mixer_time = tk.StringVar(value="00:00.00 / 00:00.00")

        self._build()
        self.controller = SeparationController(
            dispatch=self._events.put,
            on_change=self._render_state,
            on_success=self._separation_succeeded,
        )
        self.mixer_controller = MixerController(
            dispatch=self._events.put,
            on_change=self._render_mixer_state,
        )
        self._render_state(self.controller.state)
        self._render_mixer_state(self.mixer_controller.state)
        self._show_view("separation")
        self.root.after(50, self._drain_events)
        threading.Thread(
            target=stem_cache.sweep_orphans, name="stemslayer-stem-cache-sweep", daemon=True
        ).start()

    def _build(self) -> None:
        self.root.grid_rowconfigure(2, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self._build_nav()

        self.workspace = ctk.CTkFrame(self.root, fg_color=COLORS["window"], corner_radius=0)
        self.workspace.grid(row=2, column=0, sticky="nsew")
        self.workspace.grid_rowconfigure(0, weight=1)
        self.workspace.grid_columnconfigure(0, weight=1)

        self.separation_view = ctk.CTkFrame(self.workspace, fg_color=COLORS["window"], corner_radius=0)
        self.separation_view.grid(row=0, column=0, sticky="nsew")
        self._build_separation_view()

        self.mixer_view = ctk.CTkFrame(self.workspace, fg_color=COLORS["window"], corner_radius=0)
        self._build_mixer_view()

    def _build_nav(self) -> None:
        nav = ctk.CTkFrame(self.root, height=NAV_HEIGHT, corner_radius=0, fg_color=COLORS["window"])
        nav.grid(row=0, column=0, sticky="ew")
        nav.grid_propagate(False)
        nav.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(nav, text=APP_NAME.upper(), text_color=COLORS["text"], font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, padx=(32, 36)
        )

        tabs = ctk.CTkFrame(nav, fg_color="transparent")
        tabs.grid(row=0, column=1, sticky="w")
        self._tab_buttons: dict[str, ctk.CTkButton] = {}
        self._tab_underlines: dict[str, ctk.CTkFrame] = {}
        tab_specs = (
            ("separation", "SPLIT", lambda: self._show_view("separation")),
            ("mixer", "MIXER", lambda: self._show_view("mixer")),
            ("export", "EXPORT", self._open_export_dialog),
        )
        for index, (key, label, handler) in enumerate(tab_specs):
            column = ctk.CTkFrame(tabs, fg_color="transparent")
            column.grid(row=0, column=index, padx=(0 if index == 0 else 24, 0))
            button = ctk.CTkButton(
                column,
                text=label,
                command=handler,
                fg_color="transparent",
                hover_color=COLORS["field"],
                text_color=COLORS["muted2"],
                text_color_disabled=COLORS["disabled"],
                font=("Segoe UI", 11, "bold"),
                corner_radius=6,
                height=NAV_HEIGHT - 20,
            )
            button.pack()
            underline = ctk.CTkFrame(column, height=2, corner_radius=0, fg_color=COLORS["window"])
            underline.pack(fill="x")
            self._tab_buttons[key] = button
            self._tab_underlines[key] = underline

        divider = ctk.CTkFrame(self.root, height=1, corner_radius=0, fg_color=COLORS["line"])
        divider.grid(row=1, column=0, sticky="ew")

    def _set_active_tab(self, key: str) -> None:
        for tab_key, button in self._tab_buttons.items():
            active = tab_key == key
            button.configure(text_color=COLORS["text"] if active else COLORS["muted2"])
            self._tab_underlines[tab_key].configure(fg_color=COLORS["accent"] if active else COLORS["window"])

    def _build_separation_view(self) -> None:
        shell = self.separation_view
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(0, weight=1)

        center = ctk.CTkFrame(shell, fg_color=COLORS["window"], corner_radius=0, width=600)
        center.place(relx=0.5, y=30, anchor="n")

        header = ctk.CTkFrame(center, fg_color=COLORS["window"], corner_radius=0)
        header.pack(fill="x")
        ctk.CTkLabel(header, text="Four clean channels. One local pass.", text_color=COLORS["text"], font=("Segoe UI", 22, "bold")).pack()
        ctk.CTkLabel(
            header,
            text="Select your source audio, then split it into four clean local stems.",
            text_color=COLORS["muted"],
            font=("Segoe UI", 12),
        ).pack(pady=(6, 0))

        ctk.CTkLabel(center, text="INPUT AUDIO", text_color=COLORS["muted"], font=("Segoe UI", 10, "bold"), anchor="w").pack(
            fill="x", pady=(28, 6)
        )
        dropzone = ctk.CTkFrame(center, fg_color=COLORS["surface_alt"], corner_radius=12, border_width=1, border_color=COLORS["line_strong"])
        dropzone.pack(fill="x")
        dz_inner = ctk.CTkFrame(dropzone, fg_color="transparent")
        dz_inner.pack(pady=22)
        _icon(dz_inner, "audio", 26, COLORS["muted"], COLORS["surface_alt"]).pack()
        self.input_value_label = ctk.CTkLabel(dz_inner, text="No file selected", text_color=COLORS["text"], font=("Segoe UI", 12, "bold"))
        self.input_value_label.pack(pady=(10, 2))
        drop_hint = ctk.CTkLabel(
            dz_inner,
            text="Drag and drop an audio file, or click to browse",
            text_color=COLORS["muted2"],
            font=("Segoe UI", 10),
        )
        drop_hint.pack()
        self.input_button = ctk.CTkButton(
            dz_inner,
            text="Browse file",
            command=self._pick_input,
            fg_color="transparent",
            hover_color=COLORS["field"],
            border_width=1,
            border_color=COLORS["line_strong"],
            text_color=COLORS["text"],
            corner_radius=8,
            font=("Segoe UI", 11, "bold"),
        )
        self.input_button.pack(pady=(12, 0))
        for widget in (dropzone, dz_inner):
            widget.bind("<Button-1>", lambda _event: self._pick_input())
        self._register_drop_zone(dropzone)

        ctk.CTkLabel(center, text="CHANNELS TO EXTRACT", text_color=COLORS["muted"], font=("Segoe UI", 10, "bold"), anchor="w").pack(
            fill="x", pady=(24, 6)
        )
        chips = ctk.CTkFrame(center, fg_color="transparent")
        chips.pack(fill="x")
        for index, (name, color) in enumerate(STEMS):
            chips.grid_columnconfigure(index, weight=1)
            chip = ctk.CTkFrame(chips, fg_color=COLORS["field"], corner_radius=999, border_width=1, border_color=COLORS["line"])
            chip.grid(row=0, column=index, padx=(0 if index == 0 else 8, 0), sticky="ew")
            inner = ctk.CTkFrame(chip, fg_color="transparent")
            inner.pack(pady=10)
            dot = tk.Canvas(inner, width=8, height=8, bg=COLORS["field"], highlightthickness=0)
            dot.create_oval(0, 0, 8, 8, fill=color, outline="")
            dot.pack(side="left", padx=(0, 6))
            ctk.CTkLabel(inner, text=name, text_color=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(side="left")

        self.status_banner = ctk.CTkFrame(center, fg_color=COLORS["surface"], corner_radius=10)
        self.status_banner.pack(fill="x", pady=(28, 0))
        banner_inner = ctk.CTkFrame(self.status_banner, fg_color="transparent")
        banner_inner.pack(fill="x", padx=16, pady=14)
        self.status_icon = _icon(banner_inner, "dot", 20, COLORS["muted"], COLORS["surface"])
        self.status_icon.pack(side="left", padx=(0, 12))
        status_copy = ctk.CTkFrame(banner_inner, fg_color="transparent")
        status_copy.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            status_copy, textvariable=self.status_headline, text_color=COLORS["text"], font=("Segoe UI", 12, "bold"), anchor="w"
        ).pack(fill="x")
        ctk.CTkLabel(
            status_copy,
            textvariable=self.status_detail,
            text_color=COLORS["muted"],
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=460,
        ).pack(fill="x", pady=(2, 0))

        footer = ctk.CTkFrame(center, fg_color="transparent")
        footer.pack(fill="x", pady=(20, 40))
        self.action = ctk.CTkButton(
            footer,
            text="Separate into 4 stems",
            command=self._start,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["accent_text"],
            text_color_disabled=COLORS["muted2"],
            corner_radius=8,
            font=("Segoe UI", 12, "bold"),
            height=40,
        )
        self.action.pack(side="right")

    def _register_drop_zone(self, widget: tk.Widget) -> None:
        """Register the complete CTk widget tree so every visible drop-zone surface accepts files."""
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", self._drop_input)
        for child in widget.winfo_children():
            self._register_drop_zone(child)

    def _drop_input(self, event) -> str:
        paths = parse_drop_paths(event.data, self.root.tk.splitlist)
        if paths:
            self.controller.set_input_file(paths[0])
        return getattr(event, "action", "copy")

    def _build_mixer_view(self) -> None:
        shell = self.mixer_view
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(shell, fg_color=COLORS["window"], corner_radius=0)
        header.grid(row=0, column=0, padx=32, pady=(22, 0), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title_box, text="Four stems. One shared timeline.", text_color=COLORS["text"], font=("Segoe UI", 18, "bold")).pack(
            anchor="w"
        )
        ctk.CTkLabel(
            title_box,
            textvariable=self.mixer_detail,
            text_color=COLORS["muted2"],
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=680,
        ).pack(anchor="w", pady=(3, 0))
        self.load_folder_button = ctk.CTkButton(
            header,
            text="Load stems folder",
            command=self._pick_stems_folder,
            fg_color="transparent",
            hover_color=COLORS["field"],
            border_width=1,
            border_color=COLORS["line_strong"],
            text_color=COLORS["text"],
            corner_radius=8,
            font=("Segoe UI", 11, "bold"),
        )
        self.load_folder_button.grid(row=0, column=1, sticky="e")

        transport = ctk.CTkFrame(shell, fg_color=COLORS["window"], corner_radius=0)
        transport.grid(row=1, column=0, padx=32, pady=(20, 16), sticky="ew")
        transport.grid_columnconfigure(2, weight=1)

        self.play_button = _RoundButton(
            transport,
            diameter=52,
            fg=COLORS["accent"],
            fg_disabled=COLORS["field"],
            icon_color=COLORS["accent_text"],
            command=self._toggle_play,
        )
        self.play_button.frame.grid(row=0, column=0, sticky="w")

        self.mixer_status = ctk.CTkLabel(
            transport, textvariable=self.mixer_headline, text_color=COLORS["text"], font=("Segoe UI", 11, "bold"), anchor="w"
        )
        self.mixer_status.grid(row=0, column=1, padx=(14, 14), sticky="w")

        self.timeline = tk.Canvas(transport, height=26, bg=COLORS["window"], highlightthickness=0)
        self.timeline.grid(row=0, column=2, sticky="ew")
        self.timeline.bind("<Button-1>", self._seek_press)
        self.timeline.bind("<B1-Motion>", self._seek_motion)
        self.timeline.bind("<ButtonRelease-1>", self._seek_release)
        self.timeline.bind("<Configure>", lambda _event: self._draw_timeline())

        ctk.CTkLabel(
            transport, textvariable=self.mixer_time, text_color=COLORS["muted"], font=("Consolas", 10)
        ).grid(row=0, column=3, padx=(14, 14))

        self.export_button = ctk.CTkButton(
            transport,
            text="Export",
            command=self._open_export_dialog,
            fg_color="transparent",
            hover_color=COLORS["field"],
            border_width=1,
            border_color=COLORS["line_strong"],
            text_color=COLORS["text"],
            corner_radius=8,
            font=("Segoe UI", 11, "bold"),
        )
        self.export_button.grid(row=0, column=4, sticky="e")

        lanes = ctk.CTkFrame(shell, fg_color=COLORS["window"], corner_radius=0)
        lanes.grid(row=2, column=0, padx=32, pady=(0, 22), sticky="nsew")
        lanes.grid_columnconfigure(0, weight=1)
        for index, (stem_name, label, color) in enumerate(STEM_ROWS):
            lanes.grid_rowconfigure(index, weight=1, uniform="lane")
            lane = ctk.CTkFrame(lanes, fg_color=COLORS["surface"], corner_radius=12, border_width=1, border_color=COLORS["line"])
            lane.grid(row=index, column=0, pady=(0 if index == 0 else 10, 0), sticky="nsew")
            lane.grid_columnconfigure(1, weight=1)
            lane.grid_rowconfigure(0, weight=1)

            controls = ctk.CTkFrame(lane, width=190, height=104, fg_color=COLORS["surface"], corner_radius=0)
            controls.grid(row=0, column=0, padx=(4, 8), pady=10, sticky="nsw")
            controls.grid_propagate(False)
            bar = tk.Canvas(controls, width=4, height=84, bg=color, highlightthickness=0)
            bar.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(10, 0))
            ctk.CTkLabel(controls, text=label, text_color=COLORS["text"], font=("Segoe UI", 11, "bold"), anchor="w").grid(
                row=0, column=1, columnspan=3, padx=(10, 0), sticky="w"
            )
            scale = ctk.CTkSlider(
                controls,
                from_=0,
                to=100,
                width=112,
                height=14,
                fg_color=COLORS["line"],
                progress_color=color,
                button_color=COLORS["text"],
                button_hover_color=COLORS["text"],
                command=lambda value, name=stem_name: self._volume_changed(name, value),
            )
            scale.set(100)
            scale.grid(row=1, column=1, columnspan=2, padx=(10, 2), pady=(8, 0), sticky="w")
            percent = ctk.CTkLabel(controls, text="100%", width=34, anchor="e", text_color=COLORS["muted"], font=("Consolas", 9))
            percent.grid(row=1, column=3, pady=(8, 0), sticky="e")
            mute = ctk.CTkButton(
                controls,
                text="M",
                width=26,
                height=22,
                corner_radius=6,
                fg_color=COLORS["field"],
                hover_color=COLORS["line"],
                text_color=COLORS["muted"],
                text_color_disabled=COLORS["disabled"],
                font=("Segoe UI", 9, "bold"),
                command=lambda name=stem_name: self._toggle_mute(name),
            )
            mute.grid(row=2, column=1, padx=(10, 3), pady=(8, 0), sticky="w")
            solo = ctk.CTkButton(
                controls,
                text="S",
                width=26,
                height=22,
                corner_radius=6,
                fg_color=COLORS["field"],
                hover_color=COLORS["line"],
                text_color=COLORS["muted"],
                text_color_disabled=COLORS["disabled"],
                font=("Segoe UI", 9, "bold"),
                command=lambda name=stem_name: self._toggle_solo(name),
            )
            solo.grid(row=2, column=2, padx=3, pady=(8, 0), sticky="w")

            canvas = tk.Canvas(lane, bg=COLORS["surface"], highlightthickness=0)
            canvas.grid(row=0, column=1, padx=(0, 12), pady=10, sticky="nsew")
            canvas.bind("<Button-1>", self._seek_press)
            canvas.bind("<B1-Motion>", self._seek_motion)
            canvas.bind("<ButtonRelease-1>", self._seek_release)
            canvas.bind("<Configure>", lambda _event: self._draw_waveforms())
            self._waveform_canvases[stem_name] = canvas
            self._lane_widgets[stem_name] = {"scale": scale, "percent": percent, "mute": mute, "solo": solo, "color": color}

    def _pick_input(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="Choose input audio", filetypes=(("Audio files", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("All files", "*.*"))
        )
        if selected:
            self.controller.set_input_file(selected)

    def _pick_stems_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="Choose a folder with four WAV stems")
        if selected:
            self._open_mixer_folder(selected, title=Path(selected).name)

    def _open_mixer_folder(self, folder: str | Path, *, title: str = "") -> None:
        self._show_view("mixer")
        self._preview_frame = None
        self.mixer_controller.load(folder, title=title)

    def _start(self) -> None:
        self.controller.start()

    def _separation_succeeded(self, result: Path) -> None:
        self._cache_directories.append(Path(result))
        input_file = self.controller.state.input_file
        title = Path(input_file).stem if input_file else ""
        self._open_mixer_folder(result, title=title)

    def _open_export_dialog(self) -> None:
        if not self.mixer_controller.state.has_session:
            return
        self._close_export_overlay()

        overlay = ctk.CTkFrame(
            self.workspace, fg_color=COLORS["surface"], corner_radius=16, border_width=1, border_color=COLORS["line_strong"], width=380
        )
        overlay.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        overlay.pack_propagate(False)
        self.export_overlay = overlay

        body = ctk.CTkFrame(overlay, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=22)

        head_row = ctk.CTkFrame(body, fg_color="transparent")
        head_row.pack(fill="x")
        ctk.CTkLabel(head_row, text="Export stems", text_color=COLORS["text"], font=("Segoe UI", 16, "bold"), anchor="w").pack(
            side="left"
        )
        ctk.CTkButton(
            head_row,
            text="✕",
            width=26,
            height=26,
            corner_radius=13,
            fg_color="transparent",
            hover_color=COLORS["field"],
            text_color=COLORS["muted"],
            command=self._close_export_overlay,
        ).pack(side="right")

        stem_vars: dict[str, tk.BooleanVar] = {stem_name: tk.BooleanVar(value=True) for stem_name, _label, _color in STEM_ROWS}
        select_all_var = tk.BooleanVar(value=True)

        select_all = ctk.CTkCheckBox(
            body,
            text="Select all",
            variable=select_all_var,
            command=lambda: self._set_all_export_vars(stem_vars, select_all_var.get()),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent"],
            checkmark_color=COLORS["accent_text"],
            border_color=COLORS["line_strong"],
            text_color=COLORS["text"],
            corner_radius=6,
            font=("Segoe UI", 11, "bold"),
        )
        select_all.pack(anchor="w", pady=(18, 10))

        checkbox_widgets: list[ctk.CTkCheckBox] = []
        rows = ctk.CTkFrame(body, fg_color="transparent")
        rows.pack(fill="x")
        for stem_name, label, color in STEM_ROWS:
            row = ctk.CTkFrame(rows, fg_color=COLORS["field"], corner_radius=10)
            row.pack(fill="x", pady=4)
            row_inner = ctk.CTkFrame(row, fg_color="transparent")
            row_inner.pack(fill="x", padx=12, pady=10)
            _icon(row_inner, STEM_ICONS[stem_name], 16, color, COLORS["field"]).pack(side="left", padx=(0, 10))
            checkbox = ctk.CTkCheckBox(
                row_inner,
                text=label,
                variable=stem_vars[stem_name],
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent"],
                checkmark_color=COLORS["accent_text"],
                border_color=COLORS["line_strong"],
                text_color=COLORS["text"],
                corner_radius=6,
                font=("Segoe UI", 11),
            )
            checkbox.pack(side="left")
            checkbox_widgets.append(checkbox)

        message = ctk.CTkLabel(body, text="", text_color=COLORS["error"], font=("Segoe UI", 10), anchor="w", justify="left", wraplength=320)
        message.pack(fill="x", pady=(14, 0))

        results_frame = ctk.CTkFrame(body, fg_color="transparent")
        results_frame.pack(fill="both", expand=True, pady=(6, 0))

        export_button = ctk.CTkButton(
            body,
            text="Export 4 selected",
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["accent_text"],
            text_color_disabled=COLORS["muted2"],
            corner_radius=10,
            font=("Segoe UI", 12, "bold"),
            height=42,
        )
        export_button.pack(fill="x", pady=(14, 0), side="bottom")

        def refresh_button_label(*_args) -> None:
            count = sum(1 for var in stem_vars.values() if var.get())
            export_button.configure(text=f"Export {count} selected", state="normal" if count else "disabled")

        for var in stem_vars.values():
            var.trace_add("write", refresh_button_label)
        refresh_button_label()

        def start_export() -> None:
            chosen = filedialog.askdirectory(parent=self.root, title="Choose an export folder")
            if not chosen:
                return
            selected_names = [name for name, var in stem_vars.items() if var.get()]
            ok = self.mixer_controller.export_stems(selected_names, chosen)
            if not ok:
                message.configure(text="Export is unavailable because the mixer session has ended.")
                return
            message.configure(text="Exporting…")
            select_all.configure(state="disabled")
            export_button.configure(state="disabled")
            for checkbox in checkbox_widgets:
                checkbox.configure(state="disabled")
            self.root.after(100, lambda: self._poll_export_dialog(overlay, message, results_frame, export_button))

        export_button.configure(command=start_export)

    def _set_all_export_vars(self, stem_vars: dict[str, tk.BooleanVar], value: bool) -> None:
        for var in stem_vars.values():
            var.set(value)

    def _poll_export_dialog(self, overlay: ctk.CTkFrame, message: ctk.CTkLabel, results_frame: ctk.CTkFrame, export_button: ctk.CTkButton) -> None:
        if not overlay.winfo_exists():
            return
        state = self.mixer_controller.state
        if state.export_phase == "running":
            self.root.after(100, lambda: self._poll_export_dialog(overlay, message, results_frame, export_button))
            return
        if state.export_phase != "done":
            return

        message.configure(text="Export complete")
        labels = {stem_name: label for stem_name, label, _color in STEM_ROWS}
        for stem_name, path, code in state.export_results:
            display_name = labels.get(stem_name, stem_name)
            if path is not None:
                text = f"{display_name}: ✓ {path.name}"
            else:
                text = f"{display_name}: ✗ {code}"
            ctk.CTkLabel(results_frame, text=text, text_color=COLORS["text"], font=("Segoe UI", 10), anchor="w", justify="left").pack(
                anchor="w", pady=1
            )

        export_button.configure(text="Close", state="normal", command=self._close_export_overlay)

    def _close_export_overlay(self) -> None:
        if self.export_overlay is not None and self.export_overlay.winfo_exists():
            self.export_overlay.destroy()
        self.export_overlay = None

    def _show_view(self, view: str) -> None:
        if view == "mixer":
            self.separation_view.grid_remove()
            self.mixer_view.grid(row=0, column=0, sticky="nsew")
            self.root.geometry(f"1120x{720 + NAV_HEIGHT + 1}")
            self.root.minsize(800, 621)
        else:
            self.mixer_view.grid_remove()
            self.separation_view.grid(row=0, column=0, sticky="nsew")
            self.root.geometry(f"960x{620 + NAV_HEIGHT + 1}")
            self.root.minsize(800, 621)
        self._view = view
        self._set_active_tab(view)

    def _render_state(self, state: GuiState) -> None:
        self.input_value_label.configure(text=Path(state.input_file).name if state.input_file else "No file selected")
        self.status_headline.set(state.headline)
        self.status_detail.set(state.detail)
        if state.phase == "error":
            icon_kind, icon_color = "alert", COLORS["error"]
        elif state.phase == "success":
            icon_kind, icon_color = "check", COLORS["success"]
        else:
            icon_kind, icon_color = "dot", COLORS["muted"]
        _draw_icon(self.status_icon, icon_kind, 20, icon_color)
        running = state.phase == "running"
        self.input_button.configure(state="disabled" if running else "normal")
        self.action.configure(state="normal" if state.can_start else "disabled", text="Separating…" if running else "Separate into 4 stems")

    def _render_mixer_state(self, state: MixerState) -> None:
        if self._closed:
            return
        if self._preview_frame is not None and int(state.position) == self._preview_frame:
            self._preview_frame = None
        model = MixerViewModel.from_controller_state(state, preview_frame=self._preview_frame)
        self._mixer_model = model
        self.mixer_headline.set(state.headline)
        self.mixer_detail.set(state.detail)
        self.mixer_time.set(f"{format_time(model.position_seconds)} / {format_time(model.duration_seconds)}")
        self.play_button.set_icon("pause" if state.playing else "play")
        self.play_button.set_enabled(state.can_play)
        export_ready = state.session is not None and state.phase in {"ready", "playing"}
        self.export_button.configure(state="normal" if export_ready else "disabled")
        self._tab_buttons["export"].configure(state="normal" if export_ready else "disabled")
        self.load_folder_button.configure(state="disabled" if state.phase == "loading" else "normal")
        self._syncing_controls = True
        try:
            for stem_name, widgets in self._lane_widgets.items():
                setting = next((item for item in state.settings.settings if item.name == stem_name), None)
                enabled = state.session is not None and state.phase in {"ready", "playing"}
                widgets["scale"].configure(state="normal" if enabled else "disabled")
                widgets["mute"].configure(state="normal" if enabled else "disabled")
                widgets["solo"].configure(state="normal" if enabled else "disabled")
                if setting is not None:
                    percent = setting.gain * 100.0
                    widgets["scale"].set(percent)
                    widgets["percent"].configure(text=f"{percent:.0f}%")
                    widgets["mute"].configure(fg_color=COLORS["error"] if setting.muted else COLORS["field"])
                    widgets["solo"].configure(fg_color=widgets["color"] if setting.solo else COLORS["field"])
        finally:
            self._syncing_controls = False
        if state.session is not self._waveform_session:
            self._waveform_session = state.session
            self.root.after_idle(self._draw_waveforms)
        self._draw_playheads()
        self._draw_timeline()

    def _draw_waveforms(self) -> None:
        session = self._waveform_session
        for stem_name, canvas in self._waveform_canvases.items():
            canvas.delete("waveform")
            if session is None:
                self._draw_playheads()
                continue
            try:
                peaks = session.peaks_for(stem_name)
            except (AttributeError, KeyError):
                continue
            width = max(1, canvas.winfo_width())
            height = max(1, canvas.winfo_height())
            center = height / 2.0
            half = max(2.0, center - 8.0)
            color = next(item[2] for item in STEM_ROWS if item[0] == stem_name)
            if peaks:
                for x in range(width):
                    index = min(len(peaks) - 1, int(x * len(peaks) / width))
                    amplitude = min(1.0, max(0.0, float(peaks[index])))
                    canvas.create_line(x, center - amplitude * half, x, center + amplitude * half, fill=color, tags="waveform")
            canvas.create_line(0, center, width, center, fill=COLORS["line"], tags="waveform")
        self._draw_playheads()

    def _draw_playheads(self) -> None:
        ratio = self._mixer_model.preview_ratio
        for canvas in self._waveform_canvases.values():
            canvas.delete("playhead")
            width = canvas.winfo_width()
            height = canvas.winfo_height()
            if width <= 1 or height <= 1:
                continue
            x = max(0.0, min(float(width - 1), ratio * width))
            canvas.create_line(x, 0, x, height, fill=COLORS["accent"], width=2, tags="playhead")

    def _rounded_bar(self, canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, radius: float, color: str) -> None:
        if x1 <= x0:
            return
        radius = min(radius, (x1 - x0) / 2)
        canvas.create_oval(x0, y0, x0 + 2 * radius, y1, fill=color, outline="")
        canvas.create_oval(x1 - 2 * radius, y0, x1, y1, fill=color, outline="")
        if x1 - radius > x0 + radius:
            canvas.create_rectangle(x0 + radius, y0, x1 - radius, y1, fill=color, outline="")

    def _draw_timeline(self) -> None:
        self.timeline.delete("all")
        width = self.timeline.winfo_width()
        height = self.timeline.winfo_height()
        if width <= 1:
            return
        track_h = 8
        y0, y1 = (height - track_h) / 2, (height - track_h) / 2 + track_h
        self._rounded_bar(self.timeline, 0, y0, width, y1, track_h / 2, COLORS["field"])
        ratio = self._mixer_model.preview_ratio
        fill_w = max(track_h, ratio * width)
        self._rounded_bar(self.timeline, 0, y0, fill_w, y1, track_h / 2, COLORS["accent"])
        cx = max(8.0, min(float(width - 8), ratio * width))
        self.timeline.create_oval(cx - 8, height / 2 - 8, cx + 8, height / 2 + 8, fill=COLORS["text"], outline=COLORS["window"], width=3)

    def _seek_frame_at(self, event) -> int:
        widget = event.widget
        width = max(1, widget.winfo_width())
        return self._mixer_model.frame_for_pixel(event.x, width)

    def _seek_press(self, event) -> None:
        if not self._mixer_model.frame_count or not self.mixer_controller.state.can_play:
            return
        self._seek_dragging = True
        self._preview_frame = self._seek_frame_at(event)
        self._mixer_model = self._mixer_model.with_preview(self._preview_frame)
        self._draw_playheads()
        self._draw_timeline()

    def _seek_motion(self, event) -> None:
        if not self._seek_dragging:
            return
        self._preview_frame = self._seek_frame_at(event)
        self._mixer_model = self._mixer_model.with_preview(self._preview_frame)
        self._draw_playheads()
        self._draw_timeline()

    def _seek_release(self, event) -> None:
        if not self._seek_dragging:
            return
        self._seek_dragging = False
        target = self._seek_frame_at(event)
        self._preview_frame = target
        self._mixer_model = self._mixer_model.with_preview(target)
        if not self.mixer_controller.seek(target):
            self._preview_frame = None
        self._draw_playheads()
        self._draw_timeline()

    def _toggle_play(self) -> None:
        if self.mixer_controller.state.playing:
            self.mixer_controller.pause()
        else:
            self.mixer_controller.play()

    def _volume_changed(self, stem_name: str, value: float | str) -> None:
        percent = float(value)
        widgets = self._lane_widgets.get(stem_name)
        if widgets:
            widgets["percent"].configure(text=f"{percent:.0f}%")
        if not self._syncing_controls and self.mixer_controller.state.session is not None:
            self.mixer_controller.set_volume_percent(stem_name, percent)

    def _toggle_mute(self, stem_name: str) -> None:
        self.mixer_controller.toggle_mute(stem_name)

    def _toggle_solo(self, stem_name: str) -> None:
        self.mixer_controller.toggle_solo(stem_name)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.mixer_controller.close()
        except AttributeError:
            pass
        # The playback engine must be closed first: its worker thread join is
        # what releases the SoundFile read handles that would otherwise turn
        # a Windows cache-directory cleanup into a sharing violation.
        for directory in self._cache_directories:
            stem_cache.discard(directory)
        self.root.destroy()

    def _drain_events(self) -> None:
        if self._closed:
            return
        try:
            while True:
                self._events.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--self-test"]:
        if getattr(sys, "frozen", False):
            frozen_worker_path()
        return 0
    if arguments:
        raise ValueError(f"Unsupported arguments: {' '.join(arguments)}")
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    TkinterDnD.require(root)
    StemslayerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
