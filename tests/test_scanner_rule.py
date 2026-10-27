import io
import json
import os
import sys
import types
import unittest
from datetime import datetime as _real_dt, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: None)

import lambda_function as lf  # noqa: E402

B = lf.S3_BUCKET
SK = lf._SCANNER_SET_KEY
T0 = _real_dt(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)

HOME_A, HOME_B, DC = "93.1.1.1", "94.2.2.2", "3.3.3.3"
DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"
SCANNER_UA = "Mozilla/5.0 (compatible; Proofpoint URL scanner)"


class StubS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise Exception("An error occurred (NoSuchKey) when calling the GetObject operation")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, **kw):
        self.puts.append(kw)
        body = kw["Body"]
        self.objects[(kw["Bucket"], kw["Key"])] = body if isinstance(body, bytes) else body.encode()

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def ip_cache(ip, dc):
    return ((B, "ip-cache/" + ip + ".json"),
            json.dumps({"at": T0.timestamp() + 999999, "kw": dc, "host": dc,
                        "org": "Amazon AWS" if dc else "Home ISP"}).encode())


class Clock:
    def __init__(self):
        self.t = T0

    def patch(self):
        clock = self

        class FakeDT(_real_dt):
            @classmethod
            def now(cls, tz=None):
                return clock.t

        return mock.patch.object(lf, "datetime", FakeDT)


