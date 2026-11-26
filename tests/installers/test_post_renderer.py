"""post-renderer.py: the eks-gitops Druid chart's render, filtered for a kind cluster.

The production chart runs unmodified, so this filter is the whole of the local
delta: it decides which rendered resources reach the cluster and which node
labels the pods may ask for. These tests hold it to the contract CLAUDE.md
states for it — the Karpenter kinds and ExternalSecret dropped, the EKS-only
nodeSelector labels stripped, everything else left as the chart rendered it.
"""

import sys

import pytest
import yaml

HELPER = "stack/data/druid/post-renderer.py"

# The labels CLAUDE.md names, and the two the chart helper hard-codes that are
# wrong on an arm64 kind node.
EKS_ONLY = {
    "karpenter.k8s.aws/instance-family": "m7i",
    "karpenter.sh/capacity-type": "on-demand",
    "eks.amazonaws.com/nodegroup": "druid",
    "kubernetes.io/arch": "amd64",
    "kubernetes.io/os": "linux",
}


def deployment(name: str, node_selector) -> dict:
    spec = {"containers": [{"name": "c", "image": "druid:1"}]}
    if node_selector is not None:
        spec["nodeSelector"] = node_selector
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"template": {"spec": spec}}}


def pod_template(node_selector: dict) -> str:
    """The Overlord's pod template, as the chart embeds it in a ConfigMap value."""
    return yaml.safe_dump({"apiVersion": "v1", "kind": "PodTemplate",
                           "template": {"spec": {"nodeSelector": node_selector}}})


def render(helper, *docs) -> list:
    return list(yaml.safe_load_all(helper.render(yaml.safe_dump_all(docs))))


@pytest.fixture
def helper(load_script):
    return load_script(HELPER)


class TestWhatReachesTheCluster:
    @pytest.mark.parametrize("kind", ["NodePool", "EC2NodeClass", "ExternalSecret"])
    def test_a_kind_with_nothing_to_apply_to_locally_is_dropped(self, helper, kind):
        dropped = {"apiVersion": "x/v1", "kind": kind, "metadata": {"name": "druid"}}
        kept = deployment("historical", None)
        assert render(helper, dropped, kept) == [kept]

    def test_an_empty_document_is_dropped(self, helper):
        kept = deployment("broker", None)
        assert list(yaml.safe_load_all(helper.render(f"---\n---\n{yaml.safe_dump(kept)}"))) == [
            kept]

    def test_an_empty_render_stays_empty(self, helper):
        # install.sh runs the filter over an empty render to prove it can start
        # before it changes anything on the cluster.
        assert helper.render("") == ""


class TestNodeSelectors:
    def test_the_eks_only_labels_are_stripped_and_the_rest_kept(self, helper):
        (out,) = render(helper, deployment("historical", {**EKS_ONLY, "role": "druid"}))
        assert out["spec"]["template"]["spec"]["nodeSelector"] == {"role": "druid"}

    def test_a_selector_left_empty_is_removed(self, helper):
        (out,) = render(helper, deployment("historical", dict(EKS_ONLY)))
        assert "nodeSelector" not in out["spec"]["template"]["spec"]

    def test_a_selector_that_is_not_a_mapping_is_left_alone(self, helper):
        (out,) = render(helper, deployment("historical", "kubernetes.io/arch=amd64"))
        assert out["spec"]["template"]["spec"]["nodeSelector"] == "kubernetes.io/arch=amd64"

    def test_selectors_inside_lists_are_reached(self, helper):
        items = [deployment("a", dict(EKS_ONLY)), deployment("b", {"role": "druid"})]
        (out,) = render(helper, {"apiVersion": "v1", "kind": "List", "items": items})
        specs = [item["spec"]["template"]["spec"] for item in out["items"]]
        assert "nodeSelector" not in specs[0]
        assert specs[1]["nodeSelector"] == {"role": "druid"}


class TestThePodTemplateConfigMap:
    def configmap(self, name: str, **data) -> dict:
        return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name},
                "data": data}

    def test_the_embedded_pod_template_is_stripped(self, helper):
        cm = self.configmap("druid-task-base", template=pod_template({**EKS_ONLY, "role": "x"}))
        (out,) = render(helper, cm)
        inner = yaml.safe_load(out["data"]["template"])
        assert inner["template"]["spec"]["nodeSelector"] == {"role": "x"}

    @pytest.mark.parametrize("value", [
        "druid.indexer.runner.type: k8s\n  : [unbalanced",
        "- a list\n- not a mapping\n",
        "plain text",
    ], ids=["not-yaml", "a-list", "a-scalar"])
    def test_a_value_that_is_not_a_yaml_mapping_is_left_exactly(self, helper, value):
        (out,) = render(helper, self.configmap("druid-task-base", value=value))
        assert out["data"]["value"] == value

    def test_a_value_that_is_not_a_string_is_left_exactly(self, helper):
        (out,) = render(helper, self.configmap("druid-task-base", replicas=3))
        assert out["data"]["replicas"] == 3

    def test_another_configmap_is_not_parsed(self, helper):
        template = pod_template(dict(EKS_ONLY))
        (out,) = render(helper, self.configmap("druid-common", template=template))
        assert out["data"]["template"] == template

    def test_a_task_base_configmap_with_no_data(self, helper):
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "druid-task-base"}}
        assert render(helper, cm) == [cm]

    def test_a_resource_with_no_metadata(self, helper):
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "data": {"t": pod_template(EKS_ONLY)}}
        assert render(helper, cm) == [cm]


class TestCommandLine:
    def test_filters_stdin_to_stdout(self, run_script):
        docs = [{"apiVersion": "karpenter.sh/v1", "kind": "NodePool", "metadata": {"name": "p"}},
                deployment("historical", {**EKS_ONLY, "role": "druid"})]
        run = run_script(HELPER, stdin=yaml.safe_dump_all(docs))
        assert run.status == 0
        (out,) = yaml.safe_load_all(run.out)
        assert out["spec"]["template"]["spec"]["nodeSelector"] == {"role": "druid"}

    def test_an_empty_render_exits_zero(self, run_script):
        run = run_script(HELPER, stdin="")
        assert (run.status, run.out.strip(), run.err) == (0, "", "")

    def test_refuses_to_start_without_pyyaml(self, load_script, monkeypatch):
        # None in sys.modules makes the import raise ImportError, as a python3
        # with no PyYAML installed does.
        monkeypatch.setitem(sys.modules, "yaml", None)
        with pytest.raises(SystemExit) as e:
            load_script(HELPER)
        assert "needs PyYAML" in str(e.value.code)
