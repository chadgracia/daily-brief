import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import FakeSES, lf  # noqa: E402

BUCKET = "full-pipeline-cache"
LOCK = (BUCKET, "mailer-send-lock.json")


class PreconditionFailed(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}


class LockS3:
    """Stub S3 honouring IfNoneMatch / IfMatch and supporting delete_object."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.etags = {k: "e0" for k in self.objects}
        self.puts, self.deletes, self.n = [], [], 0

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise Exception("An error occurred (NoSuchKey) when calling the GetObject operation")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]), "ETag": self.etags[(Bucket, Key)]}

    def put_object(self, **kw):
        k = (kw["Bucket"], kw["Key"])
        if kw.get("IfNoneMatch") == "*" and k in self.objects:
            raise PreconditionFailed()
        if "IfMatch" in kw and self.etags.get(k) != kw["IfMatch"]:
            raise PreconditionFailed()
        self.n += 1
        self.puts.append(kw)
        self.objects[k] = kw["Body"]
        self.etags[k] = "e" + str(self.n)

    def delete_object(self, Bucket, Key):
        self.deletes.append(Key)
        self.objects.pop((Bucket, Key), None)


def _lock_doc(age_min, heartbeat_min=None, released=False, sent=None):
    now = datetime.now(timezone.utc)
    d = {"kind": "weekly", "subject": "Old", "started_at": (now - timedelta(minutes=age_min)).isoformat()}
    if heartbeat_min is not None:
        d["heartbeat_at"] = (now - timedelta(minutes=heartbeat_min)).isoformat()
    if sent is not None:
        d["sent_so_far"] = sent
    if released:
        d.update(released=True, released_at=now.isoformat())
    return json.dumps(d).encode()


def _released(s3):
    return json.loads(s3.objects[LOCK]).get("released") is True


class AccessDenied(Exception):
    response = {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}


class NoReleaseS3(LockS3):
    """Lock writes carrying "released" fail, like a missing permission."""

    def put_object(self, **kw):
        if kw["Key"] == LOCK[1] and b'"released"' in kw["Body"]:
            raise AccessDenied("An error occurred (AccessDenied) when calling the PutObject operation")
        return super().put_object(**kw)


class SES(FakeSES):
    def send_raw_email(self, **kw):
        return {"MessageId": "r"}


ROWS = [("P" + str(i), "p" + str(i) + "@x.com", 100 + i, ()) for i in range(25)]


class Base:
    def _s3(self, extra=None, cls=LockS3):
        objs = {(BUCKET, "news-mailer.json"): json.dumps({"intro": "hi", "items": []}).encode(),
                (BUCKET, "partner-desk/access.json"): json.dumps({"built_at": "x", "people": {}}).encode()}
        objs.update(extra or {})
        return cls(objs)

    def _run(self, body, s3, ses=None, render=None, fn=None):
        ses = ses or SES()
        render = render or (lambda first, pid, *a, **k: "<p>x</p>")
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "_fetch_mailer_rows", return_value=(list(ROWS), [])), \
                mock.patch.object(lf, "_render_news_email", side_effect=render), \
                mock.patch.object(lf, "_render_mailer_email", side_effect=render), \
                mock.patch.object(lf, "_merge_send_times"), \
                mock.patch.object(lf, "_fetch_json", return_value={"deals": []}), \
                mock.patch.object(lf, "_mailer_buyer_counts", return_value={}), \
                mock.patch.object(lf, "_mailer_eligible", return_value=([{"id": 1}], [])), \
                mock.patch.object(lf, "_load_mailer_selection", return_value={"1"}), \
                mock.patch.object(lf, "_mailer_buy_companies", return_value=[]):
            res = (fn or self.fn)(body)
        return res, ses

    @staticmethod
    def bulk(ses):
        return [m["Destination"]["ToAddresses"][0] for m in ses.sent if m["Source"] == lf.MAILER_FROM]

    def test_second_concurrent_send_gets_409(self):
        s3 = self._s3()
        inner = {}

        def render(first, pid, *a, **k):
            if "res" not in inner:  # fire a second send while the first is mid-loop
                inner["res"], inner["ses"] = self._run({"subject": "Other"}, s3)
            return "<p>x</p>"
        res, ses = self._run({"subject": "S"}, s3, render=render)
        self.assertEqual(json.loads(res["body"])["sent"], 25)
        self.assertEqual(inner["res"]["statusCode"], 409)
        err = json.loads(inner["res"]["body"])["error"]
        self.assertRegex(err, r"^A send is already running \(started \d{2}:\d{2} UTC, last progress "
                              r"\d{2}:\d{2} UTC, \d+ sent so far\)\. "
                              r"Check your send report email before trying again\.$")
        self.assertEqual(inner["ses"].sent, [])
        self.assertTrue(_released(s3))
        self.assertEqual(s3.deletes, [])

    def test_fresh_lock_refuses_and_sends_nothing(self):
        held = _lock_doc(2)
        s3 = self._s3({LOCK: held})
        res, ses = self._run({"subject": "S"}, s3)
        self.assertEqual(res["statusCode"], 409)
        self.assertEqual(ses.sent, [])
        self.assertEqual(s3.puts, [])
        self.assertEqual(s3.deletes, [])
        self.assertEqual(s3.objects[LOCK], held)

    def test_stale_lock_taken_over(self):
        s3 = self._s3({LOCK: _lock_doc(31)})
        res, ses = self._run({"subject": "S"}, s3)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.bulk(ses)), 25)
        lock_puts = [json.loads(p["Body"]) for p in s3.puts if p["Key"] == LOCK[1]]
        self.assertEqual(lock_puts[0]["kind"], self.kind)
        self.assertEqual(lock_puts[0]["subject"], "S")
        self.assertTrue(_released(s3))

    def test_lock_released_on_success_and_exception(self):
        s3 = self._s3()
        self._run({"subject": "S"}, s3)
        self.assertTrue(_released(s3))
        s3 = self._s3()
        with mock.patch.object(lf, "_split_blocked", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._run({"subject": "S"}, s3)
        self.assertTrue(_released(s3))
        self.assertEqual(s3.deletes, [])

    def test_release_overwrites_never_deletes(self):
        s3 = self._s3()
        s3.delete_object = mock.Mock(side_effect=AssertionError("delete_object called"))
        self._run({"subject": "S"}, s3)
        doc = json.loads(s3.objects[LOCK])
        self.assertEqual(set(doc), {"kind", "subject", "started_at", "heartbeat_at", "sent_so_far",
                                    "released", "released_at"})
        self.assertEqual((doc["kind"], doc["subject"], doc["sent_so_far"], doc["released"]),
                         (self.kind, "S", 25, True))
        s3.delete_object.assert_not_called()
        lock_puts = [p for p in s3.puts if p["Key"] == LOCK[1]]
        self.assertTrue(all("IfMatch" in p for p in lock_puts[1:]))  # heartbeats + release are conditional

    def test_heartbeat_on_every_flush(self):
        s3 = self._s3()
        self._run({"subject": "S"}, s3)
        beats = [json.loads(p["Body"]) for p in s3.puts if p["Key"] == LOCK[1]]
        beats = [b for b in beats if "heartbeat_at" in b and not b.get("released")]
        self.assertEqual([b["sent_so_far"] for b in beats], [10, 20, 25])

    def test_released_lock_acquired_immediately(self):
        s3 = self._s3({LOCK: _lock_doc(1, heartbeat_min=0, released=True, sent=40)})
        res, ses = self._run({"subject": "S"}, s3)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.bulk(ses)), 25)

    def test_dead_heartbeat_taken_over(self):
        s3 = self._s3({LOCK: _lock_doc(20, heartbeat_min=6, sent=30)})
        res, ses = self._run({"subject": "S"}, s3)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.bulk(ses)), 25)
        first = json.loads([p for p in s3.puts if p["Key"] == LOCK[1]][0]["Body"])
        self.assertEqual(first["subject"], "S")

    def test_live_heartbeat_refused_with_details(self):
        held = _lock_doc(20, heartbeat_min=2, sent=30)
        s3 = self._s3({LOCK: held})
        res, ses = self._run({"subject": "S"}, s3)
        self.assertEqual(res["statusCode"], 409)
        err = json.loads(res["body"])["error"]
        self.assertIn("last progress", err)
        self.assertIn("30 sent so far", err)
        self.assertEqual(ses.sent, [])
        self.assertEqual(s3.objects[LOCK], held)

    def test_failed_release_warns_in_report(self):
        s3 = self._s3(cls=NoReleaseS3)
        res, ses = self._run({"subject": "S"}, s3)
        self.assertEqual(res["statusCode"], 200)
        report = ses.sent[-1]
        self.assertEqual(report["Message"]["Body"]["Text"]["Data"].split("\n")[0],
                         "WARNING: send lock was NOT released — the next send will be refused for up to 5 minutes.")
        self.assertTrue(report["Message"]["Subject"]["Data"].endswith(" recipients (lock not released)"))
        self.assertFalse(_released(s3))
        self.assertEqual(s3.deletes, [])

    def test_successful_release_report_unchanged(self):
        s3 = self._s3()
        res, ses = self._run({"subject": "S"}, s3)
        report = ses.sent[-1]
        self.assertNotIn("WARNING", report["Message"]["Body"]["Text"]["Data"])
        self.assertNotIn("lock not released", report["Message"]["Subject"]["Data"])

    def test_resend_same_subject_later_date_skips(self):
        s3 = self._s3()
        res, ses = self._run({"subject": "S"}, s3)
        self.assertEqual(len(self.bulk(ses)), 25)
        later = datetime.now(timezone.utc) + timedelta(days=3)

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return later if tz else later.replace(tzinfo=None)
        with mock.patch.object(lf, "datetime", Later):
            res, ses = self._run({"subject": "  s  "}, s3)
        body = json.loads(res["body"])
        self.assertEqual((body["sent"], body["skipped"]), (0, 25))
        self.assertEqual(self.bulk(ses), [])

    def test_different_subject_sends(self):
        s3 = self._s3()
        self._run({"subject": "S"}, s3)
        res, ses = self._run({"subject": "S2"}, s3)
        self.assertEqual(json.loads(res["body"])["sent"], 25)

    def test_progress_and_report(self):
        s3 = self._s3()
        res, ses = self._run({"subject": "S"}, s3)
        key = lf._send_progress_key(self.kind, "S")
        self.assertRegex(key, r"^mailer-send-[0-9a-f]{12}\.json$")
        flushes = [json.loads(p["Body"])["sent"] for p in s3.puts if p["Key"] == key]
        self.assertEqual([len(f) for f in flushes], [10, 20, 25])  # every 10 sends, then the final flush
        self.assertEqual(set(json.loads(s3.objects[(BUCKET, key)])), {"sent"})
        self.assertIn("Dedupe file: " + key + "\n", ses.sent[-1]["Message"]["Body"]["Text"]["Data"])
        self.assertNotEqual(lf._send_progress_key("weekly", "S"), lf._send_progress_key("news", "S"))

    def test_test_send_unaffected_by_lock(self):
        s3 = self._s3({LOCK: _lock_doc(1)})
        res, ses = self._run({"subject": "S"}, s3, fn=self.probe_fn)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual([m["Destination"]["ToAddresses"] for m in ses.sent],
                         [["cgracia@rainmakersecurities.com"]])
        self.assertEqual(s3.deletes, [])


class Weekly(Base, unittest.TestCase):
    kind = "weekly"
    fn = staticmethod(lf._handle_mailer_send)
    probe_fn = staticmethod(lf._handle_mailer_test)


class News(Base, unittest.TestCase):
    kind = "news"
    fn = staticmethod(lf._handle_news_send)
    probe_fn = staticmethod(lf._handle_news_test)


class ComposerScripts(unittest.TestCase):
    def test_network_error_message(self):
        for script in (lf.MAILER_COMPOSER_SCRIPT, lf.NEWS_COMPOSER_SCRIPT):
            self.assertIn("Connection lost — the send may still be running. Check your send report "
                          "email before doing anything.", script)
            self.assertIn("j._status === 409 ? j.error", script)
            catch = script.split("Connection lost")[1].split("});")[0]
            self.assertNotIn("disabled = false", catch)


class StatusLine(unittest.TestCase):
    def _line(self, objs):
        return lf._send_lock_status_html(LockS3(objs))

    def test_finished(self):
        out = self._line({LOCK: _lock_doc(40, heartbeat_min=35, released=True, sent=812)})
        self.assertIn("Last send: Old · started ", out)
        self.assertRegex(out, r"started \d{2}:\d{2} Kyiv · last progress \d{2}:\d{2} Kyiv · 812 sent · Finished<")

    def test_running(self):
        out = self._line({LOCK: _lock_doc(10, heartbeat_min=1, sent=50)})
        self.assertIn("50 sent · Running<", out)

    def test_stopped(self):
        out = self._line({LOCK: _lock_doc(20, heartbeat_min=12, sent=50)})
        self.assertIn("50 sent · Stopped — no progress for 12 min<", out)

    def test_kyiv_time(self):
        out = self._line({LOCK: json.dumps({"subject": "X", "started_at": "2026-11-02T10:05:00+00:00",
                                            "heartbeat_at": "2026-11-02T10:20:00+00:00",
                                            "released": True, "sent_so_far": 3}).encode()})
        self.assertIn("started 12:05 Kyiv · last progress 12:20 Kyiv · 3 sent · Finished", out)

    def test_missing_or_unreadable(self):
        self.assertIn(">No send recorded yet.<", self._line({}))
        self.assertIn(">No send recorded yet.<", self._line({LOCK: b"not json"}))

    def test_composers_show_line(self):
        s3 = LockS3({LOCK: _lock_doc(10, heartbeat_min=1, sent=7)})
        with mock.patch.object(lf.boto3, "client", return_value=s3), \
                mock.patch.object(lf, "_load_news_mailer", return_value={"items": []}), \
                mock.patch.object(lf, "_news_send_content", return_value=({"items": []}, 0)), \
                mock.patch.object(lf, "_render_news_email", return_value="<p>e</p>"):
            news = lf._render_news_composer(1)["body"]
        with mock.patch.object(lf.boto3, "client", return_value=s3), \
                mock.patch.object(lf, "_fetch_json", return_value={"deals": []}), \
                mock.patch.object(lf, "_mailer_buyer_counts", return_value={}), \
                mock.patch.object(lf, "_mailer_eligible", return_value=([], [])), \
                mock.patch.object(lf, "_mailer_buy_companies", return_value=[]), \
                mock.patch.object(lf, "_load_mailer_selection", return_value=set()), \
                mock.patch.object(lf, "_render_mailer_email", return_value="<p>e</p>"):
            weekly = lf._render_mailer_composer(1)["body"]
        for page, btn in ((news, "Send to list</button></div>"), (weekly, "Send to all recipients</button></div>")):
            self.assertIn(btn + '<p id="send-lock-status"', page)
            self.assertIn("7 sent · Running", page)


if __name__ == "__main__":
    unittest.main()
