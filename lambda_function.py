import json
import os
import re
import urllib.error
import urllib.request
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
TRADES_DEAL_URL = "trades.graciagroup.com/deals/{}"

IQF_URL_ENTITY = (
    "https://www.rainmakersecurities.com/"
    "investor-qualification-form-for-entity-persons"
)
IQF_URL_NATURAL = (
    "https://www.rainmakersecurities.com/"
    "investor-qualification-form-for-natural-persons"
)
CEF_URL_ENTITY = (
    "https://www.rainmakersecurities.com/"
    "client-engagement-form-for-entity-persons"
)
CEF_URL_NATURAL = (
    "https://www.rainmakersecurities.com/"
    "client-engagement-form-for-natural-persons"
)

CF_DEAL_SIDE = "custom_label_1958"
CF_GROSS = "custom_label_3064339"
CF_NET = "custom_label_3064369"
CF_STRUCTURE = "custom_label_3064360"
CF_TICKET_MIN = "custom_label_3065488"
CF_TICKET_MAX = "custom_label_3064645"
CF_MKT_ASK = "custom_label_3997297"
CF_MKT_BID = "custom_label_3997298"
CF_IQF = "custom_label_3763008"
CF_CEF = "custom_label_3796440"
CF_NEWSLETTER = "custom_label_3775335"
CF_EMAIL_STATUS = "custom_label_2447206"
CF_PERSON_BUY_INTERESTS = "custom_label_3322093"
CF_PERSON_SELL_INTERESTS = "custom_label_3759156"
CF_PERSON_HOLD_INTERESTS = "custom_label_3740611"
CF_AGENT_AGREEMENT = "custom_label_3714334"
CF_TOP_SPV_MANAGER = "custom_label_3948282"
CF_SEC_PRIORITY = "custom_label_3912746"
CF_NOTICE = "custom_label_3815544"
CF_MAX_SIZE = "custom_label_3064645"
CF_SHARE_COUNT = "custom_label_3070843"

OPT_SELL = 5011675
OPT_BUY = 5077819
OPT_STRUCT_DIRECT = 6250090
OPT_STRUCT_FUND = 5077906
OPT_IQF_YES = 6496840
OPT_IQF_PENDING = 6496842
OPT_IQF_NO = 6496841
OPT_CEF_YES = 6600515
OPT_CEF_PENDING = 6600514
OPT_CEF_NO = 6600513
OPT_CEF_NA = 6600516
OPT_AGENT_YES_BUYSIDE = 6354274
OPT_AGENT_YES_SELLSIDE = 6354277

AGENT_YES_OPTS = {OPT_AGENT_YES_BUYSIDE, OPT_AGENT_YES_SELLSIDE}

OPT_SPV_BUILD_CONNECTION = 7026574   # Whitelisted: Build Connection
OPT_SPV_MONTHLY_UPDATES = 7026575    # Whitelisted: Monthly Updates
OPT_SPV_SCREEN = 7026573             # Screen
OPT_SPV_SCREENING = 7026576          # Screening
SPV_WHITELISTED_OPTS = {OPT_SPV_BUILD_CONNECTION, OPT_SPV_MONTHLY_UPDATES}
SPV_SCREEN_OPTS = {OPT_SPV_SCREEN, OPT_SPV_SCREENING}
SPV_OPT_LABELS = {
    OPT_SPV_BUILD_CONNECTION: "Whitelisted: Build Connection",
    OPT_SPV_MONTHLY_UPDATES: "Whitelisted: Monthly Updates",
    OPT_SPV_SCREEN: "Screen",
    OPT_SPV_SCREENING: "Screening",
}

OPT_SEC_PRIORITY_HIGH = 6919452      # High - Decision-Maker with Cash

OPT_NEWSLETTER_SUBSCRIBED_WEEKLY = 6613674

OPT_NOTICE_POST = 6762534
OPT_NOTICE_UPDATE = 6762537
NOTICE_ACTIONABLE = {OPT_NOTICE_POST, OPT_NOTICE_UPDATE}

POST_STRUCTURE_LABELS = {
    6250090: "Direct",
    5077906: "Fund/SPV",
    5077903: "Forward",
    6361933: "Unknown",
    5077909: "None",
}

NEWSLETTER_OPTS = {OPT_NEWSLETTER_SUBSCRIBED_WEEKLY, 6613673, 6582981}

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

CEF_LABELS = {
    OPT_CEF_YES: "✓",
    OPT_CEF_PENDING: "…",
    OPT_CEF_NO: "✗",
    OPT_CEF_NA: "✗",
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

TO_CLOSE_ALL_STAGES = {STAGE_MATCHED, STAGE_LOI_SIGNED, STAGE_TRANSFER_NOTICE}
TO_CLOSE_AGED_STAGE = None
TO_CLOSE_AGE_DAYS = 30

NEG_DISTANCE_BG = "#d4edda"

FONT_STACK = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "Helvetica,Arial,sans-serif"
)
BODY_STYLE = (
    f"font-family:{FONT_STACK}; margin:0; padding:0; background:#ffffff;"
    " color:#1f2937; font-size:14px; line-height:1.5;"
)
CONTAINER_STYLE = "max-width:960px; margin:0 auto; padding:24px;"
H1_STYLE = (
    f"font-family:{FONT_STACK}; font-size:40px; font-weight:700;"
    " color:#0f172a; letter-spacing:-0.02em; line-height:1.1;"
    " margin:0 0 24px 0;"
)
H2_STYLE = (
    f"font-family:{FONT_STACK}; font-size:20px; font-weight:600;"
    " color:#111827; letter-spacing:-0.01em;"
    " border-bottom:1px solid #e5e7eb; padding-bottom:6px;"
    " margin:32px 0 12px 0;"
)
SUB_SUMMARY_STYLE = (
    "font-size:13px; color:#4b5563; margin:0 0 4px 0; font-weight:normal;"
)
TABLE_STYLE = "width:100%; border-collapse:collapse; table-layout:fixed;"
TH_BASE = (
    "background:#f9fafb; font-weight:600; font-size:12px;"
    " color:#6b7280;"
    " padding:8px 10px; text-align:left;"
    " border-bottom:1px solid #f3f4f6;"
    " word-wrap:break-word; overflow-wrap:break-word;"
)
TD_STYLE = (
    "font-size:13px; color:#1f2937; padding:10px;"
    " border-bottom:1px solid #f3f4f6;"
    " word-wrap:break-word; overflow-wrap:break-word;"
    " vertical-align:top;"
)
LINK_STYLE = "color:#2563eb; text-decoration:none;"
LINK_STYLE_500 = "color:#2563eb; text-decoration:none; font-weight:500;"
SECTION_GAP_HTML = '<div style="height: 24px;"></div>'
SUB_HEADING_STYLE = (
    "font-size:14px; font-weight:600; color:#4b5563;"
    " margin:16px 0 6px 0;"
)


