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

LABEL = "custom_label_4011458"


def _companies_doc(n, flagged=()):
    comps = []
    for i in range(n):
        val = [7217010] if i in flagged else None
        comps.append({"id": 1000 + i, "name": f"Co {i}", "email": "x@y.z",
                      "custom_fields": {"custom_label_1": [1, 2], LABEL: val}})
    return {"last_updated": "2026-10-22T08:00:00Z", "total": n, "companies": comps}


class _Body(io.BytesIO):
    pass


class FakeS3:
    def __init__(self, doc=None, raw=None, missing=False):
        self.raw = raw if raw is not None else (json.dumps(doc).encode() if doc is not None else b"")
        self.missing = missing
        self.puts = []

    def get_object(self, Bucket, Key):
        if self.missing:
            raise Exception("NoSuchKey")
        return {"Body": _Body(self.raw)}

    def put_object(self, **kw):
        self.puts.append(kw)


class RestrictedDetection(unittest.TestCase):
    def test_int(self):
        self.assertTrue(lf._company_is_restricted({"custom_fields": {LABEL: [7217010]}}))

    def test_string(self):
        self.assertTrue(lf._company_is_restricted({"custom_fields": {LABEL: ["7217010"]}}))
        self.assertTrue(lf._company_is_restricted({"custom_fields": {LABEL: "7217010"}}))

    def test_other_option(self):
        self.assertFalse(lf._company_is_restricted({"custom_fields": {LABEL: [7217011]}}))

    def test_null(self):
        self.assertFalse(lf._company_is_restricted({"custom_fields": {LABEL: None}}))

    def test_missing_key(self):
        self.assertFalse(lf._company_is_restricted({"custom_fields": {}}))
        self.assertFalse(lf._company_is_restricted({}))
        self.assertFalse(lf._company_is_restricted({"custom_fields": None}))

    def test_stream_matches_dict_check(self):
        doc = {"last_updated": "T", "companies": [
            {"id": 1, "name": "A", "custom_fields": {LABEL: [7217010]}},
            {"id": 2, "name": "B", "custom_fields": {LABEL: ["7217010", 5]}},
            {"id": 3, "name": "C", "custom_fields": {LABEL: None}},
            {"id": 4, "name": "D", "custom_fields": {}},
            {"id": 5, "name": "E"},
            {"id": 6, "name": "F", "custom_fields": {LABEL: 7217010}},
        ]}
        lu, n, rs = lf._scan_companies_stream(io.BytesIO(json.dumps(doc).encode()))
        self.assertEqual((lu, n), ("T", 6))
        self.assertEqual(rs, [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}, {"id": 6, "name": "F"}])
        self.assertEqual([c["id"] for c in doc["companies"] if lf._company_is_restricted(c)], [1, 2, 6])


class BuildRestricted(unittest.TestCase):
    def _run(self, s3):
        with mock.patch.object(lf.boto3, "client", return_value=s3, create=True):
            return lf.build_restricted()

    def test_aborts_below_5000(self):
        s3 = FakeS3(_companies_doc(4999, flagged={3}))
        res = self._run(s3)
        self.assertFalse(res["ok"])
        self.assertIn("4999", res["reason"])
        self.assertEqual(s3.puts, [])

    def test_aborts_missing_file(self):
        s3 = FakeS3(missing=True)
        res = self._run(s3)
        self.assertFalse(res["ok"])
        self.assertEqual(s3.puts, [])

    def test_aborts_unparseable(self):
        s3 = FakeS3(raw=b'{"companies": [{"id": 1,')
        res = self._run(s3)
        self.assertFalse(res["ok"])
        self.assertEqual(s3.puts, [])

    def test_writes_list(self):
        s3 = FakeS3(_companies_doc(5000, flagged={7, 4242}))
        res = self._run(s3)
        self.assertTrue(res["ok"])
        self.assertEqual(len(s3.puts), 1)
        put = s3.puts[0]
        self.assertEqual((put["Bucket"], put["Key"]), ("full-pipeline-cache", "restricted-issuers.json"))
        doc = json.loads(put["Body"])
        self.assertEqual(doc["company_count_scanned"], 5000)
        self.assertEqual(doc["source_last_updated"], "2026-10-22T08:00:00Z")
        self.assertEqual((doc["field_id"], doc["option_id"]), (4011458, 7217010))
        self.assertEqual(doc["companies"], [{"id": 1007, "name": "Co 7"}, {"id": 5242, "name": "Co 4242"}])
        self.assertIn("built_at", doc)

    def test_empty_list_is_valid(self):
        s3 = FakeS3(_companies_doc(5000))
        res = self._run(s3)
        self.assertTrue(res["ok"])
        self.assertEqual(json.loads(s3.puts[0]["Body"])["companies"], [])


class ScheduledEvent(unittest.TestCase):
    def test_early_return_before_any_routing(self):
        sentinel = {"ok": True, "sentinel": 1}
        with mock.patch.object(lf, "build_restricted", return_value=sentinel) as br, \
                mock.patch.object(lf, "_build_brief_html") as brief, \
                mock.patch.object(lf, "handle_http_request") as http, \
                mock.patch.object(lf.boto3, "client", create=True) as client:
            res = lf.lambda_handler({"action": "build_restricted"}, None)
            # Even with an http context attached, the action wins.
            res2 = lf.lambda_handler({"action": "build_restricted",
                                      "requestContext": {"http": {"method": "GET"}}}, None)
        self.assertIs(res, sentinel)
        self.assertIs(res2, sentinel)
        self.assertEqual(br.call_count, 2)
        brief.assert_not_called()
        http.assert_not_called()
        client.assert_not_called()


class AdminView(unittest.TestCase):
    def _get(self, params, s3):
        os.environ["BRIEF_PAGE_KEY"] = "k*1"
        ev = {"requestContext": {"http": {"method": "GET"}}, "queryStringParameters": params}
        with mock.patch.object(lf.boto3, "client", return_value=s3, create=True):
            return lf.lambda_handler(ev, None)

    def test_requires_key(self):
        self.assertEqual(self._get({"view": "restricted"}, FakeS3())["statusCode"], 403)
        self.assertEqual(self._get({"view": "restricted", "key": "bad"}, FakeS3())["statusCode"], 403)

    def test_renders_list(self):
        doc = {"built_at": "B", "source_last_updated": "S", "company_count_scanned": 9810,
               "companies": [{"id": 138856152, "name": "Advanced Manufacturing Company of America"}]}
        res = self._get({"view": "restricted", "key": "k*1"}, FakeS3(doc))
        self.assertEqual(res["statusCode"], 200)
        self.assertIn("https://app.pipelinecrm.com/companies/138856152", res["body"])
        self.assertIn("Advanced Manufacturing Company of America", res["body"])
        self.assertIn("9810", res["body"])
        self.assertIn("rebuild=1", res["body"])
        self.assertIn("key=k%2A1", res["body"])

    def test_rebuild_runs_builder(self):
        with mock.patch.object(lf, "build_restricted", return_value={"ok": True, "restricted_count": 1}) as br:
            res = self._get({"view": "restricted", "key": "k*1", "rebuild": "1"}, FakeS3(missing=True))
        br.assert_called_once()
        self.assertIn("Rebuild succeeded", res["body"])


if __name__ == "__main__":
    unittest.main()
