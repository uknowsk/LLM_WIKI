"""vault_lint: the second-brain vault checker (10_raw_data / 20_ai_wiki / 30_outputs), built on the engine's grounding check."""
from pathlib import Path

import pytest

from llmwiki.vault_lint import MANIFEST, main, run_checks

RAW_A = "---\ntype: raw\ndate: 2026-09-15\nsource: x\n---\n# 2026-09-15_회의\n\n출시일은 2026-12-04, 예산은 3,500만원으로 한다.\n"
RAW_B = "---\ntype: raw\ndate: 2026-09-20\nsource: x\n---\n# 2026-09-20_메모\n\n새로 알 것 없음 12\n"
WIKI = ("---\ntype: wiki\ntopic: 알파\nupdated: 2026-10-08\nsources:\n  - \"[[2026-09-15_회의]]\"\nstatus: stable\n---\n"
        "# 알파\n\n## 요약\n출시일은 2026-12-04, 예산은 3,500만원이다. 관련: [[사람]]\n")
PERSON = ("---\ntype: wiki\ntopic: 사람\nupdated: 2026-10-08\nsources:\n  - \"[[2026-09-15_회의]]\"\nstatus: stable\n---\n"
          "# 사람\n\n[[알파]] 담당.\n")
INDEX = "# 위키 인덱스\n\n## 문서 목록\n- [[알파]] — x (updated: 2026-10-08)\n- [[사람]] — y (updated: 2026-10-08)\n\n## 처리 대기 중인 원본\n"
LOG = "# 작업 로그\n\n- 2026-10-08 ingest no material: [[2026-09-20_메모]] — 새 내용 없음\n"
OUT = ("---\ntype: output\ndate: 2026-10-08\nbased_on:\n  - \"[[알파]]\"\nstatus: draft\n---\n# 2026-10-08_뉴스\n\n출시일은 2026-12-04 입니다.\n")


def make_vault(tmp_path: Path, **over) -> Path:
    files = {"10_raw_data/2026-09-15_회의.md": RAW_A, "10_raw_data/2026-09-20_메모.md": RAW_B,
             "20_ai_wiki/알파.md": WIKI, "20_ai_wiki/사람.md": PERSON, "20_ai_wiki/_index.md": INDEX,
             "20_ai_wiki/_log.md": LOG, "30_outputs/2026-10-08_뉴스.md": OUT,
             "20_ai_wiki/_templates/t.md": "[[깨져도 무시]] 9999"}
    files.update(over)
    for rel, text in files.items():
        if text is None:
            continue
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def kinds(findings, level=None):
    return sorted(f.kind for f in findings if level is None or f.level == level)


def test_clean_vault_has_no_errors_and_ignores_templates(tmp_path):
    v = make_vault(tmp_path)
    run_checks(v, accept=True)
    assert [f for f in run_checks(v) if f.level == "E"] == []
    assert main(["--vault", str(v)]) == 0


def test_broken_wikilink_is_an_error_aliases_and_headings_resolve(tmp_path):
    v = make_vault(tmp_path, **{"20_ai_wiki/알파.md": WIKI.replace("[[사람]]", "[[없는문서]] [[사람|별칭]] [[사람#상세]]")})
    errs = [f for f in run_checks(v) if f.kind == "broken-link"]
    assert [f.detail for f in errs] == ["없는문서"]


def test_links_inside_code_spans_and_blocks_are_examples_not_links(tmp_path):
    log = LOG + "\n형식: `- 신규 [[문서명]]`\n\n```\n[[또 다른 예시]]\n```\n"
    v = make_vault(tmp_path, **{"20_ai_wiki/_log.md": log})
    assert [f for f in run_checks(v) if f.kind == "broken-link"] == []


