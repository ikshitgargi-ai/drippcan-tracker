"""Regression: scheduled jobs must run inside an app context so get_db()/g
work from a bare APScheduler worker thread (the daily backup was silently
dying with 'Working outside of application context')."""
import os, sys, tempfile, threading
os.environ.pop('DATABASE_URL', None); os.environ.pop('ADMIN_TOKEN', None); os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='sched_ctx_'); os.environ['DB_DIR'] = _TMP
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import pytest

@pytest.fixture(scope='module')
def app_module():
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'): del sys.modules[m]
    os.environ['DB_DIR'] = _TMP
    import importlib.util
    spec = importlib.util.spec_from_file_location('app', os.path.join(os.path.dirname(__file__), '..', 'app.py'))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def test_get_db_raises_in_bare_thread(app_module):
    err = {}
    def worker():
        try: app_module.get_db()
        except Exception as e: err['e'] = type(e).__name__
    t = threading.Thread(target=worker); t.start(); t.join()
    assert err.get('e') == 'RuntimeError'  # proves the bug exists without the wrapper

def test_backup_builder_runs_in_wrapped_thread(app_module):
    out = {}
    def worker():
        try:
            payload = app_module._in_app_context(app_module._build_essential_backup)()
            out['ok'] = isinstance(payload, dict) and len(payload) > 0
        except Exception as e:
            out['err'] = f'{type(e).__name__}: {e}'
    t = threading.Thread(target=worker); t.start(); t.join()
    assert out.get('err') is None, out.get('err')
    assert out.get('ok') is True
