import hashlib
import json
import tempfile
from pathlib import Path

from .mixer import MixSetting, MixerSnapshot, render_mix
from .pcm import PlanarPCM, compute_residual
from .publication import publish_atomic
from .wav import encode_float32_wav


def _pcm(left, right):
    return PlanarPCM(48_000, (tuple(left), tuple(right)))


def build_synthetic_oracle():
    vocals = _pcm((0.10, -0.10, 0.20, -0.20), (0.05, -0.05, 0.10, -0.10))
    drums = _pcm((0.20, 0.00, -0.20, 0.00), (0.10, 0.00, -0.10, 0.00))
    bass = _pcm((0.05, 0.05, 0.05, 0.05), (0.025, 0.025, 0.025, 0.025))
    source = _pcm((0.36, -0.04, 0.06, -0.14), (0.18, -0.02, 0.03, -0.07))
    other = compute_residual(source, vocals, drums, bass).pcm
    stems = {"Vocals": vocals, "Drums": drums, "Bass": bass, "Other": other}
    snapshot = MixerSnapshot(tuple(MixSetting(name) for name in stems))
    remix = render_mix(stems, snapshot)
    wav = encode_float32_wav(remix)
    report = {
        "fixture": "synthetic-project-generated",
        "rights": "No third-party audio; numeric samples are generated in source.",
        "calibration": "CALIBRATION REQUIRED",
        "frames": remix.frame_count,
        "channels": remix.channel_count,
        "sample_rate": remix.sample_rate,
        "wav_sha256": hashlib.sha256(wav).hexdigest(),
    }
    return wav, json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main():
    first_wav, first_report = build_synthetic_oracle()
    second_wav, second_report = build_synthetic_oracle()
    if (first_wav, first_report) != (second_wav, second_report):
        raise SystemExit("FAIL: synthetic fixture generation was not deterministic")

    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    published = publish_atomic(root, "fixture-result", {"remix.wav": first_wav, "oracle.json": first_report})
    if sorted(path.name for path in published.iterdir()) != ["oracle.json", "remix.wav"]:
        raise SystemExit("FAIL: atomic fixture publication was incomplete")
    digest = hashlib.sha256(first_wav).hexdigest()
    temporary.cleanup()
    if root.exists():
        raise SystemExit("FAIL: fixture harness temporary files were not cleaned")
    print(f"PASS: deterministic synthetic Float32 WAV {digest}; atomic publication; cleanup confirmed; CALIBRATION REQUIRED")


if __name__ == "__main__":
    main()
