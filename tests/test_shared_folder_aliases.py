"""WIKI_INBOX_ALIASES: folder name -> space mapping, validation (fail closed), NFC/NFD and case handling."""
from __future__ import annotations

import json
import unicodedata

import pytest

from llmwiki.doctor.__main__ import run_checks
from llmwiki.pipeline import inbox
from llmwiki.pipeline.__main__ import main as pipeline_main
from llmwiki.pipeline.aliases import AliasError, load_aliases, parse_aliases
from llmwiki.pipeline.watch import Watcher
from test_pipeline_support import make_env

NFC = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
NFD = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731
GOOD = {"인사팀": "dept-hr", "인프라팀": "dept-a", "인프라팀/당직": "dept-a/part-1"}


def amap(d):
    return parse_aliases(json.dumps(d, ensure_ascii=False).encode("utf-8"))


def spaces(env, aliases):
    return {i.path.name: i.space for i in inbox.scan(env.settings, aliases)}


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def test_alias_maps_folder_to_space_and_canonical_still_works(env):
    env.drop("인사팀", "a.md", "# a\n\n1")
    env.drop("dept-b", "b.md", "# b\n\n2")
    env.drop("기타팀", "c.md", "# c\n\n3")
    got = spaces(env, amap(GOOD))
    assert got == {"a.md": "dept-hr", "b.md": "dept-b", "c.md": None}
    assert spaces(env, None)["a.md"] is None  # without the alias file the Korean folder is rejected as before


def test_longest_prefix_wins_and_subfolders_inherit(env):
    env.drop("인프라팀", "top.md", "# t\n\n1")
    env.drop("인프라팀/당직", "duty.md", "# d\n\n2")
    env.drop("인프라팀/2026", "sub.md", "# s\n\n3")
    env.drop("인프라팀/당직/야간", "night.md", "# n\n\n4")
    assert spaces(env, amap(GOOD)) == {"top.md": "dept-a", "duty.md": "dept-a/part-1",
                                       "sub.md": "dept-a", "night.md": "dept-a/part-1"}


def test_more_than_four_folder_levels_rejected(env):
    env.drop("인사팀/a/b/c/d", "deep.md", "# d\n\n1")
    assert spaces(env, amap(GOOD)) == {"deep.md": None}


def test_nfc_vs_nfd_folder_names_and_case_insensitivity(env):
    a = amap({"인사팀": "dept-hr", "Finance팀": "dept-fin"})
    assert a.resolve([NFD("인사팀")]) == "dept-hr" == a.resolve([NFC("인사팀")])
    assert a.resolve(["FINANCE팀"]) == "dept-fin" == a.resolve(["finance팀"])
    # a key written in NFD in the file is the same folder as the NFC one
    assert amap({NFD("인사팀"): "dept-hr"}).entries == {NFC("인사팀"): "dept-hr"}
    env.drop(NFD("인사팀"), "n.md", "# n\n\n1")  # created by a client that sends decomposed Hangul
    assert spaces(env, a) == {"n.md": "dept-hr"}


def test_duplicate_keys_incl_unicode_form_and_case_refused():
    for raw in ('{"인사팀": "dept-hr", "인사팀": "dept-a"}',
                json.dumps({NFC("인사팀"): "dept-hr", NFD("인사팀"): "dept-a"}),
                json.dumps({"Hr팀": "dept-hr", "hR팀": "dept-a"})):
        with pytest.raises(AliasError):
            parse_aliases(raw.encode("utf-8"))


BAD_KEYS = ["", " 인사팀", "인사팀 ", "../x", "a/../b", "a/./b", "a\\b", "C:/x", "c:", "a:b", "_done", "x/_hidden", "인사팀.",
            "인사팀 /x", "CON", "nul", "com1/x", "aux.txt", "a//b", "/abs", "a/b/c/d/e", "a*b", "a?b", 'a"b', "a<b", "a|b",
            "a\tb", "..", "a..b"]


@pytest.mark.parametrize("key", BAD_KEYS)
def test_invalid_keys_refused(key):
    with pytest.raises(AliasError):
        amap({key: "dept-hr"})


@pytest.mark.parametrize("value", ["", "Dept-HR", "dept hr", "../dept-b", "_done", "dept-a/_x", "a/b/c/d/e", "con", 5, None, ["x"]])
def test_invalid_values_refused(value):
    with pytest.raises(AliasError):
        amap({"인사팀": value})


@pytest.mark.parametrize("raw", [b"", b"[]", b'"x"', b"{bad", b"\xff\xfe", b'{"a": }', pytest.param(b"[" * 50000, id="deep-nesting")])
def test_invalid_json_refused(raw):
    with pytest.raises(AliasError):
        parse_aliases(raw)


def test_size_cap_64k(tmp_path):
    big = json.dumps({f"팀{i}": "dept-a" for i in range(6000)}, ensure_ascii=False)
    assert len(big.encode()) > 64 * 1024
    with pytest.raises(AliasError, match="너무 큽니다"):
        parse_aliases(big.encode("utf-8"))
    p = tmp_path / "a.json"
    p.write_text(big, encoding="utf-8")
    with pytest.raises(AliasError):
        load_aliases({"WIKI_INBOX_ALIASES": str(p)})


