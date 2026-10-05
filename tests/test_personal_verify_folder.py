"""Fail-closed default: process_file verifies the inbox folder unless a caller explicitly turns it off."""
import inspect
from pathlib import Path

from llmwiki.pipeline import run as pipeline_run
from test_personal_support import make_personal


def test_process_file_verifies_folder_by_default():
    assert inspect.signature(pipeline_run.process_file).parameters["verify_folder"].default is True


def test_personal_passes_verify_folder_false_explicitly(tmp_path, monkeypatch):
    seen = {}

    def fake(path, space, *a, **kw):
        seen.update(kw)
        return pipeline_run.ProcessResult("done", Path(path), space)

    monkeypatch.setattr("llmwiki.personal.runtime.process_file", fake)
    p = make_personal(tmp_path, process=None)
    p.rt.worker._process(tmp_path / "x.md", "personal")
    assert seen["verify_folder"] is False
