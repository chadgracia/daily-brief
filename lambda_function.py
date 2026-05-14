import json
from datetime import datetime, timezone
from html import escape

import boto3

S3_BUCKET = "full-pipeline-cache"
S3_REGION = "us-east-1"
SES_REGION = "us-east-1"

FROM_ADDR = "agent@agent.graciagroup.com"
TO_ADDRS = ["cgracia@rainmakersecurities.com", "kate@graciagroup.com"]

CF_DEAL_SIDE = "custom_label_1958"
CF_GROSS = "custom_label_3064339"
CF_NET = "custom_label_3064369"
CF_REFRESH = "custom_label_3994687"
CF_HIIVE_ASK = "custom_label_3997297"
CF_HIIVE_BID = "custom_label_3997298"

OPT_SELL = 5011675
OPT_BUY = 5077819

STAGE_FIRM = 111800
STAGE_MATCHED = 2381534
STAGE_INQUIRY = 2109142
STAGE_HOLD = 2094373
STAGE_CONFIRM = 2388323
ACTIVE_STAGES = {STAGE_FIRM, STAGE_MATCHED, STAGE_INQUIRY, STAGE_HOLD, STAGE_CONFIRM}

STALE_THRESHOLD_DAYS = 50
TIGHT_PCT = 0.10


def _cf(record, key):
    cf = record.get("custom_fields") or {}
    return cf.get(key)


def _cf_option_id(record, key):
    v = _cf(record, key)
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get("option_id") or v.get("id") or v.get("value")
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict):
            return first.get("option_id") or first.get("id")
        return first
    return v


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


def _cf_date(record, key):
    v = _cf(record, key)
    if isinstance(v, dict):
        v = v.get("value") or v.get("date")
    return _parse_dt(v)


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
    if opt is None:
        return None
    try:
        opt = int(opt)
    except (TypeError, ValueError):
        return None
    if opt == OPT_BUY:
        return "BUY"
    if opt == OPT_SELL:
        return "SELL"
    return None


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


def _contact(deal):
    pc = deal.get("primary_contact") or deal.get("person") or {}
    if isinstance(pc, list):
        pc = pc[0] if pc else {}
    if not isinstance(pc, dict):
        return "", ""
    name = pc.get("name") or " ".join(
        p for p in (pc.get("first_name"), pc.get("last_name")) if p
    ).strip()
    email = pc.get("email")
    if not email:
        emails = pc.get("emails") or []
        if isinstance(emails, list) and emails:
            first = emails[0]
            email = first.get("address") if isinstance(first, dict) else first
    return name or "", email or ""


def _fetch_json(s3, key):
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def _fmt_price(v):
    if v is None:
        return ""
    return f"${v:,.2f}"


def _deal_title(d):
    return d.get("name") or d.get("title") or f"Deal {d.get('id', '')}"


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
        if not co["buys"] or not co["sells"]:
            continue
        best_buy = max(co["buys"], key=lambda t: t[0])
        best_sell = min(co["sells"], key=lambda t: t[0])
        if best_buy[0] >= best_sell[0]:
            rows.append({
                "company": co["name"],
                "buy_price": best_buy[0],
                "buy_deal": best_buy[1],
                "sell_price": best_sell[0],
                "sell_deal": best_sell[1],
                "spread": best_buy[0] - best_sell[0],
            })
    rows.sort(key=lambda r: r["spread"], reverse=True)
    return rows


def _build_tight(deals, companies):
    co_by_id = {c.get("id"): c for c in companies if c.get("id") is not None}
    rows = []
    for d in deals:
        if _stage_id(d) != STAGE_FIRM:
            continue
        side = _deal_side(d)
        cid = _company_id(d)
        co = co_by_id.get(cid)
        if not co:
            continue
        co_name = co.get("name") or _company_name(d)
        if side == "BUY":
            ask = _cf_number(co, CF_HIIVE_ASK)
            gross = _cf_number(d, CF_GROSS)
            if not ask or gross is None:
                continue
            dist = (ask - gross) / ask
            if abs(dist) <= TIGHT_PCT:
                rows.append({
                    "company": co_name,
                    "side": "BUY",
                    "your_price": gross,
                    "hiive_price": ask,
                    "distance": dist,
                    "deal": d,
                })
        elif side == "SELL":
            bid = _cf_number(co, CF_HIIVE_BID)
            net = _cf_number(d, CF_NET)
            if not bid or net is None:
                continue
            dist = (net - bid) / bid
            if abs(dist) <= TIGHT_PCT:
                rows.append({
                    "company": co_name,
                    "side": "SELL",
                    "your_price": net,
                    "hiive_price": bid,
                    "distance": dist,
                    "deal": d,
                })
    rows.sort(key=lambda r: abs(r["distance"]))
    return rows


def _build_matched(deals, now):
    rows = []
    for d in deals:
        if _stage_id(d) != STAGE_MATCHED:
            continue
        days = _days_since(_parse_dt(d.get("updated_at")), now)
        rows.append({
            "title": _deal_title(d),
            "company": _company_name(d),
            "contact": _contact(d),
            "days": days if days is not None else 0,
        })
    rows.sort(key=lambda r: r["days"], reverse=True)
    return rows


