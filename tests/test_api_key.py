"""Gateway keys use ~/.lmg/.env, never the caller's working directory."""
from pathlib import Path

import pytest
from dotenv import dotenv_values

from llm_gateway import gateway


def test_dotenv_path_is_in_lmg_home():
    assert gateway.DOTENV == Path.home() / ".lmg" / ".env"


@pytest.fixture
def key_paths(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    caller = tmp_path / "caller"
    repo.mkdir()
    caller.mkdir()
    monkeypatch.setattr(gateway, "DOTENV", repo / ".env")
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.chdir(caller)
    return repo / ".env", caller / ".env"


def test_repository_key_is_used_instead_of_caller_or_parent_key(key_paths):
    repo, caller = key_paths
    repo.write_text("GATEWAY_API_KEY=repo-key\n", encoding="utf-8")
    caller.write_text("GATEWAY_API_KEY=caller-key\n", encoding="utf-8")
    caller.parent.parent.joinpath(".env").write_text("GATEWAY_API_KEY=parent-key\n", encoding="utf-8")
    assert gateway.api_key() == "repo-key"


def test_environment_key_takes_priority_without_creating_dotenv(key_paths, monkeypatch):
    repo, caller = key_paths
    monkeypatch.setenv("GATEWAY_API_KEY", "environment-key")
    assert gateway.api_key() == "environment-key"
    assert not repo.exists()
    assert not caller.exists()


@pytest.mark.parametrize("existing", [None, "OTHER=value", "OTHER=value\n"])
def test_generated_key_is_saved_only_in_repository_and_reused(key_paths, existing, capsys):
    repo, caller = key_paths
    caller.write_text("GATEWAY_API_KEY=caller-key\n", encoding="utf-8")
    if existing is not None:
        repo.write_text(existing, encoding="utf-8")
    key = gateway.api_key()
    assert key and key != "caller-key"
    assert dotenv_values(repo)["GATEWAY_API_KEY"] == key
    assert gateway.api_key() == key
    assert caller.read_text(encoding="utf-8") == "GATEWAY_API_KEY=caller-key\n"
    if existing is not None:
        assert repo.read_text(encoding="utf-8").startswith("OTHER=value\n")
    assert key not in capsys.readouterr().err
