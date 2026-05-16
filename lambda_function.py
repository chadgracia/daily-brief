import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

import boto3

S3_BUCKET = "full-pipeline-cache"
S3_REGION = "us-east-1"
SES_REGION = "us-east-1"

FROM_ADDR = "agent@agent.graciagroup.com"
TO_ADDRS = ["cgracia@rainmakersecurities.com", "kate@graciagroup.com"]

PIPELINE_DEAL_URL = "https://app.pipelinecrm.com/deals/{}"
PIPELINE_PERSON_URL = "https://app.pipelinecrm.com/people/{}"
CONFIRM_URL = "https://s5qv2qkmjt2qejliwchvqukseq0wgwff.lambda-url.us-east-1.on.aws"
CONFIRM_SECRET = b"trade-update"

CF_DEAL_SIDE = "custom_label_1958"
CF_GROSS = "custom_label_3064339"
CF_NET = "custom_label_3064369"
CF_STRUCTURE = "custom_label_3064360"
CF_TICKET_MIN = "custom_label_3065488"
CF_TICKET_MAX = "custom_label_3064645"
CF_MKT_ASK = "custom_label_3997297"
CF_MKT_BID = "custom_label_3997298"
CF_IQF = "custom_label_3763008"
CF_NEWSLETTER = "custom_label_3775335"
CF_EMAIL_STATUS = "custom_label_2447206"
CF_PERSON_BUY_INTERESTS = "custom_label_3322093"
CF_PERSON_SELL_INTERESTS = "custom_label_3759156"

OPT_SELL = 5011675
OPT_BUY = 5077819
OPT_STRUCT_DIRECT = 6250090
OPT_STRUCT_FUND = 5077906
OPT_IQF_YES = 6496840
OPT_IQF_PENDING = 6496842
OPT_IQF_NO = 6496841

NEWSLETTER_OPTS = {6613674, 6613673, 6582981}

EMAIL_STATUS_REASONS = {
    3940558: "Bouncing",
    4943722: "Blocked",
    3940678: "Needed",
}

TAG_WHITELIST_CONTACT_ESTABLISHED = 3280123
REASON_NO_WORK_EMAIL = "No work email"

STRUCTURE_LABELS = {
    OPT_STRUCT_DIRECT: "Direct",
    OPT_STRUCT_FUND: "Fund",
}

IQF_LABELS = {
    OPT_IQF_YES: "✓",
    OPT_IQF_PENDING: "…",
    OPT_IQF_NO: "✗",
}

STAGE_FIRM = 111800
STAGE_MATCHED = 2381534
STAGE_INQUIRY = 2109142
STAGE_HOLD = 2094373
STAGE_CONFIRM = 2388323
STAGE_LOI_SIGNED = 2517909
STAGE_TRANSFER_NOTICE = 2533998
STAGE_SPA_SIGNED = 2381535

ACTIVE_STAGES = {STAGE_FIRM, STAGE_MATCHED, STAGE_INQUIRY, STAGE_HOLD, STAGE_CONFIRM}
MARKET_STAGES = ACTIVE_STAGES - {STAGE_HOLD}

STAGE_LABELS = {
    STAGE_FIRM: "FIRM",
    STAGE_MATCHED: "MATCHED",
    STAGE_INQUIRY: "INQUIRY",
    STAGE_HOLD: "HOLD",
    STAGE_CONFIRM: "CONFIRM",
    STAGE_LOI_SIGNED: "LOI SIGNED",
    STAGE_TRANSFER_NOTICE: "TRANSFER NOTICE",
    STAGE_SPA_SIGNED: "SPA SIGNED",
}

TIGHT_PCT = 0.10
TICKET_TOLERANCE = 0.10

TO_CLOSE_ALL_STAGES = {STAGE_MATCHED, STAGE_LOI_SIGNED}
TO_CLOSE_AGED_STAGE = STAGE_TRANSFER_NOTICE
TO_CLOSE_AGE_DAYS = 30

NEG_DISTANCE_BG = "#d4edda"
SECTION_GAP_HTML = '<div style="height: 24px;"></div>'


