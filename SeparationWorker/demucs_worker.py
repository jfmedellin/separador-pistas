"""Frozen Demucs entry point used by the portable Windows GUI."""

from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run Demucs' command-line entry point in the bundled worker process."""
    from demucs.separate import main as demucs_main

    demucs_main(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
