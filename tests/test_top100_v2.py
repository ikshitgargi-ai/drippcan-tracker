"""FIX B — Top-100 ranking v2 (2026-07-14 punch list).

What v2 must guarantee:
  - DEFAULT ranks are gap-first: stores carrying NEITHER SKU outrank stores
    carrying one, which outrank stores carrying both. "Carrying" uses the
    FRESHER of the latest SOD row and the latest live lcbo.com row per store
    (a date tie goes to live).
  - Inside each group: CORE geography (Toronto proper + the York-region
    spine) before OUTER (Mississauga, Brampton, everything else), then class
    AAA>AA>A>B>C>'', then tier routed>territory>discovered.
  - POST /api/top100/rebalance recomputes defaults but PRESERVES every
    manual override (any priority_rank audit entry whose changed_by is not
    'seed-default'/'rebalance') — including a manually CLEARED rank.
  - /api/top100 rows gain skus_carried (0/1/2), carried_detail (per-brand
    units + source) and geo_tier.
  - Every priority write re-sequences the whole ranked list to UNIQUE
    sequential ranks 1..N in one transaction (kills the two-rank-4s bug) and
    returns the re-sequenced affected rows.
  - /api/top100/rebalance is INTERNAL: the owner view gets 403.

The fixture is 6 hand-built stores with known SOD + live rows so the exact
expected order is checkable end to end:

  store  city         geo    class  tier        carries            group
  101    Toronto      CORE   B      routed      nothing            1
  102    Mississauga  OUTER  AAA    routed      nothing            1
  103    Toronto      CORE   AAA    routed      Phoenix (SOD 5)    2
  104    Brampton     OUTER  AA     territory   BOTH (SOD+live)    3
  105    Vaughan      CORE   A      territory   SOD said 4, but a  1
                                                FRESHER live row
                                                says 0 -> gap
  106    Scarborough  CORE   ''     discovered  Phoenix (live 3)   2

  Expected default order: 105, 101, 102, 103, 106, 104
  (105 before 101: class A beats B inside CORE; 101 before 102: CORE
  geography beats 102's AAA class — geography outranks class across the
  group; 103 before 106: AAA beats ''.)

Run with: python3 -m pytest tests/test_top100_v2.py -v
"""
import os
import random
import sqlite3
import sys

import pytest

# Force SQLite for tests so we never touch production Postgres.
os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
os.environ.pop('SOD_CRON_TOKEN', None)

TEST_DB_DIR = '/tmp/drippcan_top100v2_test'
os.makedirs(TEST_DB_DIR, exist_ok=True)
TEST_DB = os.path.join(TEST_DB_DIR, 'drippcan.db')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

PHOENIX = '0014318'
DAYAA = '0044451'
APP_PY = os.path.join(os.path.dirname(__file__), '..', 'app.py')

SOD_DATE = '2026-07-13'            # start-of-day snapshot
LIVE_AT = '2026-07-14 09:00:00'    # fresher live batch (next morning)

EXPECTED_DEFAULT_ORDER = [105, 101, 102, 103, 106, 104]

#         sn,  city,          class, tier,         route_day, route_stop
FIXTURE_STORES = [
    (101, 'Toronto',     'B',   'routed',     1, 1),
    (102, 'Mississauga', 'AAA', 'routed',     1, 2),
    (103, 'Toronto',     'AAA', 'routed',     2, 1),
    (104, 'Brampton',    'AA',  'territory',  None, None),
    (105, 'Vaughan',     'A',   'territory',  None, None),
    (106, 'Scarborough', '',    'discovered', None, None),
]


@pytest.fixture(scope='module')
def app_module():
    """Import app.py fresh against an isolated SQLite file (same pattern as
    test_dripp.py — DB_DIR re-asserted because collection order matters)."""
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
    app_module._rate_buckets.clear()
    with app_module.app.test_client() as c:
        yield c


def _db():
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _rank_state():
    """{store_number: priority_rank} for every active territory store."""
    db = _db()
    out = {r['store_number']: r['priority_rank'] for r in db.execute(
        "SELECT store_number, priority_rank FROM territory_stores "
        "WHERE active=1").fetchall()}
    db.close()
    return out


def _assert_unique_sequential(state):
    """The FIX B invariant: assigned ranks are exactly 1..K, no dupes."""
    ranks = sorted(v for v in state.values() if v is not None)
    assert ranks == list(range(1, len(ranks) + 1)), (
        f'ranks must be unique sequential 1..N, got {ranks}')


