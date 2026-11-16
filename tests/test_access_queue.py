"""Deal access queue on the online brief: render, Approve, Decline. Fake S3 and
Pipeline; no AWS, no live Pipeline write, no email."""
import base64
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import FakeS3, lf  # noqa: E402

B = "full-pipeline-cache"
Q = "deal-gate/queue.json"
A = "deal-gate/approved.json"
SLIM = "syndicate-dash/people-slim.json"
KEY = "test-brief-key"
NOW = 1_900_000_000.0
PEOPLE = {"people": [
    {"id": 302, "first_name": "Hal", "last_name": "Hold", "email": "hal@z.com",
     "company": {"name": "Hold Co"}, "custom_fields": {"custom_label_3923758": 7161646}},
    {"id": 305, "full_name": "Sue Self", "email": "sue@z.com", "custom_fields": {"custom_label_3923758": [7223459]}},
]}


class S3(FakeS3):
    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise Exception("NoSuchKey")
        return {"ETag": '"%d"' % len(self.objects[(Bucket, Key)])}


def store(queue=None, approved=None):
    objs = {(B, SLIM): json.dumps(PEOPLE).encode()}
    if queue is not None:
        objs[(B, Q)] = json.dumps(queue).encode()
    if approved is not None:
        objs[(B, A)] = json.dumps(approved).encode()
    return S3(objs)


QUEUE = {"items": {
    "hal@z.com": {"email": "hal@z.com", "person_id": "302", "reason": "signup",
                  "first_at": "2030-03-17T10:00:00+00:00", "last_at": "2030-03-17T10:00:00+00:00"},
    "sue@z.com": {"email": "sue@z.com", "person_id": "305", "reason": "self-confirmed",
                  "first_at": "2030-03-17T11:00:00+00:00", "last_at": "2030-03-17T11:00:00+00:00"},
    "new@z.com": {"email": "new@z.com", "person_id": None, "reason": "signup",
                  "first_at": "2030-03-17T12:00:00+00:00", "last_at": "2030-03-17T12:00:00+00:00"}},
    "declined": {}}


class SES:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_email(self, **kw):
        if self.fail:
            raise RuntimeError("MessageRejected")
        self.sent.append(kw)
        return {"MessageId": "x"}


def post(s3, act, email, api, b64=False, ses=None):
    body = urlencode({"act": act, "email": email})
    ev = {"body": base64.b64encode(body.encode()).decode() if b64 else body, "isBase64Encoded": b64}
    with mock.patch.object(lf, "call_pipeline_api", api), \
            mock.patch.object(lf.boto3, "client", side_effect=AssertionError("no real AWS client")):
        return lf._handle_access_queue_post(ev, s3=s3, jwt="jwt", now=NOW, ses=ses if ses is not None else SES())


class Api:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, method, endpoint, payload=None, jwt=None, timeout=15):
        self.calls.append((method, endpoint, payload))
        return {"status": self.status, "data": {}}