def _normalize_id(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


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
            raw = (
                item.get("option_id")
                or item.get("id")
                or item.get("company_id")
                or item.get("value")
            )
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
    if deal is None:
        return ""
    cid = _normalize_id(_company_id(deal))
    if person and cid is not None:
        if cid in _cf_option_ids(person, CF_PERSON_BUY_INTERESTS):
            return " (b)"
        if cid in _cf_option_ids(person, CF_PERSON_SELL_INTERESTS):
            return " (s)"
    # No buy-side fallback per spec; sell-side falls back to either the
    # custom-field deal side (canonical for this org) or the built-in
    # deal_type string.
    if _deal_side(deal) == "SELL" or _deal_type(deal) == "sell":
        return " (s)"
    return ""


def _person_role(deal, person):
    annot = _buyer_seller_annotation(deal, person)
    if annot == " (b)":
        return "buyer"
    if annot == " (s)":
        return "seller"
    return None


def _iqf_cell(person, deal):
    if _person_role(deal, person) == "seller":
        return '<span style="color:#6b7280;">—</span>'
    return _colorize_symbol(_person_iqf(person))


def _split_contacts_by_role(people, deal):
    people = [p for p in people if isinstance(p, dict)]
    if not people:
        return None, None

    buyer = None
    seller = None
    unresolved = []
    for p in people:
        role = _person_role(deal, p)
        if role == "buyer":
            if buyer is None:
                buyer = p
        elif role == "seller":
            if seller is None:
                seller = p
        else:
            unresolved.append(p)

    if buyer is not None or seller is not None:
        return buyer, seller

    is_sell = _deal_side(deal) == "SELL" or _deal_type(deal) == "sell"

    if len(unresolved) == 1:
        if is_sell:
            return None, unresolved[0]
        return unresolved[0], None

    primary_id = _normalize_id(deal.get("primary_contact_id"))
    if primary_id is None:
        pc = deal.get("primary_contact") or {}
        if isinstance(pc, dict):
            primary_id = _normalize_id(pc.get("id"))
    if primary_id is not None:
        primary = next(
            (p for p in unresolved
             if _normalize_id(p.get("id")) == primary_id),
            None,
        )
        if primary is not None:
            other = next((p for p in unresolved if p is not primary), None)
            if is_sell:
                return other, primary
            return primary, other

    return unresolved[0], unresolved[1] if len(unresolved) > 1 else None


def _single_contact_cell(person, deal, company):
    if not isinstance(person, dict):
        return ""
    n, e = _person_name_email(person)
    if not n and not e:
        return ""
    cell = _contact_cell(n, email=e, person_id=person.get("id"))
    env = _envelope_link(deal, person, company)
    if env:
        cell = f"{cell}{env}"
    return cell


def _stack2(top, bottom):
    if not top and not bottom:
        return ""
    return (
        f'<div style="margin-bottom:8px;">{top or "&nbsp;"}</div>'
        f'<div style="margin-bottom:0;">{bottom or "&nbsp;"}</div>'
    )


def _stacked_contact_cell(buyer, seller, deal, company):
    top = _single_contact_cell(buyer, deal, company) if buyer else ""
    bottom = _single_contact_cell(seller, deal, company) if seller else ""
    return _stack2(top, bottom)


def _stacked_iqf_cell(buyer, seller, deal):
    top = _iqf_cell(buyer, deal) if buyer else ""
    bottom = '<span style="color:#6b7280;">—</span>' if seller else ""
    return _stack2(top, bottom)


def _stacked_cef_cell(buyer, seller):
    top = _colorize_symbol(_person_cef(buyer)) if buyer else ""
    bottom = _colorize_symbol(_person_cef(seller)) if seller else ""
    return _stack2(top, bottom)


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


def _person_cef(p):
    return CEF_LABELS.get(_cf_option_id(p, CF_CEF), "")


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
            pid = _normalize_id(p.get("id"))
            full = people_by_id.get(pid) if pid is not None else None
            chosen = full or p
            key = pid if pid is not None else id(chosen)
            if key in seen:
                continue
            seen.add(key)
            out.append(chosen)
    else:
        for pid in (deal.get("person_ids") or []):
            npid = _normalize_id(pid)
            full = people_by_id.get(npid) if npid is not None else None
            if not full or npid in seen:
                continue
            seen.add(npid)
            out.append(full)
    return out


def _fetch_json(s3, key):
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def _load_security_names(s3):
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key="security_names.json")
        data = json.loads(obj["Body"].read())
        return {int(k): v for k, v in data.items()}
    except Exception:
        return {}


def get_jwt():
    s3 = boto3.client('s3')
    obj = s3.get_object(Bucket="pipeline-token", Key="pipeline-jwt.json")
    return json.loads(obj['Body'].read())['jwt']


def call_pipeline_api(method, endpoint, payload=None, jwt=None, timeout=15):
    base = "https://api.pipelinecrm.com/api/v3"
    url  = f"{base}{endpoint}"
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type":  "application/json"
    }
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "data": json.loads(r.read().decode())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "data": e.read().decode()}
    except Exception as e:
        return {"status": 500, "data": str(e)}


SYSTEM_NOTE_PREFIXES = (
    "deal stage was changed",
    "stage was changed",
    "stage changed",
    "custom field",
    "deal was created",
    "deal owner was changed",
    "deal owner changed",
    "person was added",
    "person was removed",
    "company was changed",
    "primary contact was changed",
    "deal was updated",
)


def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r'<[^>]+>', '', s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return s.strip()


def _is_system_note(note):
    if not isinstance(note, dict):
        return False
    raw = note.get("title") or note.get("content") or ""
    cleaned = _strip_html(raw).lower()
    return any(cleaned.startswith(prefix) for prefix in SYSTEM_NOTE_PREFIXES)


def _fetch_latest_notes(deal_id, jwt):
    if deal_id in (None, "") or not jwt:
        return []
    endpoint = f"/notes.json?conditions[deal_id]={deal_id}"
    result = call_pipeline_api("GET", endpoint, jwt=jwt, timeout=3)
    if result.get("status") != 200:
        return []
    data = result.get("data")
    if not isinstance(data, dict):
        return []
    entries = data.get("entries") or []
    if not isinstance(entries, list) or not entries:
        return []
    entries = [e for e in entries if not _is_system_note(e)]
    if not entries:
        return []
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    entries.sort(
        key=lambda n: _parse_dt(n.get("created_at")) or epoch,
        reverse=True,
    )
    return entries[:3]


def _latest_activity_cell(notes):
    if not notes:
        return '<span style="color:#9ca3af;">—</span>'
    parts = []
    last = len(notes) - 1
    for i, note in enumerate(notes):
        created = _parse_dt(note.get("created_at"))
        date_str = created.strftime("%m/%d") if created else ""
        body = _strip_html(note.get("title"))
        if not body:
            body = _strip_html(note.get("content"))
        truncated = body[:40]
        if len(body) > 40:
            snippet = truncated + "..."
        else:
            snippet = truncated
        mb = "0" if i == last else "8px"
        parts.append(
            f'<div class="activity-entry" data-row-key-skip="1"'
            f' style="margin-bottom:{mb}; font-size:12px; line-height:1.3;">'
        )
        if date_str:
            parts.append(
                f'<div style="color:#6b7280;">{escape(date_str)}</div>'
            )
        parts.append(f'<div class="activity-snippet">{escape(snippet)}</div>')
        parts.append(
            '<div class="activity-full" style="display:none;">'
            f'{escape(body)}</div>'
        )
        parts.append('</div>')
    return "".join(parts)


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
    href = PIPELINE_DEAL_URL.format(escape(s, quote=True))
    return f'<a href="{href}" style="{LINK_STYLE}">{escape(s)}</a>'