@pytest.fixture(scope='module')
def fixture_stores(app_module):
    """Insert the 6 designed stores + their known SOD and live rows.

    No territory ingest here — full control over cities/classes/tiers means
    the exact expected rebalance order is assertable."""
    db = _db()
    for sn, city, klass, tier, rd, rs in FIXTURE_STORES:
        db.execute(
            "INSERT INTO territory_stores "
            "(store_number, tier, class, account, address, city, postal, "
            " route_day, route_stop, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sn, tier, klass, f'LCBO #{sn}', f'{sn} Test Rd', city, '',
             rd, rs, 'seed'))
    # SOD start-of-day rows (2026-07-13)
    sod_rows = [
        (PHOENIX, 103, 5, 'PHOENIX ULTRA SMOOTH VODKA'),   # 103 carries one
        (PHOENIX, 104, 6, 'PHOENIX ULTRA SMOOTH VODKA'),   # 104 carries both
        (DAYAA,   104, 4, 'DAYAA ARAK'),
        (PHOENIX, 105, 4, 'PHOENIX ULTRA SMOOTH VODKA'),   # superseded by live 0
    ]
    for sku, sn, on_hand, name in sod_rows:
        db.execute(
            "INSERT INTO sod_inventory "
            "(sku, store_number, snapshot_date, status, on_hand, product_name) "
            "VALUES (?,?,?,?,?,?)", (sku, sn, SOD_DATE, 'L', on_hand, name))
    # Fresher live lcbo.com batch (2026-07-14 09:00)
    live_rows = [
        (PHOENIX, 105, 0),   # fresher live says SOLD OUT -> 105 is a gap store
        (PHOENIX, 106, 3),   # live-only listing -> 106 carries one
        (DAYAA,   104, 2),   # fresher reading for 104's Dayaa
    ]
    for sku, sn, qty in live_rows:
        db.execute(
            "INSERT INTO lcbo_live_snapshots "
            "(sku, store_number, qty, store_name, city, checked_at, batch_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (sku, sn, qty, f'LCBO #{sn}', '', LIVE_AT, 'batch-v2-test'))
    db.commit()
    db.close()
    return [s[0] for s in FIXTURE_STORES]


class TestRankingV2:
    """FIX B end to end, in execution order against the shared fixture DB."""

    # ── rebalance is internal only ──────────────────────────────────────────

    def test_rebalance_is_not_owner_reachable(self, fixture_stores, client):
        for hdr, qs in (({'X-View': 'owner'}, ''), ({}, '?view=owner')):
            resp = client.post(f'/api/top100/rebalance{qs}', headers=hdr)
            assert resp.status_code == 403
            assert resp.get_json() == {'error': 'owner view: not permitted'}

    # ── the rebalance fixture test: known SOD + live -> exact order ────────

    def test_rebalance_puts_zero_sku_core_stores_first(self, fixture_stores, client):
        resp = client.post('/api/top100/rebalance')
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body['status'] == 'ok'
        assert body['group_counts'] == {'none': 3, 'one': 2, 'both': 1}
        assert body['preserved'] == 0          # nothing manual yet
        assert body['rebalanced'] == 6         # every store got a rank
        assert body['total_ranked'] == 6

        state = _rank_state()
        _assert_unique_sequential(state)
        by_rank = sorted(state, key=lambda sn: state[sn])
        assert by_rank == EXPECTED_DEFAULT_ORDER, (
            f'expected {EXPECTED_DEFAULT_ORDER}, got {by_rank}')
        # The headline claim: both zero-SKU CORE-city stores lead the board,
        # and the store carrying BOTH SKUs is dead last.
        assert state[105] == 1 and state[101] == 2
        assert state[104] == 6

    def test_rebalance_audits_as_rebalance(self, fixture_stores, client):
        db = _db()
        actors = {r['changed_by'] for r in db.execute(
            "SELECT DISTINCT changed_by FROM territory_status_history "
            "WHERE field='priority_rank'").fetchall()}
        db.close()
        assert actors == {'rebalance'}

    def test_rebalance_is_idempotent(self, fixture_stores, client):
        before = _rank_state()
        body = client.post('/api/top100/rebalance').get_json()
        assert body['rebalanced'] == 0         # already in v2 order
        assert _rank_state() == before

    # ── /api/top100 rows gain the v2 fields ────────────────────────────────

    def test_board_rows_carry_v2_fields(self, fixture_stores, client):
        body = client.get('/api/top100?nocache=1').get_json()
        rows = {r['store_number']: r for r in body['rows']}
        assert [r['store_number'] for r in body['rows']] == EXPECTED_DEFAULT_ORDER
        for sn in EXPECTED_DEFAULT_ORDER:
            r = rows[sn]
            assert r['skus_carried'] in (0, 1, 2)
            assert set(r['carried_detail'].keys()) == {'phoenix', 'dayaa', 'source'}
            assert r['geo_tier'] in ('CORE', 'OUTER')
        assert {sn: rows[sn]['skus_carried'] for sn in rows} == {
            101: 0, 102: 0, 103: 1, 104: 2, 105: 0, 106: 1}
        assert {sn: rows[sn]['geo_tier'] for sn in rows} == {
            101: 'CORE', 102: 'OUTER', 103: 'CORE',
            104: 'OUTER', 105: 'CORE', 106: 'CORE'}
        # A store with no reading anywhere: nulls, carries nothing
        assert rows[101]['carried_detail'] == {
            'phoenix': None, 'dayaa': None, 'source': None}

    def test_carried_detail_uses_fresher_of_sod_and_live(self, fixture_stores, client):
        body = client.get('/api/top100?nocache=1').get_json()
        rows = {r['store_number']: r for r in body['rows']}
        # 105: SOD said 4 on the 13th, live said 0 on the 14th -> live wins
        assert rows[105]['carried_detail'] == {
            'phoenix': 0, 'dayaa': None, 'source': 'live'}
        assert rows[105]['skus_carried'] == 0
        # 103: SOD-only reading survives untouched
        assert rows[103]['carried_detail'] == {
            'phoenix': 5, 'dayaa': None, 'source': 'sod'}
        # 106: live-only listing counts as carrying
        assert rows[106]['carried_detail'] == {
            'phoenix': 3, 'dayaa': None, 'source': 'live'}
        # 104: Phoenix from SOD (no live row), Dayaa from the fresher live
        # row; the freshest reading used is live -> source 'live'
        assert rows[104]['carried_detail'] == {
            'phoenix': 6, 'dayaa': 2, 'source': 'live'}
        assert rows[104]['skus_carried'] == 2

    # ── priority writes re-sequence to UNIQUE ranks + return affected ──────

    def test_priority_write_resequences_and_returns_affected(
            self, fixture_stores, client):
        # Board is 105,101,102,103,106,104 — move 104 (rank 6) to rank 2
        resp = client.post('/api/top100/priority',
                           json={'store_number': 104, 'rank': 2,
                                 'changed_by': 'Namit'})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['rank'] == 2 and body['old_rank'] == 6
        assert body['changed_by'] == 'Namit'

        state = _rank_state()
        _assert_unique_sequential(state)
        assert sorted(state, key=lambda sn: state[sn]) == [
            105, 104, 101, 102, 103, 106]

        # affected = the full re-sequenced order of every row that moved
        # (104 in, everything from old rank 2 down shifted by one)
        affected = {a['store_number']: a for a in body['affected']}
        assert affected[104] == {'store_number': 104, 'old_rank': 6, 'rank': 2}
        for sn in (101, 102, 103, 106):
            assert affected[sn]['rank'] == state[sn]
        got_order = [a['store_number'] for a in body['affected']]
        assert got_order == sorted(got_order, key=lambda sn: state[sn])
        assert 105 not in affected  # rank 1 never moved

        # Audit: the target under the real actor, cascades as 'rebalance'
        db = _db()
        target = db.execute(
            "SELECT changed_by, new_value FROM territory_status_history "
            "WHERE field='priority_rank' AND store_number=104 "
            "ORDER BY id DESC LIMIT 1").fetchone()
        cascade = db.execute(
            "SELECT changed_by FROM territory_status_history "
            "WHERE field='priority_rank' AND store_number=101 "
            "ORDER BY id DESC LIMIT 1").fetchone()
        db.close()
        assert (target['changed_by'], target['new_value']) == ('Namit', '2')
        assert cascade['changed_by'] == 'rebalance'

    # ── manual overrides survive rebalance ──────────────────────────────────

    def test_manual_override_survives_rebalance(self, fixture_stores, client):
        # 104 was manually pinned to rank 2 by Namit in the previous test
        resp = client.post('/api/top100/rebalance')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['preserved'] == 1

        state = _rank_state()
        _assert_unique_sequential(state)
        assert state[104] == 2, 'the manual rank-2 pin must survive rebalance'
        # Defaults resettle around the pin in v2 order
        assert sorted(state, key=lambda sn: state[sn]) == [
            105, 104, 101, 102, 103, 106]

    def test_manually_cleared_rank_survives_rebalance(self, fixture_stores, client):
        resp = client.post('/api/top100/priority',
                           json={'store_number': 106, 'rank': None,
                                 'changed_by': 'Ikshit'})
        assert resp.status_code == 200
        assert resp.get_json()['rank'] is None
        state = _rank_state()
        assert state[106] is None
        _assert_unique_sequential(state)   # gap closed: remaining are 1..5

        body = client.post('/api/top100/rebalance').get_json()
        assert body['preserved'] == 2      # 104 pinned + 106 cleared
        state = _rank_state()
        assert state[106] is None, 'a manually CLEARED rank stays cleared'
        assert state[104] == 2
        _assert_unique_sequential(state)
        assert body['total_ranked'] == 5

    # ── the duplicate-rank bug can never come back ──────────────────────────

    def test_planted_duplicate_ranks_healed_by_any_write(
            self, fixture_stores, client):
        # Recreate the production bug: two rank 4s + a gap, planted directly
        db = _db()
        db.execute("UPDATE territory_stores SET priority_rank=4 "
                   "WHERE store_number IN (101, 102)")
        db.execute("UPDATE territory_stores SET priority_rank=9 "
                   "WHERE store_number=103")
        db.commit()
        db.close()
        ranks = [v for v in _rank_state().values() if v is not None]
        assert len(set(ranks)) < len(ranks)    # dupes really planted

        resp = client.post('/api/top100/priority',
                           json={'store_number': 105, 'rank': 1,
                                 'changed_by': 'Vaneet'})
        assert resp.status_code == 200
        state = _rank_state()
        _assert_unique_sequential(state)       # one write heals the board
        assert state[105] == 1

    def test_thirty_random_writes_keep_ranks_unique_sequential(
            self, fixture_stores, client):
        """The spec's property test: 30 random rank writes (moves, re-pins,
        clears, out-of-range ranks) and the unique-sequential invariant must
        hold after EVERY single one."""
        db = _db()
        for sn in range(201, 209):             # richer board: 8 extra stores
            db.execute(
                "INSERT INTO territory_stores "
                "(store_number, tier, class, account, city, source) "
                "VALUES (?,?,?,?,?,?)",
                (sn, 'territory', 'A', f'LCBO #{sn}', 'Toronto', 'seed'))
        db.commit()
        db.close()

        rng = random.Random(20260714)          # deterministic run
        all_sns = [s[0] for s in FIXTURE_STORES] + list(range(201, 209))
        for i in range(30):
            sn = rng.choice(all_sns)
            rank = rng.choice([None, None] + list(range(1, 20)))
            resp = client.post('/api/top100/priority',
                               json={'store_number': sn, 'rank': rank,
                                     'changed_by': 'Namit'})
            assert resp.status_code == 200, f'write {i}: {resp.get_json()}'
            body = resp.get_json()
            state = _rank_state()
            _assert_unique_sequential(state)
            if rank is None:
                assert state[sn] is None, f'write {i}: clear must stick'
            else:
                assert state[sn] == body['rank'], f'write {i}: rank mismatch'
                ranked_count = sum(1 for v in state.values() if v is not None)
                assert 1 <= state[sn] <= ranked_count
            # response affected rows always match the DB state
            for a in body['affected']:
                assert state[a['store_number']] == a['rank'], f'write {i}'

    def test_rebalance_after_the_storm_still_unique_and_grouped(
            self, fixture_stores, client):
        """After 30 random manual writes everything is manual except the 8
        fresh stores — rebalance must still produce unique sequential ranks
        with every pinned store preserved where possible."""
        before = _rank_state()
        resp = client.post('/api/top100/rebalance')
        assert resp.status_code == 200
        state = _rank_state()
        _assert_unique_sequential(state)
        body = resp.get_json()
        assert body['total_ranked'] == sum(
            1 for v in state.values() if v is not None)
        # Stores never manually touched must not hold a manual pin
        db = _db()
        manual = {r['store_number'] for r in db.execute(
            "SELECT DISTINCT store_number FROM territory_status_history "
            "WHERE field='priority_rank' "
            "AND changed_by NOT IN ('seed-default','rebalance')").fetchall()}
        db.close()
        assert body['preserved'] == len(manual & set(before.keys()))
