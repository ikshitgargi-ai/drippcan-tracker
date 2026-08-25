"""No listing goes unpaid.

Rule (CEO): a store is billable when a rep TOUCHED it (any activity type puts
it in anu_accounts) AND our product has inventory there now (SOD or lcbo.com),
for a post-launch listing. The reconcile proves every such listing is counted,
and flags any that qualifies but is not being billed.
"""
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


def _fresh(app_module):
    with app_module.app.app_context():
        db = app_module.get_db()
        for t in ('listing_ledger', 'anu_accounts', 'activities',
                  'sod_inventory', 'sod_store_sku_changes', 'lcbo_live_snapshots'):
            try:
                db.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        db.commit()


def _store(app_module, sn, city='Toronto'):
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                   "VALUES (?,?,?)", (sn, f'LCBO #{sn}', city))
        db.commit()


def _touch(app_module, sn, atype='store_visit', when='2026-07-18'):
    """Any activity type is a touch and claims the store into anu_accounts."""
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("INSERT OR IGNORE INTO reps (name) VALUES ('Namit')")
        rid = db.execute("SELECT id FROM reps WHERE name='Namit'").fetchone()[0]
        sid = db.execute("SELECT id FROM stores WHERE store_number=?", (sn,)).fetchone()[0]
        db.execute("INSERT INTO activities (store_id, rep_id, activity_type, rep, "
                   "created_at, visit_date) VALUES (?,?,?,?, ?, ?)",
                   (sid, rid, atype, 'Namit', when + ' 14:00:00', when))
        # a touch claims the store into anu_accounts (any activity type does)
        app_module._ANU_ACCOUNTS_READY = False
        app_module._ensure_anu_accounts(db)
        db.execute("INSERT OR IGNORE INTO anu_accounts (store_number, account_ref, "
                   "claimed_at, first_touch_type) VALUES (?,?,?,?)",
                   (sn, f'ANU-{sn}', when + ' 14:00:00', atype))
        db.commit()


def _listing(app_module, sn, date='2026-07-25', source='sod'):
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("INSERT INTO listing_ledger (sku, store_number, event, "
                   "observed_date, source, source_detail) VALUES "
                   "(?,?, 'LISTED', ?, ?, 't')", (PHX, sn, date, source))
        db.commit()


def _stock(app_module, sn, date='2026-08-01'):
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("INSERT INTO sod_inventory (sku, store_number, snapshot_date, "
                   "status, on_hand, product_name) VALUES (?,?,?, 'L', 6, 'x')",
                   (PHX, sn, date))
        db.commit()


class TestTheRule:
    def test_touched_stocked_postlaunch_is_billed(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6001)
        _touch(app_module, 6001, 'call')      # a call is a touch
        _listing(app_module, 6001, '2026-07-25')
        _stock(app_module, 6001)
        r = client.get('/api/billing/reconcile').get_json()
        assert r['buckets']['billed'] >= 1
        assert 6001 not in {l['store_number'] for l in r['leaks']}

    def test_no_inventory_is_not_billed_and_not_a_leak(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6002)
        _touch(app_module, 6002)
        _listing(app_module, 6002, '2026-07-25')   # listed, but never stocked
        r = client.get('/api/billing/reconcile').get_json()
        assert r['buckets']['no_inventory'] >= 1
        assert 6002 not in {l['store_number'] for l in r['leaks']}
        assert r['buckets']['billed'] == 0

    def test_prelaunch_is_baseline(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6003)
        _touch(app_module, 6003)
        _listing(app_module, 6003, '2026-07-10')   # on/before launch
        _stock(app_module, 6003)
        r = client.get('/api/billing/reconcile').get_json()
        assert r['buckets']['baseline'] >= 1
        assert r['buckets']['billed'] == 0

    def test_stocked_but_untouched_is_review_not_billed(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6004)
        _listing(app_module, 6004, '2026-07-25')   # listed + stocked, no touch
        _stock(app_module, 6004)
        r = client.get('/api/billing/reconcile').get_json()
        assert 6004 in {x['store_number'] for x in r['review']}
        assert r['buckets']['billed'] == 0

    def test_lcbo_com_inventory_alone_qualifies(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6005)
        _touch(app_module, 6005)
        _listing(app_module, 6005, '2026-07-25')
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT INTO lcbo_live_snapshots (sku, store_number, qty, "
                       "batch_id) VALUES (?,6005,4,'b1')", (PHX,))
            db.commit()
        r = client.get('/api/billing/reconcile').get_json()
        assert r['buckets']['billed'] >= 1, 'lcbo.com stock alone must qualify'


