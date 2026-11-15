import hashlib
import hmac
import json
import os
import tempfile
import unittest
import urllib.parse

from runconvert import JobFailedError, RunConvert, RunConvertError, WaitTimeoutError, construct_event, verify_signature
from runconvert.client import HTTPResponse


def ok(data, meta=None):
    body = {"success": True, "data": data}
    if meta:
        body["meta"] = meta
    return HTTPResponse(200, {}, json.dumps(body).encode())


def fail(status, code, message, headers=None):
    return HTTPResponse(status, headers or {}, json.dumps({"success": False, "error": {"code": code, "message": message}}).encode())


class Fake(RunConvert):
    """A client whose HTTP goes to ``handler(call, n)`` instead of the network."""

    def __init__(self, handler, **kw):
        super().__init__("rc_live_test", base_url="https://api.example/v3", max_retries=2, **kw)
        self.handler = handler
        self.calls = []
        self.downloads = {}

    def _send(self, method, url, headers, data, timeout):
        u = urllib.parse.urlsplit(url)
        body = data.read() if hasattr(data, "read") else data
        call = {"method": method, "path": u.path.replace("/v3", "", 1), "query": dict(urllib.parse.parse_qsl(u.query)), "headers": headers, "body": body}
        self.calls.append(call)
        return self.handler(call, len(self.calls))

    def _stream_to(self, url, dest):
        with open(dest, "wb") as f:
            f.write(self.downloads.get(url, b""))
        return 200 if url in self.downloads else 404


