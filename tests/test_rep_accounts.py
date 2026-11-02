"""Reps can be added and retired from the app, sign in with a PIN once one is
set, and a retired rep keeps every visit ever logged. Nothing is deleted."""
import importlib.util
import os
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)
import rep_accounts as ra  # noqa: E402


@pytest.fixture(scope='module')
def app_module():
    prev = os.environ.get('DB_DIR')
    tmp = tempfile.mkdtemp(prefix='reps_')
    os.environ.pop('DATABASE_URL', None)
    os.environ['DB_DIR'] = tmp
    os.environ['SALVAGE_RESTORE_DISABLED'] = '1'
    os.environ['OWNER_PASSCODE'] = 'test-owner-passcode'
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]
    spec = importlib.util.spec_from_file_location('app', os.path.join(ROOT, 'app.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    os.environ.pop('OWNER_PASSCODE', None)
    if prev is None:
        os.environ.pop('DB_DIR', None)
    else:
        os.environ['DB_DIR'] = prev
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


OWNER = {'X-Owner-Passcode': 'test-owner-passcode'}


class TestPins:
    def test_hash_and_verify(self):
        h = ra.hash_pin('2468')
        assert h.startswith('pbkdf2$') and ra.verify_pin('2468', h) and not ra.verify_pin('2469', h)

    def test_pin_rules(self):
        assert ra.pin_ok('1234') and ra.pin_ok('12345678')
        assert not ra.pin_ok('123') and not ra.pin_ok('12a4') and not ra.pin_ok('123456789')


class TestRosterFromTheDatabase:
    def test_seed_roster_is_active_and_owner_role_set(self, client, app_module):
        reps = client.get('/api/reps').get_json()
        names = {r['name'] for r in reps}
        assert names == set(app_module.ANU_REP_ROSTER)
        assert all(r['active'] for r in reps)
        assert next(r for r in reps if r['name'] == 'Ikshit')['role'] == 'owner'

    def test_manager_endpoints_are_guarded(self, client):
        assert client.get('/api/reps/all').status_code == 403
        assert client.post('/api/reps', json={'name': 'Mallory'}).status_code == 403
        assert client.get('/api/reps/all', headers=OWNER).status_code == 200

    def test_add_rep_appears_everywhere_at_once(self, client, app_module):
        r = client.post('/api/reps', json={'name': 'Priya', 'role': 'rep', 'phone': '416-000-0000'}, headers=OWNER)
        assert r.status_code == 201, r.get_json()
        assert 'Priya' in {x['name'] for x in client.get('/api/reps').get_json()}
        assert 'Priya' in app_module.REP_ROSTER_DEFAULT, 'the roster constant follows the database'
        assert 'priya' in app_module._OFFICIAL_STORE_REPS

    def test_retire_keeps_history_and_leaves_the_pickers(self, client, app_module):
        reps = {x['name']: x for x in client.get('/api/reps').get_json()}
        pid = reps['Priya']['id']
        # log a visit as Priya first
        with app_module.app.app_context():
            db = app_module.get_db()
            sid = db.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()[0]
        v = client.post('/api/activities', json={'store_id': sid, 'rep_id': pid, 'activity_type': 'store_visit', 'notes': 'hello'},
                        headers={'Origin': 'https://drippcan-web.vercel.app'})
        assert v.status_code in (200, 201), v.get_json()
        r = client.patch(f'/api/reps/{pid}', json={'active': False}, headers=OWNER)
        assert r.status_code == 200 and r.get_json()['active'] is False and r.get_json()['retired_at']
        assert 'Priya' not in {x['name'] for x in client.get('/api/reps').get_json()}
        assert 'Priya' not in app_module.REP_ROSTER_DEFAULT
        allr = client.get('/api/reps/all', headers=OWNER).get_json()
        assert any(x['name'] == 'Priya' and not x['active'] for x in allr), 'retired, never deleted'
        with app_module.app.app_context():
            db = app_module.get_db()
            n = db.execute("SELECT COUNT(*) FROM activities WHERE rep_id=?", (pid,)).fetchone()[0]
        assert n == 1, 'the retired rep keeps every visit'
        # a retired rep cannot be logged for
        v2 = client.post('/api/activities', json={'store_id': sid, 'rep_id': pid, 'activity_type': 'store_visit'},
                         headers={'Origin': 'https://drippcan-web.vercel.app'})
        assert v2.status_code == 403

    def test_reactivate_under_the_same_name(self, client):
        r = client.post('/api/reps', json={'name': 'priya', 'role': 'manager'}, headers=OWNER)
        assert r.status_code == 201 and r.get_json()['active'] is True and r.get_json()['role'] == 'manager'
        assert len([x for x in client.get('/api/reps/all', headers=OWNER).get_json() if x['name'].lower() == 'priya']) == 1


class TestSignIn:
    def test_legacy_name_only_sign_in_until_a_pin_is_set(self, client):
        r = client.post('/api/auth/login', json={'name': 'Namit'})
        assert r.status_code == 200
        body = r.get_json()
        assert body['token'] and body['rep']['name'] == 'Namit' and body['pin_required'] is False
        me = client.get('/api/auth/me', headers={'X-Rep-Token': body['token']})
        assert me.status_code == 200 and me.get_json()['name'] == 'Namit'

    def test_pin_required_once_set_and_wrong_pin_refused(self, client):
        reps = {x['name']: x for x in client.get('/api/reps').get_json()}
        nid = reps['Namit']['id']
        assert client.patch(f'/api/reps/{nid}', json={'pin': '1357'}, headers=OWNER).status_code == 200
        r = client.post('/api/auth/login', json={'name': 'Namit'})
        assert r.status_code == 401 and r.get_json().get('pin_required') is True
        assert client.post('/api/auth/login', json={'name': 'Namit', 'pin': '0000'}).status_code == 401
        ok = client.post('/api/auth/login', json={'name': 'Namit', 'pin': '1357'})
        assert ok.status_code == 200 and ok.get_json()['pin_required'] is True
        assert client.patch(f'/api/reps/{nid}', json={'pin': '12'}, headers=OWNER).status_code == 400

    def test_visit_logged_with_a_session_is_that_rep_and_impersonation_is_refused(self, client, app_module):
        reps = {x['name']: x for x in client.get('/api/reps').get_json()}
        nid, sid_rep = reps['Namit']['id'], reps['Surya']['id']
        tok = client.post('/api/auth/login', json={'name': 'Namit', 'pin': '1357'}).get_json()['token']
        with app_module.app.app_context():
            sid = app_module.get_db().execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()[0]
        # body says Surya, session says Namit: the session wins
        v = client.post('/api/activities', json={'store_id': sid, 'rep_id': sid_rep, 'activity_type': 'call'},
                        headers={'Origin': 'https://drippcan-web.vercel.app', 'X-Rep-Token': tok})
        assert v.status_code in (200, 201)
        with app_module.app.app_context():
            last = app_module.get_db().execute("SELECT rep_id FROM activities ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert last == nid
        # no session, naming the rep who has a PIN: refused
        v2 = client.post('/api/activities', json={'store_id': sid, 'rep_id': nid, 'activity_type': 'call'},
                         headers={'Origin': 'https://drippcan-web.vercel.app'})
        assert v2.status_code == 401 and v2.get_json().get('pin_required') is True

    def test_manager_session_opens_the_manager_endpoints(self, client):
        reps = {x['name']: x for x in client.get('/api/reps/all', headers=OWNER).get_json()}
        pid = reps['Priya']['id']   # reactivated as a manager above
        client.patch(f'/api/reps/{pid}', json={'pin': '9876'}, headers=OWNER)
        tok = client.post('/api/auth/login', json={'name': 'Priya', 'pin': '9876'}).get_json()['token']
        assert client.get('/api/reps/all', headers={'X-Rep-Token': tok}).status_code == 200
        rep_tok = client.post('/api/auth/login', json={'name': 'Surya'}).get_json()['token']
        assert client.get('/api/reps/all', headers={'X-Rep-Token': rep_tok}).status_code == 403

    def test_logout_ends_the_session(self, client):
        tok = client.post('/api/auth/login', json={'name': 'Surya'}).get_json()['token']
        assert client.post('/api/auth/logout', headers={'X-Rep-Token': tok}).status_code == 200
        assert client.get('/api/auth/me', headers={'X-Rep-Token': tok}).status_code == 401
