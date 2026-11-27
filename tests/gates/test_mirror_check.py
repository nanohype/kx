"""mirror-check.py: kx's chart pins held to the eks-gitops catalog, against trees of its own.

The gate's self-test proves the comparison. These tests drive what surrounds
it, on a kx tree and a catalog checkout written here: reading the manifest and
the checkout, refusing a checkout that is not at the pin, the report a failing
comparison prints, and the rewrite `sync` makes.
"""

import json
import pathlib
import subprocess

import pytest

GATE = "scripts/mirror-check.py"

ADDONS = """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: addons}
spec:
  generators:
    - list:
        elements:
          - {name: a, chart: chart-a, chartVersion: 1.0.0}
          - {name: b, chart: chart-b, chartVersion: 2.0.0}
  template:
    spec:
      source:
        repoURL: https://charts.example
        chart: '{{ .chart }}'
        targetRevision: '{{ .chartVersion }}'
"""

CRDS = """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: crds}
spec:
  template:
    spec:
      source:
        repoURL: https://github.com/o/crds
        path: manifests/crds
        targetRevision: v1.0.0
"""

REPO_ADD = "helm repo add x https://charts.example --force-update >/dev/null\n"
INSTALLERS = {
    "stack/s/a/install.sh":
        REPO_ADD + "helm upgrade --install a x/chart-a --version 1.0.0 --wait --timeout 5m\n",
    "stack/s/b/install.sh":
        REPO_ADD + "# --version 0.9.0 was the pin before this one\n"
        "helm upgrade --install b x/chart-b \\\n  --version 2.0.0 \\\n  --wait --timeout 5m\n",
    # The slices the gate exempts by name, installing from a path with no pin.
    "stack/ai-platform/operator/install.sh": "helm upgrade --install op ./chart --wait --timeout 5m\n",
    "stack/data/druid/install.sh": "helm template d ./chart >manifest.yaml\n",
}


def git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True, timeout=60).stdout.strip()


def commit(root: pathlib.Path) -> str:
    """Commit the catalog as it stands, and return the commit."""
    git(root, "add", "-A")
    git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-q", "-m", "catalog")
    return git(root, "rev-parse", "HEAD")


class Trees:
    """A kx tree and an eks-gitops checkout, with the gate pointed at both."""

    def __init__(self, base: pathlib.Path, mirror, monkeypatch):
        self.kx, self.gitops, self.mirror = base / "kx", base / "eks-gitops", mirror
        (self.gitops / "applicationsets").mkdir(parents=True)
        git(self.gitops, "init", "-q")
        self.catalog(ADDONS, CRDS)
        for rel, body in INSTALLERS.items():
            (self.kx / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.kx / rel).write_text(body)
        self.manifest(ref=self.head)
        monkeypatch.setattr(mirror, "ROOT", self.kx)
        monkeypatch.setattr(mirror, "MANIFEST", self.kx / "stack" / "upstream.json")
        monkeypatch.setenv("EKS_GITOPS_DIR", str(self.gitops))

    def catalog(self, addons: str, crds: str = CRDS) -> None:
        (self.gitops / "applicationsets" / "addons.yaml").write_text(addons)
        (self.gitops / "applicationsets" / "crds.yaml").write_text(crds)
        self.head = commit(self.gitops)

    def manifest(self, ref: str, divergences=(), crd_installers=None) -> None:
        answered = [{"appset": "crds.yaml", "mechanism": "the chart renders them"}]
        doc = {"upstream": {"ref": ref}, "divergences": list(divergences),
               "crdInstallers": answered if crd_installers is None else crd_installers}
        (self.kx / "stack" / "upstream.json").write_text(json.dumps(doc, indent=2) + "\n")

    def read(self) -> dict:
        return self.mirror.load_manifest()


@pytest.fixture
def mirror(load_script):
    return load_script(GATE)


@pytest.fixture
def trees(tmp_path, mirror, monkeypatch):
    return Trees(tmp_path, mirror, monkeypatch)


def refused(capsys, call) -> str:
    """What the gate says as it refuses, having stopped with status 1."""
    with pytest.raises(SystemExit) as stopped:
        call()
    assert stopped.value.code == 1
    return capsys.readouterr().err