class ScannerRule(unittest.TestCase):
    def setUp(self):
        self.s3 = StubS3(dict(ip_cache(ip, ip == DC) for ip in (HOME_A, HOME_B, DC)))
        self.clock = Clock()
        self.p_dt = self.clock.patch()
        self.p_dt.start()
        self.p_net = mock.patch.object(lf.urllib.request, "urlopen", side_effect=AssertionError("no network"))
        self.p_net.start()
        self.p_ses = mock.patch.object(lf.boto3, "client", return_value=mock.Mock())
        self.ses = self.p_ses.start()

    def tearDown(self):
        mock.patch.stopall()

    def at(self, secs):
        self.clock.t = _real_dt.fromtimestamp(T0.timestamp() + secs, timezone.utc)

    def click(self, pid, ip, ua=DESKTOP):
        return lf._classify_click(self.s3, {"ip": ip, "ua": ua}, pid)

    def last_reason(self):
        logs = [json.loads(p["Body"]) for p in self.s3.puts if p["Key"].startswith(lf._CLICK_LOG_PREFIX)]
        return logs[-1]["reason"]

    def scanner_burst(self, pid, ip):
        self.at(0)
        self.assertEqual(self.click(pid, ip, SCANNER_UA), "scanner")

    def test_flag_records_ip_and_ts_and_keeps_day_list(self):
        self.scanner_burst(7, HOME_A)
        data = json.loads(self.s3.objects[(B, SK)])
        self.assertEqual(data[lf._scanner_day_key()], [7])
        ev = data["_events"]["7"][0]
        self.assertEqual((ev["ip"], ev["ts"]), (HOME_A, T0.timestamp()))
        self.assertEqual(lf._load_scanner_set(self.s3), {7})

    def test_different_residential_ip_3h_later_is_human(self):
        self.scanner_burst(7, HOME_A)
        self.at(3 * 3600)
        self.assertEqual(self.click(7, HOME_B), "human")

    def test_same_ip_3h_later_is_scanner(self):
        self.scanner_burst(7, HOME_A)
        self.at(3 * 3600)
        self.assertEqual(self.click(7, HOME_A), "scanner")
        self.assertEqual(self.last_reason(), "previously flagged (same IP)")

    def test_same_ip_after_24h_not_matched(self):
        self.scanner_burst(7, HOME_A)
        self.at(24 * 3600 + 60)
        self.assertEqual(self.click(7, HOME_A), "human")

    def test_different_ip_5_min_later_is_scanner(self):
        self.scanner_burst(7, HOME_A)
        self.at(5 * 60)
        self.assertEqual(self.click(7, HOME_B), "scanner")
        self.assertEqual(self.last_reason(), "previously flagged (<10 min)")

    def test_old_format_entry_still_scanner_same_ip(self):
        self.s3.objects[(B, SK)] = json.dumps({lf._scanner_day_key(): [7]}).encode()
        self.at(3 * 3600)
        self.assertEqual(self.click(7, HOME_A), "scanner")
        self.assertEqual(self.last_reason(), "previously flagged (same IP)")

    def test_old_format_entry_survives_conversion(self):
        self.s3.objects[(B, SK)] = json.dumps({lf._scanner_day_key(): [7]}).encode()
        self.scanner_burst(8, HOME_B)  # first new-format write
        data = json.loads(self.s3.objects[(B, SK)])
        self.assertEqual(data["_legacy"][lf._scanner_day_key()], [7])
        self.assertEqual(sorted(data[lf._scanner_day_key()]), [7, 8])
        self.at(3 * 3600)
        self.assertEqual(self.click(7, HOME_A), "scanner")

    def test_datacenter_ip_later_still_scanner(self):
        self.scanner_burst(7, HOME_A)
        self.at(3 * 3600)
        self.assertEqual(self.click(7, DC), "scanner")
        self.assertTrue(self.last_reason().startswith("data-center IP"))

    def test_scanner_ua_later_still_scanner(self):
        self.scanner_burst(7, HOME_A)
        self.at(3 * 3600)
        self.assertEqual(self.click(7, HOME_B, SCANNER_UA), "scanner")
        self.assertEqual(self.last_reason(), "scanner user-agent")

    def test_honeypot_flags_with_ip(self):
        params = {"pid": "9", "t": lf._mailer_token("9", "hp"), "hp": "1"}
        with mock.patch.object(lf.boto3, "client", return_value=self.s3):
            res = lf._handle_mailer_honeypot(params, {"ip": HOME_A, "ua": "x"})
        self.assertEqual(res["statusCode"], 204)
        data = json.loads(self.s3.objects[(B, SK)])
        self.assertIn(9, data[lf._scanner_day_key()])
        self.assertEqual(data["_events"]["9"][0]["ip"], HOME_A)
        self.at(3 * 3600)
        self.assertEqual(self.click(9, HOME_A), "scanner")
        self.assertEqual(self.click(9, HOME_B), "human")

    def test_honeypot_route_passes_meta(self):
        ev = {"queryStringParameters": {"view": "click", "hp": "1", "pid": "9",
                                        "t": lf._mailer_token("9", "hp")},
              "requestContext": {"http": {"method": "GET", "sourceIp": HOME_A, "userAgent": "x"}}}
        with mock.patch.object(lf.boto3, "client", return_value=self.s3):
            lf.handle_http_request(ev)
        self.assertEqual(json.loads(self.s3.objects[(B, SK)])["_events"]["9"][0]["ip"], HOME_A)

    def test_reflag_appends_event_but_alerts_once(self):
        with mock.patch.object(lf.boto3, "client") as cl:
            lf._flag_scanner(self.s3, 7, "r", {"ip": HOME_A}, alert=True)
            self.at(60)
            lf._flag_scanner(self.s3, 7, "r", {"ip": HOME_B}, alert=True)
        self.assertEqual(cl.return_value.send_email.call_count, 1)
        evs = json.loads(self.s3.objects[(B, SK)])["_events"]["7"]
        self.assertEqual([e["ip"] for e in evs], [HOME_A, HOME_B])

    def test_unflag_clears_events(self):
        self.scanner_burst(7, HOME_A)
        ev = {"queryStringParameters": {"view": "unflag", "pid": "7", "key": "k"},
              "requestContext": {"http": {"method": "GET"}}}
        with mock.patch.dict(os.environ, {"BRIEF_PAGE_KEY": "k"}), \
             mock.patch.object(lf.boto3, "client", return_value=self.s3):
            lf.handle_http_request(ev)
        data = json.loads(self.s3.objects[(B, SK)])
        self.assertNotIn("7", data["_events"])
        self.assertEqual(data[lf._scanner_day_key()], [])
        self.at(60)
        self.assertEqual(self.click(7, HOME_A), "human")


if __name__ == "__main__":
    unittest.main()
