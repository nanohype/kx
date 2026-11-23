"""credentials.py: what may become the Secret copied into every tenants-* namespace.

The helper is the only thing between a workstation's AWS profile and a Secret
Kyverno clones into every tenant namespace, so these tests hold it to the rules
its docstring states: only a session that expires by itself is admitted, one
already expired is refused, and the Secret records when it stops working.
"""

import base64
import io
import json
from datetime import datetime, timezone

import pytest

HELPER = "stack/ai-platform/bedrock-credentials/credentials.py"

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

# Far enough from any clock a test runs under that main(), which reads the real
# time, sees one as current and the other as past.
FUTURE = "2999-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"


def session(**fields) -> dict:
    """The process-credential JSON the aws CLI prints for an SSO or assumed-role session."""
    creds = {
        "Version": 1,
        "AccessKeyId": "ASIAEXAMPLE",
        "SecretAccessKey": "secret-access-key",
        "SessionToken": "session-token",
        "Expiration": "2026-01-01T13:30:00Z",
    }
    creds.update(fields)
    return {k: v for k, v in creds.items() if v is not None}


@pytest.fixture
def credentials(load_script):
    return load_script(HELPER)


def refusal(excinfo) -> str:
    message = str(excinfo.value.code)
    assert message.startswith("bedrock-credentials: ")
    return message


class TestParseTime:
    def test_a_utc_time_written_with_z(self, credentials):
        assert credentials.parse_time("2026-01-01T13:30:00Z") == datetime(
            2026, 1, 1, 13, 30, tzinfo=timezone.utc)

    def test_a_time_with_an_offset(self, credentials):
        at = credentials.parse_time("2026-01-01T15:30:00+02:00")
        assert at == datetime(2026, 1, 1, 13, 30, tzinfo=timezone.utc)

    def test_a_time_with_no_offset_is_not_one(self, credentials):
        assert credentials.parse_time("2026-01-01T13:30:00") is None

    def test_text_that_is_no_time(self, credentials):
        assert credentials.parse_time("tomorrow") is None


class TestLoad:
    def test_a_complete_session(self, credentials):
        creds = session()
        assert credentials.load(io.StringIO(json.dumps(creds))) == creds

    def test_output_that_is_not_json(self, credentials):
        with pytest.raises(SystemExit) as e:
            credentials.load(io.StringIO("Unable to locate credentials"))
        assert "is not JSON" in refusal(e)

    def test_json_that_is_not_an_object(self, credentials):
        with pytest.raises(SystemExit) as e:
            credentials.load(io.StringIO("[]"))
        assert "not a JSON object" in refusal(e)

    def test_a_long_lived_key_has_no_session_token(self, credentials):
        with pytest.raises(SystemExit) as e:
            credentials.load(io.StringIO(json.dumps(session(SessionToken=None, Expiration=None))))
        message = refusal(e)
        assert "no SessionToken" in message
        assert "long-lived access key" in message

    def test_every_missing_field_is_named(self, credentials):
        with pytest.raises(SystemExit) as e:
            credentials.load(io.StringIO(json.dumps(session(AccessKeyId="", SecretAccessKey=None))))
        assert "no AccessKeyId, SecretAccessKey." in refusal(e)


class TestAdmit:
    def test_a_session_that_has_not_expired(self, credentials):
        creds = session()
        assert credentials.admit(creds, NOW) is creds

    @pytest.mark.parametrize("expiration", [None, ""], ids=["absent", "empty"])
    def test_credentials_with_no_expiry(self, credentials, expiration):
        with pytest.raises(SystemExit) as e:
            credentials.admit(session(Expiration=expiration), NOW)
        assert "carry no expiry" in refusal(e)

    @pytest.mark.parametrize("expiration", ["2026-01-01T13:30:00", "soon"],
                             ids=["no-offset", "not-a-time"])
    def test_an_expiry_that_is_not_a_time_with_an_offset(self, credentials, expiration):
        with pytest.raises(SystemExit) as e:
            credentials.admit(session(Expiration=expiration), NOW)
        assert "when they stop working is unknown" in refusal(e)

    @pytest.mark.parametrize("expiration", ["2026-01-01T12:00:00Z", "2026-01-01T11:59:59Z"],
                             ids=["expiring-now", "expired"])
    def test_a_session_that_has_expired(self, credentials, expiration):
        with pytest.raises(SystemExit) as e:
            credentials.admit(session(Expiration=expiration), NOW)
        assert f"the session expired at {expiration}" in refusal(e)


