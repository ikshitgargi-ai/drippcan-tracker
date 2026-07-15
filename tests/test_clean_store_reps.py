import os, sys, tempfile
os.environ.pop('DATABASE_URL', None); os.environ.pop('ADMIN_TOKEN', None); os.environ.pop('API_KEY', None)
_T=tempfile.mkdtemp(prefix='cleanreps_'); os.environ['DB_DIR']=_T
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import pytest
@pytest.fixture(scope='module')
def app_module():
    for m in list(sys.modules):
        if m=='app' or m.startswith('app.'): del sys.modules[m]
    os.environ['DB_DIR']=_T
    import importlib.util
    spec=importlib.util.spec_from_file_location('app', os.path.join(os.path.dirname(__file__),'..','app.py'))
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
@pytest.fixture
def client(app_module): return app_module.app.test_client()
def test_clean_store_reps(app_module, client):
    with app_module.app.app_context():
        db=app_module.get_db()
        for sn,rep in ((8001,'Wendy Jones'),(8002,'meghan borisko'),(8003,'Ikshit Sharma'),(8004,'Namit'),(8005,'Montana Marshall')):
            db.execute("INSERT OR IGNORE INTO stores (store_number, rep) VALUES (?,?)",(sn,rep))
        db.commit()
    r=client.post('/api/admin/clean-store-reps', json={})
    assert r.status_code==200, r.get_json()
    b=r.get_json()
    assert b['normalized_ikshit_sharma']>=1
    assert b['cleared_stray']>=2
    remaining=set(x.lower() for x in b['remaining_reps'])
    assert 'wendy jones' not in remaining and 'meghan borisko' not in remaining and 'montana marshall' not in remaining
    assert 'ikshit' in remaining and 'namit' in remaining