class AccessQueue(unittest.TestCase):
    def setUp(self):
        lf._daq_slim_cache.update({"etag": None, "checked_at": 0.0, "by_email": None, "by_id": None})
        p = mock.patch("builtins.print"); self.log = p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"BRIEF_PAGE_KEY": KEY}); p.start(); self.addCleanup(p.stop)

    def q(self, s3):
        return json.loads(s3.objects[(B, Q)])

    def test_hidden_when_empty_or_missing(self):
        self.assertEqual(lf._render_access_queue_section(store()), "")
        self.assertEqual(lf._render_access_queue_section(store({"items": {}, "declined": {}})), "")

    def test_rows_and_post_forms(self):
        html = lf._render_access_queue_section(store(QUEUE))
        self.assertIn("Deal access queue (3)", html)
        for s in ("Hal Hold", "Hold Co", "hal@z.com", "Hold: Screen for Substantive", "signup",
                  "Sue Self", "Self-Confirmed Accredited", "self-confirmed", "2030-03-17",
                  "https://app.pipelinecrm.com/people/302", "not in CRM yet"):
            self.assertIn(s, html)
        self.assertEqual(html.count('method="post"'), 5)          # 2 + 2 + Decline only for new@z.com
        self.assertEqual(html.count(">Approve<"), 2)
        self.assertNotIn("<a href=\"?view=access-queue", html)     # never GET links
        self.assertIn('action="?view=access-queue&amp;key=test-brief-key"', html)

    def test_approve_puts_substantive_then_approved_json(self):
        s3, api = store(QUEUE), Api()
        r = post(s3, "approve", "hal@z.com", api, b64=True)
        self.assertEqual(r["statusCode"], 303)
        self.assertEqual(api.calls, [("PUT", "/people/302.json",
                                      {"person": {"custom_fields": {"custom_label_3923758": 7162165}}})])
        self.assertIn("302", json.loads(s3.objects[(B, A)]))
        self.assertNotIn("hal@z.com", self.q(s3)["items"])
        self.assertEqual(set(self.q(s3)["items"]), {"sue@z.com", "new@z.com"})

    def test_approve_pipeline_failure_changes_nothing(self):
        s3, api = store(QUEUE), Api(status=422)
        r = post(s3, "approve", "hal@z.com", api)
        self.assertIn("Nothing changed", r["body"])
        self.assertEqual(s3.puts, [])

    def test_approve_refused_without_crm_record(self):
        s3, api = store(QUEUE), Api()
        r = post(s3, "approve", "new@z.com", api)
        self.assertIn("not in the CRM yet", r["body"])
        self.assertEqual((api.calls, s3.puts), ([], []))

    def test_decline_sets_hold_and_records_declined(self):
        s3, api = store(QUEUE), Api()
        r = post(s3, "decline", "sue@z.com", api)
        self.assertEqual(r["statusCode"], 303)
        self.assertEqual(api.calls[0][2], {"person": {"custom_fields": {"custom_label_3923758": 7161646}}})
        q = self.q(s3)
        self.assertNotIn("sue@z.com", q["items"])
        self.assertIn("sue@z.com", q["declined"])
        self.assertNotIn((B, A), s3.objects)

    def test_decline_without_crm_record_skips_pipeline(self):
        s3, api = store(QUEUE), Api()
        post(s3, "decline", "new@z.com", api)
        self.assertEqual(api.calls, [])
        self.assertIn("new@z.com", self.q(s3)["declined"])

    def test_approved_json_pruned_and_appended(self):
        old = datetime.fromtimestamp(NOW - 30 * 86400, timezone.utc).isoformat()
        s3 = store(QUEUE, approved={"999": old, "305": datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat()})
        post(s3, "approve", "hal@z.com", Api())
        self.assertEqual(set(json.loads(s3.objects[(B, A)])), {"305", "302"})

    def test_unknown_action_and_missing_item(self):
        s3, api = store(QUEUE), Api()
        self.assertIn("unknown request", post(s3, "delete", "hal@z.com", api)["body"])
        self.assertIn("no longer in the queue", post(s3, "approve", "zzz@z.com", api)["body"])
        self.assertEqual((api.calls, s3.puts), ([], []))

    def test_route_is_post_only_behind_key(self):
        ev = {"queryStringParameters": {"view": "access-queue", "key": "wrong"},
              "requestContext": {"http": {"method": "POST"}}, "body": "act=approve&email=hal%40z.com"}
        with mock.patch.dict(os.environ, {"BRIEF_PAGE_KEY": KEY}):
            self.assertEqual(lf.handle_http_request(ev)["statusCode"], 403)
        with mock.patch.object(lf, "_handle_access_queue_post", return_value={"statusCode": 303}) as h:
            ev["queryStringParameters"]["key"] = KEY
            self.assertEqual(lf.handle_http_request(ev)["statusCode"], 303)
            h.assert_called_once()

    def test_logs_never_contain_emails(self):
        s3 = store(QUEUE)
        post(s3, "approve", "hal@z.com", Api())
        post(s3, "decline", "sue@z.com", Api(status=500))
        logged = " ".join(str(c) for c in self.log.call_args_list)
        self.assertNotIn("@z.com", logged)


if __name__ == "__main__":
    unittest.main()


