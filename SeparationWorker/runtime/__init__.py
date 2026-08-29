"""Portable per-job worker supervision contracts."""

from .supervisor import Supervisor, WorkerFailure, WorkerResult

__all__ = ["Supervisor", "WorkerFailure", "WorkerResult"]
