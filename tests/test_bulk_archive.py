import csv
import email
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import FakeS3, FakeSES, lf  # noqa: E402

CHAD = 1259927678
ROWS = [
    ("Ann", "ann@x.com", 1, ()),
    ("Blk", "b@rainmakersecurities.com", 2, ()),
    ("Ref", "ref@client.com", 3, ()),
    ("Old", "old@x.com", 4, ()),
    ("Bad", "bounce@x.com", 5, ()),
    ("Nop", "nopid@x.com", None, ()),
    ("Zed", "zed@x.com", 7),
]
ACCESS = {"built_at": "x", "people": {"3": {"tt": None, "pd": 7218570, "intermediary": False,
                                            "buying": [], "emails": ["ref@client.com"]}}}


class RawSES(FakeSES):
    def __init__(self, raw_error=None, fail_to=()):
        super().__init__()
        self.raw = []
        self.raw_error = raw_error
        self.fail_to = set(fail_to)

    def send_email(self, **kw):
        if kw["Destination"]["ToAddresses"][0] in self.fail_to:
            raise RuntimeError("rejected")
        return super().send_email(**kw)

    def send_raw_email(self, **kw):
        if self.raw_error:
            raise self.raw_error
        self.raw.append(kw)
        return {"MessageId": "r"}


def _render(first, pid, *a, **k):
    return '<p>Hi ' + first + '</p><a href="https://example.test/?view=click&pid=' + str(pid) + '">x</a>'


