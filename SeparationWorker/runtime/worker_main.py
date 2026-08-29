import socket


def install_network_denial():
    """Deny Python socket creation in addition to the platform sandbox boundary."""

    def denied(*_args, **_kwargs):
        raise PermissionError("Worker network access is denied by the offline runtime contract.")

    socket.socket = denied
    socket.create_connection = denied


def runtime_messages(job, identities, *, total_steps, cadence_seconds=5.0):
    """Yield deterministic protocol events for adapters to emit around local work."""
    yield 0.0, {
        "type": "hello",
        "protocol": {"major": 1},
        "worker": "1.0.0",
        "runtime": "portable",
        "capabilities": ["vocals", "drums", "bass", "other"],
        "identities": identities,
    }
    elapsed = 0.0
    for completed in range(1, total_steps + 1):
        elapsed += cadence_seconds
        yield elapsed, {
            "type": "progress",
            "job": job,
            "stage": "running",
            "completed": completed,
            "total": total_steps,
        }
        if completed != total_steps:
            yield elapsed, {"type": "heartbeat", "job": job, "stage": "running"}
    yield elapsed, {"type": "completed", "job": job, "manifest": {}}


def run_worker(job, identities, adapter, emit):
    """Run an injected local adapter; asset acquisition is intentionally out of scope."""
    install_network_denial()
    emit(next(runtime_messages(job, identities, total_steps=1))[1])
    try:
        manifest = adapter.run(job, emit)
        emit({"type": "completed", "job": job, "manifest": manifest})
    except Exception as error:
        emit(
            {
                "type": "failed",
                "job": job,
                "error": {
                    "code": "worker.inference_failed",
                    "stage": "running",
                    "cause": str(error) or type(error).__name__,
                    "recovery": "Inspect the local worker diagnostics and restart the complete job.",
                    "retryable": True,
                },
            }
        )
