import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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
NUDGE_URL = "https://ak5zolfpynhrimrsuw5rbjchwu0ktexz.lambda-url.us-east-1.on.aws/"
NUDGE_KEY = "YUARqVzldaiY4P8EZA855faT"

MASTER_CSS_URL = "https://s3.us-east-1.amazonaws.com/main.css/master.css"

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
CF_NEXUS = "custom_label_3751449"
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
CF_COMPANY_HIGH_PRIORITY = "custom_label_4002734"
HP_SPV_SELLER = 7190470     # High Priority: "Source SPV Seller"
HP_DIRECT_SELLER = 7190471  # High Priority: "Source Direct Seller"
CF_AGENT_AGREEMENT = "custom_label_3714334"
CF_TOP_SPV_MANAGER = "custom_label_3948282"
CF_SEC_PRIORITY = "custom_label_3912746"
CF_INVESTOR_LEVEL = "custom_label_3923758"
CF_TRANSACTOR_TYPE = "custom_label_3759163"
CF_TICKET_SIZE = "custom_label_3052210"
CF_NOTICE = "custom_label_3815544"
CF_MAX_SIZE = "custom_label_3064645"
CF_SHARE_COUNT = "custom_label_3070843"

OPT_SELL = 5011675
OPT_BUY = 5077819
OPT_STRUCT_DIRECT = 6250090
OPT_STRUCT_FUND = 5077906
OPT_NEXUS_DIRECT = 6460632
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
BIG_BID_MIN = 1000000

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


def _single_contact_cell(person, deal, company, interactive=False):
    if not isinstance(person, dict):
        return ""
    n, e = _person_name_email(person)
    if not n and not e:
        return ""
    cell = _contact_cell(n, email=e, person_id=person.get("id"),
                         interactive=interactive)
    env = _envelope_link(deal, person, company, interactive=interactive)
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


def _stacked_contact_cell(buyer, seller, deal, company, interactive=False):
    top = _single_contact_cell(buyer, deal, company, interactive=interactive) if buyer else ""
    bottom = _single_contact_cell(seller, deal, company, interactive=interactive) if seller else ""
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


def _build_crm_updates(interactive):
    items = _load_pending_updates()
    today_s = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pending, awaiting = [], []
    for key, it in items.items():
        st = (it.get("status") or "pending").lower()
        rec = dict(it)
        rec["_key"] = key
        rec["_calc"] = _compute_update(it)
        if st == "pending":
            snooze = it.get("snoozed_until") or ""
            if snooze and snooze > today_s:
                continue
            pending.append(rec)
        elif st == "written" and not it.get("pps_confirmed"):
            snooze = it.get("snoozed_until") or ""
            if snooze and snooze > today_s:
                continue
            rec["_needs_lookup"] = not it.get("written_pps")
            awaiting.append(rec)
    pending.sort(key=lambda r: r.get("date") or "", reverse=True)
    seen_cos = set()
    deduped = []
    for r in pending:
        cid = r.get("co_id")
        if cid and cid in seen_cos:
            continue
        if cid:
            seen_cos.add(cid)
        deduped.append(r)
    pending = deduped
    awaiting.sort(key=lambda r: r.get("date") or "", reverse=True)
    return pending, awaiting


CRM_LR_VAL_FIELD = "custom_label_3790429"
CRM_LR_PPS_FIELD = "custom_label_3064363"
CRM_LR_DATE_FIELD = "custom_label_3826032"
CRM_LR_SERIES_FIELD = "custom_label_3914626"
CRM_CATALYST_FIELD = "custom_label_3999603"
CRM_ORG_TYPE_FIELD = "custom_label_625142"
CRM_ORG_TYPE_TRADED_ISSUER = 5103523


def _handle_crm_post(body):
    """Write one queued valuation update to Pipeline. POST only."""
    def _fail(msg, code=400):
        return {"statusCode": code,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": msg})}

    key = body.get("key")
    if not key:
        return _fail("missing key")

    items = _load_pending_updates()
    item = items.get(key)
    if not item:
        return _fail("item not found in queue")
    _st = (item.get("status") or "pending").lower()
    if _st != "pending" and not (_st == "written" and not item.get("pps_confirmed")):
        return _fail("item already posted")

    co_id = item.get("co_id")
    if not co_id:
        return _fail("item has no company id")

    try:
        jwt = get_jwt()
    except Exception as e:
        return _fail(f"could not load JWT: {e}", 500)

    # Re-read the live record so we never write from a stale snapshot.
    cur = call_pipeline_api("GET", f"/companies/{co_id}.json", jwt=jwt)
    if cur.get("status") != 200 or not isinstance(cur.get("data"), dict):
        return _fail(f"could not read company {co_id}: {cur.get('status')}", 500)
    record = cur["data"]
    cur_cf = record.get("custom_fields") or {}

    fresh = dict(item)
    fresh["cur_lr_val"] = cur_cf.get(CRM_LR_VAL_FIELD)
    fresh["cur_lr_pps"] = cur_cf.get(CRM_LR_PPS_FIELD)
    calc = _compute_update(fresh)

    typed_pps = _pu_float(body.get("pps"))
    pps_to_write = typed_pps if typed_pps else calc["new_pps"]
    pps_is_estimate = (typed_pps is None and calc["pps_source"] == "derived")

    series_in = (body.get("series") or "").strip()

    fields = {}
    if calc["new_val_bn"]:
        fields[CRM_LR_VAL_FIELD] = round(calc["new_val_bn"], 4)
    if pps_to_write:
        fields[CRM_LR_PPS_FIELD] = round(pps_to_write, 4)
    if item.get("date"):
        fields[CRM_LR_DATE_FIELD] = item["date"]
    if series_in:
        fields[CRM_LR_SERIES_FIELD] = series_in

    catalyst = (item.get("catalyst") or "").strip()
    if catalyst and pps_is_estimate:
        catalyst = f"{catalyst} - PPS Estimated"
    if catalyst:
        fields[CRM_CATALYST_FIELD] = catalyst

    org = cur_cf.get(CRM_ORG_TYPE_FIELD)
    org_list = list(org) if isinstance(org, list) else ([org] if org else [])
    org_ids = []
    for o in org_list:
        try:
            org_ids.append(int(o))
        except (TypeError, ValueError):
            pass
    if CRM_ORG_TYPE_TRADED_ISSUER not in org_ids:
        org_ids.append(CRM_ORG_TYPE_TRADED_ISSUER)
        fields[CRM_ORG_TYPE_FIELD] = org_ids

    if not fields:
        return _fail("nothing to write")

    res = call_pipeline_api("PUT", f"/companies/{co_id}.json",
                            {"company": {"custom_fields": fields}}, jwt=jwt)
    if res.get("status") != 200:
        return _fail(f"Pipeline write failed: {res.get('status')} {res.get('data')}", 500)

    item["status"] = "written"
    item["written_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for k2, it2 in items.items():
        if k2 == key:
            continue
        if it2.get("co_id") == co_id and (it2.get("status") or "pending").lower() == "pending":
            it2["status"] = "superseded"
    item["written_val_bn"] = fields.get(CRM_LR_VAL_FIELD)
    item["written_pps"] = fields.get(CRM_LR_PPS_FIELD)
    item["pps_estimated"] = bool(pps_is_estimate)
    if pps_to_write and not pps_is_estimate:
        item["pps_confirmed"] = True
    items[key] = item
    try:
        _log_completion("kate", "valuation_posted", key)
    except Exception:
        pass
    try:
        boto3.client("s3").put_object(
            Bucket=S3_BUCKET, Key="pending-updates.json",
            Body=json.dumps({"items": items}, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:
        return {"statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": True,
                                    "warning": f"CRM written but queue not updated: {e}"})}

    return {"statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": True})}