def _deal_title_link(deal):
    title = _deal_title(deal)
    did = deal.get("id")
    if did in (None, ""):
        return escape(title)
    href = PIPELINE_DEAL_URL.format(escape(str(did), quote=True))
    return f'<a href="{href}" style="{LINK_STYLE_500}">{escape(title)}</a>'


def _resolve_person(snapshot, people_by_id):
    if not isinstance(snapshot, dict):
        return {}
    if not people_by_id:
        return snapshot
    pid = _normalize_id(snapshot.get("id"))
    if pid is None:
        return snapshot
    return people_by_id.get(pid) or snapshot


def _envelope_link(deal, person, company):
    if not isinstance(person, dict):
        return ""
    _, email = _person_name_email(person)
    if not email:
        return ""
    if not isinstance(deal, dict):
        deal = {}
    if not isinstance(company, dict):
        company = {}

    side = _deal_side(deal)
    side_word = "sell" if side == "SELL" else "buy" if side == "BUY" else ""
    company_name = company.get("name") or _company_name(deal) or ""
    subject = " ".join(
        p for p in ("Quick question on your", company_name, side_word, "indication")
        if p
    )

    first_name = _person_first_name(person) or _person_full_name(person) or ""
    deal_id = deal.get("id") if deal else ""

    if side == "BUY":
        your_price = _cf_number(deal, CF_GROSS)
        market_price = _cf_number(company, CF_MKT_ASK)
    elif side == "SELL":
        your_price = _cf_number(deal, CF_NET)
        market_price = _cf_number(company, CF_MKT_BID)
    else:
        your_price = None
        market_price = None

    lines = [
        f"Dear {first_name}:",
        "I have a quick question about your indication: "
        f"{TRADES_DEAL_URL.format(deal_id)}",
    ]
    if your_price is not None:
        if market_price is not None:
            lines.append(
                f"I recall your price was {_fmt_price(your_price)} "
                f"and I see the market price is {_fmt_price(market_price)}."
            )
        else:
            lines.append(f"I recall your price was {_fmt_price(your_price)}.")

    if side == "BUY" and _cf_option_id(person, CF_IQF) != OPT_IQF_YES:
        lines.append("")
        lines.append(
            "I noticed that we don't have an Investor Qualification Form on file. "
            "Can you take a moment to fill this out so that you're all papered up "
            "and ready to close?"
        )
        lines.append(f"Entity: {IQF_URL_ENTITY}")
        lines.append(f"Natural person: {IQF_URL_NATURAL}")
    elif side == "SELL" and _cf_option_id(person, CF_CEF) != OPT_CEF_YES:
        lines.append("")
        lines.append(
            "I have some inquiries but would need this form to move forward. "
            "Can you take a moment to fill it out?"
        )
        lines.append(f"Entity: {CEF_URL_ENTITY}")
        lines.append(f"Natural person: {CEF_URL_NATURAL}")

    body = "\r\n".join(lines)
    href = (
        f"mailto:{quote(email, safe='@')}"
        f"?subject={quote(subject, safe='')}"
        f"&body={quote(body, safe='')}"
    )
    return (
        f'<a href="{escape(href, quote=True)}"'
        ' style="color:#2563eb; text-decoration:none; margin-left:6px;"'
        ' title="Email">✉</a>'
    )


def _contact_cell(name, email=None, person_id=None):
    label = name or email or ""
    if not label:
        return ""
    if person_id in (None, ""):
        return escape(label)
    href = PIPELINE_PERSON_URL.format(escape(str(person_id), quote=True))
    return f'<a href="{href}" style="{LINK_STYLE_500}">{escape(label)}</a>'


def _email_link(email):
    if not email:
        return ""
    href = f"mailto:{quote(email, safe='@')}"
    return (
        f'<a href="{escape(href, quote=True)}" style="{LINK_STYLE}">'
        f"{escape(email)}</a>"
    )


def _colorize_symbol(sym):
    if not sym:
        return ""
    if sym == "✓":
        return '<span style="color:#16a34a;">✓</span>'
    if sym == "✗":
        return '<span style="color:#dc2626;">✗</span>'
    return escape(sym)


def _people_cells(people, deal=None, company=None):
    entries = []
    for p in people:
        n, e = _person_name_email(p)
        if not n and not e:
            continue
        contact_link = _contact_cell(n, email=e, person_id=p.get("id"))
        annotation = _buyer_seller_annotation(deal, p) if deal is not None else ""
        envelope = _envelope_link(deal, p, company) if deal is not None else ""
        cell = contact_link
        if annotation:
            cell = f"{cell}{escape(annotation)}"
        if envelope:
            cell = f"{cell}{envelope}"
        entries.append((
            cell,
            _iqf_cell(p, deal),
            _colorize_symbol(_person_cef(p)),
        ))
    if not entries:
        return "", "", ""
    last = len(entries) - 1
    contact_parts, iqf_parts, cef_parts = [], [], []
    for i, (c, q, ce) in enumerate(entries):
        mb = "0" if i == last else "8px"
        contact_parts.append(
            f'<div style="margin-bottom:{mb}; white-space:nowrap;">{c}</div>'
        )
        iqf_parts.append(f'<div style="margin-bottom:{mb};">{q}</div>')
        cef_parts.append(f'<div style="margin-bottom:{mb};">{ce}</div>')
    return "".join(contact_parts), "".join(iqf_parts), "".join(cef_parts)


def _commission_cell(deal):
    if _cf_option_id(deal, CF_AGENT_AGREEMENT) in AGENT_YES_OPTS:
        return _colorize_symbol("✓")
    return ""


def _header_row(labels, with_checkbox=False):
    n = len(labels)
    pct = 100.0 / n if n else 100.0
    cells = []
    if with_checkbox:
        cells.append(f'<th style="width:32px; {TH_BASE}"></th>')
    for lbl in labels:
        style = f"width:{pct:.4f}%; {TH_BASE}"
        if isinstance(lbl, (list, tuple)):
            inner = "".join(
                f'<div style="line-height:1.3;">{escape(line)}</div>'
                for line in lbl
            )
        else:
            inner = escape(lbl)
        cells.append(f'<th style="{style}">{inner}</th>')
    return "<tr>" + "".join(cells) + "</tr>"


def _row_open(section, row_id, interactive):
    if interactive and row_id not in (None, ""):
        key = f"{section}:{row_id}"
        return f'<tr data-row-key="{escape(key, quote=True)}">'
    return "<tr>"


def _checkbox_td(section, row_id, interactive, dismiss_days=None):
    if not interactive or row_id in (None, ""):
        return ""
    key = f"{section}:{row_id}"
    dismiss_attr = f' data-dismiss-days="{int(dismiss_days)}"' if dismiss_days is not None else ""
    return (
        '<td style="width:32px; text-align:center; padding:10px;'
        ' border-bottom:1px solid #f3f4f6; vertical-align:top;">'
        f'<input type="checkbox" class="row-dismiss"'
        f' data-row-key="{escape(key, quote=True)}"{dismiss_attr} />'
        '</td>'
    )


def _td(content, extra=""):
    style = TD_STYLE + extra
    return f'<td style="{style}">{content}</td>'


def _section_heading(text):
    return f'<h2 style="{H2_STYLE}">{escape(text)}</h2>'