class TestCheck:
    def test_a_tree_that_matches_the_catalog(self, trees, capsys):
        trees.mirror.cmd_check(trees.read())
        assert (f"mirror-check: 2 chart(s) match eks-gitops@{trees.head[:12]} (0 declared "
                f"divergence(s), 1 CRD installer(s) answered)") in capsys.readouterr().out

    def test_a_pin_the_catalog_has_moved_past(self, trees, capsys):
        trees.catalog(ADDONS.replace("chartVersion: 2.0.0", "chartVersion: 2.1.0"))
        trees.manifest(ref=trees.head)
        with pytest.raises(SystemExit) as stopped:
            trees.mirror.cmd_check(trees.read())
        assert stopped.value.code == 1
        out = capsys.readouterr().out
        assert "✗ chart-b: kx pins 2.0.0, eks-gitops pins 2.1.0  (stack/s/b/install.sh)" in out
        assert f"mirror-check: 1 slice(s) disagree with eks-gitops@{trees.head[:12]}" in out

    def test_a_catalog_addon_with_no_slice_here(self, trees, capsys):
        trees.catalog(ADDONS.replace(
            "          - {name: b,", "          - {name: c, chart: chart-c, chartVersion: 3.0.0}\n"
                                     "          - {name: b,"))
        trees.manifest(ref=trees.head)
        with pytest.raises(SystemExit):
            trees.mirror.cmd_check(trees.read())
        assert "✗ chart-c: eks-gitops pins 3.0.0, kx has no slice for it" in capsys.readouterr().out

    def test_a_divergence_declared_with_a_reason(self, trees, capsys):
        trees.catalog(ADDONS.replace(
            "          - {name: b,", "          - {name: c, chart: chart-c, chartVersion: 3.0.0}\n"
                                     "          - {name: b,"))
        trees.manifest(ref=trees.head, divergences=[
            {"chart": "chart-c", "kind": "gitops-only", "reason": "no cluster role locally"}])
        trees.mirror.cmd_check(trees.read())
        assert "(1 declared divergence(s)," in capsys.readouterr().out

    def test_a_chart_kx_pins_that_the_catalog_does_not(self, trees, capsys):
        trees.catalog(ADDONS.replace("          - {name: b, chart: chart-b, chartVersion: 2.0.0}\n",
                                     ""))
        trees.manifest(ref=trees.head)
        with pytest.raises(SystemExit):
            trees.mirror.cmd_check(trees.read())
        assert ("✗ chart-b: kx pins 2.0.0, eks-gitops has no entry for it  "
                "(stack/s/b/install.sh)") in capsys.readouterr().out

    def test_a_crd_installer_nothing_here_answers(self, trees, capsys):
        trees.manifest(ref=trees.head, crd_installers=[])
        with pytest.raises(SystemExit):
            trees.mirror.cmd_check(trees.read())
        out = capsys.readouterr().out
        assert ("✗ crds.yaml: eks-gitops installs CRDs from https://github.com/o/crds "
                "(manifests/crds @ v1.0.0)") in out

    def test_an_answer_for_a_crd_installer_that_is_gone(self, trees, capsys):
        trees.manifest(ref=trees.head, crd_installers=[
            {"appset": "crds.yaml"}, {"appset": "gone.yaml"}])
        with pytest.raises(SystemExit):
            trees.mirror.cmd_check(trees.read())
        assert ("✗ gone.yaml: declared in crdInstallers but no longer exists upstream"
                in capsys.readouterr().out)

    def test_a_checkout_that_is_not_at_the_pin(self, trees, capsys):
        trees.manifest(ref="0" * 40)
        err = refused(capsys, lambda: trees.mirror.cmd_check(trees.read()))
        assert f"is on {trees.head[:12]} but the pin is 000000000000" in err

    def test_a_ref_that_is_not_a_commit(self, trees, capsys):
        trees.manifest(ref="main")
        err = refused(capsys, lambda: trees.mirror.cmd_check(trees.read()))
        assert "upstream.ref is main, which is not a commit sha" in err

    def test_no_checkout_to_read(self, trees, capsys, monkeypatch):
        monkeypatch.setenv("EKS_GITOPS_DIR", str(trees.kx / "nowhere"))
        err = refused(capsys, lambda: trees.mirror.cmd_check(trees.read()))
        assert "no eks-gitops checkout at" in err


class TestManifest:
    @pytest.mark.parametrize("body,says", [
        (None, "no manifest at"),
        ("{not json", "is not valid JSON"),
        ('{"divergences": []}', "has no 'upstream'"),
        ('{"upstream": {}}', "has no 'upstream.ref'"),
    ], ids=["missing", "not-json", "no-upstream", "no-ref"])
    def test_a_manifest_the_gate_cannot_read_is_refused_by_name(self, trees, capsys, body, says):
        path = trees.kx / "stack" / "upstream.json"
        if body is None:
            path.unlink()
        else:
            path.write_text(body)
        assert says in refused(capsys, trees.read)