def test_hub_pages_need_no_sources_and_count_as_inbound_links(tmp_path):
    hub = "---\ntype: hub\ntopic: 홈\nupdated: 2026-10-08\nstatus: stable\n---\n# 홈\n\n- [[알파]]\n- [[외톨이]]\n"
    lonely = PERSON.replace("topic: 사람", "topic: 외톨이").replace("# 사람", "# 외톨이").replace("[[알파]] 담당.", "내용")
    idx = INDEX + "- [[홈]] — h (updated: 2026-10-08)\n- [[외톨이]] — o (updated: 2026-10-08)\n"
    v = make_vault(tmp_path, **{"20_ai_wiki/홈.md": hub, "20_ai_wiki/외톨이.md": lonely, "20_ai_wiki/_index.md": idx})
    found = {(f.kind, f.file) for f in run_checks(v)}
    assert not [1 for k, file in found if k in ("missing-frontmatter", "bad-source") and file.endswith("홈.md")]
    assert ("orphan", "20_ai_wiki/외톨이.md") not in found  # the hub links to it
    assert ("orphan", "20_ai_wiki/홈.md") not in found  # a hub is the root: nothing has to link to it


def test_findings_are_not_duplicated(tmp_path):
    v = make_vault(tmp_path, **{"20_ai_wiki/알파.md": WIKI.replace("3,500만원", "4,500만원") + "\n다시 4,500만원\n"})
    found = [x for x in run_checks(v) if x.kind == "ungrounded-number"]
    assert len(found) == len(set(found)) == 1


def test_private_pages_cannot_feed_an_output_unless_the_output_is_private(tmp_path):
    private = WIKI.replace("status: stable", "status: stable\nvisibility: private")
    v = make_vault(tmp_path / "a", **{"20_ai_wiki/알파.md": private})
    assert "private-in-output" in kinds(run_checks(v), "E")
    ok = OUT.replace("status: draft", "status: draft\naudience: private")
    v2 = make_vault(tmp_path / "b", **{"20_ai_wiki/알파.md": private, "30_outputs/2026-10-08_뉴스.md": ok})
    assert "private-in-output" not in kinds(run_checks(v2))
    assert "private-in-output" not in kinds(run_checks(make_vault(tmp_path / "c")))  # nothing is private by default


def test_old_drafts_are_flagged_as_stale(tmp_path):
    from datetime import date
    draft = WIKI.replace("status: stable", "status: draft").replace("updated: 2026-10-08", "updated: 2026-08-01")
    v = make_vault(tmp_path, **{"20_ai_wiki/알파.md": draft, "20_ai_wiki/_index.md": INDEX.replace("[[알파]] — x (updated: 2026-10-08)", "[[알파]] — x (updated: 2026-08-01)")})
    assert "stale-draft" in kinds(run_checks(v, today=date(2026, 10, 8)), "W")
    assert "stale-draft" not in kinds(run_checks(v, today=date(2026, 8, 20)))


def test_attachments_of_any_type_are_protected_by_the_manifest(tmp_path):
    v = make_vault(tmp_path)
    (v / "10_raw_data/_files").mkdir()
    (v / "10_raw_data/_files/scan.pdf").write_bytes(b"%PDF-1")
    run_checks(v, accept=True)
    (v / "10_raw_data/_files/scan.pdf").write_bytes(b"%PDF-2")
    assert ("E", "raw-changed") in {(x.level, x.kind) for x in run_checks(v)}


def test_index_must_match_the_files(tmp_path):
    v = make_vault(tmp_path, **{"20_ai_wiki/_index.md": INDEX.replace("- [[사람]] — y (updated: 2026-10-08)\n", "- [[유령]] — z (updated: 2026-10-08)\n")})
    k = kinds(run_checks(v), "E")
    assert k.count("index-missing") == 1 and k.count("index-dangling") == 1


def test_index_date_must_match_the_article(tmp_path):
    v = make_vault(tmp_path, **{"20_ai_wiki/_index.md": INDEX.replace("[[알파]] — x (updated: 2026-10-08)", "[[알파]] — x (updated: 2026-01-01)")})
    assert "index-date" in kinds(run_checks(v), "E")


def test_ungrounded_number_date_or_quote_in_wiki_is_an_error(tmp_path):
    v = make_vault(tmp_path, **{"20_ai_wiki/알파.md": WIKI.replace("3,500만원", "4,500만원").replace("2026-12-04", "2026-12-05")})
    found = {(f.kind, f.detail) for f in run_checks(v) if f.level == "E" and f.kind.startswith("ungrounded")}
    assert ("ungrounded-number", "4,500") in found and ("ungrounded-date", "2026-12-05") in found