class Archive(unittest.TestCase):
    def _run(self, fn, body, rows=ROWS, ses=None):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prefix = "news-send-" if fn is lf._handle_news_send else "mailer-send-"
        s3 = FakeS3({
            ("full-pipeline-cache", "news-mailer.json"): json.dumps({"intro": "hi", "items": []}).encode(),
            ("full-pipeline-cache", "partner-desk/access.json"): json.dumps(ACCESS).encode(),
            ("full-pipeline-cache", prefix + today + ".json"): json.dumps({"sent": ["old@x.com"]}).encode(),
        })
        ses = ses or RawSES(fail_to={"bounce@x.com"})
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "_fetch_mailer_rows", return_value=(list(rows), [])), \
                mock.patch.object(lf, "_render_news_email", side_effect=_render) as rn, \
                mock.patch.object(lf, "_render_mailer_email", side_effect=_render) as rm, \
                mock.patch.object(lf, "_merge_send_times"), \
                mock.patch.object(lf, "_fetch_json", return_value={"deals": []}), \
                mock.patch.object(lf, "_mailer_buyer_counts", return_value={}), \
                mock.patch.object(lf, "_mailer_eligible", return_value=([{"id": 1}], [])), \
                mock.patch.object(lf, "_load_mailer_selection", return_value={"1"}), \
                mock.patch.object(lf, "_mailer_buy_companies", return_value=[]):
            res = fn(body)
        return res, s3, ses, (rn if fn is lf._handle_news_send else rm)

    def _parse(self, raw):
        msg = email.message_from_string(raw["RawMessage"]["Data"])
        html = csv_text = None
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode("utf-8")
            elif part.get_filename() == "recipients.csv":
                csv_text = part.get_payload(decode=True).decode("utf-8")
        return msg, html, list(csv.DictReader(io.StringIO(csv_text)))

    def _check(self, fn, body, label, extra_status=None):
        res, s3, ses, render = self._run(fn, body)
        self.assertEqual(len(ses.raw), 1)
        raw = ses.raw[0]
        self.assertEqual(raw["Destinations"], ["cgracia@rainmakersecurities.com"])
        self.assertEqual(raw["Source"], lf.FROM_ADDR)
        msg, html, rows = self._parse(raw)
        subj = str(email.header.make_header(email.header.decode_header(msg["Subject"])))
        self.assertRegex(subj, r"^Archive: S — \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$")
        header, _, rendered = html.partition("<hr")
        self.assertNotIn("graciagroup", header.lower())
        for bad in ("ses", "lambda", lf.FROM_ADDR.lower()):
            self.assertNotIn(bad, header.lower())
        self.assertIn("Bulk send archive", header)
        self.assertIn("Mailer: " + label, header)
        for line in ("Sent: 2", "Failed: 1", "Blocked: 1", "Excluded (Referral – Client): 1",
                     "Saved search: " + str(lf.MAILER_SEARCH_ID)):
            self.assertIn(line, header)
        self.assertIn("pid=" + str(CHAD), rendered)
        self.assertIn("Hi Chad", rendered)
        self.assertNotIn("pid=1\"", rendered)
        self.assertEqual(render.call_args_list[-1].args[:2], ("Chad", CHAD))
        status = {r["email"]: r["status"] for r in rows}
        expected = {"ann@x.com": "sent", "b@rainmakersecurities.com": "blocked",
                    "ref@client.com": "excluded", "old@x.com": "skipped_already_sent",
                    "bounce@x.com": "failed", "nopid@x.com": "skipped_no_person_id",
                    "zed@x.com": "sent"}
        expected.update(extra_status or {})
        self.assertEqual(status, expected)
        self.assertEqual(list(rows[0].keys()), ["first_name", "email", "person_id", "status", "sent_at_utc"])
        for r in rows:
            self.assertEqual(bool(r["sent_at_utc"]), r["status"] == "sent")
        self.assertEqual(json.loads(res["body"])["sent"], 2)
        self.assertNotIn("ARCHIVE EMAIL FAILED", ses.sent[-1]["Message"]["Body"]["Text"]["Data"])
        return header

    def test_weekly(self):
        header = self._check(lf._handle_mailer_send, {"subject": "S"}, "Weekly newsletter")
        self.assertIn("Limit: none", header)

    def test_news(self):
        header = self._check(lf._handle_news_send, {"subject": "S"}, "News mailer")
        self.assertIn("Limit: none", header)

    def test_weekly_limit(self):
        # limit applies after blocked/excluded removal: ann, old, bounce, nopid, zed -> first 4
        res, s3, ses, _ = self._run(lf._handle_mailer_send, {"subject": "S", "limit": "4"})
        msg, html, rows = self._parse(ses.raw[0])
        self.assertIn("Limit: 4", html.partition("<hr")[0])
        self.assertEqual({r["email"]: r["status"] for r in rows}["zed@x.com"], "not_sent_limit")
        self.assertEqual(len(rows), len(ROWS))

    def test_no_archive_when_nothing_sent(self):
        for fn in (lf._handle_mailer_send, lf._handle_news_send):
            res, s3, ses, _ = self._run(fn, {"subject": "S"}, rows=[ROWS[1], ROWS[3]])
            self.assertEqual(json.loads(res["body"])["sent"], 0)
            self.assertEqual(ses.raw, [])

    def test_archive_failure_is_soft(self):
        for fn in (lf._handle_mailer_send, lf._handle_news_send):
            ok_res, ok_s3, ok_ses, _ = self._run(fn, {"subject": "S"})
            ses = RawSES(raw_error=RuntimeError("throttled"), fail_to={"bounce@x.com"})
            res, s3, ses, _ = self._run(fn, {"subject": "S"}, ses=ses)
            self.assertEqual(res, ok_res)
            self.assertEqual([p["Body"] for p in s3.puts], [p["Body"] for p in ok_s3.puts])
            report = ses.sent[-1]
            self.assertEqual(report["Destination"]["ToAddresses"], lf.TO_ADDRS)
            text = report["Message"]["Body"]["Text"]["Data"]
            self.assertIn("ARCHIVE EMAIL FAILED: RuntimeError: throttled", text)
            ok_text = ok_ses.sent[-1]["Message"]["Body"]["Text"]["Data"]
            self.assertEqual(text.replace("ARCHIVE EMAIL FAILED: RuntimeError: throttled\n", ""), ok_text)
            self.assertEqual(report["Message"]["Subject"], ok_ses.sent[-1]["Message"]["Subject"])

    def test_render_failure_is_soft(self):
        err = lf._send_bulk_archive(RawSES(), "News mailer", "S", [],
                                    mock.Mock(side_effect=KeyError("boom")), list)
        self.assertIn("KeyError", err)
        err = lf._send_bulk_archive(RawSES(), "News mailer", "S", [],
                                    lambda: "<p>x</p>", mock.Mock(side_effect=ValueError("csv")))
        self.assertIn("ValueError", err)

    def test_test_sends_have_no_archive(self):
        for fn in (lf._handle_mailer_test, lf._handle_news_test):
            res, s3, ses, _ = self._run(fn, {"subject": "S"})
            self.assertEqual(ses.raw, [])
            self.assertEqual(len(ses.sent), 1)


if __name__ == "__main__":
    unittest.main()
