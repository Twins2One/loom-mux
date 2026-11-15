# -*- coding: utf-8 -*-
"""文风档案新落点 + 两个新接口）。

要守住的三件事：
1. 档案住在 `<知识库>/语料库/<档案名>/{原文,蒸馏原稿,写手参考.md}`，**不再跟书走**；
2. `GET /api/style/archives` 只报份数、不带正文；**盘上空就返回 []，不造假数据**；
3. `GET /api/partner/pending` 跨通道统计、已拍板的不算、拍板后就减。
"""
from __future__ import annotations

import json

import pytest

from loom import partner_store as ps, stylepack as sp


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "知识库"
    (root / sp.LIBRARY_SUBDIR).mkdir(parents=True)
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(root))
    return root


# ── 落点 ──────────────────────────────────────────────────────────────────
def test_档案落在知识库语料库而不是书里(project, lib):
    p = sp.pack_dir(project, "主档案")
    assert str(lib / sp.LIBRARY_SUBDIR / "主档案") == str(p)


def test_三层目录名与前端规格一致():
    assert (sp.DOCS_SUB, sp.DISTILLED_SUB, sp.ARCHIVE_FILE) == ("原文", "蒸馏原稿", "写手参考.md")
    assert sp.STATUS_FILE.startswith("_")      # 元数据对界面隐藏


def test_加一篇文档后能列出来(project, lib):
    out = sp.add_doc(project, "主档案", "样本一", "他站在门口。风从走廊里灌进来。")
    assert out.get("ok"), out
    libs = sp.list_library(project, "主档案")
    assert libs["篇数"] == 1
    assert (lib / sp.LIBRARY_SUBDIR / "主档案" / sp.DOCS_SUB).is_dir()


def test_老落点有档案时仍然读得到(project, lib):
    """兼容：万一某本书里已经有 `<书>/style-packs/`，别让它失明。"""
    old = project / sp.PACKS_REL / "旧档案"
    (old / sp.DOCS_SUB).mkdir(parents=True)
    (old / sp.DOCS_SUB / "老样本.md").write_text("老样本正文。", encoding="utf-8")
    assert sp.pack_dir(project, "旧档案") == old
    names = [x["名字"] for x in sp.list_packs(project)]
    assert "旧档案" in names


# ── /api/style/archives ───────────────────────────────────────────────────
def _client():
    from fastapi.testclient import TestClient
    from loom import server
    return TestClient(server.app, base_url="http://127.0.0.1:8611")


