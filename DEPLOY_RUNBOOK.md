# Dripp Tracker — Deploy Runbook

Backend for Dripp Cann Spirits' two LCBO SKUs:

| SKU | Brand | Product | Price (verified lcbo.com 2026-07-14) |
| --- | --- | --- | --- |
| 0014318 | Phoenix | Phoenix Ultra Smooth Vodka | $31.15 / 750 mL |
| 0044451 | Dayaa | Dayaa Arak | $29.85 / 750 mL |

Reps (internal roster): Ikshit, Vaneet, Ed, Namit. Owner-facing surfaces show
every rep as a GTA region label (GTA CENTRAL / GTA WEST / GTA NORTH /
GTA EAST; unknown values collapse to "GTA") and the owner view is
fail-closed: only the allowlisted endpoints respond, everything else under
/api returns 403. Field-work attribution baseline: 2026-07-15.

Local dev: `python3 app.py` → port **5070** (SQLite dev file `drippcan.db`).
Frontend (`drippcan-web`) runs on port **3002** (`next dev -p 3002`).

## Deploy order

### 1. Neon — new Postgres project `drippcan`
- Create a NEW Neon project named `drippcan`. Copy its `DATABASE_URL`
  (with `?sslmode=require`).
- NEVER reuse another app's database (lcbo-tracker and anu-imports each have
  their own Neon projects — this one is separate by design).
- Tables are created by the app on first boot (`init_db()` is idempotent,
  CREATE TABLE IF NOT EXISTS + additive migrations only).

### 2. Render — blueprint deploy (`render.yaml`)
- New Blueprint from this repo. Service: **drippcan-tracker**, plan **free**
  (comment in render.yaml covers the $7 starter upgrade path).
- Set the environment variables (see `.env.example` for the full annotated list):
  - `DATABASE_URL` — the NEW `drippcan` Neon URL, never any other app's
  - `SOD_USER` / `SOD_PASSWORD` / `SOD_AGENT_ID=1113` — same LCBO SOD account
    as the other trackers
  - `ADMIN_TOKEN` — FRESH token (`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`)
  - `SOD_CRON_TOKEN` — FRESH token; also stored as the GitHub Actions secret
  - `OWNER_PASSCODE` — FRESH strong passcode for the owner (brand) view
  - `RESEND_API_KEY` — email alerts + daily backup-to-email
  - `ALERT_EMAIL_TO` — REQUIRED for the daily 02:00 ET backup-to-email.
    Without it the backup silently skips ("data stored forever" breaks).
    Verified missing on Render 2026-07-14 — set it in the same pass as the
    deploy (the main session owns the Render dashboard step).
  - `CORS_ORIGINS` — optional; defaults already cover
    `https://drippcan-web.vercel.app` + `http://localhost:3002`
- Verify `https://drippcan-tracker.onrender.com/healthz` responds (free tier
  cold-starts in ~50s; 503 "stale" is expected before the first SOD sync).

### 3. GitHub Action — the daily wake (`.github/workflows/sod-daily.yml`)
- The free web service sleeps, so there is deliberately NO Render cron block.
  A GitHub Action wakes the service and hits the protected endpoints:
  - daily `15 8 * * *` UTC → `POST /api/sod/cron` (SOD ingest)
  - `0 13,17,21 * * *` UTC → `POST /api/live/refresh` (live-scrape safety net)
- The workflow file cannot be pushed with a git token that lacks the
  `workflow` scope — commit it through the GitHub web UI at deploy time.
- Add repo secret `SOD_CRON_TOKEN` (must match the Render env var).

### 4. Vercel — `drippcan-web` frontend
- Import the `drippcan-web` repo into Vercel.
- Env var: `NEXT_PUBLIC_API_BASE=https://drippcan-tracker.onrender.com`.
- Local dev uses the same-origin proxy pattern (`.env.local` with an empty
  `NEXT_PUBLIC_API_BASE`), rewriting to `localhost:5070`.

### 5. Seed + first sync
```bash
BASE=https://drippcan-tracker.onrender.com
# Territory seed (189 stores; idempotent, re-runnable, never deletes)
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" $BASE/api/territory/ingest
# First live lcbo.com refresh (both SKUs, one polite batch)
curl -X POST -H "Authorization: Bearer $SOD_CRON_TOKEN" $BASE/api/live/refresh
# First SOD ingest (or wait for the scheduled Action)
curl -X POST -H "Authorization: Bearer $SOD_CRON_TOKEN" $BASE/api/sod/cron
```

### 6. Verify end to end
- `GET /healthz` → 200 after the first SOD sync.
- `GET /api/territory` → 189 stores.
- `GET /api/live/latest?sku=0014318` and `?sku=0044451` → store rows.
- `GET /api/reconcile?days=7` → rows with per-source timestamps.
- Owner view: `?view=owner` responses contain no rep names and no notes.
- Frontend loads at `https://drippcan-web.vercel.app` against the Render API.

## Data forever (backup layers)
- **Neon is the primary forever store.** All production data lives in the
  `drippcan` Neon Postgres project; the Render host is disposable.
- **Daily 02:00 ET backup-to-email** (`start_backup_scheduler` in app.py)
  emails a full JSON export of every CRM/audit/territory/live-engine table
  as an attachment via Resend. It needs BOTH `RESEND_API_KEY` and
  `ALERT_EMAIL_TO` set on Render — with either missing it silently skips.
  Trigger a manual run any time: `POST /api/admin/run-backup-now` with
  `X-Admin-Token`.
- **Neon free-tier PITR window is ~6 hours** — point-in-time restore only
  covers the same working day. The email backup is the long-horizon safety
  net; the restore path is `POST /api/admin/import?mode=merge` with the
  attachment.
- **Retention guard:** `sod_inventory`, `lcbo_live_snapshots`, `activities`
  and `territory_status_history` are append-only. No code path hard-deletes
  from them (tests grep the source); the SOD snapshot rollback moves rows to
  `sod_inventory_archive` instead of destroying them, and import
  `mode=replace` refuses to truncate protected tables (falls back to merge).

## Standing rules
- Postgres in production, SQLite only for local dev. Inventory and status
  snapshots are APPEND-ONLY; never destructive updates.
- Never point `DATABASE_URL` at another app's database.
- Postgres date columns: never `COALESCE(date_col, '')` — CAST AS TEXT first.
- The SOD ingest SAVEPOINT recovery in app.py must stay; never reintroduce
  `conn.rollback()` in sync recovery.
# NOTE: deploy/sod-daily.yml must be created as .github/workflows/sod-daily.yml via the GitHub web UI (git tokens lack workflow scope).