class QueueDetailsColumns(unittest.TestCase):
    def setUp(self):
        lf._daq_slim_cache.update({"etag": None, "checked_at": 0.0, "by_email": None, "by_id": None})
        p = mock.patch("builtins.print"); p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"BRIEF_PAGE_KEY": KEY}); p.start(); self.addCleanup(p.stop)

    def test_form_columns_and_name_precedence(self):
        people = {"people": PEOPLE["people"] + [
            {"id": 310, "first_name": ".", "last_name": "(unverified)", "email": "dot@z.com", "custom_fields": {}}]}
        q = {"items": {
            "hal@z.com": {"email": "hal@z.com", "person_id": "302", "reason": "self-confirmed",
                          "first_name": "Harry", "last_name": "H", "country": "Canada", "employer": "<b>Hold Co</b>",
                          "accredited": "yes", "details_at": "2030-03-17T10:00:00+00:00", "from": "/deal/9?x=1",
                          "first_at": "2030-03-17T10:00:00+00:00", "last_at": "2030-03-17T10:00:00+00:00"},
            "dot@z.com": {"email": "dot@z.com", "person_id": "310", "reason": "signup", "first_name": "Dee",
                          "last_name": "Dot", "country": "France", "accredited": "unsure", "from": "/",
                          "first_at": "2030-03-17T11:00:00+00:00", "last_at": "2030-03-17T11:00:00+00:00"},
            "new@z.com": {"email": "new@z.com", "person_id": None, "reason": "signup", "first_name": "Nia",
                          "last_name": "New", "country": "Japan",
                          "first_at": "2030-03-17T12:00:00+00:00", "last_at": "2030-03-17T12:00:00+00:00"},
            "old@z.com": {"email": "old@z.com", "person_id": None, "reason": "signup",
                          "first_at": "2030-03-17T09:00:00+00:00", "last_at": "2030-03-17T09:00:00+00:00"}},
            "declined": {}}
        s3 = S3({(B, SLIM): json.dumps(people).encode(), (B, Q): json.dumps(q).encode()})
        html = lf._render_access_queue_section(s3)
        for h in ("Country", "Employer", "Accredited", "From"):
            self.assertIn(f">{h}</th>", html)
        self.assertIn(">Hal Hold</a>", html)               # real Pipeline name wins over "Harry H"
        self.assertNotIn("Harry", html)
        self.assertIn(">Dee Dot</a>", html)                # placeholder Pipeline name -> form name
        self.assertIn(">Nia New</td>", html)               # not in CRM: form name
        self.assertIn(">(no name)</td>", html)             # no form, no CRM
        for s in ("Canada", "France", "Japan", ">Yes<", ">Not sure<", "&lt;b&gt;Hold Co&lt;/b&gt;",
                  'href="https://trades.graciagroup.com/deal/9?x=1"', ">/deal/9?x=1</a>"):
            self.assertIn(s, html)
        self.assertNotIn("<b>Hold Co</b>", html)
        self.assertEqual(html.count(">Approve<"), 2)        # hal, dot (CRM records); approve/decline unchanged


