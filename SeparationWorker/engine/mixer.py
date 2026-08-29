import math
from dataclasses import dataclass

from .pcm import AudioContractError, PlanarPCM, require_common_identity


@dataclass(frozen=True)
class MixSetting:
    name: str
    gain: float = 1.0
    muted: bool = False
    solo: bool = False

    def __post_init__(self):
        if not self.name or not math.isfinite(self.gain) or self.gain < 0:
            raise AudioContractError(
                "mixer.invalid_setting",
                "Mixer names must be present and gains must be finite and non-negative.",
                "Capture a valid immutable mixer state and retry export.",
            )


@dataclass(frozen=True)
class MixerSnapshot:
    settings: tuple

    def __post_init__(self):
        settings = tuple(self.settings)
        if len({setting.name for setting in settings}) != len(settings):
            raise AudioContractError(
                "mixer.duplicate_stem",
                "The mixer snapshot contains a duplicate stem.",
                "Capture exactly one state entry for each published stem.",
            )
        object.__setattr__(self, "settings", settings)


def render_mix(stems, snapshot):
    names = {setting.name for setting in snapshot.settings}
    if names != set(stems):
        raise AudioContractError(
            "mixer.identity_mismatch",
            "Mixer state does not identify exactly the supplied stems.",
            "Reload the complete result and capture a new immutable state.",
        )
    ordered = [(setting, stems[setting.name]) for setting in snapshot.settings]
    reference = ordered[0][1] if ordered else None
    if reference is None:
        raise AudioContractError(
            "mixer.empty",
            "No stems were supplied to the mixer oracle.",
            "Load a validated published result before export.",
        )
    require_common_identity(reference, *(audio for _, audio in ordered[1:]))
    solo_active = any(setting.solo for setting, _ in ordered)
    channels = []
    for channel_index in range(reference.channel_count):
        output = []
        for frame in range(reference.frame_count):
            mixed64 = 0.0
            for setting, audio in ordered:
                audible = not setting.muted and (not solo_active or setting.solo)
                if audible:
                    mixed64 += float(audio.planar[channel_index][frame]) * float(setting.gain)
            output.append(mixed64)
        channels.append(tuple(output))
    return PlanarPCM(reference.sample_rate, tuple(channels))
