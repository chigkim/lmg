"""lmg: the gateway, the jobs handed to agy, Claude Code and Codex, and the plans they spend.

    lmg proxy [run|start|stop|restart|status]    the gateway: daemon.py
    lmg ask agy|claude|codex read|edit PROMPT [ID]   a job, and review, clean and list: ask.py
    lmg usage [NOTE]                             each plan's windows, and a ledger row with a note: usage.py
    lmg usage calls ...                          every call each plan was spent on: calls.py
    lmg usage pace ...                           what a task on each harness costs, and how many more fit: pace.py
    lmg usage worth                              what each window is worth in dollars: calls.py

Each form's own options follow its words; `lmg proxy --help`, `lmg ask --help` and `lmg usage --help` list them.
"""
import sys


def main(argv=None):
    # Each group imports only its own modules, so the gateway starts and stops without loading the rest.
    command_line, argv = argv is None, sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    group, rest = argv[0], argv[1:]
    if group == "proxy":
        from . import daemon
        return daemon.main(rest)
    if group == "ask":
        from . import ask
        return ask.main(rest, command_line=command_line)
    if group == "usage":
        if rest and rest[0] == "calls":
            from . import calls
            return calls.main(rest[1:])
        if rest and rest[0] == "pace":
            from . import pace
            return pace.main(rest[1:])
        if rest and rest[0] == "worth":
            from . import calls
            return calls.worth(rest[1:])
        from . import usage
        return usage.main(rest)
    print(f"unknown {group}: proxy, ask or usage", file=sys.stderr)
    return 2
