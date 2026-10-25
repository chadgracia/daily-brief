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


class FakeS3List(FakeS3):
    def list_objects_v2(self, Bucket, Prefix, StartAfter=""):
        keys = sorted(k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix) and k > StartAfter)
        return {"Contents": [{"Key": k} for k in keys]}


class PdEvent(unittest.TestCase):
    def setUp(self):
        lf._PD_SECRET = "abc"

    def _gp(self, pid=42, exp_delta=3600, secret="abc"):
        exp = int(lf.datetime.now(lf.timezone.utc).timestamp()) + exp_delta
        sig = hmac.new(secret.encode(), f"{pid}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{pid}.{exp}.{sig}"

    def _run(self, body, pd=None, s3=None, raw=None, put_status=200):
        s3 = s3 or FakeS3List({
            ("full-pipeline-cache", "partner-desk/access.json"): json.dumps(
                {"built_at": "x", "people": {"42": {"tt": 6577160, "pd": pd, "intermediary": True,
                                                    "buying": [], "emails": ["a@b.com"]}}}).encode(),
            ("full-pipeline-cache", "deals.json"): json.dumps(
                {"deals": [{"id": 7, "company": {"id": 9, "name": "SpaceX"}}]}).encode()})
        ses = FakeSES()
        person = {"id": 42, "first_name": "A", "last_name": "B", "email": "a@b.com",
                  "custom_fields": {TT: [6577160], **({PD: pd} if pd is not None else {})}}
        calls = []

        def api(method, endpoint, payload=None, jwt=None, timeout=15):
            calls.append((method, endpoint, payload))
            return {"status": put_status if method == "PUT" else 200, "data": person}

        ev = {"queryStringParameters": {"view": "pd-event"},
              "requestContext": {"http": {"method": "POST", "sourceIp": "1.2.3.4", "userAgent": "UA"}},
              "body": raw if raw is not None else json.dumps(body)}
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "call_pipeline_api", side_effect=api), \
                mock.patch.object(lf, "get_jwt", return_value="j"):
            res = lf.handle_http_request(ev)
        return res, calls, ses, s3

    def _events(self, s3):
        return [json.loads(v) for (b, k), v in s3.objects.items() if k.startswith("partner-desk/events/")]

    def test_bad_sig(self):
        res, calls, ses, s3 = self._run({"gp": self._gp(secret="nope"), "action": "leave", "did": ""})
        self.assertEqual(res["statusCode"], 403)
        self.assertEqual((calls, ses.sent, s3.puts), ([], [], []))

    def test_expired(self):
        res, calls, ses, s3 = self._run({"gp": self._gp(exp_delta=-5), "action": "leave", "did": ""})
        self.assertEqual(res["statusCode"], 403)
        self.assertEqual((calls, ses.sent, s3.puts), ([], [], []))

    def test_unknown_action(self):
        res, calls, ses, s3 = self._run({"gp": self._gp(), "action": "hack", "did": ""})
        self.assertEqual(res["statusCode"], 400)
        self.assertEqual((calls, ses.sent, s3.puts), ([], [], []))

    def test_iqf_writes_for_promotable(self):
        for pd in (None, 7218524, 7218525, 7218527):
            for action in ("iqf_individual", "iqf_entity"):
                res, calls, ses, s3 = self._run({"gp": self._gp(), "action": action, "did": "7"}, pd=pd)
                self.assertEqual(res, {"statusCode": 204, "body": ""})
                puts = [c for c in calls if c[0] == "PUT"]
                self.assertEqual(puts, [("PUT", "/people/42.json",
                                         {"person": {"custom_fields": {PD: 7218523}}})])
                acc = json.loads(s3.objects[("full-pipeline-cache", "partner-desk/access.json")])
                self.assertEqual(acc["people"]["42"]["pd"], 7218523)
                ev = self._events(s3)[0]
                self.assertEqual((ev["pd_before"], ev["pd_after"], ev["wrote"]), (pd, 7218523, True))

    def test_iqf_skips_protected(self):
        for pd in (7218526, 7218528, 7218570, 7218529, 7218523):
            res, calls, ses, s3 = self._run({"gp": self._gp(), "action": "iqf_entity", "did": ""}, pd=pd)
            self.assertEqual(res["statusCode"], 204)
            self.assertFalse([c for c in calls if c[0] == "PUT"])
            self.assertFalse([p for p in s3.puts if p["Key"] == "partner-desk/access.json"])
            ev = self._events(s3)[0]
            self.assertEqual((ev["pd_after"], ev["wrote"]), (pd, False))

    def test_non_iqf_never_writes_and_logs(self):
        for action in ("partner_desk", "leave", "iqf_individual"):
            res, calls, ses, s3 = self._run({"gp": self._gp(), "action": action, "did": "7"}, pd=7218524,
                                            raw=None)
            self.assertEqual(res["statusCode"], 204)
            evs = self._events(s3)
            self.assertEqual(len(evs), 1)
            self.assertEqual((evs[0]["pid"], evs[0]["action"], evs[0]["did"], evs[0]["ip"], evs[0]["ua"]),
                             (42, action, "7", "1.2.3.4", "UA"))
            self.assertEqual(bool([c for c in calls if c[0] == "PUT"]), action == "iqf_individual")
            self.assertEqual(ses.sent[0]["Message"]["Subject"]["Data"], "Partner Desk pop-up: A B — " + action)
            text = ses.sent[0]["Message"]["Body"]["Text"]["Data"]
            self.assertIn("Deal: 7 — SpaceX", text)
            self.assertIn("Intermediary - Co-Broker", text)
            self.assertIn("https://app.pipelinecrm.com/people/42", text)

    def test_text_plain_beacon_body(self):
        res, calls, ses, s3 = self._run(None, raw=json.dumps({"gp": self._gp(), "action": "leave", "did": ""}))
        self.assertEqual(res["statusCode"], 204)
        self.assertEqual(len(self._events(s3)), 1)

    def test_email_dedupe(self):
        res, calls, ses, s3 = self._run({"gp": self._gp(), "action": "leave", "did": ""})
        self.assertEqual(len(ses.sent), 1)
        _, _, ses2, s3 = self._run({"gp": self._gp(), "action": "leave", "did": ""}, s3=s3)
        self.assertEqual(ses2.sent, [])
        self.assertEqual(len(self._events(s3)), 2)
        _, _, ses3, s3 = self._run({"gp": self._gp(), "action": "partner_desk", "did": ""}, s3=s3)
        self.assertEqual(len(ses3.sent), 1)
        # an email older than 10 minutes no longer suppresses
        for (b, k), v in list(s3.objects.items()):
            if k.startswith("partner-desk/events/"):
                ev = json.loads(v)
                ev["ts_ms"] -= 11 * 60 * 1000
                del s3.objects[(b, k)]
                prefix, name = k.rsplit("/", 1)
                s3.objects[(b, prefix + "/" + str(ev["ts_ms"]) + "-42.json")] = json.dumps(ev).encode()
        _, _, ses4, s3 = self._run({"gp": self._gp(), "action": "leave", "did": ""}, s3=s3)
        self.assertEqual(len(ses4.sent), 1)

    def test_fail_soft(self):
        with mock.patch.object(lf, "_pd_recently_emailed", side_effect=RuntimeError("boom")), \
                mock.patch.object(lf, "_fetch_json", side_effect=RuntimeError("boom")):
            res, calls, ses, s3 = self._run({"gp": self._gp(), "action": "iqf_entity", "did": "7"})
        self.assertEqual(res, {"statusCode": 204, "body": ""})


if __name__ == "__main__":
    unittest.main()
