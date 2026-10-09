"""~/.lmg, where lmg keeps what is yours rather than the tool's: its key, settings, logs, ledgers, and the folder
Claude Code runs in. Found from the home folder, so every command and checkout shares it whatever folder it runs in."""
import pathlib

HOME = pathlib.Path.home() / ".lmg"
