"""首次启动的旧知识库迁移提示（作者 2026-11-11「两者都做」的 (a) 半边）。

钉住作者列的六条要求：
1. 三个条件同时成立才提示；2. 不自动搬数据；3. 给出提示体；4. 检测失败不阻塞启动；
5. 说"不用了"之后不再提示；6. 旧位置是候选列表而非单值。
加一条：新旧路径相同或互为父子 ⇒ 判为不需要迁移。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom import libroot


@pytest.fixture
def clean(tmp_path, monkeypatch):
    """摘掉 env（否则恒走 env 层，默认/检测那条路根本测不到），隔离 LOOM_HOME。"""
    monkeypatch.delenv("LOOM_LIBRARY_ROOT", raising=False)
    home = tmp_path / "loom_home"
    home.mkdir()
    monkeypatch.setenv("LOOM_HOME", str(home))
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (), raising=False)
    return home


def _old_lib(tmp_path: Path, name: str = "旧库") -> Path:
    p = tmp_path / name
    (p / "教材" / "夜课").mkdir(parents=True)
    (p / "教材" / "夜课" / "第01册.md").write_text("# 一\n", encoding="utf-8")
    return p


def _cfg(home: Path) -> dict:
    p = home / "library.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


# ── 该提示 / 不该提示 ────────────────────────────────────

def test_three_conditions_met_prompts(clean, tmp_path, monkeypatch):
    """从未配置 + 旧库有内容 + 新默认空 ⇒ 提示，且问句里带旧路径。"""
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))

    r = libroot.detect_migration()
    assert r["code"] == "library_migration_available"
    assert r["old_root"] == str(old)
    assert r["new_root"] == str(clean / "知识库")
    assert r["可迁移"] is True
    assert str(old) in r["问"]
    assert "迁移" in r["问"]
    assert r["候选"] == [str(old)]
    # 只检测，不动数据
    assert (old / "教材" / "夜课" / "第01册.md").is_file()
    assert not (clean / "知识库").exists()
    assert _cfg(clean) == {}


def test_no_prompt_when_already_configured(clean, tmp_path, monkeypatch):
    """配过知识库位置就不再提示（那是用户已经做过选择）。"""
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))
    libroot._save_config({"root": str(tmp_path / "已配置的库")})

    r = libroot.detect_migration()
    assert r["code"] == "none"
    assert "已配置" in r["原因"]


def test_no_prompt_when_new_default_has_content(clean, tmp_path, monkeypatch):
    """新默认位置已经有东西了 ⇒ 不提示（再迁就把新内容盖了）。"""
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))
    new = clean / "知识库"
    new.mkdir()
    (new / "已经在这.md").write_text("x\n", encoding="utf-8")

    r = libroot.detect_migration()
    assert r["code"] == "none"
    assert "已有内容" in r["原因"]


def test_new_default_empty_dir_still_prompts(clean, tmp_path, monkeypatch):
    """新默认只是空目录不算"有内容"——空壳不该挡住迁移。"""
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))
    (clean / "知识库").mkdir()

    assert libroot.detect_migration()["code"] == "library_migration_available"


def test_no_prompt_when_old_missing_or_empty(clean, tmp_path, monkeypatch):
    """旧候选不存在、或只是空目录 ⇒ 没有可迁移项（空目录不算"有内容"）。"""
    empty_old = tmp_path / "空的旧库"
    empty_old.mkdir()
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES",
                        (str(tmp_path / "根本没有"), str(empty_old)))

    r = libroot.detect_migration()
    assert r["code"] == "none"
    assert "没有发现" in r["原因"]


def test_no_prompt_when_env_pins_root(clean, tmp_path, monkeypatch):
    """env 指定了位置就别插手（跟解析优先级保持一致）。"""
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(tmp_path / "env库"))

    r = libroot.detect_migration()
    assert r["code"] == "none"
    assert "环境变量" in r["原因"]


def test_same_or_nested_paths_never_prompt(clean, tmp_path, monkeypatch):
    """需求方特别点的那条：新旧路径相同或互为父子 ⇒ 判为不需要迁移。

    这里让候选 = LOOM_HOME 本身（新默认 `知识库/` 的父目录），并给它塞一个文件，
    保证"有内容"这一关是通过的——于是被挡住的只能是父子重叠这一关，
    而不是被"没内容"顺手挡掉（那样测了等于没测）。
    """
    (clean / "某个文件.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(clean),))
    assert libroot._has_content(clean) is True          # 前提成立，走到重叠判定
    assert libroot.detect_migration()["code"] == "none"

    # 同一目录也一样
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(clean / "知识库"),))
    assert libroot.detect_migration()["code"] == "none"


# ── 说"不用了"要记住 ────────────────────────────────────

def test_dismiss_is_remembered_and_silences_prompt(clean, tmp_path, monkeypatch):
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))
    assert libroot.detect_migration()["code"] == "library_migration_available"

    d = libroot.dismiss_migration(str(old))
    assert d["ok"] is True
    assert libroot.detect_migration()["code"] == "none"

    saved = _cfg(clean)
    assert saved["migration_dismissed"][0]["old_root"] == str(old)
    assert isinstance(saved["migration_dismissed"][0]["at"], int)


def test_dismiss_does_not_wipe_other_config(clean, tmp_path, monkeypatch):
    """记"不用了"不能顺手把 root / previous / 迁移时间抹掉。"""
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))
    libroot._save_config({"root": str(tmp_path / "甲"), "previous": str(tmp_path / "乙"),
                          "migrated_at": 999})

    libroot.dismiss_migration(str(old))
    saved = _cfg(clean)
    assert saved["root"] == str(tmp_path / "甲")
    assert saved["previous"] == str(tmp_path / "乙")
    assert saved["migrated_at"] == 999


def test_dismiss_is_idempotent(clean, tmp_path):
    libroot.dismiss_migration("E:\\Book\\知识库")
    libroot.dismiss_migration("E:\\Book\\知识库")
    assert len(_cfg(clean)["migration_dismissed"]) == 1


def test_dismiss_other_candidate_still_prompts(clean, tmp_path, monkeypatch):
    """只记住说过"不用了"的那个候选；日后别的历史位置出现了仍会提。"""
    a = _old_lib(tmp_path, "旧库甲")
    b = _old_lib(tmp_path, "旧库乙")
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(a), str(b)))
    libroot.dismiss_migration(str(a))

    r = libroot.detect_migration()
    assert r["code"] == "library_migration_available"
    assert r["old_root"] == str(b)


def test_dismiss_without_root_is_refused(clean):
    r = libroot.dismiss_migration("   ")
    assert r["ok"] is False
    assert "没给旧位置" in r["结果"]


# ── 不许阻塞启动 / 候选可扩展 ────────────────────────────

def test_detect_never_raises_even_if_plumbing_breaks(clean, monkeypatch):
    """检测内部炸了也必须返回 code=none，绝不把异常抛给启动路径。"""
    def boom():
        raise RuntimeError("模拟底座异常")

    monkeypatch.setattr(libroot, "default_root", boom)
    r = libroot.detect_migration()
    assert r["code"] == "none"
    assert "检测失败" in r["原因"]


def test_legacy_candidates_is_an_extensible_list():
    """旧位置必须是候选列表而不是硬编码单值（作者明确要求便于以后追加）。"""
    assert isinstance(libroot.LEGACY_CANDIDATES, tuple)
    assert len(libroot.LEGACY_CANDIDATES) >= 1
    assert all(isinstance(x, str) for x in libroot.LEGACY_CANDIDATES)


# ── 首次启动真的走通整条路（前端问的第 4 点暴露出来的缺陷）────────────

def test_first_run_migration_must_pass_old_root_as_source(clean, tmp_path, monkeypatch):
    """首次启动迁移：**源必须显式传探测到的 old_root**。

    这是契约里原本的洞：不传 source 时 migrate 默认拿"当前知识库根"当源，
    而首次启动时它正是那个还空着的新默认位置 ⇒ 直接"源知识库不存在"。
    前端一问才发现——光跑 GET 检测看不出这个。
    """
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))
    new_root = libroot.default_root()

    det = libroot.detect_migration()
    assert det["code"] == "library_migration_available"

    # 反面：不传源 → 源默认取"当前知识库根"，而首次启动时它就是目标那个空位置。
    # 实测这会被"源与目标不能是同一个目录"先拦下（同一路径分支比"源不存在"更早命中）。
    wrong = libroot.migrate(new_root)
    assert wrong["ok"] is False
    assert wrong["源"] == str(new_root)      # 源确实指错了地方
    assert not (new_root / "教材").exists()

    # 正面：传探测到的 old_root → 整条路走通
    right = libroot.migrate(new_root, source_root=det["old_root"])
    assert right["ok"] is True, right
    assert right["校验"]["通过"] is True
    assert libroot.library_root() == new_root
    assert (new_root / "教材" / "夜课" / "第01册.md").is_file()
    assert old.is_dir()          # 源不动
    # 迁移完就不该再提示
    assert libroot.detect_migration()["code"] == "none"


def test_old_root_is_always_the_first_candidate(clean, tmp_path, monkeypatch):
    """前端问的第 3 点：`old_root` 必须恒等于 `候选[0]`，问句也用它拼。

    这样"默认选中项"和文案永远同源，不会出现"说 A、默认选 B"。
    """
    a = _old_lib(tmp_path, "旧库甲")
    b = _old_lib(tmp_path, "旧库乙")
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(a), str(b)))

    r = libroot.detect_migration()
    assert r["候选"] == [str(a), str(b)]
    assert r["old_root"] == r["候选"][0]
    assert r["old_root"] in r["问"]


def test_dismiss_accepts_the_actually_chosen_candidate(clean, tmp_path, monkeypatch):
    """前端问的第 4 点：dismiss 传"用户实际选中的那个候选"，不是死传 old_root。

    选了 B 点「不用了」→ B 闭嘴、A 仍会提；若此时误传 old_root(A)，
    作者下次启动还会被同一个 B 再问一遍。
    """
    a = _old_lib(tmp_path, "旧库甲")
    b = _old_lib(tmp_path, "旧库乙")
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(a), str(b)))
    assert libroot.detect_migration()["old_root"] == str(a)

    libroot.dismiss_migration(str(b))          # 用户选的是 B
    r = libroot.detect_migration()
    assert r["code"] == "library_migration_available"
    assert r["old_root"] == str(a)             # A 还在提
    assert str(b) not in r["候选"]             # B 已闭嘴


# ── 并发：第二个直接拒，不并发复制 ──────────────────────

def test_second_migrate_is_refused_while_one_is_running(clean, tmp_path, monkeypatch):
    """前端问的第 2 点，兜底语义必须是"直接拒"而不是"并发复制同一目标"。

    做法：抢在别人持锁时调 migrate，应当立刻返回失败且**不产生任何目录**。
    """
    src = _old_lib(tmp_path)
    assert libroot._SWITCH_LOCK.acquire(blocking=False)      # 模拟"迁移进行中"
    try:
        r = libroot.migrate(tmp_path / "新库", source_root=src)
        assert r["ok"] is False
        assert "正在进行" in r["结果"]
        assert not (tmp_path / "新库").exists()
        assert not list(tmp_path.glob(".新库.迁移中-*"))
    finally:
        libroot._SWITCH_LOCK.release()

    # 锁一放，正常迁移照旧能做
    assert libroot.migrate(tmp_path / "新库", source_root=src)["ok"] is True


def test_repoint_and_revert_are_refused_while_migrating(clean, tmp_path):
    """搬运途中不许改指针，否则搬完不知道该落到谁身上。"""
    libroot._save_config({"root": str(tmp_path / "甲")})
    assert libroot._SWITCH_LOCK.acquire(blocking=False)
    try:
        assert libroot.repoint(tmp_path)["ok"] is False
        assert libroot.revert()["ok"] is False
        assert "正在进行" in libroot.repoint(tmp_path)["结果"]
    finally:
        libroot._SWITCH_LOCK.release()


# ── source 不传必须明确报错（需求方追加要求）──────────────

def test_migrate_without_source_says_so_explicitly(clean, tmp_path):
    """不传源时不能默默拿"当前知识库根"顶上——错误里必须点破根因。

    首次启动时"当前根"= 还空着的新默认位置，跟目标同一路径。旧实现会报
    "源与目标不能是同一个目录"，看不出该传什么；现在必须直说"未指定迁移源（source）"。
    """
    new_root = libroot.default_root()
    r = libroot.migrate(new_root)          # 故意不传 source_root
    assert r["ok"] is False
    assert "未指定迁移源" in r["结果"]
    assert "source" in r["结果"]
    assert not (new_root / "教材").exists()


def test_migrate_without_source_when_current_root_is_missing(clean, tmp_path):
    """当前根不存在（老用户首次升级就是这形态）时同样要直说，而不是"源知识库不存在"。"""
    libroot._save_config({"root": str(tmp_path / "配了但还没建的库")})
    r = libroot.migrate(tmp_path / "新库")
    assert r["ok"] is False
    assert "未指定迁移源" in r["结果"]
    assert "不存在" in r["结果"]


def test_api_requires_source_for_migrate(clean, tmp_path):
    """API 层同样必填：migrate=true 且 source 为空 → 明确拒绝，且完全不动盘。"""
    from loom.api import projects as pj

    body = pj.LibraryRootBody(root=str(tmp_path / "新库"), migrate=True, source="")
    r = pj.library_root_set(body)
    assert r["ok"] is False
    assert "迁移必须指定源目录" in r["结果"]
    assert "migrate:false" in r["结果"]        # 顺带告诉对方该走哪条路
    assert not (tmp_path / "新库").exists()


def test_repoint_warns_when_target_is_empty(clean, tmp_path):
    """切到一个空目录不能静默成功——界面马上会变成"没有知识库"，得当场提醒。"""
    empty = tmp_path / "空目录"
    empty.mkdir()
    r = libroot.repoint(empty)
    assert r["ok"] is True
    assert "空" in r["提醒"]
    assert "迁移" in r["提醒"]

    # 有内容的目录不该乱提醒
    full = _old_lib(tmp_path, "有货的库")
    assert "提醒" not in libroot.repoint(full)


# ── 新位置是否存在（2026-11-16 独立复审 P2-3 的修法）────────────────────

def test_detect_reports_target_existence_when_absent(clean, tmp_path, monkeypatch):
    """新默认位置不存在时：仍然报可迁移，但要**如实带出「新位置存在」= False**。

    前端据此把「用新位置」禁用（repoint 要求目标已存在，否则那个按钮点了必然失败）。
    repoint 的严格契约不变——自动建目录会让打错的路径凭空长出文件夹。
    """
    old = _old_lib(tmp_path)
    monkeypatch.setattr(libroot, "LEGACY_CANDIDATES", (str(old),))

    r = libroot.detect_migration()
    assert r["code"] == "library_migration_available"
    assert r["新位置存在"] is False          # 还没建
    assert not Path(r["new_root"]).exists()
    # 后端照样拒绝把指针指过去
    assert libroot.repoint(r["new_root"])["ok"] is False


def test_detect_reports_target_existence_when_present(clean, monkeypatch):
    """新默认位置已存在（空目录）时：标记为 True，前端就不会禁用那个按钮。"""
    (clean / "知识库").mkdir()
    r = libroot.detect_migration()
    # 没有旧库候选 ⇒ code=none，但「新位置存在」照样要如实回传
    assert r["新位置存在"] is True
