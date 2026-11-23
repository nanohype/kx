#!/usr/bin/env python3
"""Turn a resolved AWS profile into the Secret Kyverno clones to each tenant.

    aws configure export-credentials --profile P --format process \\
      | python3 credentials.py check

    aws configure export-credentials --profile P --format process \\
      | python3 credentials.py manifest <name> <namespace> <region> \\
      | kubectl apply -f -

    python3 credentials.py remaining <expiration>

The credentials arrive on stdin, in the process-credential format the aws CLI
prints, and leave as a Secret manifest on stdout. Nothing secret is taken as an
argument or read from the environment: an argument is in this process's argv,
where `ps` shows it to every other process on the workstation, and an exported
variable reaches every command the installer runs after it. The region and an
expiration are not secrets, and are arguments.

Credentials with no expiry are refused. The Secret is cloned into every
tenants-* namespace, and only a session stops working there by itself: a
long-lived access key reports no expiry, and neither does a session the aws CLI
gave none for, so either would stay valid in every copy until someone revoked
it. Credentials that have already expired are refused too, since every call
signed with them would fail.

The Secret records when the session stops working, in its
`kx-credentials-expiration` annotation. `check` refuses what `manifest` would
refuse, before the installer writes anything, and prints that time for its
summary. `remaining` reads it back for the verify task and exits 1 once it has
passed.
"""

import base64
import json
import sys
from datetime import datetime, timezone

# The keys the gateway's signing sidecar reads, in the names the AWS SDKs look
# for, each paired with the process-credential field it comes from.
REQUIRED = {
    "AWS_ACCESS_KEY_ID": "AccessKeyId",
    "AWS_SECRET_ACCESS_KEY": "SecretAccessKey",
    "AWS_SESSION_TOKEN": "SessionToken",
}

# The annotation that records when the credentials stop working. Unprefixed:
# kx owns no domain to prefix it with, and Kubernetes reads an unprefixed key as
# private to the cluster's user, which is what this one is.
EXPIRATION = "kx-credentials-expiration"


def fail(msg: str):
    raise SystemExit(f"bedrock-credentials: {msg}")


def parse_time(value: str):
    """`value` as an aware datetime, or None when it is not a time with a UTC offset."""
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def load(stream) -> dict:
    try:
        creds = json.load(stream)
    except json.JSONDecodeError as e:
        fail(f"the aws CLI's output is not JSON ({e}).")
    if not isinstance(creds, dict):
        fail("the aws CLI's output is not a JSON object.")
    missing = [field for field in REQUIRED.values() if not creds.get(field)]
    if missing:
        fail(f"the resolved credentials have no {', '.join(missing)}. A long-lived access key "
             f"carries no session token; use an SSO or assumed-role profile.")
    return creds


def admit(creds: dict, now: datetime) -> dict:
    """The credentials, unless they would not stop working by themselves."""
    expiration = creds.get("Expiration")
    if not expiration:
        fail("the resolved credentials carry no expiry, so a copy in every tenants-* namespace "
             "would stay valid until someone revoked it. Use an SSO or assumed-role profile, "
             "whose session the aws CLI reports an expiry for.")
    at = parse_time(expiration)
    if at is None:
        fail(f"the credentials' expiry {expiration!r} is not a time with a UTC offset, so when "
             f"they stop working is unknown.")
    if at <= now:
        fail(f"the session expired at {expiration}. Renew it, for an SSO profile with "
             f"`aws sso login`, and run this again.")
    return creds


def manifest(creds: dict, name: str, namespace: str, region: str) -> dict:
    values = {key: creds[field] for key, field in REQUIRED.items()}
    values["AWS_REGION"] = region
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {EXPIRATION: creds["Expiration"]},
        },
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()},
    }


def remaining(value: str, now: datetime) -> tuple[bool, str]:
    """(whether credentials recorded as expiring at `value` still work at `now`,
    a line saying when they stop)."""
    at = parse_time(value)
    if at is None:
        return False, f"{value!r} is not a time with a UTC offset, so when it expires is unknown"
    left = int((at - now).total_seconds())
    if left <= 0:
        return False, f"expired at {value}"
    hours, minutes = divmod(left // 60, 60)
    return True, f"{value}, in {hours}h {minutes:02d}m"


USAGE = ("usage: credentials.py check\n"
         "       credentials.py manifest <secret-name> <namespace> <region>\n"
         "       credentials.py remaining <expiration>\n"
         "(check and manifest read the aws CLI's process-credential JSON on stdin)")


def main(argv: list[str]) -> int:
    now = datetime.now(timezone.utc)
    if argv[1:] == ["check"]:
        print(admit(load(sys.stdin), now)["Expiration"])
        return 0
    if len(argv) == 5 and argv[1] == "manifest":
        print(json.dumps(manifest(admit(load(sys.stdin), now), argv[2], argv[3], argv[4])))
        return 0
    if len(argv) == 3 and argv[1] == "remaining":
        still_valid, line = remaining(argv[2], now)
        print(line)
        return 0 if still_valid else 1
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
