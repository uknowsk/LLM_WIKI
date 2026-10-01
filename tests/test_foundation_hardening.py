import pytest

from llmwiki.acl import can_read
from llmwiki.auth import DevAuthProvider, User, get_provider
from llmwiki.config import load_settings

U = User("u1", "Kim", "dept-a", None, frozenset({"dept-a"}))


@pytest.mark.parametrize("env", ["Production", "PRODUCTION ", "prod", "", "staging", None])
def test_dev_auth_refused_unless_dev_or_test(env):
    e = {"WIKI_AUTH_PROVIDER": "dev"}
    if env is not None:
        e["WIKI_ENV"] = env
    with pytest.raises(RuntimeError):
        get_provider(load_settings(e), {"u1": U})


def test_unset_env_defaults_to_production():
    assert load_settings({}).env == "production"


@pytest.mark.parametrize("env", ["development", " Development ", "TEST"])
def test_dev_auth_allowed_in_dev_and_test(env):
    p = get_provider(load_settings({"WIKI_ENV": env}), {"u1": U})
    assert p.authenticate({"user_id": "u1"}) == U


def test_dev_authenticate_never_raises():
    p = DevAuthProvider({"u1": U})
    assert p.authenticate({"user_id": ["x"]}) is None
    assert p.authenticate({"user_id": {"a": 1}}) is None
    assert p.authenticate({}) is None


@pytest.mark.parametrize("v,exp", [("1", True), ("true", True), ("YES", True), ("On", True),
                                   ("0", False), ("", False), ("no", False)])
def test_mask_pii_env_values(v, exp):
    assert load_settings({"WIKI_MASK_PII": v}).mask_pii_default is exp


def test_can_read_rejects_bare_string():
    with pytest.raises(TypeError):
        can_read(U, "dept-a")
    assert can_read(U, ["dept-a"])
    assert not can_read(U, ["dept-a/part-1"])
