"""scripts/gatelib: what the gates share, held to its contract apart from them.

Every gate reads its tree through this library, so a defect here changes what
each of them decides without a line of any gate changing. The gates' own
controls exercise it only as far as each gate happens to; these tests hold each
function to what it promises.
"""

import pathlib
import subprocess

import pytest
import yaml

from gatelib import manifests, pins, report, scratch, text


class TestStripComments:
    def test_a_comment_body_is_blanked_to_its_length(self):
        assert text.strip_comments("x=1  # set x") == "x=1" + " " * 9

    def test_a_line_that_is_all_comment(self):
        assert text.strip_comments("# the whole line") == " " * 16

    def test_a_hash_that_opens_no_comment_is_kept(self):
        line = "curl https://x.example/a#frag \"q # quoted\" 'r # quoted' a#b"
        assert text.strip_comments(line) == line

    def test_an_escaped_quote_does_not_close_its_string(self):
        line = 'echo "a \\" # still quoted" # gone'
        assert text.strip_comments(line) == 'echo "a \\" # still quoted" ' + " " * 6

    def test_an_apostrophe_inside_a_word_opens_no_quote(self):
        assert text.strip_comments("dont'care # comment") == "dont'care " + " " * 9

    def test_a_double_quote_left_open_keeps_the_rest_of_its_line(self):
        # Only an apostrophe can be a letter, so the second reading still finds
        # the quote open and everything after it is quoted.
        line = 'say "never closed # not a comment'
        assert text.strip_comments(line) == line

    def test_every_line_keeps_its_length(self):
        source = "a  # one\n\n'b' # two\nc\n  # three"
        assert [len(line) for line in text.strip_comments(source).splitlines()] == [
            len(line) for line in source.splitlines()]


class TestReport:
    def test_say_names_the_gate_on_stderr(self, capsys):
        report.say("a-gate", "the reason")
        assert capsys.readouterr() == ("", "a-gate: the reason\n")

    def test_refuse_says_why_and_hands_back_its_status(self, capsys):
        assert report.refuse("a-gate", "the reason") == 1
        assert report.refuse("a-gate", "the reason", code=2) == 2
        assert capsys.readouterr().err == "a-gate: the reason\n" * 2

    def test_die_stops_with_its_status(self, capsys):
        with pytest.raises(SystemExit) as stopped:
            report.die("a-gate", "the reason", 3)
        assert stopped.value.code == 3
        assert capsys.readouterr().err == "a-gate: the reason\n"

    @pytest.mark.parametrize("status", [0, 1, 2])
    def test_guarded_hands_back_what_main_returns(self, status):
        assert report.guarded(lambda: status, "a-gate", 9) == status

    @pytest.mark.parametrize("cmd,named", [
        (["helm", "pull", "a chart"], "`helm pull 'a chart'` did not finish within 30s"),
        ("git fetch origin", "`git fetch origin` did not finish within 1.5s"),
    ], ids=["argv", "string"])
    def test_a_command_that_outran_its_bound_is_a_refusal_naming_it(self, capsys, cmd, named):
        def main():
            raise subprocess.TimeoutExpired(cmd, 30 if isinstance(cmd, list) else 1.5)

        assert report.guarded(main, "a-gate", 2) == 2
        assert capsys.readouterr().err == (
            f"a-gate: {named}, so this run could not complete. That says nothing about "
            f"the tree.\n")

    def test_any_other_failure_is_not_turned_into_a_refusal(self):
        def main():
            raise ValueError("a fault in the gate")

        with pytest.raises(ValueError, match="a fault in the gate"):
            report.guarded(main, "a-gate", 2)


class TestManifestLoader:
    def test_a_bare_equals_is_read_as_text(self):
        doc = yaml.load("enum:\n  - =\n  - '!='\n", Loader=manifests.ManifestLoader)
        assert doc == {"enum": ["=", "!="]}

    def test_the_plain_safe_loader_refuses_it(self):
        # What the loader exists for: without it, the document is lost.
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load("- =\n")


class TestPodSpecs:
    @pytest.mark.parametrize("kind", sorted(manifests.WORKLOADS - {"Pod", "CronJob"}))
    def test_a_workload_yields_its_template_spec(self, kind):
        doc = {"kind": kind, "metadata": {"name": "w"},
               "spec": {"template": {"spec": {"containers": []}}}}
        assert list(manifests.pod_specs(doc)) == [("w", {"containers": []})]

    def test_a_pod_yields_its_own_spec(self):
        doc = {"kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": []}}
        assert list(manifests.pod_specs(doc)) == [("p", {"containers": []})]

    def test_a_cronjob_yields_its_job_templates_spec(self):
        doc = {"kind": "CronJob", "metadata": {"name": "c"},
               "spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": []}}}}}}
        assert list(manifests.pod_specs(doc)) == [("c", {"containers": []})]

    @pytest.mark.parametrize("doc", [
        {"kind": "Job"},
        {"kind": "CronJob", "metadata": None, "spec": {"jobTemplate": None}},
        {"kind": "Pod", "metadata": {}, "spec": None},
    ], ids=["job-bare", "cronjob-nulls", "pod-nulls"])
    def test_a_workload_with_no_name_or_spec(self, doc):
        assert list(manifests.pod_specs(doc)) == [("?", {})]

    @pytest.mark.parametrize("doc", [
        {"kind": "Service", "spec": {"ports": []}}, {}, {"kind": None}],
        ids=["service", "no-kind", "null-kind"])
    def test_a_kind_that_runs_no_pod_yields_nothing(self, doc):
        assert list(manifests.pod_specs(doc)) == []


