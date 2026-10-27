import io
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: None)

import lambda_function as lf  # noqa: E402

B, K = "pipeline-token", "partner-desk-secret.json"


class S3Err(Exception):
    """Mimics botocore ClientError's .response shape."""
    def __init__(self, code, status, op):
        super().__init__(f"An error occurred ({code}) when calling the {op} operation")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class StubS3:
    def __init__(self, stored=None, get_error=None, put_race_value=None, put_error=None):
        self.stored = stored              # bytes or None
        self.get_error = get_error
        self.put_race_value = put_race_value  # another writer wins between our read and our put
        self.put_error = put_error
        self.gets = 0
        self.puts = []

    def get_object(self, Bucket, Key):
        assert (Bucket, Key) == (B, K)
        self.gets += 1
        if self.get_error:
            raise self.get_error
        if self.stored is None:
            raise S3Err("NoSuchKey", 404, "GetObject")
        return {"Body": io.BytesIO(self.stored)}

    def put_object(self, **kw):
        self.puts.append(kw)
        if self.put_error:
            raise self.put_error
        if kw.get("IfNoneMatch") != "*":
            raise AssertionError("unconditional write attempted")
        if self.put_race_value is not None:
            self.stored = self.put_race_value
            raise S3Err("PreconditionFailed", 412, "PutObject")
        if self.stored is not None:
            raise S3Err("PreconditionFailed", 412, "PutObject")
        self.stored = kw["Body"]


class PdSecret(unittest.TestCase):
    def setUp(self):
        lf._PD_SECRET = None

    def tearDown(self):
        lf._PD_SECRET = None

    def test_missing_creates_with_if_none_match(self):
        s3 = StubS3()
        secret, status = lf._pd_secret_ensure(s3)
        self.assertEqual(status, "created")
        self.assertEqual(len(secret), 64)
        self.assertEqual(len(s3.puts), 1)
        put = s3.puts[0]
        self.assertEqual((put["Bucket"], put["Key"], put["IfNoneMatch"]), (B, K, "*"))
        self.assertEqual(json.loads(put["Body"])["secret"], secret)

    def test_412_rereads_stored_value(self):
        s3 = StubS3(put_race_value=b'{"secret": "winner"}')
        secret, status = lf._pd_secret_ensure(s3)
        self.assertEqual((secret, status), ("winner", "existed"))
        self.assertEqual(len(s3.puts), 1)
        self.assertEqual(s3.gets, 2)
        self.assertEqual(lf._pd_secret(StubS3()), "winner")  # cached, no new read/write

    def test_existing_read_never_written(self):
        s3 = StubS3(stored=b'{"secret": "abc"}')
        self.assertEqual(lf._pd_secret_ensure(s3), ("abc", "existed"))
        self.assertEqual(s3.puts, [])

    def test_existing_empty_secret_never_overwritten(self):
        s3 = StubS3(stored=b'{"secret": ""}')
        with self.assertRaises(RuntimeError):
            lf._pd_secret(s3)
        self.assertEqual(s3.puts, [])

    def test_access_denied_no_write(self):
        s3 = StubS3(get_error=S3Err("AccessDenied", 403, "GetObject"))
        with self.assertRaises(Exception):
            lf._pd_secret(s3)
        self.assertEqual(s3.puts, [])
        self.assertIsNone(lf._PD_SECRET)

    def test_put_access_denied_raises(self):
        s3 = StubS3(put_error=S3Err("AccessDenied", 403, "PutObject"))
        with self.assertRaises(Exception):
            lf._pd_secret(s3)
        self.assertIsNone(lf._PD_SECRET)


class RebuildPage(unittest.TestCase):
    def setUp(self):
        lf._PD_SECRET = None

    def tearDown(self):
        lf._PD_SECRET = None

    def _render(self, s3):
        ok = {"ok": True, "built_at": "now", "total": 1, "intermediaries": 0, "pd_counts": {}}
        with mock.patch.object(lf.boto3, "client", return_value=s3), \
             mock.patch.object(lf, "build_partner_desk_access", return_value=ok):
            return lf._render_pd_access_rebuild()["body"]

    def test_created_line_without_secret(self):
        s3 = StubS3()
        body = self._render(s3)
        self.assertIn("Partner Desk secret: created", body)
        self.assertNotIn(json.loads(s3.stored)["secret"], body)

    def test_existed_line_without_secret(self):
        body = self._render(StubS3(stored=b'{"secret": "topsecretvalue"}'))
        self.assertIn("Partner Desk secret: existed", body)
        self.assertNotIn("topsecretvalue", body)

    def test_fail_line(self):
        s3 = StubS3(get_error=S3Err("AccessDenied", 403, "GetObject"))
        body = self._render(s3)
        self.assertIn("Partner Desk secret: FAIL (", body)
        self.assertIn("AccessDenied", body)
        self.assertEqual(s3.puts, [])

    def test_route_shows_status_line(self):
        ev = {"queryStringParameters": {"view": "pd-access-rebuild", "key": "k"},
              "requestContext": {"http": {"method": "GET"}}}
        s3 = StubS3(stored=b'{"secret": "hiddenvalue"}')
        ok = {"ok": True, "built_at": "now", "total": 1, "intermediaries": 0, "pd_counts": {}}
        with mock.patch.dict(os.environ, {"BRIEF_PAGE_KEY": "k"}), \
             mock.patch.object(lf.boto3, "client", return_value=s3), \
             mock.patch.object(lf, "build_partner_desk_access", return_value=ok):
            body = lf.handle_http_request(ev)["body"]
        self.assertIn("Partner Desk secret: existed", body)
        self.assertNotIn("hiddenvalue", body)


if __name__ == "__main__":
    unittest.main()
