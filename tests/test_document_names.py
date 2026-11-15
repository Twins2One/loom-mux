import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from loom.document_names import RenameError, rename_document


@pytest.fixture
def doc(tmp_path):
    rel = "外置大脑/设定集/20-人物/主角·未命名.md"
    p = tmp_path / rel
    p.parent.mkdir(parents=True)
    p.write_bytes("# 主角\r\n\r\n人物设定。".encode())
    return tmp_path.resolve(), rel, hashlib.sha256(p.read_bytes()).hexdigest()


def test_confirmed_rename_preserves_bytes(doc):
    root, rel, h = doc
    before = (root / rel).read_bytes()
    out = rename_document(root, rel, "主角·周川.md", h, True)
    assert (root / out["rel"]).read_bytes() == before
    assert not (root / rel).exists()
    assert out["hash"] == h


@pytest.mark.parametrize("name", ["../别处.md", "x/y.md", "NUL.md", "a.txt", " .md", "x.md.", "成长档案.md"])
def test_invalid_names_leave_original(doc, name):
    root, rel, h = doc
    with pytest.raises(RenameError):
        rename_document(root, rel, name, h, True)
    assert (root / rel).is_file()


def test_confirmation_and_hash_are_required(doc):
    root, rel, h = doc
    for base, confirm in [(h, False), ("", True), ("old", True)]:
        with pytest.raises(RenameError):
            rename_document(root, rel, "周川.md", base, confirm)
    assert (root / rel).is_file()


def test_existing_destination_is_never_replaced(doc):
    root, rel, h = doc
    p = (root / rel).with_name("主角·周川.md")
    p.write_text("原有文档", encoding="utf-8")
    with pytest.raises(RenameError) as e:
        rename_document(root, rel, p.name, h, True)
    assert e.value.code == "name_exists"
    assert p.read_text(encoding="utf-8") == "原有文档"
    assert (root / rel).is_file()


def test_fixed_world_document_cannot_be_renamed(tmp_path):
    p = tmp_path / "外置大脑/设定集/10-世界观/力量体系.md"
    p.parent.mkdir(parents=True)
    p.write_text("体系", encoding="utf-8")
    with pytest.raises(RenameError) as e:
        rename_document(tmp_path.resolve(), p.relative_to(tmp_path).as_posix(), "新名字.md", "", True)
    assert e.value.code == "fixed_document"
