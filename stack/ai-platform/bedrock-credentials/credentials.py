#!/usr/bin/env python3
"""Turn a resolved AWS profile into the Secret Kyverno clones to each tenant.

    aws configure export-credentials --profile P --format process \\
      | python3 credentials.py check <refuse|allow>

    aws configure export-credentials --profile P --format process \\
      | python3 credentials.py manifest <name> <namespace> <region> <refuse|allow> \\
      | kubectl apply -f -

    python3 credentials.py remaining <expiration>

The credentials arrive on stdin, in the process-credential format the aws CLI
prints, and leave as a Secret manifest on stdout. Nothing secret is taken as an
argument or read from the environment: an argument is in this process's argv,
where `ps` shows it to every other process on the workstation, and an exported
variable reaches every command the installer runs after it. The region, the
decision on a long-lived key and an expiration are not secrets, and are
arguments.

A long-lived access key is refused unless the installer passes `allow`. It has
no session token and does not expire, and the Secret is cloned into every
tenants-* namespace, so a key copied there stays valid in each of them until
someone rotates it, where a session stops working by itself.

The Secret records when it stops working, in its `kx-credentials-expiration`
annotation: the time the aws CLI reported, `never` for a long-lived key, or
`unreported` for a session whose expiry the CLI did not give. `check` refuses
what `manifest` would refuse, before the installer writes anything, and prints
that value for its summary. `remaining` reads it back for the verify task and
exits 1 once it has passed.
"""

import base64
import json
import sys
from datetime import datetime, timezone

# The keys the gateway's signing sidecar reads, in the names the AWS SDKs look
# for, each paired with the process-credential field it comes from. The session
# token is absent for a long-lived key, and an empty value is what the SDKs
# expect in that case.
REQUIRED = {"AWS_ACCESS_KEY_ID": "AccessKeyId", "AWS_SECRET_ACCESS_KEY": "SecretAccessKey"}
OPTIONAL = {"AWS_SESSION_TOKEN": "SessionToken"}

# The annotation that records when the credentials stop working. Unprefixed:
# kx owns no domain to prefix it with, and Kubernetes reads an unprefixed key as
# private to the cluster's user, which is what this one is.
EXPIRATION = "kx-credentials-expiration"

# What the installer passes to say whether a long-lived key may be installed.
LONG_LIVED = ("refuse", "allow")


def load(stream) -> dict:
    try:
        creds = json.load(stream)
    except json.JSONDecodeError as e:
        raise SystemExit(f"bedrock-credentials: the aws CLI's output is not JSON ({e}).") from None
    if not isinstance(creds, dict):
        raise SystemExit("bedrock-credentials: the aws CLI's output is not a JSON object.")
    missing = [field for field in REQUIRED.values() if not creds.get(field)]
    if missing:
        raise SystemExit(f"bedrock-credentials: the resolved profile has no {', '.join(missing)}"
                         f" — a Secret built from it would fail every call as a 403.")
    return creds


def admit(creds: dict, long_lived: str) -> dict:
    """The credentials, unless they are a long-lived key the installer did not allow.

    A session always carries a token, since a temporary key cannot sign without
    one, so a key without a token is a long-lived one.
    """
    if not creds.get("SessionToken") and long_lived != "allow":
        raise SystemExit(
            "bedrock-credentials: the resolved profile is a long-lived access key, with no session"
            " token. It would be cloned into every tenants-* namespace and stay valid there until"
            " it is rotated. Use an SSO or assumed-role profile, or set"
            " KX_ALLOW_LONG_LIVED_AWS_KEY=1 to install it anyway.")
    return creds


def expiration(creds: dict) -> str:
    """When the credentials stop working, as the Secret records it."""
    if creds.get("Expiration"):
        return creds["Expiration"]
    return "unreported" if creds.get("SessionToken") else "never"


def manifest(creds: dict, name: str, namespace: str, region: str) -> dict:
    values = {key: creds[field] for key, field in REQUIRED.items()}
    values.update({key: creds.get(field) or "" for key, field in OPTIONAL.items()})
    values["AWS_REGION"] = region
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {EXPIRATION: expiration(creds)},
        },
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()},
    }


def remaining(value: str, now: datetime) -> tuple[bool, str]:
    """(whether credentials recorded as expiring at `value` still work at `now`,
    a line saying when they stop)."""
    if value == "never":
        return True, "never: a long-lived key, installed with KX_ALLOW_LONG_LIVED_AWS_KEY=1"
    if value == "unreported":
        return True, "not reported: the aws CLI gave no expiry for this session"
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        at = None
    if at is None or at.tzinfo is None:
        return False, f"{value!r} is not a time with a UTC offset, so when it expires is unknown"
    left = int((at - now).total_seconds())
    if left <= 0:
        return False, f"expired at {value}"
    hours, minutes = divmod(left // 60, 60)
    return True, f"{value}, in {hours}h {minutes:02d}m"


USAGE = ("usage: credentials.py check <refuse|allow>\n"
         "       credentials.py manifest <secret-name> <namespace> <region> <refuse|allow>\n"
         "       credentials.py remaining <expiration>\n"
         "(check and manifest read the aws CLI's process-credential JSON on stdin)")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "check" and argv[2] in LONG_LIVED:
        creds = admit(load(sys.stdin), argv[2])
        if not creds.get("SessionToken"):
            print("bedrock-credentials: installing a long-lived access key, as"
                  " KX_ALLOW_LONG_LIVED_AWS_KEY=1 asks. It stays valid in every tenants-*"
                  " namespace until it is rotated.", file=sys.stderr)
        print(expiration(creds))
        return 0
    if len(argv) == 6 and argv[1] == "manifest" and argv[5] in LONG_LIVED:
        creds = admit(load(sys.stdin), argv[5])
        print(json.dumps(manifest(creds, argv[2], argv[3], argv[4])))
        return 0
    if len(argv) == 3 and argv[1] == "remaining":
        still_valid, line = remaining(argv[2], datetime.now(timezone.utc))
        print(line)
        return 0 if still_valid else 1
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
