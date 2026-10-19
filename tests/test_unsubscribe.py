"""Weekly mailer unsubscribe tests. Run: python3.12 -m unittest discover -s tests

boto3 isn't needed locally: it is stubbed before import, and every AWS / Pipeline
call is patched per test.
"""
import email
import json
import os
import sys
import types
import unittest
from email import policy
from unittest import mock

sys.modules.setdefault("boto3", types.ModuleType("boto3"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MAILER_CLICK_SECRET"] = "test-secret"
os.environ.pop("UNSUB_WEEKLY_TAG_ID", None)

import lambda_function as lf  # noqa: E402

TAG_ID = 555
PID = 123456


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        body = self.objects[Key]
        return {"Body": types.SimpleNamespace(read=lambda: body)}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body


class FakeSES:
    def __init__(self):
        self.raw = []
        self.simple = []

    def send_raw_email(self, **kw):
        self.raw.append(kw)

    def send_email(self, **kw):
        self.simple.append(kw)
        return {"MessageId": "m"}


class FakePipeline:
    """Minimal stand-in for call_pipeline_api: tag list + one person."""

    def __init__(self, tags=None, person_tags=None, put_status=200):
        self.tags = tags if tags is not None else [{"id": TAG_ID, "name": "Unsub: Weekly"}]
        self.person = {"id": PID, "first_name": "Ann", "email": "ann@example.com",
                       "predefined_contacts_tag_ids": list(person_tags or [])}
        self.put_status = put_status
        self.get_statuses = []  # consumed one per person GET before succeeding, e.g. [429, 429]
        self.put_statuses = []
        self.calls = []

    def __call__(self, method, endpoint, payload=None, jwt=None, timeout=15):
        self.calls.append((method, endpoint, payload))
        if endpoint.startswith("/admin/predefined_contacts_tags"):
            if method == "GET":
                return {"status": 200, "data": {"entries": self.tags}}
            self.tags.append({"id": 777, "name": payload["predefined_contacts_tag"]["name"]})
            return {"status": 201, "data": self.tags[-1]}
        if endpoint.startswith("/people.json"):
            return {"status": 200, "data": {"entries": [dict(self.person)]}}
        if endpoint == f"/people/{PID}.json":
            if method == "GET":
                if self.get_statuses:
                    return {"status": self.get_statuses.pop(0), "data": "Too Many Requests"}
                return {"status": 200, "data": dict(self.person)}
            if self.put_statuses:
                return {"status": self.put_statuses.pop(0), "data": "server error"}
            if self.put_status != 200:
                return {"status": self.put_status, "data": "forbidden"}
            self.person.update(payload["person"])
            return {"status": 200, "data": dict(self.person)}
        return {"status": 404, "data": "not found"}


class Base(unittest.TestCase):
    def setUp(self):
        lf._UNSUB_TAG_CACHE.clear()
        self.s3 = FakeS3()
        self.ses = FakeSES()
        self.pipe = FakePipeline()
        self.sleeps = []

        def client(name, region_name=None):
            return {"s3": self.s3, "ses": self.ses}[name]

        for target, value in (
            ("boto3.client", client),
            ("lambda_function.get_jwt", lambda: "jwt"),
            ("lambda_function.call_pipeline_api", self.pipe),
            ("lambda_function.time.sleep", self.sleeps.append),
        ):
            p = mock.patch(target, value, create=True)
            p.start()
            self.addCleanup(p.stop)


class TokenTests(unittest.TestCase):
    def test_good_token_round_trips(self):
        tok = lf._unsub_token(PID, "Ann@Example.com")
        self.assertEqual(lf._parse_unsub_token(tok), (PID, "ann@example.com"))

    def test_token_hides_raw_ids(self):
        tok = lf._unsub_token(PID, "ann@example.com")
        url = lf._unsub_url(PID, "ann@example.com")
        for raw in (str(PID), "ann", "example.com"):
            self.assertNotIn(raw, tok)
            self.assertNotIn(raw, url)

    def test_email_only_token(self):
        tok = lf._unsub_token(None, "x@example.com")
        self.assertEqual(lf._parse_unsub_token(tok), (None, "x@example.com"))
        self.assertIsNone(lf._parse_unsub_token(lf._unsub_token(None, "")))

    def test_tampered_token_rejected(self):
        tok = lf._unsub_token(PID, "ann@example.com")
        i = 12
        flipped = tok[:i] + ("A" if tok[i] != "A" else "B") + tok[i + 1:]
        self.assertIsNone(lf._parse_unsub_token(flipped))
        self.assertIsNone(lf._parse_unsub_token(tok[:-2]))
        self.assertIsNone(lf._parse_unsub_token("garbage!!"))
        self.assertIsNone(lf._parse_unsub_token(""))

    def test_wrong_secret_rejected(self):
        tok = lf._unsub_token(PID, "ann@example.com")
        with mock.patch.dict(os.environ, {"MAILER_CLICK_SECRET": "other"}):
            self.assertIsNone(lf._parse_unsub_token(tok))

    def test_expired_token_rejected(self):
        issued = 1_700_000_000
        tok = lf._unsub_token(PID, "ann@example.com", issued=issued)
        ttl = lf.UNSUB_TOKEN_TTL_DAYS * 86400
        self.assertIsNotNone(lf._parse_unsub_token(tok, now=issued + ttl - 1))
        self.assertIsNone(lf._parse_unsub_token(tok, now=issued + ttl + 1))

    def test_missing_secret_fails_closed(self):
        with mock.patch.dict(os.environ, {"MAILER_CLICK_SECRET": ""}):
            with self.assertRaises(RuntimeError):
                lf._unsub_token(PID, "ann@example.com")
            self.assertIsNone(lf._parse_unsub_token("anything"))


def _event(method, token):
    return {"requestContext": {"http": {"method": method, "sourceIp": "1.2.3.4", "userAgent": "x"}},
            "queryStringParameters": {"view": "unsub", "u": token},
            "body": "List-Unsubscribe=One-Click" if method == "POST" else None}


class RouteTests(Base):
    def test_get_records_and_shows_page(self):
        tok = lf._unsub_token(PID, "ann@example.com")
        resp = lf.lambda_handler(_event("GET", tok), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("unsubscribed from the weekly list", resp["body"])
        self.assertIn(str(PID), json.loads(self.s3.objects[lf.UNSUB_LIST_KEY])["people"])
        self.assertIn(TAG_ID, self.pipe.person["predefined_contacts_tag_ids"])
        self.assertEqual(len(self.ses.simple), 1)  # alert to Chad

    def test_post_one_click_records_with_no_page(self):
        tok = lf._unsub_token(PID, "ann@example.com")
        resp = lf.lambda_handler(_event("POST", tok), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(resp["body"], "")
        self.assertIn(str(PID), json.loads(self.s3.objects[lf.UNSUB_LIST_KEY])["people"])
        self.assertIn(TAG_ID, self.pipe.person["predefined_contacts_tag_ids"])

    def test_repeat_unsub_does_not_realert(self):
        tok = lf._unsub_token(PID, "ann@example.com")
        lf.lambda_handler(_event("POST", tok), None)
        lf.lambda_handler(_event("GET", tok), None)
        self.assertEqual(len(self.ses.simple), 1)
        puts = [c for c in self.pipe.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)

    def test_confirmation_page_shows_email(self):
        tok = lf._unsub_token(PID, "a<b>@example.com")
        resp = lf.lambda_handler(_event("GET", tok), None)
        self.assertIn("Recorded for <strong>a&lt;b&gt;@example.com</strong>", resp["body"])

    def test_preview_link_records_nothing(self):
        html = lf._render_mailer_email("Chad", lf.MAILER_PREVIEW_PID, [], [])
        self.assertIn(lf.UNSUB_PREVIEW_URL.replace("&", "&amp;"), html)
        resp = lf.lambda_handler(_event("GET", "preview"), None)
        self.assertEqual(resp["statusCode"], 400)
        self.assertNotIn(lf.UNSUB_LIST_KEY, self.s3.objects)

    def test_bad_token_records_nothing(self):
        for method, code in (("GET", 400), ("POST", 400)):
            resp = lf.lambda_handler(_event(method, "bogus"), None)
            self.assertEqual(resp["statusCode"], code)
        self.assertNotIn(lf.UNSUB_LIST_KEY, self.s3.objects)
        self.assertEqual(self.pipe.calls, [])

    def test_route_needs_no_admin_key_and_is_not_a_click(self):
        tok = lf._unsub_token(PID, "ann@example.com")
        with mock.patch.object(lf, "_classify_click") as cls, \
                mock.patch.object(lf, "_handle_mailer_click") as click:
            resp = lf.lambda_handler(_event("GET", tok), None)
        self.assertEqual(resp["statusCode"], 200)
        cls.assert_not_called()
        click.assert_not_called()
        self.assertNotIn("click-log/", "".join(self.s3.objects))


class PipelineWriteTests(Base):
    def test_adds_tag_keeping_existing_tags(self):
        self.pipe.person["predefined_contacts_tag_ids"] = [11, 22]
        ok, detail = lf._tag_person_unsub(PID, "jwt")
        self.assertTrue(ok, detail)
        put = [c for c in self.pipe.calls if c[0] == "PUT"][0]
        self.assertEqual(put[1], f"/people/{PID}.json")
        self.assertEqual(put[2], {"person": {"predefined_contacts_tag_ids": [11, 22, TAG_ID]}})

    def test_already_tagged_skips_write(self):
        self.pipe.person["predefined_contacts_tag_ids"] = [TAG_ID]
        ok, detail = lf._tag_person_unsub(PID, "jwt")
        self.assertTrue(ok)
        self.assertEqual(detail, "already tagged")
        self.assertFalse([c for c in self.pipe.calls if c[0] == "PUT"])

    def test_creates_tag_when_missing(self):
        self.pipe.tags = []
        ok, _ = lf._tag_person_unsub(PID, "jwt")
        self.assertTrue(ok)
        self.assertIn(777, self.pipe.person["predefined_contacts_tag_ids"])

    def test_env_var_pins_tag_id(self):
        with mock.patch.dict(os.environ, {"UNSUB_WEEKLY_TAG_ID": "999"}):
            ok, _ = lf._tag_person_unsub(PID, "jwt")
        self.assertTrue(ok)
        self.assertIn(999, self.pipe.person["predefined_contacts_tag_ids"])
        self.assertFalse([c for c in self.pipe.calls if "admin" in c[1]])

    def test_write_failure_still_suppresses_and_alerts(self):
        self.pipe.put_status = 403
        tok = lf._unsub_token(PID, "ann@example.com")
        resp = lf.lambda_handler(_event("GET", tok), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn(str(PID), json.loads(self.s3.objects[lf.UNSUB_LIST_KEY])["people"])
        subj = self.ses.simple[0]["Message"]["Subject"]["Data"]
        self.assertIn("FAILED", subj)

    def test_429_then_success_retries_with_backoff(self):
        self.pipe.get_statuses = [429, 429]
        self.pipe.put_statuses = [503]
        ok, detail = lf._tag_person_unsub(PID, "jwt")
        self.assertTrue(ok, detail)
        self.assertEqual(self.sleeps, [1.0, 2.0, 1.0])
        self.assertIn(TAG_ID, self.pipe.person["predefined_contacts_tag_ids"])

    def test_non_retryable_status_is_not_retried(self):
        self.pipe.put_status = 403
        ok, _ = lf._tag_person_unsub(PID, "jwt")
        self.assertFalse(ok)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len([c for c in self.pipe.calls if c[0] == "PUT"]), 1)

    def test_exhausted_retries_queue_then_later_pass_tags(self):
        self.pipe.get_statuses = [429] * lf.UNSUB_RETRY_ATTEMPTS
        tok = lf._unsub_token(PID, "ann@example.com")
        resp = lf.lambda_handler(_event("GET", tok), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])
        self.assertIn(str(PID), json.loads(self.s3.objects[lf.UNSUB_LIST_KEY])["people"])
        pending = json.loads(self.s3.objects[lf.UNSUB_PENDING_KEY])["people"]
        self.assertIn("person read failed (429)", pending[str(PID)]["last_error"])
        self.assertIn("queued for retry", self.ses.simple[0]["Message"]["Body"]["Text"]["Data"])
        self.assertEqual(lf._retry_unsub_queue(self.s3), (1, 0))
        self.assertIn(TAG_ID, self.pipe.person["predefined_contacts_tag_ids"])

    def test_retry_pass_keeps_failures_queued(self):
        self.s3.objects[lf.UNSUB_PENDING_KEY] = json.dumps(
            {"people": {str(PID): {"email": "ann@example.com", "passes": 0}}}).encode()
        self.pipe.get_statuses = [429] * 10
        self.assertEqual(lf._retry_unsub_queue(self.s3), (0, 1))
        self.assertEqual(json.loads(self.s3.objects[lf.UNSUB_PENDING_KEY])["people"][str(PID)]["passes"], 1)

    def test_scheduled_run_retries_queue(self):
        with mock.patch.object(lf, "_retry_unsub_queue") as retry, \
                mock.patch.object(lf, "_build_brief_html", return_value=("<p></p>", "Oct 19", {})):
            lf.lambda_handler({}, None)
        retry.assert_called_once()

    def test_s3_failure_shows_error_not_success(self):
        def boom(**kw):
            raise RuntimeError("s3 down")
        self.s3.put_object = boom
        tok = lf._unsub_token(PID, "ann@example.com")
        resp = lf.lambda_handler(_event("GET", tok), None)
        self.assertEqual(resp["statusCode"], 500)
        self.assertNotIn("You&rsquo;ve been unsubscribed", resp["body"])


ROWS = [("Ann", "ann@example.com", PID), ("Bob", "bob@example.com", 2),
        ("Cat", "cat@example.com", 3), ("Dan", "DAN@example.com", 4)]


class SendTests(Base):
    def setUp(self):
        super().setUp()
        self.s3.objects.update({
            "deals.json": json.dumps({"deals": []}).encode(),
            # Bob tagged by hand in Pipeline (cache only), Ann opted out via link (S3 list only).
            "people.json": json.dumps({"people": [
                {"id": 2, "email": "bob@example.com", "predefined_contacts_tag_ids": [TAG_ID]},
                {"id": 3, "email": "cat@example.com", "predefined_contacts_tag_ids": [1]},
            ]}).encode(),
            lf.UNSUB_LIST_KEY: json.dumps({"people": {
                str(PID): {"email": "ann@example.com"},
                "99": {"email": "dan@example.com"},  # email match, different id
            }}).encode(),
        })
        for target, value in (
            ("lambda_function._fetch_mailer_rows", lambda search_id=None: (list(ROWS), [])),
            ("lambda_function._mailer_eligible", lambda deals, counts=None: ([{"id": 1}], [])),
            ("lambda_function._load_mailer_selection", lambda s3: {"1"}),
            ("lambda_function._mailer_buyer_counts", lambda s3: {}),
            ("lambda_function._mailer_buy_companies", lambda s3, counts: []),
            ("lambda_function._mailer_table", lambda *a, **k: "<table></table>"),
            ("lambda_function._mailer_buy_table", lambda *a, **k: "<table></table>"),
            ("lambda_function._merge_send_times", lambda s3, t: None),
        ):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_drop_suppressed(self):
        kept, n = lf._drop_suppressed(ROWS, {PID, 2}, {"dan@example.com"})
        self.assertEqual([r[1] for r in kept], ["cat@example.com"])
        self.assertEqual(n, 3)

    def test_send_skips_tagged_and_opted_out(self):
        resp = lf._handle_mailer_send({"subject": "Weekly Pre-IPO secondary transactions: test"})
        body = json.loads(resp["body"])
        self.assertEqual(body["sent"], 1)
        self.assertEqual(body["unsubscribed"], 3)
        self.assertEqual([m["Destinations"] for m in self.ses.raw], [["cat@example.com"]])
        report = self.ses.simple[-1]["Message"]["Body"]["Text"]["Data"]
        self.assertIn("Skipped (Unsub: Weekly): 3", report)

    def test_sent_message_has_headers_and_footer(self):
        lf._handle_mailer_send({"subject": "Weekly Pre-IPO secondary transactions: — test"})
        raw = self.ses.raw[0]["RawMessage"]["Data"]
        msg = email.message_from_bytes(raw, policy=policy.default)
        lu = str(msg["List-Unsubscribe"])
        self.assertRegex(lu, r"^<https://[^>]+\?view=unsub&u=[A-Za-z0-9_-]+>, "
                             r"<mailto:cgracia@graciagroup\.com\?subject=unsubscribe>$")
        self.assertEqual(str(msg["List-Unsubscribe-Post"]), "List-Unsubscribe=One-Click")
        self.assertEqual(str(msg["Subject"]), "Weekly Pre-IPO secondary transactions: — test")
        self.assertEqual(str(msg["Reply-To"]), "cgracia@rainmakersecurities.com")
        for line in raw.split(b"\r\n"):
            self.assertLessEqual(len(line), 998)
        html = msg.get_body(("html",)).get_content()
        url = lu.split(">")[0][1:]
        self.assertIn('href="' + url.replace("&", "&amp;") + '"', html)
        self.assertIn(">Unsubscribe</a>", html)
        self.assertNotIn("TODO", html)
        self.assertNotIn("postal address", html)
        # header + footer token belong to this recipient
        token = url.split("u=")[1]
        self.assertEqual(lf._parse_unsub_token(token), (3, "cat@example.com"))

    def test_test_send_uses_headers(self):
        lf._handle_mailer_test({"subject": "x"})
        msg = email.message_from_bytes(self.ses.raw[0]["RawMessage"]["Data"], policy=policy.default)
        self.assertIn("view=unsub", str(msg["List-Unsubscribe"]))
        self.assertEqual(str(msg["List-Unsubscribe-Post"]), "List-Unsubscribe=One-Click")

    def _test_send_token(self):
        lf._handle_mailer_test({"subject": "x"})
        msg = email.message_from_bytes(self.ses.raw[0]["RawMessage"]["Data"], policy=policy.default)
        return str(msg["List-Unsubscribe"]).split("u=")[1].split(">")[0]

    def test_test_send_token_uses_recipient_person(self):
        people = json.loads(self.s3.objects["people.json"])
        people["people"].append({"id": 4242, "email": "CGracia@RainmakerSecurities.com"})
        self.s3.objects["people.json"] = json.dumps(people).encode()
        self.assertEqual(lf._parse_unsub_token(self._test_send_token()),
                         (4242, "cgracia@rainmakersecurities.com"))

    def test_test_send_token_email_only_when_no_person(self):
        tok = self._test_send_token()
        self.assertEqual(lf._parse_unsub_token(tok), (None, "cgracia@rainmakersecurities.com"))
        self.pipe.calls.clear()
        resp = lf.lambda_handler(_event("GET", tok), None)
        self.assertEqual(resp["statusCode"], 200)
        listed = json.loads(self.s3.objects[lf.UNSUB_LIST_KEY])["people"]
        self.assertIn("email:cgracia@rainmakersecurities.com", listed)
        self.assertNotIn(str(lf.MAILER_PREVIEW_PID), listed)
        self.assertEqual(self.pipe.calls, [])  # no Pipeline write for email-only tokens
        pids, emails, _ = lf._mailer_suppressed(self.s3)
        self.assertIn("cgracia@rainmakersecurities.com", emails)

    def test_send_retries_queued_tag_writes(self):
        self.s3.objects[lf.UNSUB_PENDING_KEY] = json.dumps(
            {"people": {str(PID): {"email": "ann@example.com", "passes": 0}}}).encode()
        lf._handle_mailer_send({"subject": "s"})
        self.assertIn(TAG_ID, self.pipe.person["predefined_contacts_tag_ids"])
        self.assertEqual(json.loads(self.s3.objects[lf.UNSUB_PENDING_KEY])["people"], {})
        report = self.ses.simple[-1]["Message"]["Body"]["Text"]["Data"]
        self.assertIn("Pipeline tag retries: 1 tagged, 0 still queued", report)

    def test_admin_list_page_shows_skipped_count(self):
        resp = lf._render_mailer_page()
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("1 recipients &middot; 3 skipped (Unsub: Weekly)", resp["body"])
        self.assertNotIn("ann@example.com", resp["body"])

    def test_composer_preview_shows_skipped_count(self):
        resp = lf._render_mailer_composer(lf.MAILER_PREVIEW_PID)
        self.assertIn("1 recipients will be sent &middot; 3 skipped (Unsub: Weekly)", resp["body"])

    def _patch_news(self):
        content = {"intro": "hi", "items": [], "_search_id": "19530439"}
        for target, value in (
            ("lambda_function._load_news_mailer", lambda s3: content),
            ("lambda_function._news_send_content", lambda c: (c, 0)),
            ("lambda_function._render_news_email", lambda first, pid, c: "<p>news</p>"),
        ):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_news_send_skips_tagged_and_opted_out(self):
        self._patch_news()
        resp = lf._handle_news_send({"subject": "News", "search_id": "19530439"})
        body = json.loads(resp["body"])
        self.assertEqual(body["sent"], 1)
        self.assertEqual(body["unsubscribed"], 3)
        sent_to = [m["Destination"]["ToAddresses"] for m in self.ses.simple
                   if m["Source"] == lf.MAILER_FROM]
        self.assertEqual(sent_to, [["cat@example.com"]])
        report = self.ses.simple[-1]["Message"]["Body"]["Text"]["Data"]
        self.assertIn("Skipped (Unsub: Weekly): 3", report)

    def test_news_composer_shows_skipped_count(self):
        self._patch_news()
        resp = lf._render_news_composer(lf.MAILER_PREVIEW_PID)
        self.assertIn("1 recipients will be sent &middot; 3 skipped (Unsub: Weekly)", resp["body"])

    def test_tag_lookup_failure_falls_back_to_s3_list(self):
        self.pipe.tags = []
        pids, emails, tag_ok = lf._mailer_suppressed(self.s3)
        self.assertFalse(tag_ok)
        self.assertEqual(pids, {PID, 99})
        self.assertFalse([c for c in self.pipe.calls if c[0] == "POST"])  # send path never creates the tag


class UndoTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["BRIEF_PAGE_KEY"] = "k"
        self.addCleanup(os.environ.pop, "BRIEF_PAGE_KEY", None)
        self.pipe.person.update({"id": PID, "email": "cgracia@rainmakersecurities.com",
                                 "predefined_contacts_tag_ids": [11, TAG_ID]})
        self.s3.objects.update({
            "people.json": json.dumps({"people": [dict(self.pipe.person)]}).encode(),
            lf.UNSUB_LIST_KEY: json.dumps({"people": {
                str(PID): {"email": "someone@else.com"},
                "email:cgracia@rainmakersecurities.com": {"email": "cgracia@rainmakersecurities.com"},
                "77": {"email": "cgracia@rainmakersecurities.com"},
                "88": {"email": "keep@example.com"},
            }}).encode(),
            lf.UNSUB_PENDING_KEY: json.dumps({"people": {str(PID): {"email": "x"}}}).encode(),
        })

    def _undo(self, key="k"):
        return lf.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                  "queryStringParameters": {"view": "unsub_undo", "key": key, "pid": str(PID),
                                                            "email": "cgracia@rainmakersecurities.com"}}, None)

    def test_requires_admin_key(self):
        self.assertEqual(self._undo("wrong")["statusCode"], 403)
        self.assertIn("88", json.loads(self.s3.objects[lf.UNSUB_LIST_KEY])["people"])

    def test_undo_removes_entries_and_tag_and_reports_people(self):
        resp = self._undo()
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(list(json.loads(self.s3.objects[lf.UNSUB_LIST_KEY])["people"]), ["88"])
        self.assertEqual(json.loads(self.s3.objects[lf.UNSUB_PENDING_KEY])["people"], {})
        self.assertEqual(self.pipe.person["predefined_contacts_tag_ids"], [11])
        self.assertIn("Pipeline tag: removed from person " + str(PID), resp["body"])
        self.assertIn("(people.json cache): " + str(PID), resp["body"])
        self.assertIn("(live API, status 200): " + str(PID), resp["body"])

    def test_undo_when_not_tagged(self):
        self.pipe.person["predefined_contacts_tag_ids"] = [11]
        resp = self._undo()
        self.assertIn("was not tagged", resp["body"])
        self.assertFalse([c for c in self.pipe.calls if c[0] == "PUT"])


if __name__ == "__main__":
    unittest.main()