def test_档案接口空盘时返回空列表(project, lib):
    r = _client().get("/api/style/archives", params={"root": str(project)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["archives"] == []
    assert "语料库" in body["root"]


def test_档案接口报三层份数(project, lib):
    sp.add_doc(project, "主档案", "样本一", "他站在门口。风从走廊里灌进来。")
    sp.add_doc(project, "主档案", "样本二", "他没有回头，只是把手里的伞收了起来。")
    writer = lib / sp.LIBRARY_SUBDIR / "主档案" / sp.ARCHIVE_FILE
    writer.write_text("# 写手参考\n- 短句为主\n", encoding="utf-8")
    r = _client().get("/api/style/archives", params={"root": str(project)})
    body = r.json()
    assert len(body["archives"]) == 1
    a = body["archives"][0]
    assert a["name"] == "主档案"
    assert a["layer_counts"]["raw"] == 2
    assert a["has_writer"] is True and a["layer_counts"]["writer"] == 1


# ── /api/style/library + /api/style/file（2026-10-27 书房壳三层要读正文）────
def _archive_with_layers(lib):
    base = lib / sp.LIBRARY_SUBDIR / "主档案"
    (base / sp.DOCS_SUB).mkdir(parents=True, exist_ok=True)
    (base / sp.DISTILLED_SUB).mkdir(parents=True, exist_ok=True)
    (base / sp.DOCS_SUB / "样本一.md").write_text("他站在门口。风从走廊里灌进来。", encoding="utf-8")
    (base / sp.DISTILLED_SUB / "样本一.md").write_text("# 分析\n证据：短句。", encoding="utf-8")
    (base / sp.ARCHIVE_FILE).write_text("# 写手参考\n- 短句为主\n", encoding="utf-8")
    return base


def test_档案细目接口(project, lib):
    _archive_with_layers(lib)
    r = _client().get("/api/style/library", params={"name": "主档案"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["档案"] == "主档案"
    assert body["篇数"] == 1 and body["有档案"] is True
    assert body["文档"][0]["标题"] == "样本一" and body["文档"][0]["已蒸馏"] is True


def test_读第三层写手参考(project, lib):
    _archive_with_layers(lib)
    body = _client().get("/api/style/file",
                         params={"name": "主档案", "layer": "写手参考"}).json()
    assert body["ok"] is True and "短句为主" in body["text"]


def test_读第一二层按篇读(project, lib):
    _archive_with_layers(lib)
    b1 = _client().get("/api/style/file",
                       params={"name": "主档案", "layer": "原文", "file": "样本一.md"}).json()
    assert b1["ok"] is True and "风从走廊" in b1["text"]
    b2 = _client().get("/api/style/file",
                       params={"name": "主档案", "layer": "蒸馏原稿", "file": "样本一.md"}).json()
    assert b2["ok"] is True and "证据" in b2["text"]


@pytest.mark.parametrize("kw", [
    {"layer": "原文", "file": "../写手参考.md"},       # 带目录 → 拒
    {"layer": "原文", "file": "样本一.txt"},           # 不是 .md → 拒
    {"layer": "原文", "file": ""},                    # 空 → 拒
    {"layer": "蒸馏原稿", "file": "_状态.json"},        # 元数据不外泄
    {"layer": "随便"},                                # 层名不认 → 拒
])
def test_读档案越界一律拒(project, lib, kw):
    _archive_with_layers(lib)
    params = {"name": "主档案", **kw}
    body = _client().get("/api/style/file", params=params).json()
    assert body["ok"] is False and body["error"], body


def test_名字带路径分隔符时拒绝(project, lib):
    _archive_with_layers(lib)
    body = _client().get("/api/style/file",
                         params={"name": "../etc", "layer": "写手参考"}).json()
    assert body["ok"] is False


def test_超大文件不进文档列表(project, lib):
    """列表与读取用同一把尺：读不动的文件不该出现在列表里让人点（对方复核提出）。"""
    base = _archive_with_layers(lib)
    (base / sp.DOCS_SUB / "超大.md").write_bytes(b"x" * (sp.MAX_DOC_BYTES + 1024))
    body = _client().get("/api/style/library", params={"name": "主档案"}).json()
    assert "超大" not in [d["标题"] for d in body["文档"]]
    assert body["超大跳过"] == 1 and body["篇数"] == 1
    # 列表里没有的东西，读也读不到（同一把尺，不是两处各写一个数）
    f = _client().get("/api/style/file",
                      params={"name": "主档案", "layer": "原文", "file": "超大.md"}).json()
    assert f["ok"] is False


def test_不存在的档案与建了但空的档案可区分(project, lib):
    """两者原来长得一模一样，界面只能说"还没有档案"，用户会以为是自己建过只是空的。"""
    none = _client().get("/api/style/library", params={"name": "不存在的档案"}).json()
    assert none["ok"] is True and none["存在"] is False and none["篇数"] == 0
    (lib / sp.LIBRARY_SUBDIR / "空档案").mkdir(parents=True)
    empty = _client().get("/api/style/library", params={"name": "空档案"}).json()
    assert empty["存在"] is True and empty["篇数"] == 0 and empty["有档案"] is False


def test_超大文本加不进来():
    big = "字" * (sp.MAX_DOC_BYTES // 3 + 200)      # 中文 3 字节/字 → 超 2 MB
    out = sp.add_doc(None, "主档案", "整本", big)
    assert out["ok"] is False and out["code"] == "too_big"
    assert "分段" in out["reason"]


# ── 写接口在「书房没有当前书」时也要能用语料分析师页）──
def test_不传root也能喂样本(lib):
    c = _client()
    r = c.post("/api/style/raw", json={"name": "主档案", "filename": "样本A", "text": "他站在门口。"})
    body = r.json()
    assert body.get("ok") is True, body
    assert body["库"]["篇数"] == 1
    got = c.get("/api/style/library", params={"name": "主档案"}).json()
    assert got["存在"] is True and got["篇数"] == 1


def test_不传root也能删样本(lib):
    c = _client()
    c.post("/api/style/raw", json={"name": "主档案", "filename": "样本B", "text": "风从走廊里灌进来。"})
    r = c.post("/api/style/delete", json={"name": "主档案", "title": "样本B"})
    assert r.status_code == 200
    assert c.get("/api/style/library", params={"name": "主档案"}).json()["篇数"] == 0


def test_不传root时文件名里的路径分隔符仍被拒(lib):
    """既定的错误口径：坏名字走 400 + {error:"人话"}，不是 200+ok:false。"""
    r = _client().post("/api/style/raw",
                       json={"name": "../etc", "filename": "x", "text": "正文"})
    assert r.status_code == 400, r.text
    assert r.json().get("error")


class _FakeBackend:
    """蒸馏用的假后端：回一段带证据/特征/置信度的原文，够 _distill_one 收下。"""

    def complete(self, system, user, on_chunk=None, **kw):
        return ("## 特征\n- 句子短促，动词优先\n\n"
                "## 证据\n- 「他站在门口。」（样本一）\n\n"
                "## 置信度\n- 中\n")


def test_不传root也能启动蒸馏(lib, monkeypatch):
    """作者 2026-10-27 晚裁「蒸馏开放」：书房没有当前书也要能蒸。"""
    sp.add_doc(None, "主档案", "样本一", "他站在门口。风从走廊里灌进来。")
    monkeypatch.setattr("loom.routes.get_backend_for", lambda cfg, slot="": _FakeBackend())
    r = _client().post("/api/style/distill",
                       json={"name": "主档案", "docs": ["样本一"], "detail": "中"})
    assert r.status_code != 400, r.text
    body = r.json()
    assert body.get("code") != "not_a_book", body
    # 蒸馏原稿落不落盘取决于输出校验；这里只验"没被书上下文挡住"这条通路
    assert "ok" in body or "code" in body


# ── /api/partner/pending ──────────────────────────────────────────────────
def _proposal(project, pid, *, content="重生流", role=""):
    ev = {"t": "proposal", "id": pid, "slot": "外置大脑/设定集/00-立项.md#题材", "content": content}
    if role:
        ev["角色"] = role
    ps.append_event(project, ev)


def test_待拍板接口能数出跨通道的卡(project):
    _proposal(project, "p-1", content="重生流")
    p = project / ".伙伴对话" / "agent-大纲师.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"t": "proposal", "id": "p-2", "content": "废土流",
                             "角色": "大纲师"}, ensure_ascii=False) + "\n", encoding="utf-8")
    r = _client().get("/api/partner/pending", params={"root": str(project)})
    body = r.json()
    assert body["count"] == 2, body
    assert {i["id"] for i in body["items"]} == {"p-1", "p-2"}
    assert all(i["摘要"] for i in body["items"])


def test_拍过板的卡不再计入(project):
    _proposal(project, "p-1")
    ps.append_event(project, {"t": "confirm", "id": "p-1", "landed": "x"})
    body = _client().get("/api/partner/pending", params={"root": str(project)}).json()
    assert body["count"] == 0 and body["items"] == []


def test_摘要把Markdown记号剥掉再截(project):
    """角标卡片只放一行字，`##`/`**`/空行不该跟着进摘要（对方复核提出）。"""
    _proposal(project, "p-md",
              content="## 卷一情感线（三轴·六段）\n\n### 一、总口径\n卷一的情感线不是恋爱线，是**双线**并置。")
    item = _client().get("/api/partner/pending", params={"root": str(project)}).json()["items"][0]
    s = item["摘要"]
    assert "#" not in s and "**" not in s, s
    assert "\n" not in s, s
    assert s.startswith("卷一情感线（三轴·六段）"), s
    assert len(s) <= 41, s          # 40 字 + 一个省略号


def test_摘要短文本原样保留不加省略号(project):
    _proposal(project, "p-short", content="重生流")
    item = _client().get("/api/partner/pending", params={"root": str(project)}).json()["items"][0]
    assert item["摘要"] == "重生流"


def test_摘要剥引用记号与分隔线(project):
    """对方复核的 nit：`>` 引用记号会露在句中（`---` 分隔线同理）。"""
    _proposal(project, "p-quote",
              content="## 卷一卷纲 · 节奏修订\n\n---\n\n> 本文件覆盖《卷纲·第一卷》的六段推进\n> > 嵌套引用也要剥\n\n正文从这一句开始。")
    item = _client().get("/api/partner/pending", params={"root": str(project)}).json()["items"][0]
    s = item["摘要"]
    assert ">" not in s, s
    assert "---" not in s, s
    assert "\n" not in s and "#" not in s, s
    assert s.startswith("卷一卷纲 · 节奏修订"), s


def test_摘要不碰正文里的正常大于号(project):
    """`3 > 2` 不是引用记号，一个字都不许动。"""
    from loom import partner_store as _ps
    assert _ps.brief("条件是 3 > 2 且 a<b") == "条件是 3 > 2 且 a<b"


def test_没卡时返回零而不是报错(project):
    body = _client().get("/api/partner/pending", params={"root": str(project)}).json()
    assert body["ok"] is True and body["count"] == 0
    # 真书也要带 `存在`（只在不存在的分支给 false，会让 `if (j.存在)` 的写法把真书判假）
    assert body["存在"] is True


def test_汇总查询也带存在键(project):
    body = _client().get("/api/partner/pending").json()
    assert body["ok"] is True and body["存在"] is True


def test_传一本不存在的书时返回零而不是400(tmp_path):
    """2026-10-27 晚：显式传死书根不该是客户端错误——书没了就是没卡，但要给一句人话。"""
    r = _client().get("/api/partner/pending", params={"root": str(tmp_path / "没有这本书")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["count"] == 0 and body["items"] == []
    assert body["存在"] is False
    assert "没找到" in body["warning"]


def test_传一个非书目录时也不报400(tmp_path):
    """目录存在但没有 loom.toml：同样按"没有这本书"处理，不 400。"""
    d = tmp_path / "普通文件夹"
    d.mkdir()
    r = _client().get("/api/partner/pending", params={"root": str(d)})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0


# ── root 可选（2026-10-27 对方复核提出：书房首页没有"当前书"）───────────────
def test_档案接口不传root也能列(project, lib):
    """档案跨书共享 ⇒ 不传 root 也照样能列（只看知识库那一侧）。"""
    sp.add_doc(project, "主档案", "样本一", "他站在门口。")
    r = _client().get("/api/style/archives")
    assert r.status_code == 200, r.text
    assert len(r.json()["archives"]) == 1


def test_待拍板接口不传root时汇总书架上的书(project):
    """不传 root → 汇总书架上所有活着的书；每条 item 带 root（拍板时要用）。"""
    from loom import userconf

    _proposal(project, "p-abc", content="重生流")
    userconf.record_project(project, "测试书")
    r = _client().get("/api/partner/pending")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1, body
    assert body["items"][0]["root"] == str(project)
