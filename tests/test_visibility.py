import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import FakeS3, FakeSES, lf  # noqa: E402

BUCKET = "full-pipeline-cache"
VF = lf.VISIBILITY_FIELD


def _deal(did, vis=None, side=None, name=None):
    cf = {lf.CF_DEAL_SIDE: side or lf.OPT_SELL}
    if vis is not None:
        cf[VF] = vis
    return {"id": did, "name": name or "Deal %d" % did, "company": {"name": name or "Co%d" % did},
            "deal_stage": {"id": lf.STAGE_FIRM}, "custom_fields": cf}


DEALS = [_deal(1), _deal(2, lf.VIS_ALL), _deal(3, lf.VIS_INTERESTED), _deal(4, [lf.VIS_PRIVATE]),
         _deal(5, "7223458.0"), _deal(6, "")]


def _s3(deals=DEALS, overrides=None, extra=None):
    objs = {(BUCKET, "deals.json"): json.dumps({"deals": deals}).encode()}
    if overrides is not None:
        objs[(BUCKET, "visibility-overrides.json")] = (
            overrides if isinstance(overrides, bytes) else json.dumps(overrides).encode())
    objs.update(extra or {})
    return FakeS3(objs)


class Base(unittest.TestCase):
    def setUp(self):
        lf._vis_overrides_cache.update({"at": 0.0, "data": None})
        lf._BRIEF_VIS_MAP = None
        p = mock.patch("builtins.print")
        self.log = p.start()
        self.addCleanup(p.stop)


class Normalise(Base):
    def test_values(self):
        n = lf._norm_visibility
        for v in (None, "", "abc", [], True, 123, lf.VIS_ALL, "7223456", {"x": 1}):
            self.assertEqual(n(v), lf.VIS_ALL, v)
        for v in (lf.VIS_PRIVATE, "7223458", "7223458.0", " 7223458 ", [7223458], ["7223458"], 7223458.0):
            self.assertEqual(n(v), lf.VIS_PRIVATE, v)
        self.assertEqual(n([lf.VIS_INTERESTED]), lf.VIS_INTERESTED)


class Map(Base):
    def test_from_custom_fields_without_overrides_file(self):
        m = lf._visibility_map(DEALS, _s3())
        self.assertEqual(m, {"1": lf.VIS_ALL, "2": lf.VIS_ALL, "3": lf.VIS_INTERESTED,
                             "4": lf.VIS_PRIVATE, "5": lf.VIS_PRIVATE, "6": lf.VIS_ALL})
        self.log.assert_not_called()

    def test_override_wins(self):
        ov = {"1": {"v": lf.VIS_PRIVATE, "at": "2026-11-14T00:00:00Z"},
              "4": {"v": lf.VIS_ALL, "at": "2026-11-14T00:00:00Z"},
              "99": {"v": "7223457", "at": "2026-11-14T00:00:00Z"}}
        m = lf._visibility_map(DEALS, _s3(overrides=ov))
        self.assertEqual(m["1"], lf.VIS_PRIVATE)
        self.assertEqual(m["4"], lf.VIS_ALL)
        self.assertEqual(m["99"], lf.VIS_INTERESTED)

    def test_overrides_cached_60s(self):
        s3 = _s3(overrides={"1": {"v": lf.VIS_PRIVATE}})
        lf._visibility_map(DEALS, s3)
        s3.objects[(BUCKET, "visibility-overrides.json")] = b"{}"
        self.assertEqual(lf._visibility_map(DEALS, s3)["1"], lf.VIS_PRIVATE)
        lf._vis_overrides_cache["at"] -= 61
        self.assertEqual(lf._visibility_map(DEALS, s3)["1"], lf.VIS_ALL)

    def test_malformed_overrides_logs_and_uses_deals(self):
        m = lf._visibility_map(DEALS, _s3(overrides=b"not json"))
        self.assertEqual(m["4"], lf.VIS_PRIVATE)
        self.assertEqual(m["1"], lf.VIS_ALL)
        self.assertEqual(self.log.call_count, 1)
        self.assertIn("visibility:", self.log.call_args.args[0])


