"""Queue fairness and limits: a bulk drop by one department must not make another department wait behind it, and a
runaway inbox must not grow the job table without bound (files stay in the inbox, nothing is moved or lost)."""
import pytest

from llmwiki.pipeline.queue import round_robin
from llmwiki.pipeline.run import ProcessResult
from llmwiki.pipeline.watch import Watcher, batch_jobs, max_pending_jobs
from test_pipeline_support import make_env


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def fake_process(order):
    def process(path, space):
        order.append(path.name)
        return ProcessResult("done", path, space)
    return process


def test_round_robin_interleaves_spaces_and_keeps_each_spaces_order():
    rows = [("a1", "A", 0), ("a2", "A", 0), ("a3", "A", 0), ("a4", "A", 0), ("b1", "B", 0), ("c1", "C", 0)]
    assert [r[0] for r in round_robin(rows)] == ["a1", "b1", "c1", "a2", "a3", "a4"]
    assert round_robin([]) == []
    assert [r[0] for r in round_robin(rows[:4])] == ["a1", "a2", "a3", "a4"]  # one space: unchanged


def test_a_late_small_drop_is_not_stuck_behind_a_bulk_drop(env):
    for i in range(6):
        env.drop("dept-a", f"a{i}.md", f"# A{i}\n\n내용 {i}{i}")
    env.drop("dept-b", "b0.md", "# B0\n\n내용 77")
    env.watcher.scan_once()
    order: list[str] = []
    env.queue.run_pending(fake_process(order), max_jobs=3)
    assert "b0.md" in order  # served in the first batch of 3, not after all six of dept-a


def test_max_jobs_limits_one_batch_and_the_rest_waits(env):
    for i in range(5):
        env.drop("dept-a", f"a{i}.md", f"# A{i}\n\n내용 {i}{i}")
    env.watcher.scan_once()
    order: list[str] = []
    assert len(env.queue.run_pending(fake_process(order), max_jobs=2)) == 2
    assert env.queue.pending_count() == 3


def test_pending_cap_defers_new_files_without_losing_them(env):
    for i in range(5):
        env.drop("dept-a", f"a{i}.md", f"# A{i}\n\n내용 {i}{i}")
    w = Watcher(env.settings, env.queue, env.audit, max_pending=3)
    counts = w.scan_once()
    assert (counts["queued"], w.deferred) == (3, 2)
    assert len(list((env.settings.data_dir / "inbox" / "dept-a").glob("*.md"))) == 5  # nothing moved, nothing deleted
    env.queue.run_pending(lambda p, s: env.process(p, s))  # the three queued files are really processed and moved away
    counts2 = w.scan_once()  # room again: the two deferred files are picked up now
    assert (counts2["queued"], w.deferred) == (2, 0)


def test_pending_count_ignores_finished_jobs(env):
    env.drop("dept-a", "a.md", "# A\n\n내용 11")
    env.watcher.scan_once()
    assert env.queue.pending_count() == 1
    env.queue.run_pending(fake_process([]))
    assert env.queue.pending_count() == 0


@pytest.mark.parametrize("raw,expected", [("", 500), ("abc", 500), ("0", 500), ("-5", 500), ("25", 25)])
def test_pending_cap_env_parsing(raw, expected):
    assert max_pending_jobs({"WIKI_MAX_PENDING_JOBS": raw}) == expected


@pytest.mark.parametrize("raw,expected", [("", 10), ("x", 10), ("0", 10), ("-1", 10), ("4", 4)])
def test_batch_env_parsing(raw, expected):
    assert batch_jobs({"WIKI_BATCH_JOBS": raw}) == expected
