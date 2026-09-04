"""Tkinter/customtkinter studio workspace for separating and auditioning Windows stems."""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from tkinterdnd2 import DND_FILES, TkinterDnD

from SeparationWorker.demucs_adapter import frozen_worker_path
from SeparationWorker.engine import stem_cache
from SeparationWorker.engine.mixer import MixerSnapshot
from SeparationWorker.engine.stem_profile import LEGACY_PROFILE
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.gui_controller import GuiState, SeparationController
from SeparationWorker.history import HistoryStore, LibraryState, SplitLibraryController, TrackRecord
from SeparationWorker.instance_lock import acquire_single_instance
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

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"
# One scale for every view. Nothing renders below the label step: on a
# 1080p+ desktop 9-10pt reads as fine print, which is what the old
# per-widget sizes had drifted into.
TYPE_SCALE = {
    "label": 11,
    "caption": 12,
    "body": 12,
    "title": 14,
    "heading": 18,
    "display": 22,
}


def ui_font(role: str, *, bold: bool = False, mono: bool = False) -> tuple:
    family = MONO_FAMILY if mono else FONT_FAMILY
    size = TYPE_SCALE[role]
    if mono:
        size -= 1
    return (family, size, "bold") if bold else (family, size)


# CTkFrame has no alpha channel, so a "tinted" circular button (a colour wash
# over the window background, as in a web mockup) has to be a precomputed
# solid hex: channel = window*(1-alpha) + tint*alpha, alpha 0.16 at rest and
# 0.08 for the disabled Remove wash, against COLORS["window"].
LIBRARY_ACTION_TINTS = {
    "accent_rest": "#2A2720",
    "accent_hover": "#3D372D",
    "error_rest": "#2A1D1C",
    "error_hover": "#3D2725",
    "error_disabled": "#1C1414",
}


# Presentation for every lane a registered profile can publish. Unknown lanes
# fall back to a readable label and the neutral colour rather than failing to
# render, so a future profile never produces a blank row.
LANE_COLORS = {
    "vocals": "#A292A3",
    "drums": "#B6927B",
    "bass": "#87A987",
    "lead_guitar": "#C09A6B",
    "rhythm_guitar": "#7F94A8",
    "guitar_center": "#C09A6B",
    "guitar_sides": "#7F94A8",
    "other": "#8BA4B0",
}
LANE_ICONS = {"vocals": "mic", "drums": "drum", "bass": "bass"}
LANE_FALLBACK_COLOR = "#8BA4B0"
LANE_FALLBACK_ICON = "layers"
ABSENT_LANE_SUFFIX = "NOT IN THIS TRACK"
CHANNEL_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight"}

# The separation view places its content, so anything past the window bottom
# is clipped with no scrollbar and the primary action becomes unreachable.
# This height must fit the tallest profile's content plus a bottom margin
# matching the top one; test_gui_lane_rendering measures and enforces it.
SEPARATION_CONTENT_TOP = 30
LIBRARY_MAX_WIDTH = 1100
SEPARATION_VIEW_HEIGHT = 764

# The mixer sizes itself to the lane strip instead of squeezing the strip into
# a fixed window. Every lane keeps LANE_HEIGHT whatever the profile publishes,
# because a shared height that shrinks per lane silently clips the controls at
# the bottom of each lane, and Tk reports no error when it does.
LANE_CONTENT_HEIGHT = 96
LANE_INNER_PAD = 10
LANE_GAP = 12
# One continuous line across the whole strip, so the position reads at a
# glance instead of being redrawn per lane and broken by every lane gap.
PLAYHEAD_WIDTH = 2
SKIP_SECONDS = 10.0
LANE_HEIGHT = LANE_CONTENT_HEIGHT + 2 * LANE_INNER_PAD
# The mixer height that is not lane strip. This is only the fallback used
# before the widgets exist; the live value is measured from the header and the
# transport, because a hand-carried constant goes stale the moment either of
# them gains a row and then quietly squeezes the strip.
MIXER_CHROME_HEIGHT = 261
HEADER_PAD = (22, 14)
TRANSPORT_PAD = (16, 20)
MIXER_MIN_LANES = 4
# Leave room for the taskbar and window decorations when capping to the screen.
SCREEN_MARGIN = 80


def lane_strip_height(lane_count: int) -> int:
    """Return the height the lane strip needs for this many lanes."""
    lane_count = max(1, lane_count)
    return lane_count * LANE_HEIGHT + (lane_count - 1) * LANE_GAP


def mixer_view_height(lane_count: int, chrome: int = MIXER_CHROME_HEIGHT) -> int:
    """Return the mixer height that shows every lane without clipping."""
    return chrome + lane_strip_height(max(lane_count, MIXER_MIN_LANES))



def channel_word(count: int) -> str:
    return CHANNEL_WORDS.get(count, str(count))


def lane_id(stem_name: str) -> str:
    return Path(stem_name).stem.lower()


def lane_label(stem_name: str) -> str:
    return lane_id(stem_name).replace("_", " ").upper()


def lane_color(stem_name: str) -> str:
    return LANE_COLORS.get(lane_id(stem_name), LANE_FALLBACK_COLOR)


def lane_icon(stem_name: str) -> str:
    return LANE_ICONS.get(lane_id(stem_name), LANE_FALLBACK_ICON)
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


def space_toggles_playback(view: str, can_play: bool) -> bool:
    """Report whether a Space press should toggle Mixer transport."""
    return view == "mixer" and can_play


def format_time(seconds: float) -> str:
    """Format a timeline value without exposing audio implementation details."""
    seconds = max(0.0, float(seconds))
    minutes, remainder = divmod(seconds, 60.0)
    return f"{int(minutes):02d}:{remainder:05.2f}"


