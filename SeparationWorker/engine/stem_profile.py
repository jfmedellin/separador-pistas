"""Immutable stem layout profiles and the manifest every published result carries.

A profile is the single authority for one separation layout: which lanes exist,
in which order, which model produced them, which raw outputs fold into the
residual, and whether a lane is allowed to publish as silence. Every other
layer reads the layout from here instead of re-deriving it from file names.

The Metal profile declares that it needs a Lead/Rhythm specialist but registers
none, so it cannot be enabled: a profile that requires a specialist and has not
been given one is rejected at construction time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .pcm import AudioContractError
from .stereo_split import SPLITTER_ID

MANIFEST_NAME = "stem-result.json"
MANIFEST_SCHEMA_VERSION = 1

LEGACY_PROFILE_ID = "legacy-four-stem"
METAL_PROFILE_ID = "metal-six-stem"
METAL_STEREO_PROFILE_ID = "metal-stereo-six-stem"


class StemProfileError(AudioContractError):
    """Actionable error raised when a profile or result manifest is invalid."""


def _error(code: str, cause: str, recovery: str) -> StemProfileError:
    return StemProfileError(code, cause, recovery)


def _canonical_bytes(payload) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True)
class StemLane:
    """One published lane of a profile."""

    lane_id: str
    display_name: str
    role_group: str | None = None
    residual: bool = False
    absentable: bool = False

    @property
    def file_name(self) -> str:
        return f"{self.lane_id}.wav"


@dataclass(frozen=True)
class StemProfile:
    """An immutable, ordered stem layout with its producing pipeline identity."""

    profile_id: str
    display_name: str
    lanes: tuple[StemLane, ...]
    primary_model: str
    raw_outputs: tuple[str, ...]
    residual_sources: tuple[str, ...] = ()
    specialist_input: str | None = None
    specialist_id: str | None = None
    split_input: str | None = None
    splitter_id: str | None = None
    enabled: bool = True
    accepts_manifestless_results: bool = False
    # Shown when the profile is selected. Cosmetic, so it stays out of the
    # pipeline fingerprint and never invalidates a cached result.
    note: str = ""

    def __post_init__(self) -> None:
        if not self.lanes:
            raise _error(
                "profile.empty",
                f"Profile {self.profile_id!r} declares no lanes.",
                "Declare at least one ordered lane.",
            )
        lane_ids = [lane.lane_id for lane in self.lanes]
        if len(set(lane_ids)) != len(lane_ids):
            raise _error(
                "profile.duplicate_lane",
                f"Profile {self.profile_id!r} repeats a lane identifier.",
                "Declare each lane exactly once.",
            )
        if len([lane for lane in self.lanes if lane.residual]) > 1:
            raise _error(
                "profile.multiple_residuals",
                f"Profile {self.profile_id!r} declares more than one residual lane.",
                "Fold every unseparated source into a single residual lane.",
            )
        unknown_residual = sorted(set(self.residual_sources) - set(self.raw_outputs))
        if unknown_residual:
            raise _error(
                "profile.unknown_residual_source",
                f"Residual sources are not produced by {self.primary_model!r}: {', '.join(unknown_residual)}.",
                "Fold only raw outputs the primary model actually emits.",
            )
        if self.residual_sources and not any(lane.residual for lane in self.lanes):
            raise _error(
                "profile.missing_residual_lane",
                f"Profile {self.profile_id!r} folds sources without declaring a residual lane.",
                "Declare the residual lane that receives the folded sources.",
            )
        if self.specialist_input is not None and self.specialist_input not in self.raw_outputs:
            raise _error(
                "profile.unknown_specialist_input",
                f"The specialist input {self.specialist_input!r} is not produced by {self.primary_model!r}.",
                "Feed the specialist a raw output the primary model emits.",
            )
        # A profile that needs a specialist and has none can never be enabled.
        # This is the activation gate expressed as a construction invariant.
        if self.enabled and self.specialist_input is not None and self.specialist_id is None:
            raise _error(
                "profile.enabled_without_specialist",
                f"Profile {self.profile_id!r} requires a specialist but none is registered.",
                "Admit a specialist before enabling this profile.",
            )
        if self.specialist_input is not None and self.split_input is not None:
            raise _error(
                "profile.conflicting_decomposition",
                f"Profile {self.profile_id!r} declares both a specialist and a splitter.",
                "Decompose a stem one way only.",
            )
        if self.split_input is not None:
            if self.split_input not in self.raw_outputs:
                raise _error(
                    "profile.unknown_split_input",
                    f"The split input {self.split_input!r} is not produced by {self.primary_model!r}.",
                    "Split a raw output the primary model actually emits.",
                )
            if not self.splitter_id:
                raise _error(
                    "profile.missing_splitter",
                    f"Profile {self.profile_id!r} declares a split input with no splitter.",
                    "Name the deterministic splitter that produces the lanes.",
                )

    @property
    def lane_ids(self) -> tuple[str, ...]:
        return tuple(lane.lane_id for lane in self.lanes)

    @property
    def file_names(self) -> tuple[str, ...]:
        return tuple(lane.file_name for lane in self.lanes)

    @property
    def residual_lane(self) -> StemLane | None:
        return next((lane for lane in self.lanes if lane.residual), None)

    def lane(self, lane_id: str) -> StemLane:
        for lane in self.lanes:
            if lane.lane_id == lane_id:
                return lane
        raise _error(
            "profile.unknown_lane",
            f"Profile {self.profile_id!r} has no lane {lane_id!r}.",
            "Reference a lane declared by this profile.",
        )

    def role_lanes(self, role_group: str) -> tuple[StemLane, ...]:
        return tuple(lane for lane in self.lanes if lane.role_group == role_group)

    @property
    def pipeline_fingerprint(self) -> str:
        """Return the digest of everything that changes what a result contains."""
        identity = {
            "schema": MANIFEST_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "primary_model": self.primary_model,
            "specialist_id": self.specialist_id,
            "specialist_input": self.specialist_input,
            "splitter_id": self.splitter_id,
            "split_input": self.split_input,
            "raw_outputs": list(self.raw_outputs),
            "residual_sources": list(self.residual_sources),
            "lanes": list(self.lane_ids),
        }
        return hashlib.sha256(_canonical_bytes(identity)).hexdigest()


LEGACY_PROFILE = StemProfile(
    profile_id=LEGACY_PROFILE_ID,
    display_name="Legacy",
    lanes=(
        StemLane("vocals", "Vocals"),
        StemLane("drums", "Drums"),
        StemLane("bass", "Bass"),
        StemLane("other", "Other", residual=True),
    ),
    primary_model="htdemucs",
    raw_outputs=("vocals", "drums", "bass", "other"),
    enabled=True,
    accepts_manifestless_results=True,
)

METAL_PROFILE = StemProfile(
    profile_id=METAL_PROFILE_ID,
    display_name="Metal Roles",
    lanes=(
        StemLane("vocals", "Vocals"),
        StemLane("drums", "Drums"),
        StemLane("bass", "Bass"),
        StemLane("lead_guitar", "Lead Guitar", role_group="guitar", absentable=True),
        StemLane("rhythm_guitar", "Rhythm Guitar", role_group="guitar", absentable=True),
        StemLane("other", "Other", residual=True),
    ),
    primary_model="htdemucs_6s",
    raw_outputs=("vocals", "drums", "bass", "guitar", "piano", "other"),
    residual_sources=("piano", "other"),
    specialist_input="guitar",
    specialist_id=None,
    enabled=False,
    accepts_manifestless_results=False,
)

# Stereo position is not a musical role. This profile splits the isolated
# guitar by where it sits in the image, which in conventional metal production
# usually puts solos in the centre and doubled rhythms on the sides. It cannot
# tell a centred rhythm from a solo, so its lanes are named for position and
# it is never presented as Lead/Rhythm separation. Nothing is admitted here:
# the split is deterministic, with no weights and no license.
METAL_STEREO_PROFILE = StemProfile(
    profile_id=METAL_STEREO_PROFILE_ID,
    display_name="Metal Stereo",
    lanes=(
        StemLane("vocals", "Vocals"),
        StemLane("drums", "Drums"),
        StemLane("bass", "Bass"),
        StemLane("guitar_center", "Guitar Center", role_group="guitar", absentable=True),
        StemLane("guitar_sides", "Guitar Sides", role_group="guitar", absentable=True),
        StemLane("other", "Other", residual=True),
    ),
    primary_model="htdemucs_6s",
    raw_outputs=("vocals", "drums", "bass", "guitar", "piano", "other"),
    residual_sources=("piano", "other"),
    split_input="guitar",
    splitter_id=SPLITTER_ID,
    enabled=True,
    accepts_manifestless_results=False,
    note=(
        "Splits the isolated guitar by stereo position. In conventional metal production "
        "solos usually sit in the centre and doubled rhythms on the sides, but position is "
        "not role: a centred rhythm lands in the centre lane."
    ),
)

PROFILES: Mapping[str, StemProfile] = {
    LEGACY_PROFILE.profile_id: LEGACY_PROFILE,
    METAL_STEREO_PROFILE.profile_id: METAL_STEREO_PROFILE,
    METAL_PROFILE.profile_id: METAL_PROFILE,
}


def resolve_profile(profile_id: str) -> StemProfile:
    """Return a registered profile, or fail actionably."""
    profile = PROFILES.get(profile_id)
    if profile is None:
        raise _error(
            "profile.unknown",
            f"No stem profile is registered under {profile_id!r}.",
            f"Request one of: {', '.join(sorted(PROFILES))}.",
        )
    return profile


@dataclass(frozen=True)
class PublishedLane:
    """One lane as it was actually published."""

    lane_id: str
    absent: bool = False
    absence_reason: str | None = None

    @property
    def file_name(self) -> str:
        return f"{self.lane_id}.wav"


@dataclass(frozen=True)
class StemResultManifest:
    """The immutable record a published result carries beside its WAV files."""

    profile_id: str
    pipeline_fingerprint: str
    lanes: tuple[PublishedLane, ...]
    omitted: tuple[str, ...] = ()
    validation: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = MANIFEST_SCHEMA_VERSION

    @property
    def file_names(self) -> tuple[str, ...]:
        return tuple(lane.file_name for lane in self.lanes)

    @property
    def absent_lane_ids(self) -> tuple[str, ...]:
        return tuple(lane.lane_id for lane in self.lanes if lane.absent)

    def to_json_bytes(self) -> bytes:
        return _canonical_bytes(
            {
                "schema_version": self.schema_version,
                "profile_id": self.profile_id,
                "pipeline_fingerprint": self.pipeline_fingerprint,
                "lanes": [
                    {
                        "lane_id": lane.lane_id,
                        "absent": lane.absent,
                        "absence_reason": lane.absence_reason,
                    }
                    for lane in self.lanes
                ],
                "omitted": list(self.omitted),
                "validation": dict(self.validation),
            }
        )


def build_manifest(
    profile: StemProfile,
    *,
    absent_lanes: Mapping[str, str] | None = None,
    omitted: Sequence[str] = (),
    validation: Mapping[str, object] | None = None,
) -> StemResultManifest:
    """Return the manifest for one complete result, rejecting invalid layouts.

    A role lane may publish as declared silence; a residual lane may be omitted
    entirely. Neither is allowed for any other lane, and the two are mutually
    exclusive for the same lane.
    """
    absent_lanes = dict(absent_lanes or {})
    omitted = tuple(omitted)
    overlap = sorted(set(absent_lanes) & set(omitted))
    if overlap:
        raise _error(
            "manifest.conflicting_lane",
            f"A lane cannot be both absent and omitted: {', '.join(overlap)}.",
            "Publish an absent role lane as silence, or omit a negligible residual, never both.",
        )
    for lane_id, reason in absent_lanes.items():
        lane = profile.lane(lane_id)
        if not lane.absentable:
            raise _error(
                "manifest.lane_not_absentable",
                f"Lane {lane_id!r} may not publish as silence.",
                "Publish every required lane, or fail the job when its content is missing.",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise _error(
                "manifest.missing_absence_reason",
                f"Absent lane {lane_id!r} carries no calibrated reason.",
                "Record why the role is absent before publishing the silent lane.",
            )
    for lane_id in omitted:
        lane = profile.lane(lane_id)
        if not lane.residual:
            raise _error(
                "manifest.lane_not_omittable",
                f"Lane {lane_id!r} is not a residual and may not be omitted.",
                "Publish the lane, or declare an absent role lane as silence instead.",
            )
    lanes = tuple(
        PublishedLane(lane.lane_id, lane.lane_id in absent_lanes, absent_lanes.get(lane.lane_id))
        for lane in profile.lanes
        if lane.lane_id not in omitted
    )
    return StemResultManifest(
        profile_id=profile.profile_id,
        pipeline_fingerprint=profile.pipeline_fingerprint,
        lanes=lanes,
        omitted=omitted,
        validation=dict(validation or {}),
    )


def parse_manifest(data: bytes) -> StemResultManifest:
    """Return the manifest recorded in a published result, or fail actionably."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(
            "manifest.unreadable",
            f"The result manifest is not readable JSON: {exc}",
            "Discard the result and separate the song again.",
        ) from exc
    if not isinstance(payload, dict):
        raise _error(
            "manifest.unreadable",
            "The result manifest is not an object.",
            "Discard the result and separate the song again.",
        )
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise _error(
            "manifest.unsupported_schema",
            f"Only manifest schema version {MANIFEST_SCHEMA_VERSION} is supported.",
            "Discard the result and separate the song again with this build.",
        )
    raw_lanes = payload.get("lanes")
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise _error(
            "manifest.unreadable",
            "The result manifest declares no lanes.",
            "Discard the result and separate the song again.",
        )
    lanes = []
    for entry in raw_lanes:
        if not isinstance(entry, dict) or not isinstance(entry.get("lane_id"), str):
            raise _error(
                "manifest.unreadable",
                "A manifest lane entry is malformed.",
                "Discard the result and separate the song again.",
            )
        lanes.append(
            PublishedLane(entry["lane_id"], bool(entry.get("absent")), entry.get("absence_reason"))
        )
    return StemResultManifest(
        profile_id=str(payload.get("profile_id", "")),
        pipeline_fingerprint=str(payload.get("pipeline_fingerprint", "")),
        lanes=tuple(lanes),
        omitted=tuple(payload.get("omitted") or ()),
        validation=payload.get("validation") or {},
    )


def verify_manifest(manifest: StemResultManifest, profile: StemProfile) -> StemResultManifest:
    """Confirm a parsed manifest belongs to this exact profile and pipeline."""
    if manifest.profile_id != profile.profile_id:
        raise _error(
            "manifest.profile_mismatch",
            f"The result was produced by {manifest.profile_id!r}, not {profile.profile_id!r}.",
            "Separate the song again with the requested profile.",
        )
    if manifest.pipeline_fingerprint != profile.pipeline_fingerprint:
        raise _error(
            "manifest.fingerprint_mismatch",
            "The result was produced by a different pipeline identity.",
            "Separate the song again; results are never reused across pipelines.",
        )
    expected = build_manifest(
        profile,
        absent_lanes={lane.lane_id: lane.absence_reason or "" for lane in manifest.lanes if lane.absent},
        omitted=manifest.omitted,
    )
    if expected.file_names != manifest.file_names:
        raise _error(
            "manifest.layout_mismatch",
            "The recorded lanes do not match the profile layout or order.",
            "Discard the result and separate the song again.",
        )
    return manifest
