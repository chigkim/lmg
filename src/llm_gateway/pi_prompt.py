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
# Underscores are separators here, unlike Python's regex word boundaries.
PI_WORD = re.compile(r"(?<![^\W_])[Pp]i(?![^\W_])")


def adapt_pi_prompt(system):
    """Return the append for Claude Code's preset, or None for unrecognized prompts. Keep rendered rules and content outside the generated scaffold, then remove Pi/pi words with punctuation and underscores treated as separators."""
    adapted, count = PI_SCAFFOLD.subn(lambda match: match.group("rules"), system)
    return PI_WORD.sub("", adapted) if count else None
