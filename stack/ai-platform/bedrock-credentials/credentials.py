#!/usr/bin/env python3
"""Turn a resolved AWS profile into the Secret Kyverno clones to each tenant.

    aws configure export-credentials --profile P --format process \\
      | python3 credentials.py manifest <name> <namespace> <region> \\
      | kubectl apply -f -

    ... | python3 credentials.py expiry

The credentials arrive on stdin, in the process-credential format the aws CLI
prints, and leave as a Secret manifest on stdout. Nothing secret is taken as an
argument or read from the environment: an argument is in this process's argv,
where `ps` shows it to every other process on the workstation, and an exported
variable reaches every command the installer runs after it. The region is not a
secret and is an argument.

`expiry` prints when the session expires, for the installer's summary.
"""

import base64
import json
import sys

# The keys the gateway's signing sidecar reads, in the names the AWS SDKs look
# for, each paired with the process-credential field it comes from. The session
# token is absent for a long-lived key, and an empty value is what the SDKs
# expect in that case.
REQUIRED = {"AWS_ACCESS_KEY_ID": "AccessKeyId", "AWS_SECRET_ACCESS_KEY": "SecretAccessKey"}
OPTIONAL = {"AWS_SESSION_TOKEN": "SessionToken"}


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


def manifest(creds: dict, name: str, namespace: str, region: str) -> dict:
    values = {key: creds[field] for key, field in REQUIRED.items()}
    values.update({key: creds.get(field) or "" for key, field in OPTIONAL.items()})
    values["AWS_REGION"] = region
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()},
    }


def expiry(creds: dict) -> str:
    return creds.get("Expiration") or "no expiry (long-lived key)"


USAGE = ("usage: credentials.py manifest <secret-name> <namespace> <region>\n"
         "       credentials.py expiry\n"
         "(the aws CLI's process-credential JSON on stdin)")


def main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[1] == "manifest":
        print(json.dumps(manifest(load(sys.stdin), argv[2], argv[3], argv[4])))
        return 0
    if len(argv) == 2 and argv[1] == "expiry":
        print(expiry(load(sys.stdin)))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
