import hashlib
import hmac
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

TT, PD, BUY = "custom_label_3759163", "custom_label_4011837", "custom_label_3322093"


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise Exception("An error occurred (NoSuchKey) when calling the GetObject operation")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[(kw["Bucket"], kw["Key"])] = kw["Body"]


class FakeSES:
    def __init__(self):
        self.sent = []

    def send_email(self, **kw):
        self.sent.append(kw)
        return {"MessageId": "x"}


def _people_doc():
    return {"people": [
        {"id": 1, "email": "Broker@X.com ", "home_email": "b@home.com", "email2": "broker@x.com",
         "custom_fields": {TT: [6577160], BUY: [101, 102]}},
        {"id": 2, "email": "ref@client.com", "custom_fields": {PD: 7218570}},
        {"id": 3, "email": "investor@x.com", "custom_fields": {TT: [6484811], BUY: [101]}},
        {"id": 4, "email": "ff@x.com", "custom_fields": {TT: 6888333, PD: [7218527]}},
    ]}


AGENT_DATA = {"security_ids": {"SpaceX": {"h": 1, "b": 101, "s": 2},
                               "Anthropic#": {"h": None, "b": 102, "s": None}}}


class BuildAccess(unittest.TestCase):
    def test_build(self):
        s3 = FakeS3({("full-pipeline-cache", "people.json"): json.dumps(_people_doc()).encode(),
                     ("pipeline-token", "agent-data.json"): json.dumps(AGENT_DATA).encode()})
        with mock.patch.object(lf, "call_pipeline_api") as api:
            res = lf.build_partner_desk_access(s3)
            api.assert_not_called()
        self.assertTrue(res["ok"])
        doc = json.loads(s3.puts[-1]["Body"])
        self.assertEqual(s3.puts[-1]["Key"], "partner-desk/access.json")
        self.assertEqual(set(doc["people"]), {"1", "2", "4"})
        p1 = doc["people"]["1"]
        self.assertEqual(p1, {"tt": 6577160, "pd": None, "intermediary": True,
                              "buying": ["spacex", "anthropic"],
                              "emails": ["broker@x.com", "b@home.com"]})
        self.assertEqual(doc["people"]["2"]["pd"], 7218570)
        self.assertFalse(doc["people"]["2"]["intermediary"])
        self.assertTrue(doc["people"]["4"]["intermediary"])
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["intermediaries"], 2)
        self.assertEqual(res["pd_counts"], {7218570: 1, 7218527: 1})

    def test_live_label_fallback(self):
        s3 = FakeS3({("full-pipeline-cache", "people.json"): json.dumps(_people_doc()).encode()})
        live = {"status": 200, "data": {"entries": [{"id": 3322093, "custom_field_label_dropdown_entries": [
            {"id": 101, "name": "SpaceX$"}, {"id": 102, "name": "Anthropic #"}]}]}}
        with mock.patch.object(lf, "call_pipeline_api", return_value=live), \
                mock.patch.object(lf, "get_jwt", return_value="j"):
            lf.build_partner_desk_access(s3)
        doc = json.loads(s3.puts[-1]["Body"])
        self.assertEqual(doc["people"]["1"]["buying"], ["spacex", "anthropic"])

    def test_fail_soft_keeps_previous(self):
        s3 = FakeS3({("full-pipeline-cache", "partner-desk/access.json"): b'{"old": 1}'})
        res = lf.build_partner_desk_access(s3)
        self.assertFalse(res["ok"])
        self.assertEqual(s3.puts, [])


class Handoff(unittest.TestCase):
    def setUp(self):
        lf._PD_SECRET = None

    def test_secret_generated_and_cached(self):
        s3 = FakeS3()
        s1 = lf._pd_secret(s3)
        self.assertEqual(len(s1), 64)
        self.assertEqual(json.loads(s3.puts[0]["Body"])["secret"], s1)
        self.assertEqual(lf._pd_secret(FakeS3()), s1)

    def test_secret_existing(self):
        s3 = FakeS3({("pipeline-token", "partner-desk-secret.json"): b'{"secret": "abc"}'})
        self.assertEqual(lf._pd_secret(s3), "abc")
        self.assertEqual(s3.puts, [])

    def test_format(self):
        lf._PD_SECRET = "abc"
        with mock.patch.object(lf.boto3, "client", return_value=FakeS3()):
            tok = lf._pd_handoff(42)
        pid, exp, sig = tok.split(".")
        self.assertEqual(pid, "42")
        now = int(lf.datetime.now(lf.timezone.utc).timestamp())
        self.assertTrue(abs(int(exp) - (now + 30 * 86400)) < 5)
        self.assertEqual(sig, hmac.new(b"abc", f"42|{exp}".encode(), hashlib.sha256).hexdigest()[:32])

    def test_click_get_appends_gp(self):
        lf._PD_SECRET = "abc"
        s3 = FakeS3({("full-pipeline-cache", "deals.json"): json.dumps({"deals": []}).encode()})
        params = {"pid": "42", "did": "7", "t": lf._mailer_token("42", "7")}
        with mock.patch.object(lf.boto3, "client", return_value=s3):
            res = lf._handle_mailer_click(params, "GET")
        self.assertIn("https://trades.graciagroup.com/deal/7?gp=42.", res["body"])


