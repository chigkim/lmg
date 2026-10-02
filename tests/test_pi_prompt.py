"""Pi prompt adaptation uses fake SDK clients only: no Claude process or paid request."""
import asyncio

import pytest

from llm_gateway import gateway
from llm_gateway.pi_prompt import PI_PREAMBLE, adapt_pi_prompt


def pi_prompt(tail=""):
    return (PI_PREAMBLE + "\n\n<tools>\n- read: Read files\n</tools>\n\n"
            "<rules>\n- Use read to inspect files\n- Keep project policy\n</rules>\n\n"
            "<docs>\nPi documentation (read only when the user asks about pi itself, its SDK, extensions, themes, skills, or TUI):\n"
            "- Main documentation: D:/pi/README.md\n"
            "- custom providers (docs/custom-provider.md), pi packages (docs/packages.md)\n</docs>" + tail)


def test_only_generated_scaffold_is_replaced():
    tail = ("\n\n<addendum>\nUser instructions\n</addendum>\n\n"
            "<project_context>\n<project_instructions path=\"AGENTS.md\">\nProject policy\n</project_instructions>\n</project_context>\n\n"
            "<skills>\nRead D:/skills/example/SKILL.md\n</skills>\n\n<cwd>\nD:/project\n</cwd>\n\n"
            "<custom_section>\nExtension instructions\n</custom_section>")
    adapted = adapt_pi_prompt(pi_prompt(tail))
    assert adapted == "<rules>\n- Use read to inspect files\n- Keep project policy\n</rules>" + tail
    assert PI_PREAMBLE not in adapted
    assert "docs/packages.md" not in adapted
    assert "<tools>" not in adapted


@pytest.mark.parametrize("text, expected", [
    ("Pi pi", " "),
    ("Pi-agent pi-agent", "-agent -agent"),
    ("Pi_tools pi_tools", "_tools _tools"),
    ("agent-Pi agent_pi _pi_", "agent- agent_ __"),
    ("(Pi), 'pi'; Pi/pi.", "(), ''; /."),
    ("pipeline spin api Pi2 2pi épi pié", "pipeline spin api Pi2 2pi épi pié"),
])
def test_pi_words_are_removed_with_punctuation_and_underscore_boundaries(text, expected):
    tail = "\n\n<addendum>\n" + text + "\n</addendum>"
    assert adapt_pi_prompt(pi_prompt(tail)) == adapt_pi_prompt(pi_prompt()) + "\n\n<addendum>\n" + expected + "\n</addendum>"


def test_pi_words_are_removed_from_rules_and_wrappers():
    raw = "Pi wrapper\n" + pi_prompt().replace("Keep project policy", "Keep pi_policy") + "\npi-footer"
    adapted = adapt_pi_prompt(raw)
    assert adapted.startswith(" wrapper\n<rules>")
    assert "Keep _policy" in adapted
    assert adapted.endswith("\n-footer")


def test_wrappers_and_repeated_embedded_pi_prompts_are_preserved():
    raw = "Wrapper instructions\n" + pi_prompt() + "\nChild instructions\n" + pi_prompt() + "\nFinal instructions"
    adapted = adapt_pi_prompt(raw)
    assert adapted.startswith("Wrapper instructions\n<rules>")
    assert "\nChild instructions\n<rules>" in adapted
    assert adapted.endswith("\nFinal instructions")
    assert PI_PREAMBLE not in adapted


def test_crlf_and_portable_text_are_not_normalized():
    raw = pi_prompt("\n\n<addendum>\nKeep spacing  \n</addendum>").replace("\n", "\r\n")
    adapted = adapt_pi_prompt(raw)
    assert adapted == "<rules>\r\n- Use read to inspect files\r\n- Keep project policy\r\n</rules>\r\n\r\n<addendum>\r\nKeep spacing  \r\n</addendum>"


@pytest.mark.parametrize("system", [
    "", "You are a helpful assistant.", "You are running inside pi.",
    "Summarize this conversation.",
    "Explain docs/custom-provider.md and docs/packages.md.",
    "<tools>\nRead files\n</tools>\n<rules>\nBe concise\n</rules>\n<docs>\nDocs\n</docs>",
    PI_PREAMBLE,
    pi_prompt().replace("Pi documentation (read only", "My documentation (read only"),
    pi_prompt().replace("</tools>", ""),
])
def test_unrecognized_and_summary_prompts_are_untouched(system):
    assert adapt_pi_prompt(system) is None


def sdk_options(monkeypatch, tmp_path, systems):
    seen = []

    class Client:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            options = self.options
            path = (options.extra_args["append-system-prompt-file"] if options.system_prompt["type"] == "preset"
                    else options.system_prompt["path"])
            with open(path, encoding="utf-8", newline="") as file:
                seen.append((options, file.read(), path))
            return self

        async def __aexit__(self, *exc):
            return False

        async def receive_messages(self):
            return
            yield

    monkeypatch.setattr(gateway, "ClaudeSDKClient", Client)
    monkeypatch.setattr(gateway, "PROMPTS", tmp_path)
    monkeypatch.setattr(gateway, "log", lambda *parts: None)

    async def run():
        sessions = [gateway.ClaudeSession("sonnet", system, [], None) for system in systems]
        await asyncio.gather(*(session.task for session in sessions))
        return sessions

    sessions = asyncio.run(run())
    assert list(tmp_path.iterdir()) == []
    return seen, sessions


def test_pi_uses_the_preset_and_an_append_file_without_enabling_native_tools(monkeypatch, tmp_path):
    system = pi_prompt("\n\n<addendum>\n" + "x" * 40000 + "\n</addendum>")
    seen, sessions = sdk_options(monkeypatch, tmp_path, [system])
    options, append, path = seen[0]
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert options.extra_args == {"append-system-prompt-file": path}
    assert append == adapt_pi_prompt(system)
    assert options.tools == [] and options.setting_sources == [] and options.skills == []
    assert sessions[0].key == gateway.fingerprint("sonnet", system, [])


@pytest.mark.parametrize("system", ["Other client's prompt\r\n" + "x" * 40000, "Summarize this conversation."], ids=["long-custom", "summary"])
def test_other_prompts_still_use_the_original_file_mode(monkeypatch, tmp_path, system):
    seen, _ = sdk_options(monkeypatch, tmp_path, [system])
    options, sent, path = seen[0]
    assert options.system_prompt == {"type": "file", "path": path}
    assert options.extra_args == {}
    assert sent == system


def test_both_front_doors_deliver_pi_system_text_to_the_same_claude_path():
    system = pi_prompt()
    responses = {"instructions": system, "input": "hello"}
    chat = gateway.chat_to_responses({"messages": [{"role": "system", "content": system}, {"role": "user", "content": "hello"}]})
    assert gateway.entries(responses) == gateway.entries(chat)
    assert adapt_pi_prompt(gateway.entries(chat)[0]) is not None


def test_openai_request_is_not_adapted(monkeypatch):
    sent = []

    async def provider(model, body):
        sent.append((model, body))
        yield {"type": "response.completed"}

    monkeypatch.setitem(gateway.PROVIDERS, "openai", provider)
    body = {"model": "openai/test", "instructions": pi_prompt(), "input": "hello"}

    async def run():
        return [event async for event in gateway.backend(body)]

    asyncio.run(run())
    assert sent == [("test", body)]
    assert sent[0][1]["instructions"] == pi_prompt()
