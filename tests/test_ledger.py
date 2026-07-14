"""Canonical listing ledger — the #1 feature (sections A + B + C).

Tracking accuracy IS the product. These tests assert that:
  - listing_ledger + store_listings exist in the SQLite init_db branch,
  - _ledger_record folds LISTED / RECONFIRMED / DELISTED correctly, including
    the DELISTED-only-if-observed>=last-LISTED guard,
  - the ledger insert is idempotent (UNIQUE guard, one row/day/source),
  - store_listings is a PURE fold of the ledger (rebuild == incremental),
  - /api/listings/record (manual), /api/listings/backfill and
    /api/listings/rebuild behave and are idempotent,
  - the SOD-loss guarantee holds: wipe every SOD table, rebuild from the
    ledger alone, and store_listings is byte-for-byte the same,
  - the immutable ledger is never UPDATE-d or DELETE-d anywhere in app.py.

Run with: python3 -m pytest tests/test_ledger.py -v
"""
import os
import re
import sqlite3
import sys

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
os.environ.pop('SOD_CRON_TOKEN', None)

TEST_DB_DIR = '/tmp/drippcan_ledger_test'
os.makedirs(TEST_DB_DIR, exist_ok=True)
TEST_DB = os.path.join(TEST_DB_DIR, 'drippcan.db')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

PHOENIX = '0014318'
DAYAA = '0044451'
APP_PY = os.path.join(os.path.dirname(__file__), '..', 'app.py')


@pytest.fixture(scope='module')
def app_module():
    prev_db_dir = os.environ.get('DB_DIR')
    os.environ['DB_DIR'] = TEST_DB_DIR
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    for mod in list(sys.modules):
        if mod == 'app' or mod.startswith('app.'):
            del sys.modules[mod]
    import importlib.util
    spec = importlib.util.spec_from_file_location('app', APP_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    yield m
    if prev_db_dir is None:
        os.environ.pop('DB_DIR', None)
    else:
        os.environ['DB_DIR'] = prev_db_dir


@pytest.fixture
def client(app_module):
    app_module.app.config['TESTING'] = True
    try:
        app_module._rate_buckets.clear()
    except Exception:
        pass
    with app_module.app.test_client() as c:
        yield c


def _db():
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _reset_ledger():
    """Clear ledger + cache tables between tests (never touched in prod)."""
    conn = _db()
    for t in ('listing_ledger', 'store_listings', 'sod_store_sku_changes',
              'live_listing_events', 'sod_inventory', 'sod_products', 'event_log'):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def _record(app_module, sku, store, event, source, detail, observed, note=''):
    conn = _db()
    cur = conn.cursor()
    ok = app_module._ledger_record(cur, sku, store, event, source, detail, observed, note=note)
    conn.commit()
    conn.close()
    return ok


def _listing(sku, store):
    conn = _db()
    row = conn.execute(
        "SELECT status, first_listed_date, last_confirmed_date, delisted_date, "
        "sources_seen, confirm_count FROM store_listings WHERE sku=? AND store_number=?",
        (sku, store)).fetchone()
    conn.close()
    return row


# ── A. Schema exists in the SQLite init_db branch ──────────────────────────
def test_tables_exist(app_module):
    conn = _db()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert 'listing_ledger' in tables
    assert 'store_listings' in tables


def test_ledger_unique_guard(app_module):
    _reset_ledger()
    conn = _db()
    conn.execute(
        "INSERT INTO listing_ledger (sku, store_number, event, source, observed_date) "
        "VALUES (?,?,?,?,?)", (PHOENIX, 100, 'LISTED', 'sod', '2026-07-01'))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO listing_ledger (sku, store_number, event, source, observed_date) "
            "VALUES (?,?,?,?,?)", (PHOENIX, 100, 'LISTED', 'sod', '2026-07-01'))
        conn.commit()
    conn.close()