def _cf(record, key):
    cf = record.get("custom_fields") or {}
    return cf.get(key)


def _cf_option_id(record, key):
    v = _cf(record, key)
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("option_id") or v.get("id") or v.get("value")
    elif isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict):
            v = first.get("option_id") or first.get("id")
        else:
            v = first
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _cf_option_ids(record, key):
    v = _cf(record, key)
    if v is None:
        return set()
    items = v if isinstance(v, list) else [v]
    out = set()
    for item in items:
        if isinstance(item, dict):
            raw = item.get("option_id") or item.get("id") or item.get("value")
        else:
            raw = item
        if raw is None:
            continue
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _cf_number(record, key):
    v = _cf(record, key)
    if isinstance(v, dict):
        v = v.get("value") or v.get("amount") or v.get("number")
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_dt(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = str(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _days_since(dt, now):
    if not dt:
        return None
    return (now - dt).days


def _deal_side(deal):
    opt = _cf_option_id(deal, CF_DEAL_SIDE)
    if opt == OPT_BUY:
        return "BUY"
    if opt == OPT_SELL:
        return "SELL"
    return None


def _deal_type(deal):
    dt = deal.get("deal_type")
    if isinstance(dt, dict):
        dt = dt.get("name") or dt.get("type") or ""
    return str(dt).strip().lower() if dt else ""


def _buyer_seller_annotation(deal, person):
    cid = _company_id(deal)
    try:
        cid_int = int(cid) if cid is not None else None
    except (TypeError, ValueError):
        cid_int = None
    if person and cid_int is not None:
        if cid_int in _cf_option_ids(person, CF_PERSON_BUY_INTERESTS):
            return " (buyer)"
        if cid_int in _cf_option_ids(person, CF_PERSON_SELL_INTERESTS):
            return " (seller)"
    if _deal_type(deal) == "sell":
        return " (seller)"
    return ""


def _deal_structure_label(deal):
    return STRUCTURE_LABELS.get(_cf_option_id(deal, CF_STRUCTURE), "")


def _stage_id(deal):
    stage = deal.get("deal_stage") or {}
    sid = stage.get("id") if isinstance(stage, dict) else None
    if sid is None:
        sid = deal.get("deal_stage_id")
    try:
        return int(sid) if sid is not None else None
    except (TypeError, ValueError):
        return None


def _company_id(deal):
    co = deal.get("company") or {}
    if isinstance(co, dict):
        return co.get("id") or deal.get("company_id")
    return deal.get("company_id")


def _company_name(deal):
    co = deal.get("company") or {}
    if isinstance(co, dict):
        return co.get("name") or deal.get("company_name") or "(unknown)"
    return deal.get("company_name") or "(unknown)"


def _person_name_email(p):
    if not isinstance(p, dict):
        return "", ""
    name = p.get("name") or " ".join(
        x for x in (p.get("first_name"), p.get("last_name")) if x
    ).strip()
    email = p.get("email")
    if not email:
        emails = p.get("emails") or []
        if isinstance(emails, list) and emails:
            first = emails[0]
            email = first.get("address") if isinstance(first, dict) else first
    return name or "", email or ""


def _person_first_name(p):
    if not isinstance(p, dict):
        return ""
    fn = p.get("first_name")
    if fn:
        return fn
    name = p.get("name") or ""
    return name.split()[0] if name else ""


def _person_iqf(p):
    return IQF_LABELS.get(_cf_option_id(p, CF_IQF), "")


def _person_email(p):
    if not isinstance(p, dict):
        return ""
    email = p.get("email")
    if not email:
        emails = p.get("emails") or []
        if isinstance(emails, list) and emails:
            first = emails[0]
            email = first.get("address") if isinstance(first, dict) else first
    if not isinstance(email, str):
        return ""
    return email.strip()


def _person_full_name(p):
    if not isinstance(p, dict):
        return ""
    n = p.get("full_name") or p.get("name")
    if n:
        return n
    parts = [p.get("first_name"), p.get("last_name")]
    return " ".join(x for x in parts if x).strip()


def _has_tag(person, tag_id):
    tags = person.get("predefined_contacts_tag_ids") or []
    if not isinstance(tags, list):
        return False
    for t in tags:
        try:
            if int(t) == tag_id:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _primary_contact(deal):
    pc = deal.get("primary_contact") or deal.get("person") or {}
    if isinstance(pc, list):
        pc = pc[0] if pc else {}
    return pc if isinstance(pc, dict) else {}


def _deal_people(deal, people_by_id):
    out = []
    seen = set()
    raw = deal.get("people") or []
    if isinstance(raw, list) and raw:
        for p in raw:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            full = people_by_id.get(pid) if pid is not None else None
            chosen = full or p
            key = pid if pid is not None else id(chosen)
            if key in seen:
                continue
            seen.add(key)
            out.append(chosen)
    else:
        for pid in (deal.get("person_ids") or []):
            full = people_by_id.get(pid)
            if not full or pid in seen:
                continue
            seen.add(pid)
            out.append(full)
    return out


def _fetch_json(s3, key):
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def _fmt_price(v):
    if v is None:
        return ""
    return f"${v:,.2f}"


def _deal_title(d):
    return d.get("name") or d.get("title") or f"Deal {d.get('id', '')}"


def _deal_link(deal):
    did = deal.get("id")
    if did in (None, ""):
        return ""
    s = str(did)
    return f'<a href="{PIPELINE_DEAL_URL.format(escape(s, quote=True))}">{escape(s)}</a>'


def _make_token(deal_id):
    msg = str(deal_id).encode()
    sig = hmac.new(CONFIRM_SECRET, msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _confirm_email_link(deal, company, side):
    did = deal.get("id")
    if did in (None, ""):
        return ""
    pc = _primary_contact(deal)
    _, email = _person_name_email(pc)
    if not email:
        return ""
    first_name = _person_first_name(pc)
    token = _make_token(did)
    url = f"{CONFIRM_URL}?deal_id={quote(str(did))}&token={quote(token)}"
    subject = (
        f"Confirming your {company} {side} order"
        if side else f"Confirming your {company} order"
    )
    body = (
        f"Hello {first_name},\n\n"
        "Can you let me know if this deal is still valid? If so, we may "
        "have a match. Please confirm the terms here:\n\n"
        f"{url}\n\n"
        "Thanks,\n"
        "Chad"
    )
    href = (
        f"mailto:{quote(email, safe='@')}"
        f"?subject={quote(subject)}&body={quote(body)}"
    )
    return f'<a href="{escape(href, quote=True)}" title="Send confirmation email">✉</a>'


def _deal_id_cell(deal, company, side):
    link = _deal_link(deal)
    confirm = _confirm_email_link(deal, company, side)
    if confirm:
        return f"{link} {confirm}" if link else confirm
    return link


def _contact_cell(name, email, company=None, side=None):
    if not email:
        return escape(name) if name else ""
    label = f"{name} <{email}>" if name else f"<{email}>"
    if company and side:
        subject = f"Re: Your {company} {side} order"
    elif company:
        subject = f"Re: Your {company} order"
    else:
        subject = ""
    href = f"mailto:{quote(email, safe='@')}"
    if subject:
        href += f"?subject={quote(subject)}"
    return f'<a href="{escape(href, quote=True)}">{escape(label)}</a>'


def _people_cells(people, company, side=None):
    contacts = []
    iqfs = []
    for p in people:
        n, e = _person_name_email(p)
        if not n and not e:
            continue
        contacts.append(_contact_cell(n, e, company, side))
        iqfs.append(escape(_person_iqf(p)))
    contact_html = "<br>".join(contacts) if contacts else ""
    iqf_html = "<br>".join(iqfs) if iqfs else ""
    return contact_html, iqf_html


def _ticket_compat(buy, sell):
    buy_min = _cf_number(buy, CF_TICKET_MIN) or 0
    sell_min = _cf_number(sell, CF_TICKET_MIN) or 0
    bm = _cf_number(buy, CF_TICKET_MAX)
    sm = _cf_number(sell, CF_TICKET_MAX)
    buy_max = bm if bm is not None else float("inf")
    sell_max = sm if sm is not None else float("inf")
    lo = max(buy_min, sell_min)
    hi = min(buy_max, sell_max)
    if lo <= hi:
        return True
    gap = lo - hi
    max_size = max(buy_max, sell_max)
    if max_size == float("inf"):
        return True
    if max_size == 0:
        return False
    return (gap / max_size) <= TICKET_TOLERANCE


def _build_crossed(deals):
    by_co = {}
    for d in deals:
        if _stage_id(d) != STAGE_FIRM:
            continue
        side = _deal_side(d)
        if side not in ("BUY", "SELL"):
            continue
        cid = _company_id(d)
        if cid is None:
            continue
        bucket = by_co.setdefault(cid, {"name": _company_name(d), "buys": [], "sells": []})
        if side == "BUY":
            g = _cf_number(d, CF_GROSS)
            if g is not None:
                bucket["buys"].append((g, d))
        else:
            n = _cf_number(d, CF_NET)
            if n is not None:
                bucket["sells"].append((n, d))

    rows = []
    for co in by_co.values():
        best = None
        for g, buy in co["buys"]:
            buy_struct = _cf_option_id(buy, CF_STRUCTURE)
            if buy_struct not in STRUCTURE_LABELS:
                continue
            for n, sell in co["sells"]:
                if g < n or n == 0:
                    continue
                sell_struct = _cf_option_id(sell, CF_STRUCTURE)
                if sell_struct != buy_struct:
                    continue
                if not _ticket_compat(buy, sell):
                    continue
                pct = (g - n) / n * 100
                if best is None or pct > best["pct"]:
                    best = {
                        "company": co["name"],
                        "structure": STRUCTURE_LABELS[buy_struct],
                        "buy_price": g,
                        "buy_deal": buy,
                        "sell_price": n,
                        "sell_deal": sell,
                        "pct": pct,
                    }
        if best:
            rows.append(best)
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows


def _build_tight(deals, companies):
    co_by_id = {c.get("id"): c for c in companies if c.get("id") is not None}
    rows = []
    for d in deals:
        sid = _stage_id(d)
        if sid not in MARKET_STAGES:
            continue
        side = _deal_side(d)
        cid = _company_id(d)
        co = co_by_id.get(cid)
        if not co:
            continue
        co_name = co.get("name") or _company_name(d)
        stage_label = STAGE_LABELS.get(sid, str(sid))
        structure = _deal_structure_label(d)
        if side == "BUY":
            ask = _cf_number(co, CF_MKT_ASK)
            gross = _cf_number(d, CF_GROSS)
            if not ask or gross is None:
                continue
            dist = (ask - gross) / ask
            if abs(dist) <= TIGHT_PCT:
                rows.append({
                    "company": co_name,
                    "side": "BUY",
                    "stage": stage_label,
                    "structure": structure,
                    "your_price": gross,
                    "marketplace_price": ask,
                    "distance": dist,
                    "deal": d,
                })
        elif side == "SELL":
            bid = _cf_number(co, CF_MKT_BID)
            net = _cf_number(d, CF_NET)
            if not bid or net is None:
                continue
            dist = (net - bid) / bid
            if abs(dist) <= TIGHT_PCT:
                rows.append({
                    "company": co_name,
                    "side": "SELL",
                    "stage": stage_label,
                    "structure": structure,
                    "your_price": net,
                    "marketplace_price": bid,
                    "distance": dist,
                    "deal": d,
                })
    rows.sort(key=lambda r: (0 if r["side"] == "SELL" else 1, r["distance"]))
    return rows


def _build_to_close(deals, now, people_by_id):
    rows = []
    for d in deals:
        sid = _stage_id(d)
        if sid not in TO_CLOSE_ALL_STAGES and sid != TO_CLOSE_AGED_STAGE:
            continue
        days = _days_since(_parse_dt(d.get("updated_at")), now)
        days = days if days is not None else 0
        if sid == TO_CLOSE_AGED_STAGE and days < TO_CLOSE_AGE_DAYS:
            continue
        rows.append({
            "stage": STAGE_LABELS.get(sid, str(sid)),
            "title": _deal_title(d),
            "company": _company_name(d),
            "people": _deal_people(d, people_by_id),
            "days": days,
            "deal": d,
        })
    rows.sort(key=lambda r: r["days"], reverse=True)
    return rows


def _build_to_invoice(deals, now, people_by_id):
    rows = []
    for d in deals:
        if _stage_id(d) != STAGE_SPA_SIGNED:
            continue
        days = _days_since(_parse_dt(d.get("updated_at")), now)
        rows.append({
            "title": _deal_title(d),
            "company": _company_name(d),
            "people": _deal_people(d, people_by_id),
            "days": days if days is not None else 0,
            "deal": d,
        })
    rows.sort(key=lambda r: r["days"], reverse=True)
    return rows


def _count_newsletter_recipients(people):
    n = 0
    for p in people:
        if not (_cf_option_ids(p, CF_NEWSLETTER) & NEWSLETTER_OPTS):
            continue
        if not _person_email(p):
            continue
        n += 1
    return n


def _build_leads_to_revive(people, companies):
    co_by_id = {c.get("id"): c for c in companies if c.get("id") is not None}
    rows_by_pid = {}
    for p in people:
        pid = p.get("id")
        if pid is None:
            continue

        email = _person_email(p)
        bucket1 = (not email) and _has_tag(p, TAG_WHITELIST_CONTACT_ESTABLISHED)

        bucket2_reason = None
        for opt in _cf_option_ids(p, CF_EMAIL_STATUS):
            if opt in EMAIL_STATUS_REASONS:
                bucket2_reason = EMAIL_STATUS_REASONS[opt]
                break

        if not bucket1 and not bucket2_reason:
            continue

        reason = REASON_NO_WORK_EMAIL if bucket1 else bucket2_reason

        co = co_by_id.get(p.get("company_id")) or {}
        co_name = co.get("name") or ""
        co_website = co.get("website") or ""

        rows_by_pid[pid] = {
            "reason": reason,
            "name": _person_full_name(p),
            "iqf": _person_iqf(p),
            "person_id": pid,
            "linked_in_url": p.get("linked_in_url") or "",
            "company_name": co_name,
            "company_website": co_website,
        }

    rows = list(rows_by_pid.values())
    rows.sort(key=lambda r: (r["reason"], r["name"].lower()))
    return rows


def _render_html(crossed, tight, to_close, to_invoice, leads,
                 newsletter_recipient_count, leads_to_revive_count, date_str):
    out = [f"<html><body><h1>Daily Brief -- {escape(date_str)}</h1>"]

    out.append("<h2>A. INVOICE: Get paid and win deal</h2>")
    if not to_invoice:
        out.append("<p>(No SPA-signed deals awaiting invoice.)</p>")
    else:
        out.append("<table border='1' cellpadding='4' cellspacing='0'>")
        out.append("<tr><th>Company</th><th>Deal Title</th><th>Contact</th>"
                   "<th>IQF</th><th>Days Since Update</th><th>Deal ID</th></tr>")
        for r in to_invoice:
            side = _deal_side(r["deal"])
            contact_html, iqf_html = _people_cells(r["people"], r["company"], side)
            out.append(
                "<tr>"
                f"<td>{escape(r['company'])}</td>"
                f"<td>{escape(r['title'])}</td>"
                f"<td>{contact_html}</td>"
                f"<td>{iqf_html}</td>"
                f"<td>{r['days']}</td>"
                f"<td>{_deal_link(r['deal'])}</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append("<h2>B. CLOSE: Move toward finish line</h2>")
    if not to_close:
        out.append("<p>(Nothing to close.)</p>")
    else:
        out.append("<table border='1' cellpadding='4' cellspacing='0'>")
        out.append("<tr><th>Stage</th><th>Company</th><th>Deal Title</th>"
                   "<th>Contact</th><th>IQF</th><th>Days Since Update</th>"
                   "<th>Deal ID</th></tr>")
        for r in to_close:
            contact_html, iqf_html = _people_cells(r["people"], r["company"])
            side = _deal_side(r["deal"])
            out.append(
                "<tr>"
                f"<td>{escape(r['stage'])}</td>"
                f"<td>{escape(r['company'])}</td>"
                f"<td>{escape(r['title'])}</td>"
                f"<td>{contact_html}</td>"
                f"<td>{iqf_html}</td>"
                f"<td>{r['days']}</td>"
                f"<td>{_deal_id_cell(r['deal'], r['company'], side)}</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append("<h2>C. INTRODUCE: Crossed trades</h2>")
    if not crossed:
        out.append("<p>(None)</p>")
    else:
        out.append("<table border='1' cellpadding='4' cellspacing='0'>")
        out.append("<tr><th>Company</th><th>Structure</th>"
                   "<th>Buy</th><th>Buy Contact</th><th>Buy Deal ID</th>"
                   "<th>Sell</th><th>Sell Contact</th><th>Sell Deal ID</th>"
                   "<th>% Diff</th></tr>")
        for r in crossed:
            b_pc = _primary_contact(r["buy_deal"])
            s_pc = _primary_contact(r["sell_deal"])
            bn, be = _person_name_email(b_pc)
            sn, se = _person_name_email(s_pc)
            b_iqf = _person_iqf(b_pc)
            s_iqf = _person_iqf(s_pc)
            buy_contact = _contact_cell(bn, be, r['company'], 'BUY')
            if b_iqf:
                buy_contact = f"{buy_contact} {escape(b_iqf)}"
            sell_contact = _contact_cell(sn, se, r['company'], 'SELL')
            if s_iqf:
                sell_contact = f"{sell_contact} {escape(s_iqf)}"
            out.append(
                "<tr>"
                f"<td>{escape(r['company'])}</td>"
                f"<td>{escape(r['structure'])}</td>"
                f"<td>{escape(_fmt_price(r['buy_price']))}</td>"
                f"<td>{buy_contact}</td>"
                f"<td>{_deal_id_cell(r['buy_deal'], r['company'], 'BUY')}</td>"
                f"<td>{escape(_fmt_price(r['sell_price']))}</td>"
                f"<td>{sell_contact}</td>"
                f"<td>{_deal_id_cell(r['sell_deal'], r['company'], 'SELL')}</td>"
                f"<td>{r['pct']:+.2f}%</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append("<h2>D. EXPLORE: Update or engage close matches</h2>")
    tight_groups = [
        ("SELL", [r for r in tight if r["side"] == "SELL"]),
        ("BUY", [r for r in tight if r["side"] == "BUY"]),
    ]
    if not tight:
        out.append("<p>(None)</p>")
    else:
        for idx, (_, group) in enumerate(tight_groups):
            if not group:
                continue
            if idx > 0:
                out.append(SECTION_GAP_HTML)
            out.append("<table border='1' cellpadding='4' cellspacing='0'>")
            out.append("<tr><th>Company</th><th>Side</th><th>Stage</th>"
                       "<th>Structure</th><th>Your Price</th><th>Marketplace</th>"
                       "<th>% Distance</th><th>Contact</th><th>IQF</th>"
                       "<th>Deal ID</th></tr>")
            for r in group:
                pc = _primary_contact(r["deal"])
                n, e = _person_name_email(pc)
                iqf_sym = _person_iqf(pc)
                contact_html = _contact_cell(n, e, r['company'], r['side'])
                annotation = _buyer_seller_annotation(r["deal"], pc)
                if annotation:
                    contact_html = f"{contact_html}{escape(annotation)}"
                dist_style = (
                    f' style="background-color:{NEG_DISTANCE_BG}"'
                    if r["distance"] < 0 else ""
                )
                out.append(
                    "<tr>"
                    f"<td>{escape(r['company'])}</td>"
                    f"<td>{escape(r['side'])}</td>"
                    f"<td>{escape(r['stage'])}</td>"
                    f"<td>{escape(r['structure'])}</td>"
                    f"<td>{escape(_fmt_price(r['your_price']))}</td>"
                    f"<td>{escape(_fmt_price(r['marketplace_price']))}</td>"
                    f"<td{dist_style}>{r['distance'] * 100:+.2f}%</td>"
                    f"<td>{contact_html}</td>"
                    f"<td>{escape(iqf_sym)}</td>"
                    f"<td>{_deal_id_cell(r['deal'], r['company'], r['side'])}</td>"
                    "</tr>"
                )
            out.append("</table>")

    out.append("<h2>F. Leads to Revive</h2>")
    out.append(f"<p>Total Newsletter Recipients: {newsletter_recipient_count}</p>")
    out.append(f"<p>Leads to Revive: {leads_to_revive_count}</p>")
    if not leads:
        out.append("<p>(No leads to revive — clean book!)</p>")
    else:
        out.append("<table border='1' cellpadding='4' cellspacing='0'>")
        out.append("<tr><th>Reason</th><th>Name</th><th>IQF</th>"
                   "<th>Pipeline</th><th>LinkedIn</th><th>Company</th></tr>")
        for r in leads:
            person_href = PIPELINE_PERSON_URL.format(
                escape(str(r["person_id"]), quote=True)
            )
            pipeline_cell = f'<a href="{person_href}">open</a>'
            if r["linked_in_url"]:
                li_cell = (
                    f'<a href="{escape(r["linked_in_url"], quote=True)}">'
                    'LinkedIn</a>'
                )
            else:
                li_cell = ""
            if r["company_website"] and r["company_name"]:
                co_cell = (
                    f'<a href="{escape(r["company_website"], quote=True)}">'
                    f'{escape(r["company_name"])}</a>'
                )
            elif r["company_name"]:
                co_cell = escape(r["company_name"])
            else:
                co_cell = ""
            out.append(
                "<tr>"
                f"<td>{escape(r['reason'])}</td>"
                f"<td>{escape(r['name'])}</td>"
                f"<td>{escape(r['iqf'])}</td>"
                f"<td>{pipeline_cell}</td>"
                f"<td>{li_cell}</td>"
                f"<td>{co_cell}</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append("</body></html>")
    return "".join(out)


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    date_str = f"{now:%B} {now.day}, {now.year}"

    s3 = boto3.client("s3", region_name=S3_REGION)
    deals_doc = _fetch_json(s3, "deals.json")
    companies_doc = _fetch_json(s3, "companies.json")
    people_doc = _fetch_json(s3, "people.json")
    deals = deals_doc.get("deals", []) or []
    companies = companies_doc.get("companies", []) or []
    people = people_doc.get("people", []) or []
    people_by_id = {p.get("id"): p for p in people if p.get("id") is not None}

    crossed = _build_crossed(deals)
    tight = _build_tight(deals, companies)
    to_close = _build_to_close(deals, now, people_by_id)
    to_invoice = _build_to_invoice(deals, now, people_by_id)
    leads = _build_leads_to_revive(people, companies)
    leads_to_revive_count = len(leads)
    newsletter_recipient_count = _count_newsletter_recipients(people)

    body_html = _render_html(
        crossed, tight, to_close, to_invoice, leads,
        newsletter_recipient_count, leads_to_revive_count, date_str,
    )
    subject = f"Daily Brief -- {date_str}"

    ses = boto3.client("ses", region_name=SES_REGION)
    resp = ses.send_email(
        Source=FROM_ADDR,
        Destination={"ToAddresses": TO_ADDRS},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": body_html, "Charset": "UTF-8"}},
        },
    )

    return {
        "statusCode": 200,
        "message_id": resp.get("MessageId"),
        "counts": {
            "crossed": len(crossed),
            "tight": len(tight),
            "to_close": len(to_close),
            "to_invoice": len(to_invoice),
            "leads_to_revive": leads_to_revive_count,
            "newsletter_recipients": newsletter_recipient_count,
            "deals": len(deals),
            "companies": len(companies),
            "people": len(people),
        },
    }
