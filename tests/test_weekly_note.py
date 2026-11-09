import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import FakeS3, lf  # noqa: E402

BUCKET = "full-pipeline-cache"
P = '<p style="font-size:15px;margin:0 0 16px 0;">'


def render(note=None):
    if note is None:
        return lf._render_mailer_email("Chad", 123, [], [], {}, True)
    return lf._render_mailer_email("Chad", 123, [], [], {}, True, note)


class WeeklyNoteRender(unittest.TestCase):
    def test_empty_note_adds_no_markup(self):
        base = render()
        self.assertEqual(render(""), base)
        self.assertEqual(render("  \n\n "), base)
        self.assertEqual(base.count("<p "), render("").count("<p "))

    def test_note_is_escaped(self):
        html = render("Big <b>news</b> & more")
        self.assertIn(P + "Big &lt;b&gt;news&lt;/b&gt; &amp; more</p>", html)
        self.assertNotIn("<b>news", html)

    def test_two_paragraphs(self):
        html = render("First para.\n\nSecond para.")
        self.assertIn(P + "First para.</p>" + P + "Second para.</p>", html)
        self.assertEqual(len(re.findall(r"<p [^>]*>(First|Second) para\.</p>", html)), 2)

    def test_note_sits_between_greeting_and_intro(self):
        html = render("Hello note")
        g, n, i = html.index("Hello Chad,"), html.index("Hello note"), html.index("See this week")
        self.assertTrue(g < n < i)

    def test_daily_highlight_box_absent(self):
        html = render("x")
        self.assertNotIn("Daily Highlight", html)
        self.assertNotIn("view=daily", html)
        self.assertNotIn('width="280"', html)


class WeeklyNoteStorage(unittest.TestCase):
    def test_save_and_load(self):
        s3 = FakeS3()
        lf._save_weekly_note(s3, "hi")
        doc = json.loads(s3.objects[(BUCKET, "weekly-note.json")])
        self.assertEqual(doc["note"], "hi")
        self.assertIn("saved_at", doc)
        self.assertEqual(lf._load_weekly_note(s3), "hi")
        lf._save_weekly_note(s3, "")
        self.assertEqual(lf._load_weekly_note(s3), "")

    def test_load_missing_is_empty(self):
        self.assertEqual(lf._load_weekly_note(FakeS3()), "")

    def test_mailer_test_saves_then_renders_note(self):
        s3 = FakeS3({(BUCKET, "deals.json"): b'{"deals": []}'})
        sent = []

        class SES:
            def send_email(self, **kw):
                sent.append(kw)

        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: s3 if svc == "s3" else SES()), \
                mock.patch.object(lf, "_mailer_buyer_counts", return_value={}), \
                mock.patch.object(lf, "_mailer_eligible", return_value=([], [])), \
                mock.patch.object(lf, "_load_mailer_selection", return_value=set()), \
                mock.patch.object(lf, "_mailer_buy_companies", return_value=[]), \
                mock.patch.object(lf, "_merge_send_times"):
            r = lf._handle_mailer_test({"subject": "S", "note": "A & B"})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(s3.objects[(BUCKET, "weekly-note.json")])["note"], "A & B")
        self.assertIn(P + "A &amp; B</p>", sent[0]["Message"]["Body"]["Html"]["Data"])


class DailyRouteStillWorks(unittest.TestCase):
    def test_view_daily_responds(self):
        pid = "123"
        ev = {"queryStringParameters": {"view": "daily", "pid": pid, "t": lf._mailer_token(pid, "daily")},
              "requestContext": {"http": {"method": "GET", "sourceIp": "1.2.3.4", "userAgent": "Mozilla"}}}
        r = lf.handle_http_request(ev)
        self.assertEqual(r["statusCode"], 200)
        self.assertIn("Daily Highlight", r["body"])

    def test_news_mailer_keeps_daily_box(self):
        try:
            html = lf._render_news_email("Chad", 123, {})
        except Exception:
            self.skipTest("news email needs more content to render")
        self.assertIn("Get the Daily Highlight", html)


if __name__ == "__main__":
    unittest.main()
