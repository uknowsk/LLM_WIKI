"""Brain profiles and the router: choose the LLM (claude / gemini / gauss / local) or let it switch by itself, never sending a
data class to a brain that is not cleared for it, and never leaking one profile's key or hosts into another."""
import json
from pathlib import Path

import pytest

from llmwiki.brains import BrainError, BrainPolicyError, BrainRouter, load_profiles, profile_env, router_from_env
from llmwiki.config import load_settings
from llmwiki.engine.llm import ContextExceeded, LLMError

REPO = Path(__file__).resolve().parent.parent
ALL = ["public", "personal", "company", "private"]
CFG = {"default": "auto", "brains": {
    "local": {"base_url": "http://127.0.0.1:1234/v1", "model": "m-local", "cost": 0, "data": ALL},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "m-gem", "cost": 0.5,
               "data": ["public", "personal"], "allowed_hosts": ["generativelanguage.googleapis.com"], "api_key_file": "gem.key"},
    "gauss": {"base_url": "https://gauss.corp.example/v1", "model": "m-gauss", "cost": 1, "data": ["public", "personal", "company"],
              "allowed_hosts": ["gauss.corp.example"], "api_key_file": "gauss.key", "env": {"WIKI_LLM_STREAM": "1"}},
    "claude": {"custom_config": "config/llm-claude.example.json", "model": "m-claude", "cost": 2, "data": ["public", "personal"],
               "allowed_hosts": ["api.anthropic.com"], "api_key_file": "claude.key"},
}}
SETTINGS = load_settings({"WIKI_ENV": "test"})


class Brain:
    """Looks its error up at call time, so a test can heal or break a brain after the client was created."""

    def __init__(self, name, calls, errors=None):
        self.name, self.calls, self.errors = name, calls, errors if errors is not None else {}

    def complete(self, system, prompt, temperature=None, response_format=None):
        self.calls.append(self.name)
        if self.errors.get(self.name):
            raise self.errors[self.name]
        return f"answer-from-{self.name}"


def make_router(tmp_path, choice="auto", data="company", errors=None, **kw):
    profiles, _ = load_profiles(CFG, base=tmp_path)
    calls: list[str] = []
    errors = errors or {}
    r = BrainRouter(profiles, SETTINGS, choice=choice, data=data,
                    factory=lambda p, s: Brain(p.name, calls, errors), **kw)
    return r, calls


def test_auto_picks_cheapest_brain_cleared_for_the_data_class(tmp_path):
    for data, expected in {"personal": ["local", "gemini", "gauss", "claude"], "company": ["local", "gauss"],
                           "private": ["local"], "public": ["local", "gemini", "gauss", "claude"]}.items():
        r, _ = make_router(tmp_path, data=data)
        assert r.candidates() == expected, data


def test_explicit_choice_is_honored_and_never_falls_through_a_policy_wall(tmp_path):
    r, calls = make_router(tmp_path, choice="gemini", data="personal")
    assert r.complete("s", "p") == "answer-from-gemini" and calls == ["gemini"] and r.last_used == "gemini"
    with pytest.raises(BrainPolicyError):  # company documents may not go to gemini, and nothing else is tried instead
        make_router(tmp_path, choice="gemini", data="company")[0].complete("s", "p")


def test_failover_in_auto_mode_but_never_in_explicit_mode(tmp_path):
    r, calls = make_router(tmp_path, data="personal", errors={"local": LLMError("down"), "gemini": LLMError("HTTP 429")})
    assert r.complete("s", "p") == "answer-from-gauss" and calls == ["local", "gemini", "gauss"] and r.last_used == "gauss"
    r2, calls2 = make_router(tmp_path, choice="local", data="personal", errors={"local": LLMError("down")})
    with pytest.raises(LLMError):
        r2.complete("s", "p")
    assert calls2 == ["local"]


def test_context_exceeded_is_not_a_reason_to_switch(tmp_path):
    r, calls = make_router(tmp_path, data="personal", errors={"local": ContextExceeded("too long", 4096)})
    with pytest.raises(ContextExceeded):
        r.complete("s", "p")
    assert calls == ["local"]