class ApprovalEmail(unittest.TestCase):
    def setUp(self):
        lf._daq_slim_cache.update({"etag": None, "checked_at": 0.0, "by_email": None, "by_id": None})
        p = mock.patch("builtins.print"); self.log = p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"BRIEF_PAGE_KEY": KEY}); p.start(); self.addCleanup(p.stop)

    def queue(self, frm="/deal/9?v=tok&key=k", first="Harry"):
        q = json.loads(json.dumps(QUEUE))
        q["items"]["hal@z.com"].update({"first_name": first, "from": frm,
                                        "linkedin": "https://www.linkedin.com/in/hal"})
        return q

    def test_sent_once_with_right_link(self):
        s3, ses = store(self.queue()), SES()
        r = post(s3, "approve", "hal@z.com", Api(), ses=ses)
        self.assertEqual(r["statusCode"], 303)
        self.assertEqual(len(ses.sent), 1)
        m = ses.sent[0]
        self.assertEqual(m["Destination"], {"ToAddresses": ["hal@z.com"], "BccAddresses": lf.TO_ADDRS})
        self.assertEqual(m["Source"], lf.MAILER_FROM)
        self.assertEqual(m["ReplyToAddresses"], ["cgracia@rainmakersecurities.com"])
        self.assertEqual(m["Message"]["Subject"]["Data"], "Your access is approved")
        self.assertEqual(m["Message"]["Body"]["Text"]["Data"],
                         "Hi Harry,\n\nYour access to deal terms at Gracia Group is approved.\n\n"
                         "View the deal: https://trades.graciagroup.com/deal/9\n\n"
                         "If I can help with anything, just reply to this email.\n\nChad Gracia\n")
        r = post(s3, "approve", "hal@z.com", Api(), ses=ses)       # same approval again: item gone
        self.assertIn("no longer in the queue", r["body"])
        self.assertEqual(len(ses.sent), 1)

    def test_link_falls_back_to_site_root_and_hello(self):
        for frm, want in (("/", "https://trades.graciagroup.com/"), ("/?view=x&sso=abc", "https://trades.graciagroup.com/"),
                          ("https://evil.example/deal/9", "https://trades.graciagroup.com/"),
                          ("/deal/9/", "https://trades.graciagroup.com/deal/9"), ("/deal/abc", "https://trades.graciagroup.com/"),
                          (None, "https://trades.graciagroup.com/")):
            self.assertEqual(lf._daq_return_link(frm), want, frm)
        q = self.queue(first="")
        q["items"]["hal@z.com"]["first_name"] = ""
        s3, ses = store(q), SES()
        post(s3, "approve", "hal@z.com", Api(), ses=ses)
        self.assertTrue(ses.sent[0]["Message"]["Body"]["Text"]["Data"].startswith("Hi Hal,"))  # Pipeline first name
        q["items"]["new2@z.com"] = {"email": "new2@z.com", "person_id": None}
        self.assertTrue(lf._daq_send_approval_email(ses, "x@z.com", "", "/"))
        self.assertTrue(ses.sent[-1]["Message"]["Body"]["Text"]["Data"].startswith("Hello,"))

    def test_not_sent_on_decline_or_failed_pipeline_write(self):
        ses = SES()
        post(store(self.queue()), "decline", "hal@z.com", Api(), ses=ses)
        r = post(store(self.queue()), "approve", "hal@z.com", Api(status=500), ses=ses)
        self.assertIn("Nothing changed", r["body"])
        self.assertEqual(ses.sent, [])

    def test_approval_succeeds_when_send_fails(self):
        s3 = store(self.queue())
        r = post(s3, "approve", "hal@z.com", Api(), ses=SES(fail=True))
        self.assertIn("Approved. The approval email could not be sent.", r["body"])
        self.assertIn("302", json.loads(s3.objects[(B, A)]))
        self.assertNotIn("hal@z.com", json.loads(s3.objects[(B, Q)])["items"])
        logged = " ".join(str(c) for c in self.log.call_args_list)
        self.assertIn("approval email failed", logged)
        for pii in ("hal@z.com", "Harry", "Hal"):
            self.assertNotIn(pii, logged)

    def test_linkedin_column(self):
        q = self.queue()
        q["items"]["sue@z.com"]["linkedin"] = "javascript:alert(1)"
        html = lf._render_access_queue_section(store(q))
        self.assertIn(">LinkedIn</th>", html)
        self.assertIn('<a href="https://www.linkedin.com/in/hal" target="_blank" rel="noopener noreferrer"', html)
        self.assertEqual(html.count(">Profile</a>"), 1)
        self.assertNotIn("javascript:", html)
        self.assertIn("emails the person a link back to the deal. Decline sets Hold and sends nothing.", html)


