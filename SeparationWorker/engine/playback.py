"""Command-driven, synchronized playback for a validated stem session."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .mixer import MixerSnapshot, effective_gains
from .pcm import AudioContractError
from .stem_session import STEM_NAMES


_DEFAULT_BLOCK_SIZE = 4_096


class PlaybackError(AudioContractError):
    """Actionable error raised at the native audio or file boundary."""


@dataclass(frozen=True)
class PlaybackState:
    """A snapshot safe to pass from the playback worker to the UI."""

    position: int
    playing: bool
    frame_count: int

    @property
    def duration_frames(self) -> int:
        return self.frame_count


def _default_reader_factory(path):
    import soundfile as sf

    return sf.SoundFile(path)


def _default_stream_factory(**kwargs):
    import sounddevice as sd

    return sd.OutputStream(**kwargs)


def _error(code: str, cause: str, recovery: str) -> PlaybackError:
    return PlaybackError(code, cause, recovery)


class PlaybackEngine:
    """Play four aligned WAV readers through one worker-owned output stream.

    The public methods only enqueue commands. The worker is the sole owner of
    readers and the native stream, which keeps seek, settings, and cleanup
    serialized with block writes.
    """

    def __init__(
        self,
        session,
        *,
        stream_factory: Callable | None = None,
        reader_factory: Callable | None = None,
        on_state: Callable[[PlaybackState], None] | None = None,
        on_error: Callable[[PlaybackError], None] | None = None,
        block_size: int = _DEFAULT_BLOCK_SIZE,
    ):
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size <= 0:
            raise ValueError("block_size must be a positive integer")
        self._validate_session(session)
        self._session = session
        self._stream_factory = stream_factory or _default_stream_factory
        self._reader_factory = reader_factory or _default_reader_factory
        self._on_state = on_state
        self._on_error = on_error
        self._block_size = block_size
        self._commands = queue.Queue()
        self._lock = threading.Lock()
        self._position = 0
        self._playing = False
        self._closed = False
        self._closing = False
        self._error_reported = False
        self._readers = None
        self._stream = None
        self._snapshot = MixerSnapshot(tuple())
        self._thread = threading.Thread(target=self._run, name="limbus-playback", daemon=True)
        self._thread.start()

    @staticmethod
    def _validate_session(session):
        paths = tuple(getattr(session, "paths", ()))
        if len(paths) != len(STEM_NAMES):
            raise ValueError("Playback requires exactly four stem paths")
        for attribute in ("sample_rate", "channels", "frame_count"):
            value = getattr(session, attribute, None)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Playback session requires a positive {attribute}")

    @property
    def position(self) -> int:
        with self._lock:
            return self._position

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._playing

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def session(self):
        return self._session

    def play(self):
        self._enqueue("play")

    def pause(self):
        self._enqueue("pause")

    def seek(self, frame: int):
        if isinstance(frame, bool) or not isinstance(frame, (int, float)):
            raise ValueError("Playback seek requires a numeric frame")
        self._enqueue("seek", int(frame))

    def apply(self, snapshot: MixerSnapshot):
        if not isinstance(snapshot, MixerSnapshot):
            raise TypeError("Playback settings require a MixerSnapshot")
        self._enqueue("apply", snapshot)

    def replace(self, session):
        """Replace content through the same serialized lifecycle as close."""
        self._validate_session(session)
        self._enqueue("replace", session)

    def close(self):
        with self._lock:
            if self._closed or self._closing:
                thread = self._thread
            else:
                self._closing = True
                thread = self._thread
                self._commands.put(("close", None))
        if thread is not threading.current_thread():
            thread.join()

    def _enqueue(self, command, value=None):
        with self._lock:
            if self._closed or self._closing:
                return
            self._commands.put((command, value))

    def _state(self) -> PlaybackState:
        with self._lock:
            return PlaybackState(self._position, self._playing, self._session.frame_count)

    def _emit_state(self):
        if self._on_state is None:
            return
        try:
            self._on_state(self._state())
        except Exception:
            # UI callback failures must not take down the audio worker.
            return

    def _emit_error(self, error):
        if self._on_error is None:
            return
        try:
            self._on_error(error)
        except Exception:
            return

    def _run(self):
        try:
            while True:
                if not self._playing:
                    command, value = self._commands.get()
                    if self._handle(command, value):
                        return
                    continue

                # Drain commands before every block, making UI changes take
                # effect at the next block without touching native objects.
                while True:
                    try:
                        command, value = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if self._handle(command, value):
                        return
                    if not self._playing:
                        break
                if self._playing:
                    self._write_block()
        finally:
            self._stop_resources()
            with self._lock:
                self._playing = False
                self._closed = True
                self._closing = False

    def _handle(self, command, value):
        if command == "play":
            self._start_playback()
        elif command == "pause":
            if self._playing:
                self._playing = False
                self._emit_state()
        elif command == "seek":
            target = max(0, min(int(value), self._session.frame_count))
            try:
                self._seek_readers(target)
            except Exception as exc:
                self._fail(_error(
                    "playback.seek",
                    f"Could not seek the stem readers: {exc}.",
                    "Close the mixer and load the complete stem folder again.",
                ))
            else:
                with self._lock:
                    self._position = target
                self._emit_state()
        elif command == "apply":
            self._snapshot = value
        elif command == "replace":
            self._stop_resources()
            self._session = value
            with self._lock:
                self._position = 0
                self._playing = False
            self._error_reported = False
            self._emit_state()
        elif command == "close":
            self._stop_resources()
            with self._lock:
                self._playing = False
            self._emit_state()
            return True
        return False

    def _start_playback(self):
        if self._position >= self._session.frame_count:
            self._position = 0
        self._error_reported = False
        try:
            self._ensure_resources()
            self._seek_readers(self._position)
            with self._lock:
                self._playing = True
            self._emit_state()
        except PlaybackError as error:
            self._fail(error)
        except Exception as exc:
            self._fail(_error(
                "playback.output_open",
                f"Could not open audio playback: {exc}.",
                "Check the Windows output device and retry playback.",
            ))

    def _ensure_resources(self):
        if self._readers is not None and self._stream is not None:
            return
        readers = []
        stream = None
        try:
            for path in self._session.paths:
                readers.append(self._reader_factory(Path(path)))
            stream = self._stream_factory(
                samplerate=self._session.sample_rate,
                channels=self._session.channels,
                dtype="float32",
                blocksize=self._block_size,
            )
            if hasattr(stream, "start"):
                stream.start()
        except Exception as exc:
            for reader in readers:
                self._close_safely(reader)
            self._close_safely(stream)
            raise _error(
                "playback.output_open",
                f"Could not open the four stems or Windows audio output: {exc}.",
                "Check that all WAV files are readable and a Windows output device is available.",
            ) from exc
        self._readers = readers
        self._stream = stream

    def _seek_readers(self, target):
        if self._readers is None:
            return
        for reader in self._readers:
            reader.seek(target)

    def _write_block(self):
        remaining = self._session.frame_count - self._position
        if remaining <= 0:
            self._stop_resources()
            with self._lock:
                self._playing = False
            self._emit_state()
            return
        requested = min(self._block_size, remaining)
        gains = effective_gains(self._snapshot)
        mixed = np.zeros((requested, self._session.channels), dtype=np.float32)
        try:
            for stem_name, reader in zip(STEM_NAMES, self._readers):
                try:
                    block = np.asarray(
                        reader.read(requested, dtype="float32", always_2d=True), dtype=np.float32
                    )
                except Exception as exc:
                    raise _error(
                        "playback.reader_read",
                        f"Could not read {stem_name}: {exc}.",
                        "Replace the unreadable WAV and load the complete stem folder again.",
                    ) from exc
                if block.shape != (requested, self._session.channels):
                    raise _error(
                        "playback.reader_read",
                        f"{stem_name} returned {block.shape[0]} frames; expected {requested}.",
                        "Replace the truncated WAV and load the complete stem folder again.",
                    )
                gain = np.float32(gains.get(stem_name, 1.0))
                mixed += block * gain
            np.clip(mixed, -1.0, 1.0, out=mixed)
            self._stream.write(mixed)
        except PlaybackError as error:
            self._fail(error)
            return
        except Exception as exc:
            self._fail(_error(
                "playback.output_write",
                f"Audio output stopped while writing a stem block: {exc}.",
                "Verify the Windows output device and retry playback.",
            ))
            return
        with self._lock:
            self._position += requested
        self._emit_state()
        if self._position >= self._session.frame_count:
            self._stop_resources()
            with self._lock:
                self._playing = False
            self._emit_state()

    def _fail(self, error: PlaybackError):
        if self._error_reported:
            return
        self._error_reported = True
        self._stop_resources()
        with self._lock:
            self._playing = False
        self._emit_state()
        self._emit_error(error)

    def _stop_resources(self):
        stream, readers = self._stream, self._readers
        self._stream = None
        self._readers = None
        self._close_safely(stream, stop=True)
        for reader in readers or ():
            self._close_safely(reader)

    @staticmethod
    def _close_safely(resource, *, stop=False):
        if resource is None:
            return
        if stop and hasattr(resource, "stop"):
            try:
                resource.stop()
            except Exception:
                pass
        if hasattr(resource, "close"):
            try:
                resource.close()
            except Exception:
                pass
