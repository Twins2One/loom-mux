"""Isolated SQLite evidence for retrieval modes; no model/network dependency."""
import sqlite3
import struct
import pytest
from loom import memory as m


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(m, '_db_path', lambda: tmp_path / 'memory.sqlite')
    monkeypatch.setattr(m, '_embed_one', lambda q: None)
    monkeypatch.setattr(m, '_fact_by_anchor', lambda *a: '')
    conn = m._connect()
    def add(book, text, superseded=None):
        cur = conn.execute('INSERT INTO memory(role,book,anchor,digest,vector,created,superseded_by) VALUES(?,?,?,?,?,?,?)',
                           ('角色', m._norm_root(book), 'anchor', text, struct.pack('ff', 1., 0.), 1, superseded))
        m._index_side_tables(conn, cur.lastrowid, text)
        conn.commit()
    yield conn, add
    conn.close()


def test_keyword_scope_and_superseded(db):
    conn, add = db
    for _ in range(20):
        add('/other', '顾明怕水')
    add('/book', '顾明怕水')
    add('', '顾明怕水')
    add('/book', '顾明怕水', 999)
    got = m.recall('/book', '顾明')
    assert got['mode'] == 'keyword' and got['degraded']
    assert len(got['条']) == 2
    assert {r['book'] for r in got['条']} == {'/book', ''}
    assert got['channels']['keyword']['status'] == 'executed'
    assert len(m.recall('/book', '顾明', scope='all', k=50)['条']) == 15


def test_zero_hits_empty_and_recovery(db, monkeypatch):
    conn, add = db
    add('/book', '顾明怕水')
    zero = m.recall('/book', '不存在')
    assert zero['ok'] and zero['mode'] == 'keyword' and zero['条'] == []
    empty = m.recall('/book', ' ')
    assert empty['mode'] == 'empty_query' and not empty['degraded'] and not empty['notice']
    np = pytest.importorskip('numpy')
    monkeypatch.setattr(m, '_embed_one', lambda q: np.array([1., 0.]))
    normal = m.recall('/book', '顾明')
    assert normal['mode'] == 'hybrid' and not normal['notice']
    assert m.retrieval_event(normal, scope='book')['degraded'] is False


def test_channels_both_failed(db, monkeypatch):
    conn, add = db
    add('/book', '「顾明」怕水')
    real = m._connect
    def broken():
        c = real()
        c.execute('DROP TABLE memory_fts')
        c.execute('DROP TABLE entity_index')
        c.commit()
        return c
    monkeypatch.setattr(m, '_connect', broken)
    got = m.recall('/book', '顾明')
    assert not got['ok'] and got['mode'] == 'unavailable'
    assert got['channels']['keyword']['status'] == 'failed'
    assert got['channels']['entity']['status'] == 'failed'
    assert '已关键词兜底' not in got['notice']


def test_entity_only_and_vector_exception(db, monkeypatch):
    conn, add = db
    add('/book', '「顾明」怕水')
    real = m._connect
    def no_fts():
        c = real()
        c.execute('DROP TABLE memory_fts')
        return c
    monkeypatch.setattr(m, '_connect', no_fts)
    def fail(q):
        raise RuntimeError('model failure')
    monkeypatch.setattr(m, '_embed_one', fail)
    got = m.recall('/book', '顾明')
    assert got['mode'] == 'keyword' and len(got['条']) == 1
    assert got['channels']['entity']['status'] == 'executed'
    assert got['channels']['vector']['status'] == 'failed'


def test_study_notice_and_event_same_result(db):
    from loom.study.ask import _auto_memory
    events = []
    text = _auto_memory('书房伙伴', '顾明', '/book', events.append)
    assert len(events) == 1 and events[0]['origin'] == 'automatic'
    assert events[0]['scope'] == 'all'
    assert events[0]['notice'] in text


def test_database_failure_is_observable(monkeypatch):
    def broken():
        raise sqlite3.OperationalError('private disk detail')
    monkeypatch.setattr(m, '_embed_one', lambda q: None)
    monkeypatch.setattr(m, '_connect', broken)
    got = m.recall('/book', '顾明')
    assert got['mode'] == 'unavailable' and not got['ok']
    assert 'private disk' not in got['notice']


def test_cross_book_original_uses_hit_root(db, monkeypatch):
    conn, add = db
    add('/other', '顾明怕水')
    seen = []
    monkeypatch.setattr(m, '_fact_by_anchor', lambda root, *a: seen.append(str(root).replace('\\', '/')) or '原文')
    got = m.recall('/book', '顾明', scope='all')
    assert got['条'][0]['原文'] == '原文'
    assert seen == ['/other']


@pytest.mark.parametrize('role', ['', '知识顾问'])
def test_actual_model_system_and_stream(db, monkeypatch, role):
    from loom.study import ask as a
    monkeypatch.setattr(a, 'search', lambda q: {'节选': [], '统计': {'段': 0, '词': 0, '给了': 0, '截断': False}})
    monkeypatch.setattr(a, 'read_current', lambda v: None)
    calls = []
    class Backend:
        def complete(self, system, user, on_chunk=None, **kwargs):
            calls.append((system, user))
            return '答复'
    events = []
    a.ask('顾明', events.append, backend=Backend(), role=role)
    retrieved = [e for e in events if e['t'] == 'memory_retrieval']
    assert len(retrieved) == 1 and calls
    assert retrieved[0]['notice'] in calls[0][0]
    assert retrieved[0]['mode'] == 'keyword'
