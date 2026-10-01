import pytest

from llmwiki.engine import article as art
from llmwiki.engine.compile import CompileError
from llmwiki.engine.lint import lint_grounding
from llmwiki.engine.llm import OpenAICompatClient
from llmwiki.engine.search import bm25_rank, tokenize
from test_engine_support import make_env, triage


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path / "data")


def test_new_article_files_links_index_log(env):
    rec, res = env.ingest("dept-a", "w1.md", "서버 장애는 2026-09-30 에 발생, 복구 120분.",
                          triage(title="서버 장애", topic="incident", body="2026-09-30 장애, 복구 120분"))
    assert res.decision == "New" and res.article == "incident/서버-장애.md"
    text = (env.settings.wiki_dir / res.article).read_text(encoding="utf-8")
    assert "[w1.md](../../raw/dept-a/w1.md)" in text and "## Sources" in text
    meta = env.settings.wiki_dir / "_meta" / "dept-a"
    assert "[[incident/서버-장애|서버 장애]]" in (meta / "index.md").read_text(encoding="utf-8")
    assert "New | raw/dept-a/w1.md" in (meta / "log.md").read_text(encoding="utf-8")
    assert env.store.article_spaces(res.article) == {"dept-a"}
    assert res.suspects == 0


def test_update_disputed_and_cascade(env):
    _, r1 = env.ingest("dept-a", "1.md", "서버 장애 복구 120분", triage(title="서버 장애", topic="incident", body="복구 120분"))
    _, r2 = env.ingest("dept-a", "2.md", "네트워크 점검 서버 장애 관련", triage(
        title="네트워크 점검", topic="ops", body="점검", related=[r1.article]))
    assert art.parse("x", (env.settings.wiki_dir / r1.article).read_text(encoding="utf-8")).related == [r2.article]
    # Update merges sources into the same article
    _, r3 = env.ingest("dept-a", "3.md", "서버 장애 복구 완료", triage("Update", target=r1.article, body="복구 120분, 완료"))
    assert r3.article == r1.article and len(env.store.article_sources(r1.article)) == 2
    # Disputed keeps old body and appends the conflict
    _, r4 = env.ingest("dept-a", "4.md", "서버 장애 복구 90분", triage("Disputed", target=r1.article, body="90분 주장"))
    t = (env.settings.wiki_dir / r4.article).read_text(encoding="utf-8")
    assert "복구 120분, 완료" in t and "## Disputed" in t and "90분 주장" in t


def test_no_material_creates_nothing(env):
    _, res = env.ingest("dept-a", "n.md", "잡담", triage("No material"))
    assert res.article is None and env.store.article_paths() == []


def test_invalid_reply_and_foreign_target_rejected(env):
    with pytest.raises(CompileError):
        env.ingest("dept-a", "x.md", "텍스트", "not json")
    _, rb = env.ingest("dept-b", "b.md", "비밀 보고", triage(title="비밀", body="b"))
    with pytest.raises(CompileError):  # LLM tries to merge into another space's article
        env.ingest("dept-a", "y.md", "비밀 보고 추가", triage("Update", target=rb.article, body="x"))
    with pytest.raises(CompileError):
        env.ingest("", "z.md", "text", triage())


def test_same_title_in_two_spaces_stay_separate(env):
    _, ra = env.ingest("dept-a", "a.md", "주간 보고 A", triage(title="주간 보고", topic="report", body="A"))
    _, rb = env.ingest("dept-b", "b.md", "주간 보고 B", triage(title="주간 보고", topic="report", body="B"))
    assert ra.article != rb.article
    assert "주간 보고 A" not in env.compile_llm.calls[-1][1].split("CANDIDATE")[1]


def test_lint_flags_ungrounded_and_does_not_fix(env):
    _, res = env.ingest("dept-a", "w.md", '매출 1,200만원, 날짜 2026-09-30, "정확한 원문 인용 문장" 포함',
                        triage(title="매출", body='매출 1,300만원, 2026-09-31 확인, "정확한 원문 인용 문장" 과 "지어낸 긴 인용 문장입니다"'))
    assert res.suspects == 3
    before = (env.settings.wiki_dir / res.article).read_text(encoding="utf-8")
    kinds = {(s.kind, s.value) for s in lint_grounding(env.settings, env.store)}
    assert kinds == {("number", "1,300"), ("date", "2026-09-31"), ("quote", "지어낸 긴 인용 문장입니다")}
    assert (env.settings.wiki_dir / res.article).read_text(encoding="utf-8") == before


def test_lint_clean_and_missing_source(env):
    env.ingest("dept-a", "w.md", "매출 1,200만원 2026-09-30", triage(title="매출", body="매출 1,200만원 2026-09-30"))
    assert lint_grounding(env.settings, env.store) == []
    (env.settings.data_dir / "raw/dept-a/w.md").unlink()
    kinds = [s.kind for s in lint_grounding(env.settings, env.store)]
    assert kinds[0] == "missing-source"


def test_korean_bm25():
    docs = {"a": "서버 장애 복구 절차", "b": "휴가 신청 방법"}
    assert bm25_rank("장애가 복구", docs)[0][0] == "a"
    assert bm25_rank("전혀무관", docs) == []
    assert tokenize("매출은 100") == ["매출", "출은", "100"]


def test_openai_client_intranet_only():
    OpenAICompatClient("http://127.0.0.1:8000/v1", "m")
    OpenAICompatClient("http://10.1.2.3/v1", "m")
    OpenAICompatClient("http://llm-server:8000/v1", "m")
    with pytest.raises(ValueError):
        OpenAICompatClient("https://api.example.com/v1", "m")
