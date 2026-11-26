"""metadata-secret.py: CNPG's druid-metadata-app Secret, re-keyed for the Druid chart.

The chart helper reads exactly four keys off a Secret named for the release.
These tests hold the helper to copying those four as CNPG stored them, still
base64-encoded, and to refusing a source that lacks any of them rather than
writing a Secret the chart would mount with a field missing.
"""

import json

import pytest

HELPER = "stack/data/druid/metadata-secret.py"

# CNPG's Secret as `kubectl get secret -o json` prints it. The values are the
# base64 of what CNPG stored, and CNPG publishes more keys than the chart reads.
CNPG_SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "druid-metadata-app", "namespace": "druid"},
    "type": "kubernetes.io/basic-auth",
    "data": {
        "username": "ZHJ1aWQ=",
        "password": "c2VjcmV0",
        "host": "ZHJ1aWQtbWV0YWRhdGEtcnc=",
        "dbname": "ZHJ1aWQ=",
        "port": "NTQzMg==",
        "uri": "cG9zdGdyZXNxbDovL2RydWlk",
        "jdbc-uri": "amRiYzpwb3N0Z3Jlc3FsOi8vZHJ1aWQ=",
    },
}


@pytest.fixture
def helper(load_script):
    return load_script(HELPER)


class TestTranslate:
    def test_the_four_keys_the_chart_reads_copied_still_encoded(self, helper):
        secret = helper.translate(CNPG_SECRET, "kx-ds-druid-metadata", "druid")
        assert secret == {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "kx-ds-druid-metadata", "namespace": "druid"},
            "type": "Opaque",
            "data": {
                "username": "ZHJ1aWQ=",
                "password": "c2VjcmV0",
                "host": "ZHJ1aWQtbWV0YWRhdGEtcnc=",
                "dbname": "ZHJ1aWQ=",
            },
        }

    @pytest.mark.parametrize("data", [None, [], "c2VjcmV0"], ids=["absent", "a-list", "a-string"])
    def test_a_source_with_no_data_map(self, helper, data):
        source = {**CNPG_SECRET, "data": data}
        if data is None:
            del source["data"]
        with pytest.raises(SystemExit) as e:
            helper.translate(source, "n", "druid")
        assert "carries no data map" in str(e.value.code)

    def test_every_key_cnpg_did_not_publish_is_named(self, helper):
        data = {k: v for k, v in CNPG_SECRET["data"].items() if k not in ("password", "dbname")}
        with pytest.raises(SystemExit) as e:
            helper.translate({**CNPG_SECRET, "data": data}, "n", "druid")
        assert "has no password, dbname" in str(e.value.code)


class TestCommandLine:
    def test_prints_the_chart_shaped_secret(self, run_script):
        run = run_script(HELPER, "kx-ds-druid-metadata", "druid", stdin=json.dumps(CNPG_SECRET))
        assert run.status == 0
        secret = json.loads(run.out)
        assert secret["metadata"] == {"name": "kx-ds-druid-metadata", "namespace": "druid"}
        assert set(secret["data"]) == {"username", "password", "host", "dbname"}

    def test_refuses_input_that_is_not_json(self, run_script):
        run = run_script(HELPER, "kx-ds-druid-metadata", "druid",
                         stdin='Error from server (NotFound): secrets "druid-metadata-app"')
        assert (run.status, run.out) == (1, "")
        assert "did not arrive as JSON" in run.err

    def test_refuses_a_secret_missing_a_key(self, run_script):
        source = {**CNPG_SECRET, "data": {"username": "ZHJ1aWQ="}}
        run = run_script(HELPER, "kx-ds-druid-metadata", "druid", stdin=json.dumps(source))
        assert (run.status, run.out) == (1, "")
        assert "has no password, host, dbname" in run.err

    @pytest.mark.parametrize("args", [(), ("kx-ds-druid-metadata",), ("a", "b", "c")],
                             ids=["nothing", "name-only", "one-too-many"])
    def test_anything_but_a_name_and_a_namespace_prints_the_usage(self, run_script, args):
        run = run_script(HELPER, *args, stdin=json.dumps(CNPG_SECRET))
        assert (run.status, run.out) == (2, "")
        assert "usage: " in run.err
        assert "<secret-name> <namespace>" in run.err
