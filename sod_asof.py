"""As-of semantics for the LCBO daily inventory file.

What the file is. The LCBO daily file (alldlyinventory<DAY>.zip) is a
LATEST-POSITION file, not a same-day snapshot. Every store's rows carry the
date that store last reported, so one file routinely mixes dates. On the
31 Oct 2026 file roughly half the network was one or two days behind (547,271
rows dated 30 Oct and 245,731 dated 31 Oct inside the 1 Nov file, one store
still on 15 Oct), and the file was the same size as always.

What went wrong before this module. The ingest took the newest date in the
file as "the snapshot" and kept only rows carrying exactly that date. Every
late-reporting store therefore vanished from the day's position, the change
detector read the vanishing as DROPPED, DROPPED folded into the listing
ledger as DELISTED, and a false DELISTED feeds billing (a store that "comes
back" reads as a recovered listing). 36 false delistings and a false
reactivation landed in one tracker over two days.

What this module does.
  build_latest_position  one row per (sku, store): the newest row across all
                         dates in the file, stamped with the file date and
                         carrying its own as_of_date. The day's position is
                         complete even when stores are late.
  prior_position         the comparison baseline read the same way from the
                         database (newest row per store within a window), so
                         a missed ingest day cannot invent listings either.
  per_sku_aggregates     the SKU rollup recomputed from the position.
  network_freshness      share of rows carrying the file date, plus the
                         stores whose newest row is older: the data-quality
                         signal the trackers surface and alert on.
  void_feed_fault_events marks (never deletes) the ledger and change-log
                         events that absence alone produced, so billing and
                         the gap report stop seeing them.
  ensure_columns         the idempotent schema additions behind all of this.

Nothing here deletes a row. Voiding writes voided_at and void_reason on the
existing row and the readers exclude voided rows.
"""
from datetime import date, timedelta

# Locations in the LCBO file that are not retail stores (addresses from the
# store directory). Labels are best reading of public addresses; confirm
# before quoting them to a client.
WAREHOUSE_LOCATIONS = {
    940: 'LCBO location 940, Toronto [verify]',
    941: 'LCBO location 941, 2460 Stanfield Rd, Mississauga [verify]',
    945: 'LCBO location 945, 1980 Bank St, Ottawa [verify]',
    947: 'LCBO location 947, 2000 Boundary Rd, Whitby (Durham RSC) [verify]',
    950: 'LCBO location 950, 955 Wilton Grove Rd, London (London RSC) [verify]',
    955: 'LCBO location 955 [verify]',
    974: 'LCBO location 974, 205 Speirs Giffen Ave, Caledon (warehouse) [verify]',
}

FRESH_SHARE_ALERT = 0.80   # alert when fewer than 80% of rows carry the file date
PRIOR_WINDOW_DAYS = 7      # how far back the comparison baseline may reach


def build_latest_position(rows, file_date):
    """One row per (sku, store): the newest across every date in the file.

    Returns (latest_rows, stats). Each returned row is a copy with
    snapshot_date = file_date and as_of_date = the row's own date, so a reader
    sees the complete network under one date and still knows how old each
    store's word is.
    """
    best = {}
    for r in rows:
        k = (r['sku'], r['store_number'])
        cur = best.get(k)
        if cur is None or r['snapshot_date'] > cur['snapshot_date']:
            best[k] = r
    latest = []
    as_of_hist = {}
    fresh_stores, stale_stores = set(), set()
    for (sku, sn), r in best.items():
        as_of = r['snapshot_date']
        nr = dict(r)
        nr['as_of_date'] = as_of
        nr['snapshot_date'] = file_date
        latest.append(nr)
        as_of_hist[as_of] = as_of_hist.get(as_of, 0) + 1
        (fresh_stores if as_of == file_date else stale_stores).add(sn)
    stale_stores -= fresh_stores
    stats = {
        'pairs': len(latest),
        'stale_pairs': sum(n for d, n in as_of_hist.items() if d != file_date),
        'stale_stores': sorted(stale_stores),
        'as_of_hist': dict(sorted(as_of_hist.items())),
        'oldest_as_of': min(as_of_hist) if as_of_hist else None,
    }
    return latest, stats


