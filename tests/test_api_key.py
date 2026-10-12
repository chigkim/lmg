"""Gateway keys come from the environment or config.toml, never the caller's working directory."""
import pytest

from llm_gateway import gateway


@pytest.fixture
def config(tmp_path, monkeypatch):
    caller = tmp_path / "caller"
    caller.mkdir()
    caller.joinpath(".env").write_text("GATEWAY_API_KEY=caller-key\n", encoding="utf-8")
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.chdir(caller)
    return tmp_path / "home" / "config.toml"


def test_missing_config_is_written_with_the_default_key(config):
    assert gateway.api_key(config) == gateway.DEFAULT_KEY == "lmg-proxy"
    assert config.read_text(encoding="utf-8") == gateway.DEFAULT_CONFIG.read_text(encoding="utf-8")


def test_config_key_is_used_instead_of_caller_dotenv(config):
    config.parent.mkdir()
    config.write_text('[proxy]\napi_key = " config-key "\n', encoding="utf-8")
    assert gateway.api_key(config) == "config-key"


@pytest.mark.parametrize("text", ["", "[proxy]\n", '[proxy]\napi_key = ""\n'])
def test_config_without_key_uses_the_default(config, text):
    config.parent.mkdir()
    config.write_text(text, encoding="utf-8")
    assert gateway.api_key(config) == "lmg-proxy"


def test_environment_key_takes_priority(config, monkeypatch):
    config.parent.mkdir()
    config.write_text('[proxy]\napi_key = "config-key"\n', encoding="utf-8")
    monkeypatch.setenv("GATEWAY_API_KEY", "environment-key")
    assert gateway.api_key(config) == "environment-key"


def test_key_that_is_not_a_string_is_refused(config):
    config.parent.mkdir()
    config.write_text("[proxy]\napi_key = 123\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="api_key"):
        gateway.api_key(config)
