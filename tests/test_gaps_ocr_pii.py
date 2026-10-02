"""Gap fixes: CommandOcrEngine, doctor OCR/PII checks, WIKI_PII_POLICY_FILE, pipeline CLI startup (synthetic data only)."""
from __future__ import annotations

import json
import sys

import pytest

from llmwiki.doctor.__main__ import run_checks
from llmwiki.ingest.pdf import parse_pdf
from llmwiki.pipeline.__main__ import main as pipeline_main
from llmwiki.pipeline.ocr_command import CommandOcrEngine, OcrError, ocr_from_env
from llmwiki.pipeline.pii_policy import PiiPolicyError, load_pii_policy
from llmwiki.pipeline.run import mask_for_space

STUB = r'''
import os, sys
mode = sys.argv[1]
data = sys.stdin.buffer.read()
if mode == "pages":
    sys.stdout.buffer.write(("p1:" + os.environ.get("WIKI_OCR_INPUT", "") + "\fp2:%d" % len(data)).encode("utf-8"))
elif mode == "count":
    open(sys.argv[2], "a").write("x")
    sys.stdout.buffer.write(b"a\fb")
elif mode == "fail":
    sys.stderr.write("SECRET-DOC-TEXT"); sys.exit(3)
elif mode == "sleep":
    import time; time.sleep(30)
elif mode == "big":
    sys.stdout.write("x" * 100000)
elif mode == "badutf8":
    sys.stdout.buffer.write(b"\xff\xfe")
elif mode == "env":
    sys.stdout.write("LEAK" if "WIKI_SECRET_KEY" in os.environ else "clean")
'''


@pytest.fixture
def stub(tmp_path):
    p = tmp_path / "stub.py"
    p.write_text(STUB, encoding="utf-8")
    return p


def eng(stub, *args, **kw):
    return CommandOcrEngine(sys.executable, [str(stub), *args], **kw)


PDF = b"%PDF-1.4 synthetic"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8


def test_pdf_mode_splits_pages_and_runs_once(stub, tmp_path):
    counter = tmp_path / "n"
    e = eng(stub, "count", str(counter))
    assert [e.ocr_page(PDF, 0), e.ocr_page(PDF, 1)] == ["a", "b"]
    assert counter.read_text() == "x"  # one child for the whole PDF
    with pytest.raises(OcrError, match="page 3"):
        e.ocr_page(PDF, 2)


def test_kind_env_and_image_mode(stub):
    assert eng(stub, "pages").ocr_page(PDF, 0) == "p1:pdf"
    assert eng(stub, "pages", input_kind="image").ocr_page(PNG, 0).startswith("p1:image")
    with pytest.raises(OcrError, match="cannot OCR a PDF"):
        eng(stub, "pages", input_kind="image").ocr_page(PDF, 0)


def test_nonzero_exit_hides_stderr(stub):
    with pytest.raises(OcrError) as ei:
        eng(stub, "fail").ocr_page(PDF, 0)
    assert "code 3" in str(ei.value) and "SECRET-DOC-TEXT" not in str(ei.value)


def test_timeout_output_cap_bad_utf8_missing_command(stub):
    with pytest.raises(OcrError, match="timed out"):
        eng(stub, "sleep", timeout=0.5).ocr_page(PDF, 0)
    with pytest.raises(OcrError, match="cap"):
        eng(stub, "big", max_output=1000).ocr_page(PDF, 0)
    with pytest.raises(OcrError, match="UTF-8"):
        eng(stub, "badutf8").ocr_page(PDF, 0)
    with pytest.raises(OcrError, match="cannot start"):
        CommandOcrEngine("Z:/no/such/ocr.exe").ocr_page(PDF, 0)


def test_child_does_not_inherit_wiki_secrets(stub, monkeypatch):
    monkeypatch.setenv("WIKI_SECRET_KEY", "k")
    assert eng(stub, "env").ocr_page(PDF, 0) == "clean"


