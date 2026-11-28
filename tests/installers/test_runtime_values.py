"""runtime-values.py: each Druid runtime string, the catalog's with kx's lines after it.

helm replaces a string value rather than merging it, so a runtime string kx
sets whole would restate the catalog's and drift from it. These tests hold the
helper to composing the two in that order, to leaving the strings kx does not
touch to the catalog, and to refusing a string the catalog no longer sets
rather than deploying kx's lines alone.
"""

import json

import pytest
import yaml

HELPER = "stack/data/druid/runtime-values.py"

CATALOG = {
    "hostedId": "prod",
    "runtime": "druid.startup.logging.logProperties=true\ndruid.storage.type=s3\n",
    "router": {"replicas": 2, "runtime": "druid.plaintextPort=8888"},
    "broker": {"runtime": "druid.broker.http.numConnections=20"},
    "coordinator": {"runtime": "druid.coordinator.period=PT30S"},
}

LOCAL = {
    "hostedId": "kx",
    "runtime": "\ndruid.s3.endpoint.url=http://minio.minio.svc.cluster.local:9000\n",
    "router": {"runtime": "druid.router.http.numConnections=10"},
    "broker": {"replicas": 1},
}


@pytest.fixture
def helper(load_script):
    return load_script(HELPER)


def properties(text: str) -> dict:
    """A properties string as Druid's loader reads it: the last value for a key wins."""
    read = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            read[key] = value
    return read


class TestRuntimeKeys:
    def test_the_top_level_string_and_each_services(self, helper):
        assert helper.runtime_keys(LOCAL) == [("runtime",), ("router", "runtime")]

    @pytest.mark.parametrize("local", [
        {"runtime": None}, {"runtime": {"a": 1}}, {"router": "a string"}, {"router": {}},
        {"router": {"runtime": 3}},
    ], ids=["null", "a-mapping", "service-not-a-mapping", "service-without-one", "not-a-string"])
    def test_anything_that_is_not_a_string_is_not_one(self, helper, local):
        assert helper.runtime_keys(local) == []


class TestCompose:
    def test_the_catalogs_lines_then_kxs(self, helper):
        out = helper.compose(CATALOG, LOCAL)
        assert out == {
            "runtime": "druid.startup.logging.logProperties=true\ndruid.storage.type=s3\n\n"
                       f"{helper.MARKER}\n"
                       "druid.s3.endpoint.url=http://minio.minio.svc.cluster.local:9000",
            "router": {"runtime": f"druid.plaintextPort=8888\n\n{helper.MARKER}\n"
                                  "druid.router.http.numConnections=10"},
        }

    def test_a_string_kx_does_not_touch_is_left_to_the_catalog(self, helper):
        out = helper.compose(CATALOG, LOCAL)
        assert "broker" not in out and "coordinator" not in out

    def test_a_property_kx_sets_again_wins(self, helper):
        local = {"runtime": "druid.storage.type=local"}
        composed = helper.compose(CATALOG, local)["runtime"]
        assert properties(composed)["druid.storage.type"] == "local"
        assert properties(composed)["druid.startup.logging.logProperties"] == "true"

    def test_nothing_to_compose(self, helper):
        assert helper.compose(CATALOG, {"hostedId": "kx"}) == {}

    @pytest.mark.parametrize("catalog,name", [
        ({}, "runtime"),
        ({"runtime": ["a list"]}, "runtime"),
        ({"runtime": "x=1"}, "router.runtime"),
        ({"runtime": "x=1", "router": "a string"}, "router.runtime"),
        ({"runtime": "x=1", "router": {"replicas": 1}}, "router.runtime"),
    ], ids=["no-runtime", "not-a-string", "no-service", "service-not-a-mapping",
            "service-without-one"])
    def test_a_string_the_catalog_does_not_set_is_refused(self, helper, catalog, name):
        with pytest.raises(SystemExit) as e:
            helper.compose(catalog, LOCAL)
        assert f"adds lines to {name}, which the catalog's values do not set" in str(e.value.code)


class TestLoad:
    def test_a_mapping(self, helper, tmp_path):
        path = tmp_path / "values.yaml"
        path.write_text("runtime: |-\n  a=1\n")
        assert helper.load(str(path)) == {"runtime": "a=1"}

    def test_a_file_that_is_not_there(self, helper, tmp_path):
        with pytest.raises(SystemExit) as e:
            helper.load(str(tmp_path / "absent.yaml"))
        assert "cannot read" in str(e.value.code) and "No such file" in str(e.value.code)

    def test_a_file_that_does_not_parse(self, helper, tmp_path):
        path = tmp_path / "values.yaml"
        path.write_text("runtime: [unclosed\n")
        with pytest.raises(SystemExit) as e:
            helper.load(str(path))
        assert "does not parse as YAML" in str(e.value.code)

    @pytest.mark.parametrize("text", ["", "- a list\n", "a scalar\n"],
                             ids=["empty", "a-list", "a-scalar"])
    def test_a_file_that_is_not_a_mapping(self, helper, tmp_path, text):
        path = tmp_path / "values.yaml"
        path.write_text(text)
        with pytest.raises(SystemExit) as e:
            helper.load(str(path))
        assert "is not a mapping of values" in str(e.value.code)


class TestCommandLine:
    @pytest.fixture
    def files(self, tmp_path):
        catalog, local = tmp_path / "catalog.yaml", tmp_path / "values-local.yaml"
        catalog.write_text(yaml.safe_dump(CATALOG))
        local.write_text(yaml.safe_dump(LOCAL))
        return str(catalog), str(local)

    def test_prints_values_helm_reads_back_as_the_composed_strings(self, helper, run_script,
                                                                    files):
        run = run_script(HELPER, *files)
        assert (run.status, run.err) == (0, "")
        assert json.loads(run.out) == yaml.safe_load(run.out) == helper.compose(CATALOG, LOCAL)

    def test_refuses_a_catalog_that_dropped_a_string_kx_adds_to(self, run_script, tmp_path,
                                                                 files):
        catalog = tmp_path / "old-catalog.yaml"
        catalog.write_text(yaml.safe_dump({"runtime": "x=1"}))
        run = run_script(HELPER, str(catalog), files[1])
        assert (run.status, run.out) == (1, "")
        assert "adds lines to router.runtime" in run.err

    @pytest.mark.parametrize("args", [(), ("catalog.yaml",), ("a", "b", "c")],
                             ids=["nothing", "one", "one-too-many"])
    def test_anything_but_two_files_prints_the_usage(self, run_script, args):
        run = run_script(HELPER, *args)
        assert (run.status, run.out) == (2, "")
        assert "usage: " in run.err and "<catalog-values.yaml> <values-local.yaml>" in run.err
