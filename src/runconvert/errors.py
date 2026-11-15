"""Errors raised by the RunConvert client."""

from __future__ import annotations

from typing import Any, Optional


class RunConvertError(Exception):
    """The API said no: ``{"success": false, "error": {"code", "message"}}``.

    ``code`` is machine-readable (e.g. ``BILLING_INSUFFICIENT_CREDITS``). New codes can
    appear, so branch on ``status`` for anything you don't recognise.
    """

    def __init__(self, message: str, status: int = 0, code: str = "", body: Any = None) -> None:
        super().__init__(message)
        self.message = message
        #: HTTP status, or 0 when the request never got a response.
        self.status = status
        #: API error code, empty when the API sent none.
        self.code = code
        #: The raw response body, when there was one.
        self.body = body


class JobFailedError(RunConvertError):
    """The job ended with status ``error``. ``task`` is the first failed task (code, reason, message)."""

    def __init__(self, job: dict, task: Optional[dict] = None) -> None:
        message = (task or {}).get("message") or f"Job {job.get('id')} failed"
        super().__init__(message, 0, (task or {}).get("code", ""), job)
        self.job = job
        self.task = task


class WaitTimeoutError(RunConvertError):
    """``jobs.wait`` gave up before the job reached ``finished`` or ``error``."""

    def __init__(self, job: dict, timeout: float) -> None:
        super().__init__(f"Job {job.get('id')} still {job.get('status')} after {round(timeout)}s", 0, "WAIT_TIMEOUT", job)
        self.job = job