def _build_stale(deals, now):
    rows = []
    for d in deals:
        if _stage_id(d) not in ACTIVE_STAGES:
            continue
        refresh = _cf_date(d, CF_REFRESH) or _parse_dt(d.get("created_at"))
        days = _days_since(refresh, now)
        if days is None or days <= STALE_THRESHOLD_DAYS:
            continue
        rows.append({
            "title": _deal_title(d),
            "company": _company_name(d),
            "contact": _contact(d),
            "days": days,
        })
    rows.sort(key=lambda r: r["days"], reverse=True)
    return rows


def _contact_cell(name, email):
    if name and email:
        return f"{escape(name)} &lt;{escape(email)}&gt;"
    if name:
        return escape(name)
    if email:
        return f"&lt;{escape(email)}&gt;"
    return ""


def _render_html(crossed, tight, matched, stale, date_str):
    out = [f"<html><body><h1>Daily Brief — {escape(date_str)}</h1>"]

    out.append("<h2>A. Crossed in Own Book</h2>")
    if not crossed:
        out.append("<p>(None)</p>")
    else:
        out.append("<table border='1' cellpadding='4' cellspacing='0'>")
        out.append("<tr><th>Company</th><th>Buy</th><th>Buy Contact</th>"
                   "<th>Buy Deal ID</th><th>Sell</th><th>Sell Contact</th>"
                   "<th>Sell Deal ID</th><th>Spread</th></tr>")
        for r in crossed:
            bn, be = _contact(r["buy_deal"])
            sn, se = _contact(r["sell_deal"])
            out.append(
                "<tr>"
                f"<td>{escape(r['company'])}</td>"
                f"<td>{escape(_fmt_price(r['buy_price']))}</td>"
                f"<td>{_contact_cell(bn, be)}</td>"
                f"<td>{escape(str(r['buy_deal'].get('id', '')))}</td>"
                f"<td>{escape(_fmt_price(r['sell_price']))}</td>"
                f"<td>{_contact_cell(sn, se)}</td>"
                f"<td>{escape(str(r['sell_deal'].get('id', '')))}</td>"
                f"<td>{escape(_fmt_price(r['spread']))}</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append("<h2>B. Tight Markets vs Hiive (within 10%)</h2>")
    if not tight:
        out.append("<p>(None)</p>")
    else:
        out.append("<table border='1' cellpadding='4' cellspacing='0'>")
        out.append("<tr><th>Company</th><th>Side</th><th>Your Price</th>"
                   "<th>Hiive</th><th>% Distance</th><th>Contact</th>"
                   "<th>Deal ID</th></tr>")
        for r in tight:
            n, e = _contact(r["deal"])
            out.append(
                "<tr>"
                f"<td>{escape(r['company'])}</td>"
                f"<td>{escape(r['side'])}</td>"
                f"<td>{escape(_fmt_price(r['your_price']))}</td>"
                f"<td>{escape(_fmt_price(r['hiive_price']))}</td>"
                f"<td>{r['distance'] * 100:+.2f}%</td>"
                f"<td>{_contact_cell(n, e)}</td>"
                f"<td>{escape(str(r['deal'].get('id', '')))}</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append("<h2>C. Matched — Follow Up</h2>")
    if not matched:
        out.append("<p>(No Matched deals — verify stage 2381534.)</p>")
    else:
        out.append("<table border='1' cellpadding='4' cellspacing='0'>")
        out.append("<tr><th>Deal</th><th>Company</th><th>Contact</th>"
                   "<th>Days Since Update</th></tr>")
        for r in matched:
            n, e = r["contact"]
            out.append(
                "<tr>"
                f"<td>{escape(r['title'])}</td>"
                f"<td>{escape(r['company'])}</td>"
                f"<td>{_contact_cell(n, e)}</td>"
                f"<td>{r['days']}</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append("<h2>D. Approaching Stale (&gt;50 days since refresh)</h2>")
    if not stale:
        out.append("<p>(None)</p>")
    else:
        out.append("<table border='1' cellpadding='4' cellspacing='0'>")
        out.append("<tr><th>Deal</th><th>Company</th><th>Contact</th>"
                   "<th>Days Since Refresh</th></tr>")
        for r in stale:
            n, e = r["contact"]
            out.append(
                "<tr>"
                f"<td>{escape(r['title'])}</td>"
                f"<td>{escape(r['company'])}</td>"
                f"<td>{_contact_cell(n, e)}</td>"
                f"<td>{r['days']}</td>"
                "</tr>"
            )
        out.append("</table>")

    out.append("</body></html>")
    return "".join(out)


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d UTC")

    s3 = boto3.client("s3", region_name=S3_REGION)
    deals_doc = _fetch_json(s3, "deals.json")
    companies_doc = _fetch_json(s3, "companies.json")
    deals = deals_doc.get("deals", []) or []
    companies = companies_doc.get("companies", []) or []

    crossed = _build_crossed(deals)
    tight = _build_tight(deals, companies)
    matched = _build_matched(deals, now)
    stale = _build_stale(deals, now)

    body_html = _render_html(crossed, tight, matched, stale, date_str)
    subject = f"Daily Brief — {date_str}"

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
            "matched": len(matched),
            "stale": len(stale),
            "deals": len(deals),
            "companies": len(companies),
        },
    }