def _save_pending_updates(items):
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET, Key="pending-updates.json",
        Body=json.dumps({"items": items}, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def _handle_crm_snooze(body):
    key = body.get("key")
    items = _load_pending_updates()
    item = items.get(key)
    if not item:
        return {"statusCode": 400, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": "item not found"})}
    until = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
    item["snoozed_until"] = until
    items[key] = item
    try:
        _save_pending_updates(items)
    except Exception as e:
        return {"statusCode": 500, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": f"save failed: {e}"})}
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": True, "snoozed_until": until})}


COMPLETIONS_KEY = "daily-brief-completions.json"


def _load_completions():
    """Read the completions log {date: {owner: [events]}}. Never raises."""
    try:
        obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=COMPLETIONS_KEY)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _log_completion(owner, kind, key, done=True):
    """Append (or on done=False remove) a completion event for today."""
    owner = (owner or "chad").lower()
    if owner not in ("chad", "kate"):
        owner = "chad"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _load_completions()
    day = data.setdefault(today, {})
    events = day.setdefault(owner, [])
    events = [e for e in events if not (e.get("kind") == kind and e.get("key") == key)]
    if done:
        events.append({"kind": kind, "key": key,
                       "at": datetime.now(timezone.utc).strftime("%H:%M")})
    day[owner] = events
    # keep 60 days
    for d in sorted(data.keys())[:-60]:
        data.pop(d, None)
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET, Key=COMPLETIONS_KEY,
        Body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def _handle_row_done(body):
    key = body.get("key") or ""
    if not key:
        return {"statusCode": 400, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": "missing key"})}
    try:
        _log_completion(body.get("owner"), "row", key, done=bool(body.get("done", True)))
    except Exception as e:
        return {"statusCode": 500, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": f"log failed: {e}"})}
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": True})}


def _handle_lead_email(body):
    """Kate found a new work email: move old email -> email2, set new email,
    mark Email Status In Progress, log completion."""
    def _fail(msg, code=400):
        return {"statusCode": code, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": msg})}
    pid = str(body.get("person_id") or "").strip()
    new_email = (body.get("email") or "").strip()
    if not pid.isdigit():
        return _fail("missing person_id")
    if "@" not in new_email or "." not in new_email.split("@")[-1]:
        return _fail("enter a valid email address")
    try:
        jwt = get_jwt()
    except Exception as e:
        return _fail(f"token error: {e}", 500)
    cur = call_pipeline_api("GET", f"/people/{pid}.json", jwt=jwt)
    if cur.get("status") != 200:
        return _fail(f"Pipeline read failed: {cur.get('status')}", 502)
    old_email = (cur["data"].get("email") or "").strip()
    person = {"email": new_email,
              "custom_fields": {CF_EMAIL_STATUS: 4923706}}
    if old_email and old_email.lower() != new_email.lower():
        person["email2"] = old_email
    res = call_pipeline_api("PUT", f"/people/{pid}.json", {"person": person}, jwt=jwt)
    if res.get("status") != 200:
        return _fail(f"Pipeline write failed: {res.get('status')} {res.get('data')}", 502)
    try:
        _log_completion("kate", "email_updated", f"person:{pid}")
    except Exception:
        pass
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": True})}


def _handle_crm_delete(body):
    key = body.get("key")
    items = _load_pending_updates()
    item = items.get(key)
    if not item:
        return {"statusCode": 400, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": "item not found"})}
    item["status"] = "dismissed"
    item["dismissed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items[key] = item
    try:
        _log_completion("kate", "valuation_deleted", key)
    except Exception:
        pass
    try:
        _save_pending_updates(items)
    except Exception as e:
        return {"statusCode": 500, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": f"save failed: {e}"})}
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": True})}


def _handle_crm_confirm(body):
    """Write only the confirmed PPS for an already-posted item. POST only."""
    def _fail(msg, code=400):
        return {"statusCode": code, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": msg})}
    key = body.get("key")
    if not key:
        return _fail("missing key")
    pps = _pu_float(body.get("pps"))
    if not pps:
        return _fail("enter a PPS value first")
    items = _load_pending_updates()
    item = items.get(key)
    if not item:
        return _fail("item not found in queue")
    co_id = item.get("co_id")
    if not co_id:
        return _fail("item has no company id")
    try:
        jwt = get_jwt()
    except Exception as e:
        return _fail(f"could not load JWT: {e}", 500)
    res = call_pipeline_api("PUT", f"/companies/{co_id}.json",
                            {"company": {"custom_fields":
                             {CRM_LR_PPS_FIELD: round(pps, 4)}}}, jwt=jwt)
    if res.get("status") != 200:
        return _fail(f"Pipeline write failed: {res.get('status')} {res.get('data')}", 500)
    item["written_pps"] = round(pps, 4)
    item["pps_confirmed"] = True
    item["pps_estimated"] = False
    item["pps_confirmed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items[key] = item
    try:
        _save_pending_updates(items)
    except Exception as e:
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": True,
                                    "warning": f"PPS written but queue not updated: {e}"})}
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": True})}


def _load_pending_updates():
    """Read the valuation-scanner update queue. Never raises."""
    try:
        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket=S3_BUCKET, Key="pending-updates.json")
        data = json.loads(obj["Body"].read())
        return data.get("items") or {}
    except Exception:
        return {}


def _pu_float(v):
    try:
        if v in (None, ""):
            return None
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _compute_update(item):
    """Work out what would be written for one queue item."""
    val_usd = _pu_float(item.get("valuation_usd"))
    new_val_bn = val_usd / 1e9 if val_usd else None

    reported_pps = _pu_float(item.get("pps_usd"))
    cur_val = _pu_float(item.get("cur_lr_val"))
    cur_pps = _pu_float(item.get("cur_lr_pps"))
    raise_usd = _pu_float(item.get("raise_amount_usd"))

    new_pps = None
    pps_source = "none"
    if reported_pps:
        new_pps = reported_pps
        pps_source = "reported"
    elif val_usd and cur_val and cur_pps and cur_val > 0:
        basis_usd = val_usd
        if raise_usd and raise_usd < val_usd:
            basis_usd = val_usd - raise_usd
        new_pps = cur_pps * (basis_usd / 1e9) / cur_val
        pps_source = "derived"

    return {
        "new_val_bn": new_val_bn,
        "new_pps": new_pps,
        "pps_source": pps_source,
        "cur_val": cur_val,
        "cur_pps": cur_pps,
    }


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


def _fetch_person_won_total(person_id, jwt):
    """Pipeline's /people.json list endpoint stubs won_deals_total to 0;
    only /people/<id>.json returns the real computed rollup. Used to
    enrich the top-30 buyers shown in section 5."""
    if person_id in (None, "") or not jwt:
        return None
    endpoint = f"/people/{person_id}.json"
    result = call_pipeline_api("GET", endpoint, jwt=jwt, timeout=3)
    if result.get("status") != 200:
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    return data.get("won_deals_total")


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


def _deal_link(deal, interactive=False):
    did = deal.get("id")
    if did in (None, ""):
        return ""
    s = str(did)
    href = PIPELINE_DEAL_URL.format(escape(s, quote=True))
    if interactive:
        return f'<a href="{href}">{escape(s)}</a>'
    return f'<a href="{href}" style="{LINK_STYLE}">{escape(s)}</a>'


def _deal_title_link(deal, interactive=False):
    title = _deal_title(deal)
    did = deal.get("id")
    if did in (None, ""):
        return escape(title)
    href = PIPELINE_DEAL_URL.format(escape(str(did), quote=True))
    if interactive:
        return f'<a href="{href}">{escape(title)}</a>'
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


def _envelope_link(deal, person, company, interactive=False):
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

    paragraphs = [
        f"Dear {first_name}:",
        "I have a quick question about your indication: "
        f"{TRADES_DEAL_URL.format(deal_id)}",
    ]
    if your_price is not None:
        if market_price is not None:
            paragraphs.append(
                f"I recall your price was {_fmt_price(your_price)} "
                f"and I see the market price is {_fmt_price(market_price)}."
            )
        else:
            paragraphs.append(f"I recall your price was {_fmt_price(your_price)}.")

    if side == "BUY" and _cf_option_id(person, CF_IQF) != OPT_IQF_YES:
        paragraphs.append(
            "I noticed that we don't have an Investor Qualification Form on file. "
            "Can you take a moment to fill this out so that you're all papered up "
            "and ready to close?"
        )
        paragraphs.append(
            f"Entity: {IQF_URL_ENTITY}\r\nNatural person: {IQF_URL_NATURAL}"
        )
    elif side == "SELL" and _cf_option_id(person, CF_CEF) != OPT_CEF_YES:
        paragraphs.append(
            "I have some inquiries but would need this form to move forward. "
            "Can you take a moment to fill it out?"
        )
        paragraphs.append(
            f"Entity: {CEF_URL_ENTITY}\r\nNatural person: {CEF_URL_NATURAL}"
        )

    # Blank line between paragraphs; the Entity / Natural-person links stay
    # together as a single block.
    body = "\r\n\r\n".join(paragraphs)
    if interactive:
        # Open a compose window in Outlook on the web (Microsoft 365) rather
        # than relying on the browser's default mailto handler.
        href = (
            "https://outlook.office.com/mail/deeplink/compose"
            f"?to={quote(email, safe='@')}"
            f"&subject={quote(subject, safe='')}"
            f"&body={quote(body, safe='')}"
        )
        return (
            f'&nbsp;<a href="{escape(href, quote=True)}"'
            ' target="_blank" rel="noopener" title="Email">✉</a>'
        )
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


def _nudge_link(deal_id, interactive=False):
    if deal_id in (None, ""):
        return ""
    href = f"{NUDGE_URL}?deal_id={quote(str(deal_id), safe='')}&key={quote(NUDGE_KEY, safe='')}"
    if interactive:
        return (
            f'&nbsp;<a href="{escape(href, quote=True)}"'
            ' target="_blank" rel="noopener"'
            ' title="Nudge client to update or cancel">🔔</a>'
        )
    return (
        f'<a href="{escape(href, quote=True)}"'
        ' style="color:#2563eb; text-decoration:none; margin-left:6px;"'
        ' target="_blank" rel="noopener" title="Nudge client to update or cancel">🔔</a>'
    )


def _contact_cell(name, email=None, person_id=None, interactive=False):
    label = name or email or ""
    if not label:
        return ""
    if person_id in (None, ""):
        return escape(label)
    href = PIPELINE_PERSON_URL.format(escape(str(person_id), quote=True))
    if interactive:
        return f'<a href="{href}">{escape(label)}</a>'
    return f'<a href="{href}" style="{LINK_STYLE_500}">{escape(label)}</a>'


def _email_link(email, interactive=False):
    if not email:
        return ""
    href = f"mailto:{quote(email, safe='@')}"
    if interactive:
        return f'<a href="{escape(href, quote=True)}">{escape(email)}</a>'
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


def _people_cells(people, deal=None, company=None, interactive=False):
    entries = []
    for p in people:
        n, e = _person_name_email(p)
        if not n and not e:
            continue
        contact_link = _contact_cell(n, email=e, person_id=p.get("id"),
                                     interactive=interactive)
        annotation = _buyer_seller_annotation(deal, p) if deal is not None else ""
        envelope = (
            _envelope_link(deal, p, company, interactive=interactive)
            if deal is not None else ""
        )
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


def _header_row(labels, with_checkbox=False, interactive=False):
    n = len(labels)
    pct = 100.0 / n if n else 100.0
    cells = []
    if with_checkbox:
        if interactive:
            cells.append('<th></th>')
        else:
            cells.append(f'<th style="width:32px; {TH_BASE}"></th>')
    for lbl in labels:
        if isinstance(lbl, (list, tuple)):
            inner = "".join(
                f'<div style="line-height:1.3;">{escape(line)}</div>'
                for line in lbl
            )
        else:
            inner = escape(lbl)
        if interactive:
            cells.append(f'<th>{inner}</th>')
        else:
            style = f"width:{pct:.4f}%; {TH_BASE}"
            cells.append(f'<th style="{style}">{inner}</th>')
    return "<tr>" + "".join(cells) + "</tr>"


def _row_open(section, row_id, interactive):
    if interactive and row_id not in (None, ""):
        key = f"{section}:{row_id}"
        return f'<tr data-row-key="{escape(key, quote=True)}">'
    return "<tr>"


def _email_update_td(section, pid, interactive):
    if not interactive or pid in (None, ""):
        return ""
    key = f"{section}:{pid}"
    return (
        '<td style="white-space:nowrap;">'
        f'<input type="email" class="lead-email" data-row-key="{escape(key, quote=True)}"'
        f' data-person-id="{escape(str(pid), quote=True)}" placeholder="new work email"'
        ' style="font-family:inherit; font-size:12px; width:190px; padding:3px 6px;'
        ' border:1px solid #d1d5db; border-radius:6px;" />'
        f' <button type="button" class="lead-email-update" data-row-key="{escape(key, quote=True)}"'
        ' style="padding:3px 10px; border-radius:999px; cursor:pointer;'
        ' border:1px solid #b8c2cc; background:#CCDBEA; font-family:inherit;'
        ' font-size:12px; font-weight:500;">Update</button>'
        '</td>'
    )


def _checkbox_td(section, row_id, interactive, dismiss_days=None):
    if not interactive or row_id in (None, ""):
        return ""
    key = f"{section}:{row_id}"
    if dismiss_days:
        return (
            '<td>'
            f'<input type="checkbox" class="row-dismiss"'
            f' data-row-key="{escape(key, quote=True)}"'
            f' data-dismiss-days="{int(dismiss_days)}" />'
            '</td>'
        )
    return (
        '<td>'
        f'<select class="row-snooze" data-row-key="{escape(key, quote=True)}"'
        ' style="font-family:inherit; font-size:11px; color:#6b7280;'
        ' border:1px solid #d1d5db; border-radius:6px; padding:2px;'
        ' background:#fff;">'
        '<option value="" selected>&ndash;</option>'
        '<option value="1">1d</option>'
        '<option value="3">3d</option>'
        '<option value="7">1w</option>'
        '<option value="30">30d</option>'
        '</select>'
        '</td>'
    )


def _td(content, extra="", interactive=False):
    if interactive:
        if extra:
            return f'<td style="{extra.lstrip()}">{content}</td>'
        return f'<td>{content}</td>'
    style = TD_STYLE + extra
    return f'<td style="{style}">{content}</td>'


def _section_heading(text, interactive=False):
    if interactive:
        return f'<h2>{escape(text)}</h2>'
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
            f'<h2>{hide_btn}{escape(text)}</h2>'
        )
    return (
        f'<div class="section" data-section-key="{escape(key, quote=True)}">'
        f'<h2 style="{H2_STYLE}">{hide_btn}{escape(text)}</h2>'
    )


def _section_close():
    return "</div>"


def _muted_p(text, interactive=False):
    if interactive:
        return f'<p>{escape(text)}</p>'
    return f'<p style="{SUB_SUMMARY_STYLE}">{escape(text)}</p>'


def _open_table(interactive=False):
    if interactive:
        return '<table>'
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
            buy_structs = _cf_option_ids(buy, CF_STRUCTURE) & set(STRUCTURE_LABELS)
            if not buy_structs:
                continue
            for n, sell in co["sells"]:
                if g < n or n == 0:
                    continue
                sell_structs = _cf_option_ids(sell, CF_STRUCTURE) & set(STRUCTURE_LABELS)
                shared = buy_structs & sell_structs
                if not shared:
                    continue
                if not _ticket_compat(buy, sell):
                    continue
                pct = (g - n) / n * 100
                if best is None or pct > best["pct"]:
                    best = {
                        "company": co["name"],
                        "structure": ", ".join(
                            sorted(STRUCTURE_LABELS[s] for s in shared)
                        ),
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
        if sid not in MARKET_STAGES or sid == STAGE_MATCHED:
            continue
        if _cf_option_id(d, CF_NEXUS) != OPT_NEXUS_DIRECT:
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
            max_size = _cf_number(d, CF_TICKET_MAX)
            big_bid = max_size is not None and max_size >= BIG_BID_MIN
            dist = (ask - gross) / ask if (ask and gross is not None) else None
            near = dist is not None and abs(dist) <= TIGHT_PCT
            if not near and not big_bid:
                continue
            rows.append({
                "company": co_name,
                "side": "BUY",
                "stage": stage_label,
                "structure": structure,
                "your_price": gross,
                "marketplace_price": ask,
                "distance": dist,
                "big_bid": big_bid and not near,
                "_max_size": max_size,
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
    rows.sort(key=lambda r: (
        0 if r["side"] == "SELL" else 1,
        r["distance"] is None,
        r["distance"] if r["distance"] is not None else 0.0,
        -(r.get("_max_size") or 0.0),
    ))
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
            "cef": _person_cef(p),
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


# Top-buyers-to-warm ranking signals (higher = better buyer)
SEC_PRIORITY_RANK = {6919452: 3, 6919453: 2, 6919454: 1, 6926715: 0}  # High/Med/Low/None
ENTITY_RANK = {
    6484810: 3, 6484811: 3,            # Natural Person, Family Office
    6484813: 2,                        # Wealth Advisor
    6484812: 1, 7037492: 1, 6484815: 1,  # Institution, Hedge Fund, Corporation
    6484808: 0,                        # VC or PE Fund
}
EXCLUDE_TRANSACTOR = {6577160, 6888332, 6716196, 6484809, 6859893}  # intermediaries/holders/syndicator
INCLUDE_TRANSACTOR = {6484811, 6484810, 7037492, 6484812, 6484813}  # Family Office, Natural Person, Hedge Fund, Institution, Wealth Advisor
INVESTOR_LEVEL_RANK = {6950564: 3, 6950563: 2, 7162165: 1, 6950561: 0}  # QP/Accredited/Substantive/Unknown
TICKET_ORDER = [6870210, 6631962, 5014552, 5014555, 5014558, 5014561, 5014564, 5014567, 5014570]  # small -> large

TICKET_LABELS = {
    6870210: "> 100K",
    6631962: "$250K",
    5014552: "- $1M",
    5014555: "$1M - $5M",
    5014558: "$5M - $10M",
    5014561: "$10M - $25M",
    5014564: "$25M - $50M",
    5014567: "$50M - $100M",
    5014570: "$100M +",
}

TRANSACTOR_TYPE_LABELS = {
    6484810: "Natural Person",
    6484811: "Family Office",
    6484813: "Wealth Advisor",
    6484812: "Institution",
    7037492: "Hedge Fund",
    6484808: "VC or PE Fund",
    6484815: "Corporation",
    6484809: "Ex-Employee Holder",
    6716196: "Employee Holder",
    6892622: "Employee Holder - VIP",
    6484814: "SPV Manager",
    6859893: "Syndicator",
    6577160: "Intermediary - Co-Broker",
    6888332: "Intermediary - Foreign Finder",
    6888333: "Intermediary - Other",
}


def _ticket_rank(p):
    ids = _cf_option_ids(p, CF_TICKET_SIZE)
    best = -1
    for i, opt in enumerate(TICKET_ORDER):
        if opt in ids:
            best = i
    return best


def _build_top_buyers_to_warm(people, jwt=None):
    rows = []
    for p in people:
        buy_ids = _cf_option_ids(p, CF_PERSON_BUY_INTERESTS)
        if not buy_ids:
            continue
        ttype_opt = _cf_option_id(p, CF_TRANSACTOR_TYPE)
        ttypes = _cf_option_ids(p, CF_TRANSACTOR_TYPE)
        if not (ttypes & INCLUDE_TRANSACTOR):
            continue
        sec_rank = SEC_PRIORITY_RANK.get(_cf_option_id(p, CF_SEC_PRIORITY), 0)
        entity_rank = max((ENTITY_RANK.get(o, 0) for o in ttypes), default=0)
        level_rank = INVESTOR_LEVEL_RANK.get(_cf_option_id(p, CF_INVESTOR_LEVEL), 0)
        ticket_rank = _ticket_rank(p)
        max_ticket_oid = TICKET_ORDER[ticket_rank] if ticket_rank >= 0 else None
        rows.append({
            "person_id": p.get("id"),
            "name": _person_full_name(p) or "",
            "email": _person_email(p),
            "iqf": _person_iqf(p),
            "buy_count": len(buy_ids),
            "transactor_type": TRANSACTOR_TYPE_LABELS.get(ttype_opt, ""),
            "max_ticket": TICKET_LABELS.get(max_ticket_oid, "") if max_ticket_oid else "",
            "won_deals_total": 0.0,
            "_sort": (-sec_rank, -entity_rank, -level_rank, -ticket_rank, len(buy_ids)),
        })
    rows.sort(key=lambda r: (r["_sort"], r["name"].lower()))
    rows = rows[:30]
    for r in rows:
        live = _fetch_person_won_total(r["person_id"], jwt)
        try:
            r["won_deals_total"] = float(live or 0)
        except (TypeError, ValueError):
            r["won_deals_total"] = 0.0
    return rows


def _build_priority_names(companies, people, security_names):
    """Companies flagged High Priority (Source SPV/Direct Seller), each with the
    count and names of people in the CRM who currently hold that company.

    Holders are keyed by the "holds" dropdown entry id (unique per security),
    which security_names maps to a display name. A flagged company is bridged to
    that entry id by matching the company name to security_names."""
    flagged = {HP_SPV_SELLER, HP_DIRECT_SELLER}
    hold_id_by_name = {}
    for eid, nm in security_names.items():
        if nm:
            hold_id_by_name.setdefault(nm.strip().lower(), eid)
    rows = []
    for co in companies:
        if not (_cf_option_ids(co, CF_COMPANY_HIGH_PRIORITY) & flagged):
            continue
        cid = _normalize_id(co.get("id"))
        name = co.get("name") or f"#{cid}"
        hold_id = hold_id_by_name.get(name.strip().lower())
        if hold_id is None:
            holders = []
        else:
            holders = [
                _person_full_name(p) or ""
                for p in people
                if hold_id in _cf_option_ids(p, CF_PERSON_HOLD_INTERESTS)
            ]
        holders = sorted((h for h in holders if h), key=str.lower)
        rows.append({
            "company_id": cid,
            "name": name,
            "holder_count": len(holders),
            "holders": holders,
        })
    rows.sort(key=lambda r: (-r["holder_count"], r["name"].lower()))
    return rows


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
  const LONG_DISMISS_KEY = "dailyBriefLongDismissed";
  const DAY_MS = 24 * 60 * 60 * 1000;
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
  function readLong() {
    try { return JSON.parse(localStorage.getItem(LONG_DISMISS_KEY) || "{}"); }
    catch (e) { return {}; }
  }
  function writeLong(obj) {
    localStorage.setItem(LONG_DISMISS_KEY, JSON.stringify(obj));
  }
  function getLongDismissed() {
    const obj = readLong();
    const now = Date.now();
    let changed = false;
    for (const k of Object.keys(obj)) {
      if (!obj[k] || obj[k] < now) { delete obj[k]; changed = true; }
    }
    if (changed) writeLong(obj);
    return obj;
  }
  function addLongDismissed(key, days) {
    const obj = readLong();
    obj[key] = Date.now() + days * DAY_MS;
    writeLong(obj);
  }
  function removeLongDismissed(key) {
    const obj = readLong();
    delete obj[key];
    writeLong(obj);
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
    const longSet = new Set(Object.keys(getLongDismissed()));
    document.querySelectorAll("tr[data-row-key]").forEach(tr => {
      const key = tr.getAttribute("data-row-key");
      if (list.has(key) || longSet.has(key)) {
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
    const refreshBtn = document.getElementById("refresh-btn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function() {
        refreshBtn.disabled = true;
        refreshBtn.textContent = "Refreshing…";
        window.location.reload();
      });
    }
    document.querySelectorAll(".crm-post").forEach(btn => {
      btn.addEventListener("click", function() {
        const key = btn.getAttribute("data-key");
        const row = document.querySelector('[data-crm-row="' + CSS.escape(key) + '"]');
        const ppsEl = row ? row.querySelector(".crm-pps") : null;
        const serEl = row ? row.querySelector(".crm-series") : null;
        btn.disabled = true;
        btn.textContent = "Posting…";
        fetch(window.location.pathname + window.location.search, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: "crm_post",
            key: key,
            pps: ppsEl ? ppsEl.value : "",
            series: serEl ? serEl.value : ""
          })
        }).then(r => r.json()).then(d => {
          if (d && d.ok) {
            btn.textContent = "✓ Posted";
            btn.style.background = "#d1fae5";
            btn.style.borderColor = "#6ee7b7";
            if (row) { row.style.opacity = "0.55"; }
          } else {
            btn.disabled = false;
            btn.textContent = "Retry";
            alert("Post failed: " + ((d && d.error) || "unknown error"));
          }
        }).catch(e => {
          btn.disabled = false;
          btn.textContent = "Retry";
          alert("Post failed: " + e);
        });
      });
    });
    function crmRowAction(btn, action, confirmMsg) {
      const key = btn.getAttribute("data-key");
      if (confirmMsg && !confirm(confirmMsg)) { return; }
      const row = document.querySelector('[data-crm-row="' + CSS.escape(key) + '"]');
      btn.disabled = true;
      fetch(window.location.pathname + window.location.search, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: action, key: key })
      }).then(r => r.json()).then(d => {
        if (d && d.ok) {
          if (row) { row.style.display = "none"; }
        } else {
          btn.disabled = false;
          alert("Failed: " + ((d && d.error) || "unknown error"));
        }
      }).catch(e => {
        btn.disabled = false;
        alert("Failed: " + e);
      });
    }
    document.querySelectorAll(".crm-snooze").forEach(btn => {
      btn.addEventListener("click", function() {
        crmRowAction(btn, "crm_snooze", null);
      });
    });
    document.querySelectorAll(".crm-delete").forEach(btn => {
      btn.addEventListener("click", function() {
        crmRowAction(btn, "crm_delete", "Delete this valuation update permanently?");
      });
    });
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
    document.querySelectorAll(".daily-action-link").forEach(link => {
      link.addEventListener("click", function() {
        const section = link.closest("[data-section-key]");
        if (!section) return;
        const key = section.getAttribute("data-section-key");
        const list = getTodaySections();
        if (!list.includes(key)) list.push(key);
        setTodaySections(list);
        section.style.display = "none";
      });
    });
    document.querySelectorAll("select.row-snooze").forEach(sel => {
      sel.addEventListener("change", function() {
        const key = this.getAttribute("data-row-key");
        const tr = this.closest("tr[data-row-key]");
        const v = this.value;
        if (!v) {
          removeLongDismissed(key);
          if (tr) tr.style.display = "";
          return;
        }
        const days = parseInt(v, 10) || 1;
        addLongDismissed(key, days);
        if (tr) tr.style.display = "none";
      });
    });
    function reportDone(el, key, done) {
      const ownerEl = el.closest("[data-owner]");
      const owner = ownerEl ? ownerEl.getAttribute("data-owner") : "chad";
      fetch(window.location.pathname + window.location.search, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "row_done", key: key, owner: owner, done: done })
      }).catch(() => {});
    }
    document.querySelectorAll("button.lead-email-update").forEach(btn => {
      btn.addEventListener("click", function() {
        const key = btn.getAttribute("data-row-key");
        const tr = btn.closest("tr[data-row-key]");
        const input = tr ? tr.querySelector("input.lead-email") : null;
        const email = input ? input.value.trim() : "";
        const pid = input ? input.getAttribute("data-person-id") : "";
        if (!email) { alert("Enter the new work email first."); return; }
        btn.disabled = true;
        fetch(window.location.pathname + window.location.search, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "lead_email", person_id: pid, email: email })
        }).then(r => r.json()).then(d => {
          if (d && d.ok) {
            const list = getToday();
            if (!list.includes(key)) list.push(key);
            setToday(list);
            if (tr) tr.style.display = "none";
            updateCounter();
          } else {
            btn.disabled = false;
            alert("Failed: " + ((d && d.error) || "unknown error"));
          }
        }).catch(e => { btn.disabled = false; alert("Failed: " + e); });
      });
    });
    document.querySelectorAll("input.row-dismiss").forEach(cb => {
      cb.addEventListener("change", function() {
        const key = this.getAttribute("data-row-key");
        const daysAttr = this.getAttribute("data-dismiss-days");
        const tr = this.closest("tr[data-row-key]");
        reportDone(this, key, this.checked);
        if (daysAttr !== null) {
          if (this.checked) {
            const days = parseInt(daysAttr, 10) || 30;
            addLongDismissed(key, days);
            if (tr) tr.style.display = "none";
          } else {
            removeLongDismissed(key);
            if (tr) tr.style.display = "";
          }
          return;
        }
        const list = getToday();
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
        writeLong({});
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
                 popular_spv_managers, priority_names, interactive=False):
    pu_pending, pu_awaiting = _build_crm_updates(interactive)
    chad_count = (
        len(to_invoice) + len(to_close) + len(crossed) + len(tight)
        + len(spv_managers_to_warm) + len(top_buyers_to_warm)
    )
    kate_count = (len(to_post) + len(leads) + len(priority_names)
                  + len(pu_pending) + len(pu_awaiting))
    out = []
    if interactive:
        out.append(
            "<!doctype html>"
            '<html lang="en"><head>'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<base target="_blank">'
            f'<title>Daily Brief — {escape(date_str)}</title>'
            f'<link rel="stylesheet" href="{escape(MASTER_CSS_URL, quote=True)}">'
            '</head><body class="reset-anchor">'
            f'<div style="background:#ffffff; {CONTAINER_STYLE}">'
            f'<h1 class="page-title">Daily Brief — {escape(date_str)}</h1>'
            '<div class="filter-bar">'
            '<button type="button" id="refresh-btn" class="refresh-btn">'
            '↻ Refresh data</button>'
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
        out.append(
            f'<p style="font-family:{FONT_STACK}; font-size:14px; margin:0 0 24px 0;">'
            '<a href="https://bddpwqsqvt32ritxpjqlqwhaim0ykbol.lambda-url.us-east-1.on.aws/?key=alkj%2A707q235-qjdf" '
            'style="color:#2563eb; text-decoration:none; font-weight:500;">'
            'Open the interactive Daily Brief &rarr;</a></p>'
        )
        web_base = os.environ.get("BRIEF_PAGE_URL")
        web_key = os.environ.get("BRIEF_PAGE_KEY")
        if web_base and web_key:
            sep = "&" if "?" in web_base else "?"
            web_url = f"{web_base}{sep}key={quote(web_key, safe='')}"
            out.append(
                '<p style="font-size:14px; margin:0 0 24px 0;">'
                f'<a href="{escape(web_url, quote=True)}"'
                f' style="{LINK_STYLE_500}">'
                'View interactive web version &rarr;</a></p>'
            )

    # ── Daily actions (website only) ──────────────────────────────────────
    if interactive:
        out.append('<div data-owner="chad">')
        out.append(f'<h1 style="{QUEUE_H1_STYLE_TOP}">Daily actions</h1>')
        out.append(
            '<div data-section-key="chad-0-pricing" '
            'style="border:1px solid #e5e7eb; border-radius:8px; '
            'padding:16px 18px; margin:0 0 12px 0; background:#ffffff;">'
            '<a href="https://jw2kk4a73jbft32yf5lr7u22bm0bgkiy.lambda-url.us-east-1.on.aws/" '
            'target="_blank" rel="noopener" class="daily-action-link" '
            'style="color:#2563eb; text-decoration:none; font-size:16px; font-weight:600;">'
            'Update third-party pricing &rarr;</a>'
            '<div style="font-size:13px; color:#6b7280; margin-top:4px;">'
            'Opens the Hiive pricing tool. This item returns tomorrow.</div>'
            '</div>'
        )
        out.append(
            '<div data-section-key="chad-0-mailer" '
            'style="border:1px solid #e5e7eb; border-radius:8px; '
            'padding:16px 18px; margin:0 0 12px 0; background:#ffffff;">'
            '<a href="https://bddpwqsqvt32ritxpjqlqwhaim0ykbol.lambda-url.us-east-1.on.aws/'
            '?key=alkj%2A707q235-qjdf&amp;view=mailer" '
            'target="_blank" rel="noopener" class="daily-action-link" '
            'style="color:#2563eb; text-decoration:none; font-size:16px; font-weight:600;">'
            'Weekly mailer recipients &rarr;</a>'
            '<div style="font-size:13px; color:#6b7280; margin-top:4px;">'
            'First name and email from the &ldquo;S: Weekly Mailer Leads&rdquo; '
            'focused list. Copy into SharePoint, then run the flow.</div>'
            '</div>'
        )
        out.append('</div>')

    # ── Chad — Trading queue ──────────────────────────────────────────────
    out.append('<div data-owner="chad">')
    if interactive:
        out.append('<h1>Chad — Trading queue</h1>')
    else:
        out.append(
            f'<h1 style="{QUEUE_H1_STYLE}">Chad — Trading queue</h1>'
        )

    # ── A. INVOICE ────────────────────────────────────────────────────────
    out.append(_section_open(
        "chad-1-invoice", "1. INVOICE: Get paid and win deal", interactive,
    ))
    if not to_invoice:
        out.append(_muted_p("(No SPA-signed deals awaiting invoice.)",
                            interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row([
            "Deal Title", "Contact", "IQF", "CEF", "Agent Agreement",
        ], with_checkbox=interactive, interactive=interactive))
        for r in to_invoice:
            co = companies_by_id.get(_normalize_id(_company_id(r["deal"])))
            contact_html, iqf_html, cef_html = _people_cells(
                r["people"], deal=r["deal"], company=co, interactive=interactive,
            )
            did = r["deal"].get("id")
            out.append(
                _row_open("A", did, interactive)
                + _checkbox_td("A", did, interactive)
                + _td(_deal_title_link(r["deal"], interactive=interactive),
                      interactive=interactive)
                + _td(contact_html, interactive=interactive)
                + _td(iqf_html, interactive=interactive)
                + _td(cef_html, interactive=interactive)
                + _td(_commission_cell(r["deal"]), interactive=interactive)
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── B. CLOSE ──────────────────────────────────────────────────────────
    out.append(_section_open(
        "chad-2-close", "2. CLOSE: Move toward finish line", interactive,
    ))
    if not to_close:
        out.append(_muted_p("(Nothing to close.)", interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row([
            "Stage", "Deal Title", ("Buyer", "Seller"),
            "IQF", "CEF", "Agent Agreement", "Latest activity",
        ], with_checkbox=interactive, interactive=interactive))
        for r in to_close:
            co = companies_by_id.get(_normalize_id(_company_id(r["deal"])))
            buyer, seller = _split_contacts_by_role(r["people"], r["deal"])
            contact_html = _stacked_contact_cell(buyer, seller, r["deal"], co,
                                                 interactive=interactive)
            iqf_html = _stacked_iqf_cell(buyer, seller, r["deal"])
            cef_html = _stacked_cef_cell(buyer, seller)
            did = r["deal"].get("id")
            out.append(
                _row_open("B", did, interactive)
                + _checkbox_td("B", did, interactive)
                + _td(escape(r["stage"]), interactive=interactive)
                + _td(_deal_title_link(r["deal"], interactive=interactive),
                      interactive=interactive)
                + _td(contact_html, interactive=interactive)
                + _td(iqf_html, interactive=interactive)
                + _td(cef_html, interactive=interactive)
                + _td(_commission_cell(r["deal"]), interactive=interactive)
                + _td(_latest_activity_cell(r.get("latest_notes") or []),
                      interactive=interactive)
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── C. INTRODUCE ──────────────────────────────────────────────────────
    out.append(_section_open(
        "chad-3-introduce", "3. INTRODUCE: Crossed trades", interactive,
    ))
    if not crossed:
        out.append(_muted_p("(None)", interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row([
            "Company", "Structure",
            "Buy", "Buy Deal ID",
            "Sell", "Sell Deal ID",
            ("Buyer", "Seller"),
            "% Diff",
        ], with_checkbox=interactive, interactive=interactive))
        for r in crossed:
            b_pc = r["buy_primary"]
            s_pc = r["sell_primary"]
            buy_co = companies_by_id.get(_normalize_id(_company_id(r["buy_deal"])))
            sell_co = companies_by_id.get(_normalize_id(_company_id(r["sell_deal"])))
            buyer_html = _single_contact_cell(b_pc, r["buy_deal"], buy_co,
                                              interactive=interactive)
            seller_html = _single_contact_cell(s_pc, r["sell_deal"], sell_co,
                                               interactive=interactive)
            contact_html = _stack2(buyer_html, seller_html)
            buy_id = r["buy_deal"].get("id")
            sell_id = r["sell_deal"].get("id")
            row_id = f"{buy_id}_{sell_id}" if buy_id and sell_id else (buy_id or sell_id)
            out.append(
                _row_open("C", row_id, interactive)
                + _checkbox_td("C", row_id, interactive)
                + _td(escape(r["company"]), interactive=interactive)
                + _td(escape(r["structure"]), interactive=interactive)
                + _td(escape(_fmt_price(r["buy_price"])), interactive=interactive)
                + _td(_deal_link(r["buy_deal"], interactive=interactive),
                      interactive=interactive)
                + _td(escape(_fmt_price(r["sell_price"])), interactive=interactive)
                + _td(_deal_link(r["sell_deal"], interactive=interactive),
                      interactive=interactive)
                + _td(contact_html, interactive=interactive)
                + _td(f"{r['pct']:+.2f}%", interactive=interactive)
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
        out.append(_muted_p("(None)", interactive=interactive))
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
            if interactive:
                out.append(f'<p>{escape(sub_heading)}</p>')
            else:
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
            out.append(_open_table(interactive=interactive))
            out.append(_header_row(labels, with_checkbox=interactive,
                                   interactive=interactive))
            for r in group:
                pc = r["primary"]
                n, e = _person_name_email(pc)
                iqf_html = _iqf_cell(pc, r["deal"])
                cef_html = _colorize_symbol(_person_cef(pc))
                co = companies_by_id.get(_normalize_id(_company_id(r["deal"])))
                contact_html = _contact_cell(n, email=e, person_id=pc.get("id"),
                                              interactive=interactive)
                env = _envelope_link(r["deal"], pc, co, interactive=interactive)
                if env:
                    contact_html = f"{contact_html}{env}"
                contact_html = f"{contact_html}{_nudge_link(r['deal'].get('id'), interactive=interactive)}"
                dist_extra = (
                    f" background-color:{NEG_DISTANCE_BG};"
                    if r["distance"] is not None and r["distance"] < 0 else ""
                )
                dist_cell = (
                    "&mdash;" if r["distance"] is None
                    else f"{r['distance'] * 100:+.2f}%"
                )
                if r.get("big_bid"):
                    dist_cell = f"{dist_cell} ($1M+)"
                did = r["deal"].get("id")
                out.append(
                    _row_open("D", did, interactive)
                    + _checkbox_td("D", did, interactive)
                    + _td(_deal_title_link(r["deal"], interactive=interactive),
                          interactive=interactive)
                    + _td(escape(r["stage"]), interactive=interactive)
                    + _td(escape(r["structure"]), interactive=interactive)
                    + _td(escape(_fmt_price(r["your_price"])),
                          interactive=interactive)
                    + _td(escape(_fmt_price(r["marketplace_price"])),
                          interactive=interactive)
                    + _td(dist_cell, extra=dist_extra,
                          interactive=interactive)
                    + _td(contact_html, interactive=interactive)
                    + _td(iqf_html, interactive=interactive)
                    + _td(cef_html, interactive=interactive)
                    + "</tr>"
                )
            out.append("</table>")
    out.append(_section_close())

    # ── 5. TOP BUYERS TO WARM ─────────────────────────────────────────────
    out.append(_section_open(
        "chad-5-top-buyers", "5. TOP BUYERS TO WARM", interactive,
    ))
    out.append(_muted_p(f"{len(top_buyers_to_warm)} top buyers to warm",
                        interactive=interactive))
    if not top_buyers_to_warm:
        out.append(_muted_p("(None)", interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row(
            ["Name", "Email", "Type", "Max Ticket", "# Buy Interests", "IQF"],
            with_checkbox=interactive, interactive=interactive,
        ))
        for r in top_buyers_to_warm:
            pid = r["person_id"]
            name_cell = _contact_cell(
                r["name"], email=r["email"], person_id=pid,
                interactive=interactive,
            )
            try:
                won_total = float(r.get("won_deals_total") or 0)
            except (TypeError, ValueError):
                won_total = 0.0
            if won_total > 0:
                title = f"Won deals: ${int(won_total):,}"
                name_cell = (
                    f'{name_cell} '
                    f'<span title="{escape(title, quote=True)}"'
                    ' style="color:#16a34a; font-weight:600;">$</span>'
                )
            out.append(
                _row_open("H", pid, interactive)
                + _checkbox_td("H", pid, interactive, dismiss_days=30)
                + _td(name_cell, interactive=interactive)
                + _td(_email_link(r["email"], interactive=interactive),
                      interactive=interactive)
                + _td(escape(r["transactor_type"]),
                      interactive=interactive)
                + _td(escape(r["max_ticket"]), interactive=interactive)
                + _td(f"{r['buy_count']}", interactive=interactive)
                + _td(_colorize_symbol(r["iqf"]), interactive=interactive)
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── 6. SPV MANAGERS TO WARM ───────────────────────────────────────────
    out.append(_section_open(
        "chad-6-spv-managers", "6. SPV MANAGERS TO WARM", interactive,
    ))
    out.append(_muted_p(f"{len(spv_managers_to_warm)} SPV managers to warm",
                        interactive=interactive))
    if not spv_managers_to_warm:
        out.append(_muted_p("(None)", interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row([
            "Name", "Email", "Top SPV Manager", "# Sells", "# Buys", "CEF",
        ], with_checkbox=interactive, interactive=interactive))
        for r in spv_managers_to_warm:
            pid = r["person_id"]
            out.append(
                _row_open("G", pid, interactive)
                + _checkbox_td("G", pid, interactive, dismiss_days=30)
                + _td(_contact_cell(
                    r["name"], email=r["email"], person_id=pid,
                    interactive=interactive,
                ), interactive=interactive)
                + _td(_email_link(r["email"], interactive=interactive),
                      interactive=interactive)
                + _td(escape(r["spv_value"]), interactive=interactive)
                + _td(f"{r['sell_count']}", interactive=interactive)
                + _td(f"{r['buy_count']}", interactive=interactive)
                + _td(_colorize_symbol(r["cef"]), interactive=interactive)
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    out.append("</div>")  # close data-owner="chad"

    # ── Kate — Queue ──────────────────────────────────────────────────────
    out.append('<div data-owner="kate">')
    if interactive:
        out.append('<h1>Kate — Queue</h1>')
    else:
        out.append(
            f'<h1 style="{QUEUE_H1_STYLE_TOP}">Kate — Queue</h1>'
        )

    # ── 0. UPDATE CRM ────────────────────────────────────────────────────
    out.append(_section_open(
        "kate-0-crm-updates", "0. UPDATE CRM: New valuations from the news",
        interactive,
    ))
    if not pu_pending and not pu_awaiting:
        out.append(_muted_p("(No pending valuation updates.)",
                            interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row(
            ["Company", "Headline", "Date", "New LR Val", "New LR PPS",
             "Series", ""],
            with_checkbox=False, interactive=interactive))
        if pu_pending:
            for r in pu_pending:
                c = r["_calc"]
                val_s = f"${c['new_val_bn']:.2f}B" if c["new_val_bn"] else "&mdash;"
                if c["cur_val"]:
                    val_s += ('<div style="font-size:11px; color:#9ca3af;">'
                              f"now ${c['cur_val']:.2f}B</div>")
                cur_pps_note = ""
                if c["cur_pps"]:
                    cur_pps_note = ('<div style="font-size:11px; color:#9ca3af;">'
                                    f"now ${c['cur_pps']:.2f}</div>")
                head = escape(r.get("headline") or "")
                url = escape(r.get("url") or "", quote=True)
                head_html = f'<a href="{url}">{head}</a>' if url else head
                co_id = r.get("co_id")
                co_name = escape(r.get("company") or "")
                if co_id:
                    co_html = (f'<a href="https://app.pipelinedeals.com/companies/'
                               f'{escape(str(co_id), quote=True)}">{co_name}</a>')
                else:
                    co_html = co_name
                rkey = escape(r.get("_key") or "", quote=True)
                if interactive:
                    pps_val = f"{c['new_pps']:.2f}" if c["new_pps"] else ""
                    pps_note = ""
                    if c["pps_source"] == "derived":
                        pps_note = ('<div style="font-size:11px; color:#9ca3af;">'
                                    'estimated</div>')
                    pps_cell = (
                        f'<input type="text" class="crm-pps" data-key="{rkey}"'
                        f' value="{pps_val}" placeholder="look up"'
                        ' style="width:82px; padding:3px 5px; font-family:inherit;'
                        ' font-size:13px;">' + pps_note + cur_pps_note
                    )
                    series_cell = (
                        f'<input type="text" class="crm-series" data-key="{rkey}"'
                        f' value="{escape(r.get("round_series") or "", quote=True)}"'
                        ' placeholder="e.g. Series D"'
                        ' style="width:92px; padding:3px 5px; font-family:inherit;'
                        ' font-size:13px;">'
                    )
                    post_cell = (
                        f'<button type="button" class="crm-post" data-key="{rkey}"'
                        ' style="padding:5px 14px; border-radius:999px; cursor:pointer;'
                        ' border:1px solid #b8c2cc; background:#CCDBEA;'
                        ' font-family:inherit; font-size:13px; font-weight:500;">'
                        'Post</button>'
                        f'<div style="margin-top:6px;">'
                        f'<button type="button" class="crm-snooze" data-key="{rkey}"'
                        ' style="padding:3px 8px; border-radius:999px; cursor:pointer;'
                        ' border:1px solid #d1d5db; background:#f3f4f6;'
                        ' font-family:inherit; font-size:11px; margin-right:4px;">'
                        'Snooze 1w</button>'
                        f'<button type="button" class="crm-delete" data-key="{rkey}"'
                        ' style="padding:3px 8px; border-radius:999px; cursor:pointer;'
                        ' border:1px solid #d1d5db; background:#f3f4f6;'
                        ' font-family:inherit; font-size:11px; color:#b91c1c;">'
                        'Delete</button></div>'
                    )
                else:
                    if c["new_pps"]:
                        tag = " (est.)" if c["pps_source"] == "derived" else ""
                        pps_cell = f"${c['new_pps']:.2f}{tag}"
                    else:
                        pps_cell = "&mdash; needs lookup"
                    pps_cell += cur_pps_note
                    series_cell = escape(r.get("round_series") or "") or "&mdash;"
                    post_cell = ""
                out.append(
                    f'<tr data-crm-row="{rkey}">'
                    + _td(co_html, interactive=interactive)
                    + _td(head_html, interactive=interactive)
                    + _td(escape(r.get("date") or ""), interactive=interactive)
                    + _td(val_s, interactive=interactive)
                    + _td(pps_cell, interactive=interactive)
                    + _td(series_cell, interactive=interactive)
                    + _td(post_cell, interactive=interactive)
                    + "</tr>"
                )
        if pu_awaiting:
            for r in pu_awaiting:
                url = escape(r.get("url") or "", quote=True)
                head = escape(r.get("headline") or "")
                head_html = f'<a href="{url}">{head}</a>' if url else head
                co_id = r.get("co_id")
                co_name = escape(r.get("company") or "")
                if co_id:
                    co_html = (f'<a href="https://app.pipelinedeals.com/companies/'
                               f'{escape(str(co_id), quote=True)}">{co_name}</a>')
                else:
                    co_html = co_name
                rkey = escape(r.get("_key") or "", quote=True)
                written_pps = _pu_float(r.get("written_pps"))
                written_val = _pu_float(r.get("written_val_bn"))
                val_s = f"${written_val:.2f}B" if written_val else "&mdash;"
                val_s += ('<div style="font-size:11px; color:#9ca3af;">'
                          'written to CRM</div>')
                date_cell = escape(r.get("date") or "")
                if r.get("written_at"):
                    date_cell += ('<div style="font-size:11px; color:#9ca3af;">'
                                  f'posted {escape(str(r.get("written_at")))}</div>')
                note = ("not published - look up" if r.get("_needs_lookup")
                        else "estimated - confirm")
                if interactive:
                    pps_val = f"{written_pps:.2f}" if written_pps else ""
                    pps_cell = (
                        f'<input type="text" class="crm-pps" data-key="{rkey}"'
                        f' value="{pps_val}" placeholder="look up"'
                        ' style="width:82px; padding:3px 5px; font-family:inherit;'
                        ' font-size:13px;">'
                        f'<div style="font-size:11px; color:#9ca3af;">{note}</div>'
                    )
                    post_cell = (
                        f'<button type="button" class="crm-post" data-key="{rkey}"'
                        ' style="padding:5px 14px; border-radius:999px; cursor:pointer;'
                        ' border:1px solid #b8c2cc; background:#CCDBEA;'
                        ' font-family:inherit; font-size:13px; font-weight:500;">'
                        'Post</button>'
                        f'<div style="margin-top:6px;">'
                        f'<button type="button" class="crm-snooze" data-key="{rkey}"'
                        ' style="padding:3px 8px; border-radius:999px; cursor:pointer;'
                        ' border:1px solid #d1d5db; background:#f3f4f6;'
                        ' font-family:inherit; font-size:11px; margin-right:4px;">'
                        'Snooze 1w</button>'
                        f'<button type="button" class="crm-delete" data-key="{rkey}"'
                        ' style="padding:3px 8px; border-radius:999px; cursor:pointer;'
                        ' border:1px solid #d1d5db; background:#f3f4f6;'
                        ' font-family:inherit; font-size:11px; color:#b91c1c;">'
                        'Delete</button></div>'
                    )
                else:
                    pps_cell = (f"${written_pps:.2f} (est.)" if written_pps
                                else "&mdash; needs lookup")
                    pps_cell += ('<div style="font-size:11px; color:#9ca3af;">'
                                 f'{note}</div>')
                    post_cell = ""
                series_cell = escape(r.get("round_series") or "") or "&mdash;"
                out.append(
                    f'<tr data-crm-row="{rkey}">'
                    + _td(co_html, interactive=interactive)
                    + _td(head_html, interactive=interactive)
                    + _td(date_cell, interactive=interactive)
                    + _td(val_s, interactive=interactive)
                    + _td(pps_cell, interactive=interactive)
                    + _td(series_cell, interactive=interactive)
                    + _td(post_cell, interactive=interactive)
                    + "</tr>"
                )
        out.append("</table>")
    out.append(_section_close())

    # ── E. POST: Trades to post to Notice ─────────────────────────────────
    out.append(_section_open(
        "kate-1-post", "1. POST: Trades to post to Notice", interactive,
    ))
    if not to_post:
        out.append(_muted_p("(Nothing to post — clean book!)",
                            interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row([
            "Action", "Deal Title", "Volume", "Structure", "Shares", "Price",
        ], with_checkbox=interactive, interactive=interactive))
        for r in to_post:
            d = r["deal"]
            did = d.get("id")
            volume = _cf_number(d, CF_MAX_SIZE)
            shares = _cf_number(d, CF_SHARE_COUNT)
            price = _post_price(d)
            out.append(
                _row_open("E", did, interactive)
                + _checkbox_td("E", did, interactive)
                + _td(escape(r["action"]), interactive=interactive)
                + _td(_deal_title_link(d, interactive=interactive),
                      interactive=interactive)
                + _td(escape(_fmt_price_or_dash(volume)),
                      interactive=interactive)
                + _td(escape(_post_structure_label(d)),
                      interactive=interactive)
                + _td(escape(_fmt_int_or_dash(shares)),
                      interactive=interactive)
                + _td(escape(_fmt_price_or_dash(price)),
                      interactive=interactive)
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── 2. Priority Names ─────────────────────────────────────────────────
    out.append(_section_open(
        "kate-2-priority-names", "2. Priority Names", interactive,
    ))
    out.append(_muted_p(
        "Companies flagged High Priority (hot Hiive book). Research each for "
        "direct/SPV sellers and enter Pitchbook data.",
        interactive=interactive,
    ))
    if not priority_names:
        out.append(_muted_p(
            "(None flagged — nothing to research today.)",
            interactive=interactive,
        ))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row([
            "Name", "Action", "# Holders", "Holders",
        ], interactive=interactive))
        for r in priority_names:
            hl = r["holders"]
            if len(hl) > 5:
                holders_text = ", ".join(hl[:5]) + f" + {len(hl) - 5} more"
            else:
                holders_text = ", ".join(hl)
            out.append(
                "<tr>"
                + _td(escape(r["name"]), interactive=interactive)
                + _td("Enter Pitchbook Data", interactive=interactive)
                + _td(escape(str(r["holder_count"])), interactive=interactive)
                + _td(escape(holders_text), interactive=interactive)
                + "</tr>"
            )
        out.append("</table>")
    out.append(_section_close())

    # ── 3. Leads to Revive ────────────────────────────────────────────────
    out.append(_section_open(
        "kate-3-leads", "3. Leads to Revive", interactive,
    ))
    out.append(_muted_p(
        f"Priority Leads (active deals, no work email): {len(priority_leads)}",
        interactive=interactive,
    ))
    out.append(_muted_p(
        f"Total Newsletter Recipients: {newsletter_recipient_count}",
        interactive=interactive,
    ))
    out.append(_muted_p(
        f"Leads to Revive: {leads_to_revive_count}",
        interactive=interactive,
    ))

    priority_heading_text = (
        "Priority — active deals with no work email (research these first)"
    )
    if interactive:
        out.append(f'<p>{escape(priority_heading_text)}</p>')
    else:
        priority_heading_style = (
            "font-size:14px; font-weight:600; color:#374151;"
            " margin:16px 0 6px 0;"
        )
        out.append(
            f'<p style="{priority_heading_style}">'
            f'{escape(priority_heading_text)}'
            '</p>'
        )
    if not priority_leads:
        out.append(_muted_p("(No priority leads — clean book!)",
                            interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row([
            "Name", "# Active Deals", "Companies", "LinkedIn",
        ], with_checkbox=interactive, interactive=interactive))
        for r in priority_leads:
            name_href = PIPELINE_PERSON_URL.format(
                escape(str(r["person_id"]), quote=True)
            )
            if interactive:
                name_cell = f'<a href="{name_href}">{escape(r["name"])}</a>'
            else:
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
                li_href = escape(r["linked_in_url"], quote=True)
                if interactive:
                    li_cell = f'<a href="{li_href}">LinkedIn</a>'
                else:
                    li_cell = (
                        f'<a href="{li_href}" style="{LINK_STYLE_500}">LinkedIn</a>'
                    )
            else:
                li_cell = ""
            pid = r["person_id"]
            out.append(
                _row_open("Fpri", pid, interactive)
                + _email_update_td("Fpri", pid, interactive)
                + _td(name_cell, interactive=interactive)
                + _td(escape(str(r["active_deal_count"])),
                      interactive=interactive)
                + _td(escape(companies_text), interactive=interactive)
                + _td(li_cell, interactive=interactive)
                + "</tr>"
            )
        out.append("</table>")
        out.append(SECTION_GAP_HTML)

    if not leads:
        out.append(_muted_p("(No leads to revive — clean book!)",
                            interactive=interactive))
    else:
        out.append(_open_table(interactive=interactive))
        out.append(_header_row([
            "Reason", "Name", "Pipeline", "LinkedIn", "Company",
        ], with_checkbox=interactive, interactive=interactive))
        for r in leads:
            person_href = PIPELINE_PERSON_URL.format(
                escape(str(r["person_id"]), quote=True)
            )
            if interactive:
                pipeline_cell = f'<a href="{person_href}">open</a>'
            else:
                pipeline_cell = (
                    f'<a href="{person_href}" style="{LINK_STYLE_500}">open</a>'
                )
            if r["linked_in_url"]:
                li_href = escape(r["linked_in_url"], quote=True)
                if interactive:
                    li_cell = f'<a href="{li_href}">LinkedIn</a>'
                else:
                    li_cell = (
                        f'<a href="{li_href}" style="{LINK_STYLE_500}">LinkedIn</a>'
                    )
            else:
                li_cell = ""
            if r["company_website"] and r["company_name"]:
                co_href = escape(r["company_website"], quote=True)
                if interactive:
                    co_cell = f'<a href="{co_href}">{escape(r["company_name"])}</a>'
                else:
                    co_cell = (
                        f'<a href="{co_href}" style="{LINK_STYLE}">'
                        f'{escape(r["company_name"])}</a>'
                    )
            elif r["company_name"]:
                co_cell = escape(r["company_name"])
            else:
                co_cell = ""
            pid = r["person_id"]
            out.append(
                _row_open("Fmain", pid, interactive)
                + _email_update_td("Fmain", pid, interactive)
                + _td(escape(r["reason"]), interactive=interactive)
                + _td(escape(r["name"]), interactive=interactive)
                + _td(pipeline_cell, interactive=interactive)
                + _td(li_cell, interactive=interactive)
                + _td(co_cell, interactive=interactive)
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
    top_buyers_to_warm = _build_top_buyers_to_warm(people, jwt=jwt)
    popular_spv_managers = _build_popular_spv_managers(people, security_names)
    priority_names = _build_priority_names(companies, people, security_names)

    body_html = _render_html(
        crossed, tight, to_close, to_invoice, leads,
        newsletter_recipient_count, leads_to_revive_count, date_str,
        companies_by_id, priority_leads, to_post,
        spv_managers_to_warm, top_buyers_to_warm, popular_spv_managers,
        priority_names,
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


MAILER_SEARCH_ID = 19530439
PIPELINE_API_KEY = "ZRMHN4uJotjRDcZa8hKi"
PIPELINE_APP_KEY = "571978be28bd3b5b515a2cc5db96b674"
MAILER_MAX_PAGES = 25


def _fetch_mailer_page(page, search_id=MAILER_SEARCH_ID):
    url = (
        "https://api.pipelinecrm.com/api/v3/searches/"
        + str(search_id)
        + "/perform.json?per_page=200&page=" + str(page)
        + "&api_key=" + PIPELINE_API_KEY
        + "&app_key=" + PIPELINE_APP_KEY
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _collect_mailer_entries(data, seen, rows, missing):
    cols = [c.get("id") for c in (data.get("columns") or [])]
    if "person_first_name" not in cols or "person_email" not in cols:
        raise RuntimeError(
            "Focused list columns changed; got: "
            + ", ".join(str(c) for c in cols)
        )
    i_first = cols.index("person_first_name")
    i_email = cols.index("person_email")
    for entry in (data.get("entries") or []):
        if not isinstance(entry, list) or len(entry) <= max(i_first, i_email):
            continue
        first = (entry[i_first] or "").strip()
        email = (entry[i_email] or "").strip()
        if not email:
            continue
        dedupe_key = email.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if not first:
            missing.append(email)
        rows.append((first, email))


def _fetch_mailer_rows(search_id=MAILER_SEARCH_ID):
    """Run the 'S: Weekly Mailer Leads' focused list (saved search 19530439).
    Page 1 is fetched first to learn the page count; pages 2..N are then
    fetched concurrently. Returns (rows, missing) where rows is
    [(first_name, email)] deduped on lowercased email."""
    seen = set()
    rows = []
    missing = []

    first_page = _fetch_mailer_page(1, search_id)
    _collect_mailer_entries(first_page, seen, rows, missing)

    pagination = first_page.get("pagination") or {}
    try:
        pages = int(pagination.get("pages") or 1)
    except (TypeError, ValueError):
        pages = 1
    pages = min(pages, MAILER_MAX_PAGES)

    if pages > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(_fetch_mailer_page, p, search_id): p
                for p in range(2, pages + 1)
            }
            results = []
            for future in concurrent.futures.as_completed(futures):
                results.append((futures[future], future.result()))
        for _page, data in sorted(results, key=lambda pair: pair[0]):
            _collect_mailer_entries(data, seen, rows, missing)

    return rows, missing


MAILER_SCRIPT = """
<script>
(function () {
  var btn = document.getElementById('copy-tsv');
  if (!btn) return;
  btn.addEventListener('click', function () {
    var ta = document.getElementById('tsv');
    ta.style.display = 'block';
    ta.select();
    try { document.execCommand('copy'); btn.textContent = 'Copied'; }
    catch (e) { btn.textContent = 'Select the box and copy'; }
    ta.style.display = 'none';
  });
})();
</script>
"""


def _render_mailer_page(search_id=MAILER_SEARCH_ID):
    try:
        rows, missing = _fetch_mailer_rows(search_id)
    except Exception as exc:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": "Could not build the mailer list: " + str(exc),
        }

    tsv = "\n".join(f + "\t" + e for f, e in rows)

    out = []
    out.append(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Weekly Mailer Recipients</title></head>"
        '<body style="font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937;"
        'font-size:14px;max-width:820px;margin:0 auto;padding:24px;">'
    )
    out.append(
        '<h1 style="font-size:20px;margin:0 0 4px 0;">Weekly Mailer Recipients</h1>'
    )
    src_label = ("&ldquo;S: Weekly Mailer Leads&rdquo;" if search_id == MAILER_SEARCH_ID
                 else "saved search " + str(search_id))
    out.append(
        '<p style="color:#6b7280;margin:0 0 16px 0;">Source: focused list '
        + src_label + " &middot; "
        + str(len(rows))
        + " recipients</p>"
    )
    out.append(
        '<form method="get" style="margin:0 0 16px 0;">'
        '<input type="hidden" name="view" value="mailer">'
        '<input type="hidden" name="list" value="1">'
        '<input type="hidden" name="key" value="' + escape(os.environ.get("BRIEF_PAGE_KEY", ""), quote=True) + '">'
        '<input type="text" name="search" inputmode="numeric" placeholder="Saved search ID" '
        'value="' + (str(search_id) if search_id != MAILER_SEARCH_ID else "") + '" '
        'style="padding:8px 10px;border:1px solid #d1d5db;border-radius:6px;'
        'font-size:14px;font-family:inherit;width:180px;">'
        '<button type="submit" style="margin-left:8px;padding:8px 18px;'
        "border:1px solid #d1d5db;background:#ffffff;color:#374151;"
        "font-size:14px;font-weight:500;border-radius:6px;cursor:pointer;"
        'font-family:inherit;">Load list</button>'
        "</form>"
    )
    out.append(
        '<button type="button" id="copy-tsv" style="padding:8px 18px;'
        "border:1px solid #d1d5db;background:#ffffff;color:#374151;"
        "font-size:14px;font-weight:500;border-radius:6px;cursor:pointer;"
        'font-family:inherit;margin-bottom:16px;">Copy as tab-separated</button>'
    )
    out.append(
        '<textarea id="tsv" style="display:none;position:absolute;left:-9999px;">'
        + escape(tsv)
        + "</textarea>"
    )

    if missing:
        out.append(
            '<div style="background:#f3f4f6;border:1px solid #e5e7eb;'
            'border-radius:6px;padding:12px 14px;margin-bottom:16px;'
            'font-size:13px;color:#4b5563;">'
            "<strong>"
            + str(len(missing))
            + " included with no first name</strong> &mdash; these will read "
            "&ldquo;Hello &mdash;&rdquo; in the merge. Add a first name in the "
            "CRM to fix: "
            + escape(", ".join(missing))
            + "</div>"
        )

    out.append(
        '<table style="border-collapse:collapse;width:100%;font-size:14px;">'
        '<tr><th style="border:1px solid #e5e7eb;padding:6px 10px;'
        'text-align:left;background:#f9fafb;">First Name</th>'
        '<th style="border:1px solid #e5e7eb;padding:6px 10px;'
        'text-align:left;background:#f9fafb;">Email</th></tr>'
    )
    for first, email in rows:
        out.append(
            '<tr><td style="border:1px solid #e5e7eb;padding:6px 10px;">'
            + escape(first)
            + '</td><td style="border:1px solid #e5e7eb;padding:6px 10px;">'
            + escape(email)
            + "</td></tr>"
        )
    out.append("</table>")
    out.append(MAILER_SCRIPT)
    out.append("</body></html>")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": "".join(out),
    }


MAILER_BASE_URL = "https://bddpwqsqvt32ritxpjqlqwhaim0ykbol.lambda-url.us-east-1.on.aws/"
MAILER_PREVIEW_PID = 1259927678
MAILER_SELECTION_KEY = "mailer-selection.json"


def _mailer_token(person_id, deal_id):
    secret = os.environ.get("MAILER_CLICK_SECRET", "")
    msg = str(person_id) + ":" + str(deal_id)
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:24]


def _mailer_click_url(person_id, deal_id):
    return (
        MAILER_BASE_URL
        + "?view=click&pid=" + str(person_id)
        + "&did=" + str(deal_id)
        + "&t=" + _mailer_token(person_id, deal_id)
    )


def _mailer_fmt_money(v):
    if v is None:
        return ""
    v = float(v)
    if v >= 1_000_000:
        return "$" + ("%.1f" % (v / 1_000_000)).rstrip("0").rstrip(".") + "M"
    if v >= 1_000:
        return "$" + ("%.0f" % (v / 1_000)) + "K"
    return "$" + ("%.0f" % v)


def _mailer_size(deal):
    lo = _cf_number(deal, CF_TICKET_MIN)
    hi = _cf_number(deal, CF_TICKET_MAX)
    if lo and hi and lo == hi:
        return _mailer_fmt_money(lo)
    if lo and hi:
        return _mailer_fmt_money(lo) + " – " + _mailer_fmt_money(hi)
    if hi:
        return "up to " + _mailer_fmt_money(hi)
    if lo:
        return "from " + _mailer_fmt_money(lo)
    return "—"


def _mailer_eligible(deals, buyer_counts=None):
    sells, buys = [], []
    for d in deals:
        if _stage_id(d) not in (STAGE_FIRM, STAGE_INQUIRY):
            continue
        side = _deal_side(d)
        if side == "SELL":
            sells.append(d)
        elif side == "BUY":
            buys.append(d)

    def _key(d):
        return _cf_number(d, CF_TICKET_MAX) or 0

    sells.sort(key=_key, reverse=True)
    buys.sort(key=lambda d: (_company_name(d) or _deal_title(d)).strip().lower())
    return sells, buys


def _load_mailer_selection(s3):
    try:
        doc = _fetch_json(s3, MAILER_SELECTION_KEY)
        return {str(x) for x in (doc.get("deal_ids") or [])}
    except Exception:
        return set()


def _save_mailer_selection(deal_ids):
    boto3.client("s3", region_name=S3_REGION).put_object(
        Bucket=S3_BUCKET, Key=MAILER_SELECTION_KEY,
        Body=json.dumps({"deal_ids": sorted(deal_ids),
                         "saved_at": datetime.now(timezone.utc).isoformat()}).encode("utf-8"),
        ContentType="application/json",
    )


_LOGO_CACHE = {}


def _mailer_logo_url(name):
    if not name:
        return None
    if name in _LOGO_CACHE:
        return _LOGO_CACHE[name]
    url = "https://bannerlogos.s3.us-east-1.amazonaws.com/" + urllib.parse.quote(name) + ".png"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=3) as resp:
            ok = resp.status == 200
    except Exception:
        ok = False
    _LOGO_CACHE[name] = url if ok else None
    return _LOGO_CACHE[name]


def _mailer_logo_img(logos, deal):
    url = _mailer_logo_url(_company_name(deal) or _deal_title(deal))
    if not url:
        return ""
    return ('<img src="' + escape(url, quote=True)
            + '" width="16" height="16" alt="" '
            'style="vertical-align:-3px;margin-right:6px;border:0;object-fit:contain;">')


def _mailer_buyer_counts(s3):
    try:
        buy = (_fetch_json(s3, "interest_people.json") or {}).get("buy") or {}
    except Exception:
        return {}
    counts = {}
    for name, ids in buy.items():
        if isinstance(ids, list):
            counts[name.strip().lower()] = len(ids)
    return counts


def _mailer_table(title, rows, person_id, buyer_counts=None, logos=None):
    th = (
        'style="text-align:left;padding:8px 10px;border-bottom:2px solid #1f2937;'
        'font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:#374151;"'
    )
    td = 'style="padding:9px 10px;border-bottom:1px solid #e5e7eb;font-size:14px;color:#1f2937;"'
    out = [
        '<h2 style="font-size:16px;margin:28px 0 8px 0;color:#111827;">' + escape(title) + "</h2>",
        '<table style="border-collapse:collapse;width:100%;">',
        "<tr><th " + th + ">Company (Click for Details)</th><th " + th + ">Structure</th>"
        "<th " + th + ">Size</th></tr>",
    ]
    if not rows:
        out.append("<tr><td " + td + ' colspan="3">None this week</td></tr>')
    for d in rows:
        href = _mailer_click_url(person_id, d.get("id"))
        name = escape(_company_name(d) or _deal_title(d))
        if buyer_counts is not None:
            n = buyer_counts.get(_normalize_id(_company_id(d)), 0)
            if n:
                name += " (" + str(n) + " buyer" + ("" if n == 1 else "s") + ")"
        out.append(
            "<tr>"
            "<td " + td + '><a href="' + href + '" style="color:#1d4ed8;font-weight:600;text-decoration:none;">' + name + "</a></td>"
            "<td " + td + ">" + escape(_deal_structure_label(d) or "—") + "</td>"
            "<td " + td + ">" + escape(_mailer_size(d)) + "</td>"
            "</tr>"
        )
    out.append("</table>")
    return "".join(out)


def _mailer_buy_table(title, rows, person_id, buyer_counts, logos=None):
    td = 'style="padding:9px 10px;border-bottom:1px solid #e5e7eb;font-size:14px;color:#1f2937;"'
    out = [
        '<h2 style="font-size:16px;margin:28px 0 8px 0;color:#111827;">' + escape(title) + "</h2>",
        '<table style="border-collapse:collapse;width:100%;">',
    ]
    if not rows:
        out.append("<tr><td " + td + ">None this week</td></tr>")
    seen = set()
    for d in rows:
        cid = _normalize_id(_company_id(d))
        if cid in seen:
            continue
        seen.add(cid)
        href = _mailer_click_url(person_id, d.get("id"))
        name = escape(_company_name(d) or _deal_title(d))
        n = (buyer_counts or {}).get((_company_name(d) or _deal_title(d)).strip().lower(), 0)
        buyers = str(n) + " Buyer" + ("" if n == 1 else "s") if n else "Buyers waiting"
        out.append(
            "<tr><td " + td + '><span style="font-weight:600;">' + name + "</span>"
            " &mdash; " + buyers + ' &mdash; <a href="' + href
            + '" style="color:#1d4ed8;font-weight:600;text-decoration:none;">Make an offer</a></td></tr>'
        )
    out.append("</table>")
    return "".join(out)


def _render_mailer_email(first_name, person_id, sells, buys, buyer_counts=None, logos=None):
    greet = "Hello " + escape(first_name) + "," if first_name else "Hello,"
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,'
        'Helvetica,Arial,sans-serif;max-width:680px;margin:0 auto;padding:24px;color:#1f2937;">'
        '<h1 style="font-size:18px;margin:0 0 18px 0;color:#111827;">Pre-IPO Secondary '
        "Opportunities from the Gracia Group</h1>"
        '<table role="presentation" width="100%" style="border-collapse:collapse;margin:0 0 20px 0;"><tr>'
        '<td valign="top" style="padding-right:18px;">'
        '<p style="font-size:15px;margin:0 0 16px 0;">' + greet + "</p>"
        '<p style="font-size:15px;margin:0 0 16px 0;">See this week&rsquo;s trades for '
        "accredited investors below. If you are looking for something that isn&rsquo;t listed, "
        "just let me know.</p>"
        '<p style="font-size:15px;margin:0;">Live orders this week. '
        "Click any company to see full details.</p>"
        "</td>"
        '<td valign="top" width="280">'
        '<div style="border:1px solid #cdc9c0;border-left:4px solid #3d5a73;background:#f7f6f3;'
        'border-radius:6px;padding:14px 18px;">'
        '<p style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;'
        'color:#3d5a73;font-weight:700;margin:0 0 6px 0;">New &mdash; Daily Highlight</p>'
        '<p style="font-size:13px;margin:0 0 10px 0;">One trade that stands out, in your inbox '
        "each day: a recent news item, a distressed seller, limited supply &mdash; or a deal that "
        "for various reasons is not published in our main books. Be among the first to see these "
        "opportunities. Stop any time.</p>"
        '<a href="https://bddpwqsqvt32ritxpjqlqwhaim0ykbol.lambda-url.us-east-1.on.aws/'
        '?view=daily&amp;pid=' + str(person_id) + "&amp;t=" + _mailer_token(person_id, "daily")
        + '" style="display:inline-block;background:#3d5a73;color:#ffffff;font-size:13px;'
        'font-weight:600;padding:8px 14px;border-radius:6px;text-decoration:none;">'
        "Get the Daily Highlight</a></div></td>"
        "</tr></table>"
        + _mailer_table("Sell orders — shares available", sells, person_id, None, logos)
        + _mailer_buy_table("Buy orders — buyers seeking shares", buys, person_id, buyer_counts, logos)
        + MAILER_SIGNATURE_HTML
        + "</div>"
    )


CF_PERSON_DAILY_MAILER = "custom_label_4006998"
DAILY_MAILER_SUBSCRIBED = 7203960
DAILY_MAILER_UNSUBSCRIBED = 7203961
DAILY_MAILER_HOLD = 7203962


def _daily_signup_page(message):
    return {"statusCode": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                     '<meta name="viewport" content="width=device-width, initial-scale=1">'
                     "<title>Daily Highlight</title></head>"
                     '<body style="font-family:-apple-system,BlinkMacSystemFont,'
                     "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937;"
                     'max-width:560px;margin:80px auto;padding:0 24px;text-align:center;">'
                     '<h1 style="font-size:22px;">' + message + "</h1>"
                     '<p style="color:#6b7280;font-size:14px;">Chad Gracia &middot; Gracia Group '
                     "&middot; Rainmaker Securities</p></body></html>")}


def _handle_daily_signup(params):
    pid_raw = str(params.get("pid") or "").strip()
    token = str(params.get("t") or "").strip()
    unsub = str(params.get("off") or "").strip() == "1"
    if not pid_raw.isdigit():
        return {"statusCode": 400, "body": "Bad link"}
    if not hmac.compare_digest(token, _mailer_token(pid_raw, "daily")):
        return {"statusCode": 403, "body": "Invalid link"}
    pid = int(pid_raw)
    try:
        jwt = get_jwt()
        cur = call_pipeline_api("GET", f"/people/{pid}.json", jwt=jwt)
        person = cur["data"] if cur.get("status") == 200 and isinstance(cur.get("data"), dict) else {}
        who = _person_full_name(person) or "Person " + str(pid)
        who_email = _person_email(person) or ""
        current = _cf_option_id(person, CF_PERSON_DAILY_MAILER)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if unsub:
            if current == DAILY_MAILER_HOLD:
                action = "unsubscribe requested while on Hold — left as Hold"
            else:
                call_pipeline_api("PUT", f"/people/{pid}.json",
                                  {"person": {"custom_fields": {CF_PERSON_DAILY_MAILER: DAILY_MAILER_UNSUBSCRIBED}}},
                                  jwt=jwt)
                action = "unsubscribed"
            page = _daily_signup_page("You&rsquo;re unsubscribed from the Daily Highlight.")
        else:
            if current == DAILY_MAILER_HOLD:
                action = "signup while on Hold — left as Hold"
            else:
                call_pipeline_api("PUT", f"/people/{pid}.json",
                                  {"person": {"custom_fields": {CF_PERSON_DAILY_MAILER: DAILY_MAILER_SUBSCRIBED}}},
                                  jwt=jwt)
                action = "subscribed"
            page = _daily_signup_page("You&rsquo;re in &mdash; the Daily Highlight starts with the next issue.")
        call_pipeline_api("POST", "/notes.json",
                          {"note": {"content": "Daily Highlight " + action + " via email link " + today,
                                    "note_category_id": 69759, "person_id": pid}},
                          jwt=jwt)
        boto3.client("ses", region_name=SES_REGION).send_email(
            Source=FROM_ADDR,
            Destination={"ToAddresses": TO_ADDRS},
            Message={"Subject": {"Data": "Daily Highlight: " + who + " " + action, "Charset": "UTF-8"},
                     "Body": {"Text": {"Data": who + " (" + who_email + ") " + action
                                       + ".\n\nPerson: https://app.pipelinecrm.com/people/" + str(pid) + "\n",
                                       "Charset": "UTF-8"}}},
        )
        return page
    except Exception as e:
        print(f"daily signup failed pid={pid_raw}: {e}")
        return _daily_signup_page("Something went wrong &mdash; please reply to any of our emails and we&rsquo;ll sort it.")


def _handle_mailer_click(params):
    pid_raw = str(params.get("pid") or "").strip()
    did_raw = str(params.get("did") or "").strip()
    token = str(params.get("t") or "").strip()
    if not (pid_raw.isdigit() and did_raw.isdigit()):
        return {"statusCode": 400, "body": "Bad link"}
    if not hmac.compare_digest(token, _mailer_token(pid_raw, did_raw)):
        return {"statusCode": 403, "body": "Invalid link"}
    pid = int(pid_raw)
    did = int(did_raw)
    redirect = {"statusCode": 302,
                "headers": {"Location": "https://trades.graciagroup.com/deal/" + str(did)},
                "body": ""}
    try:
        s3 = boto3.client("s3", region_name=S3_REGION)
        deals = (_fetch_json(s3, "deals.json") or {}).get("deals", []) or []
        deal = next((d for d in deals if _normalize_id(d.get("id")) == did), None)
        if not deal:
            return redirect
        side = _deal_side(deal)
        cid = _normalize_id(_company_id(deal))
        if side not in ("SELL", "BUY") or cid is None:
            return redirect
        co_name = _company_name(deal) or _deal_title(deal)
        if side == "BUY":
            redirect = {"statusCode": 302,
                        "headers": {"Location": "https://7u6sphgup5gjuywcvpuwzhruiq0asgdz.lambda-url.us-east-1.on.aws/"
                                    "?name=" + urllib.parse.quote(co_name) + "&side=sell"},
                        "body": ""}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        jwt = get_jwt()
        cur = call_pipeline_api("GET", f"/people/{pid}.json", jwt=jwt)
        person = cur["data"] if cur.get("status") == 200 and isinstance(cur.get("data"), dict) else {}
        who = (_person_full_name(person) or "Person " + str(pid))
        who_email = _person_email(person) or ""
        interest_status = "note only"
        if side == "SELL":
            agent_obj = s3.get_object(Bucket="pipeline-token", Key="agent-data.json")
            sec_ids = json.loads(agent_obj["Body"].read()).get("security_ids", {}) or {}
            entry = (sec_ids.get(co_name) or {}).get("b")
            if entry:
                existing = _cf_option_ids(person, CF_PERSON_BUY_INTERESTS)
                new_ids = sorted(existing | {int(entry)})
                res = call_pipeline_api("PUT", f"/people/{pid}.json",
                                        {"person": {"custom_fields": {CF_PERSON_BUY_INTERESTS: new_ids}}},
                                        jwt=jwt)
                interest_status = ("Buy Interest added" if res.get("status") == 200
                                   else f"Buy Interest write FAILED ({res.get('status')})")
            else:
                interest_status = "Buy Interest NOT added — no security entry for '" + co_name + "' in agent-data.json"
        note = ("Clicked " + co_name + " " + side.lower() + " order via weekly newsletter "
                + today + " — " + interest_status)
        call_pipeline_api("POST", "/notes.json",
                          {"note": {"content": note, "note_category_id": 69759,
                                    "person_id": pid, "company_id": cid, "deal_id": did}},
                          jwt=jwt)
        alert = (who + " (" + who_email + ") clicked " + co_name + " " + side.lower()
                 + " order in the weekly mailer.\n\n" + interest_status + "\n\n"
                 + "Person: https://app.pipelinecrm.com/people/" + str(pid) + "\n"
                 + "Deal: https://app.pipelinecrm.com/deals/" + str(did) + "\n")
        boto3.client("ses", region_name=SES_REGION).send_email(
            Source=FROM_ADDR,
            Destination={"ToAddresses": TO_ADDRS},
            Message={"Subject": {"Data": "Mailer click: " + who + " — " + co_name + " (" + side.lower() + ")", "Charset": "UTF-8"},
                     "Body": {"Text": {"Data": alert, "Charset": "UTF-8"}}},
        )
    except Exception as e:
        print(f"mailer click write failed pid={pid} did={did}: {e}")
    return redirect


CF_DEAL_AGENT_AGREEMENT = "custom_label_3714334"
AGENT_AGREEMENT_SELLSIDE = 6354277


def _mailer_composer_column(title, rows, selected):
    out = ['<div style="flex:1;min-width:280px;">'
           '<h3 style="font-size:14px;margin:0 0 8px 0;">' + escape(title) + "</h3>"]
    for d in rows:
        did = str(d.get("id"))
        checked = " checked" if did in selected else ""
        co = _company_name(d) or _deal_title(d)
        dt = _deal_title(d)
        extra = (' <span style="color:#9ca3af;">(' + escape(dt) + ")</span>"
                 if dt and dt != co else "")
        ssa = ('<span title="Sell-side agreement in place" '
               'style="color:#1f7a4d;font-weight:700;"> &#9679;</span>'
               if _cf_option_id(d, CF_DEAL_AGENT_AGREEMENT) == AGENT_AGREEMENT_SELLSIDE else "")
        out.append(
            '<label style="display:block;padding:4px 0;font-size:13px;">'
            '<input type="checkbox" name="deal" value="' + did + '"' + checked + "> "
            + '<a href="https://app.pipelinecrm.com/deals/' + did
            + '" target="_blank" style="color:#1f2937;text-decoration:none;border-bottom:1px dotted #9ca3af;">'
            + escape(co) + "</a>" + extra + ssa + " &middot; "
            + escape(_deal_structure_label(d) or "—") + " &middot; "
            + escape(_mailer_size(d)) + " &middot; "
            + escape(_fmt_price(_cf_number(d, CF_GROSS)) or "—")
            + "</label>"
        )
    out.append("</div>")
    return "".join(out)


MAILER_COMPOSER_SCRIPT = """
<script>
(function () {
  var btn = document.getElementById('save-sel');
  if (!btn) return;
  var tst = document.getElementById('test-send');
  if (tst) tst.addEventListener('click', function () {
    tst.disabled = true; tst.textContent = 'Sending…';
    fetch(window.location.href, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'mailer_test'})
    }).then(function (r) { return r.json(); }).then(function (j) {
      tst.textContent = j.ok ? 'Test sent ✓' : 'Failed — try again';
      tst.disabled = false;
    }).catch(function () { tst.textContent = 'Failed — try again'; tst.disabled = false; });
  });
  var clr = document.getElementById('clear-sel');
  if (clr) clr.addEventListener('click', function () {
    document.querySelectorAll('input[name=deal]').forEach(function (c) { c.checked = false; });
    btn.click();
  });
  btn.addEventListener('click', function () {
    var ids = [];
    document.querySelectorAll('input[name=deal]:checked').forEach(function (c) { ids.push(c.value); });
    btn.textContent = 'Saving...';
    fetch(window.location.href, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'mailer_select', deal_ids: ids})
    }).then(function (r) { return r.json(); })
      .then(function () { window.location.reload(); })
      .catch(function () { btn.textContent = 'Save failed'; });
  });
})();
</script>
"""


MAILER_FROM = "Chad Gracia <cgracia@graciagroup.com>"

MAILER_SIGNATURE_HTML = (
    '<p style="font-size:14px;margin:28px 0 0 0;color:#1f2937;">Chad Gracia<br>'
    "Registered Representative, Rainmaker Securities<br>"
    "WhatsApp: +380 99 346 4098</p>"
    '<p style="font-size:14px;margin:10px 0 0 0;">Indications for Accredited Investors: '
    '<a href="https://trades.graciagroup.com/" style="color:#1d4ed8;">https://trades.graciagroup.com/</a></p>'
    '<hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">'
    '<div style="font-size:11px;color:#6b7280;line-height:1.5;">'
    "<p style=\"margin:0 0 8px 0;\">DISCLOSURE: Rainmaker Securities, LLC (&ldquo;RMS&rdquo;) is a "
    '<a href="https://www.finra.org/#/" style="color:#6b7280;">FINRA</a> registered broker-dealer and '
    '<a href="https://www.sipc.org/" style="color:#6b7280;">SIPC</a> member. Find this broker-dealer and its agents on '
    '<a href="https://brokercheck.finra.org" style="color:#6b7280;">BrokerCheck</a>. Our relationship summary can be found on the '
    '<a href="https://www.rainmakersecurities.com/crs" style="color:#6b7280;">RMS website</a>.</p>'
    '<p style="margin:0 0 8px 0;">RMS is engaged by its clients to make referrals to buyers or sellers of private '
    "securities (&ldquo;Securities&rdquo;). If such client closes a Securities transaction with a buyer or seller so "
    "referred, RMS is entitled to a success fee from the client. Such success fee may be in the form of cash or in "
    "warrants to purchase securities of the client or client&rsquo;s affiliate. RMS or RMS representatives may hold "
    "equity in its issuer clients or in the issuers of securities purchased or sold by the parties to a transaction.</p>"
    '<p style="margin:0 0 8px 0;">This communication is confidential and is addressed only to its intended recipient. '
    "This communication does not represent an offer or solicitation to buy or sell Securities. Such an offer must be "
    "made via definitive legal documentation by the seller of securities.</p>"
    '<p style="margin:0 0 8px 0;">Investments in the Securities are speculative and involve a high degree of risk. '
    "An investor in the Securities should have little to no need for liquidity in the foreseeable future and have "
    "sufficient finances to withstand the loss of the entire investment.</p>"
    '<p style="margin:0 0 8px 0;">RMS does not recommend the purchase or sale of Securities. Potential buyers or '
    "sellers of the Securities should seek professional counsel prior to entering into any transaction.</p>"
    '<p style="margin:0 0 4px 0;font-weight:700;">RISK FACTORS</p>'
    '<p style="margin:0 0 8px 0;">Investments in the Securities are speculative and involve a high degree of risk. '
    "Companies engaging in private placements may be early stage and high risk. You should be able to afford the "
    "increased risk of loss with such investments, including the potential of a total loss.</p>"
    '<p style="margin:0 0 8px 0;">An investor in the Securities should have little to no need for liquidity in the '
    "foreseeable future. Unlike an investment purchased on a stock exchange, an investment in a private placement is "
    "highly illiquid. You will most likely be investing in restricted securities, may have difficulty finding a buyer "
    "for the securities when you can resell and, as a result, may need to hold the securities indefinitely.</p>"
    '<p style="margin:0;">Limited disclosure Information. Companies engaging in private placements are not required '
    "to provide the disclosure that would be required in a registered offering. You may have less information to make "
    "an informed investment decision than, for example, stock purchased on a stock exchange, including information "
    "that may help you determine whether the price asked for the investment is a fair price. Potential buyers or "
    "sellers of the Securities should seek professional counsel prior to entering into any transaction.</p></div>"
)


def _handle_mailer_test(body):
    pid = 1259927678
    s3 = boto3.client("s3", region_name=S3_REGION)
    deals = (_fetch_json(s3, "deals.json") or {}).get("deals", []) or []
    counts = _mailer_buyer_counts(s3)
    sells_all, buys_all = _mailer_eligible(deals, counts)
    selected = _load_mailer_selection(s3)
    sells = [d for d in sells_all if str(d.get("id")) in selected]
    buys = [d for d in buys_all if str(d.get("id")) in selected]
    email_html = _render_mailer_email("Chad", pid, sells, buys, counts, True)
    boto3.client("ses", region_name=SES_REGION).send_email(
        Source=MAILER_FROM,
        Destination={"ToAddresses": ["cgracia@rainmakersecurities.com"]},
        ReplyToAddresses=["cgracia@rainmakersecurities.com"],
        Message={"Subject": {"Data": "[TEST] Live orders this week — Gracia Group", "Charset": "UTF-8"},
                 "Body": {"Html": {"Data": email_html, "Charset": "UTF-8"}}},
    )
    return {"statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": True, "sells": len(sells), "buys": len(buys)})}


def _render_mailer_composer(pid):
    s3 = boto3.client("s3", region_name=S3_REGION)
    deals = (_fetch_json(s3, "deals.json") or {}).get("deals", []) or []
    counts = _mailer_buyer_counts(s3)
    sells_all, buys_all = _mailer_eligible(deals, counts)
    selected = _load_mailer_selection(s3)
    sells = [d for d in sells_all if str(d.get("id")) in selected]
    buys = [d for d in buys_all if str(d.get("id")) in selected]
    email_html = _render_mailer_email("Chad", pid, sells, buys, counts, True)

    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Weekly mailer</title></head>"
        '<body style="margin:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937;\">"
        '<div style="max-width:960px;margin:0 auto;padding:24px;">'
        '<h1 style="font-size:20px;margin:0 0 4px 0;">Weekly mailer</h1>'
        '<p style="color:#6b7280;font-size:13px;margin:0 0 16px 0;">Tick the orders to include, '
        "save, and the preview below updates. Nothing is sent from this page yet.</p>"
        '<button type="button" id="save-sel" style="margin:0 0 12px 0;padding:8px 18px;'
        "border:1px solid #d1d5db;background:#ffffff;color:#374151;font-size:14px;font-weight:500;"
        'border-radius:6px;cursor:pointer;font-family:inherit;">Save selection &amp; update preview</button>'
        '<button type="button" id="clear-sel" style="margin:0 0 12px 10px;padding:8px 18px;'
        "border:1px solid #d1d5db;background:#ffffff;color:#374151;font-size:14px;font-weight:500;"
        'border-radius:6px;cursor:pointer;font-family:inherit;">Clear all</button>'
        '<button type="button" id="test-send" style="margin:0 0 12px 10px;padding:8px 18px;'
        "border:1px solid #3d5a73;background:#3d5a73;color:#ffffff;font-size:14px;font-weight:600;"
        'border-radius:6px;cursor:pointer;font-family:inherit;">Send test to cgracia@rainmakersecurities.com</button>'
        '<div style="display:flex;gap:24px;flex-wrap:wrap;background:#ffffff;border:1px solid #e5e7eb;'
        'border-radius:6px;padding:16px;margin-bottom:24px;">'
        + _mailer_composer_column("Sell orders (" + str(len(sells_all)) + ")", sells_all, selected)
        + _mailer_composer_column(
            "Buy orders (" + str(len(buys_all)) + ")",
            sorted(buys_all, key=lambda d: (counts or {}).get(
                (_company_name(d) or _deal_title(d)).strip().lower(), 0), reverse=True),
            selected)
        + "</div>"
        '<p style="color:#6b7280;font-size:13px;margin:0 0 8px 0;">Preview as recipient &middot; '
        + str(len(sells)) + " sell / " + str(len(buys)) + " buy selected</p>"
        '<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;">'
        + email_html + "</div></div>"
        + MAILER_COMPOSER_SCRIPT
        + "</body></html>"
    )
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }


def handle_http_request(event):
    params = event.get("queryStringParameters") or {}
    if isinstance(params, dict) and params.get("view") == "click":
        return _handle_mailer_click(params)
    if isinstance(params, dict) and params.get("view") == "daily":
        return _handle_daily_signup(params)
    key = params.get("key") if isinstance(params, dict) else None
    expected = os.environ.get("BRIEF_PAGE_KEY")
    if not expected or key != expected:
        return {"statusCode": 403, "body": "Forbidden"}

    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "GET")
    if method == "POST":
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            body = {}
        if body.get("action") == "crm_post":
            return _handle_crm_post(body)
        if body.get("action") == "crm_snooze":
            return _handle_crm_snooze(body)
        if body.get("action") == "crm_delete":
            return _handle_crm_delete(body)
        if body.get("action") == "crm_confirm_pps":
            return _handle_crm_confirm(body)
        if body.get("action") == "row_done":
            return _handle_row_done(body)
        if body.get("action") == "lead_email":
            return _handle_lead_email(body)
        if body.get("action") == "mailer_test":
            return _handle_mailer_test(body)
        if body.get("action") == "mailer_select":
            ids = {str(x) for x in (body.get("deal_ids") or []) if str(x).isdigit()}
            _save_mailer_selection(ids)
            return {"statusCode": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"ok": True, "count": len(ids)})}
        return {"statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"ok": False, "error": "unknown action"})}

    # TEMP SEED ROUTE — remove once the update queue has real scanner data.
    if params.get("seed") == "xsight":
        _seed_key = ("https://www.prnewswire.com/news-releases/xsight-labs-raises-more-than-300-million-at-2-8-billion-valuation-to-power-next-generation-ai-and-cloud-networks-302838293.html"
                     "|138329336")
        _seed_items = _load_pending_updates()
        _seed_items[_seed_key] = {
            "company": "Xsight",
            "co_id": 138329336,
            "headline": "Xsight Labs Raises More than $300 Million at $2.8 Billion Valuation to Power Next-Generation AI and Cloud Networks",
            "url": "https://www.prnewswire.com/news-releases/xsight-labs-raises-more-than-300-million-at-2-8-billion-valuation-to-power-next-generation-ai-and-cloud-networks-302838293.html",
            "date": "2026-07-30",
            "catalyst": "Closed $300.0M round Jul '26 - details pending",
            "round_series": None,
            "valuation_usd": 2800000000,
            "valuation_basis": "post",
            "raise_amount_usd": 300000000,
            "pps_usd": None,
            "cur_lr_val": None,
            "cur_lr_pps": None,
            "cur_lr_date": None,
            "cur_lr_series": None,
            "found_at": "2026-08-03",
            "status": "pending",
        }
        try:
            boto3.client("s3").put_object(
                Bucket=S3_BUCKET, Key="pending-updates.json",
                Body=json.dumps({"items": _seed_items}, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
            )
            return {"statusCode": 200,
                    "headers": {"Content-Type": "text/plain"},
                    "body": f"Seeded. Queue now has {len(_seed_items)} item(s)."}
        except Exception as e:
            return {"statusCode": 500,
                    "headers": {"Content-Type": "text/plain"},
                    "body": f"Seed failed: {e}"}

    if params.get("view") == "mailer" and not params.get("list"):
        pid_raw = str(params.get("pid") or "").strip()
        pid = int(pid_raw) if pid_raw.isdigit() else MAILER_PREVIEW_PID
        return _render_mailer_composer(pid)

    if params.get("view") == "mailer":
        sid = str(params.get("search") or "").strip()
        if sid.isdigit():
            return _render_mailer_page(int(sid))
        return _render_mailer_page()

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
