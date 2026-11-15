"""Live tests against the real API. Needs RUNCONVERT_API_KEY; converts one small text file (about 2 credits)."""
import os
import tempfile
import unittest

from runconvert import RunConvert, RunConvertError

KEY = os.environ.get("RUNCONVERT_API_KEY")


@unittest.skipUnless(KEY, "set RUNCONVERT_API_KEY to run live tests")
class LiveTests(unittest.TestCase):
    def test_me(self):
        me = RunConvert().me()
        self.assertTrue(me.get("credits"))

    def test_operations(self):
        rc = RunConvert(api_key="")
        self.assertTrue(any(f["format"] == "txt" for f in rc.operations.list(category="document")["formats"]))
        self.assertTrue(any(o["format"] == "pdf" for o in rc.operations.for_input("TXT")["outputs"]))
        self.assertTrue(rc.operations.for_pair("mp4", "mp3")["engines"])

    def test_convert_text_to_pdf(self):
        rc = RunConvert()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "sdk-live-test.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write("RunConvert Python SDK live test\nLine two\n\nCafé – 你好 – Привет\n")
            seen = []
            res = rc.convert(src, "pdf", out_dir=os.path.join(d, "out"), tag="sdk-python-live-test", on_update=lambda j: seen.append(j["status"]), timeout=150)
            self.assertEqual(res.job["status"], "finished")
            with open(res.saved[0], "rb") as f:
                pdf = f.read()
            self.assertEqual(pdf[:5], b"%PDF-")
            print(f"\n  job {res.job['id']}: {' → '.join(dict.fromkeys(seen))}, {res.job.get('credits_used')} credit(s), {len(pdf)} bytes")

    def test_bad_key(self):
        with self.assertRaises(RunConvertError) as cm:
            RunConvert(api_key="rc_live_invalid", max_retries=0).me()
        self.assertEqual(cm.exception.status, 401)


if __name__ == "__main__":
    unittest.main()
