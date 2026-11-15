"""同章新执行与活动执行重试的身份边界。"""
from loom import agents, task_state as ts
from loom.paths import TASKS_DIR
from loom.config import Config


def test_completed_chapter_gets_new_run_and_keeps_old(tmp_path):
    progress = lambda ev: None
    cfg = Config(title="测试")
    agents._pipeline_task(tmp_path, 1, cfg, progress)
    base = tmp_path / TASKS_DIR / "流水线"
    first = ts.load_snapshot(base, "t-pipe-0001")
    agents._pipeline_task(tmp_path, 1, cfg, progress, finish=True)
    before = ts.load_snapshot(base, first.task_id).to_dict()
    second_id = agents._pipeline_task(tmp_path, 1, cfg, progress)
    assert second_id != first.task_id and second_id
    bound = agents._task_progress(progress, second_id)
    agents._pipeline_step_done(tmp_path, 1, "大纲师", "提纲", bound)
    agents._agentic_step_done(tmp_path, 1, "提纲", bound)
    agents._pipeline_task(tmp_path, 1, cfg, bound, finish=True)
    assert ts.load_snapshot(base, first.task_id).to_dict() == before
    second = ts.load_snapshot(base, second_id)
    assert second.status == "completed"
    assert ts.rebuild_from_log(base, second_id).to_dict() == second.to_dict()


def test_active_pipeline_retry_retains_identity(tmp_path):
    cfg = Config(title="测试")
    progress = lambda ev: None
    first = agents._pipeline_task(tmp_path, 2, cfg, progress)
    assert first
    assert agents._pipeline_task(tmp_path, 2, cfg, progress) == first


def test_agentic_real_entry_keeps_each_execution_bound(tmp_path, monkeypatch):
    from loom import write_tools, writeloop
    cfg = Config(title="测试")
    identities = []

    def fake_run(session, **kwargs):
        identities.append(session.progress._pipeline_task_id)
        agents._agentic_step_done(tmp_path, 1, "提纲", session.progress)
        return "正文"

    monkeypatch.setattr(writeloop, "run_chapter", fake_run)
    monkeypatch.setattr(agents, "_finish_chapter", lambda *args, **kwargs: (tmp_path / "正文.md", "正文"))
    monkeypatch.setattr(agents, "_hardfacts_for", lambda root: "")
    for _ in range(2):
        agents._run_agentic(tmp_path, 1, None, cfg, lambda event: None)
    assert len(set(identities)) == 2
    base = tmp_path / TASKS_DIR / "流水线"
    for tid in identities:
        assert ts.load_snapshot(base, tid).status == "completed"
        assert ts.rebuild_from_log(base, tid).to_dict() == ts.load_snapshot(base, tid).to_dict()