# ── B. Fold semantics ──────────────────────────────────────────────────────
def test_fold_listed_sets_first_and_status(app_module):
    _reset_ledger()
    assert _record(app_module, PHOENIX, 201, 'LISTED', 'sod', '2026-07-02', '2026-07-02')
    row = _listing(PHOENIX, 201)
    assert row['status'] == 'LISTED'
    assert row['first_listed_date'] == '2026-07-02'
    assert row['last_confirmed_date'] == '2026-07-02'
    assert row['delisted_date'] is None
    assert row['confirm_count'] == 1
    assert row['sources_seen'] == 'sod'


def test_fold_reconfirm_bumps_and_first_listed_is_min(app_module):
    _reset_ledger()
    _record(app_module, PHOENIX, 202, 'LISTED', 'sod', '2026-07-05', '2026-07-05')
    _record(app_module, PHOENIX, 202, 'LISTED', 'live', 'b1', '2026-07-02')  # earlier
    _record(app_module, PHOENIX, 202, 'RECONFIRMED', 'rep', 'Namit', '2026-07-09')
    row = _listing(PHOENIX, 202)
    assert row['first_listed_date'] == '2026-07-02'      # min over LISTED
    assert row['last_confirmed_date'] == '2026-07-09'     # max over LISTED+RECONFIRM
    assert row['confirm_count'] == 3
    assert row['sources_seen'] == 'live,rep,sod'          # sorted, deduped
    assert row['status'] == 'LISTED'


def test_fold_delisted_guard_ignores_stale(app_module):
    _reset_ledger()
    _record(app_module, PHOENIX, 203, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    _record(app_module, PHOENIX, 203, 'RECONFIRMED', 'sod', '2026-07-05', '2026-07-05')
    # Stale delist observed BEFORE the latest presence proof → ignored.
    _record(app_module, PHOENIX, 203, 'DELISTED', 'sod', '2026-07-03', '2026-07-03')
    row = _listing(PHOENIX, 203)
    assert row['status'] == 'LISTED'
    assert row['delisted_date'] is None
    # A delist on/after the latest confirmation wins.
    _record(app_module, PHOENIX, 203, 'DELISTED', 'sod', '2026-07-10', '2026-07-10')
    row = _listing(PHOENIX, 203)
    assert row['status'] == 'DELISTED'
    assert row['delisted_date'] == '2026-07-10'
    assert row['confirm_count'] == 2  # DELISTED never counts as a confirmation


def test_ledger_record_is_idempotent(app_module):
    _reset_ledger()
    assert _record(app_module, PHOENIX, 204, 'LISTED', 'sod', '2026-07-01', '2026-07-01') is True
    assert _record(app_module, PHOENIX, 204, 'LISTED', 'sod', '2026-07-01', '2026-07-01') is False
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM listing_ledger WHERE sku=? AND store_number=?",
                     (PHOENIX, 204)).fetchone()[0]
    conn.close()
    assert n == 1
    assert _listing(PHOENIX, 204)['confirm_count'] == 1


def test_ledger_record_audits_event_log(app_module):
    _reset_ledger()
    _record(app_module, PHOENIX, 205, 'LISTED', 'manual', 'test', '2026-07-01')
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM event_log WHERE event_type='listing_listed'").fetchone()[0]
    conn.close()
    assert n >= 1


# ── Manual record endpoint (§B) ────────────────────────────────────────────
def test_manual_record_endpoint(app_module, client):
    _reset_ledger()
    resp = client.post('/api/listings/record', json={
        'sku': PHOENIX, 'store_number': 300, 'event': 'LISTED',
        'observed_date': '2026-07-01', 'note': 'known placement'})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body['inserted'] is True
    assert body['listing']['status'] == 'LISTED'
    assert body['listing']['first_listed_date'] == '2026-07-01'
    # Bad event rejected.
    bad = client.post('/api/listings/record', json={
        'sku': PHOENIX, 'store_number': 300, 'event': 'NONSENSE'})
    assert bad.status_code == 400


