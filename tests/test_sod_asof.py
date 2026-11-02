"""The LCBO daily file is a latest-position file with mixed dates per store.
A late-reporting store must never read as dropped or delisted, a missed or
partial day must never invent listings, and the false events the old logic
wrote must be voided (never deleted) and leave billing and the gap report."""
import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import zipfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
import sod_asof as sa  # noqa: E402

SKU = '0014318'
NAME = 'PHOENIX VODKA DDP'


def _line(d, sku, store, status, qty, name=NAME):
    """One 47-char fixed-width SOD row: date(8) sku(7) name(17) store(4) status(1) sign(1) qty(9)."""
    return f"{d.replace('-', '')}{sku}{name:<17.17}{store:04d}{status} {qty:09d}"


def _zip(lines, member='alldlyinventoryTST.dat'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, '\n'.join(lines) + '\n')
    return buf.getvalue()


class FakeClient:
    def __init__(self, zip_bytes, name, date):
        self._z, self._n, self._d = zip_bytes, name, date

    def download_zip_bytes(self, source, filename=None):
        return self._z, self._n, self._d


# ----------------------------------------------------------------- units --
class TestPosition:
    def test_newest_row_per_store_wins_and_carries_its_own_date(self):
        rows = [
            {'snapshot_date': '2026-11-01', 'sku': SKU, 'store_number': 101, 'status': 'L', 'on_hand': 4, 'product_name': NAME},
            {'snapshot_date': '2026-10-30', 'sku': SKU, 'store_number': 102, 'status': 'L', 'on_hand': 6, 'product_name': NAME},
            {'snapshot_date': '2026-10-29', 'sku': SKU, 'store_number': 102, 'status': 'L', 'on_hand': 9, 'product_name': NAME},
        ]
        latest, stats = sa.build_latest_position(rows, '2026-11-01')
        by = {r['store_number']: r for r in latest}
        assert set(by) == {101, 102}
        assert by[101]['as_of_date'] == '2026-11-01' and by[101]['snapshot_date'] == '2026-11-01'
        assert by[102]['as_of_date'] == '2026-10-30' and by[102]['snapshot_date'] == '2026-11-01'
        assert by[102]['on_hand'] == 6, 'the newest row for the store, not the oldest'
        assert stats['stale_pairs'] == 1 and stats['stale_stores'] == [102]
        assert stats['oldest_as_of'] == '2026-10-30'

    def test_aggregates_and_freshness(self):
        rows = [
            {'snapshot_date': '2026-11-01', 'sku': SKU, 'store_number': 1, 'status': 'L', 'on_hand': 4, 'product_name': NAME},
            {'snapshot_date': '2026-11-01', 'sku': SKU, 'store_number': 2, 'status': 'D', 'on_hand': 1, 'product_name': NAME},
        ]
        agg = sa.per_sku_aggregates(rows)[SKU]
        assert agg['store_count'] == 2 and agg['total_on_hand'] == 5
        assert agg['status_counts'] == {'L': 1, 'D': 1}
        assert sa.network_freshness({'2026-11-01': 847356, '2026-10-30': 547271, '2026-10-31': 245731}, '2026-11-01') == 0.5166
        assert sa.network_freshness({}, '2026-11-01') is None


