"""Window-independent state and background execution for the Windows GUI."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from SeparationWorker.demucs_adapter import separate_audio


@dataclass(frozen=True)
class GuiState:
    input_file: str = ""
    output_directory: str = ""
    phase: str = "idle"
    headline: str = "Choose a song and destination"
    detail: str = "The result will contain vocals, drums, bass, and other."

    @property
    def can_start(self) -> bool:
        return bool(self.input_file and self.output_directory and self.phase != "running")


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="limbus-separation", daemon=True).start()


class SeparationController:
    def __init__(
        self,
        *,
        separate: Callable[[str, str], Path] = separate_audio,
        start_worker: Callable[[Callable[[], None]], None] = _start_thread,
        dispatch: Callable[[Callable[[], None]], None] = lambda callback: callback(),
        on_change: Callable[[GuiState], None] = lambda _state: None,
        on_success: Callable[[Path], None] = lambda _result: None,
    ):
        self._separate = separate
        self._start_worker = start_worker
        self._dispatch = dispatch
        self._on_change = on_change
        self._on_success = on_success
        self.state = GuiState()

    def _set_state(self, state: GuiState) -> None:
        self.state = state
        self._on_change(state)

    def _selection_changed(self, **changes: str) -> bool:
        if self.state.phase == "running":
            return False
        state = replace(self.state, **changes)
        ready = bool(state.input_file and state.output_directory)
        self._set_state(
            replace(
                state,
                phase="ready" if ready else "idle",
                headline="Ready to separate" if ready else "Choose a song and destination",
                detail=(
                    "Four WAV files will be created in the selected result folder."
                    if ready
                    else "The result will contain vocals, drums, bass, and other."
                ),
            )
        )
        return True

    def set_input_file(self, path: str) -> bool:
        return self._selection_changed(input_file=path)

    def set_output_directory(self, path: str) -> bool:
        return self._selection_changed(output_directory=path)

    def start(self) -> bool:
        if self.state.phase == "running":
            return False
        if not self.state.input_file or not self.state.output_directory:
            missing = "input audio file" if not self.state.input_file else "output directory"
            self._set_state(
                replace(
                    self.state,
                    phase="error",
                    headline=f"Choose an {missing}",
                    detail="Select both paths before starting separation.",
                )
            )
            return False

        input_file = self.state.input_file
        output_directory = self.state.output_directory
        self._set_state(
            replace(
                self.state,
                phase="running",
                headline="Separating into 4 stems",
                detail="This can take several minutes. Keep this window open.",
            )
        )
        self._start_worker(lambda: self._run(input_file, output_directory))
        return True

    def _run(self, input_file: str, output_directory: str) -> None:
        try:
            result = self._separate(input_file, output_directory)
        except Exception as error:
            cause = getattr(error, "cause", str(error))
            recovery = getattr(error, "recovery", "Check the selected paths and retry the complete song.")
            self._dispatch(lambda: self._finish_error(str(cause), str(recovery)))
            return
        self._dispatch(lambda: self._finish_success(Path(result)))

    def _finish_success(self, result: Path) -> None:
        self._set_state(
            replace(
                self.state,
                phase="success",
                headline="4 stems are ready",
                detail=str(result),
            )
        )
        try:
            self._on_success(result)
        except Exception:
            # A view transition must not turn a completed separation into a
            # failed operation if an injected UI callback raises.
            return

    def _finish_error(self, cause: str, recovery: str) -> None:
        self._set_state(
            replace(
                self.state,
                phase="error",
                headline="Separation failed",
                detail=f"{cause} {recovery}",
            )
        )