class ClientTests(unittest.TestCase):
    def setUp(self):
        import runconvert.client as c
        self._sleep = c.time.sleep
        c.time.sleep = lambda s: None  # no real waiting in tests

    def tearDown(self):
        import runconvert.client as c
        c.time.sleep = self._sleep

    def test_key_and_envelope(self):
        rc = Fake(lambda call, n: ok({"credits": {"total_remaining": 42}}))
        self.assertEqual(rc.me()["credits"]["total_remaining"], 42)
        self.assertEqual(rc.calls[0]["headers"]["Authorization"], "Bearer rc_live_test")
        self.assertTrue(rc.calls[0]["headers"]["User-Agent"].startswith("runconvert-python/"))

    def test_api_error(self):
        rc = Fake(lambda call, n: fail(422, "BILLING_INSUFFICIENT_CREDITS", "Insufficient credits."))
        with self.assertRaises(RunConvertError) as cm:
            rc.jobs.get("job_1")
        self.assertEqual((cm.exception.status, cm.exception.code, str(cm.exception)), (422, "BILLING_INSUFFICIENT_CREDITS", "Insufficient credits."))

    def test_get_retries_503(self):
        rc = Fake(lambda call, n: HTTPResponse(503, {}, b"busy") if n < 3 else ok({"id": "job_1", "status": "finished"}))
        self.assertEqual(rc.jobs.get("job_1")["status"], "finished")
        self.assertEqual(len(rc.calls), 3)

    def test_create_not_retried_after_500(self):
        rc = Fake(lambda call, n: fail(500, "INTERNAL", "boom"))
        with self.assertRaises(RunConvertError):
            rc.jobs.create({})
        self.assertEqual(len(rc.calls), 1)

    def test_create_retried_on_429(self):
        rc = Fake(lambda call, n: fail(429, "ACTIVE_JOB_LIMIT_REACHED", "x", {"Retry-After": "0"}) if n == 1 else ok({"id": "job_1", "status": "waiting", "tasks": []}))
        self.assertEqual(rc.jobs.create({})["id"], "job_1")
        self.assertEqual(len(rc.calls), 2)
        self.assertEqual(rc.calls[1]["query"]["include"], "tasks")

    def test_list_and_all(self):
        rc = Fake(lambda call, n: ok([{"id": "job_p" + call["query"]["page"], "status": "finished"}], {"page": int(call["query"]["page"]), "total_pages": 2}))
        page = rc.jobs.list(status="finished", tag="x", page=1, per_page=1)
        self.assertEqual(page["meta"]["total_pages"], 2)
        self.assertEqual(rc.calls[0]["query"]["filter[status]"], "finished")
        self.assertEqual([j["id"] for j in rc.jobs.all(per_page=1)], ["job_p1", "job_p2"])

    def test_wait(self):
        states = ["waiting", "processing", "finished"]
        rc = Fake(lambda call, n: ok({"id": "job_1", "status": states[min(n - 1, 2)], "tasks": []}))
        seen = []
        self.assertEqual(rc.jobs.wait("job_1", on_update=lambda j: seen.append(j["status"]))["status"], "finished")
        self.assertEqual(seen, states)

    def test_wait_failed_and_timeout(self):
        failed = {"id": "job_1", "status": "error", "tasks": [{"name": "c", "operation": "convert", "status": "error", "code": "INVALID_DATA", "reason": "INPUT_EMPTY", "message": "The file is empty."}]}
        rc = Fake(lambda call, n: ok(failed))
        with self.assertRaises(JobFailedError) as cm:
            rc.jobs.wait("job_1")
        self.assertEqual((cm.exception.task["reason"], str(cm.exception)), ("INPUT_EMPTY", "The file is empty."))
        self.assertEqual(rc.jobs.wait("job_1", raise_on_error=False)["status"], "error")
        rc2 = Fake(lambda call, n: ok({"id": "job_1", "status": "processing", "tasks": []}))
        with self.assertRaises(WaitTimeoutError):
            rc2.jobs.wait("job_1", timeout=0.001)

    def test_delete_204_and_operations(self):
        rc = Fake(lambda call, n: HTTPResponse(204, {}) if call["method"] == "DELETE" else ok({"engines": []}))
        self.assertIsNone(rc.jobs.delete("job_1"))
        rc.operations.for_pair("MP4", "MP3")
        self.assertEqual(rc.calls[1]["query"], {"input": "mp4", "output": "mp3"})

    def test_webhooks(self):
        body = json.dumps({"event": "job.finished", "job": {"id": "job_1"}})
        sig = "sha256=" + hmac.new(b"whsec_x", body.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature(body, sig, "whsec_x"))
        self.assertTrue(verify_signature(body.encode(), sig, "whsec_x"))
        self.assertFalse(verify_signature(body + " ", sig, "whsec_x"))
        self.assertFalse(verify_signature(body, sig, "other"))
        self.assertFalse(verify_signature(body, "sha256=nothex", "whsec_x"))
        self.assertFalse(verify_signature(body, None, "whsec_x"))
        self.assertEqual(construct_event(body, sig, "whsec_x")["job"]["id"], "job_1")
        with self.assertRaises(ValueError):
            construct_event(body, "sha256=" + "0" * 64, "whsec_x")

    def test_convert_upload_wait_download(self):
        polls = {"n": 0}

        def handler(call, n):
            if call["method"] == "POST" and call["path"] == "/jobs":
                return ok({"id": "job_1", "status": "waiting", "tasks": [{"name": "import-1", "operation": "import/direct/upload", "status": "waiting", "upload_url": "https://up.example/u/1?token=t"}]})
            if call["method"] == "PUT":
                return HTTPResponse(200, {}, b"")
            polls["n"] += 1
            return ok({"id": "job_1", "status": "finished" if polls["n"] > 1 else "processing", "tasks": [{"name": "export-1", "operation": "export/url", "status": "finished", "result": {"files": [{"filename": "notes.pdf", "url": "https://dl.example/notes.pdf"}]}}]})

        rc = Fake(handler)
        rc.downloads["https://dl.example/notes.pdf"] = b"PDF"
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "notes.txt")
            with open(src, "w") as f:
                f.write("hello")
            res = rc.convert(src, "pdf", out_dir=os.path.join(d, "out"), options={"page_size": "a4"})
            created = json.loads(rc.calls[0]["body"])
            self.assertEqual(created["tasks"]["convert-1"], {"page_size": "a4", "operation": "convert", "input": "import-1", "input_format": "txt", "output_format": "pdf"})
            put = next(c for c in rc.calls if c["method"] == "PUT")
            self.assertEqual((put["query"]["filename"], put["query"]["token"], put["body"]), ("notes.txt", "t", b"hello"))
            with open(res.saved[0], "rb") as f:
                self.assertEqual(f.read(), b"PDF")

    def test_convert_url_and_bytes(self):
        rc = Fake(lambda call, n: ok({"id": "job_2", "status": "finished" if call["method"] == "GET" else "waiting", "tasks": []}))
        rc.convert("https://example.com/a/talk.MP4?x=1", "mp3")
        tasks = json.loads(rc.calls[0]["body"])["tasks"]
        self.assertEqual(tasks["import-1"], {"operation": "import/url", "source_url": "https://example.com/a/talk.MP4?x=1"})
        self.assertEqual(tasks["convert-1"]["input_format"], "mp4")
        with self.assertRaises(ValueError):
            rc.convert(b"x", "pdf")


if __name__ == "__main__":
    unittest.main()
