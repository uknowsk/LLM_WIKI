"""process_file wires the optional embedder into the compiler; an embedder outage never fails ingest."""
from __future__ import annotations

from llmwiki.engine.embed import FakeEmbedder
from test_pipeline_support import make_env


def test_process_file_embeds_new_article(tmp_path):
    env = make_env(tmp_path)
    p = env.drop("dept-a", "a.md", "# 제목\n\n서버 장애 복구 절차 본문")
    r = env.process(p, "dept-a", embedder=FakeEmbedder())
    assert r.status == "done" and r.articles
    assert set(env.store.embedding_meta()) == set(r.articles)


def test_process_file_survives_embedder_outage(tmp_path):
    class Down:
        model = "x"

        def embed(self, texts):
            raise RuntimeError("down")

    env = make_env(tmp_path)
    p = env.drop("dept-a", "a.md", "# 제목\n\n서버 장애 복구 절차 본문")
    r = env.process(p, "dept-a", embedder=Down())
    assert r.status == "done" and r.articles and env.store.embedding_meta() == {}