def test_a_failed_brain_rests_for_the_cooldown_then_is_tried_again(tmp_path):
    now = [100.0]
    errors = {"local": LLMError("down")}
    r, calls = make_router(tmp_path, data="personal", errors=errors, clock=lambda: now[0], cooldown=60.0)
    r.complete("s", "p")
    r.complete("s", "p")
    assert calls == ["local", "gemini", "gemini"]  # the second call skipped the resting brain
    now[0] += 61
    errors.clear()
    assert r.complete("s", "p") == "answer-from-local"


def test_all_brains_failing_reports_names_and_error_types_only(tmp_path):
    err = {n: LLMError("secret prompt text SECRET-XYZ") for n in ("local", "gemini", "gauss", "claude")}
    r, _ = make_router(tmp_path, data="personal", errors=err)
    with pytest.raises(LLMError) as e:
        r.complete("s", "the user's prompt")
    msg = str(e.value)
    assert "local" in msg and "gemini" in msg and "LLMError" in msg
    assert "SECRET-XYZ" not in msg and "user's prompt" not in msg


def test_unusable_profile_is_skipped_in_auto_and_an_error_when_chosen(tmp_path):
    profiles, _ = load_profiles(CFG, base=tmp_path)
    calls: list[str] = []

    def factory(p, s):
        if p.name == "local":
            raise ValueError("WIKI_LLM_API_KEY_FILE cannot be read")
        return Brain(p.name, calls)

    assert BrainRouter(profiles, SETTINGS, choice="auto", data="personal", factory=factory).complete("s", "p") == "answer-from-gemini"
    with pytest.raises(BrainError):
        BrainRouter(profiles, SETTINGS, choice="local", data="personal", factory=factory).complete("s", "p")


def test_each_profile_gets_an_isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_LLM_API_KEY", "LEAKED-KEY")
    monkeypatch.setenv("WIKI_LLM_ALLOWED_HOSTS", "evil.example")
    monkeypatch.setenv("WIKI_LLM_PROXY", "http://proxy.example:8080")
    profiles, _ = load_profiles(CFG, base=tmp_path)
    env = profile_env(profiles["gemini"])
    assert env["WIKI_LLM_ALLOWED_HOSTS"] == "generativelanguage.googleapis.com"
    assert env["WIKI_LLM_API_KEY_FILE"] == str(tmp_path / "gem.key")
    assert "WIKI_LLM_API_KEY" not in env and "WIKI_LLM_PROXY" not in env and "evil.example" not in json.dumps(env)
    assert profile_env(profiles["gauss"])["WIKI_LLM_STREAM"] == "1" and "WIKI_LLM_STREAM" not in profile_env(profiles["gemini"])
    assert profile_env(profiles["claude"])["WIKI_LLM_PROVIDER"] == "custom"


@pytest.mark.parametrize("mutate", [
    lambda c: c["brains"]["local"].update(colour="red"),
    lambda c: c["brains"]["local"].update(data=["public", "secret"]),
    lambda c: c["brains"]["local"].update(data=[]),
    lambda c: c["brains"]["local"].update(cost="cheap"),
    lambda c: c["brains"]["local"].pop("model"),
    lambda c: c["brains"]["local"].pop("base_url"),
    lambda c: c["brains"]["local"].update(env={"WIKI_LLM_API_KEY": "inline-secret"}),
    lambda c: c["brains"]["local"].update(env={"WIKI_LLM_BASE_URL": "http://elsewhere/v1"}),
    lambda c: c["brains"]["local"].update(env={"PATH": "x"}),
    lambda c: c["brains"]["local"].update(allowed_hosts=["https://host/path"]),
    lambda c: c.update(default="nope"),
    lambda c: c.update(brains={}),
])
def test_invalid_config_is_rejected_with_a_clear_error(tmp_path, mutate):
    cfg = json.loads(json.dumps(CFG))
    mutate(cfg)
    with pytest.raises(BrainError):
        load_profiles(cfg, base=tmp_path)


