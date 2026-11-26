"""check-operator-flags.py: every flag the operator slice passes is delivered and accepted.

The gate reads three things: the slice's values (the flags set), the rendered
operator Deployment (the flags delivered), and the sibling checkout's flag
source (the flags defined). These tests build all three, point the gate at
them, and hold its verdicts: a fit passes and says what it compared, a flag
dropped on either side fails, and each input that is missing or unreadable is
refused by name rather than read as a fit over nothing.
"""

import io
import json
import sys

import pytest

GATE = "scripts/check-operator-flags.py"

DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata: {name: operator}
spec:
  template:
    spec:
      containers:
        - name: manager
          image: ghcr.io/nanohype/eks-agent-platform/operator:dev
          args: [%s]
"""

DEFINES = ('flag.BoolVar(&disableAWS, "disable-aws", false, "")\n'
           'flag.BoolVar(&leaderElect, "leader-elect", true, "")\n')


@pytest.fixture
def flags(load_script):
    return load_script(GATE)


@pytest.fixture
def workspace(flags, tmp_path, monkeypatch):
    """(render dir, checkout dir), with the gate's ROOT at a tree holding a fitting slice."""
    root = tmp_path / "kx"
    (root / "stack" / "ai-platform" / "operator").mkdir(parents=True)
    (root / flags.MANIFEST).write_text(json.dumps(
        {"siblings": {flags.SIBLING: {"flagSource": "operators/cmd/main.go"}}}))
    (root / flags.VALUES).write_text("extraArgs:\n  - --disable-aws\n")
    render = tmp_path / "render"
    render.mkdir()
    (render / "ai-platform__operator.yaml").write_text(DEPLOYMENT % "--disable-aws")
    checkout = tmp_path / "checkout"
    (checkout / "operators" / "cmd").mkdir(parents=True)
    (checkout / "operators" / "cmd" / "main.go").write_text(DEFINES)
    monkeypatch.setattr(flags, "ROOT", root)
    return root, render, checkout


def run(flags, monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", [GATE, *map(str, args)])
    status = flags.main()
    out, err = capsys.readouterr()
    return status, out, err


@pytest.fixture
def gate(flags, workspace, monkeypatch, capsys):
    """The workspace's paths, and the gate run against them."""
    root, render, checkout = workspace

    def against():
        return run(flags, monkeypatch, capsys, "--render", render, "--chart-source", checkout)

    return root, render, checkout, against


class TestVerdict:
    def test_a_flag_delivered_and_defined_fits(self, flags, workspace, monkeypatch, capsys):
        _, render, checkout = workspace
        status, out, _ = run(flags, monkeypatch, capsys, "--render", render,
                             "--chart-source", checkout)
        assert status == 0
        assert out == ("check-operator-flags: 1 flag(s) in extraArgs, each carried by the "
                       "rendered operator Deployment's arguments and each among the 2 "
                       "operators/cmd/main.go defines.\n")

    def test_a_flag_the_render_drops(self, flags, workspace, monkeypatch, capsys):
        _, render, checkout = workspace
        (render / "ai-platform__operator.yaml").write_text(DEPLOYMENT % "--leader-elect")
        status, _, err = run(flags, monkeypatch, capsys, "--render", render,
                             "--chart-source", checkout)
        assert status == 1
        assert "check-operator-flags: 1 problem(s):" in err
        assert "--disable-aws" in err

    def test_a_flag_the_binary_no_longer_defines(self, flags, workspace, monkeypatch, capsys):
        _, render, checkout = workspace
        (checkout / "operators" / "cmd" / "main.go").write_text(
            'flag.BoolVar(&awsDisabled, "aws-disabled", false, "")\n'
            'flag.BoolVar(&leaderElect, "leader-elect", true, "")\n')
        status, _, err = run(flags, monkeypatch, capsys, "--render", render,
                             "--chart-source", checkout)
        assert status == 1
        assert "--disable-aws" in err


class TestRefusals:
    def test_both_authorities_are_required(self, flags, workspace, monkeypatch, capsys):
        _, render, _ = workspace
        status, _, err = run(flags, monkeypatch, capsys, "--render", render)
        assert status == 1
        assert "both --render and --chart-source are required" in err

    def test_a_gate_that_has_not_proven_itself_refuses(self, flags, workspace, monkeypatch,
                                                       capsys):
        _, render, checkout = workspace
        monkeypatch.setattr(flags, "self_test", lambda: 1)
        status, _, err = run(flags, monkeypatch, capsys, "--render", render,
                             "--chart-source", checkout)
        assert status == 1
        assert "has not proven it rejects" in err

    def test_self_test_flag(self, flags, monkeypatch, capsys):
        status, out, _ = run(flags, monkeypatch, capsys, "--self-test")
        assert status == 0

    @pytest.mark.parametrize("breaks,says", [
        (lambda root, render, checkout: (checkout / "operators" / "cmd" / "main.go").unlink()
         or (checkout / "operators" / "cmd").rmdir() or (checkout / "operators").rmdir()
         or checkout.rmdir(),
         "no eks-agent-platform checkout at"),
        (lambda root, render, checkout: [p.unlink() for p in render.iterdir()]
         and render.rmdir(),
         "no render at"),
        (lambda root, render, checkout: (root / "stack" / "upstream.json").unlink(),
         "no manifest at"),
        (lambda root, render, checkout: (root / "stack" / "upstream.json").write_text(
            json.dumps({"siblings": {}})),
         "names no flagSource for eks-agent-platform"),
        (lambda root, render, checkout: (checkout / "operators" / "cmd" / "main.go").unlink(),
         "operators/cmd/main.go is not in the checkout at"),
        (lambda root, render, checkout: (checkout / "operators" / "cmd" / "main.go").write_text(
            "package main\n"),
         "extracted no flag definitions from operators/cmd/main.go"),
        (lambda root, render, checkout: (root / "stack" / "ai-platform" / "operator" /
                                         "values.yaml").unlink(),
         "no values file at"),
        (lambda root, render, checkout: (root / "stack" / "ai-platform" / "operator" /
                                         "values.yaml").write_text("image:\n  tag: dev\n"),
         "sets no flag in extraArgs"),
        (lambda root, render, checkout: (render / "ai-platform__operator.yaml").write_text(
            DEPLOYMENT.replace("eks-agent-platform/operator", "other/image") % "--disable-aws"),
         "no Deployment running ghcr.io/nanohype/eks-agent-platform/operator"),
    ], ids=["no-checkout", "no-render", "no-manifest", "no-flag-source-named",
            "flag-source-moved", "no-definitions-extracted", "no-values", "no-flags-set",
            "no-operator-in-render"])
    def test_an_input_it_cannot_read_is_named(self, gate, breaks, says):
        root, render, checkout, against = gate
        breaks(root, render, checkout)
        status, _, err = against()
        assert status == 1
        assert says in err


def test_a_bare_equals_in_a_rendered_crd_does_not_lose_the_document(flags, workspace,
                                                                    monkeypatch, capsys):
    _, render, checkout = workspace
    (render / "core__crds.yaml").write_text("kind: CustomResourceDefinition\nspec:\n"
                                            "  enum:\n    - =\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    status, out, _ = run(flags, monkeypatch, capsys, "--render", render,
                         "--chart-source", checkout)
    assert status == 0