def test_ocr_from_env_validation(stub, tmp_path):
    assert ocr_from_env({}) is None
    e = ocr_from_env({"WIKI_OCR_COMMAND": sys.executable, "WIKI_OCR_ARGS": json.dumps([str(stub), "pages"]),
                      "WIKI_OCR_TIMEOUT": "7"})
    assert e.timeout == 7 and e.input_kind == "pdf" and e.ocr_page(PDF, 1).startswith("p2:")
    for bad in ({"WIKI_OCR_COMMAND": "Z:/nope.exe"},
                {"WIKI_OCR_COMMAND": sys.executable, "WIKI_OCR_ARGS": "not json"},
                {"WIKI_OCR_COMMAND": sys.executable, "WIKI_OCR_ARGS": '["a", 1]'},
                {"WIKI_OCR_COMMAND": sys.executable, "WIKI_OCR_TIMEOUT": "0"},
                {"WIKI_OCR_COMMAND": sys.executable, "WIKI_OCR_INPUT": "tiff"}):
        with pytest.raises(ValueError):
            ocr_from_env(bad)


def test_scanned_pdf_goes_through_command_engine(stub):
    class Blank:
        def extract_pages(self, data):
            return ["", ""]

    doc = parse_pdf(PDF, "scan.pdf", extractor=Blank(), ocr=eng(stub, "count", str(stub) + ".n"))
    assert doc.text == "a\n\nb" and doc.metadata["ocr_pages"] == "1,2"


def _ids(env, **kw):
    return {r.id: r for r in run_checks(env, skip_llm=True, skip_embed=True, **kw)}


def test_doctor_ocr_states(stub):
    base = {"WIKI_ENV": "development"}
    assert _ids(base)["ocr.command"].level == "WARN"
    cfg = {**base, "WIKI_OCR_COMMAND": sys.executable, "WIKI_OCR_ARGS": json.dumps([str(stub), "pages"])}
    r = _ids(cfg)
    assert r["ocr.command"].level == "PASS" and "ocr.probe" in r and r["ocr.probe"].level == "WARN"
    assert _ids(cfg, probe_ocr=True)["ocr.probe"].level == "PASS"
    bad = {**base, "WIKI_OCR_COMMAND": sys.executable, "WIKI_OCR_ARGS": json.dumps([str(stub), "fail"])}
    assert _ids(bad, probe_ocr=True)["ocr.probe"].level == "FAIL"
    assert _ids({**base, "WIKI_OCR_COMMAND": "Z:/nope.exe"})["ocr.command"].level == "FAIL"


# ---- PII policy ------------------------------------------------------------------------------------
def pol(tmp_path, content):
    p = tmp_path / "pii.json"
    p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return {"WIKI_PII_POLICY_FILE": str(p)}


def test_policy_loads_and_nearest_ancestor_wins(tmp_path):
    assert load_pii_policy({}) is None
    p = load_pii_policy(pol(tmp_path, {"dept-a": True, "dept-b/part-1": False}))
    assert p == {"dept-a": True, "dept-b/part-1": False}

    class S:
        mask_pii_default = False

    assert mask_for_space(S, "dept-a/team-x", p) is True
    assert mask_for_space(S, "dept-b/part-1/x", p) is False
    assert mask_for_space(S, "other", p) is False


@pytest.mark.parametrize("content", ['{"dept-a": 1}', '{"Dept A": true}', '{"dept-a": "true"}', '["dept-a"]', "{bad",
                                     '{"dept-a": true, "dept-a": false}', '{"a/b/c/d/e": true}'])
def test_policy_rejects_invalid(tmp_path, content):
    with pytest.raises(PiiPolicyError):
        load_pii_policy(pol(tmp_path, content))


def test_policy_unreadable_fails_closed(tmp_path):
    with pytest.raises(PiiPolicyError):
        load_pii_policy({"WIKI_PII_POLICY_FILE": str(tmp_path / "missing.json")})


def test_doctor_reports_policy(tmp_path):
    ok_env = {"WIKI_ENV": "development", **pol(tmp_path, {"dept-a": True})}
    assert _ids(ok_env)["pii.policy"].level == "PASS"
    assert _ids({"WIKI_ENV": "development", "WIKI_PII_POLICY_FILE": str(tmp_path / "x")})["pii.policy"].level == "FAIL"


def test_pipeline_cli_refuses_to_start_on_bad_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WIKI_PII_POLICY_FILE", str(tmp_path / "missing.json"))
    assert pipeline_main(["--once"]) == 2
    assert "startup refused" in capsys.readouterr().err
    monkeypatch.delenv("WIKI_PII_POLICY_FILE")
    monkeypatch.setenv("WIKI_OCR_COMMAND", "Z:/nope.exe")
    assert pipeline_main(["--once"]) == 2