# ── Rep observe-listing hook (§B) ──────────────────────────────────────────
def test_rep_observe_feeds_ledger(app_module, client):
    _reset_ledger()
    resp = client.post('/api/crm/observe-listing', json={
        'sku': PHOENIX, 'store_number': 400, 'rep': 'Namit', 'on_shelf': True})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    assert resp.get_json().get('ledger_event') == 'LISTED'
    row = _listing(PHOENIX, 400)
    assert row is not None and row['status'] == 'LISTED'
    assert 'rep' in (row['sources_seen'] or '')


# ── C. Backfill + rebuild ──────────────────────────────────────────────────
def _seed_sources():
    conn = _db()
    conn.executemany(
        "INSERT INTO sod_store_sku_changes "
        "(sku, store_number, change_date, old_status, new_status, change_type) "
        "VALUES (?,?,?,?,?,?)",
        [
            (PHOENIX, 501, '2026-07-01', None, 'L', 'NEW_LISTING'),
            (PHOENIX, 502, '2026-07-02', None, 'L', 'NEW_LISTING'),
            (DAYAA, 503, '2026-07-03', None, 'L', 'NEW_LISTING'),
            (PHOENIX, 502, '2026-07-06', 'L', None, 'DROPPED'),   # 502 later dropped
            (PHOENIX, 501, '2026-07-04', 'L', 'F', 'STATUS_FLIP'),  # ignored (not L/D)
        ])
    conn.executemany(
        "INSERT INTO live_listing_events "
        "(sku, store_number, event_type, old_qty, new_qty, batch_id, prev_batch_id, event_date) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (DAYAA, 504, 'LIVE_NEW_LISTING', None, 6, 'bat2', 'bat1', '2026-07-05'),
            (PHOENIX, 501, 'LIVE_RESTOCK', 2, 8, 'bat2', 'bat1', '2026-07-05'),
        ])
    conn.commit()
    conn.close()


def test_backfill_folds_and_is_idempotent(app_module, client):
    _reset_ledger()
    _seed_sources()
    r1 = client.post('/api/listings/backfill')
    assert r1.status_code == 200, r1.get_data(as_text=True)
    b1 = r1.get_json()
    # 3 NEW_LISTING + 1 DROPPED (sod) + 1 LIVE_NEW_LISTING + 1 LIVE_RESTOCK (live)
    assert b1['by_source'].get('sod') == 4
    assert b1['by_source'].get('live') == 2
    ledger1 = b1['ledger_rows']

    # 502 was listed then dropped → DELISTED; 501/503 LISTED; 504 LISTED.
    assert _listing(PHOENIX, 501)['status'] == 'LISTED'
    assert _listing(PHOENIX, 502)['status'] == 'DELISTED'
    assert _listing(DAYAA, 503)['status'] == 'LISTED'
    assert _listing(DAYAA, 504)['status'] == 'LISTED'

    # Re-running is a no-op (UNIQUE guard) — same totals, no duplicate rows.
    r2 = client.post('/api/listings/backfill')
    assert r2.get_json()['ledger_rows'] == ledger1