def _section_open(key, text, interactive):
    """Open a dismissable section wrapper. Emits the wrapper div and the
    section heading. The renderer must close with _section_close() once
    the section's table/fallback markup has been appended."""
    hide_btn = ""
    if interactive:
        hide_btn = (
            '<button type="button" class="section-hide"'
            ' title="Hide this section"'
            ' style="float:right; background:none; border:none; cursor:pointer;'
            ' color:#9ca3af; font-size:18px; line-height:1; padding:4px 8px;'
            ' font-family:inherit;">&times;</button>'
        )
    return (
        f'<div class="section" data-section-key="{escape(key, quote=True)}">'
        f'<h2 style="{H2_STYLE}">{hide_btn}{escape(text)}</h2>'
    )


def _section_close():
    return "</div>"


def _muted_p(text):
    return f'<p style="{SUB_SUMMARY_STYLE}">{escape(text)}</p>'


def _open_table():
    return f'<table style="{TABLE_STYLE}">'


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


def _build_crossed(deals, people_by_id):
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
                        "buy_primary": _resolve_person(
                            _primary_contact(buy), people_by_id
                        ),
                        "sell_price": n,
                        "sell_deal": sell,
                        "sell_primary": _resolve_person(
                            _primary_contact(sell), people_by_id
                        ),
                        "pct": pct,
                    }
        if best:
            rows.append(best)
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows


def _build_tight(deals, companies, people_by_id):
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
                    "primary": _resolve_person(_primary_contact(d), people_by_id),
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
                    "primary": _resolve_person(_primary_contact(d), people_by_id),
                })
    rows.sort(key=lambda r: (0 if r["side"] == "SELL" else 1, r["distance"]))
    return rows


def _build_to_close(deals, now, people_by_id, jwt=None):
    rows = []
    for d in deals:
        sid = _stage_id(d)
        if sid not in TO_CLOSE_ALL_STAGES and sid != TO_CLOSE_AGED_STAGE:
            continue
        days = _days_since(_parse_dt(d.get("updated_at")), now)
        days = days if days is not None else 0
        if sid == TO_CLOSE_AGED_STAGE and days < TO_CLOSE_AGE_DAYS:
            continue
        max_s = _cf_number(d, CF_TICKET_MAX)
        min_s = _cf_number(d, CF_TICKET_MIN)
        size = max_s if max_s is not None else min_s
        rows.append({
            "stage": STAGE_LABELS.get(sid, str(sid)),
            "title": _deal_title(d),
            "company": _company_name(d),
            "people": _deal_people(d, people_by_id),
            "days": days,
            "deal": d,
            "_size": size,
            "_sid": sid,
        })
    rows.sort(key=lambda r: (r["_size"] is None, -(r["_size"] or 0.0)))
    # Fetch the most recent note for each displayed deal. Sequential with a
    # short per-call timeout (~3s) so a hung Pipeline API can't block the
    # whole brief; on any error the cell falls back to an em dash.
    for r in rows:
        r["latest_notes"] = _fetch_latest_notes(r["deal"].get("id"), jwt)
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


def _build_to_post(deals):
    rows = []
    for d in deals:
        actionable = _cf_option_ids(d, CF_NOTICE) & NOTICE_ACTIONABLE
        if not actionable:
            continue
        has_post = OPT_NOTICE_POST in actionable
        has_update = OPT_NOTICE_UPDATE in actionable
        if has_post and has_update:
            action = "Post / Update"
        elif has_post:
            action = "Post"
        else:
            action = "Update"
        rows.append({"deal": d, "action": action})
    rows.sort(key=lambda r: (_deal_title(r["deal"]) or "").lower())
    return rows


def _post_structure_label(deal):
    opts = _cf_option_ids(deal, CF_STRUCTURE)
    if not opts:
        return "—"
    names = [POST_STRUCTURE_LABELS[o] for o in opts if o in POST_STRUCTURE_LABELS]
    if not names:
        return "—"
    return ", ".join(sorted(names))


def _post_price(deal):
    side = _deal_side(deal)
    if side == "SELL":
        return _cf_number(deal, CF_NET)
    if side == "BUY":
        return _cf_number(deal, CF_GROSS)
    return None


def _fmt_int_or_dash(v):
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_price_or_dash(v):
    if v is None:
        return "—"
    return _fmt_price(v)


def _count_newsletter_recipients(people):
    n = 0
    for p in people:
        if not (_cf_option_ids(p, CF_NEWSLETTER) & NEWSLETTER_OPTS):
            continue
        if not _person_email(p):
            continue
        n += 1
    return n


def _build_priority_leads_no_email(people, deals):
    active_pids = set()
    deals_by_pid = {}
    for d in deals:
        if _stage_id(d) not in ACTIVE_STAGES:
            continue
        pids_in_deal = set()
        pc = d.get("primary_contact") or {}
        if isinstance(pc, dict):
            pid = _normalize_id(pc.get("id"))
            if pid is not None:
                pids_in_deal.add(pid)
        pcid = _normalize_id(d.get("primary_contact_id"))
        if pcid is not None:
            pids_in_deal.add(pcid)
        people_list = d.get("people") or []
        if isinstance(people_list, list):
            for p in people_list:
                if isinstance(p, dict):
                    pid = _normalize_id(p.get("id"))
                    if pid is not None:
                        pids_in_deal.add(pid)
        for pid in pids_in_deal:
            active_pids.add(pid)
            deals_by_pid.setdefault(pid, []).append(d)

    out = []
    for p in people:
        if (p.get("type") or "").strip().lower() != "lead":
            continue
        if _person_email(p):
            continue
        pid = _normalize_id(p.get("id"))
        if pid is None or pid not in active_pids:
            continue
        related = deals_by_pid.get(pid, [])
        companies_seen = []
        seen_company = set()
        for d in related:
            cname = ""
            co = d.get("company")
            if isinstance(co, dict):
                cname = (co.get("name") or "").strip()
            if not cname:
                cname = (d.get("company_name") or "").strip()
            if not cname:
                dname = (d.get("name") or "").strip()
                if dname:
                    cname = dname.split(" - ")[0].split(" – ")[0].strip()
            if not cname or cname in seen_company:
                continue
            seen_company.add(cname)
            companies_seen.append(cname)
        out.append({
            "person_id": pid,
            "name": _person_full_name(p),
            "active_deal_count": len(related),
            "companies": companies_seen,
            "linked_in_url": p.get("linked_in_url") or "",
        })
    out.sort(key=lambda r: (-r["active_deal_count"], r["name"].lower()))
    return out


def _build_leads_to_revive(people, companies, priority_pids=None):
    priority_pids = priority_pids or set()
    co_by_id = {c.get("id"): c for c in companies if c.get("id") is not None}
    rows_by_pid = {}
    for p in people:
        pid = p.get("id")
        if pid is None:
            continue

        npid = _normalize_id(pid)
        email = _person_email(p)
        bucket1 = (
            (not email)
            and _has_tag(p, TAG_WHITELIST_CONTACT_ESTABLISHED)
            and npid not in priority_pids
        )

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
            "person_id": pid,
            "linked_in_url": p.get("linked_in_url") or "",
            "company_name": co_name,
            "company_website": co_website,
        }

    rows = list(rows_by_pid.values())
    rows.sort(key=lambda r: (r["reason"], r["name"].lower()))
    return rows