# -------------------------------------------------------------- end to end --
@pytest.fixture(scope='module')
def app_module():
    # own database for this module; the previous DB_DIR is restored afterwards
    # so later test modules keep importing against their own databases
    prev = os.environ.get('DB_DIR')
    tmp = tempfile.mkdtemp(prefix='asof_')
    os.environ.pop('DATABASE_URL', None)
    os.environ['DB_DIR'] = tmp
    os.environ['SALVAGE_RESTORE_DISABLED'] = '1'
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]
    spec = importlib.util.spec_from_file_location('app', os.path.join(ROOT, 'app.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    if prev is None:
        os.environ.pop('DB_DIR', None)
    else:
        os.environ['DB_DIR'] = prev
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]


def _q(mod, sql, params=()):
    c = sqlite3.connect(mod.DB_PATH)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


D1, D2, D3 = '2026-11-10', '2026-11-11', '2026-11-12'
FILLER_SKU = '0000067'


def _filler(d, n):
    # untracked rows so the file has a realistic fresh share
    return [_line(d, FILLER_SKU, 500 + i, 'L', 10, 'SMIRNOFF VODKA') for i in range(n)]


class TestIngestKeepsLateStores:
    def test_day_one_establishes_three_stores(self, app_module):
        z = _zip([_line(D1, SKU, 101, 'L', 5), _line(D1, SKU, 102, 'L', 6), _line(D1, SKU, 103, 'L', 7)] + _filler(D1, 20))
        res = app_module.run_sod_sync('daily_a', client=FakeClient(z, 'alldlyinventoryTUE.zip', D1))
        assert res['status'] == 'success', res
        rows = _q(app_module, "SELECT store_number, status, as_of_date FROM sod_inventory WHERE sku=? AND snapshot_date=?", (SKU, D1))
        assert sorted(r[0] for r in rows) == [101, 102, 103]
        assert all(r[2] == D1 for r in rows)

    def test_day_two_late_store_is_carried_forward_not_dropped(self, app_module):
        # 101 reports on D2; 102 has not reported since D1 (its row still carries D1);
        # 103 is gone from the file entirely (a real drop).
        z = _zip([_line(D2, SKU, 101, 'L', 4), _line(D1, SKU, 102, 'L', 6)] + _filler(D2, 20))
        res = app_module.run_sod_sync('daily_a', client=FakeClient(z, 'alldlyinventoryWED.zip', D2))
        assert res['status'] == 'success', res
        pos = {r[0]: (r[1], r[2], r[3]) for r in _q(app_module,
               "SELECT store_number, status, on_hand, as_of_date FROM sod_inventory WHERE sku=? AND snapshot_date=?", (SKU, D2))}
        assert set(pos) == {101, 102}, 'the day position holds the late store too'
        assert pos[101] == ('L', 4, D2)
        assert pos[102] == ('L', 6, D1), 'carried forward with its own as-of date'
        changes = _q(app_module, "SELECT store_number, change_type, change_date FROM sod_store_sku_changes WHERE sku=? AND change_date=?", (SKU, D2))
        assert (103, 'DROPPED', D2) in changes, 'the store that left the file is a real drop'
        assert not any(c[0] == 102 for c in changes), 'the late store is not a change at all'
        delisted = _q(app_module, "SELECT store_number FROM listing_ledger WHERE sku=? AND event='DELISTED' AND source='sod' AND voided_at IS NULL", (SKU,))
        assert [r[0] for r in delisted] == [103]
        # no fabricated reconfirmation of 102 on a day LCBO did not hear from it
        assert _q(app_module, "SELECT COUNT(*) FROM listing_ledger WHERE sku=? AND store_number=102 AND event='RECONFIRMED' AND observed_date=?", (SKU, D2))[0][0] == 0
        run = _q(app_module, "SELECT fresh_share, stale_stores FROM sod_sync_runs WHERE source='daily_a' AND snapshot_date=? ORDER BY id DESC LIMIT 1", (D2,))[0]
        assert run[1] == 1 and run[0] is not None and 0.9 < run[0] < 1.0

    def test_prior_position_reads_the_baseline_as_of(self, app_module):
        c = sqlite3.connect(app_module.DB_PATH)
        try:
            prior = sa.prior_position(c.cursor(), False, SKU, D3)
        finally:
            c.close()
        assert prior == {101: 'L', 102: 'L'}, 'before D3 the newest word per store is the D2 position (103 is gone)'


class TestVoidingWhatAbsenceInvented:
    """Replay the 31 Oct fault: a DELISTED and a DROPPED written for a store that
    had merely not reported, a LISTED written when it 'came back', and a
    reactivation detected from that pair. One as-of ingest voids all of it."""

    def test_false_events_are_voided_and_billing_readers_skip_them(self, app_module):
        c = sqlite3.connect(app_module.DB_PATH)
        cur = c.cursor()
        if hasattr(app_module, '_ensure_reactivations'):
            app_module._ensure_reactivations(c)
        sa.ensure_columns(c, False)
        # old logic's artefacts for store 102 (which never left)
        cur.execute("INSERT INTO listing_ledger (sku, store_number, event, source, source_detail, observed_date) VALUES (?,?,?,?,?,?)",
                    (SKU, 102, 'DELISTED', 'sod', D3, D3))
        cur.execute("INSERT INTO sod_store_sku_changes (sku, store_number, change_date, old_status, new_status, change_type) VALUES (?,?,?,?,?,?)",
                    (SKU, 102, D3, 'L', None, 'DROPPED'))
        cur.execute("INSERT INTO listing_ledger (sku, store_number, event, source, source_detail, observed_date) VALUES (?,?,?,?,?,?)",
                    (SKU, 102, 'LISTED', 'sod', '2026-11-13', '2026-11-13'))
        if hasattr(app_module, '_ensure_reactivations'):
            cur.execute("INSERT INTO mandakini_reactivations (store_number, sku, reactivated_on, detected_via) VALUES (?,?,?,?)",
                        (102, SKU, '2026-11-13', 'delist_relist'))
        c.commit(); c.close()
        # D3 file: 102 reports again, still L (nothing ever happened to it); 101 fresh
        z = _zip([_line(D3, SKU, 101, 'L', 3), _line(D3, SKU, 102, 'L', 6)] + _filler(D3, 20))
        res = app_module.run_sod_sync('daily_a', client=FakeClient(z, 'alldlyinventoryTHU.zip', D3))
        assert res['status'] == 'success', res
        led = _q(app_module, "SELECT event, observed_date, voided_at IS NOT NULL, void_reason FROM listing_ledger WHERE sku=? AND store_number=102 AND source='sod' AND event IN ('DELISTED','LISTED') AND observed_date >= ? ORDER BY observed_date", (SKU, D3))
        assert len(led) == 2 and all(v for _, _, v, _ in led), f'both artefacts voided, nothing deleted: {led}'
        assert all('feed gap' in (r or '') for _, _, _, r in led)
        # the genuine day-one listing is untouched
        assert _q(app_module, "SELECT voided_at IS NULL FROM listing_ledger WHERE sku=? AND store_number=102 AND event='LISTED' AND observed_date=?", (SKU, D1))[0][0]
        assert _q(app_module, "SELECT COUNT(*) FROM listing_ledger WHERE sku=? AND store_number=102", (SKU,))[0][0] >= 3, 'rows stay on the ledger'
        assert _q(app_module, "SELECT voided_at IS NOT NULL FROM sod_store_sku_changes WHERE sku=? AND store_number=102 AND change_type='DROPPED'", (SKU,))[0][0]
        if hasattr(app_module, '_ensure_reactivations'):
            assert _q(app_module, "SELECT voided_at IS NOT NULL FROM mandakini_reactivations WHERE store_number=102 AND sku=?", (SKU,))[0][0], 'the recovery that never happened is voided'
        # derived cache and billing readers see the store as listed, with no delist
        sl = _q(app_module, "SELECT status FROM store_listings WHERE sku=? AND store_number=102", (SKU,))
        assert sl and sl[0][0].lower().startswith('list'), sl
        if hasattr(app_module, '_reactivation_map'):
            with app_module.app.app_context():
                db = app_module.get_db()
                assert (102, SKU) not in app_module._reactivation_map(db)
        # the real drop of 103 is untouched
        assert _q(app_module, "SELECT voided_at IS NULL FROM listing_ledger WHERE sku=? AND store_number=103 AND event='DELISTED'", (SKU,))[0][0]

    def test_a_genuine_status_flip_is_never_voided(self, app_module):
        # D4: store 101 reports D (delisting) on the day itself; that delist must stand
        D4 = '2026-11-13'
        z = _zip([_line(D4, SKU, 101, 'D', 2), _line(D4, SKU, 102, 'L', 6)] + _filler(D4, 20))
        res = app_module.run_sod_sync('daily_a', client=FakeClient(z, 'alldlyinventoryFRI.zip', D4))
        assert res['status'] == 'success', res
        row = _q(app_module, "SELECT voided_at IS NULL FROM listing_ledger WHERE sku=? AND store_number=101 AND event='DELISTED' AND observed_date=?", (SKU, D4))
        assert row and row[0][0], 'a delist backed by a D row on its date is genuine and stays'


class TestPartialDayTransition:
    """The live case: the current day was ingested by the old logic (only the
    stores that reported), so the position is half the network. Re-ingesting
    the same file with as-of semantics must complete it WITHOUT inventing
    NEW_LISTING events for the stores that were merely late."""

    def test_reingest_completes_the_day_without_new_listings(self, app_module):
        D5, D6 = '2026-11-14', '2026-11-15'
        # full day D5
        z5 = _zip([_line(D5, SKU, 201, 'L', 5), _line(D5, SKU, 202, 'L', 6), _line(D5, SKU, 203, 'L', 7)] + _filler(D5, 20))
        assert app_module.run_sod_sync('daily_a', client=FakeClient(z5, 'alldlyinventorySAT.zip', D5))['status'] == 'success'
        # simulate the OLD logic's partial D6: only 201 written, no as_of_date
        c = sqlite3.connect(app_module.DB_PATH)
        c.execute("INSERT INTO sod_inventory (sku, store_number, snapshot_date, status, on_hand, product_name, source) VALUES (?,?,?,?,?,?,?)",
                  (SKU, 201, D6, 'L', 4, NAME, 'daily_a'))
        c.commit(); c.close()
        # the D6 file as LCBO actually publishes it: 201 fresh, 202 and 203 carried on D5
        z6 = _zip([_line(D6, SKU, 201, 'L', 4), _line(D5, SKU, 202, 'L', 6), _line(D5, SKU, 203, 'L', 7)] + _filler(D6, 20))
        res = app_module.run_sod_sync('daily_a', client=FakeClient(z6, 'alldlyinventorySUN.zip', D6))
        assert res['status'] == 'success', res
        pos = sorted(r[0] for r in _q(app_module, "SELECT store_number FROM sod_inventory WHERE sku=? AND snapshot_date=?", (SKU, D6)))
        assert pos == [201, 202, 203], 'the day is complete after the re-ingest'
        new = _q(app_module, "SELECT store_number FROM sod_store_sku_changes WHERE sku=? AND change_date>=? AND change_type IN ('NEW_LISTING','RELISTED') AND voided_at IS NULL", (SKU, D6))
        assert [r[0] for r in new if r[0] in (202, 203)] == [], f'late stores must not read as new listings: {new}'
        assert _q(app_module, "SELECT COUNT(*) FROM listing_ledger WHERE sku=? AND store_number IN (202,203) AND event='LISTED' AND observed_date>=? AND voided_at IS NULL", (SKU, D6))[0][0] == 0


class TestGapReport:
    def test_gap_report_groups_by_territory_and_reads_the_position(self, app_module):
        c = app_module.app.test_client()
        r = c.get(f'/api/crm/gap-report?sku={SKU}')
        assert r.status_code == 200, r.data[:300]
        body = r.get_json()
        assert body['skus'] and body['skus'][0]['sku'] == SKU
        rep = body['skus'][0]
        assert rep['snapshot_date'] is not None
        assert isinstance(rep['territories'], list) and rep['territories']
        t0 = rep['territories'][0]
        for k in ('name', 'stores', 'carrying', 'listed_no_stock', 'delisting', 'gap_touched', 'gap_untouched', 'coverage_pct'):
            assert k in t0, k
        buckets = {row['bucket'] for row in rep['rows']}
        assert buckets <= {'gap_untouched', 'gap_touched', 'listed_no_stock', 'delisting', 'live_only'}
        # every actionable row carries an action sentence and the as-of fields
        assert all(row['action'] for row in rep['rows'])
        assert all('as_of_date' in row and 'stale_days' in row and 'live_qty' in row for row in rep['rows'])
        # warehouse rows are never counted as store gaps
        assert all(row['store_number'] not in sa.WAREHOUSE_LOCATIONS for row in rep['rows'])
        assert 'warehouse' in rep and 'warehouse_units' in rep
        # include=all also returns carrying rows
        full = c.get(f'/api/crm/gap-report?sku={SKU}&include=all').get_json()['skus'][0]
        assert any(row['bucket'] == 'carrying' for row in full['rows'])
        assert full['carrying_stores'] >= 1
