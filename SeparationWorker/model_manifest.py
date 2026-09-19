"""Frozen registry of admitted Demucs model weights (SEC-01).

Every file `model_manager.ensure_model()` is allowed to place under its
app-owned `LocalRepo`-shaped cache directory is listed here, with a full
SHA-256 digest pinned in code. This mirrors the admission pattern
`guitar_adapter.REGISTERED_SPECIALISTS`/`resolve_specialist` already ships:
a frozen constant, not an editable data file, so PyInstaller collects it as
ordinary Python source and an attacker who can write to the cache directory
still cannot make `ensure_model()` accept tampered bytes (D2).

The `.th` weight files are fetched over HTTPS from Demucs' own trusted CDN
(`https://dl.fbaipublicfiles.com/demucs/...`, per the vendored
`demucs/remote/files.txt`); the `.yaml` bag descriptors are copied from the
already-vendored `demucs/remote/` package data and re-verified, never
downloaded (D7). Both digests below were independently computed with
`sha256sum` against the downloaded/vendored bytes; the `.th` file name's own
8-hex-char suffix is Demucs' own naming convention (the first 8 hex
characters of its true SHA-256) and matches the computed digest, which is
independent confirmation the bytes are genuine and unmodified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class ModelFile:
    """One file `ensure_model()` must place, verified, under the repo directory."""

    file_name: str
    sha256: str
    source: str  # "download" | "bundled"
    urls: tuple[str, ...] = field(default_factory=tuple)  # https:// only; empty for "bundled"


@dataclass(frozen=True)
class ModelEntry:
    """One admitted model profile: its `--name` and every file it needs."""

    model_name: str
    files: tuple[ModelFile, ...]


_HYBRID_TRANSFORMER_BASE_URL = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/"


REGISTERED_MODELS: Mapping[str, ModelEntry] = {
    "htdemucs": ModelEntry(
        model_name="htdemucs",
        files=(
            ModelFile(
                file_name="955717e8-8726e21a.th",
                sha256="8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4",
                source="download",
                urls=(f"{_HYBRID_TRANSFORMER_BASE_URL}955717e8-8726e21a.th",),
            ),
            ModelFile(
                file_name="htdemucs.yaml",
                sha256="239c445d0b14454d541ad8bd9bb271c9e536d267e8a4625208744cbb2e7bb66c",
                source="bundled",
            ),
        ),
    ),
    "htdemucs_6s": ModelEntry(
        model_name="htdemucs_6s",
        files=(
            ModelFile(
                file_name="5c90dfd2-34c22ccb.th",
                sha256="34c22ccb381c6f9fdbf324f04e1e2fe21aaaf293f5ded163a162697ff9a02ddd",
                source="download",
                urls=(f"{_HYBRID_TRANSFORMER_BASE_URL}5c90dfd2-34c22ccb.th",),
            ),
            ModelFile(
                file_name="htdemucs_6s.yaml",
                sha256="207405151270af8fd81c2373c25d27950916682ac91dca7884a11ce13dad6f58",
                source="bundled",
            ),
        ),
    ),
}
