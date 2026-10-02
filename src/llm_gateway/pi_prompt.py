"""Adapt Pi's standard tagged prompt without guessing at custom or other clients' prompts."""
import re

PI_PREAMBLE = ("You are an expert coding assistant operating inside pi, a coding agent harness. "
               "You help users by reading files, executing commands, editing code, and writing new files.")
# Require the complete generated scaffold in its canonical order. A mention of Pi, a documentation path, or an unrelated client's XML tags is not enough.
PI_SCAFFOLD = re.compile(
    r"^" + re.escape(PI_PREAMBLE) + r"\r?\n\r?\n"
    r"<tools>\r?\n.*?\r?\n</tools>\r?\n\r?\n"
    r"(?P<rules><rules>\r?\n.*?\r?\n</rules>)\r?\n\r?\n"
    r"<docs>\r?\nPi documentation \(read only when the user asks about pi itself, its SDK, extensions, themes, skills, or TUI\):\r?\n"
    r".*?\r?\n</docs>",
    re.MULTILINE | re.DOTALL,
)


def adapt_pi_prompt(system):
    """Return the append for Claude Code's preset, or None when the prompt is not Pi's standard tagged scaffold. Rendered rules can contain user/tool guidelines, so keep them along with every byte outside the generated preamble, tool inventory, and documentation routing."""
    adapted, count = PI_SCAFFOLD.subn(lambda match: match.group("rules"), system)
    return adapted if count else None
