# -*- coding: utf-8 -*-
"""最近读过＋综合搜索（0.8.3，、2、3条）。

守四件：
1.最近读过只留前三本、同一册不重复；
2.搜索只接一个短句，不开放详细参数；
3.笔记要能被综合搜索找到（不单开入口）；
4.空词返回空，不报错。
"""
from __future__ import annotations

from loom.study import notes as N
from loom.study import recent as R


def test_recent_keeps_three_and_dedups():
    R._file().unlink(missing_ok=True)
    for k in range(5):
        R.touch('E:/t/r%d.md' % k, '书%d' % k, '教材', '01')
    items = R.load()
    assert len(items) ==3
    assert items[0]['名'] == '书4' #最新的在最前
    #同一册读两次只留一条
    R.touch('E:/t/r3.md', '书3', '教材', '01')
    assert len(R.load()) ==3
    assert R.load()[0]['名'] == '书3'
    R._file().unlink(missing_ok=True)


def test_recent_survives_missing_file():
    R._file().unlink(missing_ok=True)
    assert R.load() == []


def test_find_endpoint_shape():
    from fastapi.testclient import TestClient
    import loom.server as S
    c = TestClient(S.app, base_url='http://127.0.0.1:8611')
    r = c.get('/api/study/find', params={'q': ''})
    assert r.status_code ==200
    assert r.json()['命中'] == []


def test_find_returns_books_and_notes():
    from fastapi.testclient import TestClient
    import loom.server as S
    p = 'E:/t/find-book.md'
    for it in list(N.load(p)['笔记']):
        N.remove(p, it['id'])
    N.add(p, '某册', 0, '这里写了一句独一无二的话——鲸鱼回游')
    c = TestClient(S.app, base_url='http://127.0.0.1:8611')
    r = c.get('/api/study/find', params={'q': '鲸鱼回游'})
    hits = r.json()['命中']
    assert any(h['类型'] == '笔记' for h in hits), [h['类型'] for h in hits]
    for it in list(N.load(p)['笔记']):
        N.remove(p, it['id'])


def test_recent_endpoint():
    from fastapi.testclient import TestClient
    import loom.server as S
    c = TestClient(S.app, base_url='http://127.0.0.1:8611')
    r = c.get('/api/study/recent')
    assert r.status_code ==200
    assert isinstance(r.json()['书'], list)
