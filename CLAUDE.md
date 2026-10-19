# daily-brief — project memory

AWS Lambda (Python, single file `lambda_function.py`) behind a Lambda URL
(`MAILER_BASE_URL`). Scheduled invocations email the Daily Brief; HTTP requests serve the
admin pages (brief, weekly mailer composer, news mailer) and the public mailer routes.
Deployed to the `daily-brief` Lambda by `.github/workflows/deploy.yml` on push to `main`
(zips `lambda_function.py` only — stdlib + boto3, no third-party imports).

## Verify
- `python3.12 -m py_compile lambda_function.py`
- `python3.12 -m unittest discover -s tests` (boto3 is stubbed; no AWS/Pipeline access needed)

## HTTP routing (`handle_http_request`)
- Public (no key): `view=click` (mailer click tracking), `view=daily` (Daily Highlight signup),
  `view=unsub` (weekly mailer unsubscribe).
- Everything else requires `key=BRIEF_PAGE_KEY`.

## Weekly mailer ("Weekly Pre-IPO secondary transactions")
- Recipients: Pipeline saved search 19530439 "S: Weekly Mailer Leads" (`_fetch_mailer_rows`).
- Sent per recipient via SES `send_raw_email` (`_mailer_send_one`) so it can carry
  `List-Unsubscribe` (https endpoint + `mailto:cgracia@graciagroup.com?subject=unsubscribe`)
  and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. The Lambda role needs
  `ses:SendRawEmail`.
- Footer (`_mailer_unsub_footer`): per-recipient Unsubscribe link. No postal address (by choice).

## Unsubscribe
- Token (`_unsub_token` / `_parse_unsub_token`): opaque, `pid|email|issued` encrypted with
  an HMAC-SHA256 keystream + 16-byte HMAC tag; keys derived from `MAILER_CLICK_SECRET`
  (fails closed if unset). Valid for `UNSUB_TOKEN_TTL_DAYS` (365).
- `GET ?view=unsub&u=…` records + confirmation page; `POST` (one-click) records, empty 200.
  Not routed through click tracking — never counts as a click.
- Recording (`_record_unsub`): S3 opt-out list `full-pipeline-cache/mailer-unsub.json`
  (required), then Pipeline tag "Unsub: Weekly" via `PUT /people/{id}.json`
  `predefined_contacts_tag_ids`; tag id from env `UNSUB_WEEKLY_TAG_ID`, else looked up /
  created via `/admin/predefined_contacts_tags.json`. Chad gets an SES alert on new
  unsubscribes and on any Pipeline tag failure.
- An unsubscribe means out of ALL recurring mailers (weekly + news). The tag keeps the name
  "Unsub: Weekly" (it lives in Pipeline; renaming would need a migration).
- Send-time guard (`_mailer_suppressed` + `_drop_suppressed`): skips anyone on the S3 list or
  tagged in the `people.json` cache, even if the saved search returns them. Applied in
  `_handle_mailer_send` and `_handle_news_send`; skipped counts appear in both send reports,
  both composers (`_recipient_count_line`) and the recipient-list page.
- The news mailer only has the skip — no unsubscribe footer link or List-Unsubscribe headers.
- The broadcast mailer (unique messages to a pre-filtered list) intentionally does NOT check
  the tag. Don't add it there.

## Pipeline API
- JWT (S3 `pipeline-token/pipeline-jwt.json`) via `call_pipeline_api` — used for person/company
  reads and writes.
- `PIPELINE_API_KEY` + `PIPELINE_APP_KEY` query-string auth — used only for saved-search
  `perform.json`.