def library_row_detail(record: TrackRecord, *, profile_name: str | None = None) -> str:
    """Compose a library row's detail column from values every record carries.

    BPM, key, and genre are left out on purpose: the catalog never computes
    the first two, and tags rarely carry the third, so printing them only
    filled the row with dashes.
    """
    duration = format_time(record.duration_seconds).split(".")[0] if record.duration_seconds else "—"
    try:
        date = datetime.fromisoformat(record.created_at_utc.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        date = "—"
    parts = [profile_name] if profile_name else []
    parts.extend((duration, date))
    return "  ·  ".join(parts)


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
    elif kind == "trash":
        lid_y = size * 0.3
        canvas.create_line(size * 0.2, lid_y, size * 0.8, lid_y, fill=color, width=2, tags="icon")
        canvas.create_rectangle(size * 0.4, size * 0.12, size * 0.6, lid_y, outline=color, width=2, tags="icon")
        canvas.create_rectangle(size * 0.28, lid_y, size * 0.72, size * 0.86, outline=color, width=2, tags="icon")
        canvas.create_line(size * 0.42, size * 0.42, size * 0.42, size * 0.74, fill=color, width=1.6, tags="icon")
        canvas.create_line(size * 0.58, size * 0.42, size * 0.58, size * 0.74, fill=color, width=1.6, tags="icon")
    elif kind == "search":
        canvas.create_oval(size * 0.14, size * 0.14, size * 0.62, size * 0.62, outline=color, width=2, tags="icon")
        canvas.create_line(size * 0.58, size * 0.58, size * 0.86, size * 0.86, fill=color, width=2, tags="icon")
    elif kind == "plus":
        canvas.create_line(size * 0.5, size * 0.18, size * 0.5, size * 0.82, fill=color, width=2.4, tags="icon")
        canvas.create_line(size * 0.18, size * 0.5, size * 0.82, size * 0.5, fill=color, width=2.4, tags="icon")
    elif kind in {"rewind", "forward"}:
        # One glyph drawn in both directions, so the pair always reads as a
        # matched set instead of two hand-tuned triangles that drift apart.
        top, bottom, middle = size * 0.24, size * 0.76, size / 2
        base, tip = size * 0.08, size * 0.46
        for offset in (0.0, size * 0.44):
            x_base, x_tip = base + offset, tip + offset
            if kind == "rewind":
                x_base, x_tip = size - x_base, size - x_tip
            canvas.create_polygon(
                x_base, top, x_base, bottom, x_tip, middle, fill=color, outline="", tags="icon"
            )
    elif kind == "loop":
        canvas.create_arc(
            size * 0.16, size * 0.2, size * 0.84, size * 0.88,
            start=20, extent=300, style="arc", outline=color, width=2, tags="icon",
        )
        canvas.create_polygon(
            size * 0.62, size * 0.12, size * 0.92, size * 0.26, size * 0.62, size * 0.4,
            fill=color, outline="", tags="icon",
        )
    elif kind in {"volume", "volume_off"}:
        canvas.create_polygon(
            size * 0.1, size * 0.36, size * 0.28, size * 0.36, size * 0.48, size * 0.16,
            size * 0.48, size * 0.84, size * 0.28, size * 0.64, size * 0.1, size * 0.64,
            fill=color, outline="", tags="icon",
        )
        if kind == "volume":
            for extent in (0.62, 0.82):
                canvas.create_arc(
                    size * (1.16 - extent), size * (0.5 - extent / 2),
                    size * (0.16 + extent), size * (0.5 + extent / 2),
                    start=-55, extent=110, style="arc", outline=color, width=2, tags="icon",
                )
        else:
            canvas.create_line(
                size * 0.62, size * 0.36, size * 0.92, size * 0.66, fill=color, width=2, tags="icon"
            )
            canvas.create_line(
                size * 0.92, size * 0.36, size * 0.62, size * 0.66, fill=color, width=2, tags="icon"
            )


def _icon(parent: tk.Widget, kind: str, size: int, color: str, bg: str) -> tk.Canvas:
    canvas = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
    _draw_icon(canvas, kind, size, color)
    return canvas


class _RoundButton:
    """A circular, icon-only transport button (plain Tk/CTk has no built-in one)."""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        diameter: int,
        fg: str,
        fg_disabled: str,
        icon_color: str,
        command: Callable[[], None],
        icon: str = "play",
        icon_size: int | None = None,
        active_color: str | None = None,
        hover_fg: str | None = None,
    ):
        self._fg = fg
        self._fg_disabled = fg_disabled
        self._icon_color = icon_color
        self._active_color = active_color or icon_color
        self._hover_fg = hover_fg
        self._active = False
        self._icon_kind = icon
        self._command = command
        self._enabled = True
        self.frame = ctk.CTkFrame(parent, width=diameter, height=diameter, corner_radius=diameter // 2, fg_color=fg)
        self.frame.pack_propagate(False)
        self._size = icon_size or diameter
        self.canvas = tk.Canvas(
            self.frame, width=self._size, height=self._size, bg=fg, highlightthickness=0, cursor="hand2"
        )
        self.canvas.pack(expand=True)
        self.canvas.bind("<Button-1>", self._on_click)
        if hover_fg is not None:
            self.canvas.bind("<Enter>", self._on_enter)
            self.canvas.bind("<Leave>", self._on_leave)
        self._redraw()

    def _redraw(self) -> None:
        if not self._enabled:
            color = COLORS["muted2"]
        else:
            color = self._active_color if self._active else self._icon_color
        _draw_icon(self.canvas, self._icon_kind, self._size, color)

    def set_active(self, active: bool) -> None:
        """Mark a toggle as engaged, so a latched button looks latched."""
        self._active = bool(active)
        self._redraw()

    def set_icon(self, kind: str) -> None:
        self._icon_kind = kind
        self._redraw()

    def _on_click(self, _event=None) -> None:
        if self._enabled:
            self._command()

    def _on_enter(self, _event=None) -> None:
        if self._enabled and self._hover_fg is not None:
            self.frame.configure(fg_color=self._hover_fg)
            self.canvas.configure(bg=self._hover_fg)

    def _on_leave(self, _event=None) -> None:
        if self._enabled:
            self.frame.configure(fg_color=self._fg)
            self.canvas.configure(bg=self._fg)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        color = self._fg if enabled else self._fg_disabled
        self.frame.configure(fg_color=color)
        self.canvas.configure(bg=color, cursor="hand2" if enabled else "arrow")
        self._redraw()

    def is_enabled(self) -> bool:
        return self._enabled

    def invoke(self) -> None:
        """Fire the click as if the mouse had landed on the canvas -- for tests."""
        self._on_click()


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
    absent_names: tuple[str, ...] = ()

    @classmethod
    def from_controller_state(cls, state: MixerState, *, preview_frame: int | None = None):
        session = state.session
        sample_rate = int(getattr(session, "sample_rate", 0) or 0)
        frame_count = max(0, int(state.frame_count))
        position = max(0, min(int(state.position), frame_count))
        if preview_frame is not None:
            preview_frame = max(0, min(int(preview_frame), frame_count))
        return cls(
            stem_names=tuple(state.lane_names),
            position=position,
            frame_count=frame_count,
            sample_rate=sample_rate,
            playing=bool(state.playing),
            settings=state.settings,
            preview_frame=preview_frame,
            absent_names=tuple(state.absent_lanes),
        )

    @property
    def lane_names(self) -> tuple[str, ...]:
        return self.stem_names

    def is_absent(self, stem_name: str) -> bool:
        """Report whether a lane was declared absent for this track."""
        return lane_id(stem_name) in self.absent_names

    @property
    def lane_rows(self) -> tuple[tuple[str, str, str, bool], ...]:
        """Return one renderable row per published lane, in profile order.

        An absent lane keeps its row and is labeled. Hiding it would erase the
        fact that this track simply has no lead guitar, which is exactly the
        thing the user opened the mixer to find out.
        """
        return tuple(
            (
                name,
                f"{lane_label(name)} · {ABSENT_LANE_SUFFIX}" if self.is_absent(name) else lane_label(name),
                lane_color(name),
                self.is_absent(name),
            )
            for name in self.stem_names
        )

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
        self._lanes_container: ctk.CTkScrollableFrame | None = None
        self._lane_frames: list[ctk.CTkFrame] = []
        self._playhead: tk.Frame | None = None
        self._mixer_header: ctk.CTkFrame | None = None
        self._mixer_transport: ctk.CTkFrame | None = None
        self._lane_rows: tuple[tuple[str, str, str, bool], ...] = ()
        self._chips_frame: ctk.CTkFrame | None = None
        self._chip_lane_ids: tuple[str, ...] = ()
        self._profile_choices: dict[str, str] = {}
        self._mixer_model = MixerViewModel()
        self._syncing_controls = False
        self._cache_directories: list[Path] = []
        self._library_state = LibraryState()
        self._profile_dialog = None
        self.export_overlay: ctk.CTkFrame | None = None

        self.status_headline = tk.StringVar()
        self.status_detail = tk.StringVar()
        self.hero_headline = tk.StringVar()
        self.hero_subtitle = tk.StringVar()
        self.mixer_title = tk.StringVar(value=f"{channel_word(4)} stems. One shared timeline.")
        self.mixer_headline = tk.StringVar(value="Choose a four-stem folder")
        self.mixer_detail = tk.StringVar(value="Load vocals.wav, drums.wav, bass.wav, and other.wav to begin.")
        self.mixer_position_time = tk.StringVar(value="00:00.00")
        self.mixer_duration_time = tk.StringVar(value="00:00.00")
        self._master_percent_before_mute = 100

        self._build()
        self.mixer_controller = MixerController(
            dispatch=self._events.put,
            on_change=self._render_mixer_state,
        )
        self.controller = SeparationController(
            dispatch=self._events.put,
            on_change=self._render_state,
            on_success=self._separation_succeeded,
        )
        self.history_store = HistoryStore()
        self.history_store.recover_unfinished()
        # Safe only because acquire_single_instance() in main() guarantees
        # exactly one process reaches this code (ARC-01); a second process
        # racing this cleanup could delete another job's in-progress copy.
        self.history_store.purge_input_copies()
        self.library_controller = SplitLibraryController(
            self.history_store,
            dispatch=self._events.put,
            on_change=self._render_library,
            on_success=self._library_succeeded,
            on_model_progress=self._model_download_progress,
        )
        self._render_state(self.controller.state)
        self._render_mixer_state(self.mixer_controller.state)
        self._show_view("separation")
        # Bound once, app-wide: the guard (space_toggles_playback) does the
        # scoping on every press, so this never needs bind/unbind lifecycle
        # tied to view switches. `add="+"` appends rather than replacing, so
        # a future Space consumer on another bindtag is not silently dropped.
        self.root.bind_all("<space>", self._space_pressed, add="+")
        self.root.after(50, self._drain_events)
        threading.Thread(
            target=stem_cache.sweep_orphans, name="stemslayer-stem-cache-sweep", daemon=True
        ).start()
        threading.Thread(
            target=self._validate_library, name="stemslayer-library-validation", daemon=True
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

        ctk.CTkLabel(nav, text=APP_NAME.upper(), text_color=COLORS["text"], font=ui_font("title", bold=True)).grid(
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
                font=ui_font("body", bold=True),
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
        center.place(relx=0.5, y=SEPARATION_CONTENT_TOP, anchor="n")
        self._separation_center = center

        header = ctk.CTkFrame(center, fg_color=COLORS["window"], corner_radius=0)
        header.pack(fill="x")
        ctk.CTkLabel(
            header, textvariable=self.hero_headline, text_color=COLORS["text"], font=ui_font("display", bold=True)
        ).pack()
        ctk.CTkLabel(
            header,
            textvariable=self.hero_subtitle,
            text_color=COLORS["muted"],
            font=ui_font("caption"),
        ).pack(pady=(6, 0))

        ctk.CTkLabel(center, text="INPUT AUDIO", text_color=COLORS["muted"], font=ui_font("label", bold=True), anchor="w").pack(
            fill="x", pady=(28, 6)
        )
        dropzone = ctk.CTkFrame(center, fg_color=COLORS["surface_alt"], corner_radius=12, border_width=1, border_color=COLORS["line_strong"])
        dropzone.pack(fill="x")
        dz_inner = ctk.CTkFrame(dropzone, fg_color="transparent")
        dz_inner.pack(pady=22)
        _icon(dz_inner, "audio", 26, COLORS["muted"], COLORS["surface_alt"]).pack()
        self.input_value_label = ctk.CTkLabel(dz_inner, text="No file selected", text_color=COLORS["text"], font=ui_font("title", bold=True))
        self.input_value_label.pack(pady=(10, 2))
        drop_hint = ctk.CTkLabel(
            dz_inner,
            text="Drag and drop an audio file, or click to browse",
            text_color=COLORS["muted2"],
            font=ui_font("caption"),
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
            font=ui_font("body", bold=True),
        )
        self.input_button.pack(pady=(12, 0))
        for widget in (dropzone, dz_inner):
            widget.bind("<Button-1>", lambda _event: self._pick_input())
        self._register_drop_zone(dropzone)

        profile_label = ctk.CTkLabel(center, text="PROFILE", text_color=COLORS["muted"], font=ui_font("label", bold=True), anchor="w")
        profile_label.pack(
            fill="x", pady=(24, 6)
        )
        # available_profiles() is static so the selector can be built before
        # the controller exists; unavailable profiles are listed rather than
        # hidden, and selecting one explains why it cannot run.
        self._profile_choices = {
            profile.display_name: profile.profile_id
            for profile in SeparationController.available_profiles()
        }
        self.profile_selector = ctk.CTkSegmentedButton(
            center,
            values=list(self._profile_choices),
            command=self._profile_selected,
            fg_color=COLORS["field"],
            selected_color=COLORS["accent"],
            selected_hover_color=COLORS["accent"],
            unselected_color=COLORS["field"],
            unselected_hover_color=COLORS["line"],
            text_color=COLORS["text"],
            font=ui_font("body", bold=True),
        )
        self.profile_selector.pack(fill="x")

        channels_label = ctk.CTkLabel(center, text="CHANNELS TO EXTRACT", text_color=COLORS["muted"], font=ui_font("label", bold=True), anchor="w")
        channels_label.pack(
            fill="x", pady=(18, 6)
        )
        self._chips_frame = ctk.CTkFrame(center, fg_color="transparent")
        self._chips_frame.pack(fill="x")
        self._build_chips(LEGACY_PROFILE)

        self.status_banner = ctk.CTkFrame(center, fg_color=COLORS["surface"], corner_radius=10)
        self.status_banner.pack(fill="x", pady=(28, 0))
        banner_inner = ctk.CTkFrame(self.status_banner, fg_color="transparent")
        banner_inner.pack(fill="x", padx=16, pady=14)
        self.status_icon = _icon(banner_inner, "dot", 20, COLORS["muted"], COLORS["surface"])
        self.status_icon.pack(side="left", padx=(0, 12))
        status_copy = ctk.CTkFrame(banner_inner, fg_color="transparent")
        status_copy.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            status_copy, textvariable=self.status_headline, text_color=COLORS["text"], font=ui_font("title", bold=True), anchor="w"
        ).pack(fill="x")
        ctk.CTkLabel(
            status_copy,
            textvariable=self.status_detail,
            text_color=COLORS["muted"],
            font=ui_font("caption"),
            anchor="w",
            justify="left",
            wraplength=460,
        ).pack(fill="x", pady=(2, 0))

        footer = ctk.CTkFrame(center, fg_color="transparent")
        footer.pack(fill="x", pady=(20, 40))
        self.action = ctk.CTkButton(
            footer,
            text=f"Separate into {len(STEM_NAMES)} stems",
            command=self._start,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["accent_text"],
            text_color_disabled=COLORS["muted2"],
            corner_radius=8,
            font=ui_font("body", bold=True),
            height=40,
        )
        self.action.pack(side="right")
        # The durable empty state only asks for a song. Profile selection moves
        # to the compact dialog opened after file selection.
        for legacy_control in (
            profile_label,
            self.profile_selector,
            channels_label,
            self._chips_frame,
            self.status_banner,
            footer,
        ):
            legacy_control.pack_forget()
        self._build_library_view(shell)

    def _build_library_view(self, shell) -> None:
        panel = ctk.CTkFrame(shell, fg_color=COLORS["window"], corner_radius=0)
        self._library_panel = panel
        # The panel fills the window, but a four-column list gains nothing
        # from 2000px: on a wide monitor the actions drift a screen away from
        # the song they belong to. Content stays in a centered, capped column.
        content = ctk.CTkFrame(panel, fg_color="transparent", width=LIBRARY_MAX_WIDTH)
        content.pack_propagate(False)
        content.place(relx=0.5, rely=0, anchor="n", relheight=1)
        panel.bind(
            "<Configure>",
            lambda event: content.configure(width=min(event.width, LIBRARY_MAX_WIDTH)),
        )

        header = ctk.CTkFrame(content, fg_color="transparent")
        header.pack(fill="x", padx=32, pady=(24, 18))
        copy = ctk.CTkFrame(header, fg_color="transparent")
        copy.pack(side="left")
        ctk.CTkLabel(copy, text="Your split library", text_color=COLORS["text"], font=ui_font("display", bold=True), anchor="w").pack(fill="x")
        ctk.CTkLabel(copy, text="Stored locally. Drop an audio file anywhere here to add a song.", text_color=COLORS["muted"], font=ui_font("caption"), anchor="w").pack(fill="x", pady=(4, 0))
        # SEC-01: shows first-use model download progress; empty otherwise.
        self.library_model_status = tk.StringVar()
        ctk.CTkLabel(
            copy, textvariable=self.library_model_status, text_color=COLORS["muted"],
            font=ui_font("label"), anchor="w",
        ).pack(fill="x", pady=(2, 0))
        add_song = ctk.CTkFrame(header, fg_color=COLORS["accent"], corner_radius=8, height=36)
        add_song.pack_propagate(False)
        add_song.pack(side="right")
        add_song_inner = ctk.CTkFrame(add_song, fg_color="transparent")
        add_song_inner.pack(expand=True, padx=16)
        add_song_icon = _icon(add_song_inner, "plus", 14, COLORS["accent_text"], COLORS["accent"])
        add_song_icon.pack(side="left", padx=(0, 6))
        add_song_label = ctk.CTkLabel(
            add_song_inner, text="Add song", text_color=COLORS["accent_text"], font=ui_font("body", bold=True),
        )
        add_song_label.pack(side="left")
        for widget in (add_song, add_song_inner, add_song_icon, add_song_label):
            widget.bind("<Button-1>", lambda _event: self._pick_input())
            widget.configure(cursor="hand2")

        # A transparent toolbar directly on the window background, not a
        # bordered "surface" box -- the boxed controls plus boxed panel read
        # as visually loud with a real catalogue on screen.
        controls = ctk.CTkFrame(content, fg_color="transparent")
        controls.pack(fill="x", padx=32, pady=(0, 8))
        search_box = ctk.CTkFrame(controls, fg_color="transparent")
        search_box.pack(side="left")
        search_row = ctk.CTkFrame(search_box, fg_color="transparent")
        search_row.pack(fill="x")
        _icon(search_row, "search", 16, COLORS["muted2"], COLORS["window"]).pack(side="left", padx=(0, 6))
        self.library_search = ctk.CTkEntry(
            search_row, placeholder_text="Search title or artist", width=240, height=30,
            fg_color=COLORS["window"], border_width=0, text_color=COLORS["text"], font=ui_font("body"),
        )
        self.library_search.pack(side="left")
        self.library_search.bind("<KeyRelease>", lambda _event: self._library_query())
        ctk.CTkFrame(search_box, height=1, corner_radius=0, fg_color=COLORS["line"]).pack(fill="x", pady=(4, 0))

        self.library_sort = ctk.CTkOptionMenu(
            controls, values=["Newest", "Title", "Duration"], command=lambda _value: self._library_query(),
            width=110, height=30, fg_color=COLORS["window"], button_color=COLORS["window"],
            button_hover_color=COLORS["field"], text_color=COLORS["muted"], dropdown_fg_color=COLORS["surface"],
            font=ui_font("body"), dropdown_font=ui_font("body"),
        )
        self.library_sort.pack(side="right")

        ctk.CTkFrame(content, height=1, corner_radius=0, fg_color=COLORS["line"]).pack(fill="x", padx=32, pady=(0, 4))

        self.library_rows = ctk.CTkScrollableFrame(
            content, fg_color=COLORS["window"], corner_radius=0
        )
        self.library_rows.pack(fill="both", expand=True, padx=(32, 20), pady=(0, 20))
        self._autohide_scrollbar(self.library_rows)
        # Once the catalog replaces the empty-state drop zone, the library
        # itself has to accept files or drag-and-drop silently stops working.
        self._register_drop_zone(panel)

    def _autohide_scrollbar(self, frame: ctk.CTkScrollableFrame) -> None:
        """Show the scrollbar only once its content actually overflows the view.

        CTkScrollableFrame always grids its scrollbar and has no built-in
        auto-hide, so this re-wires the canvas's yscrollcommand -- the normal
        Tk hook a scrollbar's set() already receives -- to toggle it based on
        the (first, last) fractions Tk reports each time content or the
        window is resized.
        """
        canvas = frame._parent_canvas
        scrollbar = frame._scrollbar

        def _on_scroll(first: str, last: str) -> None:
            scrollbar.set(first, last)
            if float(first) <= 0.0 and float(last) >= 1.0:
                scrollbar.grid_remove()
            else:
                scrollbar.grid()

        canvas.configure(yscrollcommand=_on_scroll)

    def _library_query(self) -> None:
        if not hasattr(self, "library_controller"):
            return
        sort = {"Newest": "created_at", "Title": "title", "Duration": "duration"}.get(
            self.library_sort.get(), "created_at"
        )
        self.library_controller.set_query(
            search=self.library_search.get(), profile_id="all", status="all", sort_by=sort
        )

    def _render_library(self, state: LibraryState) -> None:
        if self._closed:
            return
        self._library_state = state
        has_catalog = bool(self.history_store.query()) if hasattr(self, "history_store") else bool(state.tracks)
        if has_catalog:
            self._separation_center.place_forget()
            # Fill the real window instead of floating a fixed-size box --
            # a catalogue view should grow with the window, not leave a dead
            # scrollable region below a handful of rows. Margins live on the
            # panel's own children (see _build_library_view) since CTk's
            # place() rejects fixed width/height offsets.
            self._library_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        else:
            self._library_panel.place_forget()
            self._separation_center.place(relx=0.5, y=SEPARATION_CONTENT_TOP, anchor="n")
        for child in self.library_rows.winfo_children():
            child.destroy()
        if has_catalog and not state.tracks:
            ctk.CTkLabel(
                self.library_rows, text="No songs match your search.",
                text_color=COLORS["muted"], font=ui_font("caption"),
            ).pack(pady=36)
            self._register_drop_zone(self.library_rows)
            return
        profile_names = {profile.profile_id: profile.display_name for profile in SeparationController.available_profiles()}
        # The profile only earns a place in the row once it tells songs apart.
        mixed_profiles = len({record.profile_id for record in state.tracks}) > 1
        last_index = len(state.tracks) - 1
        for index, record in enumerate(state.tracks):
            # A flat divided list, not individual bordered cards -- the row
            # itself carries no background or border; a hairline divider
            # below (skipped after the last row) marks the boundary instead.
            row = ctk.CTkFrame(self.library_rows, fg_color="transparent")
            row.pack(fill="x")
            body = ctk.CTkFrame(row, fg_color="transparent")
            body.pack(fill="x", padx=4, pady=12)
            # One grid per row: the title column is the only one with weight,
            # so it alone absorbs overflow from a long title. Artist, detail,
            # and the action buttons keep their width and stay aligned across
            # rows instead of drifting with the title's length.
            body.grid_columnconfigure(1, weight=1)
            color = COLORS["success"] if record.status == "ready" else COLORS["error"] if record.status == "failed" else COLORS["accent"]
            # 10px, not 8: customtkinter's anti-aliased corner rounding reads
            # as a squared-off blob at very small sizes, and a size equal to
            # its own corner_radius*2 is what actually renders as a full circle.
            dot = ctk.CTkFrame(body, width=10, height=10, corner_radius=5, fg_color=color)
            dot.pack_propagate(False)
            dot.grid(row=0, column=0, padx=(2, 12))
            ctk.CTkLabel(
                body, text=record.title, text_color=COLORS["text"],
                font=ui_font("title", bold=True), anchor="w",
            ).grid(row=0, column=1, sticky="ew")
            artist = record.artist or "Unknown artist"
            ctk.CTkLabel(
                body, text=artist, text_color=COLORS["muted"], font=ui_font("caption"),
                anchor="w", width=170,
            ).grid(row=0, column=2, sticky="w", padx=(16, 0))
            detail = library_row_detail(
                record,
                profile_name=profile_names.get(record.profile_id, record.profile_id) if mixed_profiles else None,
            )
            ctk.CTkLabel(
                body, text=detail, text_color=COLORS["muted2"], font=ui_font("caption"),
                anchor="w", width=190,
            ).grid(row=0, column=3, sticky="w", padx=(16, 0))
            if record.status == "ready":
                open_button = self._library_action_button(
                    body, "play", lambda track_id=record.track_id: self._open_library_track(track_id), danger=False,
                )
                open_button.frame.library_action = "open"
                open_button.frame.library_button = open_button
                open_button.frame.grid(row=0, column=4, padx=(24, 0))
            elif record.status in {"failed", "interrupted", "unavailable"}:
                retry_button = self._library_action_button(
                    body, "loop", lambda track_id=record.track_id: self.library_controller.retry(track_id), danger=False,
                )
                retry_button.frame.library_action = "retry"
                retry_button.frame.library_button = retry_button
                retry_button.frame.grid(row=0, column=4, padx=(24, 0))
            remove_button = self._library_action_button(
                body, "trash", lambda track_id=record.track_id: self._remove_library_track(track_id), danger=True,
            )
            remove_button.set_enabled(record.status not in {"preparing", "processing"})
            remove_button.frame.library_action = "remove"
            remove_button.frame.library_button = remove_button
            remove_button.frame.grid(row=0, column=5, padx=(8, 0))
            if record.error_detail:
                ctk.CTkLabel(
                    row, text=record.error_detail, text_color=COLORS["muted"], font=ui_font("caption"),
                    anchor="w", justify="left", wraplength=720,
                ).pack(fill="x", padx=4, pady=(0, 10))
            if index != last_index:
                ctk.CTkFrame(self.library_rows, height=1, corner_radius=0, fg_color=COLORS["line"]).pack(fill="x")
        self._register_drop_zone(self.library_rows)

    def _validate_library(self) -> None:
        self.history_store.validate_ready()
        self._events.put(self.library_controller.refresh)

    def _build_chips(self, profile) -> None:
        """Render one chip per lane the selected profile publishes.

        The hero copy is written here too, so the promised channel count can
        never drift from the chips actually shown.
        """
        word = channel_word(len(profile.lanes))
        self.hero_headline.set(f"{word} clean channels. One local pass.")
        self.hero_subtitle.set(
            f"Select your source audio, then split it into {word.lower()} clean local stems."
        )
        for child in self._chips_frame.winfo_children():
            child.destroy()
        for index in range(len(self._chip_lane_ids)):
            self._chips_frame.grid_columnconfigure(index, weight=0)
        lanes = profile.lanes
        for index, lane in enumerate(lanes):
            self._chips_frame.grid_columnconfigure(index, weight=1)
            chip = ctk.CTkFrame(
                self._chips_frame, fg_color=COLORS["field"], corner_radius=999, border_width=1, border_color=COLORS["line"]
            )
            chip.grid(row=0, column=index, padx=(0 if index == 0 else 6, 0), sticky="ew")
            inner = ctk.CTkFrame(chip, fg_color="transparent")
            inner.pack(pady=10)
            dot = tk.Canvas(inner, width=8, height=8, bg=COLORS["field"], highlightthickness=0)
            dot.create_oval(0, 0, 8, 8, fill=lane_color(lane.file_name), outline="")
            dot.pack(side="left", padx=(0, 6))
            ctk.CTkLabel(
                inner,
                text=lane.display_name.upper(),
                text_color=COLORS["text"],
                font=ui_font("label", bold=True),
            ).pack(side="left")
        self._chip_lane_ids = profile.lane_ids

    def _profile_selected(self, display_name: str) -> None:
        if self._syncing_controls:
            return
        profile_id = self._profile_choices.get(display_name)
        if profile_id is not None:
            self.controller.set_profile(profile_id)

    def _register_drop_zone(self, widget: tk.Widget) -> None:
        """Register the complete CTk widget tree so every visible drop-zone surface accepts files.

        Safe to call again on a tree that gained children: widgets already
        registered are skipped, only the new ones are wired.
        """
        if not getattr(widget, "stemslayer_drop_registered", False):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._drop_input)
            widget.stemslayer_drop_registered = True
        for child in widget.winfo_children():
            self._register_drop_zone(child)

    def _drop_input(self, event) -> str:
        paths = parse_drop_paths(event.data, self.root.tk.splitlist)
        if paths:
            self._choose_profile(paths[0])
        return getattr(event, "action", "copy")

    def _build_mixer_view(self) -> None:
        shell = self.mixer_view
        shell.grid_columnconfigure(0, weight=1)
        # The lane strip takes the free height; the transport keeps its own.
        shell.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(shell, fg_color=COLORS["window"], corner_radius=0)
        header.grid(row=0, column=0, padx=32, pady=HEADER_PAD, sticky="ew")
        self._mixer_header = header
        header.grid_columnconfigure(0, weight=1)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_box, textvariable=self.mixer_title, text_color=COLORS["text"], font=ui_font("heading", bold=True)
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box,
            textvariable=self.mixer_detail,
            text_color=COLORS["muted2"],
            font=ui_font("caption"),
            anchor="w",
            justify="left",
            wraplength=680,
        ).pack(anchor="w", pady=(3, 0))
        buttons = ctk.CTkFrame(header, fg_color="transparent")
        buttons.grid(row=0, column=1, sticky="e")
        self.load_folder_button = self._outline_button(buttons, "Load stems folder", self._pick_stems_folder)
        self.load_folder_button.grid(row=0, column=0, padx=(0, 8))
        self.export_button = self._outline_button(buttons, "Export", self._open_export_dialog)
        self.export_button.grid(row=0, column=1)

        self._lanes_container = ctk.CTkScrollableFrame(
            shell,
            fg_color=COLORS["window"],
            corner_radius=0,
            scrollbar_button_color=COLORS["line_strong"],
            scrollbar_button_hover_color=COLORS["muted"],
        )
        self._lanes_container.grid(row=1, column=0, padx=(32, 20), pady=0, sticky="nsew")
        self._lanes_container.grid_columnconfigure(0, weight=1)
        # One overlay line, a sibling of the lanes rather than a stroke drawn
        # inside each of them, so it crosses the gaps between lanes unbroken.
        self._playhead = tk.Frame(self._lanes_container, bg=COLORS["accent"], width=PLAYHEAD_WIDTH)
        for event, handler in (
            ("<Button-1>", self._seek_press),
            ("<B1-Motion>", self._seek_motion),
            ("<ButtonRelease-1>", self._seek_release),
        ):
            self._playhead.bind(event, handler)
        self._build_lanes(self._mixer_model.lane_rows)
        self._build_transport(shell)

    def _outline_button(self, parent, text: str, command) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            fg_color="transparent",
            hover_color=COLORS["field"],
            border_width=1,
            border_color=COLORS["line_strong"],
            text_color=COLORS["text"],
            corner_radius=8,
            font=ui_font("body", bold=True),
        )

    def _flat_button(self, parent, icon: str, command, *, diameter: int, icon_size: int) -> _RoundButton:
        return _RoundButton(
            parent,
            diameter=diameter,
            fg=COLORS["window"],
            fg_disabled=COLORS["window"],
            icon_color=COLORS["text"],
            command=command,
            icon=icon,
            icon_size=icon_size,
            active_color=COLORS["accent"],
        )

    def _library_action_button(self, parent, icon: str, command, *, danger: bool) -> _RoundButton:
        """A tinted, icon-only row action (Open/Retry in gold, Remove in red)."""
        rest = LIBRARY_ACTION_TINTS["error_rest" if danger else "accent_rest"]
        hover = LIBRARY_ACTION_TINTS["error_hover" if danger else "accent_hover"]
        return _RoundButton(
            parent,
            diameter=32,
            fg=rest,
            fg_disabled=LIBRARY_ACTION_TINTS["error_disabled"],
            icon_color=COLORS["error"] if danger else COLORS["accent"],
            command=command,
            icon=icon,
            icon_size=18,
            hover_fg=hover,
        )

    def _build_transport(self, shell) -> None:
        """Build the playback bar that sits under the lane strip.

        Every control here drives the playback engine. A transport that shows
        a button it cannot honour is worse than one that shows fewer.
        """
        transport = ctk.CTkFrame(shell, fg_color=COLORS["window"], corner_radius=0)
        transport.grid(row=2, column=0, padx=32, pady=TRANSPORT_PAD, sticky="ew")
        self._mixer_transport = transport
        transport.grid_columnconfigure(0, weight=1)

        cluster = ctk.CTkFrame(transport, fg_color="transparent")
        cluster.grid(row=0, column=0)

        self.master_button = self._flat_button(
            cluster, "volume", self._toggle_master_mute, diameter=32, icon_size=20
        )
        self.master_button.frame.grid(row=0, column=0, padx=(0, 8))
        self.master_slider = ctk.CTkSlider(
            cluster,
            from_=0,
            to=100,
            width=88,
            height=12,
            fg_color=COLORS["field"],
            progress_color=COLORS["muted"],
            button_color=COLORS["text"],
            button_hover_color=COLORS["text"],
            command=self._master_volume_changed,
        )
        self.master_slider.set(100)
        self.master_slider.grid(row=0, column=1, padx=(0, 30))

        self.rewind_button = self._flat_button(
            cluster, "rewind", lambda: self._skip(-SKIP_SECONDS), diameter=40, icon_size=22
        )
        self.rewind_button.frame.grid(row=0, column=2, padx=(0, 12))
        self.play_button = _RoundButton(
            cluster,
            diameter=52,
            fg=COLORS["accent"],
            fg_disabled=COLORS["field"],
            icon_color=COLORS["accent_text"],
            command=self._toggle_play,
        )
        self.play_button.frame.grid(row=0, column=3)
        self.forward_button = self._flat_button(
            cluster, "forward", lambda: self._skip(SKIP_SECONDS), diameter=40, icon_size=22
        )
        self.forward_button.frame.grid(row=0, column=4, padx=(12, 30))
        self.loop_button = self._flat_button(
            cluster, "loop", self._toggle_looping, diameter=32, icon_size=20
        )
        self.loop_button.frame.grid(row=0, column=5)

        scrub = ctk.CTkFrame(transport, fg_color="transparent")
        scrub.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        scrub.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            scrub, textvariable=self.mixer_position_time, text_color=COLORS["muted"], font=ui_font("caption", mono=True), width=52
        ).grid(row=0, column=0, padx=(0, 12))
        self.timeline = tk.Canvas(scrub, height=22, bg=COLORS["window"], highlightthickness=0)
        self.timeline.grid(row=0, column=1, sticky="ew")
        self.timeline.bind("<Button-1>", self._seek_press)
        self.timeline.bind("<B1-Motion>", self._seek_motion)
        self.timeline.bind("<ButtonRelease-1>", self._seek_release)
        self.timeline.bind("<Configure>", lambda _event: self._draw_timeline())
        ctk.CTkLabel(
            scrub, textvariable=self.mixer_duration_time, text_color=COLORS["muted"], font=ui_font("caption", mono=True), width=52
        ).grid(row=0, column=2, padx=(12, 0))

        self.mixer_status = ctk.CTkLabel(
            transport, textvariable=self.mixer_headline, text_color=COLORS["muted2"], font=ui_font("caption"), anchor="w"
        )
        self.mixer_status.grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _build_lanes(self, rows) -> None:
        """Rebuild one lane widget set per published lane, in profile order.

        A result carries its own layout, so the lane strip is rebuilt whenever
        that layout or the absent set changes. Building four fixed lanes once
        would silently drop the extra lanes of a six-lane result.
        """
        container = self._lanes_container
        # Destroy only the lanes this method built. The playhead overlay is a
        # sibling of the lanes and has to survive a relayout, so the strip
        # cannot simply clear every child of its container.
        for frame in self._lane_frames:
            frame.destroy()
        self._lane_frames.clear()
        self._lane_widgets.clear()
        self._waveform_canvases.clear()
        for index, row in enumerate(rows):
            self._build_lane(container, index, *row)
        self._lane_rows = tuple(rows)
        # The canvases the previous layout drew into are gone, so force a
        # redraw rather than trusting the unchanged session identity.
        self._waveform_session = None
        self._resize_for_lanes(len(rows))

    def _build_lane(self, container, index: int, stem_name: str, label: str, color: str, absent: bool) -> None:
        lane = ctk.CTkFrame(
            container,
            height=LANE_HEIGHT,
            fg_color=COLORS["surface"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["line"],
        )
        lane.grid(row=index, column=0, pady=(0 if index == 0 else LANE_GAP, 0), sticky="ew")
        # Every lane keeps its full height whatever the profile publishes. A
        # strip that shares one height between lanes shrinks each of them as
        # lanes are added, and Tk clips the bottom controls without a word.
        lane.grid_propagate(False)
        lane.grid_columnconfigure(1, weight=1)
        lane.grid_rowconfigure(0, weight=1)

        controls = ctk.CTkFrame(lane, width=270, fg_color=COLORS["surface"], corner_radius=0)
        controls.grid(row=0, column=0, padx=(4, 10), pady=LANE_INNER_PAD, sticky="nsw")
        controls.grid_propagate(False)
        # Spacer rows above and below centre the controls in the lane, so the
        # strip reads as evenly spaced rather than top-packed.
        controls.grid_rowconfigure(0, weight=1)
        controls.grid_rowconfigure(3, weight=1)
        bar = tk.Canvas(controls, width=4, bg=COLORS["line"] if absent else color, highlightthickness=0)
        bar.grid(row=0, column=0, rowspan=4, sticky="ns", padx=(10, 0), pady=6)
        # Mute and solo lead the row: they are the controls this mixer exists
        # for, and a name of any length can no longer push them out of view.
        # They live in their own frame so the slider below cannot widen the
        # columns they sit in and drive them apart.
        head = ctk.CTkFrame(controls, fg_color="transparent")
        head.grid(row=1, column=1, columnspan=2, padx=(10, 0), pady=(0, 8), sticky="w")
        mute = self._lane_toggle(head, "M", lambda name=stem_name: self._toggle_mute(name))
        mute.grid(row=0, column=0, padx=(0, 4))
        solo = self._lane_toggle(head, "S", lambda name=stem_name: self._toggle_solo(name))
        solo.grid(row=0, column=1, padx=(0, 12))
        ctk.CTkLabel(
            head,
            text=label,
            text_color=COLORS["muted"] if absent else COLORS["text"],
            font=ui_font("title", bold=True),
            anchor="w",
        ).grid(row=0, column=2, sticky="w")
        scale = ctk.CTkSlider(
            controls,
            from_=0,
            to=100,
            width=152,
            height=14,
            fg_color=COLORS["line"],
            progress_color=color,
            button_color=COLORS["text"],
            button_hover_color=COLORS["text"],
            command=lambda value, name=stem_name: self._volume_changed(name, value),
        )
        scale.set(100)
        scale.grid(row=2, column=1, padx=(10, 6), sticky="w")
        percent = ctk.CTkLabel(controls, text="100%", width=40, anchor="e", text_color=COLORS["muted"], font=ui_font("caption", mono=True))
        percent.grid(row=2, column=2, sticky="w")

        canvas = tk.Canvas(lane, bg=COLORS["surface"], highlightthickness=0)
        canvas.grid(row=0, column=1, padx=(0, 12), pady=LANE_INNER_PAD, sticky="nsew")
        canvas.bind("<Button-1>", self._seek_press)
        canvas.bind("<B1-Motion>", self._seek_motion)
        canvas.bind("<ButtonRelease-1>", self._seek_release)
        canvas.bind("<Configure>", lambda _event: self._draw_waveforms())
        self._lane_frames.append(lane)
        self._waveform_canvases[stem_name] = canvas
        self._lane_widgets[stem_name] = {
            "lane": lane,
            "controls": controls,
            "scale": scale,
            "percent": percent,
            "mute": mute,
            "solo": solo,
            "color": color,
        }

    def _lane_toggle(self, parent, text: str, command) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            width=28,
            height=24,
            corner_radius=6,
            fg_color=COLORS["field"],
            hover_color=COLORS["line"],
            text_color=COLORS["muted"],
            text_color_disabled=COLORS["disabled"],
            font=ui_font("body", bold=True),
            command=command,
        )

    def _resize_for_lanes(self, lane_count: int) -> None:
        """Grow the mixer window so every lane keeps its full height.

        The strip scrolls when the screen cannot hold it, so a capped window
        stays usable instead of hiding the lanes it cannot fit.
        """
        self.mixer_title.set(f"{channel_word(lane_count)} stems. One shared timeline.")
        if self._view != "mixer":
            return
        self._apply_mixer_geometry(lane_count)

    def mixer_chrome_height(self) -> int:
        """Return the mixer height taken by everything but the lane strip.

        Derived from what Tk says the view needs once the strip asks for its
        real height. Adding up paddings by hand is what let the window go on
        being sized for a transport that had already grown a row.
        """
        if self._lanes_container is None:
            return MIXER_CHROME_HEIGHT
        return max(0, self._mixer_required_height() - self._lanes_container.winfo_reqheight())

    def _mixer_required_height(self) -> int:
        """Return the height the mixer needs with the strip at full size."""
        self.root.update_idletasks()
        return self.mixer_view.winfo_reqheight()

    def _apply_mixer_geometry(self, lane_count: int) -> None:
        if self._lanes_container is None:
            return
        # The strip asks for its real height instead of relying on leftover
        # space, so Tk reports what the whole view needs and nothing has to
        # re-derive it from hand-counted paddings.
        self._lanes_container.configure(height=lane_strip_height(lane_count))
        available = self.root.winfo_screenheight() - SCREEN_MARGIN
        wanted = self._mixer_required_height() + NAV_HEIGHT + 1
        self.root.geometry(f"1120x{min(wanted, available)}")
        floor = self.mixer_chrome_height() + lane_strip_height(MIXER_MIN_LANES) + NAV_HEIGHT + 1
        self.root.minsize(880, min(floor, available))

    def _pick_input(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="Choose input audio", filetypes=(("Audio files", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("All files", "*.*"))
        )
        if selected:
            self._choose_profile(selected)

    def _choose_profile(self, source_path: str) -> None:
        """Ask for the separation pipeline only after a source was chosen."""
        if self._profile_dialog is not None and self._profile_dialog.winfo_exists():
            self._profile_dialog.destroy()
        dialog = ctk.CTkToplevel(self.root)
        self._profile_dialog = dialog
        dialog.title("Choose split profile")
        dialog.resizable(False, False)
        dialog.configure(fg_color=COLORS["surface"])
        dialog.transient(self.root)
        dialog.grab_set()
        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=22)
        ctk.CTkLabel(
            body, text="Choose a split profile", text_color=COLORS["text"],
            font=ui_font("heading", bold=True), anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            body, text=Path(source_path).name, text_color=COLORS["muted"],
            font=ui_font("caption"), anchor="w",
        ).pack(fill="x", pady=(4, 14))
        for profile in SeparationController.available_profiles():
            row = ctk.CTkFrame(body, fg_color=COLORS["field"], corner_radius=9)
            row.pack(fill="x", pady=(0, 8))
            copy = ctk.CTkFrame(row, fg_color="transparent")
            copy.pack(side="left", fill="x", expand=True, padx=12, pady=10)
            ctk.CTkLabel(
                copy, text=profile.display_name, text_color=COLORS["text"],
                font=ui_font("title", bold=True), anchor="w",
            ).pack(fill="x")
            note = profile.note or f"{len(profile.lanes)} stems"
            if not profile.enabled:
                probe = SeparationController(profile=profile)
                note = probe.state.profile_remediation
            ctk.CTkLabel(
                copy, text=note, text_color=COLORS["muted"], font=ui_font("caption"),
                anchor="w", justify="left", wraplength=300,
            ).pack(fill="x", pady=(2, 0))
            ctk.CTkButton(
                row, text="Choose" if profile.enabled else "Unavailable", width=88, height=30,
                state="normal" if profile.enabled else "disabled",
                command=lambda profile_id=profile.profile_id: self._confirm_library_add(source_path, profile_id),
                fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
                text_color=COLORS["accent_text"], text_color_disabled=COLORS["disabled"],
            ).pack(side="right", padx=10)
        # A fixed height clipped the last profile once its remediation note
        # wrapped; let the packed rows decide and only pin the width.
        dialog.update_idletasks()
        dialog.geometry(f"480x{body.winfo_reqheight() + 44}")

    def _confirm_library_add(self, source_path: str, profile_id: str) -> None:
        if self._profile_dialog is not None:
            self._profile_dialog.destroy()
            self._profile_dialog = None
        try:
            self.library_controller.add(source_path, profile_id)
        except Exception as error:
            self.status_headline.set("Could not add song")
            self.status_detail.set(str(error))

    def _open_library_track(self, track_id: str) -> None:
        record = self.library_controller.open(track_id)
        if record is not None:
            self._open_mixer_folder(record.result_directory, title=record.title)

    def _library_succeeded(self, record: TrackRecord) -> None:
        self._open_mixer_folder(record.result_directory, title=record.title)
        self.library_model_status.set("")

    def _model_download_progress(self, file_name: str, done: int, total: int) -> None:
        """SEC-01: surface `model_manager.ensure_model()`'s first-use download.

        Reached only through `SplitLibraryController._relay_model_progress`,
        which already dispatches onto the GUI thread, so this runs safely.
        """
        if self._closed:
            return
        if total > 0:
            percent = min(100, int(done * 100 / total))
            self.library_model_status.set(f"Preparing {file_name}: {percent}%")
        else:
            self.library_model_status.set(f"Preparing {file_name}…")

    def _remove_library_track(self, track_id: str) -> None:
        def release(record: TrackRecord) -> None:
            folder = self.mixer_controller.state.folder
            if folder is None:
                return
            try:
                active = Path(folder).resolve() == record.result_directory.resolve()
            except OSError:
                active = Path(folder) == record.result_directory
            if active:
                self.mixer_controller.unload()

        self.library_controller.remove(track_id, release=release)

    def _pick_stems_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="Choose a folder with published WAV stems")
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
        ctk.CTkLabel(head_row, text="Export stems", text_color=COLORS["text"], font=ui_font("heading", bold=True), anchor="w").pack(
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

        export_rows = self._lane_rows or self._mixer_model.lane_rows
        stem_vars: dict[str, tk.BooleanVar] = {row[0]: tk.BooleanVar(value=True) for row in export_rows}
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
            font=ui_font("body", bold=True),
        )
        select_all.pack(anchor="w", pady=(18, 10))

        checkbox_widgets: list[ctk.CTkCheckBox] = []
        rows = ctk.CTkFrame(body, fg_color="transparent")
        rows.pack(fill="x")
        for stem_name, label, color, _absent in export_rows:
            row = ctk.CTkFrame(rows, fg_color=COLORS["field"], corner_radius=10)
            row.pack(fill="x", pady=4)
            row_inner = ctk.CTkFrame(row, fg_color="transparent")
            row_inner.pack(fill="x", padx=12, pady=10)
            _icon(row_inner, lane_icon(stem_name), 16, color, COLORS["field"]).pack(side="left", padx=(0, 10))
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
                font=ui_font("body"),
            )
            checkbox.pack(side="left")
            checkbox_widgets.append(checkbox)

        message = ctk.CTkLabel(body, text="", text_color=COLORS["error"], font=ui_font("caption"), anchor="w", justify="left", wraplength=320)
        message.pack(fill="x", pady=(14, 0))

        results_frame = ctk.CTkFrame(body, fg_color="transparent")
        results_frame.pack(fill="both", expand=True, pady=(6, 0))

        export_button = ctk.CTkButton(
            body,
            text=f"Export {len(export_rows)} selected",
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["accent_text"],
            text_color_disabled=COLORS["muted2"],
            corner_radius=10,
            font=ui_font("body", bold=True),
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
        labels = {row[0]: row[1] for row in (self._lane_rows or self._mixer_model.lane_rows)}
        for stem_name, path, code in state.export_results:
            display_name = labels.get(stem_name, stem_name)
            if path is not None:
                text = f"{display_name}: ✓ {path.name}"
            else:
                text = f"{display_name}: ✗ {code}"
            ctk.CTkLabel(results_frame, text=text, text_color=COLORS["text"], font=ui_font("caption"), anchor="w", justify="left").pack(
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
            self._view = view
            self._apply_mixer_geometry(len(self._lane_rows))
        else:
            self.mixer_view.grid_remove()
            self.separation_view.grid(row=0, column=0, sticky="nsew")
            self.root.geometry(f"1120x{SEPARATION_VIEW_HEIGHT + NAV_HEIGHT + 1}")
            # The placed content clips instead of scrolling, so the window may
            # not be shrunk below the height that keeps the action reachable.
            self.root.minsize(800, SEPARATION_VIEW_HEIGHT + NAV_HEIGHT + 1)
        self._view = view
        self._set_active_tab(view)

    def _render_state(self, state: GuiState) -> None:
        profile = self.controller.profile
        if profile.lane_ids != self._chip_lane_ids:
            self._build_chips(profile)
        if self.profile_selector.get() != profile.display_name:
            self._syncing_controls = True
            try:
                self.profile_selector.set(profile.display_name)
            finally:
                self._syncing_controls = False
        self.input_value_label.configure(text=Path(state.input_file).name if state.input_file else "No file selected")
        self.status_headline.set(state.headline)
        self.status_detail.set(state.detail)
        if state.phase == "error":
            icon_kind, icon_color = "alert", COLORS["error"]
        elif state.phase == "unavailable":
            icon_kind, icon_color = "alert", COLORS["muted2"]
        elif state.phase == "success":
            icon_kind, icon_color = "check", COLORS["success"]
        else:
            icon_kind, icon_color = "dot", COLORS["muted"]
        _draw_icon(self.status_icon, icon_kind, 20, icon_color)
        running = state.phase == "running"
        self.input_button.configure(state="disabled" if running else "normal")
        lane_count = len(self.controller.profile.lanes)
        self.action.configure(
            state="normal" if state.can_start else "disabled",
            text="Separating…" if running else f"Separate into {lane_count} stems",
        )

    def _render_mixer_state(self, state: MixerState) -> None:
        if self._closed:
            return
        if self._preview_frame is not None and int(state.position) == self._preview_frame:
            self._preview_frame = None
        model = MixerViewModel.from_controller_state(state, preview_frame=self._preview_frame)
        self._mixer_model = model
        if model.lane_rows != self._lane_rows:
            self._build_lanes(model.lane_rows)
        self.mixer_headline.set(state.headline)
        self.mixer_detail.set(state.detail)
        self.mixer_position_time.set(format_time(model.position_seconds))
        self.mixer_duration_time.set(format_time(model.duration_seconds))
        self.play_button.set_icon("pause" if state.playing else "play")
        self.play_button.set_enabled(state.can_play)
        for button in (self.rewind_button, self.forward_button, self.loop_button, self.master_button):
            button.set_enabled(state.can_play)
        self.loop_button.set_active(state.looping)
        self.master_button.set_icon("volume_off" if state.master_percent == 0 else "volume")
        self.master_button.set_active(state.master_percent == 0)
        export_ready = state.session is not None and state.phase in {"ready", "playing"}
        self.export_button.configure(state="normal" if export_ready else "disabled")
        self._tab_buttons["export"].configure(state="normal" if export_ready else "disabled")
        self.load_folder_button.configure(state="disabled" if state.phase == "loading" else "normal")
        self._syncing_controls = True
        try:
            self.master_slider.configure(state="normal" if state.can_play else "disabled")
            if int(self.master_slider.get()) != state.master_percent:
                self.master_slider.set(state.master_percent)
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
                continue
            try:
                peaks = session.peaks_for(stem_name)
            except (AttributeError, KeyError):
                continue
            width = max(1, canvas.winfo_width())
            height = max(1, canvas.winfo_height())
            center = height / 2.0
            half = max(2.0, center - 8.0)
            color = lane_color(stem_name)
            if peaks:
                for x in range(width):
                    index = min(len(peaks) - 1, int(x * len(peaks) / width))
                    amplitude = min(1.0, max(0.0, float(peaks[index])))
                    canvas.create_line(x, center - amplitude * half, x, center + amplitude * half, fill=color, tags="waveform")
            canvas.create_line(0, center, width, center, fill=COLORS["line"], tags="waveform")
        self._draw_playheads()

    def _waveform_origin(self) -> tuple[tk.Canvas | None, int]:
        """Return the first waveform canvas and its x inside the lane strip.

        Every lane is built identically, so one canvas describes where the
        waveform area starts for all of them.
        """
        canvas = next(iter(self._waveform_canvases.values()), None)
        if canvas is None:
            return None, 0
        return canvas, canvas.winfo_x() + canvas.master.winfo_x()

    def _draw_playheads(self) -> None:
        """Move the single overlay line that crosses every lane.

        Drawing one stroke per lane leaves the position broken at every lane
        gap. A sibling widget over the whole strip reads as one line, which is
        what a shared timeline actually is.
        """
        if self._playhead is None:
            return
        canvas, origin = self._waveform_origin()
        width = canvas.winfo_width() if canvas is not None else 0
        if canvas is None or width <= 1:
            self._playhead.place_forget()
            return
        offset = max(0.0, min(float(width - PLAYHEAD_WIDTH), self._mixer_model.preview_ratio * width))
        self._playhead.place(x=int(origin + offset), y=0, relheight=1.0, width=PLAYHEAD_WIDTH)
        self._playhead.lift()

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
        if widget is self._playhead:
            # The overlay sits above the lanes, so a press that lands on the
            # line itself must still resolve against the waveform it marks.
            canvas, origin = self._waveform_origin()
            if canvas is None:
                return 0
            return self._mixer_model.frame_for_pixel(
                self._playhead.winfo_x() - origin + event.x, max(1, canvas.winfo_width())
            )
        return self._mixer_model.frame_for_pixel(event.x, max(1, widget.winfo_width()))

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

    def _skip(self, seconds: float) -> None:
        self.mixer_controller.nudge(seconds)

    def _toggle_looping(self) -> None:
        self.mixer_controller.toggle_looping()

    def _toggle_master_mute(self) -> None:
        """Drop the master to silence, and back to where it was."""
        current = self.mixer_controller.state.master_percent
        if current > 0:
            self._master_percent_before_mute = current
            self.mixer_controller.set_master_percent(0)
        else:
            self.mixer_controller.set_master_percent(self._master_percent_before_mute or 100)

    def _master_volume_changed(self, value: float | str) -> None:
        percent = float(value)
        if percent > 0:
            self._master_percent_before_mute = int(round(percent))
        if not self._syncing_controls and self.mixer_controller.state.session is not None:
            self.mixer_controller.set_master_percent(percent)

    def _toggle_play(self) -> None:
        if self.mixer_controller.state.playing:
            self.mixer_controller.pause()
        else:
            self.mixer_controller.play()

    def _space_pressed(self, _event: tk.Event) -> str | None:
        """Toggle Mixer playback on Space, gated by `space_toggles_playback`.

        Manual verification (no automated test instantiates real Tk widgets):
        1. Launch the app and load a session so the Mixer Play button is
           enabled, then press Space with focus anywhere on the Mixer screen
           -> playback starts/pauses like clicking Play/Pause, and repeated
           presses keep alternating with no stray scroll or second action.
        2. Switch to the SPLIT tab and press Space -> Mixer playback state is
           unchanged (typing a space into the library search field still
           inserts a space normally).
        3. Return to the Mixer screen with no session loaded (Play disabled)
           and press Space -> nothing happens.
        """
        if not space_toggles_playback(self._view, self.mixer_controller.state.can_play):
            return None
        self._toggle_play()
        return "break"

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
    if not acquire_single_instance():
        _report_second_instance()
        return 1
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    TkinterDnD.require(root)
    StemslayerApp(root)
    root.mainloop()
    return 0


def _report_second_instance() -> None:
    """Tell the user another instance is already running; touch no history state."""
    message = "Stemslayer is already running. Close the other window first."
    print(message, file=sys.stderr)
    try:
        dialog_root = tk.Tk()
        dialog_root.withdraw()
        messagebox.showerror("Stemslayer already running", message)
        dialog_root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
