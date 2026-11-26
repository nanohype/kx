"""check-rendered-mounts.py: no container mounts two volumes on one path.

render-check.sh pipes every slice's render into this gate. These tests drive the
same command line with manifests of their own: a render with two mounts on one
path fails its slice and names them, a clean render passes, and a render with
no manifests in it is refused rather than passed.
"""

import io
import sys

import pytest

GATE = "scripts/check-rendered-mounts.py"

CLEAN = """\
apiVersion: apps/v1
kind: Deployment
metadata: {name: api}
spec:
  template:
    spec:
      containers:
        - name: app
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: tmp, mountPath: /tmp}
"""

TWICE = """\
apiVersion: v1
kind: Pod
metadata: {name: loki}
spec:
  initContainers:
    - name: init
      volumeMounts:
        - {name: a, mountPath: /var/loki}
        - {name: b, mountPath: /var/loki}
"""


@pytest.fixture
def mounts(load_script):
    return load_script(GATE)


class TestPodSpecs:
    @pytest.mark.parametrize("doc,spec", [
        ({"kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": []}},
         {"containers": []}),
        ({"kind": "Deployment", "metadata": {"name": "p"},
          "spec": {"template": {"spec": {"containers": []}}}}, {"containers": []}),
        ({"kind": "CronJob", "metadata": {"name": "p"},
          "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": []}}}}}},
         {"containers": []}),
    ], ids=["pod", "deployment", "cronjob"])
    def test_each_workload_kind_yields_its_pod_spec(self, mounts, doc, spec):
        assert list(mounts.pod_specs(doc)) == [("p", spec)]

    def test_a_kind_that_runs_no_pod_yields_nothing(self, mounts):
        assert list(mounts.pod_specs({"kind": "Service", "spec": {"ports": []}})) == []

    def test_a_workload_with_no_name_or_spec(self, mounts):
        assert list(mounts.pod_specs({"kind": "Job"})) == [("?", {})]


class TestDuplicates:
    def test_two_mounts_on_one_path_are_named_with_their_volumes(self, mounts):
        assert mounts.duplicates(TWICE) == [
            "Pod/loki container init: 2 volumes mounted at /var/loki (a, b)"]

    def test_a_clean_render(self, mounts):
        assert mounts.duplicates(CLEAN) == []

    def test_what_is_not_a_mount_is_passed_over(self, mounts):
        stream = ("--- [a, list]\n---\nkind: Pod\nmetadata: {name: p}\nspec:\n"
                  "  containers: [just-a-string, {name: c, volumeMounts: [x, {name: v}]}]\n"
                  "---\nkind: Deployment\nmetadata: {name: d}\n")
        assert mounts.duplicates(stream) == []

    def test_yaml_value_keys_are_read_as_text(self, mounts):
        crd = "kind: CustomResourceDefinition\nspec:\n  enum:\n    - =\n"
        assert mounts.duplicates(crd) == []


class TestCommandLine:
    def test_a_clean_render_passes_its_slice(self, run_script):
        run = run_script(GATE, "core/api", stdin=CLEAN)
        assert (run.status, run.out) == (0, "")

    def test_a_render_with_two_mounts_on_one_path_fails_its_slice(self, run_script):
        run = run_script(GATE, "observability/loki", stdin=TWICE)
        assert run.status == 1
        assert run.out.startswith("FAIL  observability/loki — a container mounts two volumes "
                                  "on one path:\n")
        assert "Pod/loki container init: 2 volumes mounted at /var/loki (a, b)" in run.out

    @pytest.mark.parametrize("stdin", ["", "# nothing rendered\n", "--- [a]\n--- null\n"],
                             ids=["empty", "comment-only", "no-mappings"])
    def test_a_render_with_no_manifests_is_refused(self, run_script, stdin):
        run = run_script(GATE, "core/api", stdin=stdin)
        assert run.status == 1
        assert "rendered output contains zero manifests" in run.out

    def test_with_no_label_it_names_stdin(self, run_script):
        assert "FAIL  (stdin) —" in run_script(GATE, stdin="").out

    def test_self_test_flag(self, run_script):
        run = run_script(GATE, "--self-test")
        assert run.status == 0
        assert "cases correct" in run.out

    def test_a_gate_that_has_not_proven_itself_refuses(self, mounts, monkeypatch, capsys):
        monkeypatch.setattr(mounts, "self_test", lambda: (print("  WRONG  a case"), 1)[1])
        monkeypatch.setattr(sys, "argv", [GATE, "core/api"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(CLEAN))
        assert mounts.main() == 1
        err = capsys.readouterr().err
        assert "WRONG  a case" in err
        assert "refusing to check a render with a gate that has not proven it rejects" in err

    def test_a_case_the_gate_gets_wrong_fails_its_self_test(self, mounts, monkeypatch, capsys):
        monkeypatch.setattr(mounts, "SELF_TEST_CASES", [("two mounts passed as clean", TWICE,
                                                         False)])
        assert mounts.self_test() == 1
        out = capsys.readouterr().out
        assert "WRONG     two mounts passed as clean — flagged, expected clean" in out
        assert "FAIL  1 case(s) wrong." in out

    def test_control_outcomes_count_both_halves(self, mounts):
        outcomes = mounts.control_outcomes()
        assert outcomes["ok"] is True
        assert outcomes["rejected"] > 0 and outcomes["accepted"] > 0
