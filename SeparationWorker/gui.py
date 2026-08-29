"""Compact Tkinter studio console for the Windows MVP."""

from __future__ import annotations

import queue
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog

from SeparationWorker.gui_controller import GuiState, SeparationController


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
}

STEMS = (
    ("VOCALS", "#db6d78"),
    ("DRUMS", "#d69d49"),
    ("BASS", "#48a78f"),
    ("OTHER", "#788bd5"),
)


class LimbusApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Limbus Split")
        self.root.geometry("760x500")
        self.root.minsize(680, 470)
        self.root.configure(bg=COLORS["window"])
        self._events: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._output_parent = ""

        self.input_value = tk.StringVar()
        self.output_value = tk.StringVar()
        self.status_headline = tk.StringVar()
        self.status_detail = tk.StringVar()

        self._build()
        self.controller = SeparationController(
            dispatch=self._events.put,
            on_change=self._render_state,
        )
        self._render_state(self.controller.state)
        self.root.after(50, self._drain_events)

    def _build(self) -> None:
        shell = tk.Frame(self.root, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["line"])
        shell.grid(row=0, column=0, padx=28, pady=26, sticky="nsew")
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        shell.grid_columnconfigure(0, weight=1)

        rail = tk.Frame(shell, bg=COLORS["panel"], height=5)
        rail.grid(row=0, column=0, sticky="ew")
        for index, (_name, color) in enumerate(STEMS):
            rail.grid_columnconfigure(index, weight=1)
            tk.Frame(rail, bg=color, height=5).grid(row=0, column=index, padx=(0 if index == 0 else 2, 0), sticky="ew")

        header = tk.Frame(shell, bg=COLORS["panel"])
        header.grid(row=1, column=0, padx=32, pady=(27, 23), sticky="ew")
        tk.Label(
            header,
            text="LIMBUS  /  SPLIT",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Four clean channels. One local pass.",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 21),
        ).pack(anchor="w", pady=(7, 0))

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
        self.output_button = self._path_row(paths, 1, "RESULT FOLDER", self.output_value, "Choose folder", self._pick_output)

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
        tk.Label(status_copy, textvariable=self.status_detail, bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9), wraplength=390, justify="left").pack(anchor="w", pady=(3, 0))

        self.action = tk.Button(
            footer,
            text="Separate into 4 stems",
            command=self._start,
            bg=COLORS["button"],
            fg=COLORS["button_text"],
            activebackground="#ffffff",
            activeforeground=COLORS["button_text"],
            disabledforeground="#667487",
            relief="flat",
            bd=0,
            padx=20,
            pady=12,
            font=("Segoe UI Semibold", 10),
            cursor="hand2",
        )
        self.action.grid(row=0, column=1, padx=(20, 0), sticky="e")

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
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Choose input audio",
            filetypes=(("Audio files", "*.wav *.mp3 *.flac *.m4a *.ogg"), ("All files", "*.*")),
        )
        if selected:
            self.controller.set_input_file(selected)
            if self._output_parent:
                self.controller.set_output_directory(str(Path(self._output_parent) / f"{Path(selected).stem}-stems"))

    def _pick_output(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="Choose where to create the stem folder")
        if selected:
            self._output_parent = selected
            result_name = f"{Path(self.controller.state.input_file).stem}-stems" if self.controller.state.input_file else "separated-stems"
            self.controller.set_output_directory(str(Path(selected) / result_name))

    def _start(self) -> None:
        self.controller.start()

    def _render_state(self, state: GuiState) -> None:
        self.input_value.set(state.input_file)
        self.output_value.set(state.output_directory)
        self.status_headline.set(state.headline)
        self.status_detail.set(state.detail)
        marker = COLORS["error"] if state.phase == "error" else COLORS["success"] if state.phase == "success" else COLORS["muted"]
        self.status_marker.configure(bg=marker)
        running = state.phase == "running"
        self.input_button.configure(state="disabled" if running else "normal")
        self.output_button.configure(state="disabled" if running else "normal")
        self.action.configure(state="normal" if state.can_start else "disabled", text="Separating…" if running else "Separate into 4 stems")

    def _drain_events(self) -> None:
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