class ReferralLock(unittest.TestCase):
    def _post(self, pd):
        s3 = FakeS3({("full-pipeline-cache", "deals.json"): json.dumps({"deals": [
            {"id": 7, "name": "SpaceX sell", "company": {"id": 9, "name": "SpaceX"},
             "custom_fields": {"custom_label_1958": 5011675}}]}).encode(),
            ("pipeline-token", "agent-data.json"): json.dumps(AGENT_DATA).encode()})
        ses = FakeSES()
        person = {"id": 42, "first_name": "A", "last_name": "B", "email": "a@b.com",
                  "custom_fields": {PD: pd} if pd else {}}
        calls = []

        def api(method, endpoint, payload=None, jwt=None, timeout=15):
            calls.append((method, endpoint))
            return {"status": 200, "data": person}

        params = {"pid": "42", "did": "7", "t": lf._mailer_token("42", "7")}
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "call_pipeline_api", side_effect=api), \
                mock.patch.object(lf, "get_jwt", return_value="j"), \
                mock.patch.object(lf, "_classify_click", return_value="human"):
            lf._handle_mailer_click(params, "POST", {})
        return calls, ses

    def test_locked_skips_write(self):
        for pd in (7218526, 7218570):
            calls, ses = self._post(pd)
            self.assertNotIn(("PUT", "/people/42.json"), calls)
            self.assertIn("Buy Interest NOT added — referral-locked account",
                          ses.sent[0]["Message"]["Body"]["Text"]["Data"])

    def test_unlocked_writes(self):
        calls, ses = self._post(7218525)
        self.assertIn(("PUT", "/people/42.json"), calls)
        self.assertIn("Buy Interest added", ses.sent[0]["Message"]["Body"]["Text"]["Data"])


class ReferralLockCo(unittest.TestCase):
    def _post(self, pd):
        s3 = FakeS3({("pipeline-token", "agent-data.json"): json.dumps(AGENT_DATA).encode()})
        ses = FakeSES()
        person = {"id": 42, "first_name": "A", "last_name": "B", "email": "a@b.com",
                  "custom_fields": {PD: [pd]} if pd else {}}
        calls = []

        def api(method, endpoint, payload=None, jwt=None, timeout=15):
            calls.append((method, endpoint))
            return {"status": 200, "data": person}

        params = {"pid": "42", "co": "SpaceX", "t": lf._mailer_token("42", "c:SpaceX")}
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "call_pipeline_api", side_effect=api), \
                mock.patch.object(lf, "get_jwt", return_value="j"), \
                mock.patch.object(lf, "_classify_click", return_value="human"):
            res = lf._handle_mailer_click_co(params, "POST", {})
        return calls, ses, res

    def test_locked_skips_write(self):
        for pd in (7218526, 7218570):
            calls, ses, res = self._post(pd)
            self.assertNotIn(("PUT", "/people/42.json"), calls)
            self.assertEqual(res["statusCode"], 200)
            self.assertIn("Buy Interest NOT added — referral-locked account",
                          ses.sent[0]["Message"]["Body"]["Text"]["Data"])

    def test_unlocked_writes(self):
        calls, ses, _ = self._post(7218527)
        self.assertIn(("PUT", "/people/42.json"), calls)
        self.assertIn("Buy Interest added", ses.sent[0]["Message"]["Body"]["Text"]["Data"])


