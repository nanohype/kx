#!/usr/bin/env python3
"""Compose each Druid runtime string from the catalog's and the lines kx adds.

    python3 runtime-values.py <catalog-values.yaml> <values-local.yaml> >runtime.json

The chart takes each service's runtime properties as one string value, and helm
replaces a string value rather than merging it. So values-local.yaml carries
only the lines kx adds, under the keys the catalog uses, and this writes the
values file install.sh layers after both: each runtime string values-local.yaml
sets, as the catalog's string with kx's lines after it. A property kx sets again
overrides the catalog's, because a properties file keeps the last value it reads
for a key.

A runtime string kx adds to that the catalog does not set is refused. Layered
anyway, it would deploy kx's lines alone and lose the production config without
a word.

The output is JSON, which helm reads as YAML.
"""

import json
import sys

import yaml

# Where kx's lines start in a composed string, for whoever reads the properties
# a pod mounts.
MARKER = "# ---- kx: stack/data/druid/values-local.yaml ----"


def runtime_keys(local: dict) -> list[tuple[str, ...]]:
    """The runtime strings values-local.yaml sets: the top-level one, and each service's."""
    keys = [("runtime",)] if isinstance(local.get("runtime"), str) else []
    keys += [(service, "runtime") for service, block in local.items()
             if isinstance(block, dict) and isinstance(block.get("runtime"), str)]
    return keys


def lookup(values: dict, key: tuple[str, ...]):
    """The value at `key` in a values tree, or None where any step is missing."""
    node = values
    for part in key:
        node = node.get(part) if isinstance(node, dict) else None
    return node


def compose(catalog: dict, local: dict) -> dict:
    """The values that set each composed runtime string, to layer over both files."""
    out: dict = {}
    for key in runtime_keys(local):
        base = lookup(catalog, key)
        if not isinstance(base, str):
            raise SystemExit(f"druid: values-local.yaml adds lines to {'.'.join(key)}, which the "
                             f"catalog's values do not set, so the string deployed would be "
                             f"kx's lines alone.")
        catalog_lines = base.rstrip("\n")
        kx_lines = lookup(local, key).strip("\n")
        parent = out
        for part in key[:-1]:
            parent = parent.setdefault(part, {})
        parent[key[-1]] = f"{catalog_lines}\n\n{MARKER}\n{kx_lines}"
    return out


def load(path: str) -> dict:
    """A values file as a mapping, or a refusal that names what is wrong with it."""
    try:
        with open(path) as f:
            values = yaml.safe_load(f)
    except OSError as e:
        raise SystemExit(f"druid: cannot read {path} ({e.strerror}).") from None
    except yaml.YAMLError as e:
        raise SystemExit(f"druid: {path} does not parse as YAML ({e}).") from None
    if not isinstance(values, dict):
        raise SystemExit(f"druid: {path} is not a mapping of values.")
    return values


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <catalog-values.yaml> <values-local.yaml>", file=sys.stderr)
        return 2
    print(json.dumps(compose(load(argv[1]), load(argv[2]))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