def test_router_from_env_reads_choice_file_and_data_class(tmp_path):
    f = tmp_path / "brains.json"
    f.write_text(json.dumps(CFG), encoding="utf-8")
    env = {"WIKI_BRAIN": "auto", "WIKI_BRAINS_FILE": str(f)}
    r = router_from_env(SETTINGS, env, factory=lambda p, s: Brain(p.name, []))
    assert r.data == "company" and r.candidates() == ["local", "gauss"]  # fail closed: the engine defaults to company data
    r2 = router_from_env(SETTINGS, {**env, "WIKI_BRAIN_DATA": "personal", "WIKI_BRAIN": "claude"}, factory=lambda p, s: Brain(p.name, []))
    assert r2.candidates() == ["claude"]
    with pytest.raises(BrainPolicyError):  # explicit brain not cleared for company data fails at construction time
        router_from_env(SETTINGS, {**env, "WIKI_BRAIN": "gemini"}, factory=lambda p, s: Brain(p.name, []))
    with pytest.raises(BrainError):
        router_from_env(SETTINGS, {"WIKI_BRAIN": "auto", "WIKI_BRAINS_FILE": str(tmp_path / "missing.json")})
    with pytest.raises(BrainError):
        router_from_env(SETTINGS, {**env, "WIKI_BRAIN_DATA": "everything"})


def test_for_data_returns_a_stricter_router(tmp_path):
    r, _ = make_router(tmp_path, data="personal")
    assert r.for_data("private").candidates() == ["local"] and len(r.candidates()) == 4


def test_the_factories_use_the_router_when_wiki_brain_is_set(tmp_path, monkeypatch):
    from llmwiki.engine.llm import OpenAICompatClient, llm_from_env
    f = tmp_path / "brains.json"
    f.write_text(json.dumps(CFG), encoding="utf-8")
    env = {"WIKI_BRAIN": "local", "WIKI_BRAINS_FILE": str(f)}
    assert isinstance(llm_from_env(SETTINGS, env), BrainRouter)
    monkeypatch.setenv("WIKI_BRAIN", "local")
    monkeypatch.setenv("WIKI_BRAINS_FILE", str(f))
    assert isinstance(OpenAICompatClient.from_settings(SETTINGS), BrainRouter)
    monkeypatch.delenv("WIKI_BRAIN")
    assert not isinstance(OpenAICompatClient.from_settings(SETTINGS), BrainRouter)  # unchanged without the option


def test_doctor_points_to_the_brains_tool_instead_of_inspecting_a_single_client():
    from llmwiki.doctor.checks_llm import build_llm
    client, results = build_llm(SETTINGS, {"WIKI_BRAIN": "auto"})
    assert client is None and [(r.id, r.level) for r in results] == [("llm.brain", "WARN")]
    assert "llmwiki.brains" in results[0].hint


def test_real_clients_are_built_from_the_profiles_without_any_network_call(tmp_path):
    for n in ("gem", "gauss", "claude"):
        (tmp_path / f"{n}.key").write_text("KEY-" + n, encoding="utf-8")
    cfg = json.loads(json.dumps(CFG))
    cfg["brains"]["claude"]["custom_config"] = str(REPO / "config" / "llm-claude.example.json")
    profiles, _ = load_profiles(cfg, base=tmp_path)
    r = BrainRouter(profiles, SETTINGS, choice="auto", data="personal")  # default factory = the real provider code
    clients = {n: r._client(n) for n in ("local", "gemini", "gauss", "claude")}
    assert clients["gemini"].base_url.startswith("https://generativelanguage.googleapis.com") and clients["gemini"].model == "m-gem"
    assert clients["gauss"].stream is True and clients["gauss"].base_url == "https://gauss.corp.example/v1"
    req = clients["claude"]._build("SYSTEM TEXT", "USER TEXT", 0.0, None)  # the exact request Claude's API would get
    body = json.loads(req.data.decode("utf-8"))
    assert req.full_url == "https://api.anthropic.com/v1/messages" and req.get_header("X-api-key") == "KEY-claude"
    assert req.get_header("Anthropic-version") and body["model"] == "m-claude" and body["system"] == "SYSTEM TEXT"
    assert body["messages"] == [{"role": "user", "content": "USER TEXT"}] and body["max_tokens"] > 0
