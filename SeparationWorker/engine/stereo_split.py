"""Deterministic stereo-position split of one already-isolated stem.

Conventional metal production doubles the rhythm guitars and pans them hard
while solos and melodies sit centred, so the stereo position of an isolated
guitar stem already carries most of the role information. This module reads
that position.

It does NOT identify musical roles. A centred rhythm part lands in the centre
lane and a wide harmonised lead lands in the sides lane, and nothing here can
tell the difference. That is why the lanes it feeds are named for the position
they describe and never for a role, and why this is not, and cannot be
presented as, Lead/Rhythm separation.

Two properties make it safe to publish through the same gates a trained
specialist would face. The split is lossless up to float32 rounding, so centre
plus sides reconstructs the input at roughly 140 dB and clears any calibrated
reconstruction limit by a wide margin. And it is deterministic: there are no
weights, no license, and nothing to admit.
"""

from __future__ import annotations

import numpy as np

SPLITTER_ID = "center-sides-v1"


class StereoSplitError(ValueError):
    """Raised when a stem cannot be split by stereo position."""


def split_center_sides(samples) -> tuple[np.ndarray, np.ndarray]:
    """Return the centred and hard-panned halves of one stem.

    The pair sums back to the input: ``centre + sides`` reproduces ``samples``
    up to float32 rounding, which is far below any audible or gated limit.

    Mono input has no panning information at all, so everything is centred and
    the sides come back as digital silence. That is a truthful result, not a
    failure: the caller's absence handling publishes the silent lane with its
    reason rather than inventing content for it.
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 2 or samples.shape[0] == 0:
        raise StereoSplitError("A stereo split needs a two-dimensional frames-by-channels signal.")
    channels = samples.shape[1]
    if channels == 1:
        return samples.copy(), np.zeros_like(samples)
    if channels != 2:
        raise StereoSplitError(f"A stereo split needs one or two channels, not {channels}.")

    left = samples[:, 0].astype(np.float64)
    right = samples[:, 1].astype(np.float64)
    mid = (left + right) / 2.0
    side = (left - right) / 2.0
    center = np.stack([mid, mid], axis=1).astype(np.float32)
    sides = np.stack([side, -side], axis=1).astype(np.float32)
    return center, sides
