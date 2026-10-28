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


def _lock_doc(age_min):
    t = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    return json.dumps({"kind": "weekly", "subject": "Old", "started_at": t.isoformat()}).encode()


class SES(FakeSES):
    def send_raw_email(self, **kw):
        return {"MessageId": "r"}


ROWS = [("P" + str(i), "p" + str(i) + "@x.com", 100 + i, ()) for i in range(25)]


class Base:
    def _s3(self, extra=None):
        objs = {(BUCKET, "news-mailer.json"): json.dumps({"intro": "hi", "items": []}).encode(),
                (BUCKET, "partner-desk/access.json"): json.dumps({"built_at": "x", "people": {}}).encode()}
        objs.update(extra or {})
        return LockS3(objs)

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
        self.assertRegex(err, r"^A send is already running \(started \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\)\. "
                              r"Check your send report email before trying again\.$")
        self.assertEqual(inner["ses"].sent, [])
        self.assertNotIn(LOCK, s3.objects)

    def test_fresh_lock_refuses_and_sends_nothing(self):
        held = _lock_doc(5)
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
        self.assertNotIn(LOCK, s3.objects)

    def test_lock_released_on_success_and_exception(self):
        s3 = self._s3()
        self._run({"subject": "S"}, s3)
        self.assertNotIn(LOCK, s3.objects)
        self.assertIn(LOCK[1], s3.deletes)
        s3 = self._s3()
        with mock.patch.object(lf, "_split_blocked", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._run({"subject": "S"}, s3)
        self.assertNotIn(LOCK, s3.objects)
        self.assertIn(LOCK[1], s3.deletes)

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


if __name__ == "__main__":
    unittest.main()
