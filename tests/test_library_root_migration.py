"""知识库默认落点 + 迁移（作者 2026-11-11 裁定）。

要点逐条钉住：
- 默认不再硬编码 `E:\\Book\\知识库`，落在 Loom 根目录（LOOM_HOME）下的 `知识库/`；
- `LOOM_LIBRARY_ROOT` 仍然最优先（测试隔离全靠它）；
- 三处消费方（study/library、stylepack、guides）同口径，不再各写一份默认值；
- 迁移 = 复制 + 逐文件校验 + 切换；校验不过不改指针；源目录始终不动；可回退。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from loom import libroot


@pytest.fixture
def clean(tmp_path, monkeypatch):
    """清掉环境变量，让"默认/配置"两条路能真正被测到。

    conftest 的 autouse 夹具把 LOOM_LIBRARY_ROOT 指到 tmp；这里必须摘掉，
    否则永远只测到 env 那一层，配置与默认两路等于没测。
    """
    monkeypatch.delenv("LOOM_LIBRARY_ROOT", raising=False)
    home = tmp_path / "loom_home"
    home.mkdir()
    monkeypatch.setenv("LOOM_HOME", str(home))
    return home


def _seed(base: Path) -> None:
    """铺一个像样的知识库：带分类目录、下划线元数据、空目录、嵌套文件。"""
    (base / "教材" / "夜课").mkdir(parents=True)
    (base / "教材" / "夜课" / "第01册.md").write_text("# 一\n正文甲\n", encoding="utf-8")
    (base / "语料库" / "某档案").mkdir(parents=True)
    (base / "语料库" / "某档案" / "写手参考.md").write_text("语气偏冷\n", encoding="utf-8")
    (base / "素材").mkdir()
    (base / "_索引.md").write_text("元数据也要搬\n", encoding="utf-8")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ── 解析口径 ─────────────────────────────────────────────

def test_default_lands_under_loom_home(clean):
    """默认落点 = Loom 根目录/知识库；不存在的目录不算错，只如实说"不存在"。"""
    st = libroot.status()
    assert st["根"] == str(clean / "知识库")
    assert st["来源"] == "默认"
    assert st["存在"] is False
    assert st["可回退"] is False


def test_env_wins_over_config(clean, tmp_path, monkeypatch):
    """LOOM_LIBRARY_ROOT 最优先——测试与临时迁移都靠它压住配置。"""
    libroot._save_config({"root": str(tmp_path / "配置的库")})
    monkeypatch.setenv("LOOM_LIBRARY_ROOT", str(tmp_path / "env库"))
    assert libroot.library_root() == tmp_path / "env库"
    assert libroot.source() == "env"


def test_config_wins_over_default(clean, tmp_path):
    libroot._save_config({"root": str(tmp_path / "配置的库")})
    assert libroot.library_root() == tmp_path / "配置的库"
    assert libroot.source() == "配置"


def test_broken_config_falls_back_to_default(clean):
    """坏 JSON 不能把开书拖死：当没配过，退回默认。"""
    p = clean / "library.json"
    p.write_text("{不是 json", encoding="utf-8")
    assert libroot.load_config() == {}
    assert libroot.library_root() == clean / "知识库"


def test_three_consumers_share_one_root(clean, tmp_path):
    """三处消费方同口径——原先各写一份 `E:\\Book\\知识库`，这次收成一处。"""
    from loom import stylepack
    from loom.study import library as study_library

    libroot._save_config({"root": str(tmp_path / "统一库")})
    assert study_library.library_root() == tmp_path / "统一库"
    assert stylepack.library_root() == tmp_path / "统一库"
    assert stylepack.archives_root() == tmp_path / "统一库" / "语料库"


def test_guides_style_archive_count_uses_libroot(clean, tmp_path):
    """guides 里那处兜底路径也走 libroot（原本写死 E:\\Book\\知识库\\语料库）。"""
    from loom import guides

    root = tmp_path / "guide库"
    (root / "语料库" / "甲档案").mkdir(parents=True)
    (root / "语料库" / "甲档案" / "写手参考.md").write_text("冷\n", encoding="utf-8")
    libroot._save_config({"root": str(root)})

    # user_config_dir()/style 不存在 ⇒ 走兜底分支
    assert guides._style_archive_count() == 1


# ── 迁移 ────────────────────────────────────────────────

def test_migrate_copies_verifies_and_switches(clean, tmp_path):
    """复制 + 逐文件校验 + 切换；源目录原样保留（回退的前提）。"""
    src = tmp_path / "旧库"
    _seed(src)
    libroot._save_config({"root": str(src)})

    dst = tmp_path / "新库"
    r = libroot.migrate(dst)
    assert r["ok"] is True, r
    assert r["校验"]["通过"] is True
    assert r["校验"]["源文件数"] == 3      # 素材/ 是空目录，不计入文件数
    assert r["校验"]["源字节"] == r["校验"]["目标字节"]

    # 内容逐字一致（含下划线元数据、嵌套层级、空目录不算文件）
    for rel in ("教材/夜课/第01册.md", "语料库/某档案/写手参考.md", "_索引.md"):
        assert _sha(dst / rel) == _sha(src / rel)
    assert (dst / "素材").is_dir()

    # 指针已切换，源还在，可回退
    assert libroot.library_root() == dst
    assert src.is_dir()
    assert libroot.status()["回退目标"] == str(src)
    assert libroot.status()["可回退"] is True
    # 暂存目录已就位，不留垃圾
    assert r["暂存"] is None
    assert not list(tmp_path.glob(".新库.迁移中-*"))


def test_migrate_into_existing_empty_dir(clean, tmp_path):
    """目标是个空目录也要能落（rmdir 后改名那一段）。"""
    src = tmp_path / "源"
    _seed(src)
    libroot._save_config({"root": str(src)})
    dst = tmp_path / "空目标"
    dst.mkdir()

    r = libroot.migrate(dst)
    assert r["ok"] is True, r
    assert (dst / "_索引.md").is_file()


def test_migrate_refuses_non_empty_target(clean, tmp_path):
    """既有非空目录拒绝——不拿别人的目录当靶子，且不改指针。"""
    src = tmp_path / "源"
    _seed(src)
    libroot._save_config({"root": str(src)})
    dst = tmp_path / "别人的库"
    dst.mkdir()
    (dst / "别动.md").write_text("x\n", encoding="utf-8")

    r = libroot.migrate(dst)
    assert r["ok"] is False
    assert "拒绝覆盖" in r["结果"]
    assert libroot.library_root() == src
    assert (dst / "别动.md").read_text(encoding="utf-8") == "x\n"


def test_migrate_refuses_nested_targets(clean, tmp_path):
    """互为父子一律拒绝：搬进自己肚子里会无限套娃。"""
    src = tmp_path / "源"
    _seed(src)
    libroot._save_config({"root": str(src)})

    inside = libroot.migrate(src / "子目录")
    assert inside["ok"] is False
    assert "互相包含" in inside["结果"]

    outside = libroot.migrate(tmp_path)      # 目标是源的上层
    assert outside["ok"] is False
    assert "互相包含" in outside["结果"]
    assert libroot.library_root() == src


def test_migrate_refuses_missing_source(clean, tmp_path):
    """没指定源、且"当前根"也不可用 ⇒ 拒绝，并点破"未指定迁移源"。

    2026-11-11 改：原先这条报"源知识库不存在"，看不出该传什么；
    现在省略 source 且回退值不可用时，错误里直说"未指定迁移源（source）"。
    """
    r = libroot.migrate(tmp_path / "新库")
    assert r["ok"] is False
    assert "未指定迁移源" in r["结果"]
    assert not (tmp_path / "新库").exists()


def test_revert_switches_back(clean, tmp_path):
    """回退只切指针，数据两边都在，且回退后再点回退能回到新位置。"""
    src = tmp_path / "旧库"
    _seed(src)
    libroot._save_config({"root": str(src)})
    dst = tmp_path / "新库"
    assert libroot.migrate(dst)["ok"] is True

    r = libroot.revert()
    assert r["ok"] is True, r
    assert libroot.library_root() == src
    assert (src / "_索引.md").is_file()
    assert (dst / "_索引.md").is_file()      # 新位置的数据没被抹掉
    assert libroot.status()["回退目标"] == str(dst)


def test_revert_without_previous_says_so(clean):
    r = libroot.revert()
    assert r["ok"] is False
    assert "没有可回退" in r["结果"]


def test_repoint_requires_existing_dir(clean, tmp_path):
    """只改指针也得先有目录：指到不存在的地方要当场说清楚，不能装作成功。"""
    missing = libroot.repoint(tmp_path / "还没有的库")
    assert missing["ok"] is False
    assert "不存在" in missing["结果"]

    real = tmp_path / "已备好的库"
    real.mkdir()
    ok = libroot.repoint(real)
    assert ok["ok"] is True
    assert libroot.library_root() == real


def test_config_keeps_other_fields(clean, tmp_path):
    """只改指针时不能把 migrated_at 等既有字段抹掉。"""
    libroot._save_config({"root": str(tmp_path / "甲"), "migrated_at": 123})
    (tmp_path / "乙").mkdir()
    libroot.repoint(tmp_path / "乙")
    d = json.loads((clean / "library.json").read_text(encoding="utf-8"))
    assert d["migrated_at"] == 123
    assert d["root"] == str(tmp_path / "乙")
