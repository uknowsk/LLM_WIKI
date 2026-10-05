"""Settings parsing, --check, UI script rules, and the example env file."""
import json
import re
import threading
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from llmwiki.personal.__main__ import main
from llmwiki.personal.settings import (PersonalConfigError, personal_environ, personal_home, personal_settings,
                                       watch_folders)
from llmwiki.web.ui_js import APP_JS

ROOT = Path(__file__).resolve().parents[1]


def test_home_default_override_and_fallback(tmp_path):
    assert personal_home({"WIKI_PERSONAL_HOME": str(tmp_path / "h")}) == tmp_path / "h"
    assert personal_home({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "LLMWiki"
    assert personal_home({"WIKI_PERSONAL_HOME": " ", "LOCALAPPDATA": str(tmp_path)}) == tmp_path / "LLMWiki"
    assert personal_home({}).name == "LLMWiki"


def test_settings_are_fail_closed_and_data_dir_is_home(tmp_path):
    s = personal_settings({"WIKI_ENV": "development", "WIKI_AUTH_PROVIDER": "dev"}, tmp_path)
    assert s.env == "production" and s.auth_provider == "personal" and s.data_dir == tmp_path
    assert personal_settings({"WIKI_MASK_PII": "1"}, tmp_path).mask_pii_default is True


def test_embeddings_default_to_local_lm_studio_when_chat_is_remote():
    gauss = personal_environ({"WIKI_LLM_BASE_URL": "https://gauss.corp.example/v1"})
    assert gauss["WIKI_EMBED_BASE_URL"] == "http://127.0.0.1:1234/v1"
    local = personal_environ({"WIKI_LLM_BASE_URL": "http://127.0.0.1:1234/v1"})
    assert "WIKI_EMBED_BASE_URL" not in local
    explicit = personal_environ({"WIKI_LLM_BASE_URL": "https://g/v1", "WIKI_EMBED_BASE_URL": "http://127.0.0.1:9/v1"})
    assert explicit["WIKI_EMBED_BASE_URL"] == "http://127.0.0.1:9/v1"


def test_watch_list_formats(tmp_path):
    a, b = tmp_path / "A B", tmp_path / "b"
    assert watch_folders({"WIKI_PERSONAL_WATCH": f"{a};{b};;"}, tmp_path) == [a, b]
    assert watch_folders({"WIKI_PERSONAL_WATCH": json.dumps([str(a), str(a), str(b)])}, tmp_path) == [a, b]
    assert watch_folders({}, tmp_path) == []
    (tmp_path / "settings.json").write_text(json.dumps({"watch": [str(b)]}), encoding="utf-8")
    assert watch_folders({}, tmp_path) == [b]
    assert watch_folders({"WIKI_PERSONAL_WATCH": str(a)}, tmp_path) == [a]  # env wins over the file


@pytest.mark.parametrize("raw", ["relative/dir", '["ok"', '{"a": 1}', "[1, 2]"])
def test_bad_watch_config_is_refused(tmp_path, raw):
    with pytest.raises(PersonalConfigError):
        watch_folders({"WIKI_PERSONAL_WATCH": raw}, tmp_path)


def test_bad_settings_file_is_refused(tmp_path):
    (tmp_path / "settings.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(PersonalConfigError):
        watch_folders({}, tmp_path)


# ---- --check --------------------------------------------------------------------------------------

class _FakeModelServer:
    """OpenAI-compatible stub on a loopback port: /chat/completions and /embeddings."""

    def __init__(self):
        outer = self

        class H(WSGIRequestHandler):
            def log_message(self, *a):
                pass

        def app(environ, start_response):
            body = json.loads(environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0)) or b"{}")
            if environ["PATH_INFO"].endswith("/embeddings"):
                data = {"data": [{"index": i, "embedding": [float(len(t) % 7 + 1), float(ord(t[0]) % 5 + 1), 1.0, 0.5]}
                                 for i, t in enumerate(body["input"])]}
            else:
                data = {"choices": [{"message": {"content": "pong"}}]}
            raw = json.dumps(data).encode()
            start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(raw)))])
            outer.seen_auth = environ.get("HTTP_AUTHORIZATION")
            return [raw]

        self.seen_auth = None
        self.srv = make_server("127.0.0.1", 0, app, handler_class=H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_check_passes_against_a_working_endpoint_and_never_prints_the_key(tmp_path, capsys):
    srv = _FakeModelServer()
    try:
        watch = tmp_path / "Docs"
        watch.mkdir()
        env = {"WIKI_PERSONAL_HOME": str(tmp_path / "home"), "WIKI_LLM_BASE_URL": srv.url, "WIKI_LLM_MODEL": "m",
               "WIKI_LLM_API_KEY": "sk-very-secret-key-123", "WIKI_LLM_CONTEXT_TOKENS": "8192",
               "WIKI_EMBED_BASE_URL": srv.url, "WIKI_PERSONAL_WATCH": str(watch)}
        assert main(["--check", "--no-browser"], env) == 0
        out = capsys.readouterr().out
        assert "sk-very-secret-key-123" not in out and "[FAIL]" not in out
        assert "[PASS]" in out and "루프백 바인드 가능" in out and "감시 폴더 읽기 가능" in out
        assert not (tmp_path / "home").exists()  # --check does not create the data folder
        assert srv.seen_auth == "Bearer sk-very-secret-key-123"
    finally:
        srv.close()


def test_check_fails_with_exit_1_when_endpoints_are_down(tmp_path, capsys):
    env = {"WIKI_PERSONAL_HOME": str(tmp_path / "home"), "WIKI_LLM_BASE_URL": "http://127.0.0.1:9/v1",
           "WIKI_LLM_API_KEY": "sk-zzz-secret-9876", "WIKI_PERSONAL_WATCH": str(tmp_path / "missing")}
    assert main(["--check"], env) == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "sk-zzz-secret-9876" not in out and "[WARN]" in out  # missing watch folder = WARN


def test_check_flags_bad_watch_config_and_unwritable_home(tmp_path, capsys):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    env = {"WIKI_PERSONAL_HOME": str(blocker / "home"), "WIKI_LLM_BASE_URL": "http://127.0.0.1:9/v1",
           "WIKI_PERSONAL_WATCH": "relative"}
    assert main(["--check"], env) == 1
    out = capsys.readouterr().out
    assert "감시 폴더 설정 오류" in out and "쓸 수 없음" in out


# ---- UI script ------------------------------------------------------------------------------------

def test_ui_rules_still_hold_and_personal_features_exist():
    for banned in ("innerHTML", "outerHTML", "eval(", "document.write", "insertAdjacentHTML"):
        assert banned not in APP_JS
    assert "state.me.spaces.length === 1" in APP_JS  # single space: selector hidden, used automatically
    assert "/api/personal/status" in APP_JS and "개인 위키 · 처리 대기" in APP_JS
    assert "문서 처리 중이라 답변이 늦어질 수 있습니다" in APP_JS
    assert "personal ? null : h('button'" in APP_JS  # no logout button in personal mode


# ---- env.personal.example --------------------------------------------------------------------------

def test_every_personal_variable_is_documented_in_the_example_file():
    text = (ROOT / "config" / "env.personal.example").read_text(encoding="utf-8")
    for var in ("WIKI_PERSONAL_HOME", "WIKI_PERSONAL_WATCH", "WIKI_LLM_BASE_URL", "WIKI_LLM_MODEL", "WIKI_LLM_API_KEY_FILE",
                "WIKI_LLM_STRUCTURED", "WIKI_LLM_STREAM", "WIKI_LLM_TIMEOUT", "WIKI_LLM_CONTEXT_TOKENS", "WIKI_EMBED_MODEL",
                "WIKI_EMBED_BASE_URL", "WIKI_MASK_PII", "WIKI_MAX_INPUT_BYTES"):
        assert re.search(rf"^#?\s*{var}=", text, re.M), var
    assert "WIKI_HOST" in text  # documented as ignored
    for line in text.splitlines():  # the example must never carry a real-looking key
        assert not re.match(r"\s*WIKI_LLM_API_KEY=\S+", line)
