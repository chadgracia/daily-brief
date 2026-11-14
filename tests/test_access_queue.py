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


def post(s3, act, email, api, b64=False):
    body = urlencode({"act": act, "email": email})
    ev = {"body": base64.b64encode(body.encode()).decode() if b64 else body, "isBase64Encoded": b64}
    with mock.patch.object(lf, "call_pipeline_api", api):
        return lf._handle_access_queue_post(ev, s3=s3, jwt="jwt", now=NOW)


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
