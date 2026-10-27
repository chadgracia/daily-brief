import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import AGENT_DATA, FakeS3, FakeSES, lf  # noqa: E402

OFFER = "https://7u6sphgup5gjuywcvpuwzhruiq0asgdz.lambda-url.us-east-1.on.aws/"
DESK = "https://trades.graciagroup.com/?view=partner-desk"
TT = "custom_label_3759163"
DEALS = {"deals": [
    {"id": 7, "name": "SpaceX buy", "company": {"id": 9, "name": "SpaceX"},
     "custom_fields": {lf.CF_DEAL_SIDE: lf.OPT_BUY}},
    {"id": 8, "name": "SpaceX sell", "company": {"id": 9, "name": "SpaceX"},
     "custom_fields": {lf.CF_DEAL_SIDE: lf.OPT_SELL}},
]}


def _access(intermediary=True, pd=None):
    return {"people": {"42": {"intermediary": intermediary, "pd": pd, "emails": ["a@b.com"]}}}


class Gate(unittest.TestCase):
    def setUp(self):
        lf._PD_SECRET = "abc"
        lf._PD_ACCESS_CACHE.update({"people": None, "at": 0.0})

    def tearDown(self):
        lf._PD_ACCESS_CACHE.update({"people": None, "at": 0.0})

    def _run(self, fn, access=_access(), person_tt=6577160, handoff_fails=False):
        objs = {("full-pipeline-cache", "deals.json"): json.dumps(DEALS).encode(),
                ("pipeline-token", "agent-data.json"): json.dumps(AGENT_DATA).encode()}
        if access is not None:
            objs[("full-pipeline-cache", "partner-desk/access.json")] = json.dumps(access).encode()
        s3, ses = FakeS3(objs), FakeSES()
        person = {"id": 42, "first_name": "A", "last_name": "B", "email": "a@b.com",
                  "custom_fields": {TT: [person_tt]}}

        def api(method, endpoint, payload=None, jwt=None, timeout=15):
            return {"status": 200, "data": person if method == "GET" else {}}

        patches = [mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3),
                   mock.patch.object(lf, "call_pipeline_api", side_effect=api),
                   mock.patch.object(lf, "get_jwt", return_value="j"),
                   mock.patch.object(lf, "_classify_click", return_value="human")]
        if handoff_fails:
            patches.append(mock.patch.object(lf, "_pd_handoff", side_effect=RuntimeError("no secret")))
        for p in patches:
            p.start()
        try:
            res = fn()
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertFalse([p for p in s3.puts if p["Key"] == "partner-desk/access.json"])
        return res, ses

    @staticmethod
    def deal(method, did="7"):
        return lambda: lf._handle_mailer_click({"pid": "42", "did": did, "t": lf._mailer_token("42", did)}, method, {})

    @staticmethod
    def co(method, m=None):
        p = {"pid": "42", "co": "SpaceX", "t": lf._mailer_token("42", "c:SpaceX")}
        if m:
            p["m"] = m
        return lambda: lf._handle_mailer_click_co(p, method, {})

    def assertDeskWithGp(self, url):
        self.assertTrue(url.startswith(DESK + "&gp=42."), url)

    # --- gated ---
    def test_buy_get_gated(self):
        res, _ = self._run(self.deal("GET"))
        self.assertIn(json.dumps(DESK + "&gp=42.")[:-1], res["body"])
        self.assertNotIn(OFFER, res["body"])

    def test_buy_post_gated(self):
        res, ses = self._run(self.deal("POST"))
        self.assertEqual(res["statusCode"], 302)
        self.assertDeskWithGp(res["headers"]["Location"])
        msg = ses.sent[0]["Message"]
        self.assertTrue(msg["Subject"]["Data"].startswith("[Intermediary] "))
        self.assertIn("→ sent to Partner Desk", msg["Body"]["Text"]["Data"])

    def test_co_gated_get_and_post(self):
        for m in (None, "1"):
            with self.subTest(m=m):
                res, _ = self._run(self.co("GET", m))
                self.assertIn(json.dumps(DESK + "&gp=42.")[:-1], res["body"])
                self.assertNotIn(OFFER, res["body"])
                self.assertNotIn("mailto:", res["body"])
                res, ses = self._run(self.co("POST", m))
                self.assertEqual(res["statusCode"], 302)
                self.assertDeskWithGp(res["headers"]["Location"])
                self.assertIn("→ sent to Partner Desk", ses.sent[0]["Message"]["Body"]["Text"]["Data"])

    def test_handoff_failure_still_desk(self):
        res, _ = self._run(self.deal("POST"), handoff_fails=True)
        self.assertEqual(res["headers"]["Location"], DESK)
        res, _ = self._run(self.deal("GET"), handoff_fails=True)
        self.assertIn(json.dumps(DESK), res["body"])
        res, _ = self._run(self.co("GET"), handoff_fails=True)
        self.assertIn(json.dumps(DESK), res["body"])

    # --- not gated: unchanged ---
    def _assert_offer(self, **kw):
        res, ses = self._run(self.deal("GET"), **kw)
        self.assertIn(OFFER, res["body"])
        res, ses = self._run(self.deal("POST"), **kw)
        self.assertTrue(res["headers"]["Location"].startswith(OFFER))
        self.assertNotIn("Partner Desk", ses.sent[0]["Message"]["Body"]["Text"]["Data"])
        res, _ = self._run(self.co("GET"), **kw)
        self.assertIn(OFFER, res["body"])
        res, _ = self._run(self.co("GET", "1"), **kw)
        self.assertIn("mailto:", res["body"])
        res, _ = self._run(self.co("POST"), **kw)
        self.assertEqual(res["statusCode"], 200)

    def test_partner_registered_and_investor_pd(self):
        for pd in (7218528, 7218523, "7218528"):
            with self.subTest(pd=pd):
                self._assert_offer(access=_access(pd=pd))

    def test_non_intermediary_investor(self):
        self._assert_offer(access=_access(intermediary=False), person_tt=6484811)

    def test_unknown_pid(self):
        self._assert_offer(access={"people": {"99": {"intermediary": True, "pd": None}}})

    def test_access_unreadable(self):
        self._assert_offer(access=None)

    def test_sell_unchanged_for_gated(self):
        res, _ = self._run(self.deal("GET", "8"))
        self.assertIn("https://trades.graciagroup.com/deal/8?gp=42.", res["body"])
        res, _ = self._run(self.deal("POST", "8"))
        self.assertEqual(res["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
