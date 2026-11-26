"""Fixtures for the Python tests: loading the file under test, and running it.

Nothing kx runs is a package. The gates are scripts, and the installer helpers
are files beside the install.sh that runs them, most with a hyphen in the name.
So a test reaches its subject by path, which also keeps it on the file CI and
the installer execute rather than on a copy.
"""

import dataclasses
import importlib.util
import io
import pathlib
import runpy
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Loading a helper must leave no __pycache__ beside it. check-slice-integrity
# walks every addon directory on disk and reports any entry nothing names, so a
# bytecode cache written there by a test run turns the next integrity run red.
sys.dont_write_bytecode = True


@pytest.fixture
def load_script():
    """Load a tracked Python file as a fresh module, by its path from the repo root.

    Fresh on every call, so module-level state from one test never reaches
    another. The module is registered under a name derived from its path while
    the test runs, because a dataclass or an annotation resolves through
    sys.modules.
    """
    loaded = []

    def load(relpath: str):
        name = "kx_" + "".join(c if c.isalnum() else "_" for c in relpath)
        spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loaded.append(name)
        spec.loader.exec_module(module)
        return module

    yield load
    for name in loaded:
        sys.modules.pop(name, None)


@dataclasses.dataclass
class Run:
    """What a process running the script would have seen."""

    status: int
    out: str
    err: str


@pytest.fixture
def run_script(monkeypatch, capsys):
    """Run a tracked Python file as `python3 <file> <args>` would, with `stdin` on stdin.

    In this process rather than a child, so coverage measures it, and with the
    interpreter's own exit behaviour: SystemExit with a message prints it to
    stderr and exits 1, and one with no code exits 0.
    """

    def run(relpath: str, *args: str, stdin: str = "") -> Run:
        path = ROOT / relpath
        monkeypatch.setattr(sys, "argv", [str(path), *args])
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        status, message = 0, ""
        try:
            runpy.run_path(str(path), run_name="__main__")
        except SystemExit as e:
            if e.code is None:
                status = 0
            elif isinstance(e.code, int):
                status = e.code
            else:
                status, message = 1, f"{e.code}\n"
        out, err = capsys.readouterr()
        return Run(status, out, err + message)

    return run