def test_rebuild_is_pure_fold(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    before = _snapshot_store_listings()
    r = client.post('/api/listings/rebuild')
    assert r.status_code == 200, r.get_data(as_text=True)
    after = _snapshot_store_listings()
    assert before == after
    # Rebuild touches only the derived cache, never the ledger.
    conn = _db()
    assert conn.execute("SELECT COUNT(*) FROM listing_ledger").fetchone()[0] == r.get_json()['ledger_rows']
    conn.close()


def _snapshot_store_listings():
    conn = _db()
    rows = conn.execute(
        "SELECT sku, store_number, status, first_listed_date, last_confirmed_date, "
        "delisted_date, sources_seen, confirm_count FROM store_listings "
        "ORDER BY sku, store_number").fetchall()
    conn.close()
    return [tuple(r) for r in rows]


# ── E (scoped). The SOD-loss guarantee ─────────────────────────────────────
def test_sod_loss_guarantee(app_module, client):
    """Seed the ledger, snapshot store_listings, then wipe every SOD table and
    rebuild from the ledger alone — the materialized state must be identical."""
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    before = _snapshot_store_listings()
    assert before  # non-empty

    # Simulate TOTAL SOD loss.
    conn = _db()
    for t in ('sod_inventory', 'sod_store_sku_changes', 'sod_products'):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()

    r = client.post('/api/listings/rebuild')
    assert r.status_code == 200
    after = _snapshot_store_listings()
    assert after == before  # every listing still known, first_listed_date intact


# ── The immutability invariant ─────────────────────────────────────────────
def test_ledger_never_updated_or_deleted():
    src = open(APP_PY, encoding='utf-8').read()
    assert not re.search(r'UPDATE\s+listing_ledger', src, re.IGNORECASE)
    assert not re.search(r'DELETE\s+FROM\s+listing_ledger', src, re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════════════════
# D. READ ENDPOINTS — /api/listings*, source-health, xlsx (§D)
# ═══════════════════════════════════════════════════════════════════════════
def _json(client, path, **headers):
    r = client.get(path, headers=headers or None)
    return r.status_code, (r.get_json() if r.is_json else None), r


def test_api_listings_current_state(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    code, body, _ = _json(client, '/api/listings?nocache=1')
    assert code == 200
    by = {(r['sku'], r['store_number']): r for r in body['rows']}
    assert by[(PHOENIX, 501)]['status'] == 'LISTED'
    assert by[(PHOENIX, 501)]['first_listed_date'] == '2026-07-01'
    assert by[(PHOENIX, 502)]['status'] == 'DELISTED'
    # brand + product_name resolved, days_since_confirmed present (int or None)
    assert by[(PHOENIX, 501)]['brand'] == 'Phoenix'
    assert 'days_since_confirmed' in by[(PHOENIX, 501)]
    # summary
    s = body['summary']
    assert s['listed'] >= 1 and s['delisted'] >= 1
    assert s['first_ever'] == '2026-07-01'
    assert 'sod' in s['by_source']


def test_api_listings_status_filter(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    code, body, _ = _json(client, '/api/listings?status=DELISTED&nocache=1')
    assert code == 200
    assert body['count'] >= 1
    assert all(r['status'] == 'DELISTED' for r in body['rows'])


def test_api_listings_added_window_and_attribution(app_module, client):
    """The tracking feature: LISTED events from the ledger with attribution.
    baseline (on/before LAUNCH_DATE) vs organic (after, no prior touchpoint)."""
    _reset_ledger()
    # baseline: on/before LAUNCH_DATE 2026-07-15
    _record(app_module, PHOENIX, 601, 'LISTED', 'sod', '2026-07-10', '2026-07-10')
    # organic: after LAUNCH_DATE, no touchpoint at the store
    _record(app_module, DAYAA, 602, 'LISTED', 'live', 'b9', '2026-07-20')
    code, body, _ = _json(client, '/api/listings/added?since=2026-07-01&nocache=1')
    assert code == 200
    attr = {(r['sku'], r['store_number']): r['attribution'] for r in body['rows']}
    assert attr[(PHOENIX, 601)] == 'baseline'
    assert attr[(DAYAA, 602)] == 'organic'
    # newest first
    assert body['rows'][0]['observed_date'] >= body['rows'][-1]['observed_date']
    assert body['summary']['baseline'] >= 1
    assert body['summary']['organic'] >= 1


def test_api_listings_added_reads_ledger_not_sod(app_module, client):
    """/added must answer from the ledger even with SOD wiped."""
    _reset_ledger()
    _record(app_module, PHOENIX, 610, 'LISTED', 'manual', 'known', '2026-07-05')
    conn = _db()
    for t in ('sod_inventory', 'sod_store_sku_changes', 'sod_products'):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    code, body, _ = _json(client, '/api/listings/added?since=2026-07-01&nocache=1')
    assert code == 200
    assert any(r['store_number'] == 610 for r in body['rows'])


def test_api_listings_store_timeline(app_module, client):
    _reset_ledger()
    _record(app_module, PHOENIX, 700, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    _record(app_module, PHOENIX, 700, 'RECONFIRMED', 'live', 'b1', '2026-07-05')
    code, body, _ = _json(client, '/api/listings/store/700')
    assert code == 200
    assert body['store_number'] == 700
    assert body['event_count'] == 2
    assert len(body['current']) == 1
    assert body['current'][0]['status'] == 'LISTED'
    events = {(e['event'], e['source']) for e in body['events']}
    assert ('LISTED', 'sod') in events
    assert ('RECONFIRMED', 'live') in events


def test_api_listings_ledger_stream(app_module, client):
    _reset_ledger()
    _record(app_module, PHOENIX, 710, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    _record(app_module, PHOENIX, 710, 'DELISTED', 'sod', '2026-07-10', '2026-07-10')
    code, body, _ = _json(client, '/api/listings/ledger?days=3650&nocache=1')
    assert code == 200
    assert body['by_event'].get('LISTED', 0) >= 1
    assert body['by_event'].get('DELISTED', 0) >= 1
    # sku filter
    code2, body2, _ = _json(client, f'/api/listings/ledger?sku={PHOENIX}&days=3650')
    assert code2 == 200
    assert all(r['sku'] == PHOENIX for r in body2['rows'])


def test_api_source_health_staleness(app_module, client):
    _reset_ledger()
    today = app_module._toronto_today()
    from datetime import timedelta
    old = (today - timedelta(days=10)).isoformat()
    fresh = (today - timedelta(days=1)).isoformat()
    # sod last seen 10 days ago → stale; live seen yesterday → fresh
    _record(app_module, PHOENIX, 720, 'LISTED', 'sod', old, old)
    _record(app_module, PHOENIX, 721, 'LISTED', 'live', 'b1', fresh)
    code, body, _ = _json(client, '/api/listings/source-health')
    assert code == 200
    by = {s['source']: s for s in body['sources']}
    assert by['sod']['last_observed_date'] == old
    assert by['sod']['is_stale'] is True
    assert by['live']['is_stale'] is False
    # rep/manual are ad-hoc — never flagged stale even with no rows
    assert by['rep']['is_stale'] is False
    assert body['any_stale'] is True and 'sod' in body['stale_sources']


def test_export_listings_xlsx(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    r = client.get('/api/export/listings.xlsx')
    assert r.status_code == 200
    assert r.data[:2] == b'PK'  # xlsx is a zip container
    assert 'attachment' in r.headers.get('Content-Disposition', '')
    assert 'dripp_listings_' in r.headers.get('Content-Disposition', '')


# ── Caching: heavy reads cache + invalidate on write ───────────────────────
def test_api_listings_is_cached_and_invalidated(app_module, client):
    _reset_ledger()
    _record(app_module, PHOENIX, 730, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    r1 = client.get('/api/listings')
    assert r1.headers.get('X-Cache') == 'MISS'
    r2 = client.get('/api/listings')
    assert r2.headers.get('X-Cache') == 'HIT'
    # A manual record must invalidate the cache (next read reflects it).
    client.post('/api/listings/record', json={
        'sku': PHOENIX, 'store_number': 731, 'event': 'LISTED',
        'observed_date': '2026-07-02'})
    r3 = client.get('/api/listings')
    assert r3.headers.get('X-Cache') == 'MISS'
    assert any(row['store_number'] == 731 for row in r3.get_json()['rows'])


# ── Empty-data safety: no endpoint 500s on an empty ledger ─────────────────
def test_read_endpoints_empty_data_no_500(app_module, client):
    _reset_ledger()
    for path in ('/api/listings?nocache=1', '/api/listings/added?nocache=1',
                 '/api/listings/store/99999', '/api/listings/ledger?nocache=1',
                 '/api/listings/source-health', '/api/admin/integrity'):
        code, body, _ = _json(client, path)
        assert code == 200, f'{path} -> {code}'
    r = client.get('/api/export/listings.xlsx')
    assert r.status_code == 200 and r.data[:2] == b'PK'


# ═══════════════════════════════════════════════════════════════════════════
# E. THE DESTRUCTIVE SOD-LOSS GUARANTEE — the whole point (§E)
# store_listings AND the read endpoints are identical after wiping every SOD
# table and rebuilding from the ledger alone.
# ═══════════════════════════════════════════════════════════════════════════
def test_sod_loss_guarantee_full_including_read_endpoints(app_module, client):
    _reset_ledger()
    _seed_sources()
    # add a rep + manual proof so the guarantee covers non-SOD sources too
    _record(app_module, PHOENIX, 810, 'LISTED', 'rep', 'Namit', '2026-07-04')
    _record(app_module, DAYAA, 811, 'LISTED', 'manual', 'known placement', '2026-07-02')
    client.post('/api/listings/backfill')

    before_sl = _snapshot_store_listings()
    before_listings = client.get('/api/listings?nocache=1').get_json()['rows']
    before_added = client.get(
        '/api/listings/added?since=2026-06-01&nocache=1').get_json()['rows']
    assert before_sl and before_listings and before_added

    # Simulate TOTAL SOD loss — delete every sod_* row in the scratch DB.
    conn = _db()
    for t in ('sod_inventory', 'sod_store_sku_changes', 'sod_products'):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()

    r = client.post('/api/listings/rebuild')
    assert r.status_code == 200

    after_sl = _snapshot_store_listings()
    after_listings = client.get('/api/listings?nocache=1').get_json()['rows']
    after_added = client.get(
        '/api/listings/added?since=2026-06-01&nocache=1').get_json()['rows']

    # Every listing still known, first_listed_date intact, both read views equal.
    assert after_sl == before_sl
    assert after_listings == before_listings
    assert after_added == before_added
    # The rep + manual sources survived the SOD wipe (they were never in SOD).
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM store_listings WHERE store_number IN (810, 811)"
    ).fetchone()[0]
    conn.close()
    assert n == 2


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT SWEEP 1-4
# ═══════════════════════════════════════════════════════════════════════════
# 1 + 2. Backup + retention completeness — the ledger is the crown jewel.
def test_export_tables_include_ledger_and_backup_set(app_module):
    names = {t for t, _ in app_module._EXPORT_TABLES}
    required = {
        'listing_ledger', 'store_listings', 'territory_stores',
        'territory_status_history', 'lcbo_live_snapshots', 'lcbo_live_batches',
        'live_listing_events', 'rep_listing_observations', 'activities', 'deals',
    }
    missing = required - names
    assert not missing, f'_EXPORT_TABLES missing: {sorted(missing)}'


def test_retention_protected_includes_ledger(app_module):
    prot = set(app_module._RETENTION_PROTECTED_TABLES)
    assert 'listing_ledger' in prot
    assert 'store_listings' in prot


def test_essential_email_backup_carries_ledger(app_module):
    _reset_ledger()
    _record(app_module, PHOENIX, 900, 'LISTED', 'manual', 'x', '2026-07-01')
    with app_module.app.test_request_context('/'):
        payload = app_module._build_essential_backup()
    assert 'listing_ledger' in payload['tables']
    assert 'store_listings' in payload['tables']
    assert 'error' not in payload['tables']['listing_ledger']
    # the giant snapshot table stays out of the email (size), as designed
    assert 'sod_inventory' not in payload['tables']


# 1. Retention path — _archive_then_remove clones <table>_archive lazily and
#    moves rows there instead of destroying them.
def test_archive_then_remove_preserves_rows(app_module):
    conn = _db()
    conn.execute("DELETE FROM sod_inventory")
    for sn in (9401, 9402, 9403):
        conn.execute(
            "INSERT INTO sod_inventory "
            "(sku, store_number, snapshot_date, status, on_hand, product_name) "
            "VALUES (?,?,?,?,?,?)",
            (DAYAA, sn, '2020-02-02', 'L', 4, 'DAYAA ARAK'))
    conn.commit()
    cur = conn.cursor()
    removed = app_module._archive_then_remove(
        cur, 'sod_inventory', "snapshot_date=?", ('2020-02-02',))
    conn.commit()
    assert removed == 3
    # rows are gone from the live table but preserved in the archive clone
    live = conn.execute(
        "SELECT COUNT(*) FROM sod_inventory WHERE snapshot_date='2020-02-02'"
    ).fetchone()[0]
    arch = conn.execute(
        "SELECT COUNT(*) FROM sod_inventory_archive WHERE snapshot_date='2020-02-02'"
    ).fetchone()[0]
    conn.close()
    assert live == 0
    assert arch == 3


# 3. Data-integrity audit endpoint.
def test_admin_integrity_all_clear(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    code, body, _ = _json(client, '/api/admin/integrity')
    assert code == 200
    assert body['all_clear'] is True
    assert body['counts']['listings_without_ledger'] == 0
    assert body['counts']['ledger_orphans'] == 0
    assert body['counts']['ledger_rows'] >= 1


def test_admin_integrity_detects_drift(app_module, client):
    """A store_listings row with no backing ledger row is caught."""
    _reset_ledger()
    _record(app_module, PHOENIX, 950, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    # Inject an orphan directly into the materialized cache (no ledger backing).
    conn = _db()
    conn.execute(
        "INSERT INTO store_listings (sku, store_number, status, confirm_count) "
        "VALUES (?,?,?,?)", ('9999999', 8888, 'LISTED', 1))
    conn.commit()
    conn.close()
    code, body, _ = _json(client, '/api/admin/integrity')
    assert code == 200
    assert body['counts']['listings_without_ledger'] >= 1
    assert body['all_clear'] is False


# 4. COALESCE(<date>,'') is the Postgres crash class — must not appear.
def test_no_coalesce_date_to_empty_string():
    src = open(APP_PY, encoding='utf-8').read()
    hits = re.findall(
        r"COALESCE\([^)]*(?:observed_date|change_date|event_date|snapshot_date|"
        r"first_listed_date|last_confirmed_date|delisted_date|recorded_at|"
        r"detected_at|created_at|updated_at)[^)]*,\s*''\s*\)",
        src, re.IGNORECASE)
    assert hits == [], f'COALESCE(date, \'\') crash class found: {hits}'


# ── Owner view: allowlisted + sanitized, internal paths fail closed ─────────
def test_owner_view_listings_allowlisted_and_sanitized(app_module, client):
    _reset_ledger()
    # a rep-sourced listing carries a rep identity in source_detail
    _record(app_module, PHOENIX, 970, 'LISTED', 'rep', 'Namit', '2026-07-05')
    OWNER = {'X-View': 'owner'}
    # allowlisted reads reachable in owner view
    assert client.get('/api/listings', headers=OWNER).status_code == 200
    r_add = client.get('/api/listings/added?since=2026-07-01', headers=OWNER)
    assert r_add.status_code == 200
    txt = r_add.get_data(as_text=True)
    assert 'Namit' not in txt          # rep identity scrubbed
    assert 'GTA' in txt                # replaced with a region label
    assert client.get('/api/export/listings.xlsx', headers=OWNER).status_code == 200
    # internal ledger surfaces fail closed for the owner
    assert client.get('/api/listings/ledger', headers=OWNER).status_code == 403
    assert client.get('/api/listings/store/970', headers=OWNER).status_code == 403
    assert client.get('/api/listings/source-health', headers=OWNER).status_code == 403
    # the internal (non-owner) view still shows the rep identity
    assert 'Namit' in client.get(
        '/api/listings/added?since=2026-07-01&nocache=1').get_data(as_text=True)