class TestManifest:
    def test_the_secret_holds_the_sdk_keys_and_the_region(self, credentials):
        secret = credentials.manifest(session(), "kx-bedrock-credentials", "kyverno", "us-west-2")
        data = {k: base64.b64decode(v).decode() for k, v in secret["data"].items()}
        assert data == {
            "AWS_ACCESS_KEY_ID": "ASIAEXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "secret-access-key",
            "AWS_SESSION_TOKEN": "session-token",
            "AWS_REGION": "us-west-2",
        }

    def test_the_secret_records_when_the_session_stops_working(self, credentials):
        secret = credentials.manifest(session(), "kx-bedrock-credentials", "kyverno", "us-west-2")
        assert secret["apiVersion"] == "v1"
        assert secret["kind"] == "Secret"
        assert secret["type"] == "Opaque"
        assert secret["metadata"] == {
            "name": "kx-bedrock-credentials",
            "namespace": "kyverno",
            "annotations": {credentials.EXPIRATION: "2026-01-01T13:30:00Z"},
        }

    def test_nothing_but_the_named_keys_is_copied(self, credentials):
        secret = credentials.manifest(session(Extra="x"), "n", "ns", "us-east-1")
        assert set(secret["data"]) == {*credentials.REQUIRED, "AWS_REGION"}


class TestRemaining:
    def test_a_session_with_time_left(self, credentials):
        assert credentials.remaining("2026-01-01T13:30:00Z", NOW) == (
            True, "2026-01-01T13:30:00Z, in 1h 30m")

    def test_minutes_are_written_with_two_digits(self, credentials):
        assert credentials.remaining("2026-01-01T12:05:59Z", NOW) == (
            True, "2026-01-01T12:05:59Z, in 0h 05m")

    @pytest.mark.parametrize("expiration", ["2026-01-01T12:00:00Z", "2026-01-01T11:00:00Z"],
                             ids=["expiring-now", "expired"])
    def test_a_session_with_none_left(self, credentials, expiration):
        assert credentials.remaining(expiration, NOW) == (False, f"expired at {expiration}")

    def test_an_annotation_that_is_no_time(self, credentials):
        still_valid, line = credentials.remaining("2026-01-01T13:30:00", NOW)
        assert not still_valid
        assert "when it expires is unknown" in line


class TestCommandLine:
    def test_check_prints_when_the_session_expires(self, run_script):
        run = run_script(HELPER, "check", stdin=json.dumps(session(Expiration=FUTURE)))
        assert (run.status, run.out) == (0, f"{FUTURE}\n")

    def test_check_refuses_an_expired_session(self, run_script):
        run = run_script(HELPER, "check", stdin=json.dumps(session(Expiration=PAST)))
        assert run.status == 1
        assert run.out == ""
        assert f"the session expired at {PAST}" in run.err

    def test_manifest_prints_the_secret(self, run_script):
        run = run_script(HELPER, "manifest", "kx-bedrock-credentials", "kyverno", "eu-west-1",
                         stdin=json.dumps(session(Expiration=FUTURE)))
        assert run.status == 0
        secret = json.loads(run.out)
        assert secret["metadata"]["namespace"] == "kyverno"
        assert base64.b64decode(secret["data"]["AWS_REGION"]).decode() == "eu-west-1"

    def test_manifest_writes_nothing_for_credentials_with_no_expiry(self, run_script):
        run = run_script(HELPER, "manifest", "n", "ns", "us-east-1",
                         stdin=json.dumps(session(Expiration=None)))
        assert (run.status, run.out) == (1, "")
        assert "carry no expiry" in run.err

    def test_remaining_exits_zero_while_the_session_works(self, run_script):
        run = run_script(HELPER, "remaining", FUTURE)
        assert run.status == 0
        assert run.out.startswith(f"{FUTURE}, in ")

    def test_remaining_exits_one_once_it_has_expired(self, run_script):
        run = run_script(HELPER, "remaining", PAST)
        assert (run.status, run.out) == (1, f"expired at {PAST}\n")

    @pytest.mark.parametrize("args", [
        (),
        ("check", "extra"),
        ("manifest", "n", "ns"),
        ("remaining",),
        ("rotate", "a", "b", "c"),
    ], ids=["nothing", "check-with-an-argument", "manifest-short", "remaining-short",
            "unknown-mode"])
    def test_anything_else_prints_the_usage(self, run_script, args):
        run = run_script(HELPER, *args)
        assert run.status == 2
        assert run.out == ""
        assert run.err.startswith("usage: credentials.py check\n")
