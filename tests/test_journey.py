"""STOCK-AWARE JOURNEY — touch -> listed -> stock landed.

The founder's refinement: what matters for attribution is whether the shelf
was EMPTY when we touched it. A store that was dry at the touch and holds
bottles now is a conversion we caused. A store that already had stock never
was.

The load-bearing promise here is restraint: this view must NOT change the
agreed invoice number. It reports candidates alongside it so the definition
can be agreed with the client openly rather than moved silently.

Run: python3 -m pytest tests/test_journey.py -v
"""
import os
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='dripp_journey_test_')
os.environ['DB_DIR'] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

PHOENIX = '0014318'
TOUCH = '2026-07-20'
LATEST = '2026-07-22'


@pytest.fixture(scope='module')
def app_module():
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]
    os.environ['DB_DIR'] = _TMP
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'app', os.path.join(os.path.dirname(__file__), '..', 'app.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


@pytest.fixture(scope='module')
def seeded(app_module):
    """Four stores, one per journey stage.
      #390 listed but DRY at touch, still dry  -> listed_no_stock (the real one)
      #401 dry at touch, bottles now           -> converted
      #402 already had bottles at touch        -> already_stocked, never ours
      #403 no SOD row at touch, bottles now    -> converted
    """
    with app_module.app.app_context():
        db = app_module.get_db()
        app_module._ensure_anu_accounts(db)
        for sn, city in ((390, 'Markham'), (401, 'Toronto'),
                         (402, 'Toronto'), (403, 'Brampton')):
            db.execute("INSERT OR IGNORE INTO stores (store_number, account, "
                       "city) VALUES (?,?,?)", (sn, f'LCBO #{sn}', city))
            db.execute("INSERT OR IGNORE INTO anu_accounts (store_number, "
                       "account_ref, claimed_at, first_touch_type) "
                       "VALUES (?,?,?,'tasting')",
                       (sn, f'LCBO #{sn}', TOUCH))
        # SOD history
        def sod(sn, date, status, oh):
            db.execute("INSERT INTO sod_inventory (sku, store_number, "
                       "snapshot_date, status, on_hand, product_name) "
                       "VALUES (?,?,?,?,?,'Phoenix')",
                       (PHOENIX, sn, date, status, oh))
        # #390: listed, zero, stays zero
        sod(390, '2026-07-13', 'L', 0)
        sod(390, TOUCH, 'L', 0)
        sod(390, LATEST, 'L', 0)
        # #401: dry at touch, stock lands after
        sod(401, TOUCH, 'L', 0)
        sod(401, LATEST, 'L', 24)
        # #402: already stocked when we arrived
        sod(402, TOUCH, 'L', 30)
        sod(402, LATEST, 'L', 18)
        # #403: no row at touch at all, stock now
        sod(403, LATEST, 'L', 12)
        db.commit()
    return True


class TestJourneyStages:
    def test_each_store_lands_in_the_right_stage(self, seeded, client):
        rows = client.get('/api/anu-accounts/journey').get_json()['rows']
        stage = {r['store_number']: r['stage']
                 for r in rows if r['sku'] == PHOENIX}
        assert stage[390] == 'listed_no_stock'   # authorised, never a bottle
        assert stage[401] == 'converted'         # we caused this one
        assert stage[402] == 'already_stocked'   # never ours
        assert stage[403] == 'converted'         # absent then, stocked now

    def test_already_stocked_is_never_a_candidate(self, seeded, client):
        body = client.get('/api/anu-accounts/journey').get_json()
        rows = [r for r in body['rows']
                if r['store_number'] == 402 and r['sku'] == PHOENIX]
        assert rows and rows[0]['stage'] == 'already_stocked'
        assert rows[0]['bottles_landed'] == 0

    def test_bottles_landed_is_counted_for_conversions(self, seeded, client):
        rows = client.get('/api/anu-accounts/journey').get_json()['rows']
        r = next(x for x in rows
                 if x['store_number'] == 401 and x['sku'] == PHOENIX)
        assert r['stock_at_touch'] == 0 and r['stock_now'] == 24
        assert r['bottles_landed'] == 24

    def test_store_390_real_case_is_watchlist_not_billable(self, seeded,
                                                           client):
        # The store that prompted this: listed 07-13 with zero bottles,
        # tasted 07-20, still zero. It is a watchlist item, not a conversion.
        rows = client.get('/api/anu-accounts/journey').get_json()['rows']
        r = next(x for x in rows
                 if x['store_number'] == 390 and x['sku'] == PHOENIX)
        assert r['listed_at_touch'] is True
        assert r['stock_at_touch'] == 0 and r['stock_now'] == 0
        assert r['stage'] == 'listed_no_stock'
        assert r['bottles_landed'] == 0


class TestBillingRestraint:
    """The important tests. This view must not move the invoice."""

    def test_journey_does_not_change_the_agreed_billable_number(
            self, seeded, client):
        before = client.get('/api/anu-accounts').get_json()['summary']
        client.get('/api/anu-accounts/journey')
        after = client.get('/api/anu-accounts').get_json()['summary']
        assert before == after, 'the journey view moved the invoice number'

    def test_candidates_are_labelled_as_candidates_not_billables(
            self, seeded, client):
        body = client.get('/api/anu-accounts/journey').get_json()
        views = body['billing_views']
        assert 'stock_aware_candidates' in views
        assert 'billable' not in str(views.get('stock_aware_candidates'))
        assert 'CANDIDATE' in body['note'] or 'candidate' in body['note']
        # and it must say out loud that the client has to agree the change
        assert 'client' in body['note'].lower()

    def test_summary_counts_match_the_rows(self, seeded, client):
        body = client.get('/api/anu-accounts/journey').get_json()
        from collections import Counter
        actual = Counter(r['stage'] for r in body['rows'])
        for stage, n in body['summary'].items():
            assert actual.get(stage, 0) == n, f'{stage} count disagrees'