class WeeklyMailer(Base):
    def test_eligible_drops_interested_and_private(self):
        sells, _ = lf._mailer_eligible(DEALS, {}, _s3())
        self.assertEqual(sorted(d["id"] for d in sells), [1, 2, 6])

    def test_eligible_override_wins(self):
        ov = {"1": {"v": lf.VIS_INTERESTED}, "3": {"v": lf.VIS_ALL}}
        sells, _ = lf._mailer_eligible(DEALS, {}, _s3(overrides=ov))
        self.assertEqual(sorted(d["id"] for d in sells), [2, 3, 6])

    def test_eligible_buy_side_filtered(self):
        deals = [_deal(7, side=lf.OPT_BUY), _deal(8, lf.VIS_PRIVATE, side=lf.OPT_BUY)]
        _, buys = lf._mailer_eligible(deals, {}, _s3(deals))
        self.assertEqual([d["id"] for d in buys], [7])

    def test_eligible_without_s3_uses_custom_fields(self):
        sells, _ = lf._mailer_eligible(DEALS, {})
        self.assertEqual(sorted(d["id"] for d in sells), [1, 2, 6])

    def _run_test_send(self, selected):
        s3 = _s3(extra={(BUCKET, "mailer-selection.json"): json.dumps({"deal_ids": selected}).encode()})
        ses = FakeSES()
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "_mailer_buyer_counts", return_value={}), \
                mock.patch.object(lf, "_mailer_buy_companies", return_value=[]), \
                mock.patch.object(lf, "_save_weekly_note"), \
                mock.patch.object(lf, "_merge_send_times"), \
                mock.patch.object(lf, "MAILER_SELECTION_KEY", "mailer-selection.json"), \
                mock.patch.object(lf, "_render_mailer_email", return_value="<p>x</p>") as rm:
            res = lf._handle_mailer_test({"subject": "S"})
        return res, ses, rm

    def test_saved_selection_cannot_carry_private_through(self):
        res, ses, rm = self._run_test_send(["1", "3", "4", "5"])
        self.assertEqual(json.loads(res["body"])["sells"], 1)
        self.assertEqual([d["id"] for d in rm.call_args.args[2]], [1])
        self.assertEqual(len(ses.sent), 1)  # fake SES only; nothing real is sent

    def test_composer_does_not_offer_hidden_deals(self):
        s3 = _s3()
        with mock.patch.object(lf.boto3, "client", return_value=s3), \
                mock.patch.object(lf, "_mailer_buyer_counts", return_value={}), \
                mock.patch.object(lf, "_mailer_buy_companies", return_value=[]), \
                mock.patch.object(lf, "_load_mailer_selection", return_value=set()), \
                mock.patch.object(lf, "_send_lock_status_html", return_value=""), \
                mock.patch.object(lf, "_render_mailer_email", return_value=""):
            html = lf._render_mailer_composer(1)["body"]
        for did in ("1", "2", "6"):
            self.assertIn('name="deal" value="%s"' % did, html)
        for did in ("3", "4", "5"):
            self.assertNotIn('name="deal" value="%s"' % did, html)


NEWS = {"intro": "hi", "items": [{"deal_id": "1", "company": "Co1"}, {"deal_id": 4, "company": "Co4"},
                                  {"email_co": "Acme", "company": "Acme"}]}


