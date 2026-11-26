"""check-prose-voice.py: the prose a change adds, surfaced against its base.

The gate reads `git diff base...HEAD` in the working directory, so these tests
build a repository with a base and a change and run the gate's command line in
it. What they hold: added prose is listed and a tally in it is flagged,
vendored schemas and the gate's own file are left out, a change of code lines
alone reports no prose, and a range git cannot compute is refused rather than
read as a change with nothing in it.
"""

import io
import os
import subprocess
import sys

import pytest

GATE = "scripts/check-prose-voice.py"

IDENTITY = {"GIT_AUTHOR_NAME": "kx-test", "GIT_AUTHOR_EMAIL": "kx-test@example.invalid",
            "GIT_COMMITTER_NAME": "kx-test", "GIT_COMMITTER_EMAIL": "kx-test@example.invalid"}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **IDENTITY}, timeout=60)


def change(tmp_path, files: dict[str, str]):
    """A repository whose `base` branch is one commit behind HEAD, which adds `files`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("# kx\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "branch", "base")
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(body)
        git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", "change")
    return repo


@pytest.fixture
def prose(load_script):
    return load_script(GATE)


class TestCommandLine:
    def test_added_prose_is_listed_and_a_tally_in_it_flagged(self, tmp_path, monkeypatch,
                                                              run_script):
        repo = change(tmp_path, {
            "README.md": "This guide explains the stack.\nIt holds for all 34 of them.\n",
            "stack/s/a/values.yaml": "# the key a project overrides\nkey: value\n",
            "schemas/sources.json": "description: vendored text from upstream\n",
            "scripts/check-prose-voice.py": "# TODO quoted by the gate itself\n",
        })
        monkeypatch.chdir(repo)
        run = run_script(GATE, "base")
        assert run.status == 0
        assert "check-prose-voice: 3 prose line(s) added against base." in run.out
        assert "It holds for all 34 of them.\n      -> verification tally" in run.out
        assert "    stack/s/a/values.yaml: # the key a project overrides" in run.out
        assert "key: value" not in run.out
        assert "vendored text" not in run.out
        assert "quoted by the gate itself" not in run.out

    def test_prose_with_no_marker_says_that_is_not_a_pass(self, tmp_path, monkeypatch,
                                                         run_script):
        repo = change(tmp_path, {"README.md": "Every slice installs from its own script.\n"})
        monkeypatch.chdir(repo)
        run = run_script(GATE, "base")
        assert run.status == 0
        assert "No line matched an enforceable marker. That is not a pass" in run.out

    def test_a_change_of_code_lines_alone_adds_no_prose(self, tmp_path, monkeypatch, run_script):
        repo = change(tmp_path, {"stack/s/a/values.yaml": "replicas: 1\nimage.tag: v1.2.3\n"})
        monkeypatch.chdir(repo)
        run = run_script(GATE, "base")
        assert (run.status, run.out) == (0, "check-prose-voice: this change adds no prose "
                                            "against base.\n")

    def test_a_base_with_no_merge_base_is_refused(self, tmp_path, monkeypatch, run_script):
        repo = change(tmp_path, {"README.md": "More prose.\n"})
        monkeypatch.chdir(repo)
        run = run_script(GATE, "origin/a-ref-that-does-not-exist")
        assert run.status == 1
        assert "no merge base between origin/a-ref-that-does-not-exist and HEAD" in run.err
        assert "Refusing to report that a change adds no prose" in run.err

    def test_without_git_it_refuses_rather_than_reporting(self, tmp_path, monkeypatch,
                                                         run_script):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PATH", str(tmp_path))
        run = run_script(GATE, "base")
        assert run.status == 1
        assert "git is not on PATH, so no range can be computed" in run.err

    def test_self_test_flag(self, run_script):
        run = run_script(GATE, "--self-test")
        assert run.status == 0
        assert "case(s) behave as specified" in run.out

    def test_markers_that_stop_matching_stop_the_run(self, prose, monkeypatch, capsys):
        monkeypatch.setattr(prose, "self_test", lambda: (print("  MISSED    a case"), 1)[1])
        monkeypatch.setattr(sys, "argv", [GATE, "base"])
        assert prose.main() == 1
        err = capsys.readouterr().err
        assert "MISSED    a case" in err
        assert "markers are not behaving as specified" in err


class TestSelfTest:
    def test_a_marker_that_misses_and_one_that_over_matches_both_fail_it(self, prose,
                                                                       monkeypatch, capsys):
        monkeypatch.setattr(prose, "MARKERS", [(prose.re.compile(r"PKCS"), "over-matches")])
        assert prose.self_test() == 1
        out = capsys.readouterr().out
        assert "MISSED    # TODO: wire this up" in out
        assert "FALSE +   # PKCS#12 bundles are not supported by this chart  (over-matches)" in out

    def test_control_outcomes_count_both_halves(self, prose):
        outcomes = prose.control_outcomes()
        assert outcomes["ok"] is True
        assert outcomes["rejected"] == 6 and outcomes["accepted"] == 6


def test_a_diff_git_could_not_produce_is_refused(prose, monkeypatch):
    results = iter([subprocess.CompletedProcess([], 0, "abc\n", ""),
                    subprocess.CompletedProcess([], 128, "", "fatal: bad revision")])
    monkeypatch.setattr(prose.subprocess, "run", lambda *a, **k: next(results))
    with pytest.raises(prose.RangeUnavailable, match="git diff against base failed: fatal: "
                                                     "bad revision"):
        prose.added_prose("base")


def test_stdin_is_not_read(prose, monkeypatch, tmp_path):
    repo = change(tmp_path, {"README.md": "Prose.\n"})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not read"))
    assert prose.added_prose("base") == [("README.md", "Prose.")]
