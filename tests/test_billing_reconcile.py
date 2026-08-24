"""No listing goes unpaid: the reconciliation must catch a billable listing
that the anu-accounts count would miss, and must not cry wolf on legitimate
non-billables."""
import os
import sys
import tempfile

os.environ.pop('DATABASE_URL', None)
_TMP = tempfile.mkdtemp(prefix='dripp_reconcile_')
os.environ['DB_DIR'] = _TMP
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402
import importlib.util  # noqa: E402

PHX = '0014318'


@pytest.fixture(scope='module')
def app_module():
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]
    os.environ['DB_DIR'] = _TMP
    spec = importlib.util.spec_from_file_location(
        'app', os.path.join(os.path.dirname(__file__), '..', 'app.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def _seed(app_module):
    with app_module.app.app_context():
        db = app_module.get_db()
        app_module._ensure_anu_accounts(db) if hasattr(app_module, '_ensure_anu_accounts') else None
        for sn in (4101, 4102, 4103, 4104):
            db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                       "VALUES (?,?, 'Toronto')", (sn, f'LCBO #{sn}'))
            db.execute("INSERT OR IGNORE INTO reps (name) VALUES ('Namit')")
        rid = db.execute("SELECT id FROM reps WHERE name='Namit'").fetchone()[0]

        def touch(sn, when):
            sid = db.execute("SELECT id FROM stores WHERE store_number=?", (sn,)).fetchone()[0]
            db.execute("INSERT INTO activities (store_id, rep_id, activity_type, "
                       "rep, created_at) VALUES (?,?,?,?,?)",
                       (sid, rid, 'store_visit', 'Namit', when + ' 09:00:00'))

        def listing(sn, date):
            db.execute("INSERT INTO listing_ledger (sku, store_number, event, "
                       "observed_date, source, source_detail) VALUES "
                       "(?,?, 'LISTED', ?, 'sod', 't')", (PHX, sn, date))

        def claim(sn, when):
            db.execute("INSERT OR IGNORE INTO anu_accounts (store_number, "
                       "account_ref, claimed_at, first_touch_type) VALUES "
                       "(?,?,?, 'store_visit')", (sn, f'ANU-{sn}', when))

        # 4101: touched before listing AND claimed -> billed, fine
        touch(4101, '2026-07-20'); listing(4101, '2026-07-25'); claim(4101, '2026-07-20 09:00:00')
        # 4102: touched before listing but NOT claimed -> THE LEAK
        touch(4102, '2026-07-20'); listing(4102, '2026-07-25')
        # 4103: listed with no touch (organic), not claimed -> review, not leak
        listing(4103, '2026-07-25')
        # 4104: baseline (listed on/before launch) -> not billable, fine
        listing(4104, '2026-07-10')
        db.commit()


class TestReconcileCatchesTheLeak:
    def test_a_billable_listing_at_an_unclaimed_store_is_flagged(self, app_module, client):
        _seed(app_module)
        r = client.get('/api/billing/reconcile').get_json()
        assert r['reconciled'] is False, 'a leak must fail reconciliation'
        leak_stores = {l['store_number'] for l in r['leaks']}
        assert 4102 in leak_stores, 'the touched-but-unclaimed listing must leak'
        assert 4101 not in leak_stores, 'a claimed billable is not a leak'
        assert 4104 not in leak_stores, 'a pre-launch baseline is not a leak'

    def test_organic_is_review_not_leak(self, app_module, client):
        r = client.get('/api/billing/reconcile').get_json()
        review = {x['store_number'] for x in r['review']}
        leaks = {x['store_number'] for x in r['leaks']}
        assert 4103 in review and 4103 not in leaks

    def test_every_listing_has_a_disposition(self, app_module, client):
        r = client.get('/api/billing/reconcile').get_json()
        b = r['buckets']
        accounted = (b['billed'] + b['billed_override'] + b['baseline']
                     + b['review_organic'] + b['leak_billable_unclaimed'])
        assert accounted == r['total_listings'], 'no listing may be dropped'

    def test_integrity_fails_when_a_listing_leaks(self, app_module, client):
        j = client.get('/api/admin/integrity').get_json()
        assert j['checks']['billing_reconciled'].startswith('FAIL')
        assert j['all_clear'] is False
