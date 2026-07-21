# Scheduled Telegram Business Reports

**Status: production-ready.** Live-verified on 2026-07-21; morning and evening
test reports confirmed received in Telegram by the owner.

---

## What it does

Sends a business report to selected Telegram recipients twice a day — 06:00 and
18:00 in the **company's configured timezone**. Each delivery is a combined
all-branches report followed by one report per branch, optionally with a
structured PDF attachment.

* **Morning (06:00)** — previous day, closed out. Final sales, costs, profit,
  cash to deposit, yesterday's missing clock-outs, stock and licence alerts,
  today's preparation items.
* **Evening (18:00)** — today so far, explicitly labelled *"as of 18:00 — not a
  final full-day report"*.

## Architecture

```
Render worker (telegram_worker/worker.py)
  every 60s ──► GET  /api/telegram/reports/due       "is it 06:00 or 18:00 locally?"
            ──► POST /api/telegram/reports/claim     atomically claim the delivery
            ──► POST /api/telegram/reports/render    messages, pre-split
            ──► POST /api/telegram/reports/pdf       optional attachment
            ──► Telegram sendMessage / sendDocument
            ──► POST /api/telegram/reports/complete  outcome + audit
```

The worker holds **no schedule of its own**. The schedule lives in the database
plus the company timezone, so a restart, redeploy or scale-out loses nothing.

### Idempotency

`report_deliveries.idem_key` is UNIQUE:

```
smokestack | <tg_id> | <morning|evening> | <business_date> | <slot>
```

A worker claims a delivery by INSERTing that key. A second instance, or the same
instance after a restart, hits the constraint and skips. Manual sends use a
`manual|...|<timestamp>` key so they can never consume a scheduled slot.

### Timezone

Resolution order: `company_settings.business_timezone` → branch config → UTC.
`zoneinfo.ZoneInfo` applies DST automatically, so 06:00 stays 06:00 across a
daylight-saving change. Currently set to **Asia/Hebron** (+03:00 in summer,
+02:00 in winter). The Render server's UTC clock is never used to decide when a
slot fires.

### Data integrity

Every financial figure comes from `routers/core._costs_profit`, `_sum` and
`_purchases_sum` — the same helpers behind the dashboard, Reports page and
Financial Control Center. No formula is duplicated, so a report cannot drift
from the ERP. A value that cannot be computed renders as **"Not available"**,
never as a silent zero.

### Security

Recipients are resolved through the existing permission engine. Configuration
can only ever **narrow** an employee's ERP branch scope, never widen it — a
branch manager configured with three branches still receives only their own.
Disabled or unlinked Telegram accounts receive nothing. Every delivery and every
configuration change is written to the audit log.

### Failure handling

Per-message retry with exponential backoff (3 attempts). If some messages land
and others fail the delivery is marked `partial` rather than lost; the log
records the reason and the owner can resend. Statuses: `pending`, `processing`,
`sent`, `partial`, `failed`, `skipped`.

## Files

| File | Role |
|---|---|
| `app/reports_tg.py` | timezone, data collection, formatting, alerts, message splitting, PDF |
| `app/routers/telegram.py` | recipients, timezone, due/claim/render/pdf/complete/pending, deliveries, send-now, preview |
| `telegram_worker/worker.py` | 60-second scheduler loop, delivery, retries |
| `index.html` | Scheduled Reports panel: timezone control, recipients, manual buttons, delivery log |
| `tests/test_scheduled_reports.py` | 22 backend tests |
| `../rp-ui-test.js` | 55 UI assertions |

## Migrations

* `j9e0f1a2b3c4` — `report_recipients`, `report_deliveries` (+ unique `idem_key`)
* `k0f1a2b3c4d5` — `company_settings`, seeded from existing branch timezone

Both additive; no existing table is altered.

## Deliberately not implemented

The `report_recipients` table carries two columns that are **not** wired to any
behaviour, and are therefore **not exposed** in the API or UI:

* `language` — reports are English-only.
* `urgent_alerts` — alerts are delivered inside the scheduled reports, not
  pushed immediately when a condition occurs.

They are left in the schema for a future implementation. Do not surface them in
the UI until the behaviour exists.

## Verified in production

* Timezone Asia/Hebron: business time 14:26 (+0300) vs server UTC 11:26.
  Next runs 18:00 local = 15:00 UTC, 06:00 local = 03:00 UTC.
* Morning test report — 4 messages + PDF, 0 retries, status `sent`.
* Evening test report — 4 messages + PDF, 0 retries, status `sent`.
* Owner confirmed receipt in Telegram.