class TestKxPins:
    def test_a_slice_with_no_pin_that_nothing_exempts(self, trees, capsys):
        local = trees.kx / "stack" / "s" / "local" / "install.sh"
        local.parent.mkdir(parents=True)
        local.write_text("helm upgrade --install l ./chart --wait --timeout 5m\n")
        err = refused(capsys, trees.mirror.kx_pins)
        assert "stack/s/local installs a chart with no --version" in err
        assert "1 slice(s) install a chart this parser cannot pin" in err

    def test_an_exemption_that_outlived_its_slice(self, trees, capsys):
        (trees.kx / "stack" / "data" / "druid" / "install.sh").unlink()
        err = refused(capsys, trees.mirror.kx_pins)
        assert "NO_REMOTE_PIN names stack/data/druid, which no longer exists" in err


class TestFreshness:
    def test_a_tree_level_with_the_catalog_head(self, trees, capsys):
        trees.mirror.cmd_freshness(trees.read())
        assert ("mirror-check: kx matches the eks-gitops catalog at its default branch"
                in capsys.readouterr().out)

    def test_a_tree_behind_the_catalog_head(self, trees, capsys):
        trees.catalog(ADDONS.replace("chartVersion: 1.0.0", "chartVersion: 1.1.0"))
        with pytest.raises(SystemExit) as stopped:
            trees.mirror.cmd_freshness(trees.read())
        assert stopped.value.code == 1
        assert ("mirror-check: 1 slice(s) behind the current eks-gitops catalog"
                in capsys.readouterr().out)


class TestSync:
    def test_the_live_pin_moves_and_the_ref_follows(self, trees, capsys):
        pinned = trees.head
        trees.catalog(ADDONS.replace("chartVersion: 2.0.0", "chartVersion: 2.1.0"))
        trees.manifest(ref=pinned, divergences=[
            {"chart": "chart-z", "kind": "kx-only", "reason": "kept here — for a reason"}])
        trees.mirror.cmd_sync(trees.read())
        script = (trees.kx / "stack" / "s" / "b" / "install.sh").read_text()
        assert "  --version 2.1.0 \\\n" in script
        assert "# --version 0.9.0 was the pin before this one\n" in script
        manifest = (trees.kx / "stack" / "upstream.json").read_text()
        assert json.loads(manifest)["upstream"]["ref"] == trees.head
        assert "kept here — for a reason" in manifest
        out = capsys.readouterr().out
        assert "↻ chart-b: 2.0.0 → 2.1.0  (stack/s/b/install.sh)" in out
        assert f"mirror-check: 1 slice(s) re-pinned; upstream.ref now {trees.head[:12]}" in out
        assert "now run: ./scripts/render-check.sh" in out

    def test_a_declared_version_divergence_is_left_as_it_is(self, trees, capsys):
        trees.catalog(ADDONS.replace("chartVersion: 2.0.0", "chartVersion: 2.1.0"))
        trees.manifest(ref=trees.head, divergences=[
            {"chart": "chart-b", "kind": "version", "reason": "held back here"}])
        trees.mirror.cmd_sync(trees.read())
        assert "--version 2.0.0" in (trees.kx / "stack" / "s" / "b" / "install.sh").read_text()
        out = capsys.readouterr().out
        assert "mirror-check: 0 slice(s) re-pinned" in out
        assert "render-check" not in out


class TestCommandLine:
    def test_check_runs_after_the_self_test(self, trees, run_script, monkeypatch):
        monkeypatch.setenv("KX_GATE_ROOT", str(trees.kx))
        run = run_script(GATE, "check")
        assert run.status == 0
        assert f"mirror-check: 2 chart(s) match eks-gitops@{trees.head[:12]}" in run.out

    @pytest.mark.parametrize("argv", [[], ["compare"], ["check", "extra"]],
                             ids=["none", "unknown", "two"])
    def test_anything_but_one_command_is_refused(self, trees, run_script, monkeypatch, argv):
        monkeypatch.setenv("KX_GATE_ROOT", str(trees.kx))
        run = run_script(GATE, *argv)
        assert run.status == 1
        assert "usage: mirror-check.py {check|sync|freshness|--self-test}" in run.err
