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

B = lf.S3_BUCKET
DAY = "2026-10-27"


class FakeS3:
    def __init__(self, objects=None, page=1000):
        self.objects = dict(objects or {})
        self.gets, self.puts, self.page = [], [], page

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        chunk = keys[start:start + self.page]
        resp = {"Contents": [{"Key": k} for k in chunk], "IsTruncated": start + self.page < len(keys)}
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = str(start + self.page)
        return resp

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if (Bucket, Key) not in self.objects:
            raise Exception("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, **kw):
        self.puts.append(kw)


def click_key(hms_us, pid, day=DAY):
    return lf._CLICK_LOG_PREFIX + day + "/" + hms_us + "-" + str(pid) + ".json"


def rec(ts, pid, did=None, verdict="human", ua="Mozilla/5.0", reason="", org="Comcast"):
    return json.dumps({"ts": ts, "pid": pid, "did": did, "ip": "1.2.3.4", "ua": ua,
                       "secs_since_send": 120, "verdict": verdict, "reason": reason,
                       "org": org}).encode()


def base_objects(access=True):
    o = {
        (B, click_key("090000000001", 11)): rec(DAY + "T09:00:00+00:00", 11, did=501),
        (B, click_key("100000000002", 22)): rec(DAY + "T10:00:00+00:00", 22, verdict="scanner",
                                                 reason="ua", ua="X" * 80 + "<script>"),
        (B, click_key("110000000003", 33)): rec(DAY + "T11:00:00+00:00", 33),
        (B, click_key("120000000004", 11)): b"{not json",
    }
    if access:
        o[(B, lf.PD_ACCESS_KEY)] = json.dumps(
            {"people": {"33": {"intermediary": True}, "11": {"intermediary": False}}}).encode()
    return o


class ClicksPage(unittest.TestCase):
    def setUp(self):
        os.environ["BRIEF_PAGE_KEY"] = "k*1"
        self.services = []

    def _get(self, params, s3):
        def client(name, *a, **k):
            self.services.append(name)
            return s3
        event = {"requestContext": {"http": {"method": "GET"}}, "queryStringParameters": params}
        with mock.patch.object(lf.boto3, "client", side_effect=client), \
                mock.patch.object(lf.urllib.request, "urlopen", side_effect=AssertionError("network")):
            return lf.lambda_handler(event, None)

    def _assert_read_only(self, s3):
        self.assertEqual(s3.puts, [])
        self.assertTrue(set(self.services) <= {"s3"}, self.services)

    def test_forbidden_without_key(self):
        s3 = FakeS3(base_objects())
        self.assertEqual(self._get({"view": "clicks"}, s3)["statusCode"], 403)
        self.assertEqual(self._get({"view": "clicks", "key": "bad"}, s3)["statusCode"], 403)
        self.assertEqual(s3.gets, [])

    def test_bad_date_400(self):
        for d in ("2026-13-01", "yesterday", "2026-10-27/../x", "20261027"):
            s3 = FakeS3(base_objects())
            res = self._get({"view": "clicks", "key": "k*1", "date": d}, s3)
            self.assertEqual(res["statusCode"], 400, d)
            self.assertEqual(s3.gets, [])

    def test_rows_summary_tags(self):
        s3 = FakeS3(base_objects())
        res = self._get({"view": "clicks", "key": "k*1", "date": DAY}, s3)
        self.assertEqual(res["statusCode"], 200)
        body = res["body"]
        # newest first
        i33, i22, i11 = (body.index("/people/" + p + '"') for p in ("33", "22", "11"))
        self.assertLess(i33, i22)
        self.assertLess(i22, i11)
        self.assertIn("3 clicks &middot; 2 human &middot; 1 scanner &middot; 1 intermediary human"
                      " &middot; 1 skipped (unreadable)", body)
        self.assertEqual(body.count(">Intermediary</span>"), 1)
        self.assertIn("https://app.pipelinecrm.com/deals/501", body)
        self.assertEqual(body.count("company link"), 2)
        self.assertIn(">09:00:00</td>", body)
        self.assertIn(">11:00:00</td>", body)  # Kyiv for 09:00 UTC (EET, UTC+2 after DST ends Oct 25)
        self.assertNotIn("<script>", body)
        self.assertIn('title="' + "X" * 80 + '&lt;script&gt;"', body)
        self.assertIn('&lt;script&gt;">' + "X" * 60 + "</td>", body)  # cell cut to 60 chars
        self.assertNotIn("Showing the newest", body)
        self.assertNotIn("could not be read", body)
        self._assert_read_only(s3)

    def test_access_failure_still_renders(self):
        s3 = FakeS3(base_objects(access=False))
        res = self._get({"view": "clicks", "key": "k*1", "date": DAY}, s3)
        self.assertEqual(res["statusCode"], 200)
        self.assertNotIn(">Intermediary</span>", res["body"])
        self.assertIn("access.json could not be read", res["body"])
        self.assertIn("0 intermediary human", res["body"])
        self._assert_read_only(s3)

    def test_pid_filter(self):
        s3 = FakeS3(base_objects())
        res = self._get({"view": "clicks", "key": "k*1", "date": DAY, "pid": "11"}, s3)
        body = res["body"]
        self.assertIn("/people/11", body)
        self.assertNotIn("/people/22", body)
        self.assertNotIn("/people/33", body)
        self.assertEqual(sorted(k for k in s3.gets if "click-log" in k),
                         sorted([click_key("090000000001", 11), click_key("120000000004", 11)]))
        self.assertIn('name="key" value="k*1"', body)
        self.assertEqual(self._get({"view": "clicks", "key": "k*1", "pid": "1x"}, FakeS3())["statusCode"], 400)

    def test_cap_before_reads(self):
        objs = {(B, click_key("%06d%06d" % (i // 1000000, i % 1000000), 7)): rec(DAY + "T00:00:00+00:00", 7)
                for i in range(2105)}
        s3 = FakeS3(objs, page=1000)
        res = self._get({"view": "clicks", "key": "k*1", "date": DAY}, s3)
        click_gets = [k for k in s3.gets if "click-log" in k]
        self.assertEqual(len(click_gets), 2000)
        newest = sorted((k for (_, k) in objs), reverse=True)[:2000]
        self.assertEqual(sorted(click_gets), sorted(newest))
        self.assertIn("Showing the newest 2,000 of 2,105 clicks.", res["body"])
        self.assertIn("2000 clicks", res["body"])
        self._assert_read_only(s3)

    def test_default_date_is_today_utc(self):
        s3 = FakeS3()
        res = self._get({"view": "clicks", "key": "k*1"}, s3)
        today = lf.datetime.now(lf.timezone.utc).strftime("%Y-%m-%d")
        self.assertIn("Clicks &mdash; " + today, res["body"])
        self.assertIn("No clicks.", res["body"])


if __name__ == "__main__":
    unittest.main()