@pytest.mark.parametrize("key,value", [("dept-b", "dept-a"), ("dept-a/인사", "dept-b"), ("DEPT-B", "dept-a"),
                                       ("dept-a/part-1/x", "dept-a/part-2")])
def test_alias_must_not_redirect_a_canonical_space_folder(key, value):
    """A folder Windows ACLs as department dept-b must not be able to write into another department's space."""
    with pytest.raises(AliasError, match="충돌"):
        amap({key: value})


def test_alias_refining_inside_own_canonical_tree_is_allowed():
    assert amap({"dept-a/인사": "dept-a/hr"}).resolve(["dept-a", "인사"]) == "dept-a/hr"
    assert amap({"dept-a": "dept-a"}).resolve(["dept-a"]) == "dept-a"


def test_aliases_cannot_reach_reserved_buckets(env):
    for key in ("_done", "_rejected", "_failed", "인사/_done"):
        with pytest.raises(AliasError):
            amap({key: "dept-hr"})
    with pytest.raises(AliasError):
        amap({"인사팀": "_done"})
    env.drop("_done/인사팀", "x.md", "# x\n\n1")
    assert env.watcher.scan_once()["queued"] == 0  # reserved top-level folders are never scanned


def test_load_aliases_unset_and_missing_file(tmp_path):
    assert load_aliases({}) is None and load_aliases({"WIKI_INBOX_ALIASES": "  "}) is None
    with pytest.raises(AliasError):
        load_aliases({"WIKI_INBOX_ALIASES": str(tmp_path / "missing.json")})


def test_end_to_end_alias_folder_lands_in_canonical_space(env):
    aliases = amap(GOOD)
    w = Watcher(env.settings, env.queue, env.audit, aliases=aliases)
    env.drop("인사팀", "급여.md", "# 급여 안내\n\n지급일 25일")
    assert w.scan_once()["queued"] == 1
    res = env.queue.drain(lambda p, s: env.process(p, s, aliases=aliases))
    assert res[0].status == "done" and res[0].space == "dept-hr"
    assert env.store.article_spaces(res[0].articles[0]) == {"dept-hr"}
    assert (env.settings.raw_dir / "dept-hr").is_dir()
    done = env.settings.data_dir / "inbox" / "_done" / "dept-hr" / "급여.md"  # canonical layout, not the alias name
    assert done.is_file() and not (env.settings.data_dir / "inbox" / "_done" / "인사팀").exists()


def test_pipeline_cli_refuses_to_start_on_bad_alias_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_DATA_DIR", str(tmp_path / "data"))
    bad = tmp_path / "al.json"
    bad.write_text('{"../x": "dept-a"}', encoding="utf-8")
    monkeypatch.setenv("WIKI_INBOX_ALIASES", str(bad))
    assert pipeline_main(["--once"]) == 2
    err = capsys.readouterr().err
    assert "startup refused" in err and "별칭" in err


# ---- doctor ---------------------------------------------------------------------------------------------
def _ids(tmp_path, extra):
    env = {"WIKI_ENV": "development", "WIKI_DATA_DIR": str(tmp_path / "data"), **extra}
    return {r.id: r for r in run_checks(env, skip_llm=True, skip_embed=True)}


def test_doctor_inbox_aliases_states(tmp_path):
    assert _ids(tmp_path, {})["inbox.aliases"].level == "PASS"
    p = tmp_path / "al.json"
    p.write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
    r = _ids(tmp_path, {"WIKI_INBOX_ALIASES": str(p)})["inbox.aliases"]
    assert r.level == "WARN" and "3건 중 3건" in r.title  # no folders yet
    for k in GOOD:
        (tmp_path / "data" / "inbox").joinpath(*k.split("/")).mkdir(parents=True, exist_ok=True)
    assert _ids(tmp_path, {"WIKI_INBOX_ALIASES": str(p)})["inbox.aliases"].level == "PASS"
    p.write_text('{"../x": "dept-a"}', encoding="utf-8")
    assert _ids(tmp_path, {"WIKI_INBOX_ALIASES": str(p)})["inbox.aliases"].level == "FAIL"


def test_doctor_inbox_layout_counts_stray_files_without_names(tmp_path):
    inb = tmp_path / "data" / "inbox"
    inb.mkdir(parents=True)
    assert _ids(tmp_path, {})["inbox.layout"].level == "PASS"
    (inb / "비밀-계약서.md").write_text("x", encoding="utf-8")
    (inb / "~$lock.docx").write_text("x", encoding="utf-8")  # ignored housekeeping: not counted
    r = _ids(tmp_path, {})["inbox.layout"]
    assert r.level == "WARN" and "1개" in r.title and "비밀-계약서" not in (r.title + r.detail + r.hint)
    assert not list(inb.glob(".doctor-probe*"))