def per_sku_aggregates(latest_rows):
    """The per-SKU rollup the ingest writes to sod_products, from the position."""
    out = {}
    for r in latest_rows:
        agg = out.get(r['sku'])
        if agg is None:
            agg = {'name': r['product_name'], 'status_counts': {},
                   'store_count': 0, 'total_on_hand': 0}
            out[r['sku']] = agg
        agg['status_counts'][r['status']] = agg['status_counts'].get(r['status'], 0) + 1
        agg['store_count'] += 1
        agg['total_on_hand'] += r['on_hand']
    return out


def network_freshness(total_by_date, file_date):
    """Share of ALL rows in the file (every SKU, every store) dated file_date."""
    total = sum(total_by_date.values())
    if not total:
        return None
    return round(total_by_date.get(file_date, 0) / total, 4)


def prior_position(cur, use_postgres, sku, before_date, window_days=PRIOR_WINDOW_DAYS):
    """{store: status} as it stood before `before_date`.

    The newest row per store with snapshot_date < before_date, within
    window_days, MINUS any store with a surviving (non-voided) DROPPED change
    dated after that row: that store really left the file and must not be
    reported dropped again every day until its rows age out of the window.
    Reading the baseline this way means a day the ingest missed, or a day half
    the network was late, never shows up as stores appearing or disappearing.
    """
    ph = '%s' if use_postgres else '?'
    if use_postgres:
        cur.execute(
            "SELECT DISTINCT ON (store_number) store_number, status, CAST(snapshot_date AS TEXT) "
            "FROM sod_inventory WHERE sku=%s AND snapshot_date < %s::date "
            "AND snapshot_date >= (%s::date - %s * INTERVAL '1 day') "
            "ORDER BY store_number, snapshot_date DESC",
            (sku, before_date, before_date, window_days))
    else:
        cur.execute(
            "SELECT i.store_number, i.status, i.snapshot_date FROM sod_inventory i "
            "WHERE i.sku=? AND i.snapshot_date < ? AND i.snapshot_date >= date(?, ?) "
            "AND i.snapshot_date = (SELECT MAX(j.snapshot_date) FROM sod_inventory j "
            "  WHERE j.sku=i.sku AND j.store_number=i.store_number AND j.snapshot_date < ?)",
            (sku, before_date, before_date, f'-{window_days} days', before_date))
    newest = {int(r[0]): (r[1], str(r[2])[:10]) for r in cur.fetchall()}
    dropped = {}
    try:
        cur.execute(
            f"SELECT store_number, MAX(CAST(change_date AS TEXT)) FROM sod_store_sku_changes "
            f"WHERE sku={ph} AND change_type='DROPPED' AND voided_at IS NULL "
            f"AND change_date < {ph} GROUP BY store_number", (sku, before_date))
        dropped = {int(r[0]): str(r[1])[:10] for r in cur.fetchall()}
    except Exception:
        dropped = {}
    out = {}
    for sn, (status, d) in newest.items():
        if sn in dropped and dropped[sn] > d:
            continue
        out[sn] = status
    return out


def _table_exists(cur, use_postgres, table):
    try:
        if use_postgres:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s", (table,))
        else:
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None
    except Exception:
        return False


_COLUMN_SPECS = (
    ('sod_inventory', 'as_of_date', 'DATE'),
    ('listing_ledger', 'voided_at', 'TIMESTAMP'),
    ('listing_ledger', 'void_reason', 'TEXT'),
    ('sod_store_sku_changes', 'voided_at', 'TIMESTAMP'),
    ('sod_store_sku_changes', 'void_reason', 'TEXT'),
    ('sod_sync_runs', 'fresh_share', 'REAL'),
    ('sod_sync_runs', 'stale_stores', 'INTEGER'),
    ('mandakini_reactivations', 'voided_at', 'TIMESTAMP'),
    ('mandakini_reactivations', 'void_reason', 'TEXT'),
)