class TestHelmBlock:
    def test_a_script_with_no_helm_install(self):
        assert pins.helm_block("kubectl apply -f ./manifest.yaml\n") is None

    def test_a_helm_install_in_a_comment_is_not_one(self):
        assert pins.helm_block("# helm upgrade --install a x/a --version 1\n") is None

    def test_the_block_runs_through_its_continuation_lines(self):
        source = ("helm repo add x https://x.example\n"
                  "helm upgrade --install a x/a \\\n  --version 1.2.3 \\\n  --wait\n"
                  "echo done\n")
        (start, end), cmd = pins.helm_block(source)
        assert cmd == "helm upgrade --install a x/a \\\n  --version 1.2.3 \\\n  --wait"
        assert source[start:end] == cmd

    def test_a_block_that_ends_the_file(self):
        source = "helm template a x/a --version 1"
        assert pins.helm_block(source) == ((0, len(source)), source)

    def test_a_superseded_pin_in_a_comment_is_not_read(self):
        source = "# --version 0.0.1 was the pin\nhelm upgrade --install a x/a --version 2.0.0\n"
        _, cmd = pins.helm_block(source)
        assert pins.VERSION_FLAG.search(cmd).group(1) == "2.0.0"

    @pytest.mark.parametrize("line", [
        "helm upgrade --install a x/a",
        "  helm template a oci://r.example/o/a",
        "\tdeadline 900 helm upgrade --install a x/a",
    ], ids=["install", "template-indented", "under-deadline"])
    def test_each_shape_an_installer_runs_its_chart_in(self, line):
        assert pins.helm_block(line + " --version 1\n") is not None


class TestInstallPins:
    TREE = {
        "stack/a/alias/install.sh":
            "helm repo add x https://x.example --force-update >/dev/null\n"
            "helm upgrade --install r x/chart-a --version 1.2.3 --wait --timeout 5m\n",
        "stack/a/oci/install.sh":
            "deadline 900 helm template r oci://r.example/o/chart-b \\\n"
            "  --version 4.5.6 >\"${MANIFEST}\"\n",
        "stack/b/local/install.sh": "helm upgrade --install r ./chart --wait --timeout 1m\n",
        "stack/b/manifests/install.sh": "kubectl apply -f ./manifest.yaml\n",
        "stack/b/unadded/install.sh":
            "# helm repo add y https://y.example\n"
            "helm pull --version 0.0.1 y/elsewhere\n"
            "helm upgrade --install r y/chart-c --version 7.0.0\n",
        "stack/c/too/deep/install.sh": "helm upgrade --install r x/chart-d --version 1\n",
    }

    def test_every_install_script_is_read_as_its_command_reads(self):
        with scratch.tree(self.TREE) as root:
            assert pins.install_pins(root) == [
                pins.Pin(root / "stack/a/alias/install.sh", "x/chart-a", "chart-a", "1.2.3",
                         "https://x.example"),
                pins.Pin(root / "stack/a/oci/install.sh", "oci://r.example/o/chart-b",
                         "chart-b", "4.5.6", "oci://r.example/o/chart-b"),
                pins.Pin(root / "stack/b/local/install.sh", "./chart", "chart", None, None),
                # The repo add is a comment, so the alias resolves to nothing; and
                # the version is the install's, not the pull's above it.
                pins.Pin(root / "stack/b/unadded/install.sh", "y/chart-c", "chart-c", "7.0.0",
                         None),
            ]

    def test_a_tree_with_no_install_scripts(self, tmp_path):
        assert pins.install_pins(tmp_path) == []


class TestScratch:
    def test_a_directory_is_removed_when_its_block_ends(self):
        with scratch.directory("kx-gatelib-test-") as d:
            assert d.is_dir() and d.name.startswith("kx-gatelib-test-")
            (d / "written").write_text("x")
        assert not d.exists()

    def test_and_when_its_block_raises(self):
        with pytest.raises(RuntimeError), scratch.directory("kx-gatelib-test-") as d:
            raise RuntimeError
        assert not d.exists()

    def test_a_tree_holds_each_file_at_its_path(self):
        with scratch.tree({"a/b/c.txt": "1", "d.txt": "2"}) as root:
            assert root.name.startswith("kx-tree-")
            assert (root / "a/b/c.txt").read_text() == "1"
            assert (root / "d.txt").read_text() == "2"
            assert not (root / ".git").exists()
        assert not root.exists()

    def test_a_git_tree_stages_every_file_but_the_untracked(self):
        with scratch.tree({"s/a.sh": "a", "!s/b.sh": "b"}, git=True, untracked="!") as root:
            assert tracked(root) == ["s/a.sh"]
            assert (root / "s/b.sh").read_text() == "b"
            assert not (root / "!s").exists()

    def test_a_git_tree_with_nothing_to_stage_is_still_a_repository(self):
        with scratch.tree({"!x": "x"}, git=True, untracked="!") as root:
            assert (root / ".git").is_dir()
            assert tracked(root) == []

    def test_untracked_files_without_git(self):
        with scratch.tree({"!x": "x", "y": "y"}, untracked="!") as root:
            assert sorted(p.name for p in root.iterdir()) == ["x", "y"]


def tracked(root: pathlib.Path) -> list[str]:
    return subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True,
                          check=True, timeout=60).stdout.split()
