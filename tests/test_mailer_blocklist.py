import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import FakeS3, FakeSES, lf  # noqa: E402

ROWS = [
    ("Rain", "cgracia@rainmakersecurities.com", 1, ()),
    ("Mixed", "Someone@RainmakerSecurities.COM ", 2, ()),
    ("Apac", "apac@gmail.com", 3, ("", "x@rainmakerapac.com")),
    ("Listed", "GSelz@OutsetPartners.com", 4, ()),
    ("Outset", "other@outsetpartners.com", 5, ()),
    ("Cap", "a@rainmakercapital.com", 6, ()),
    ("Sub", "b@mail.rainmakersecurities.com", 7, ()),
    ("Plain", "z@x.com", 8),
]
SENT = ["other@outsetpartners.com", "a@rainmakercapital.com", "z@x.com"]


class Blocked(unittest.TestCase):
    def test_domains(self):
        self.assertTrue(lf._mailer_blocked(["cgracia@rainmakersecurities.com"]))
        self.assertTrue(lf._mailer_blocked(["  CGracia@RainmakerSecurities.Com "]))
        self.assertTrue(lf._mailer_blocked(["x@sub.rainmakerapac.com"]))
        self.assertTrue(lf._mailer_blocked(["me@gmail.com", "me@rainmakerapac.com"]))
        self.assertFalse(lf._mailer_blocked(["a@rainmakercapital.com"]))
        self.assertFalse(lf._mailer_blocked(["a@notrainmakersecurities.com"]))

    def test_each_listed_email(self):
        for e in lf.MAILER_BLOCK_EMAILS:
            self.assertTrue(lf._mailer_blocked([e]), e)
            self.assertTrue(lf._mailer_blocked(["ok@gmail.com", e.upper()]), e)

    def test_outset_not_listed(self):
        self.assertFalse(lf._mailer_blocked(["jdoe@outsetpartners.com"]))
        self.assertFalse(lf._mailer_blocked(["info@outsetpartners.com"]))

    def test_malformed_ignored(self):
        self.assertFalse(lf._mailer_blocked(["", None, "   ", "no-at", "a@@b", "x@", 5, {"e": 1}]))
        self.assertFalse(lf._mailer_blocked(None))

    def test_collect_picks_up_other_email_columns(self):
        data = {"columns": [{"id": "person_first_name"}, {"id": "person_email"}, {"id": "id"},
                            {"id": "person_work_email"}, {"id": "person_home_email"}],
                "entries": [["Al", "al@gmail.com", 9, None, "al@rainmakerapac.com"],
                            ["Bo", "bo@gmail.com", 10, "", ""]]}
        rows = []
        lf._collect_mailer_entries(data, set(), rows, [])
        keep, blocked = lf._split_blocked(rows)
        self.assertEqual([r[1] for r in blocked], ["al@gmail.com"])
        self.assertEqual([r[1] for r in keep], ["bo@gmail.com"])


class Sends(unittest.TestCase):
    def _s3(self):
        return FakeS3({
            ("full-pipeline-cache", "news-mailer.json"): json.dumps({"intro": "hi", "items": []}).encode(),
            ("full-pipeline-cache", "partner-desk/access.json"): json.dumps(
                {"built_at": "x", "people": {}}).encode(),
        })

    def _run(self, fn, body, extra=()):
        s3, ses, times = self._s3(), FakeSES(), {}
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "_fetch_mailer_rows", return_value=(list(ROWS), [])), \
                mock.patch.object(lf, "_render_news_email", return_value="<p>x</p>") as rn, \
                mock.patch.object(lf, "_render_mailer_email", return_value="<p>x</p>") as rm, \
                mock.patch.object(lf, "_merge_send_times",
                                  side_effect=lambda _s3, t: times.update(t)), \
                mock.patch.object(lf, "_fetch_json", return_value={"deals": []}), \
                mock.patch.object(lf, "_mailer_buyer_counts", return_value={}), \
                mock.patch.object(lf, "_mailer_eligible", return_value=([{"id": 1}], [])), \
                mock.patch.object(lf, "_load_mailer_selection", return_value={"1"}), \
                mock.patch.object(lf, "_mailer_buy_companies", return_value=[]):
            res = fn(body)
        return res, s3, ses, times, rn, rm

    def _check(self, res, s3, ses, times, render, report_prefix):
        bulk = [m for m in ses.sent if m["Source"] == lf.MAILER_FROM]
        self.assertEqual([m["Destination"]["ToAddresses"][0] for m in bulk], SENT)
        self.assertEqual(json.loads(res["body"])["blocked"], 5)
        # no click tokens rendered for blocked people (pid is the 2nd render arg)
        # (the compliance archive renders once more for Chad's own pid)
        self.assertEqual(sorted(c.args[1] for c in render.call_args_list
                                if c.args[1] != lf.MAILER_PREVIEW_PID), [5, 6, 8])
        self.assertEqual(sorted(times), ["5", "6", "8"])
        progress = [json.loads(p["Body"]) for p in s3.puts if "-send-" in p["Key"]]
        self.assertEqual(progress[-1]["sent"], sorted(SENT))
        # report to Chad (cgracia@rainmakersecurities.com) still goes out
        report = ses.sent[-1]
        self.assertEqual(report["Destination"]["ToAddresses"], ["cgracia@rainmakersecurities.com"])
        self.assertTrue(report["Message"]["Subject"]["Data"].startswith(report_prefix))
        self.assertIn("Blocked: 5", report["Message"]["Body"]["Text"]["Data"])

    def test_weekly(self):
        res, s3, ses, mt, _rn, rm = self._run(lf._handle_mailer_send, {"subject": "S"})
        self._check(res, s3, ses, mt, rm, "Weekly mailer sent: 3")

    def test_news(self):
        res, s3, ses, mt, rn, _rm = self._run(lf._handle_news_send, {"subject": "S"})
        self._check(res, s3, ses, mt, rn, "News mailer sent: 3")

    def test_test_sends_still_go_to_chad(self):
        for fn in (lf._handle_mailer_test, lf._handle_news_test):
            _res, _s3, ses, _mt, _rn, _rm = self._run(fn, {"subject": "S"})
            self.assertEqual([m["Destination"]["ToAddresses"] for m in ses.sent],
                             [["cgracia@rainmakersecurities.com"]], fn.__name__)


class Preview(unittest.TestCase):
    def test_blocked_section(self):
        with mock.patch.object(lf, "_fetch_mailer_rows", return_value=(list(ROWS), [])):
            body = lf._render_mailer_page()["body"]
        main, _, blocked = body.partition("Blocked (not sent)")
        self.assertTrue(blocked)
        self.assertIn("5 blocked recipients", blocked)
        self.assertIn("3 recipients", main)
        for e in SENT:
            self.assertIn(e, main)
            self.assertNotIn(e, blocked)
        for name, e in [("Rain", "cgracia@rainmakersecurities.com"), ("Apac", "x@rainmakerapac.com"),
                        ("Listed", "GSelz@OutsetPartners.com"), ("Sub", "b@mail.rainmakersecurities.com")]:
            self.assertIn(name, blocked)
            self.assertIn(e, blocked)
            self.assertNotIn(e, main)
        tsv = main.split('id="tsv"', 1)[1].split("</textarea>", 1)[0]
        self.assertNotIn("rainmaker" + "securities", tsv)


if __name__ == "__main__":
    unittest.main()
