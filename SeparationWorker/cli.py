"""Command-line entry point for the Windows MVP."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from SeparationWorker.demucs_adapter import DemucsSeparationError, separate_audio
from SeparationWorker.engine.publication import PublicationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="limbus-separate",
        description="Separate one audio file into vocals, drums, bass, and other stems.",
    )
    parser.add_argument("audio_file", help="Input audio file")
    parser.add_argument("output_directory", help="New directory that will contain the four WAV stems")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = separate_audio(arguments.audio_file, arguments.output_directory)
    except (DemucsSeparationError, PublicationError) as error:
        print(error, file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
