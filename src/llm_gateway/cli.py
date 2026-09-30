"""lmg: the gateway, the jobs handed to agy, Claude Code and Codex, and the plans they spend.

    lmg proxy [run|start|stop|restart|status]    the gateway: daemon.py
    lmg ask agy|claude|codex read|edit PROMPT [ID]   a job, and review, clean and list: ask.py
    lmg usage [NOTE]                             each plan's windows, and a ledger row with a note: usage.py
    lmg usage plan ...                           every call each plan was spent on: plan.py
    lmg usage pace ...                           what a kind of task costs, and how many more fit: pace.py
    lmg usage worth                              what each window is worth in dollars: plan.py

Each form's own options follow its words; `lmg proxy --help` and `lmg ask --help` list them.
"""
import sys


def main(argv=None):
    # Each group imports only its own modules: pace.py reads your pace.toml on import and exits on a bad one,
    # which is no reason for the gateway not to start or stop.
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
        if rest and rest[0] == "plan":
            from . import plan
            return plan.main(rest[1:])
        if rest and rest[0] == "pace":
            from . import pace
            return pace.main(rest[1:])
        if rest and rest[0] == "worth":
            from . import plan
            return plan.worth(rest[1:])
        from . import usage
        return usage.main(rest)
    print(f"unknown {group}: proxy, ask or usage", file=sys.stderr)
    return 2
