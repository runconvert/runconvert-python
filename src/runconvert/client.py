"""The RunConvert API client (v3). Standard library only."""

from __future__ import annotations

import json
import os
import platform
import random
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional, Union

from . import webhooks as _webhooks
from .errors import JobFailedError, RunConvertError, WaitTimeoutError

__version__ = "0.1.0"  # x-release-please-version

DEFAULT_BASE_URL = "https://api.runconvert.com/v3"
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
FileInput = Union[str, "os.PathLike[str]", bytes, BinaryIO]


@dataclass
class HTTPResponse:
    status: int
    headers: Dict[str, str]
    body: bytes = b""


@dataclass
class ConvertResult:
    #: The finished job, with its tasks.
    job: dict
    #: The converted files, each with ``filename``, ``url`` (temporary) and ``size``.
    files: List[dict]
    #: Local paths, when ``out_dir`` was given.
    saved: List[str] = field(default_factory=list)


class RunConvert:
    """The RunConvert API client.

    >>> rc = RunConvert()  # reads RUNCONVERT_API_KEY
    >>> rc.convert("report.docx", to="pdf", out_dir="out").saved
    ['out/report.pdf']
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("RUNCONVERT_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = f"runconvert-python/{__version__} python/{platform.python_version()}"
        self.jobs = Jobs(self)
        self.operations = Operations(self)
        self.files = Files(self)
        self.webhooks = Webhooks()

    # ---- high level ----

    def me(self) -> dict:
        """The account, team and credit balance behind the API key."""
        return self.request("GET", "/me")

    def convert(
        self,
        file: FileInput,
        to: str,
        *,
        from_: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        out_dir: Optional[str] = None,
        filename: Optional[str] = None,
        tag: Optional[str] = None,
        on_update: Optional[Callable[[dict], None]] = None,
        timeout: Optional[float] = None,
    ) -> ConvertResult:
        """Converts one file in one call: upload (or import a URL), convert, wait, download.

        ``file`` is a path, an ``http(s)://`` URL, bytes or a binary file object. ``from_`` is
        read from the name when left out. Engine options (e.g. ``{"audioBitrate": "192k"}``)
        go in ``options``.
        """
        is_url = isinstance(file, str) and file.lower().startswith(("http://", "https://"))
        if filename:
            name = filename
        elif is_url:
            name = urllib.parse.urlparse(str(file)).path
        elif isinstance(file, (str, os.PathLike)):
            name = os.fspath(file)
        else:
            name = getattr(file, "name", "") or ""
        source = (from_ or os.path.splitext(str(name))[1].lstrip(".")).lower()
        if not source:
            raise ValueError('Can\'t tell the source format: pass from_="mp4" or filename="video.mp4".')
        target = to.lower().lstrip(".")
        import_task = {"operation": "import/url", "source_url": file} if is_url else {"operation": "import/direct/upload"}
        convert_task = dict(options or {})
        convert_task.update({"operation": "convert", "input": "import-1", "input_format": source, "output_format": target})
        job = self.jobs.create(
            {"import-1": import_task, "convert-1": convert_task, "export-1": {"operation": "export/url", "input": "convert-1"}},
            tag=tag,
        )
        if not is_url:
            task = next((t for t in job.get("tasks") or [] if t.get("name") == "import-1"), None)
            if not task or not task.get("upload_url"):
                raise RunConvertError("The job has no upload URL", body=job)
            self.files.upload(task, file, filename=os.path.basename(str(name)) if name else None)
        job = self.jobs.wait(job["id"], on_update=on_update, timeout=timeout)
        files = Jobs.exported_files(job)
        saved = [self.files.download(f, out_dir) for f in files] if out_dir else []
        return ConvertResult(job=job, files=files, saved=saved)

    # ---- HTTP ----

    def request(self, method: str, path: str, body: Any = None, query: Optional[Dict[str, Any]] = None) -> Any:
        """Sends a request and returns ``data`` from ``{"success": true, "data": …}``."""
        return self.request_raw(method, path, body, query)[0]

    def request_raw(self, method: str, path: str, body: Any = None, query: Optional[Dict[str, Any]] = None):
        """Like :meth:`request`, returning ``(data, meta)``."""
        params = {k: v for k, v in (query or {}).items() if v is not None and v != ""}
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        # POST /jobs is only repeated when it never reached the server, so a job is never created twice.
        idempotent = method != "POST" or path.endswith("/cancel")
        attempt = 0
        while True:
            try:
                res = self._send(method, url, headers, data, self.timeout)
            except (urllib.error.URLError, OSError) as err:
                reached = isinstance(err, TimeoutError) or "timed out" in str(err)
                if attempt < self.max_retries and (idempotent or not reached):
                    attempt += 1
                    time.sleep(_backoff(attempt))
                    continue
                raise RunConvertError(f"Request to RunConvert failed: {err}") from err
            if res.status in _RETRY_STATUS and attempt < self.max_retries and (idempotent or res.status == 429):
                attempt += 1
                time.sleep(_retry_after(res) or _backoff(attempt))
                continue
            if res.status == 204:
                return None, {}
            try:
                payload = json.loads(res.body.decode("utf-8")) if res.body else None
            except ValueError:
                payload = None
            if res.status >= 400 or (isinstance(payload, dict) and payload.get("success") is False):
                err = (payload or {}).get("error") or {} if isinstance(payload, dict) else {}
                raise RunConvertError(err.get("message") or f"RunConvert API error (HTTP {res.status})", res.status, err.get("code", ""), payload if payload is not None else res.body)
            payload = payload or {}
            return payload.get("data"), payload.get("meta") or {}

    def _send(self, method: str, url: str, headers: Dict[str, str], data: Any, timeout: Optional[float]) -> HTTPResponse:
        """One HTTP exchange. Overridden in tests."""
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return HTTPResponse(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            return HTTPResponse(e.code, dict(e.headers or {}), e.read() or b"")

    def _stream_to(self, url: str, dest: str) -> int:
        """Downloads ``url`` to ``dest``; returns the status. Overridden in tests."""
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req) as r, open(dest, "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


def _backoff(attempt: int) -> float:
    return min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.75 + random.random() / 2)


def _retry_after(res: HTTPResponse) -> Optional[float]:
    value = {k.lower(): v for k, v in res.headers.items()}.get("retry-after")
    try:
        seconds = float(value) if value else 0
    except ValueError:
        return None
    return min(seconds, 30.0) if seconds > 0 else None


class Jobs:
    """Create, read, wait for and stop jobs."""

    def __init__(self, client: RunConvert) -> None:
        self._c = client

    def create(self, tasks: Dict[str, dict], *, tag: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
        """Creates a job from named tasks. Returned with its tasks (an upload task carries ``upload_url``)."""
        body: Dict[str, Any] = {"tasks": tasks}
        if tag:
            body["tag"] = tag
        if metadata:
            body["metadata"] = metadata
        return self._c.request("POST", "/jobs", body, {"include": "tasks"})

    def get(self, job_id: str) -> dict:
        """A job with its tasks."""
        return self._c.request("GET", f"/jobs/{urllib.parse.quote(job_id, safe='')}", query={"include": "tasks"})

    def list(self, *, status: Optional[str] = None, tag: Optional[str] = None, page: Optional[int] = None,
             per_page: Optional[int] = None, include_tasks: bool = False) -> Dict[str, Any]:
        """One page of jobs, newest first: ``{"data": [...], "meta": {...}}``."""
        data, meta = self._c.request_raw("GET", "/jobs", query={
            "filter[status]": status, "filter[tag]": tag, "page": page, "per_page": per_page,
            "include": "tasks" if include_tasks else None,
        })
        return {"data": data or [], "meta": meta}

    def all(self, **filters: Any) -> Iterator[dict]:
        """Every job, page by page: ``for job in rc.jobs.all(status="finished")``."""
        page = 1
        while True:
            result = self.list(page=page, **filters)
            yield from result["data"]
            total_pages = result["meta"].get("total_pages")
            if not result["data"] or (total_pages is not None and page >= total_pages):
                return
            page += 1

    def cancel(self, job_id: str) -> dict:
        """Cancels pending tasks; credits for unfinished tasks are refunded."""
        return self._c.request("POST", f"/jobs/{urllib.parse.quote(job_id, safe='')}/cancel")

    def delete(self, job_id: str) -> None:
        """Deletes a job."""
        self._c.request("DELETE", f"/jobs/{urllib.parse.quote(job_id, safe='')}")

    def wait(self, job_id: str, *, timeout: Optional[float] = None, on_update: Optional[Callable[[dict], None]] = None,
             raise_on_error: bool = True) -> dict:
        """Polls until ``finished`` or ``error`` (0.7 s, slowing to 3 s). Raises JobFailedError for a failed job."""
        started = time.monotonic()
        delay = 0.7
        while True:
            job = self.get(job_id)
            if on_update:
                on_update(job)
            if job.get("status") == "finished":
                return job
            if job.get("status") == "error":
                if not raise_on_error:
                    return job
                failed = next((t for t in job.get("tasks") or [] if t.get("status") == "error"), None)
                raise JobFailedError(job, failed)
            if timeout is not None and time.monotonic() - started + delay > timeout:
                raise WaitTimeoutError(job, timeout)
            time.sleep(delay)
            delay = min(delay + 0.3, 3.0)

    @staticmethod
    def exported_files(job: dict) -> List[dict]:
        """The files produced by the job's ``export/url`` tasks."""
        return [f for t in job.get("tasks") or [] if t.get("operation") == "export/url" for f in (t.get("result") or {}).get("files") or []]


