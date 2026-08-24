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


class TestAuditGapsClosed:
    """The three unpaid-listing paths an adversarial audit found."""

    def _fresh(self, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("DELETE FROM listing_ledger")
            db.execute("DELETE FROM anu_accounts")
            db.execute("DELETE FROM activities")
            db.commit()

    def test_same_day_touch_at_unclaimed_store_is_a_leak(self, app_module, client):
        self._fresh(app_module)
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                       "VALUES (5201,'LCBO #5201','Toronto')")
            db.execute("INSERT OR IGNORE INTO reps (name) VALUES ('Namit')")
            rid = db.execute("SELECT id FROM reps WHERE name='Namit'").fetchone()[0]
            sid = db.execute("SELECT id FROM stores WHERE store_number=5201").fetchone()[0]
            # touch and listing on the SAME day, store not claimed
            db.execute("INSERT INTO activities (store_id, rep_id, activity_type, rep, "
                       "created_at) VALUES (?,?,?,?, '2026-07-25 09:00:00')",
                       (sid, rid, 'store_visit', 'Namit'))
            db.execute("INSERT INTO listing_ledger (sku, store_number, event, "
                       "observed_date, source, source_detail) VALUES "
                       "(?,5201,'LISTED','2026-07-25','sod','t')", (PHX,))
            db.commit()
        r = client.get('/api/billing/reconcile').get_json()
        assert 5201 in {l['store_number'] for l in r['leaks']}, 'same-day touch must bill'
        assert r['reconciled'] is False

    def test_claimed_store_the_invoice_would_drop_is_a_leak(self, app_module, client):
        self._fresh(app_module)
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                       "VALUES (5202,'LCBO #5202','Toronto')")
            db.execute("INSERT OR IGNORE INTO reps (name) VALUES ('Namit')")
            rid = db.execute("SELECT id FROM reps WHERE name='Namit'").fetchone()[0]
            sid = db.execute("SELECT id FROM stores WHERE store_number=5202").fetchone()[0]
            # rep genuinely touched on 07-18, before the 07-20 listing
            db.execute("INSERT INTO activities (store_id, rep_id, activity_type, rep, "
                       "created_at) VALUES (?,?,?,?, '2026-07-18 09:00:00')",
                       (sid, rid, 'store_visit', 'Namit'))
            db.execute("INSERT INTO listing_ledger (sku, store_number, event, "
                       "observed_date, source, source_detail) VALUES "
                       "(?,5202,'LISTED','2026-07-20','sod','t')", (PHX,))
            # but the claim is dated LATER (a future-dated visit), so the invoice
            # classifier returns listed_before_touch and never bills it
            db.execute("INSERT OR IGNORE INTO anu_accounts (store_number, account_ref, "
                       "claimed_at, first_touch_type) VALUES (5202,'ANU-5202',"
                       "'2026-07-30 00:00:00','store_visit')")
            db.commit()
        r = client.get('/api/billing/reconcile').get_json()
        leak = next((l for l in r['leaks'] if l['store_number'] == 5202), None)
        assert leak is not None, 'a claimed store the invoice drops must not read as billed'
        assert r['buckets']['leak_invoice_drops_billable'] >= 1
        assert r['reconciled'] is False

    def test_listed_in_sod_but_missing_from_ledger_is_a_gap(self, app_module, client):
        self._fresh(app_module)
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                       "VALUES (5203,'LCBO #5203','Toronto')")
            # Listed in the freshest SOD after launch, but NO ledger event
            db.execute("INSERT INTO sod_inventory (sku, store_number, snapshot_date, "
                       "status, on_hand, product_name) VALUES "
                       "(?,5203,'2026-08-01','L',10,'x')", (PHX,))
            db.commit()
        r = client.get('/api/billing/reconcile').get_json()
        assert 5203 in {g['store_number'] for g in r['ledger_gaps']}, \
            'a listing in SOD with no ledger event must be surfaced'
        assert r['reconciled'] is False
