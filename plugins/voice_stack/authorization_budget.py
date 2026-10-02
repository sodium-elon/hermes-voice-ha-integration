"""Bound intent evaluation without giving a background worker action authority."""
from __future__ import annotations

import threading
import time
from typing import Callable

# Keep ownership until the actual thread exits, not merely until its caller times
# out. A permanently stuck SDK request consumes this one slot, failing closed.
_start_lock = threading.Lock()
_worker: threading.Thread | None = None


def authorize_with_budget(
    evaluator: Callable[..., bool], text: str, *, budget_seconds: float = 5.0,
    wake_deadline: float | None = None, **context,
) -> bool:
    """Run only the intent evaluator; never dispatch a callback from the worker.

    Each call owns its result. A late allow cannot authorize any later turn.
    """
    global _worker
    budget_seconds = min(budget_seconds, 5.0)
    deadline = time.monotonic() + budget_seconds
    if wake_deadline is not None:
        deadline = min(deadline, wake_deadline)
    if deadline <= time.monotonic() or not _start_lock.acquire(blocking=False):
        return False
    result = [False]

    def evaluate() -> None:
        try:
            result[0] = evaluator(text, **context) is True
        except Exception:
            pass  # SDK failures are denials, not pipeline errors.

    try:
        if _worker is not None and _worker.is_alive():
            return False
        worker = threading.Thread(target=evaluate, daemon=True,
                                  name="voice-intent-authorization")
        _worker = worker
        try:
            worker.start()
        except Exception:
            return False
    finally:
        _start_lock.release()
    worker.join(timeout=max(0.0, deadline - time.monotonic()))
    return (not worker.is_alive() and time.monotonic() < deadline
            and result[0])
