"""Window-independent orchestration for the Windows stem mixer."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from SeparationWorker.engine.mixer import (
    MixSetting,
    MixerSnapshot,
    gain_from_percent,
)
from SeparationWorker.engine.playback import PlaybackEngine, PlaybackError, PlaybackState
from SeparationWorker.engine.stem_session import STEM_NAMES, StemSession


_DEFAULT_SETTINGS = MixerSnapshot(tuple(MixSetting(name) for name in STEM_NAMES))


@dataclass(frozen=True)
class MixerState:
    """Immutable state that can be rendered by a Tkinter mixer view."""

    folder: Path | None = None
    session: object | None = None
    phase: str = "idle"
    headline: str = "Choose a four-stem folder"
    detail: str = "Load vocals.wav, drums.wav, bass.wav, and other.wav to begin."
    position: int = 0
    frame_count: int = 0
    playing: bool = False
    settings: MixerSnapshot = _DEFAULT_SETTINGS
    error_code: str | None = None

    @property
    def duration_frames(self) -> int:
        return self.frame_count

    @property
    def duration_seconds(self) -> float:
        if self.session is None:
            return 0.0
        sample_rate = getattr(self.session, "sample_rate", 0)
        return self.frame_count / sample_rate if sample_rate else 0.0

    @property
    def can_play(self) -> bool:
        return self.session is not None and self.phase in {"ready", "playing"}

    @property
    def has_session(self) -> bool:
        return self.session is not None

    @property
    def snapshot(self) -> MixerSnapshot:
        """Compatibility alias for callers that name the mix state snapshot."""
        return self.settings


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="limbus-mixer-load", daemon=True).start()


def _error_parts(error: Exception) -> tuple[str, str, str | None]:
    cause = str(getattr(error, "cause", str(error)))
    recovery = str(
        getattr(
            error,
            "recovery",
            "Check the selected stem folder and retry.",
        )
    )
    code = getattr(error, "code", None)
    detail = f"{cause} {recovery}".strip()
    return detail, recovery, str(code) if code is not None else None


class MixerController:
    """Coordinate asynchronous sessions, playback, and immutable UI state.

    The loader and playback engine are injected so controller behavior remains
    headless-testable. Every callback from a worker is routed through
    ``dispatch`` before state is changed, and generation tokens discard stale
    results from superseded loads.
    """

    def __init__(
        self,
        *,
        load_session: Callable[[str | Path], StemSession] | None = None,
        session_loader: Callable[[str | Path], StemSession] | None = None,
        playback_factory: Callable[..., PlaybackEngine] = PlaybackEngine,
        start_worker: Callable[[Callable[[], None]], None] = _start_thread,
        dispatch: Callable[[Callable[[], None]], None] = lambda callback: callback(),
        on_change: Callable[[MixerState], None] = lambda _state: None,
        on_state: Callable[[MixerState], None] | None = None,
    ):
        self._load_session = session_loader or load_session or StemSession.load
        self._playback_factory = playback_factory
        self._start_worker = start_worker
        self._dispatch = dispatch
        self._on_change = on_state or on_change
        self._lock = threading.Lock()
        self._generation = 0
        self._closed = False
        self._engine: PlaybackEngine | None = None
        self.state = MixerState()

    def _set_state(self, state: MixerState) -> None:
        self.state = state
        try:
            self._on_change(state)
        except Exception:
            # A view callback must not terminate a loader or playback worker.
            return

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return not self._closed and generation == self._generation

    def _current_engine(self):
        with self._lock:
            if self._closed or self.state.session is None:
                return None
            return self._engine

    def load(self, folder: str | Path) -> bool:
        """Load a complete stem folder asynchronously."""
        try:
            folder = Path(folder)
        except (TypeError, ValueError):
            return False

        with self._lock:
            if self._closed:
                return False
            self._generation += 1
            generation = self._generation
            previous = self._engine
            self._engine = None

        self._set_state(
            MixerState(
                folder=folder,
                phase="loading",
                headline="Loading 4 stems",
                detail="Validating the WAV files and preparing waveforms.",
            )
        )
        self._start_worker(lambda: self._load_worker(generation, folder, previous))
        return True

    def replace(self, folder: str | Path) -> bool:
        """Replace the current folder through the same stale-safe load path."""
        return self.load(folder)

    def _load_worker(self, generation: int, folder: Path, previous) -> None:
        self._close_engine(previous)
        try:
            session = self._load_session(folder)
            engine = self._playback_factory(
                session,
                on_state=lambda state: self._playback_state(generation, state),
                on_error=lambda error: self._playback_error(generation, error),
            )
        except Exception as error:
            detail, _recovery, code = _error_parts(error)
            self._dispatch(
                lambda: self._finish_error(
                    generation,
                    "Could not load stems",
                    detail,
                    code,
                )
            )
            return

        if not self._is_current(generation):
            self._close_engine(engine)
            return
        try:
            self._dispatch(lambda: self._finish_loaded(generation, folder, session, engine))
        except Exception:
            self._close_engine(engine)

    def _finish_loaded(self, generation: int, folder: Path, session, engine) -> None:
        if not self._is_current(generation):
            self._close_engine(engine)
            return
        with self._lock:
            if self._closed or generation != self._generation:
                stale = True
            else:
                self._engine = engine
                stale = False
        if stale:
            self._close_engine(engine)
            return

        loaded_folder = Path(getattr(session, "folder", folder))
        frame_count = int(getattr(session, "frame_count", 0))
        self._set_state(
            MixerState(
                folder=loaded_folder,
                session=session,
                phase="ready",
                headline="4 stems are ready",
                detail=str(loaded_folder),
                frame_count=frame_count,
                settings=_DEFAULT_SETTINGS,
            )
        )

    def _finish_error(self, generation: int, headline: str, detail: str, code: str | None) -> None:
        if not self._is_current(generation):
            return
        with self._lock:
            self._engine = None
        self._set_state(
            replace(
                self.state,
                session=None,
                phase="error",
                headline=headline,
                detail=detail,
                position=0,
                frame_count=0,
                playing=False,
                settings=_DEFAULT_SETTINGS,
                error_code=code,
            )
        )

    def _playback_state(self, generation: int, state: PlaybackState) -> None:
        try:
            self._dispatch(lambda: self._finish_playback_state(generation, state))
        except Exception:
            return

    def _finish_playback_state(self, generation: int, state: PlaybackState) -> None:
        if not self._is_current(generation):
            return
        self._set_state(
            replace(
                self.state,
                phase="playing" if state.playing else "ready",
                position=int(state.position),
                frame_count=int(state.frame_count),
                playing=bool(state.playing),
                error_code=None,
            )
        )

    def _playback_error(self, generation: int, error: PlaybackError) -> None:
        detail, _recovery, code = _error_parts(error)
        try:
            self._dispatch(lambda: self._finish_playback_error(generation, detail, code))
        except Exception:
            return

    def _finish_playback_error(self, generation: int, detail: str, code: str | None) -> None:
        if not self._is_current(generation):
            return
        with self._lock:
            self._engine = None
        self._set_state(
            replace(
                self.state,
                phase="error",
                headline="Playback failed",
                detail=detail,
                playing=False,
                error_code=code,
            )
        )

    def _valid_command_engine(self):
        return self._current_engine()

    def play(self) -> bool:
        engine = self._valid_command_engine()
        if engine is None:
            return False
        try:
            engine.play()
        except Exception:
            return False
        return True

    def pause(self) -> bool:
        engine = self._valid_command_engine()
        if engine is None:
            return False
        try:
            engine.pause()
        except Exception:
            return False
        return True

    def seek(self, frame: int | float) -> bool:
        engine = self._valid_command_engine()
        if engine is None:
            return False
        try:
            engine.seek(frame)
        except Exception:
            return False
        return True

    def _update_setting(self, stem_name: str, **changes) -> bool:
        engine = self._valid_command_engine()
        index = self._stem_index(stem_name)
        if engine is None or index is None:
            return False
        settings = list(self.state.settings.settings)
        try:
            settings[index] = replace(settings[index], **changes)
            snapshot = MixerSnapshot(tuple(settings))
            engine.apply(snapshot)
        except Exception:
            return False
        self._set_state(replace(self.state, settings=snapshot))
        return True

    def set_volume(self, stem_name: str, percent: int | float) -> bool:
        try:
            gain = gain_from_percent(percent)
        except Exception:
            return False
        return self._update_setting(stem_name, gain=gain)

    def set_volume_percent(self, stem_name: str, percent: int | float) -> bool:
        return self.set_volume(stem_name, percent)

    def set_muted(self, stem_name: str, muted: bool) -> bool:
        if not isinstance(muted, bool):
            return False
        return self._update_setting(stem_name, muted=muted)

    def set_mute(self, stem_name: str, muted: bool) -> bool:
        return self.set_muted(stem_name, muted)

    def toggle_mute(self, stem_name: str) -> bool:
        setting = self._setting(stem_name)
        return setting is not None and self.set_muted(stem_name, not setting.muted)

    def set_solo(self, stem_name: str, solo: bool) -> bool:
        if not isinstance(solo, bool):
            return False
        return self._update_setting(stem_name, solo=solo)

    def toggle_solo(self, stem_name: str) -> bool:
        setting = self._setting(stem_name)
        return setting is not None and self.set_solo(stem_name, not setting.solo)

    def _setting(self, stem_name: str):
        index = self._stem_index(stem_name)
        if index is None or self.state.session is None:
            return None
        return self.state.settings.settings[index]

    @staticmethod
    def _stem_index(stem_name: str):
        if not isinstance(stem_name, str):
            return None
        canonical = stem_name.lower()
        if not canonical.endswith(".wav"):
            canonical = f"{canonical}.wav"
        try:
            return STEM_NAMES.index(canonical)
        except ValueError:
            return None

    def _close_engine(self, engine) -> None:
        if engine is None:
            return
        try:
            engine.close()
        except Exception:
            return

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._generation += 1
            engine = self._engine
            self._engine = None
        self._close_engine(engine)
        self._set_state(
            replace(
                self.state,
                phase="closed",
                playing=False,
                session=None,
                position=0,
                frame_count=0,
            )
        )
        return True
