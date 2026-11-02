"""Rep accounts: the roster lives in the database, reps sign in with a PIN,
managers add and retire reps from the app. Nothing is ever deleted; a
retired rep keeps every visit and listing they ever logged.

Schema (added idempotently to the existing `reps` table):
  active      TRUE only for a rep a manager has put on the roster. A row that
              lands in `reps` any other way (directory imports, old seeds) is
              off the roster until a manager adds it, so stale names never
              reappear in a picker.
  role        rep | manager | owner
  email, phone, notes
  pin_hash    pbkdf2-sha256 of a 4 to 8 digit PIN, NULL until a manager sets one
  pin_set_at, retired_at, created_at
and a `rep_sessions` table (token, rep_id, created_at, expires_at,
last_seen_at, user_agent).

Sign-in is soft during the roll-out: a rep with no PIN on file still signs
in by name alone (the pre-existing behaviour), and the response says
pin_required=false so the app can show the manager that a PIN is missing.
Once a PIN is set, that rep must present it, and a visit logged in that rep's
name without a valid session is refused.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

ROLES = ('rep', 'manager', 'owner')
SESSION_DAYS = 30
PIN_MIN, PIN_MAX = 4, 8

_COLUMNS = (
    ('active', 'BOOLEAN DEFAULT FALSE'),
    ('role', "TEXT DEFAULT 'rep'"),
    ('email', "TEXT DEFAULT ''"),
    ('phone', "TEXT DEFAULT ''"),
    ('notes', "TEXT DEFAULT ''"),
    ('pin_hash', 'TEXT'),
    ('pin_set_at', 'TIMESTAMP'),
    ('retired_at', 'TIMESTAMP'),
    ('created_at', 'TIMESTAMP'),
)


def _cols(cur, use_postgres, table):
    if use_postgres:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,))
        return {r[0] for r in cur.fetchall()}
    return {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_schema(conn, use_postgres):
    """Schema only, idempotent, no rows touched: the account columns on
    `reps`, the `rep_sessions` table and the `rep_account_meta` marker table.
    Safe to call before a salvage restore (so restored rows have somewhere to
    land) and again afterwards."""
    cur = conn.cursor()
    for col, typ in _COLUMNS:
        if not use_postgres:
            typ = typ.replace('DEFAULT FALSE', 'DEFAULT 0')
        try:
            if use_postgres:
                cur.execute(f"ALTER TABLE reps ADD COLUMN IF NOT EXISTS {col} {typ}")
            elif col not in _cols(cur, use_postgres, 'reps'):
                cur.execute(f"ALTER TABLE reps ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
    cur.execute("""CREATE TABLE IF NOT EXISTS rep_sessions (
        token TEXT PRIMARY KEY, rep_id INTEGER NOT NULL, created_at TIMESTAMP, expires_at TIMESTAMP,
        last_seen_at TIMESTAMP, user_agent TEXT DEFAULT '')""")
    cur.execute("CREATE TABLE IF NOT EXISTS rep_account_meta (key TEXT PRIMARY KEY, value TEXT, set_at TIMESTAMP)")
    conn.commit()
    cur.close()


def normalize_roster(conn, use_postgres, roster_default, owner_name=None):
    """Runs once per database (marker row in rep_account_meta, which rides
    every backup and salvage snapshot): the seed roster decides who is active
    on the day the database moves to accounts, the owner gets the owner role,
    created_at is stamped. After that the database is the only roster and a
    manager is the only way on or off it. Returns True when it ran."""
    cur = conn.cursor()
    ph = '%s' if use_postgres else '?'
    now = 'NOW()' if use_postgres else 'CURRENT_TIMESTAMP'
    t, f = ('TRUE', 'FALSE') if use_postgres else ('1', '0')
    cur.execute(f"SELECT value FROM rep_account_meta WHERE key={ph}", ('roster_normalized',))
    if cur.fetchone():
        cur.close()
        return False
    names = [str(n).strip().lower() for n in (roster_default or [])]
    if names:
        phs = ','.join([ph] * len(names))
        cur.execute(f"UPDATE reps SET active = {f} WHERE LOWER(TRIM(name)) NOT IN ({phs})", names)
        cur.execute(f"UPDATE reps SET active = {t} WHERE LOWER(TRIM(name)) IN ({phs})", names)
    cur.execute(f"UPDATE reps SET created_at = {now} WHERE created_at IS NULL")
    cur.execute("UPDATE reps SET role = 'rep' WHERE role IS NULL OR role = ''")
    if owner_name:
        cur.execute(f"UPDATE reps SET role = 'owner' WHERE LOWER(TRIM(name)) = {ph} AND role = 'rep'",
                    (owner_name.strip().lower(),))
    cur.execute(f"INSERT INTO rep_account_meta (key, value, set_at) VALUES ({ph}, {ph}, {now})",
                ('roster_normalized', ','.join(roster_default or [])))
    conn.commit()
    cur.close()
    return True


def hash_pin(pin):
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac('sha256', str(pin).encode(), salt.encode(), 120_000).hex()
    return f'pbkdf2$120000${salt}${digest}'


def verify_pin(pin, stored):
    try:
        _algo, iters, salt, digest = str(stored).split('$')
        test = hashlib.pbkdf2_hmac('sha256', str(pin).encode(), salt.encode(), int(iters)).hex()
        return secrets.compare_digest(test, digest)
    except Exception:
        return False


def pin_ok(pin):
    p = str(pin or '').strip()
    return p.isdigit() and PIN_MIN <= len(p) <= PIN_MAX


def _row(r):
    d = dict(r) if hasattr(r, 'keys') else r
    return {
        'id': int(d['id']), 'name': d['name'], 'role': d.get('role') or 'rep',
        'active': bool(d.get('active')), 'email': d.get('email') or '', 'phone': d.get('phone') or '',
        'notes': d.get('notes') or '', 'has_pin': bool(d.get('pin_hash')),
        'pin_set_at': str(d['pin_set_at'])[:19] if d.get('pin_set_at') else None,
        'retired_at': str(d['retired_at'])[:19] if d.get('retired_at') else None,
        'created_at': str(d['created_at'])[:19] if d.get('created_at') else None,
    }


def _fetch_dicts(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_reps(cur, use_postgres, include_retired=False):
    where = '' if include_retired else f"WHERE active = {'TRUE' if use_postgres else '1'}"
    rows = _fetch_dicts(cur, f"SELECT id, name, role, active, email, phone, notes, pin_hash, pin_set_at, retired_at, created_at "
                             f"FROM reps {where} ORDER BY active DESC, name")
    return [_row(r) for r in rows]


def get_rep(cur, use_postgres, rep_id=None, name=None):
    ph = '%s' if use_postgres else '?'
    if rep_id is not None:
        rows = _fetch_dicts(cur, f"SELECT id, name, role, active, email, phone, notes, pin_hash, pin_set_at, retired_at, created_at FROM reps WHERE id={ph}", (int(rep_id),))
    else:
        rows = _fetch_dicts(cur, f"SELECT id, name, role, active, email, phone, notes, pin_hash, pin_set_at, retired_at, created_at FROM reps WHERE LOWER(TRIM(name))={ph}", (str(name or '').strip().lower(),))
    return rows[0] if rows else None


def create_or_reactivate(cur, use_postgres, name, role='rep', email='', phone='', pin=None, notes=''):
    """Add a rep, or bring a retired one back under the same name. Returns the
    account dict. Raises ValueError on a bad name, role or PIN."""
    name = ' '.join(str(name or '').split()).strip()
    if not name:
        raise ValueError('name is required')
    if role not in ROLES:
        raise ValueError(f'role must be one of {ROLES}')
    if pin is not None and str(pin).strip() and not pin_ok(pin):
        raise ValueError(f'PIN must be {PIN_MIN} to {PIN_MAX} digits')
    ph = '%s' if use_postgres else '?'
    now = 'NOW()' if use_postgres else 'CURRENT_TIMESTAMP'
    existing = get_rep(cur, use_postgres, name=name)
    pin_hash = hash_pin(pin) if (pin is not None and str(pin).strip()) else None
    if existing:
        sets = [f"active={'TRUE' if use_postgres else '1'}", "retired_at=NULL", f"role={ph}", f"email={ph}", f"phone={ph}", f"notes={ph}"]
        params = [role, email or '', phone or '', notes or '']
        if pin_hash:
            sets += [f"pin_hash={ph}", f"pin_set_at={now}"]
            params.append(pin_hash)
        params.append(existing['id'])
        cur.execute(f"UPDATE reps SET {', '.join(sets)} WHERE id={ph}", params)
        return _row(get_rep(cur, use_postgres, rep_id=existing['id']))
    cur.execute(
        f"INSERT INTO reps (name, role, active, email, phone, notes, pin_hash, pin_set_at, created_at) "
        f"VALUES ({ph},{ph},{'TRUE' if use_postgres else '1'},{ph},{ph},{ph},{ph},{('NOW()' if pin_hash else 'NULL') if use_postgres else ('CURRENT_TIMESTAMP' if pin_hash else 'NULL')},{now})",
        (name, role, email or '', phone or '', notes or '', pin_hash))
    return _row(get_rep(cur, use_postgres, name=name))


def update_rep(cur, use_postgres, rep_id, fields):
    """Retire (active=false, retired_at stamped), reactivate, change role or
    contact details, or set a new PIN. Never deletes."""
    ph = '%s' if use_postgres else '?'
    now = 'NOW()' if use_postgres else 'CURRENT_TIMESTAMP'
    rep = get_rep(cur, use_postgres, rep_id=rep_id)
    if not rep:
        raise ValueError('no such rep')
    sets, params = [], []
    if 'active' in fields:
        active = bool(fields['active'])
        sets.append(f"active={('TRUE' if active else 'FALSE') if use_postgres else ('1' if active else '0')}")
        sets.append(f"retired_at={'NULL' if active else now}")
    if 'role' in fields:
        if fields['role'] not in ROLES:
            raise ValueError(f'role must be one of {ROLES}')
        sets.append(f"role={ph}"); params.append(fields['role'])
    for k in ('email', 'phone', 'notes'):
        if k in fields:
            sets.append(f"{k}={ph}"); params.append(str(fields[k] or ''))
    if 'pin' in fields and fields['pin'] is not None:
        if str(fields['pin']).strip() == '':
            sets.append("pin_hash=NULL"); sets.append("pin_set_at=NULL")
        else:
            if not pin_ok(fields['pin']):
                raise ValueError(f'PIN must be {PIN_MIN} to {PIN_MAX} digits')
            sets.append(f"pin_hash={ph}"); params.append(hash_pin(fields['pin']))
            sets.append(f"pin_set_at={now}")
    if not sets:
        return _row(rep)
    params.append(int(rep_id))
    cur.execute(f"UPDATE reps SET {', '.join(sets)} WHERE id={ph}", params)
    if 'active' in fields and not fields['active']:
        # Retiring ends every open session; the rows stay (nothing is deleted).
        cur.execute(f"UPDATE rep_sessions SET expires_at={ph} WHERE rep_id={ph} AND expires_at > {ph}",
                    (datetime.utcnow() - timedelta(seconds=1), int(rep_id), datetime.utcnow()))
    return _row(get_rep(cur, use_postgres, rep_id=rep_id))


def login(cur, use_postgres, name, pin=None, user_agent=''):
    """Returns (ok, payload). ok=False payload has 'error' and maybe 'pin_required'."""
    rep = get_rep(cur, use_postgres, name=name)
    if not rep or not rep.get('active'):
        return False, {'error': 'no active rep by that name'}
    if rep.get('pin_hash'):
        if not pin:
            return False, {'error': 'PIN required', 'pin_required': True, 'rep': _row(rep)['name']}
        if not verify_pin(pin, rep['pin_hash']):
            return False, {'error': 'wrong PIN', 'pin_required': True}
    token = secrets.token_urlsafe(32)
    ph = '%s' if use_postgres else '?'
    now = datetime.utcnow()
    cur.execute(f"INSERT INTO rep_sessions (token, rep_id, created_at, expires_at, last_seen_at, user_agent) VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (token, int(rep['id']), now, now + timedelta(days=SESSION_DAYS), now, (user_agent or '')[:200]))
    return True, {'token': token, 'rep': _row(rep), 'pin_required': bool(rep.get('pin_hash')),
                  'expires_at': (now + timedelta(days=SESSION_DAYS)).isoformat() + 'Z'}


def session_rep(cur, use_postgres, token):
    if not token:
        return None
    ph = '%s' if use_postgres else '?'
    rows = _fetch_dicts(cur, f"SELECT rep_id, expires_at FROM rep_sessions WHERE token={ph}", (token,))
    if not rows:
        return None
    exp = rows[0]['expires_at']
    try:
        exp_dt = exp if isinstance(exp, datetime) else datetime.fromisoformat(str(exp).replace('Z', ''))
        if exp_dt < datetime.utcnow():
            return None
    except Exception:
        pass
    rep = get_rep(cur, use_postgres, rep_id=rows[0]['rep_id'])
    if not rep or not rep.get('active'):
        return None
    try:
        cur.execute(f"UPDATE rep_sessions SET last_seen_at={ph} WHERE token={ph}", (datetime.utcnow(), token))
    except Exception:
        pass
    return _row(rep)


def logout(cur, use_postgres, token):
    """Expire the session; the row stays as the sign-in record."""
    ph = '%s' if use_postgres else '?'
    cur.execute(f"UPDATE rep_sessions SET expires_at={ph} WHERE token={ph}", (datetime.utcnow() - timedelta(seconds=1), token))


def active_names(cur, use_postgres):
    return [r['name'] for r in list_reps(cur, use_postgres)]