def _build_spv_managers_to_warm(people):
    rows = []
    for p in people:
        opt = _cf_option_id(p, CF_TOP_SPV_MANAGER)
        if opt not in SPV_WHITELISTED_OPTS and not (
            opt in SPV_SCREEN_OPTS
            and _cf_option_id(p, CF_SEC_PRIORITY) == OPT_SEC_PRIORITY_HIGH
        ):
            continue
        name = _person_full_name(p) or ""
        rows.append({
            "person_id": p.get("id"),
            "name": name,
            "email": _person_email(p),
            "spv_value": SPV_OPT_LABELS.get(opt, ""),
            "sell_count": len(_cf_option_ids(p, CF_PERSON_SELL_INTERESTS)),
            "buy_count": len(_cf_option_ids(p, CF_PERSON_BUY_INTERESTS)),
            "_opt": opt,
        })
    rows.sort(key=lambda r: (
        0 if r["_opt"] == OPT_SPV_BUILD_CONNECTION else 1,
        r["name"].lower(),
    ))
    return rows


def _build_top_buyers_to_warm(people):
    rows = []
    for p in people:
        buy_ids = _cf_option_ids(p, CF_PERSON_BUY_INTERESTS)
        if not buy_ids:
            continue
        if _cf_option_id(p, CF_SEC_PRIORITY) != OPT_SEC_PRIORITY_HIGH:
            continue
        name = _person_full_name(p) or ""
        rows.append({
            "person_id": p.get("id"),
            "name": name,
            "email": _person_email(p),
            "buy_count": len(buy_ids),
        })
    rows.sort(key=lambda r: (-r["buy_count"], r["name"].lower()))
    return rows[:30]


def _build_popular_spv_managers(people, security_names):
    buyer_counts = {}
    for p in people:
        for eid in _cf_option_ids(p, CF_PERSON_BUY_INTERESTS):
            buyer_counts[eid] = buyer_counts.get(eid, 0) + 1
    top10 = set(
        sorted(buyer_counts, key=lambda e: buyer_counts[e], reverse=True)[:10]
    )

    rows = []
    for p in people:
        spv_opt = _cf_option_id(p, CF_TOP_SPV_MANAGER)
        if spv_opt not in SPV_WHITELISTED_OPTS:
            continue
        if _cf_option_id(p, CF_NEWSLETTER) == OPT_NEWSLETTER_SUBSCRIBED_WEEKLY:
            continue
        hold_ids = _cf_option_ids(p, CF_PERSON_HOLD_INTERESTS)
        matches = hold_ids & top10
        if not matches:
            continue
        sorted_eids = sorted(
            matches, key=lambda e: buyer_counts.get(e, 0), reverse=True
        )
        holdings = [security_names.get(eid, f"#{eid}") for eid in sorted_eids]
        rows.append({
            "person_id": p.get("id"),
            "name": _person_full_name(p) or "",
            "email": _person_email(p),
            "spv_value": SPV_OPT_LABELS.get(spv_opt, ""),
            "holdings": holdings,
        })
    rows.sort(key=lambda r: (-len(r["holdings"]), r["name"].lower()))
    return rows


QUEUE_H1_STYLE = (
    f"font-family:{FONT_STACK}; font-size:26px; font-weight:700;"
    " color:#111827; letter-spacing:-0.015em;"
    " margin:40px 0 12px 0;"
)
QUEUE_H1_STYLE_TOP = (
    f"font-family:{FONT_STACK}; font-size:26px; font-weight:700;"
    " color:#111827; letter-spacing:-0.015em;"
    " margin:56px 0 12px 0;"
)

DISMISS_BANNER_HTML = (
    '<p id="dismiss-counter" style="font-size:13px; color:#4b5563;'
    ' margin:8px 0 24px 0; display:none;">'
    '<span id="dismiss-n">0</span> dismissed today.'
    ' <a href="#" id="show-all"'
    ' style="color:#2563eb; font-weight:500; margin-left:8px;">'
    'Show all</a></p>'
)

INTERACTIVE_JS = """
<script>
(function() {
  const STORAGE_KEY = "dailyBriefDismissed";
  const SECTION_STORAGE_KEY = "dailyBriefSectionsDismissed";
  const today = new Date().toISOString().slice(0, 10);

  function readAll() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); }
    catch (e) { return {}; }
  }
  function writeAll(obj) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(obj));
  }
  function getToday() {
    const all = readAll();
    return all[today] || [];
  }
  function setToday(list) {
    const fresh = {};
    if (list.length) fresh[today] = list;
    writeAll(fresh);
  }
  function readAllSections() {
    try { return JSON.parse(localStorage.getItem(SECTION_STORAGE_KEY) || "{}"); }
    catch (e) { return {}; }
  }
  function writeAllSections(obj) {
    localStorage.setItem(SECTION_STORAGE_KEY, JSON.stringify(obj));
  }
  function getTodaySections() {
    return readAllSections()[today] || [];
  }
  function setTodaySections(list) {
    const fresh = {};
    if (list.length) fresh[today] = list;
    writeAllSections(fresh);
  }
  function applyDismissedSections() {
    const list = new Set(getTodaySections());
    document.querySelectorAll("[data-section-key]").forEach(section => {
      const key = section.getAttribute("data-section-key");
      if (list.has(key)) section.style.display = "none";
    });
  }
  function updateCounter() {
    const list = getToday();
    const counter = document.getElementById("dismiss-counter");
    const n = document.getElementById("dismiss-n");
    if (!counter || !n) return;
    n.textContent = list.length;
    counter.style.display = list.length ? "block" : "none";
  }
  function applyDismissed() {
    const list = new Set(getToday());
    document.querySelectorAll("tr[data-row-key]").forEach(tr => {
      const key = tr.getAttribute("data-row-key");
      if (list.has(key)) {
        tr.style.display = "none";
        const cb = tr.querySelector("input.row-dismiss");
        if (cb) cb.checked = true;
      }
    });
    updateCounter();
  }
  document.addEventListener("DOMContentLoaded", function() {
    applyDismissed();
    applyDismissedSections();
    document.querySelectorAll(".section-hide").forEach(btn => {
      btn.addEventListener("click", function() {
        const section = btn.closest("[data-section-key]");
        if (!section) return;
        const key = section.getAttribute("data-section-key");
        const list = getTodaySections();
        if (!list.includes(key)) list.push(key);
        setTodaySections(list);
        section.style.display = "none";
      });
    });
    document.querySelectorAll("input.row-dismiss").forEach(cb => {
      cb.addEventListener("change", function() {
        const key = this.getAttribute("data-row-key");
        const list = getToday();
        const tr = this.closest("tr[data-row-key]");
        if (this.checked) {
          if (!list.includes(key)) list.push(key);
          if (tr) tr.style.display = "none";
        } else {
          const idx = list.indexOf(key);
          if (idx >= 0) list.splice(idx, 1);
          if (tr) tr.style.display = "";
        }
        setToday(list);
        updateCounter();
      });
    });
    const showAll = document.getElementById("show-all");
    if (showAll) {
      showAll.addEventListener("click", function(e) {
        e.preventDefault();
        setToday([]);
        setTodaySections([]);
        document.querySelectorAll("tr[data-row-key]").forEach(tr => {
          tr.style.display = "";
          const cb = tr.querySelector("input.row-dismiss");
          if (cb) cb.checked = false;
        });
        document.querySelectorAll("[data-section-key]").forEach(section => {
          section.style.display = "";
        });
        updateCounter();
      });
    }

    const filterButtons = document.querySelectorAll("[data-filter-btn]");
    const queues = document.querySelectorAll("[data-owner]");
    function applyFilter(owner) {
      queues.forEach(q => {
        q.style.display = (!owner || q.getAttribute("data-owner") === owner)
          ? "" : "none";
      });
      filterButtons.forEach(b => {
        b.classList.toggle("active", b.getAttribute("data-filter-btn") === owner);
      });
    }
    filterButtons.forEach(btn => {
      btn.addEventListener("click", function() {
        const target = btn.getAttribute("data-filter-btn");
        const isActive = btn.classList.contains("active");
        applyFilter(isActive ? null : target);
      });
    });

    document.querySelectorAll(".activity-entry").forEach(entry => {
      const snippet = entry.querySelector(".activity-snippet");
      const full = entry.querySelector(".activity-full");
      if (!snippet || !full) return;
      const snippetText = snippet.textContent.replace(/\\.\\.\\.$/, "").trim();
      const fullText = full.textContent.trim();
      if (snippetText === fullText) return;
      entry.style.cursor = "pointer";
      entry.addEventListener("click", function(e) {
        e.stopPropagation();
        const expanded = entry.dataset.expanded === "true";
        snippet.style.display = expanded ? "" : "none";
        full.style.display = expanded ? "none" : "";
        entry.dataset.expanded = expanded ? "false" : "true";
      });
    });
  });
})();
</script>
"""