def ensure_columns(conn, use_postgres):
    """Idempotent. Adds the as-of and void columns wherever the table exists.
    Safe on autocommit and transactional connections alike."""
    cur = conn.cursor()
    autocommit = bool(getattr(conn, 'autocommit', False)) if use_postgres else True
    for table, col, typ in _COLUMN_SPECS:
        if not _table_exists(cur, use_postgres, table):
            continue
        try:
            if use_postgres:
                if not autocommit:
                    cur.execute("SAVEPOINT sp_asof_col")
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
                if not autocommit:
                    cur.execute("RELEASE SAVEPOINT sp_asof_col")
            else:
                cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
                if col not in cols:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except Exception:
            if use_postgres and not autocommit:
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_asof_col")
                except Exception:
                    pass
    try:
        conn.commit()
    except Exception:
        pass
    cur.close()


def void_feed_fault_events(cur, use_postgres, file_date, latest_rows, lookback_days=14):
    """Void the events that a missing row, not LCBO, produced. Never deletes.

    Candidates: 'sod' DELISTED events dated within lookback_days whose
    (sku, store) is PRESENT in today's position with status L and that have
    no sod_inventory row with status D or F on the event date (a genuine
    status flip always leaves one). A candidate is a feed-gap artefact, and
    is voided, when any of these holds:
      a. the store's current as-of date is on or before the event date: the
         store has not spoken since before the "delist", so the event cannot
         have come from it;
      b. the hole is short and bounded: an L row within 3 days before the
         event and an L row within 3 days after it (LCBO does not delist and
         relist a store inside three days; a missing row does that);
      c. lcbo.com showed stock at the store at a check 2 to 7 days after the
         event (the shelf contradicts the delist; the two-day wait skips the
         one-day lag of leftover units).
    A pair absent from the whole file is a real drop and is never touched; a
    store whose newest word is D or F keeps its delist.

    After voiding a delist, a 'sod' LISTED for the same pair within 14 days
    after it, where the pair already had L evidence before the voided delist,
    is a re-appearance, not a new listing: voided too. DROPPED, NEW_LISTING and
    RELISTED rows in sod_store_sku_changes are voided alongside, and a
    mandakini_reactivations row detected as delist_relist with no surviving
    delist behind it is voided. Returns counts.
    """
    ph = '%s' if use_postgres else '?'
    now_sql = 'NOW()' if use_postgres else 'CURRENT_TIMESTAMP'
    present_L = {(r['sku'], int(r['store_number'])) for r in latest_rows if r['status'] == 'L'}
    as_of = {(r['sku'], int(r['store_number'])): str(r.get('as_of_date', r['snapshot_date']))[:10]
             for r in latest_rows}
    fd = date.fromisoformat(str(file_date)[:10])
    since = (fd - timedelta(days=lookback_days)).isoformat()
    have_live = _table_exists(cur, use_postgres, 'lcbo_live_snapshots')
    counts = {'delists_voided': 0, 'relists_voided': 0, 'changes_voided': 0, 'reactivations_voided': 0}

    def _l_row_between(sku, sn, lo, hi):
        cur.execute(
            f"SELECT 1 FROM sod_inventory WHERE sku={ph} AND store_number={ph} AND status='L' "
            f"AND snapshot_date >= {ph} AND snapshot_date <= {ph} LIMIT 1", (sku, sn, lo, hi))
        return cur.fetchone() is not None

    cur.execute(
        f"SELECT id, sku, store_number, CAST(observed_date AS TEXT) FROM listing_ledger "
        f"WHERE event='DELISTED' AND source='sod' AND voided_at IS NULL AND observed_date >= {ph}",
        (since,))
    voided = []
    for lid, sku, sn, d in cur.fetchall():
        key = (sku, int(sn))
        d = str(d)[:10]
        if key not in present_L:
            continue
        cur.execute(
            f"SELECT 1 FROM sod_inventory WHERE sku={ph} AND store_number={ph} "
            f"AND snapshot_date={ph} AND status IN ('D','F') LIMIT 1", (sku, int(sn), d))
        if cur.fetchone():
            continue
        dd = date.fromisoformat(d)
        why = None
        if as_of.get(key, '') <= d:
            why = f"the store's latest word is dated {as_of.get(key)}, on or before the event"
        elif (_l_row_between(sku, int(sn), (dd - timedelta(days=3)).isoformat(), (dd - timedelta(days=1)).isoformat())
              and _l_row_between(sku, int(sn), (dd + timedelta(days=1)).isoformat(), (dd + timedelta(days=3)).isoformat())):
            why = 'listed within three days on both sides of the missing row'
        elif have_live:
            cur.execute(
                f"SELECT 1 FROM lcbo_live_snapshots WHERE sku={ph} AND store_number={ph} AND qty > 0 "
                f"AND checked_at >= {ph} AND checked_at < {ph} LIMIT 1",
                (sku, int(sn), (dd + timedelta(days=2)).isoformat(), (dd + timedelta(days=8)).isoformat()))
            if cur.fetchone():
                why = 'lcbo.com showed stock at the store two or more days after the event'
        if why is None:
            continue
        reason = f"feed gap: no row for this store in the {d} file; {why}"
        cur.execute(f"UPDATE listing_ledger SET voided_at={now_sql}, void_reason={ph} WHERE id={ph}", (reason, lid))
        counts['delists_voided'] += 1
        cur.execute(
            f"UPDATE sod_store_sku_changes SET voided_at={now_sql}, void_reason={ph} "
            f"WHERE sku={ph} AND store_number={ph} AND change_date={ph} "
            f"AND change_type IN ('DROPPED','DELISTED') AND voided_at IS NULL", (reason, sku, int(sn), d))
        counts['changes_voided'] += max(cur.rowcount or 0, 0)
        voided.append((sku, int(sn), d))

    for sku, sn, d in voided:
        cur.execute(
            f"SELECT 1 FROM listing_ledger WHERE sku={ph} AND store_number={ph} "
            f"AND event IN ('LISTED','RECONFIRMED') AND voided_at IS NULL AND observed_date < {ph} LIMIT 1",
            (sku, sn, d))
        if not cur.fetchone():
            continue
        until = (date.fromisoformat(d) + timedelta(days=14)).isoformat()
        cur.execute(
            f"SELECT id, CAST(observed_date AS TEXT) FROM listing_ledger WHERE sku={ph} AND store_number={ph} "
            f"AND event='LISTED' AND source='sod' AND voided_at IS NULL "
            f"AND observed_date > {ph} AND observed_date <= {ph}", (sku, sn, d, until))
        for lid2, d2 in cur.fetchall():
            d2 = str(d2)[:10]
            reason = f"feed gap: re-appearance after the voided {d} delist, not a new listing"
            cur.execute(f"UPDATE listing_ledger SET voided_at={now_sql}, void_reason={ph} WHERE id={ph}", (reason, lid2))
            counts['relists_voided'] += 1
            cur.execute(
                f"UPDATE sod_store_sku_changes SET voided_at={now_sql}, void_reason={ph} "
                f"WHERE sku={ph} AND store_number={ph} AND change_date={ph} "
                f"AND change_type IN ('NEW_LISTING','RELISTED') AND voided_at IS NULL", (reason, sku, sn, d2))
            counts['changes_voided'] += max(cur.rowcount or 0, 0)

    if _table_exists(cur, use_postgres, 'mandakini_reactivations'):
        cur.execute("SELECT id, store_number, sku, CAST(reactivated_on AS TEXT) FROM mandakini_reactivations "
                    "WHERE detected_via='delist_relist' AND voided_at IS NULL")
        for rid, sn, sku, d in cur.fetchall():
            cur.execute(
                f"SELECT 1 FROM listing_ledger WHERE sku={ph} AND store_number={ph} AND event='DELISTED' "
                f"AND voided_at IS NULL AND observed_date <= {ph} LIMIT 1", (sku, int(sn), str(d)[:10]))
            if cur.fetchone():
                continue
            cur.execute(
                f"UPDATE mandakini_reactivations SET voided_at={now_sql}, void_reason={ph} WHERE id={ph}",
                ('feed gap: the delist behind this recovery was voided; nothing was recovered', rid))
            counts['reactivations_voided'] += 1
    return counts
