"""How a gate says why it stopped.

A gate's exit status is its verdict, and the line it prints is the reason. Each
gate binds these to its own name and its own exit codes. The exit status is a
gate's contract with whatever runs it, and the gates do not share one.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Callable
from typing import NoReturn


def say(gate: str, msg: str) -> None:
    """One line on stderr, naming the gate it came from."""
    print(f"{gate}: {msg}", file=sys.stderr)


def refuse(gate: str, msg: str, code: int = 1) -> int:
    """Say why, and hand back the status for a caller that returns it."""
    say(gate, msg)
    return code


def die(gate: str, msg: str, code: int) -> NoReturn:
    """Say why, and stop the gate with that status."""
    say(gate, msg)
    sys.exit(code)


def guarded(main: Callable[[], int], gate: str, code: int) -> int:
    """Run a gate's main, and turn a command that outran its bound into a refusal.

    Every subprocess call a gate makes carries a timeout. One that expires
    raises, and an uncaught exception reads as the gate breaking rather than as
    the fact it is: a command this run could not finish. That fact is reported
    as such, with the command and its bound, under the status the gate gives
    for a run it could not complete.
    """
    try:
        return main()
    except subprocess.TimeoutExpired as e:
        cmd = e.cmd if isinstance(e.cmd, str) else shlex.join(str(c) for c in e.cmd)
        return refuse(gate, f"`{cmd}` did not finish within {e.timeout:g}s, so this run "
                            f"could not complete. That says nothing about the tree.", code)