class CatchUp(unittest.TestCase):
    """Form details saved before the person existed: link and fill names once."""
    def setUp(self):
        lf._daq_slim_cache.update({"etag": None, "checked_at": 0.0, "by_email": None, "by_id": None})
        lf._daq_live_tried.clear()
        p = mock.patch("builtins.print"); self.log = p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"BRIEF_PAGE_KEY": KEY}); p.start(); self.addCleanup(p.stop)
        self.calls = []
        self.live_entries = [{"id": 888, "email": "Nia@Z.com"}]
        self.person = {"first_name": "", "last_name": "Unknown"}

    def api(self, method, endpoint, payload=None, jwt=None, timeout=15):
        self.calls.append((method, endpoint, payload, timeout))
        if endpoint.startswith("/people.json?"):
            return {"status": 200, "data": {"entries": self.live_entries}}
        if method == "GET":
            return {"status": 200, "data": dict(self.person)}
        return {"status": 200, "data": {}}

    def store(self, extra_people=()):
        q = {"items": {"nia@z.com": {"email": "nia@z.com", "person_id": None, "reason": "signup",
                                     "first_name": "Nia", "last_name": "New", "country": "Japan",
                                     "accredited": "yes", "details_at": "2030-03-17T10:00:00+00:00",
                                     "from": "/deal/9", "first_at": "2030-03-17T10:00:00+00:00",
                                     "last_at": "2030-03-17T10:00:00+00:00"}}, "declined": {}}
        people = {"people": PEOPLE["people"] + list(extra_people)}
        return S3({(B, SLIM): json.dumps(people).encode(), (B, Q): json.dumps(q).encode()})

    def render(self, s3, now=NOW):
        with mock.patch.object(lf, "call_pipeline_api", self.api):
            return lf._render_access_queue_section(s3, now=now, jwt="jwt")

    def item(self, s3):
        return json.loads(s3.objects[(B, Q)])["items"]["nia@z.com"]

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def test_live_match_links_and_fills_names_once(self):
        s3 = self.store()
        html = self.render(s3)
        self.assertIn('href="https://app.pipelinecrm.com/people/888"', html)
        self.assertIn(">Nia New</a>", html)
        it = self.item(s3)
        self.assertEqual(it["person_id"], "888")
        self.assertTrue(it["names_synced_at"])
        self.assertEqual(self.writes(), [("PUT", "/people/888.json",
                                          {"person": {"first_name": "Nia", "last_name": "New"}}, 3)])
        lookup = [c for c in self.calls if c[1].startswith("/people.json?")][0]
        self.assertIn("conditions[email]=nia%40z.com", lookup[1])
        self.assertEqual(lookup[3], 3)
        self.calls.clear()
        self.render(s3, now=NOW + 3600)                       # later renders: no lookup, no write
        self.assertEqual(self.calls, [])

    def test_snapshot_match_no_live_lookup(self):
        s3 = self.store([{"id": 401, "first_name": ".", "email": "nia@z.com", "custom_fields": {}}])
        self.render(s3)
        self.assertFalse([c for c in self.calls if c[1].startswith("/people.json?")])
        self.assertEqual(self.item(s3)["person_id"], "401")
        self.assertEqual(self.writes()[0][1], "/people/401.json")

    def test_real_name_never_overwritten(self):
        self.person = {"first_name": "Nadia", "last_name": "Newman"}
        s3 = self.store()
        self.render(s3)
        self.assertEqual(self.writes(), [])
        self.assertTrue(self.item(s3)["names_synced_at"])      # marked done; never retried

    def test_not_found_throttled_and_never_creates(self):
        self.live_entries = [{"id": 5, "email": "nia@z.com.evil"}]
        s3 = self.store()
        self.render(s3)
        self.render(s3, now=NOW + 300)                        # within 10 min: no second lookup
        self.assertEqual(len([c for c in self.calls if c[1].startswith("/people.json?")]), 1)
        self.render(s3, now=NOW + 601)
        self.assertEqual(len([c for c in self.calls if c[1].startswith("/people.json?")]), 2)
        self.assertEqual(self.writes(), [])                  # no POST /people, no PUT
        self.assertIsNone(self.item(s3)["person_id"])

    def test_lookup_failure_is_soft(self):
        def boom(*a, **k):
            raise TimeoutError()
        s3 = self.store()
        with mock.patch.object(lf, "call_pipeline_api", boom):
            html = lf._render_access_queue_section(s3, now=NOW, jwt="jwt")
        self.assertIn("Deal access queue (1)", html)
        self.assertIn("not in CRM yet", html)
        logged = " ".join(str(c) for c in self.log.call_args_list)
        self.assertNotIn("nia@z.com", logged)
        self.assertNotIn("Nia", logged)
