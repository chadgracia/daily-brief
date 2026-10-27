import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import AGENT_DATA, FakeS3, FakeSES, lf  # noqa: E402

TT, PD = "custom_label_3759163", "custom_label_4011837"
BUY = lf.CF_PERSON_BUY_INTERESTS
DEALS = {"deals": [{"id": 7, "name": "SpaceX sell", "company": {"id": 9, "name": "SpaceX"},
                    "custom_fields": {"custom_label_1958": 5011675}}]}



def _person(cf):
    return {"id": 42, "first_name": "A", "last_name": "B", "email": "a@b.com", "custom_fields": cf}


class _Base:
    """Runs a human POST click through one handler with stub S3/SES/Pipeline."""

    def _click(self, get_result):
        s3 = FakeS3({("full-pipeline-cache", "deals.json"): json.dumps(DEALS).encode(),
                     ("pipeline-token", "agent-data.json"): json.dumps(AGENT_DATA).encode()})
        ses = FakeSES()
        calls = []

        def api(method, endpoint, payload=None, jwt=None, timeout=15):
            calls.append((method, endpoint, payload))
            if method == "GET" and endpoint == "/people/42.json":
                if isinstance(get_result, Exception):
                    raise get_result
                return get_result
            return {"status": 200, "data": {}}

        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "call_pipeline_api", side_effect=api), \
                mock.patch.object(lf, "get_jwt", return_value="j"), \
                mock.patch.object(lf, "_classify_click", return_value="human"):
            res = self.handler()
        puts = [c for c in calls if c[0] == "PUT"]
        return res, puts, ses

    def test_intermediary_each_tt(self):
        for tt in (6577160, 6888332, 6888333):
            res, puts, ses = self._click({"status": 200, "data": _person({TT: [tt], BUY: [101]})})
            self.assertEqual(puts, [], tt)
            self.assertEqual(len(ses.sent), 1, tt)
            msg = ses.sent[0]["Message"]
            self.assertTrue(msg["Subject"]["Data"].startswith("[Intermediary] "), tt)
            self.assertIn("Buy Interest NOT added — intermediary", msg["Body"]["Text"]["Data"])

    def test_investor_preserves_existing(self):
        res, puts, ses = self._click({"status": 200, "data": _person({TT: [6484811], BUY: [102, 555]})})
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][2], {"person": {"custom_fields": {BUY: [101, 102, 555]}}})
        msg = ses.sent[0]["Message"]
        self.assertFalse(msg["Subject"]["Data"].startswith("[Intermediary]"))
        self.assertIn("Buy Interest added", msg["Body"]["Text"]["Data"])

    def test_get_500_no_put(self):
        for bad in ({"status": 500, "data": "boom"}, {"status": 200, "data": "not a dict"}):
            res, puts, ses = self._click(bad)
            self.assertEqual(puts, [])
            self.assertIn("Buy Interest NOT added — person record could not be read",
                          ses.sent[0]["Message"]["Body"]["Text"]["Data"])

    def test_get_raises_no_put(self):
        res, puts, ses = self._click(RuntimeError("down"))
        self.assertEqual(puts, [])
        self.assertEqual(res["statusCode"], self.ok_status)

    def test_referral_locked(self):
        for pd in (7218526, 7218570):
            res, puts, ses = self._click({"status": 200, "data": _person({PD: [pd], BUY: [101]})})
            self.assertEqual(puts, [])
            self.assertIn("Buy Interest NOT added — referral-locked account",
                          ses.sent[0]["Message"]["Body"]["Text"]["Data"])


class DealClick(_Base, unittest.TestCase):
    ok_status = 200

    def handler(self):
        return lf._handle_mailer_click({"pid": "42", "did": "7", "t": lf._mailer_token("42", "7")}, "POST", {})


class NewsDealClick(_Base, unittest.TestCase):
    ok_status = 200

    def handler(self):
        return lf._handle_mailer_click({"pid": "42", "did": "7", "src": "news",
                                        "t": lf._mailer_token("42", "7")}, "POST", {})


class CompanyClick(_Base, unittest.TestCase):
    ok_status = 200

    def handler(self):
        return lf._handle_mailer_click_co({"pid": "42", "co": "SpaceX",
                                           "t": lf._mailer_token("42", "c:SpaceX")}, "POST", {})


class CompanyInfoRequest(_Base, unittest.TestCase):
    ok_status = 200

    def handler(self):
        return lf._handle_mailer_click_co({"pid": "42", "co": "SpaceX", "m": "1",
                                           "t": lf._mailer_token("42", "c:SpaceX")}, "POST", {})


if __name__ == "__main__":
    unittest.main()