class Operations:
    """Supported formats, targets, engines and options. Public: no key needed."""

    def __init__(self, client: RunConvert) -> None:
        self._c = client

    def list(self, *, category: Optional[str] = None) -> dict:
        """Every input format with its targets: ``{"categories": [...], "formats": [...]}``."""
        return self._c.request("GET", "/operations", query={"category": category})

    def for_input(self, input_format: str) -> dict:
        """What one format converts to, with credit costs."""
        return self._c.request("GET", "/operations", query={"input": input_format.lower()})

    def for_pair(self, input_format: str, output_format: str) -> dict:
        """Engines for one conversion and the options a convert task accepts."""
        return self._c.request("GET", "/operations", query={"input": input_format.lower(), "output": output_format.lower()})


class Files:
    """Uploads to ``import/direct/upload`` tasks and downloads of results."""

    def __init__(self, client: RunConvert) -> None:
        self._c = client

    def upload(self, task: Union[dict, str], file: FileInput, *, filename: Optional[str] = None) -> None:
        """PUTs a path, bytes or binary file object to the task's ``upload_url`` before its deadline."""
        upload_url = task if isinstance(task, str) else task.get("upload_url")
        if not upload_url:
            raise ValueError("This task has no upload_url (is it an import/direct/upload task?)")
        if filename is None and isinstance(file, (str, os.PathLike)):
            filename = os.path.basename(os.fspath(file))
        url = _with_query(upload_url, {"filename": filename} if filename else {})
        headers = {"Content-Type": "application/octet-stream", "User-Agent": self._c.user_agent}
        if isinstance(file, (str, os.PathLike)):
            headers["Content-Length"] = str(os.path.getsize(file))
            with open(file, "rb") as fh:
                res = self._c._send("PUT", url, headers, fh, None)
        else:
            res = self._c._send("PUT", url, headers, file, None)
        if res.status >= 300:
            raise RunConvertError(f"Upload failed (HTTP {res.status}): {res.body[:300].decode('utf-8', 'replace')}", res.status)

    def download(self, file: Union[dict, str], out_dir: str, filename: Optional[str] = None) -> str:
        """Saves a result file (or a URL) into ``out_dir`` keeping its name; returns the path."""
        url = file if isinstance(file, str) else file["url"]
        name = filename or (os.path.basename(urllib.parse.urlparse(url).path) if isinstance(file, str) else file["filename"])
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, os.path.basename(name))
        status = self._c._stream_to(url, dest)
        if status >= 300:
            raise RunConvertError(f"Download failed (HTTP {status})", status)
        return dest


class Webhooks:
    """``verify`` and ``construct_event`` for webhook deliveries."""

    verify = staticmethod(_webhooks.verify_signature)
    construct_event = staticmethod(_webhooks.construct_event)


def _with_query(url: str, extra: Dict[str, str]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update(extra)
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
