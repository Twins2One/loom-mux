from loom import chains, edit_origins


def test_concurrent_agent_content_is_not_mislabeled_manual(project):
    from loom.fsutil import atomic_write_text
    chains.baseline(project)
    rel = '外置大脑/设定集/00-立项.md'
    p = project / rel
    manual = p.read_text(encoding='utf-8') + '\n作者版本'
    atomic_write_text(p, manual)
    atomic_write_text(p, manual + '\nagent后续版本')
    edit_origins.record(project, rel, 'manual', content=manual)
    assert not edit_origins.manual_hints(project, chains.check(project))['hints']


def test_metadata_failure_does_not_fail_saved_document(project, monkeypatch):
    rel = '外置大脑/设定集/00-立项.md'
    def fail(*args, **kwargs):
        raise OSError('metadata unavailable')
    monkeypatch.setattr(edit_origins, 'atomic_write_text', fail)
    assert edit_origins.record(project, rel, 'manual', content='已保存正文') is False
    assert (project / rel).is_file()
    assert edit_origins.manual_hints(project, {'hints': []})['origin_warning']


def test_only_matching_manual_content_produces_banner(project):
    chains.baseline(project)
    rel = '外置大脑/设定集/00-立项.md'
    p = project / rel
    original = p.read_text(encoding='utf-8')
    p.write_text(original + '\n作者新增设定。', encoding='utf-8')
    assert not edit_origins.manual_hints(project, chains.check(project))['hints']
    edit_origins.record(project, rel, 'manual')
    assert any(h['key'] == 'settings' for h in edit_origins.manual_hints(project, chains.check(project))['hints'])
    p.write_text(original + '\n角色后续设定。', encoding='utf-8')
    assert not edit_origins.manual_hints(project, chains.check(project))['hints']
    edit_origins.record(project, rel, 'agent')
    assert not edit_origins.manual_hints(project, chains.check(project))['hints']
