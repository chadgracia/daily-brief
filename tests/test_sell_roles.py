import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import lf  # noqa: E402

ZIPLINE = 777


def person(pid, email, company_id=None, buy=None, sell=None):
    cf = {}
    if buy is not None:
        cf[lf.CF_PERSON_BUY_INTERESTS] = buy
    if sell is not None:
        cf[lf.CF_PERSON_SELL_INTERESTS] = sell
    p = {"id": pid, "name": f"P{pid}", "email": email, "custom_fields": cf}
    if company_id is not None:
        p["company_id"] = company_id
    return p


def deal(side, people, primary=None):
    d = {"id": 49667534, "name": "Zipline: $3.5M Sell", "company": {"id": ZIPLINE, "name": "Zipline"},
         "custom_fields": {lf.CF_DEAL_SIDE: [side]} if side else {}, "people": people}
    if primary is not None:
        d["primary_contact_id"] = primary["id"]
        d["primary_contact"] = {"id": primary["id"], "name": primary["name"], "email": primary["email"]}
    return d


def roles(d, people):
    return [lf._buyer_seller_annotation(d, p) for p in people]


class SellRoles(unittest.TestCase):
    def setUp(self):
        lf._ROLE_PEOPLE_BY_ID = {}

    def test_a_primary_seller_other_company_buyer(self):
        amit = person(1, "amit@zipline.com", 10)
        mark = person(2, "mark@icebreaker.vc", 20)
        d = deal(lf.OPT_SELL, [amit, mark], primary=amit)
        self.assertEqual(roles(d, [amit, mark]), [" (s)", " (b)"])
        buyer, seller = lf._split_contacts_by_role([amit, mark], d)
        self.assertIs(buyer, mark)
        self.assertIs(seller, amit)
        # order on the deal doesn't matter
        buyer, seller = lf._split_contacts_by_role([mark, amit], d)
        self.assertIs(buyer, mark)
        self.assertIs(seller, amit)

    def test_b_same_company_id_both_sellers(self):
        a = person(1, "a@fundco.com", 10)
        b = person(2, "b@otherdomain.com", 10)
        d = deal(lf.OPT_SELL, [a, b], primary=a)
        self.assertEqual(roles(d, [a, b]), [" (s)", " (s)"])
        buyer, seller = lf._split_contacts_by_role([a, b], d)
        self.assertIsNone(buyer)
        self.assertIs(seller, a)

    def test_c_no_company_id_same_corporate_domain(self):
        a = person(1, "a@fundco.com", 10)
        b = person(2, "b@FundCo.com")
        d = deal(lf.OPT_SELL, [a, b], primary=a)
        self.assertEqual(roles(d, [a, b]), [" (s)", " (s)"])

    def test_c_primary_resolved_through_people_by_id(self):
        # Deal snapshot carries only id/name/email for the primary; people.json has the company_id.
        a_full = person(1, "a@fundco.com", 10)
        b = person(2, "b@elsewhere.com", 10)
        d = deal(lf.OPT_SELL, [{"id": 1}, {"id": 2}])
        d["primary_contact"] = {"id": 1, "name": "P1", "email": "a@fundco.com"}
        lf._ROLE_PEOPLE_BY_ID = {1: a_full, 2: b}
        self.assertEqual(lf._buyer_seller_annotation(d, b), " (s)")

    def test_d_both_gmail_second_is_buyer(self):
        a = person(1, "a@gmail.com")
        b = person(2, "b@gmail.com")
        d = deal(lf.OPT_SELL, [a, b], primary=a)
        self.assertEqual(roles(d, [a, b]), [" (s)", " (b)"])

    def test_e_staff_no_role(self):
        a = person(1, "a@zipline.com", 10)
        chad = person(3, "cgracia@RainmakerSecurities.com", 10)
        g = person(4, "x@graciagroup.com")
        d = deal(lf.OPT_SELL, [a, chad, g], primary=a)
        self.assertEqual(roles(d, [a, chad, g]), [" (s)", "", ""])
        self.assertIsNone(lf._person_role(d, chad))

    def test_f_primary_with_buy_interest_still_seller(self):
        a = person(1, "a@zipline.com", 10, buy=[ZIPLINE])
        b = person(2, "b@icebreaker.vc", 20)
        d = deal(lf.OPT_SELL, [a, b], primary=a)
        self.assertEqual(roles(d, [a, b]), [" (s)", " (b)"])

    def test_sell_deal_without_primary_keeps_legacy(self):
        a = person(1, "a@zipline.com", 10)
        b = person(2, "b@icebreaker.vc", 20, buy=[ZIPLINE])
        d = deal(lf.OPT_SELL, [a, b])
        self.assertEqual(roles(d, [a, b]), [" (s)", " (b)"])

    def test_deal_type_sell_string_counts_as_sell(self):
        a = person(1, "a@zipline.com", 10)
        b = person(2, "b@icebreaker.vc", 20)
        d = deal(None, [a, b], primary=a)
        d["deal_type"] = "Sell"
        self.assertEqual(roles(d, [a, b]), [" (s)", " (b)"])

    def test_g_buy_deal_unchanged(self):
        a = person(1, "a@zipline.com", 10)
        b = person(2, "b@icebreaker.vc", 20, buy=[ZIPLINE])
        c = person(3, "c@x.com", 30, sell=[ZIPLINE])
        chad = person(4, "cgracia@rainmakersecurities.com", sell=[ZIPLINE])
        d = deal(lf.OPT_BUY, [a, b, c, chad], primary=a)
        # Legacy rules: explicit interests only, no fallback (staff not special-cased).
        self.assertEqual(roles(d, [a, b, c, chad]), ["", " (b)", " (s)", " (s)"])
        nd = deal(None, [a, b, c], primary=a)
        self.assertEqual(roles(nd, [a, b, c]), ["", " (b)", " (s)"])


if __name__ == "__main__":
    unittest.main()