def test_output_numbers_must_come_from_the_wiki_it_is_based_on(tmp_path):
    v = make_vault(tmp_path, **{"30_outputs/2026-10-08_뉴스.md": OUT.replace("입니다.", "이고 예산은 9,999만원 입니다.")})
    assert [(f.kind, f.detail) for f in run_checks(v) if f.kind == "ungrounded-number"] == [("ungrounded-number", "9,999")]


def test_raw_files_are_immutable_after_the_manifest_is_accepted(tmp_path):
    v = make_vault(tmp_path)
    assert main(["--vault", str(v), "--accept"]) == 0 and (v / "10_raw_data" / MANIFEST).is_file()
    assert [f for f in run_checks(v) if f.kind.startswith("raw-")] == []
    (v / "10_raw_data/2026-09-15_회의.md").write_text(RAW_A + "\n몰래 고침", encoding="utf-8")
    (v / "10_raw_data/2026-09-20_메모.md").unlink()
    (v / "10_raw_data/2026-10-01_새.md").write_text("---\ntype: raw\ndate: 2026-10-01\nsource: x\n---\n# 새\n\n본문 77\n", encoding="utf-8")
    got = {(f.level, f.kind) for f in run_checks(v) if f.kind.startswith("raw-")}
    assert ("E", "raw-changed") in got and ("E", "raw-deleted") in got and ("I", "raw-new") in got
    assert main(["--vault", str(v)]) == 1


def test_pending_raw_is_a_warning_but_no_material_logged_raw_is_not(tmp_path):
    v = make_vault(tmp_path, **{"10_raw_data/2026-10-08_대기.md": "---\ntype: raw\ndate: 2026-10-08\nsource: x\n---\n# 대기\n\n본문\n"})
    pend = [f for f in run_checks(v) if f.kind == "raw-unprocessed"]
    assert [f.file for f in pend] == ["10_raw_data/2026-10-08_대기.md"] and pend[0].level == "W"


def test_status_blocks_need_a_date_and_an_explanation(tmp_path):
    bad = WIKI + "\n> **Status: Outdated** (이유만 있고 날짜 없음)\n> 옛 내용\n\n> **Status: Disputed**\n"
    ok = WIKI + "\n> **Status: Outdated** (2026-10-02, 대체됨)\n> 출시일 2026-12-04\n"
    assert "bad-status" in kinds(run_checks(make_vault(tmp_path / "a", **{"20_ai_wiki/알파.md": bad})), "E")
    assert "bad-status" not in kinds(run_checks(make_vault(tmp_path / "b", **{"20_ai_wiki/알파.md": ok})))


def test_missing_frontmatter_and_missing_sources_are_errors(tmp_path):
    v = make_vault(tmp_path, **{"20_ai_wiki/사람.md": "# 사람\n\n[[알파]] 담당.\n"})
    k = kinds(run_checks(v), "E")
    assert "missing-frontmatter" in k


def test_orphan_wiki_page_is_a_warning(tmp_path):
    lonely = PERSON.replace("topic: 사람", "topic: 외톨이").replace("# 사람", "# 외톨이").replace("[[알파]] 담당.", "내용")
    v = make_vault(tmp_path, **{"20_ai_wiki/외톨이.md": lonely,
                                "20_ai_wiki/_index.md": INDEX + "- [[외톨이]] — o (updated: 2026-10-08)\n"})
    orphans = [f.file for f in run_checks(v) if f.kind == "orphan"]
    assert orphans == ["20_ai_wiki/외톨이.md"]  # being in the index does not count as an inbound link


def test_ambiguous_names_are_reported(tmp_path):
    v = make_vault(tmp_path, **{"20_ai_wiki/하위/알파.md": WIKI})
    assert "ambiguous-name" in kinds(run_checks(v), "E")


@pytest.mark.parametrize("missing", ["10_raw_data", "20_ai_wiki"])
def test_missing_folder_is_a_clear_error_not_a_crash(tmp_path, missing, capsys):
    v = make_vault(tmp_path)
    import shutil
    shutil.rmtree(v / missing)
    assert main(["--vault", str(v)]) == 2
    assert missing in capsys.readouterr().err
