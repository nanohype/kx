#!/usr/bin/env python3
"""Re-key CNPG's druid-metadata-app Secret into the Secret the Druid chart reads.

    kubectl -n druid get secret druid-metadata-app -o json \\
      | python3 metadata-secret.py <name> <namespace> \\
      | kubectl apply -f -

The chart helper reads exactly four keys off a Secret named for the release, and
CNPG publishes them under the same names, so this is a rename rather than a
translation. The values are copied as CNPG stored them, base64-encoded, so no
credential is decoded or reaches an argument list. A key CNPG stops publishing
fails here, rather than producing a Secret the chart mounts with a field
missing.

The name and namespace are arguments because install.sh owns them: the release
name it renders is what the chart helper derives the Secret's name from.
"""

import json
import sys

KEYS = ("username", "password", "host", "dbname")


def translate(source: dict, name: str, namespace: str) -> dict:
    """The chart-shaped Secret, built from CNPG's Secret as `kubectl -o json` prints it."""
    data = source.get("data")
    if not isinstance(data, dict):
        raise SystemExit("druid: druid-metadata-app carries no data map — CNPG has not "
                         "finished publishing the credentials the chart helper reads.")
    missing = [k for k in KEYS if k not in data]
    if missing:
        raise SystemExit(f"druid: druid-metadata-app has no {', '.join(missing)} — CNPG did "
                         f"not publish what the chart helper reads.")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": {k: data[k] for k in KEYS},
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <secret-name> <namespace>  (CNPG's Secret as JSON on stdin)",
              file=sys.stderr)
        return 2
    try:
        source = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        raise SystemExit(f"druid: druid-metadata-app did not arrive as JSON ({e}).") from None
    print(json.dumps(translate(source, argv[1], argv[2])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