class NeverMailReferralClient(unittest.TestCase):
    ACCESS = {"built_at": "x", "people": {
        "2": {"tt": None, "pd": 7218570, "intermediary": False, "buying": [],
              "emails": ["ref@client.com", "ref2@home.com"]},
        "4": {"tt": 6888333, "pd": 7218527, "intermediary": True, "buying": [], "emails": ["ff@x.com"]}}}
    ROWS = [("R", "Ref@Client.com", 2), ("H", "ref2@home.com", 2), ("F", "ff@x.com", 4), ("Z", "z@x.com", 5)]

    def _s3(self, with_access=True):
        objs = {("full-pipeline-cache", "news-mailer.json"): json.dumps(
            {"intro": "hi", "items": []}).encode()}
        if with_access:
            objs[("full-pipeline-cache", "partner-desk/access.json")] = json.dumps(self.ACCESS).encode()
        return FakeS3(objs)

    def _run(self, s3, fn, body):
        ses = FakeSES()
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "_fetch_mailer_rows", return_value=(list(self.ROWS), [])) as fetch, \
                mock.patch.object(lf, "_render_news_email", return_value="<p>x</p>"), \
                mock.patch.object(lf, "_merge_send_times"):
            res = fn(body)
        return res, ses, fetch

    def test_news_excludes(self):
        res, ses, _ = self._run(self._s3(), lf._handle_news_send, {"subject": "S"})
        to = [m["Destination"]["ToAddresses"][0] for m in ses.sent if m["Source"] == lf.MAILER_FROM]
        self.assertEqual(to, ["ff@x.com", "z@x.com"])
        self.assertEqual(json.loads(res["body"])["excluded_referral_client"], 2)
        self.assertIn("Excluded (Referral – Client): 2", ses.sent[-1]["Message"]["Body"]["Text"]["Data"])

    def test_news_aborts_without_access(self):
        s3 = self._s3(with_access=False)
        res, ses, fetch = self._run(s3, lf._handle_news_send, {"subject": "S"})
        self.assertEqual(res["statusCode"], 500)
        self.assertEqual(s3.puts, [])
        fetch.assert_not_called()
        self.assertEqual(len(ses.sent), 1)
        self.assertEqual(ses.sent[0]["Destination"]["ToAddresses"], lf.TO_ADDRS)
        self.assertIn("ABORTED", ses.sent[0]["Message"]["Subject"]["Data"])

    def _weekly(self, s3):
        with mock.patch.object(lf, "_fetch_json", return_value={"deals": []}), \
                mock.patch.object(lf, "_mailer_buyer_counts", return_value={}), \
                mock.patch.object(lf, "_mailer_eligible", return_value=([{"id": 1}], [])), \
                mock.patch.object(lf, "_load_mailer_selection", return_value={"1"}), \
                mock.patch.object(lf, "_mailer_buy_companies", return_value=[]), \
                mock.patch.object(lf, "_render_mailer_email", return_value="<p>x</p>"):
            return self._run(s3, lf._handle_mailer_send, {"subject": "S"})

    def test_weekly_excludes(self):
        res, ses, _ = self._weekly(self._s3())
        to = [m["Destination"]["ToAddresses"][0] for m in ses.sent if m["Source"] == lf.MAILER_FROM]
        self.assertEqual(to, ["ff@x.com", "z@x.com"])
        self.assertIn("Excluded (Referral – Client): 2", ses.sent[-1]["Message"]["Body"]["Text"]["Data"])

    def test_weekly_aborts_without_access(self):
        s3 = self._s3(with_access=False)
        res, ses, fetch = self._weekly(s3)
        self.assertEqual(res["statusCode"], 500)
        self.assertEqual(s3.puts, [])
        fetch.assert_not_called()
        self.assertEqual([m["Destination"]["ToAddresses"] for m in ses.sent], [lf.TO_ADDRS])


class Route(unittest.TestCase):
    def test_requires_key(self):
        os.environ["BRIEF_PAGE_KEY"] = "k"
        ev = {"queryStringParameters": {"view": "pd-access-rebuild"}, "requestContext": {"http": {"method": "GET"}}}
        self.assertEqual(lf.handle_http_request(ev)["statusCode"], 403)
        ev["queryStringParameters"]["key"] = "k"
        with mock.patch.object(lf, "build_partner_desk_access", return_value={
                "ok": True, "built_at": "t", "total": 3, "intermediaries": 2, "pd_counts": {7218570: 1}}):
            res = lf.handle_http_request(ev)
        self.assertEqual(res["statusCode"], 200)
        self.assertIn("Total rows: 3", res["body"])
        self.assertIn("Referral – Client (7218570): 1", res["body"])


if __name__ == "__main__":
    unittest.main()
