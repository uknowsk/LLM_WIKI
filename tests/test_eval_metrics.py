import math

import pytest

from llmwiki.eval import metrics as m


def S(*xs):
    return frozenset(xs)


def test_recall_hit_mrr_hand_computed():
    hits = [S("a"), S("x"), S("c")]
    assert m.recall_at_k(hits, {"a", "c"}, 2) == 0.5
    assert m.recall_at_k(hits, {"a", "c"}, 3) == 1.0
    assert m.recall_at_k(hits, set(), 3) == 0.0
    assert m.hit_at_k(hits, {"c"}, 2) == 0.0 and m.hit_at_k(hits, {"c"}, 3) == 1.0
    assert m.mrr(hits, {"c"}) == pytest.approx(1 / 3)
    assert m.mrr(hits, {"zz"}) == 0.0
    assert m.recall_at_k([S("a", "c")], {"a", "c"}, 1) == 1.0  # one merged article covers both gold docs


def test_ndcg_hand_computed():
    assert m.ndcg_at_k([S("a"), S("b")], {"b"}, 2, n_relevant=1) == pytest.approx(1 / math.log2(3))
    got = m.ndcg_at_k([S("a"), S("x"), S("c")], {"a", "c"}, 3)
    assert got == pytest.approx(1.5 / (1 + 1 / math.log2(3)))
    assert m.ndcg_at_k([S("a")], {"a"}, 5) == 1.0
    assert m.ndcg_at_k([], {"a"}, 5) == 0.0


@pytest.mark.parametrize("fact,answer", [
    ("1200만원", "예산은 1,200만원입니다"),
    ("1,200만원", "예산은 1200만원입니다"),
    ("21억 3,000만원", "영업이익 21억3000만원"),
    ("2026년 9월 30일", "마감은 2026-09-30 입니다"),
    ("2026-09-30", "마감은 2026.9.30."),
    ("2026.09.30", "2026년 9월 30일에 완료"),
    ("45분", "약 45 분 소요"),
    ("45 분", "45분"),
    ("10%", "비율은 10 % 이다"),
    ("12", "총 12대"),
])
def test_fact_normalization_matches(fact, answer):
    assert m.fact_in(fact, answer)


@pytest.mark.parametrize("fact,answer", [
    ("12", "총 123대"), ("12", "총 112대"),
    ("2026-09-30", "2026-09-31"), ("45분", "54분"), ("1200만원", "12000만원"),
])
def test_fact_normalization_rejects(fact, answer):
    assert not m.fact_in(fact, answer)


def test_fact_recall_and_correct():
    facts = ["1200만원", "2026년 9월 30일"]
    assert m.fact_recall(facts, "1,200만원 (2026.09.30)") == 1.0
    assert m.fact_recall(facts, "1,200만원") == 0.5
    assert m.answer_correct(facts, "1,200만원 (2026.09.30)") and not m.answer_correct(facts, "1,200만원")


def test_is_refusal():
    assert m.is_refusal("근거 없음") and m.is_refusal("  근거 없음. ") and m.is_refusal("'근거  없음'")
    assert not m.is_refusal("근거 없음이지만 아마 3대") and not m.is_refusal("")


def test_citation_accuracy():
    assert m.citation_accurate([S("x"), S("a")], {"a"})
    assert not m.citation_accurate([S("x")], {"a"}) and not m.citation_accurate([], {"a"})


def test_leak_detection_flags_planted_leaks():
    clean = m.detect_leak("dept-a", "근거 없음", {"x/a.md": {"dept-a"}}, ["21억 3,000만원"], {"x/a.md": "own text"})
    assert clean == []
    assert m.detect_leak("dept-a", "", {"x/b.md": {"dept-b"}}, [])  # cited article outside the space
    assert m.detect_leak("dept-a", "", {"x/m.md": {"dept-a", "dept-b"}}, [])  # mixed-space article
    assert m.detect_leak("dept-a", "", {"x/u.md": set()}, [])  # unknown space fails closed
    assert m.detect_leak("dept-a", "이익은 21억3000만원", {}, ["21억 3,000만원"])  # fact in the answer
    assert m.detect_leak("dept-a", "ok", {"x/a.md": {"dept-a"}}, ["21억 3,000만원"], {"x/a.md": "... 21억 3,000만원 ..."})


def test_latency_stats():
    assert m.percentile([], 50) == 0.0
    xs = list(range(1, 101))
    assert m.percentile(xs, 50) == 50 and m.percentile(xs, 95) == 95 and m.percentile(xs, 100) == 100
    st = m.latency_stats([10.0, 20.0, 30.0])
    assert st["p50"] == 20.0 and st["p95"] == 30.0 and st["mean"] == 20.0