def _render_html(crossed, tight, to_close, to_invoice, leads,
                 newsletter_recipient_count, leads_to_revive_count, date_str,
                 companies_by_id, priority_leads, to_post,
                 spv_managers_to_warm, top_buyers_to_warm,
                 popular_spv_managers, interactive=False):
    chad_count = (
        len(to_invoice) + len(to_close) + len(crossed) + len(tight)
        + len(spv_managers_to_warm) + len(top_buyers_to_warm)
    )
    kate_count = len(to_post) + len(leads) + len(popular_spv_managers)
    out = []
    if interactive:
        out.append(
            "<!doctype html>"
            '<html lang="en"><head>'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Daily Brief — {escape(date_str)}</title>'
            "<style>"
            "body { margin: 0; background: #f5f5f5; }"
            ".reset-anchor a { text-decoration: none; }"
            ".filter-bar { display: flex; gap: 8px; margin: 16px 0 24px 0; }"
            ".filter-btn { padding: 8px 18px; border: 1px solid #d1d5db;"
            " background: #ffffff; color: #374151; font-size: 14px;"
            " font-weight: 500; border-radius: 6px; cursor: pointer;"
            " font-family: inherit; }"
            ".filter-btn:hover { background: #f9fafb; }"
            ".filter-btn.active { background: #2563eb; color: #ffffff;"
            " border-color: #2563eb; }"
            "</style>"
            '</head><body class="reset-anchor">'
            f'<div style="background:#ffffff; {CONTAINER_STYLE}">'
            f'<h1 style="{H1_STYLE}">Daily Brief — {escape(date_str)}</h1>'
            '<div class="filter-bar">'
            '<button type="button" class="filter-btn"'
            f' data-filter-btn="chad">Chad: {chad_count}'
            f" To Do{'' if chad_count == 1 else 's'}</button>"
            '<button type="button" class="filter-btn"'
            f' data-filter-btn="kate">Kate: {kate_count}'
            f" To Do{'' if kate_count == 1 else 's'}</button>"
            '</div>'
        )
        out.append(DISMISS_BANNER_HTML)
    else:
        out.append(
            "<html><body style=\"" + BODY_STYLE + "\">"
            f'<div style="{CONTAINER_STYLE}">'
            f'<h1 style="{H1_STYLE}">Daily Brief — {escape(date_str)}</h1>'
        )

    # ── Chad — Trading queue ──────────────────────────────────────────────
    out.append('<div data-owner="chad">')
    out.append(
        f'<h1 style="{QUEUE_H1_STYLE}">Chad — Trading queue</h1>'
    )

    # ── A. INVOICE ────────────────────────────────────────────────────────
    out.append(_section_open(
        "chad-1-invoice", "1. INVOICE: Get paid and win deal", interactive,
    ))
    if not to_invoice:
        out.append(_muted_p("(No SPA-signed deals awaiting invoice.)"))
    else:
        out.append(_open_table())
        out.append(_header_row([
            "Deal Title", "Contact", "IQF", "CEF", "Agent Agreement",
        ], with_checkbox=interactive))
        for r in to_invoice:
            co = companies_by_id.get(_normalize_id(_company_id(r["deal"])))
            contact_html, iqf_html, cef_html = _people_cells(
                r["people"], deal=r["deal"], company=co
            )
            did = r["deal"].get("id")
            out.append(
                _row_open("A", did, interactive)
                + _checkbox_td("A", did, interactive)
                + _td(_deal_title_link(r["deal"]))
                + _td(contact_html)
                + _td(iqf_html)
                + _td(cef_html)
                + _td(_commission_cell(r["deal"]))
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── B. CLOSE ──────────────────────────────────────────────────────────
    out.append(_section_open(
        "chad-2-close", "2. CLOSE: Move toward finish line", interactive,
    ))
    if not to_close:
        out.append(_muted_p("(Nothing to close.)"))
    else:
        out.append(_open_table())
        out.append(_header_row([
            "Stage", "Deal Title", ("Buyer", "Seller"),
            "IQF", "CEF", "Agent Agreement", "Latest activity",
        ], with_checkbox=interactive))
        for r in to_close:
            co = companies_by_id.get(_normalize_id(_company_id(r["deal"])))
            buyer, seller = _split_contacts_by_role(r["people"], r["deal"])
            contact_html = _stacked_contact_cell(buyer, seller, r["deal"], co)
            iqf_html = _stacked_iqf_cell(buyer, seller, r["deal"])
            cef_html = _stacked_cef_cell(buyer, seller)
            did = r["deal"].get("id")
            out.append(
                _row_open("B", did, interactive)
                + _checkbox_td("B", did, interactive,
                               dismiss_days=7 if r.get("_sid") == STAGE_TRANSFER_NOTICE else None)
                + _td(escape(r["stage"]))
                + _td(_deal_title_link(r["deal"]))
                + _td(contact_html)
                + _td(iqf_html)
                + _td(cef_html)
                + _td(_commission_cell(r["deal"]))
                + _td(_latest_activity_cell(r.get("latest_notes") or []))
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── C. INTRODUCE ──────────────────────────────────────────────────────
    out.append(_section_open(
        "chad-3-introduce", "3. INTRODUCE: Crossed trades", interactive,
    ))
    if not crossed:
        out.append(_muted_p("(None)"))
    else:
        out.append(_open_table())
        out.append(_header_row([
            "Company", "Structure",
            "Buy", "Buy Deal ID",
            "Sell", "Sell Deal ID",
            ("Buyer", "Seller"),
            "% Diff",
        ], with_checkbox=interactive))
        for r in crossed:
            b_pc = r["buy_primary"]
            s_pc = r["sell_primary"]
            buy_co = companies_by_id.get(_normalize_id(_company_id(r["buy_deal"])))
            sell_co = companies_by_id.get(_normalize_id(_company_id(r["sell_deal"])))
            buyer_html = _single_contact_cell(b_pc, r["buy_deal"], buy_co)
            seller_html = _single_contact_cell(s_pc, r["sell_deal"], sell_co)
            contact_html = _stack2(buyer_html, seller_html)
            buy_id = r["buy_deal"].get("id")
            sell_id = r["sell_deal"].get("id")
            row_id = f"{buy_id}_{sell_id}" if buy_id and sell_id else (buy_id or sell_id)
            out.append(
                _row_open("C", row_id, interactive)
                + _checkbox_td("C", row_id, interactive)
                + _td(escape(r["company"]))
                + _td(escape(r["structure"]))
                + _td(escape(_fmt_price(r["buy_price"])))
                + _td(_deal_link(r["buy_deal"]))
                + _td(escape(_fmt_price(r["sell_price"])))
                + _td(_deal_link(r["sell_deal"]))
                + _td(contact_html)
                + _td(f"{r['pct']:+.2f}%")
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── D. EXPLORE ────────────────────────────────────────────────────────
    out.append(_section_open(
        "chad-4-explore",
        "4. EXPLORE: Update or engage close matches",
        interactive,
    ))
    if not tight:
        out.append(_muted_p("(None)"))
    else:
        groups = [
            ("BUY", "BUY trades close to ask",
             [r for r in tight if r["side"] == "BUY"]),
            ("SELL", "SELL trades close to bid",
             [r for r in tight if r["side"] == "SELL"]),
        ]
        rendered_any = False
        for side_key, sub_heading, group in groups:
            if not group:
                continue
            if rendered_any:
                out.append(SECTION_GAP_HTML)
            rendered_any = True
            out.append(
                f'<p style="{SUB_HEADING_STYLE}">{escape(sub_heading)}</p>'
            )
            if side_key == "BUY":
                labels = [
                    "Deal Title", "Stage", "Structure",
                    "Your Bid", "Market Ask", "% Diff",
                    "Contact", "IQF", "CEF",
                ]
            else:
                labels = [
                    "Deal Title", "Stage", "Structure",
                    "Your Offer", "Market Bid", "% Diff",
                    "Contact", "IQF", "CEF",
                ]
            out.append(_open_table())
            out.append(_header_row(labels, with_checkbox=interactive))
            for r in group:
                pc = r["primary"]
                n, e = _person_name_email(pc)
                iqf_html = _iqf_cell(pc, r["deal"])
                cef_html = _colorize_symbol(_person_cef(pc))
                co = companies_by_id.get(_normalize_id(_company_id(r["deal"])))
                contact_html = _contact_cell(n, email=e, person_id=pc.get("id"))
                env = _envelope_link(r["deal"], pc, co)
                if env:
                    contact_html = f"{contact_html}{env}"
                dist_extra = (
                    f" background-color:{NEG_DISTANCE_BG};"
                    if r["distance"] < 0 else ""
                )
                did = r["deal"].get("id")
                out.append(
                    _row_open("D", did, interactive)
                    + _checkbox_td("D", did, interactive)
                    + _td(_deal_title_link(r["deal"]))
                    + _td(escape(r["stage"]))
                    + _td(escape(r["structure"]))
                    + _td(escape(_fmt_price(r["your_price"])))
                    + _td(escape(_fmt_price(r["marketplace_price"])))
                    + _td(f"{r['distance'] * 100:+.2f}%", extra=dist_extra)
                    + _td(contact_html)
                    + _td(iqf_html)
                    + _td(cef_html)
                    + "</tr>"
                )
            out.append("</table>")
    out.append(_section_close())

    # ── 5. SPV MANAGERS TO WARM ───────────────────────────────────────────
    out.append(_section_open(
        "chad-5-spv-managers", "5. SPV MANAGERS TO WARM", interactive,
    ))
    out.append(_muted_p(f"{len(spv_managers_to_warm)} SPV managers to warm"))
    if not spv_managers_to_warm:
        out.append(_muted_p("(None)"))
    else:
        out.append(_open_table())
        out.append(_header_row([
            "Name", "Email", "Top SPV Manager", "# Sells", "# Buys",
        ]))
        for r in spv_managers_to_warm:
            out.append(
                "<tr>"
                + _td(_contact_cell(
                    r["name"], email=r["email"], person_id=r["person_id"]
                ))
                + _td(_email_link(r["email"]))
                + _td(escape(r["spv_value"]))
                + _td(f"{r['sell_count']}")
                + _td(f"{r['buy_count']}")
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── 6. TOP BUYERS TO WARM ─────────────────────────────────────────────
    out.append(_section_open(
        "chad-6-top-buyers", "6. TOP BUYERS TO WARM", interactive,
    ))
    out.append(_muted_p(f"{len(top_buyers_to_warm)} top buyers to warm"))
    if not top_buyers_to_warm:
        out.append(_muted_p("(None)"))
    else:
        out.append(_open_table())
        out.append(_header_row(["Name", "Email", "# Buy Interests"]))
        for r in top_buyers_to_warm:
            out.append(
                "<tr>"
                + _td(_contact_cell(
                    r["name"], email=r["email"], person_id=r["person_id"]
                ))
                + _td(_email_link(r["email"]))
                + _td(f"{r['buy_count']}")
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    out.append("</div>")  # close data-owner="chad"

    # ── Kate — Queue ──────────────────────────────────────────────────────
    out.append('<div data-owner="kate">')
    out.append(
        f'<h1 style="{QUEUE_H1_STYLE_TOP}">Kate — Queue</h1>'
    )

    # ── E. POST: Trades to post to Notice ─────────────────────────────────
    out.append(_section_open(
        "kate-1-post", "1. POST: Trades to post to Notice", interactive,
    ))
    if not to_post:
        out.append(_muted_p("(Nothing to post — clean book!)"))
    else:
        out.append(_open_table())
        out.append(_header_row([
            "Action", "Deal Title", "Volume", "Structure", "Shares", "Price",
        ], with_checkbox=interactive))
        for r in to_post:
            d = r["deal"]
            did = d.get("id")
            volume = _cf_number(d, CF_MAX_SIZE)
            shares = _cf_number(d, CF_SHARE_COUNT)
            price = _post_price(d)
            out.append(
                _row_open("E", did, interactive)
                + _checkbox_td("E", did, interactive)
                + _td(escape(r["action"]))
                + _td(_deal_title_link(d))
                + _td(escape(_fmt_price_or_dash(volume)))
                + _td(escape(_post_structure_label(d)))
                + _td(escape(_fmt_int_or_dash(shares)))
                + _td(escape(_fmt_price_or_dash(price)))
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── 2. Leads to Revive ────────────────────────────────────────────────
    out.append(_section_open(
        "kate-2-leads", "2. Leads to Revive", interactive,
    ))
    out.append(_muted_p(
        f"Priority Leads (active deals, no work email): {len(priority_leads)}"
    ))
    out.append(_muted_p(
        f"Total Newsletter Recipients: {newsletter_recipient_count}"
    ))
    out.append(_muted_p(
        f"Leads to Revive: {leads_to_revive_count}"
    ))

    priority_heading_style = (
        "font-size:14px; font-weight:600; color:#374151;"
        " margin:16px 0 6px 0;"
    )
    out.append(
        f'<p style="{priority_heading_style}">'
        'Priority — active deals with no work email (research these first)'
        '</p>'
    )
    if not priority_leads:
        out.append(_muted_p("(No priority leads — clean book!)"))
    else:
        out.append(_open_table())
        out.append(_header_row([
            "Name", "# Active Deals", "Companies", "LinkedIn",
        ], with_checkbox=interactive))
        for r in priority_leads:
            name_href = PIPELINE_PERSON_URL.format(
                escape(str(r["person_id"]), quote=True)
            )
            name_cell = (
                f'<a href="{name_href}" style="{LINK_STYLE_500}">'
                f'{escape(r["name"])}</a>'
            )
            companies = r["companies"]
            if len(companies) > 3:
                companies_text = (
                    ", ".join(companies[:3])
                    + f" + {len(companies) - 3} more"
                )
            else:
                companies_text = ", ".join(companies)
            if r["linked_in_url"]:
                li_cell = (
                    f'<a href="{escape(r["linked_in_url"], quote=True)}"'
                    f' style="{LINK_STYLE_500}">LinkedIn</a>'
                )
            else:
                li_cell = ""
            pid = r["person_id"]
            out.append(
                _row_open("Fpri", pid, interactive)
                + _checkbox_td("Fpri", pid, interactive)
                + _td(name_cell)
                + _td(escape(str(r["active_deal_count"])))
                + _td(escape(companies_text))
                + _td(li_cell)
                + "</tr>"
            )
        out.append("</table>")
        out.append(SECTION_GAP_HTML)

    if not leads:
        out.append(_muted_p("(No leads to revive — clean book!)"))
    else:
        out.append(_open_table())
        out.append(_header_row([
            "Reason", "Name", "Pipeline", "LinkedIn", "Company",
        ], with_checkbox=interactive))
        for r in leads:
            person_href = PIPELINE_PERSON_URL.format(
                escape(str(r["person_id"]), quote=True)
            )
            pipeline_cell = (
                f'<a href="{person_href}" style="{LINK_STYLE_500}">open</a>'
            )
            if r["linked_in_url"]:
                li_cell = (
                    f'<a href="{escape(r["linked_in_url"], quote=True)}"'
                    f' style="{LINK_STYLE_500}">LinkedIn</a>'
                )
            else:
                li_cell = ""
            if r["company_website"] and r["company_name"]:
                co_cell = (
                    f'<a href="{escape(r["company_website"], quote=True)}"'
                    f' style="{LINK_STYLE}">{escape(r["company_name"])}</a>'
                )
            elif r["company_name"]:
                co_cell = escape(r["company_name"])
            else:
                co_cell = ""
            pid = r["person_id"]
            out.append(
                _row_open("Fmain", pid, interactive)
                + _checkbox_td("Fmain", pid, interactive)
                + _td(escape(r["reason"]))
                + _td(escape(r["name"]))
                + _td(pipeline_cell)
                + _td(li_cell)
                + _td(co_cell)
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── I. POPULAR SPV MANAGERS: Not yet on newsletter ────────────────────
    out.append(_section_open(
        "kate-3-popular-spv",
        "3. POPULAR SPV MANAGERS: Not yet on newsletter",
        interactive,
    ))
    out.append(_muted_p(
        "SPV managers who hold top-10 most-wanted securities but aren't "
        "receiving the weekly newsletter"
    ))
    if not popular_spv_managers:
        out.append(_muted_p(
            "(None — all qualifying SPV managers are already subscribed.)"
        ))
    else:
        out.append(_open_table())
        out.append(_header_row([
            "Name", "Email", "Top SPV Manager", "Holdings in Top 10",
        ]))
        for r in popular_spv_managers:
            holdings_html = escape(", ".join(r["holdings"]))
            out.append(
                "<tr>"
                + _td(_contact_cell(
                    r["name"], email=r["email"], person_id=r["person_id"]
                ))
                + _td(_email_link(r["email"]))
                + _td(escape(r["spv_value"]))
                + _td(holdings_html)
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    out.append("</div>")  # close data-owner="kate"

    if interactive:
        out.append("</div>")
        out.append(INTERACTIVE_JS)
        out.append("</body></html>")
    else:
        out.append("</div></body></html>")
    return "".join(out)


def _build_brief_html(interactive):
    now = datetime.now(timezone.utc)
    date_str = f"{now:%B} {now.day}, {now.year}"

    s3 = boto3.client("s3", region_name=S3_REGION)
    deals_doc = _fetch_json(s3, "deals.json")
    companies_doc = _fetch_json(s3, "companies.json")
    people_doc = _fetch_json(s3, "people.json")
    security_names = _load_security_names(s3)
    deals = deals_doc.get("deals", []) or []
    companies = companies_doc.get("companies", []) or []
    people = people_doc.get("people", []) or []

    people_by_id = {}
    for p in people:
        nid = _normalize_id(p.get("id"))
        if nid is not None:
            people_by_id[nid] = p
    companies_by_id = {}
    for c in companies:
        cid = _normalize_id(c.get("id"))
        if cid is not None:
            companies_by_id[cid] = c

    # Pipeline JWT for Section B's per-row "Latest activity" lookups.
    # Fetched once per brief render; failure (missing IAM permission, S3
    # error, missing key) falls back to None and notes silently become
    # em dashes in the rendered cells.
    try:
        jwt = get_jwt()
    except Exception:
        jwt = None

    crossed = _build_crossed(deals, people_by_id)
    tight = _build_tight(deals, companies, people_by_id)
    to_close = _build_to_close(deals, now, people_by_id, jwt=jwt)
    to_invoice = _build_to_invoice(deals, now, people_by_id)
    to_post = _build_to_post(deals)
    priority_leads = _build_priority_leads_no_email(people, deals)
    priority_pids = {r["person_id"] for r in priority_leads}
    leads = _build_leads_to_revive(people, companies, priority_pids)
    leads_to_revive_count = len(leads)
    newsletter_recipient_count = _count_newsletter_recipients(people)
    spv_managers_to_warm = _build_spv_managers_to_warm(people)
    top_buyers_to_warm = _build_top_buyers_to_warm(people)
    popular_spv_managers = _build_popular_spv_managers(people, security_names)

    body_html = _render_html(
        crossed, tight, to_close, to_invoice, leads,
        newsletter_recipient_count, leads_to_revive_count, date_str,
        companies_by_id, priority_leads, to_post,
        spv_managers_to_warm, top_buyers_to_warm, popular_spv_managers,
        interactive=interactive,
    )

    counts = {
        "crossed": len(crossed),
        "tight": len(tight),
        "to_close": len(to_close),
        "to_invoice": len(to_invoice),
        "to_post": len(to_post),
        "leads_to_revive": leads_to_revive_count,
        "priority_leads_no_email": len(priority_leads),
        "newsletter_recipients": newsletter_recipient_count,
        "spv_managers_to_warm": len(spv_managers_to_warm),
        "top_buyers_to_warm": len(top_buyers_to_warm),
        "popular_spv_managers": len(popular_spv_managers),
        "deals": len(deals),
        "companies": len(companies),
        "people": len(people),
    }
    return body_html, date_str, counts


def handle_http_request(event):
    params = event.get("queryStringParameters") or {}
    key = params.get("key") if isinstance(params, dict) else None
    expected = os.environ.get("BRIEF_PAGE_KEY")
    if not expected or key != expected:
        return {"statusCode": 403, "body": "Forbidden"}

    body_html, _date_str, _counts = _build_brief_html(interactive=True)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": body_html,
    }


def lambda_handler(event, context):
    http = (event.get("requestContext") or {}).get("http")
    if http:
        return handle_http_request(event)

    body_html, date_str, counts = _build_brief_html(interactive=False)
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
        "counts": counts,
    }
