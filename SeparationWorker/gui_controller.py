"""Window-independent state and background execution for the Windows GUI."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from SeparationWorker.demucs_adapter import separate_audio
from SeparationWorker.engine import stem_cache


@dataclass(frozen=True)
class GuiState:
    input_file: str = ""
    result_directory: Path | None = None
    phase: str = "idle"
    headline: str = "Choose a song to separate"
    detail: str = "The result will contain vocals, drums, bass, and other."

    @property
    def can_start(self) -> bool:
        return bool(self.input_file) and self.phase != "running"


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="limbus-separation", daemon=True).start()


class SeparationController:
    def __init__(
        self,
        *,
        separate: Callable[[str, str | Path], Path] = separate_audio,
        cache_directory: Callable[[str | Path], Path] = stem_cache.prepare_cache_directory,
        start_worker: Callable[[Callable[[], None]], None] = _start_thread,
        dispatch: Callable[[Callable[[], None]], None] = lambda callback: callback(),
        on_change: Callable[[GuiState], None] = lambda _state: None,
        on_success: Callable[[Path], None] = lambda _result: None,
    ):
        self._separate = separate
        self._cache_directory = cache_directory
        self._start_worker = start_worker
        self._dispatch = dispatch
        self._on_change = on_change
        self._on_success = on_success
        self.state = GuiState()

    def _set_state(self, state: GuiState) -> None:
        self.state = state
        self._on_change(state)

    def set_input_file(self, path: str) -> bool:
        if self.state.phase == "running":
            return False
        result_directory = self._cache_directory(path) if path else None
        ready = bool(path)
        self._set_state(
            replace(
                self.state,
                input_file=path,
                result_directory=result_directory,
                phase="ready" if ready else "idle",
                headline="Ready to separate" if ready else "Choose a song to separate",
                detail=(
                    "Four WAV files will be prepared for the mixer."
                    if ready
                    else "The result will contain vocals, drums, bass, and other."
                ),
            )
        )
        return True

    def start(self) -> bool:
        if self.state.phase == "running":
            return False
        if not self.state.input_file:
            self._set_state(
                replace(
                    self.state,
                    phase="error",
                    headline="Choose an input audio file",
                    detail="Select an input audio file before starting separation.",
                )
            )
            return False

        input_file = self.state.input_file
        result_directory = self.state.result_directory
        self._set_state(
            replace(
                self.state,
                phase="running",
                headline="Separating into 4 stems",
                detail="This can take several minutes. Keep this window open.",
            )
        )
        self._start_worker(lambda: self._run(input_file, result_directory))
        return True

    def _run(self, input_file: str, result_directory: Path) -> None:
        try:
            result = self._separate(input_file, result_directory)
        except Exception as error:
            cause = getattr(error, "cause", str(error))
            recovery = getattr(error, "recovery", "Check the selected input and retry the complete song.")
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