class TestLeaksCaught:
    def test_rep_saw_on_shelf_stocked_unclaimed_is_a_leak(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6101)
        # a rep tapped "saw on shelf": ledger LISTED source='rep', no activity,
        # store never claimed; product is on the shelf now.
        _listing(app_module, 6101, '2026-07-25', source='rep')
        _stock(app_module, 6101)
        r = client.get('/api/billing/reconcile').get_json()
        assert 6101 in {l['store_number'] for l in r['leaks']}
        assert r['reconciled'] is False

    def test_override_on_unclaimed_store_is_a_leak(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6102)
        _listing(app_module, 6102, '2026-07-25')
        _stock(app_module, 6102)
        with app_module.app.app_context():
            db = app_module.get_db()
            try:
                db.execute("INSERT INTO billing_overrides (store_number, sku, "
                           "action, reason) VALUES (6102, ?, 'mark_billable', 'x')", (PHX,))
            except Exception:
                db.execute("INSERT INTO billing_overrides (store_number, sku, "
                           "action, reason, created_at) VALUES (6102, ?, "
                           "'mark_billable', 'x', '2026-07-25')", (PHX,))
            db.commit()
        r = client.get('/api/billing/reconcile').get_json()
        assert 6102 in {l['store_number'] for l in r['leaks']}
        assert r['reconciled'] is False

    def test_change_log_signal_without_ledger_is_a_gap(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6103)
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT INTO sod_store_sku_changes (sku, store_number, "
                       "change_date, old_status, new_status, change_type) VALUES "
                       "(?,6103,'2026-07-25',NULL,'L','NEW_LISTING')", (PHX,))
            db.commit()
        r = client.get('/api/billing/reconcile').get_json()
        assert 6103 in {g['store_number'] for g in r['ledger_gaps']}
        assert r['reconciled'] is False

    def test_integrity_fails_on_a_leak(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6104)
        _listing(app_module, 6104, '2026-07-25', source='rep')
        _stock(app_module, 6104)
        j = client.get('/api/admin/integrity').get_json()
        assert j['checks']['billing_reconciled'].startswith('FAIL')
        assert j['all_clear'] is False


class TestClean:
    def test_a_clean_book_reconciles(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6201)
        _touch(app_module, 6201)
        _listing(app_module, 6201, '2026-07-25')
        _stock(app_module, 6201)
        r = client.get('/api/billing/reconcile').get_json()
        assert r['reconciled'] is True
        assert len(r['leaks']) == 0 and len(r['ledger_gaps']) == 0

    def test_evening_touch_dated_in_toronto(self, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("DELETE FROM activities")
            db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                       "VALUES (6202,'LCBO #6202','Toronto')")
            db.execute("INSERT OR IGNORE INTO reps (name) VALUES ('Namit')")
            rid = db.execute("SELECT id FROM reps WHERE name='Namit'").fetchone()[0]
            sid = db.execute("SELECT id FROM stores WHERE store_number=6202").fetchone()[0]
            db.execute("INSERT INTO activities (store_id, rep_id, activity_type, "
                       "created_at) VALUES (?,?,?, '2026-07-20 01:00:00')",
                       (sid, rid, 'store_visit'))
            db.commit()
            tf = app_module._first_touchpoints()
        assert tf.get(6202) == '2026-07-19', f"got {tf.get(6202)}"


class TestRelistBillsAutomatically:
    """Standing rule (CEO): a rep-driven post-launch re-listing of a lapsed
    store bills automatically, no override needed (the #390 case)."""

    def test_pre_launch_delist_relist_after_launch_bills(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6301)
        _touch(app_module, 6301, 'store_visit', when='2026-07-20')
        # listed pre-launch, delisted, then rep-driven relist after launch
        _listing(app_module, 6301, '2026-06-20')            # pre-launch
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT INTO listing_ledger (sku, store_number, event, "
                       "observed_date, source, source_detail) VALUES "
                       "(?,6301,'DELISTED','2026-06-25','sod','t')", (PHX,))
            db.commit()
        _listing(app_module, 6301, '2026-07-24')            # relist, ~30-day gap
        _stock(app_module, 6301)
        r = client.get('/api/billing/reconcile').get_json()
        assert r['buckets']['billed'] >= 1, 'a rep-driven post-launch relist must bill'
        assert 6301 not in {l['store_number'] for l in r['leaks']}
        assert r['reconciled'] is True

    def test_short_flicker_is_not_a_relist(self, app_module, client):
        # A 1-day SOD status blip (L->D->L) is not a re-won placement: the
        # store stays baseline and does not bill even if touched and stocked.
        _fresh(app_module)
        _store(app_module, 6303)
        _touch(app_module, 6303)
        _listing(app_module, 6303, '2026-07-05')            # pre-launch
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT INTO listing_ledger (sku, store_number, event, "
                       "observed_date, source, source_detail) VALUES "
                       "(?,6303,'DELISTED','2026-07-23','sod','t')", (PHX,))
            db.commit()
        _listing(app_module, 6303, '2026-07-24')            # 1-day blip, not a relist
        _stock(app_module, 6303)
        r = client.get('/api/billing/reconcile').get_json()
        assert r['buckets']['baseline'] >= 1
        assert 6303 not in {x['store_number'] for x in r.get('review', [])}

    def test_pure_baseline_still_does_not_bill(self, app_module, client):
        _fresh(app_module)
        _store(app_module, 6302)
        _touch(app_module, 6302)
        _listing(app_module, 6302, '2026-07-05')            # pre-launch, never relisted
        _stock(app_module, 6302)
        r = client.get('/api/billing/reconcile').get_json()
        assert r['buckets']['baseline'] >= 1
        assert r['buckets']['billed'] == 0
