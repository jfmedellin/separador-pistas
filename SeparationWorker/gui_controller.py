"""Window-independent state and background execution for the Windows GUI."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from SeparationWorker.demucs_adapter import separate_audio
from SeparationWorker.engine import stem_cache
from SeparationWorker.engine.stem_profile import (
    LEGACY_PROFILE,
    PROFILES,
    StemProfile,
    StemProfileError,
    resolve_profile,
)


def _lane_summary(profile: StemProfile) -> str:
    """Return a readable list of the lanes a profile publishes."""
    names = [lane.display_name.lower() for lane in profile.lanes]
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _remediation(profile: StemProfile) -> str:
    """Explain, actionably, why a profile cannot run."""
    if profile.enabled:
        return ""
    if profile.specialist_input is not None and profile.specialist_id is None:
        return (
            f"No {profile.display_name} specialist has been admitted, so this profile cannot run. "
            "Admit one with Tools/admit_metal_guitar_model.py, then restart Stemslayer."
        )
    return f"The {profile.display_name} profile is not available in this build."


@dataclass(frozen=True)
class GuiState:
    input_file: str = ""
    result_directory: Path | None = None
    phase: str = "idle"
    headline: str = "Choose a song to separate"
    detail: str = f"The result will contain {_lane_summary(LEGACY_PROFILE)}."
    profile_id: str = LEGACY_PROFILE.profile_id
    profile_available: bool = True
    profile_remediation: str = ""
    job_id: str | None = None

    @property
    def can_start(self) -> bool:
        return bool(self.input_file) and self.phase != "running" and self.profile_available


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="stemslayer-separation", daemon=True).start()


class SeparationController:
    def __init__(
        self,
        *,
        separate: Callable[..., Path] = separate_audio,
        cache_directory: Callable[..., Path] = stem_cache.prepare_cache_directory,
        start_worker: Callable[[Callable[[], None]], None] = _start_thread,
        dispatch: Callable[[Callable[[], None]], None] = lambda callback: callback(),
        on_change: Callable[[GuiState], None] = lambda _state: None,
        on_success: Callable[[Path], None] = lambda _result: None,
        profile: StemProfile = LEGACY_PROFILE,
    ):
        self._separate = separate
        self._cache_directory = cache_directory
        self._start_worker = start_worker
        self._dispatch = dispatch
        self._on_change = on_change
        self._on_success = on_success
        self._profile = profile
        self.state = GuiState(
            detail=f"The result will contain {_lane_summary(profile)}.",
            profile_id=profile.profile_id,
            profile_available=profile.enabled,
            profile_remediation=_remediation(profile),
        )

    @property
    def profile(self) -> StemProfile:
        return self._profile

    @staticmethod
    def available_profiles() -> tuple[StemProfile, ...]:
        """Return every registered profile, enabled or not.

        Unavailable profiles are still offered so the view can show why they
        cannot run instead of hiding a feature the user was told exists.
        """
        return tuple(PROFILES.values())

    def _set_state(self, state: GuiState) -> None:
        self.state = state
        self._on_change(state)

    def _result_directory(self, path: str) -> Path | None:
        return self._cache_directory(path, profile=self._profile) if path else None

    def set_profile(self, profile_id: str) -> bool:
        """Select a registered profile, reporting an unavailable one actionably."""
        if self.state.phase == "running":
            return False
        try:
            profile = resolve_profile(profile_id)
        except StemProfileError:
            return False
        self._profile = profile
        available = profile.enabled
        remediation = _remediation(profile)
        has_input = bool(self.state.input_file)
        if not available:
            headline = f"{profile.display_name} is not available yet"
            detail = remediation
            phase = "unavailable"
        elif has_input:
            headline = "Ready to separate"
            detail = profile.note or f"{len(profile.lanes)} WAV files will be prepared for the mixer."
            phase = "ready"
        else:
            headline = "Choose a song to separate"
            detail = profile.note or f"The result will contain {_lane_summary(profile)}."
            phase = "idle"
        self._set_state(
            replace(
                self.state,
                result_directory=self._result_directory(self.state.input_file) if available else None,
                phase=phase,
                headline=headline,
                detail=detail,
                profile_id=profile.profile_id,
                profile_available=available,
                profile_remediation=remediation,
            )
        )
        return True

    def set_input_file(self, path: str) -> bool:
        if self.state.phase == "running":
            return False
        if not self._profile.enabled:
            self._set_state(replace(self.state, input_file=path, result_directory=None))
            return True
        result_directory = self._result_directory(path)
        ready = bool(path)
        self._set_state(
            replace(
                self.state,
                input_file=path,
                result_directory=result_directory,
                phase="ready" if ready else "idle",
                headline="Ready to separate" if ready else "Choose a song to separate",
                detail=(
                    f"{len(self._profile.lanes)} WAV files will be prepared for the mixer."
                    if ready
                    else f"The result will contain {_lane_summary(self._profile)}."
                ),
            )
        )
        return True

    def start(self, *, result_directory: str | Path | None = None, job_id: str | None = None) -> bool:
        """Start one stable job, optionally publishing to an explicit managed directory."""
        if self.state.phase == "running":
            return False
        if not self._profile.enabled:
            self._set_state(
                replace(
                    self.state,
                    phase="unavailable",
                    headline=f"{self._profile.display_name} is not available yet",
                    detail=self.state.profile_remediation or _remediation(self._profile),
                )
            )
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
        if result_directory is not None:
            try:
                result_directory = Path(result_directory)
            except (TypeError, ValueError):
                return False
        else:
            result_directory = self.state.result_directory
        if result_directory is None:
            return False
        profile = self._profile
        self._set_state(
            replace(
                self.state,
                phase="running",
                headline=f"Separating into {len(profile.lanes)} stems",
                detail="This can take several minutes. Keep this window open.",
                result_directory=result_directory,
                job_id=job_id,
            )
        )
        self._start_worker(lambda: self._run(input_file, result_directory, profile))
        return True

    def _run(self, input_file: str, result_directory: Path, profile: StemProfile) -> None:
        try:
            result = self._separate(input_file, result_directory, profile=profile)
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
                headline=f"{len(self._profile.lanes)} stems are ready",
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
