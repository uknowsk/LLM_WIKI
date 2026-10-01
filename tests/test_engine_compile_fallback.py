"""Robust triage: structured output, one retry, deterministic fallback New, truncation, No-material guard."""
import io
import json
import urllib.error

import pytest

from llmwiki.engine import triage as tg
from llmwiki.engine.compile import CompileError
from llmwiki.engine.llm import OpenAICompatClient
from test_engine_support import make_env, triage


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path / "data")


def _wiki_snapshot(env):
    return {p: (env.settings.wiki_dir / p).read_text(encoding="utf-8") for p in env.store.article_paths()}


RAW = "# 주간 회의록\n\n> Source: a.md\n> Collected: 2026-07-13\n\n안건 1: 예산 100\n안건 2: 일정 2026-08-01"


def test_invalid_json_twice_falls_back_to_new(env):
    env.scripted.append("garbage")
    _, r = env.ingest("dept-a", "m.md", RAW, "garbage")
    assert r.decision == "New" and r.fallback == "invalid-reply" and r.article
    assert len(env.compile_llm.calls) == 2  # bounded: exactly two attempts
    page = (env.settings.wiki_dir / r.article).read_text(encoding="utf-8")
    assert page.startswith("---") and "# 주간 회의록" in page and "안건 1: 예산 100" in page
    assert "> Source" not in page and "> Collected" not in page
    log = (env.settings.wiki_dir / "_meta" / "dept-a" / "log.md").read_text(encoding="utf-8")
    assert "New | raw/dept-a/m.md" in log and "fallback:invalid-reply" in log


def test_retry_uses_corrective_note_and_temperature_zero(env):
    env.scripted.append("oops")
    _, r = env.ingest("dept-a", "m.md", RAW, triage(title="회의", topic="mtg", body="안건 1: 예산 100"))
    assert r.fallback is None and r.article == "mtg/회의.md"
    assert "ONE JSON object only" in env.compile_llm.calls[1][1]
    assert "ONE JSON object only" not in env.compile_llm.calls[0][1]
    assert env.compile_llm.temperatures == [None, 0.0]
    assert all(rf == tg.RESPONSE_FORMAT for rf in env.compile_llm.response_formats)


def test_update_with_raw_path_target_falls_back_and_touches_nothing(env):
    _, r1 = env.ingest("dept-a", "1.md", "서버 장애 복구", triage(title="서버 장애", topic="inc", body="복구 120분 " * 5))
    before = _wiki_snapshot(env)
    _, r2 = env.ingest("dept-a", "2.md", "서버 장애 추가", triage("Update", target="raw/dept-a/1.md", body="x"))
    assert r2.fallback == "bad-target" and r2.decision == "New" and r2.article != r1.article
    after = _wiki_snapshot(env)
    assert {p: after[p] for p in before} == before  # no existing article modified
    _, r3 = env.ingest("dept-a", "3.md", "서버 장애 또", '{"decision":"Disputed","target":null,"body":"b"}')
    assert r3.fallback == "bad-target"


@pytest.mark.parametrize("body", ["", "짧음"])
def test_update_empty_or_shrunk_body_falls_back(env, body):
    old = "서버 장애 상세 기록 " * 20
    _, r1 = env.ingest("dept-a", "1.md", "서버 장애", triage(title="서버 장애", topic="inc", body=old))
    before = _wiki_snapshot(env)
    _, r2 = env.ingest("dept-a", "u.md", "서버 장애 갱신", triage("Update", target=r1.article, body=body))
    assert r2.decision == "New" and r2.fallback in ("empty-body", "shrink") and r2.article != r1.article
    assert _wiki_snapshot(env)[r1.article] == before[r1.article]
    assert "raw/dept-a/u.md" not in env.store.article_sources(r1.article)


def test_cross_space_candidate_never_offered_or_modified(env):
    _, rb = env.ingest("dept-b", "b.md", "비밀 보고 100", triage(title="비밀", body="b" * 50))
    before = _wiki_snapshot(env)
    _, ra = env.ingest("dept-a", "a.md", "비밀 보고 추가", triage("Update", target=rb.article, body="x" * 60))
    assert ra.fallback == "bad-target" and env.store.article_spaces(ra.article) == {"dept-a"}
    assert _wiki_snapshot(env)[rb.article] == before[rb.article]
    assert rb.article not in env.compile_llm.calls[-1][1]


def test_no_material_with_data_is_overridden(env):
    _, r = env.ingest("dept-a", "d.md", "# 라이선스\n\n| a | 10 |\n| b | 2026 |\n| c | 300 |", triage("No material"))
    assert r.decision == "New" and r.fallback == "no-material-with-data" and r.article


def test_no_material_without_data_still_drops(env):
    _, r = env.ingest("dept-a", "n.md", "잡담입니다", triage("No material"))
    assert r.article is None and r.fallback is None


def test_prompt_truncation_keeps_head_and_tail(env, monkeypatch):
    monkeypatch.setenv("WIKI_COMPILE_MAX_CHARS", "2000")
    text = "HEAD" + "x" * 10000 + "TAIL"
    env.ingest("dept-a", "big.md", text, triage(title="큰 문서", body="내용"))
    prompt = env.compile_llm.calls[0][1]
    assert "[... truncated ...]" in prompt and "HEAD" in prompt and "TAIL" in prompt and len(prompt) < 3000
    assert tg.truncate("short", 100) == ("short", False)


def test_fallback_stores_full_body_and_stem_title(env, monkeypatch):
    monkeypatch.setenv("WIKI_COMPILE_MAX_CHARS", "2000")
    text = "plain line 1\n" + "y" * 10000 + "ENDMARK"
    env.scripted.append("bad")
    _, r = env.ingest("dept-a", "stem-name.md", text, "bad")
    page = (env.settings.wiki_dir / r.article).read_text(encoding="utf-8")
    assert "ENDMARK" in page and "# stem-name" in page and r.fallback == "invalid-reply"


def test_empty_raw_fallback_fails_closed(env):
    env.scripted.append("bad")
    with pytest.raises(CompileError):
        env.ingest("dept-a", "e.md", "# only title\n> Source: x", "bad")
    assert env.store.article_paths() == []


def test_call_llm_passes_only_supported_args():
    class Old:
        def complete(self, system, prompt, temperature=None):
            return f"t={temperature}"

    assert tg.call_llm(Old(), "s", "p", 0.0, tg.RESPONSE_FORMAT) == "t=0.0"
    assert tg.call_llm(lambda s, p: "plain", "s", "p", None, tg.RESPONSE_FORMAT) == "plain"


class _Resp:
    def __init__(self, content):
        self._b = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_structured_output_rejection_falls_back_to_plain_mode():
    c = OpenAICompatClient("http://127.0.0.1:1234/v1", "m")
    sent = []

    def fake_open(req, timeout=None):
        body = json.loads(req.data)
        sent.append("response_format" in body)
        if "response_format" in body:
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b""))
        return _Resp("ok")

    c._opener.open = fake_open
    assert c.complete("s", "p", response_format=tg.RESPONSE_FORMAT) == "ok"
    assert c.complete("s", "p", response_format=tg.RESPONSE_FORMAT) == "ok"
    assert sent == [True, False, False]  # rejected once, then plain mode remembered
