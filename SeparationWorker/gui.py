"""Tkinter studio workspace for separating and auditioning Windows stems."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog

from SeparationWorker.engine import stem_cache
from SeparationWorker.engine.mixer import MixerSnapshot
from SeparationWorker.engine.stem_session import STEM_NAMES
from SeparationWorker.gui_controller import GuiState, SeparationController
from SeparationWorker.mixer_controller import MixerController, MixerState


COLORS = {
    "window": "#090e16",
    "panel": "#111a28",
    "field": "#182436",
    "line": "#26354a",
    "text": "#eef3f8",
    "muted": "#91a0b2",
    "button": "#d7e2ef",
    "button_text": "#0b1420",
    "error": "#ed7772",
    "success": "#58b89f",
    "playhead": "#f4f7fa",
}

STEMS = (
    ("VOCALS", "#db6d78"),
    ("DRUMS", "#d69d49"),
    ("BASS", "#48a78f"),
    ("OTHER", "#788bd5"),
)
STEM_ROWS = tuple((name.lower() + ".wav", name, color) for name, color in STEMS)


def format_time(seconds: float) -> str:
    """Format a timeline value without exposing audio implementation details."""
    seconds = max(0.0, float(seconds))
    minutes, remainder = divmod(seconds, 60.0)
    return f"{int(minutes):02d}:{remainder:05.2f}"


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


class LimbusApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Limbus Split")
        self.root.geometry("1120x720")
        self.root.minsize(800, 560)
        self.root.configure(bg=COLORS["window"])
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._events: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._view = "separation"
        self._closed = False
        self._preview_frame: int | None = None
        self._seek_dragging = False
        self._waveform_session = None
        self._waveform_canvases: dict[str, tk.Canvas] = {}
        self._lane_widgets: dict[str, dict[str, tk.Widget]] = {}
        self._mixer_model = MixerViewModel()
        self._syncing_controls = False
        self._cache_directories: list[Path] = []

        self.input_value = tk.StringVar()
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
        self.root.after(50, self._drain_events)
        threading.Thread(
            target=stem_cache.sweep_orphans, name="limbus-stem-cache-sweep", daemon=True
        ).start()

    def _build(self) -> None:
        self.workspace = tk.Frame(self.root, bg=COLORS["window"])
        self.workspace.grid(row=0, column=0, padx=24, pady=22, sticky="nsew")
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.workspace.grid_rowconfigure(0, weight=1)
        self.workspace.grid_columnconfigure(0, weight=1)

        self.separation_view = tk.Frame(self.workspace, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["line"])
        self.separation_view.grid(row=0, column=0, sticky="nsew")
        self._build_separation_view()

        self.mixer_view = tk.Frame(self.workspace, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["line"])
        self._build_mixer_view()

    def _build_rail(self, parent: tk.Widget) -> None:
        rail = tk.Frame(parent, bg=COLORS["panel"], height=5)
        rail.grid(row=0, column=0, sticky="ew")
        for index, (_name, color) in enumerate(STEMS):
            rail.grid_columnconfigure(index, weight=1)
            tk.Frame(rail, bg=color, height=5).grid(row=0, column=index, padx=(0 if index == 0 else 2, 0), sticky="ew")

    def _build_separation_view(self) -> None:
        shell = self.separation_view
        shell.grid_columnconfigure(0, weight=1)
        self._build_rail(shell)

        header = tk.Frame(shell, bg=COLORS["panel"])
        header.grid(row=1, column=0, padx=32, pady=(27, 23), sticky="ew")
        tk.Label(header, text="LIMBUS  /  SPLIT", bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI Semibold", 9)).pack(anchor="w")
        tk.Label(header, text="Four clean channels. One local pass.", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI Semibold", 21)).pack(anchor="w", pady=(7, 0))

        channels = tk.Frame(shell, bg=COLORS["panel"])
        channels.grid(row=2, column=0, padx=32, sticky="ew")
        for index, (name, color) in enumerate(STEMS):
            channels.grid_columnconfigure(index, weight=1)
            channel = tk.Frame(channels, bg=COLORS["field"], highlightthickness=1, highlightbackground=COLORS["line"])
            channel.grid(row=0, column=index, padx=(0 if index == 0 else 5, 0), sticky="ew")
            tk.Frame(channel, bg=color, width=4, height=26).pack(side="left")
            tk.Label(channel, text=name, bg=COLORS["field"], fg=COLORS["muted"], font=("Segoe UI Semibold", 8)).pack(side="left", padx=10, pady=7)

        paths = tk.Frame(shell, bg=COLORS["panel"])
        paths.grid(row=3, column=0, padx=32, pady=(22, 0), sticky="ew")
        paths.grid_columnconfigure(0, weight=1)
        self.input_button = self._path_row(paths, 0, "INPUT AUDIO", self.input_value, "Browse file", self._pick_input)

        footer = tk.Frame(shell, bg=COLORS["panel"])
        footer.grid(row=4, column=0, padx=32, pady=(24, 30), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        status = tk.Frame(footer, bg=COLORS["panel"])
        status.grid(row=0, column=0, sticky="w")
        self.status_marker = tk.Frame(status, bg=COLORS["muted"], width=4, height=42)
        self.status_marker.pack(side="left", fill="y", padx=(0, 12))
        status_copy = tk.Frame(status, bg=COLORS["panel"])
        status_copy.pack(side="left")
        tk.Label(status_copy, textvariable=self.status_headline, bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(status_copy, textvariable=self.status_detail, bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9), wraplength=420, justify="left").pack(anchor="w", pady=(3, 0))

        self.action = tk.Button(footer, text="Separate into 4 stems", command=self._start, bg=COLORS["button"], fg=COLORS["button_text"], activebackground="#ffffff", activeforeground=COLORS["button_text"], disabledforeground="#667487", relief="flat", bd=0, padx=20, pady=12, font=("Segoe UI Semibold", 10), cursor="hand2")
        self.action.grid(row=0, column=1, padx=(16, 0), sticky="e")

    def _build_mixer_view(self) -> None:
        shell = self.mixer_view
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(3, weight=1)
        self._build_rail(shell)

        header = tk.Frame(shell, bg=COLORS["panel"])
        header.grid(row=1, column=0, padx=24, pady=(18, 10), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        self.back_button = tk.Button(header, text="‹  Split", command=lambda: self._show_view("separation"), bg=COLORS["field"], fg=COLORS["text"], activebackground=COLORS["line"], activeforeground=COLORS["text"], relief="flat", bd=0, padx=12, pady=7, font=("Segoe UI Semibold", 9), cursor="hand2")
        self.back_button.grid(row=0, column=0, sticky="w")
        title = tk.Frame(header, bg=COLORS["panel"])
        title.grid(row=0, column=1, padx=18, sticky="w")
        tk.Label(title, text="LIMBUS  /  MIXER", bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI Semibold", 9)).pack(anchor="w")
        tk.Label(title, text="Four stems. One shared timeline.", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI Semibold", 18)).pack(anchor="w", pady=(4, 0))
        tk.Label(title, textvariable=self.mixer_detail, bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 8), wraplength=620, justify="left").pack(anchor="w", pady=(3, 0))
        self.load_folder_button = tk.Button(header, text="Load stems folder", command=self._pick_stems_folder, bg=COLORS["field"], fg=COLORS["text"], activebackground=COLORS["line"], activeforeground=COLORS["text"], relief="flat", bd=0, padx=13, pady=8, font=("Segoe UI Semibold", 9), cursor="hand2")
        self.load_folder_button.grid(row=0, column=2, sticky="e")

        transport = tk.Frame(shell, bg=COLORS["panel"])
        transport.grid(row=2, column=0, padx=24, pady=(0, 12), sticky="ew")
        transport.grid_columnconfigure(3, weight=1)
        self.play_button = tk.Button(transport, text="Play", command=self._toggle_play, bg=COLORS["button"], fg=COLORS["button_text"], activebackground="#ffffff", activeforeground=COLORS["button_text"], disabledforeground="#667487", relief="flat", bd=0, padx=18, pady=8, font=("Segoe UI Semibold", 9), cursor="hand2")
        self.play_button.grid(row=0, column=0, sticky="w")
        self.export_button = tk.Button(transport, text="Exportar", command=self._open_export_dialog, bg=COLORS["field"], fg=COLORS["text"], activebackground=COLORS["line"], activeforeground=COLORS["text"], disabledforeground="#667487", relief="flat", bd=0, padx=14, pady=8, font=("Segoe UI Semibold", 9), cursor="hand2")
        self.export_button.grid(row=0, column=1, padx=(10, 0), sticky="w")
        self.mixer_status = tk.Label(transport, textvariable=self.mixer_headline, bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI Semibold", 9))
        self.mixer_status.grid(row=0, column=2, padx=(16, 14), sticky="w")
        self.timeline = tk.Canvas(transport, height=26, bg=COLORS["panel"], highlightthickness=0)
        self.timeline.grid(row=0, column=3, sticky="ew")
        self.timeline.bind("<Button-1>", self._seek_press)
        self.timeline.bind("<B1-Motion>", self._seek_motion)
        self.timeline.bind("<ButtonRelease-1>", self._seek_release)
        tk.Label(transport, textvariable=self.mixer_time, bg=COLORS["panel"], fg=COLORS["muted"], font=("Consolas", 9)).grid(row=0, column=4, padx=(14, 0), sticky="e")

        lanes = tk.Frame(shell, bg=COLORS["panel"])
        lanes.grid(row=3, column=0, padx=24, pady=(0, 20), sticky="nsew")
        lanes.grid_columnconfigure(0, weight=1)
        for index, (stem_name, label, color) in enumerate(STEM_ROWS):
            lanes.grid_rowconfigure(index, weight=1, uniform="lane")
            lane = tk.Frame(lanes, bg=COLORS["field"], highlightthickness=1, highlightbackground=COLORS["line"])
            lane.grid(row=index, column=0, pady=(0 if index == 0 else 6, 0), sticky="nsew")
            lane.grid_columnconfigure(1, weight=1)
            lane.grid_rowconfigure(0, weight=1)
            controls = tk.Frame(lane, width=190, bg=COLORS["field"])
            controls.grid(row=0, column=0, padx=(12, 8), pady=8, sticky="nsw")
            controls.grid_propagate(False)
            tk.Frame(controls, bg=color, width=4).grid(row=0, column=0, rowspan=3, sticky="ns")
            tk.Label(controls, text=label, bg=COLORS["field"], fg=COLORS["text"], font=("Segoe UI Semibold", 9)).grid(row=0, column=1, columnspan=3, padx=(10, 0), sticky="w")
            scale = tk.Scale(controls, from_=0, to=100, orient="horizontal", resolution=1, showvalue=False, length=112, bg=COLORS["field"], fg=COLORS["muted"], troughcolor=COLORS["line"], highlightthickness=0, bd=0, command=lambda value, name=stem_name: self._volume_changed(name, value))
            scale.set(100)
            scale.grid(row=1, column=1, columnspan=2, padx=(10, 2), pady=(6, 0), sticky="w")
            percent = tk.Label(controls, text="100%", width=5, anchor="e", bg=COLORS["field"], fg=COLORS["muted"], font=("Consolas", 8))
            percent.grid(row=1, column=3, pady=(6, 0), sticky="e")
            mute = tk.Button(controls, text="M", command=lambda name=stem_name: self._toggle_mute(name), bg=COLORS["line"], fg=COLORS["text"], activebackground=COLORS["error"], activeforeground=COLORS["text"], disabledforeground="#667487", relief="flat", bd=0, width=3, pady=2, font=("Segoe UI Semibold", 8), cursor="hand2")
            mute.grid(row=2, column=1, padx=(10, 3), pady=(7, 0), sticky="w")
            solo = tk.Button(controls, text="S", command=lambda name=stem_name: self._toggle_solo(name), bg=COLORS["line"], fg=COLORS["text"], activebackground=color, activeforeground=COLORS["text"], disabledforeground="#667487", relief="flat", bd=0, width=3, pady=2, font=("Segoe UI Semibold", 8), cursor="hand2")
            solo.grid(row=2, column=2, padx=3, pady=(7, 0), sticky="w")
            canvas = tk.Canvas(lane, height=78, bg=COLORS["field"], highlightthickness=0)
            canvas.grid(row=0, column=1, padx=(0, 10), pady=8, sticky="nsew")
            canvas.bind("<Button-1>", self._seek_press)
            canvas.bind("<B1-Motion>", self._seek_motion)
            canvas.bind("<ButtonRelease-1>", self._seek_release)
            canvas.bind("<Configure>", lambda _event: self._draw_waveforms())
            self._waveform_canvases[stem_name] = canvas
            self._lane_widgets[stem_name] = {"scale": scale, "percent": percent, "mute": mute, "solo": solo, "color": color}

    def _path_row(self, parent, row, label, variable, button_text, command):
        tk.Label(parent, text=label, bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI Semibold", 8)).grid(row=row * 2, column=0, pady=(0 if row == 0 else 13, 5), sticky="w")
        line = tk.Frame(parent, bg=COLORS["panel"])
        line.grid(row=row * 2 + 1, column=0, sticky="ew")
        line.grid_columnconfigure(0, weight=1)
        entry = tk.Entry(line, textvariable=variable, state="readonly", readonlybackground=COLORS["field"], fg=COLORS["text"], relief="flat", bd=0, font=("Segoe UI", 10))
        entry.grid(row=0, column=0, ipady=9, ipadx=10, sticky="ew")
        button = tk.Button(line, text=button_text, command=command, bg=COLORS["field"], fg=COLORS["text"], activebackground=COLORS["line"], activeforeground=COLORS["text"], relief="flat", bd=0, padx=14, pady=8, font=("Segoe UI Semibold", 9), cursor="hand2")
        button.grid(row=0, column=1, padx=(7, 0))
        return button

    def _pick_input(self) -> None:
        selected = filedialog.askopenfilename(parent=self.root, title="Choose input audio", filetypes=(("Audio files", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("All files", "*.*")))
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

        dialog = tk.Toplevel(self.root)
        dialog.title("Exportar pistas")
        dialog.transient(self.root)
        dialog.configure(bg=COLORS["panel"])
        dialog.geometry("360x320")
        dialog.resizable(False, False)

        stem_vars: dict[str, tk.BooleanVar] = {
            stem_name: tk.BooleanVar(value=True) for stem_name, _label, _color in STEM_ROWS
        }
        select_all_var = tk.BooleanVar(value=True)

        body = tk.Frame(dialog, bg=COLORS["panel"])
        body.pack(fill="both", expand=True, padx=18, pady=16)

        select_all = tk.Checkbutton(
            body,
            text="Seleccionar todos",
            variable=select_all_var,
            command=lambda: self._set_all_export_vars(stem_vars, select_all_var.get()),
            bg=COLORS["panel"],
            fg=COLORS["text"],
            selectcolor=COLORS["field"],
            activebackground=COLORS["panel"],
            activeforeground=COLORS["text"],
            font=("Segoe UI Semibold", 9),
        )
        select_all.pack(anchor="w", pady=(0, 8))

        for stem_name, label, _color in STEM_ROWS:
            tk.Checkbutton(
                body,
                text=label,
                variable=stem_vars[stem_name],
                bg=COLORS["panel"],
                fg=COLORS["text"],
                selectcolor=COLORS["field"],
                activebackground=COLORS["panel"],
                activeforeground=COLORS["text"],
                font=("Segoe UI", 9),
            ).pack(anchor="w", pady=2)

        message = tk.Label(body, text="", bg=COLORS["panel"], fg=COLORS["error"], font=("Segoe UI", 8), wraplength=320, justify="left")
        message.pack(anchor="w", pady=(10, 0))

        results_frame = tk.Frame(body, bg=COLORS["panel"])
        results_frame.pack(fill="both", expand=True, pady=(4, 0))

        export_button = tk.Button(
            body,
            text="Exportar 4 seleccionados",
            bg=COLORS["button"],
            fg=COLORS["button_text"],
            activebackground="#ffffff",
            activeforeground=COLORS["button_text"],
            disabledforeground="#667487",
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            font=("Segoe UI Semibold", 9),
            cursor="hand2",
        )
        export_button.pack(anchor="e", pady=(12, 0))

        def refresh_button_label(*_args) -> None:
            count = sum(1 for var in stem_vars.values() if var.get())
            export_button.configure(
                text=f"Exportar {count} seleccionados",
                state="normal" if count else "disabled",
            )

        for var in stem_vars.values():
            var.trace_add("write", refresh_button_label)
        refresh_button_label()

        def start_export() -> None:
            chosen = filedialog.askdirectory(parent=dialog, title="Elegí la carpeta destino")
            if not chosen:
                return
            selected_names = [name for name, var in stem_vars.items() if var.get()]
            ok = self.mixer_controller.export_stems(selected_names, chosen)
            if not ok:
                message.configure(text="No se pudo exportar (sesión no disponible)")
                return
            message.configure(text="Exportando…")
            select_all.configure(state="disabled")
            export_button.configure(state="disabled")
            for widget in body.winfo_children():
                if isinstance(widget, tk.Checkbutton):
                    widget.configure(state="disabled")
            self.root.after(100, lambda: self._poll_export_dialog(dialog, message, results_frame, export_button))

        export_button.configure(command=start_export)

    def _set_all_export_vars(self, stem_vars: dict[str, tk.BooleanVar], value: bool) -> None:
        for var in stem_vars.values():
            var.set(value)

    def _poll_export_dialog(self, dialog: tk.Toplevel, message: tk.Label, results_frame: tk.Frame, export_button: tk.Button) -> None:
        if not dialog.winfo_exists():
            return
        state = self.mixer_controller.state
        if state.export_phase == "running":
            self.root.after(100, lambda: self._poll_export_dialog(dialog, message, results_frame, export_button))
            return
        if state.export_phase != "done":
            return

        message.configure(text="Exportación finalizada")
        labels = {stem_name: label for stem_name, label, _color in STEM_ROWS}
        for stem_name, path, code in state.export_results:
            display_name = labels.get(stem_name, stem_name)
            if path is not None:
                text = f"{display_name}: ✓ {path.name}"
            else:
                text = f"{display_name}: ✗ {code}"
            tk.Label(results_frame, text=text, bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 9), anchor="w", justify="left").pack(anchor="w", pady=1)

        export_button.configure(text="Cerrar", state="normal", command=dialog.destroy)

    def _show_view(self, view: str) -> None:
        if view == "mixer":
            self.separation_view.grid_remove()
            self.mixer_view.grid(row=0, column=0, sticky="nsew")
            self.root.geometry("1120x720")
        else:
            self.mixer_view.grid_remove()
            self.separation_view.grid(row=0, column=0, sticky="nsew")
            self.root.geometry("960x620")
        self._view = view

    def _render_state(self, state: GuiState) -> None:
        self.input_value.set(state.input_file)
        self.status_headline.set(state.headline)
        self.status_detail.set(state.detail)
        marker = COLORS["error"] if state.phase == "error" else COLORS["success"] if state.phase == "success" else COLORS["muted"]
        self.status_marker.configure(bg=marker)
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
        self.play_button.configure(text="Pause" if state.playing else "Play", state="normal" if state.can_play else "disabled")
        self.export_button.configure(state="normal" if (state.session is not None and state.phase in {"ready", "playing"}) else "disabled")
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
                    widgets["mute"].configure(bg=COLORS["error"] if setting.muted else COLORS["line"])
                    widgets["solo"].configure(bg=widgets["color"] if setting.solo else COLORS["line"])
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
            canvas.create_line(x, 0, x, height, fill=COLORS["playhead"], width=2, tags="playhead")

    def _draw_timeline(self) -> None:
        self.timeline.delete("all")
        width = self.timeline.winfo_width()
        height = self.timeline.winfo_height()
        if width <= 1:
            return
        self.timeline.create_line(0, height - 7, width, height - 7, fill=COLORS["line"])
        for tick in range(0, 11):
            x = tick * width / 10
            self.timeline.create_line(x, height - 10, x, height - 3, fill=COLORS["muted"])
        x = max(0.0, min(float(width - 1), self._mixer_model.preview_ratio * width))
        self.timeline.create_line(x, 2, x, height - 1, fill=COLORS["playhead"], width=2)

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

    def _volume_changed(self, stem_name: str, value: str) -> None:
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


def main() -> None:
    root = tk.Tk()
    LimbusApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