class NewsMailer(Base):
    def test_blocks(self):
        self.assertEqual(lf._news_visibility_blocks(_s3(), NEWS), [("4", "Co4", "Private")])
        ok = {"items": [{"deal_id": "1"}, {"deal_id": "2"}, {"deal_id": "6"}, {"deal_id": "404"}]}
        self.assertEqual(lf._news_visibility_blocks(_s3(), ok), [])
        inter = {"items": [{"deal_id": "3", "company": "Co3"}]}
        self.assertEqual(lf._news_visibility_blocks(_s3(), inter), [("3", "Co3", "Interested only")])

    def test_blocks_override_wins(self):
        ov = {"1": {"v": lf.VIS_INTERESTED}, "4": {"v": lf.VIS_ALL}}
        self.assertEqual(lf._news_visibility_blocks(_s3(overrides=ov), NEWS),
                         [("1", "Co1", "Interested only")])

    def test_lookup_failure_returns_none_and_logs(self):
        self.assertIsNone(lf._news_visibility_blocks(FakeS3(), NEWS))
        self.assertEqual(self.log.call_count, 1)
        self.assertIn("visibility: lookup failed", self.log.call_args.args[0])

    def _send(self, s3):
        ses = FakeSES()
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "_acquire_send_lock") as lock, \
                mock.patch.object(lf, "_fetch_mailer_rows", return_value=([], [])) as rows:
            lock.return_value = (None, {"statusCode": 409, "body": "{}"})
            res = lf._handle_news_send({"subject": "S"})
        return res, ses, lock, rows

    def test_send_refused_naming_deal(self):
        s3 = _s3(extra={(BUCKET, lf.NEWS_MAILER_KEY): json.dumps(NEWS).encode()})
        res, ses, lock, rows = self._send(s3)
        self.assertEqual(res["statusCode"], 400)
        err = json.loads(res["body"])["error"]
        self.assertIn("Co4 (deal 4) is Private", err)
        lock.assert_not_called()
        rows.assert_not_called()
        self.assertEqual(ses.sent, [])

    def test_send_allowed_when_all_visible(self):
        news = {"intro": "hi", "items": [{"deal_id": "1", "company": "Co1"}]}
        s3 = _s3(extra={(BUCKET, lf.NEWS_MAILER_KEY): json.dumps(news).encode(),
                        (BUCKET, lf.PD_ACCESS_KEY): json.dumps({"people": {}}).encode()})
        res, ses, lock, _ = self._send(s3)
        lock.assert_called_once()  # got past the visibility check

    def test_send_allowed_when_lookup_fails(self):
        s3 = FakeS3({(BUCKET, lf.NEWS_MAILER_KEY): json.dumps(NEWS).encode(),
                     (BUCKET, lf.PD_ACCESS_KEY): json.dumps({"people": {}}).encode()})
        res, ses, lock, _ = self._send(s3)
        lock.assert_called_once()
        self.assertTrue(any("visibility: lookup failed" in c.args[0] for c in self.log.call_args_list))

    def test_save_works_with_warning(self):
        s3 = _s3()
        with mock.patch.object(lf.boto3, "client", return_value=s3):
            res = lf._handle_news_save({"content": json.dumps(NEWS)})
        body = json.loads(res["body"])
        self.assertTrue(body["ok"])
        self.assertIn("Co4 (deal 4) is Private", body["warning"])
        self.assertIn((BUCKET, lf.NEWS_MAILER_KEY), s3.objects)
        ok = {"items": [{"deal_id": "2"}]}
        with mock.patch.object(lf.boto3, "client", return_value=s3):
            body = json.loads(lf._handle_news_save({"content": json.dumps(ok)})["body"])
        self.assertNotIn("warning", body)

    def test_test_send_works_with_warning(self):
        s3 = _s3(extra={(BUCKET, lf.NEWS_MAILER_KEY): json.dumps(NEWS).encode()})
        ses = FakeSES()
        with mock.patch.object(lf.boto3, "client", side_effect=lambda svc, **k: ses if svc == "ses" else s3), \
                mock.patch.object(lf, "_render_news_email", return_value="<p>x</p>"):
            res = lf._handle_news_test({"subject": "S"})
        body = json.loads(res["body"])
        self.assertTrue(body["ok"])
        self.assertIn("Co4 (deal 4) is Private", body["warning"])
        self.assertEqual(len(ses.sent), 1)
        self.assertEqual(ses.sent[0]["Destination"]["ToAddresses"], ["cgracia@rainmakersecurities.com"])

    def test_composer_shows_warning(self):
        s3 = _s3(extra={(BUCKET, lf.NEWS_MAILER_KEY): json.dumps(NEWS).encode()})
        with mock.patch.object(lf.boto3, "client", return_value=s3), \
                mock.patch.object(lf, "_send_lock_status_html", return_value=""):
            html = lf._render_news_composer(1)["body"]
        self.assertIn("Co4 (deal 4) is Private", html)


class BriefTag(Base):
    def test_tags(self):
        lf._BRIEF_VIS_MAP = lf._visibility_map(DEALS, _s3(overrides={"1": {"v": lf.VIS_INTERESTED}}))
        by = {d["id"]: d for d in DEALS}
        self.assertIn("Interested only</span>", lf._deal_title_link(by[1]))
        self.assertIn("Interested only</span>", lf._deal_link(by[3], interactive=True))
        self.assertIn("Private</span>", lf._deal_title_link(by[4], interactive=True))
        for did in (2, 6):
            self.assertNotIn("<span", lf._deal_title_link(by[did]))
            self.assertNotIn("<span", lf._deal_link(by[did]))

    def test_tag_without_map_uses_custom_fields(self):
        self.assertIn("Private</span>", lf._deal_link(_deal(9, lf.VIS_PRIVATE)))
        self.assertEqual(lf._vis_tag(_deal(9)), "")


if __name__ == "__main__":
    unittest.main()
