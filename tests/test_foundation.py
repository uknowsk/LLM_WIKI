import pytest

from llmwiki.acl import can_read, filter_readable
from llmwiki.audit import AuditLog
from llmwiki.auth import DevAuthProvider, User, get_provider
from llmwiki.config import load_settings

A = User("u1", "Kim", "dept-a", "part-1", frozenset({"dept-a", "dept-a/part-1"}))
B = User("u2", "Lee", "dept-b", None, frozenset({"dept-b"}))


def test_single_space_access():
    assert can_read(A, ["dept-a"])
    assert not can_read(B, ["dept-a"])


def test_merged_article_requires_every_source_space():
    assert can_read(A, ["dept-a", "dept-a/part-1"])
    assert not can_read(A, ["dept-a", "dept-b"])  # one unreadable source blocks it


def test_unlabeled_document_is_denied():
    assert not can_read(A, [])


def test_filter_before_llm_context():
    docs = [("x", ["dept-a"]), ("y", ["dept-b"]), ("z", ["dept-a", "dept-b"])]
    assert [d[0] for d in filter_readable(A, docs, lambda d: d[1])] == ["x"]


def test_dev_provider_lookup():
    p = DevAuthProvider({"u1": A})
    assert p.authenticate({"user_id": "u1"}) == A
    assert p.authenticate({"user_id": "nope"}) is None


def test_dev_auth_forbidden_in_production():
    s = load_settings({"WIKI_ENV": "production", "WIKI_AUTH_PROVIDER": "dev"})
    with pytest.raises(RuntimeError):
        get_provider(s)


def test_audit_records_user(tmp_path):
    log = AuditLog(tmp_path / "a.db")
    log.record(A, "query", "wiki/x.md", "q=test")
    rows = log.entries("u1")
    assert len(rows) == 1 and rows[0][1:4] == ("u1", "query", "wiki/x.md")
    log.close()


def test_paths_come_from_environment():
    s = load_settings({"WIKI_DATA_DIR": "/srv/wiki"})
    assert s.raw_dir.name == "raw" and s.db_path.name == "wiki.db"
