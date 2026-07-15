"""ANU ACCOUNTS — the permanent touched-store billing ledger.

Asserts the money-protection chain:
  - any logged rep touch permanently claims the store (ANU-<store#> ref),
  - a backdated visit moves the claim EARLIER, never later,
  - LISTED ledger events classify against the claim: baseline (existed at
    launch) / billable (new listing on/after our touch) / listed_before_touch,
  - the owner view is allowlisted, carries the evidence, and never leaks a
    real rep name (GTA region labels only),
  - the xlsx export serves both views,
  - the backfill self-heals a claim that missed the write hook.

Run with: python3 -m pytest tests/test_anu_accounts.py -v
"""
import os
import sys

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
os.environ.pop('SOD_CRON_TOKEN', None)

TEST_DB_DIR = '/tmp/drippcan_anu_accounts_test'
os.makedirs(TEST_DB_DIR, exist_ok=True)
TEST_DB = os.path.join(TEST_DB_DIR, 'drippcan.db')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

PHOENIX = '0014318'
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
    return app_module.app.test_client()


@pytest.fixture(scope='module')
def seeded(app_module):
    """Two stores + the rep roster (roster is ensured at boot)."""
    with app_module.app.app_context():
        db = app_module.get_db()
        for sn, name in ((901, 'Test Uptown'), (902, 'Test Downtown')):
            db.execute(
                "INSERT OR IGNORE INTO stores (store_number, account, city) "
                "VALUES (?,?,?)", (sn, name, 'Toronto'))
        db.commit()
    return True


def _log(client, store_number, activity_type='store_visit', rep='Ikshit',
         visit_date=None):
    body = {'store_number': store_number, 'activity_type': activity_type,
            'rep': rep}
    if visit_date:
        body['visit_date'] = visit_date
    r = client.post('/api/crm/activities', json=body)
    assert r.status_code in (200, 201), r.get_json()
    return r.get_json()


def _accounts(client, owner=False):
    headers = {'X-View': 'owner'} if owner else {}
    r = client.get('/api/anu-accounts?nocache=1', headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


class TestClaim:
    def test_touch_claims_store_forever(self, seeded, client):
        _log(client, 901, 'store_visit')
        body = _accounts(client)
        row = next(r for r in body['rows'] if r['store_number'] == 901)
        assert row['account_ref'] == 'ANU-901'
        assert row['first_touch_type'] == 'store_visit'
        assert row['touches_total'] >= 1
        assert 'Ikshit' in row['reps']

    def test_second_touch_does_not_move_claim_later(self, seeded, client):
        before = next(r for r in _accounts(client)['rows']
                      if r['store_number'] == 901)['claimed_at']
        _log(client, 901, 'tasting', rep='Namit')
        after = next(r for r in _accounts(client)['rows']
                     if r['store_number'] == 901)
        assert after['claimed_at'] == before
        assert after['touches_total'] >= 2

    def test_backdated_visit_moves_claim_earlier(self, seeded, client):
        _log(client, 902, 'call', visit_date='2026-07-16')
        early = '2026-07-15'
        _log(client, 902, 'store_visit', visit_date=early)
        row = next(r for r in _accounts(client)['rows']
                   if r['store_number'] == 902)
        assert row['claimed_at'].startswith(early)
        assert row['first_touch_type'] == 'store_visit'


class TestBillingClassification:
    def test_listing_classes(self, seeded, client, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            cur = db.cursor()
            # Baseline: listed before/at launch (2026-07-15).
            app_module._ledger_record(cur, PHOENIX, 901, 'LISTED', 'sod',
                                      'test', '2026-07-13')
            # Billable: NEW listing after our 901 claim (claim is today).
            app_module._ledger_record(cur, PHOENIX, 901, 'LISTED', 'live',
                                      'test', '2099-01-01')
            # listed_before_touch: new listing on a store we touched later.
            db.execute(
                "INSERT OR IGNORE INTO stores (store_number, account, city) "
                "VALUES (?,?,?)", (903, 'Test Late Touch', 'Vaughan'))
            app_module._ledger_record(cur, PHOENIX, 903, 'LISTED', 'live',
                                      'test', '2026-07-16')
            db.commit()
        _log(client, 903, 'store_visit', visit_date='2026-07-20')

        body = _accounts(client)
        r901 = next(r for r in body['rows'] if r['store_number'] == 901)
        classes = {x['date']: x['classification'] for x in r901['listings']}
        assert classes['2026-07-13'] == 'baseline'
        assert classes['2099-01-01'] == 'billable'
        assert r901['billable_listings'] == 1
        r903 = next(r for r in body['rows'] if r['store_number'] == 903)
        assert r903['listings'][0]['classification'] == 'listed_before_touch'
        assert body['summary']['billable_listings'] >= 1
        assert body['summary']['accounts'] >= 3


class TestOwnerView:
    def test_owner_allowlisted_and_anonymized(self, seeded, client):
        body = _accounts(client, owner=True)
        assert body['summary']['accounts'] >= 3
        dump = str(body)
        for name in ('Ikshit', 'Vaneet', 'Namit'):
            assert name not in dump
        row = next(r for r in body['rows'] if r['store_number'] == 901)
        assert any('GTA' in x for x in row['reps'])
        # Evidence still visible to the owner:
        assert row['claimed_at']
        assert row['listings']

    def test_owner_cannot_hit_raw_activities(self, seeded, client):
        r = client.get('/api/crm/activities?store_number=901',
                       headers={'X-View': 'owner'})
        assert r.status_code == 403

    def test_xlsx_export_both_views(self, seeded, client):
        r = client.get('/api/export/anu-accounts.xlsx')
        assert r.status_code == 200 and r.data[:2] == b'PK'
        r2 = client.get('/api/export/anu-accounts.xlsx?view=owner')
        assert r2.status_code == 200 and r2.data[:2] == b'PK'


class TestSelfHeal:
    def test_backfill_recovers_missed_claim(self, seeded, client, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            # Simulate a hook miss: raw activity insert, no claim row.
            db.execute(
                "INSERT OR IGNORE INTO stores (store_number, account, city) "
                "VALUES (?,?,?)", (904, 'Test Missed Hook', 'Markham'))
            db.commit()
            sid = db.execute(
                "SELECT id FROM stores WHERE store_number=904").fetchone()[0]
            rid = db.execute("SELECT id FROM reps LIMIT 1").fetchone()[0]
            db.execute(
                "INSERT INTO activities (store_id, rep_id, activity_type) "
                "VALUES (?,?,?)", (sid, rid, 'call'))
            db.execute("DELETE FROM anu_accounts WHERE store_number=904")
            db.commit()
            # New process boot re-backfills:
            app_module._ANU_ACCOUNTS_READY = False
        body = _accounts(client)
        row = next(r for r in body['rows'] if r['store_number'] == 904)
        assert row['account_ref'] == 'ANU-904'
        assert row['first_touch_type'] == 'call'
