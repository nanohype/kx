#!/usr/bin/env python3
"""Every chart this stack pins is still the chart it was pinned for.

    python3 scripts/check-chart-deprecation.py            # blocking gate, offline
    python3 scripts/check-chart-deprecation.py --live     # scheduled, hits the registries
    python3 scripts/check-chart-deprecation.py --sync     # rewrite the records from upstream
    python3 scripts/check-chart-deprecation.py --self-test

The kx-side sibling of the same check in eks-gitops. They are separate files
rather than one shared module because the two repos state their pins in
different languages — install.sh here, ApplicationSet YAML there — and because a
repo that cannot gate itself without cloning another one is not really gated.
The assertions are the same on both sides; only the parser differs.

mirror-check already holds kx's versions equal to the catalog's, but it compares
chart name to version and never looks at where the chart came from, so the two
repos could pull the same version from different repositories and it would pass.
Recording the repo here closes that from this side: changing where a chart comes
from now requires re-recording it deliberately.

That matters most for the charts mirror-check deliberately does not cover
at all — the kx-only slices declared in stack/upstream.json. Nothing upstream
watches those. This does.

Split by what is and is not a function of this commit:

  default (offline, BLOCKING) — every pinned chart has a provenance record and
      every record names a chart still pinned.

  --live (network, SCHEDULED) — fetch each pinned chart and compare: a
      `deprecated: true`, or a description that no longer matches its record.

The description comparison is the one that earns its keep. `deprecated: true` is
loud. A silently re-scoped chart is not: the OSS Loki chart moved to
grafana-community and the chart left behind was re-scoped to Grafana Enterprise
Logs, with no deprecation flag ever set. The pin resolved, the chart installed,
the render gate stayed green. The description was the only field that moved.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECORDS = ROOT / "stack" / "chart-provenance.json"

REPO_ADD = re.compile(r"^helm repo add\s+(\S+)\s+(\S+)", re.M)
HELM_INSTALL = re.compile(r"^helm upgrade --install\s+(\S+)\s+(\S+)", re.M)
VERSION_FLAG = re.compile(r"--version\s+(\S+)")


def die(msg: str) -> None:
    print(f"chart-provenance: {msg}", file=sys.stderr)
    sys.exit(1)


def pins() -> dict[str, dict]:
    """{chart: {repo, version, source}} for every helm pin in stack/*/*/install.sh."""
    found: dict[str, dict] = {}
    for script in sorted(ROOT.glob("stack/*/*/install.sh")):
        text = script.read_text()
        install = HELM_INSTALL.search(text)
        version = VERSION_FLAG.search(text)
        if not install or not version:
            continue  # kubectl-apply slices and locally-built images
        ref = install.group(2)
        if ref.startswith("oci://"):
            repo, chart = ref, ref.rstrip("/").split("/")[-1]
        else:
            alias, _, chart = ref.partition("/")
            if not chart:
                continue
            urls = {a: u for a, u in REPO_ADD.findall(text)}
            repo = urls.get(alias)
            if not repo:
                die(
                    f"{script.relative_to(ROOT)} installs {ref} but adds no repo "
                    f"named {alias!r} — the parser and the script disagree"
                )
        found[chart] = {
            "repo": repo,
            "version": version.group(1),
            "source": str(script.relative_to(ROOT)),
        }
    if not found:
        die("read no chart pins out of stack/*/*/install.sh — the parser and the tree disagree")
    return found


def load_records() -> dict:
    if not RECORDS.exists():
        die(f"{RECORDS.relative_to(ROOT)} does not exist. Run --sync to create it.")
    return json.loads(RECORDS.read_text()).get("charts", {})


def fetch(chart: str, repo: str, version: str) -> dict:
    if repo.startswith("oci://"):
        cmd = ["helm", "show", "chart", repo, "--version", version]
    else:
        cmd = ["helm", "show", "chart", "--repo", repo, chart, "--version", version]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if out.returncode != 0:
        tail = (out.stderr or out.stdout).strip().splitlines()
        return {"_error": tail[-1][:200] if tail else "no output"}
    return yaml.safe_load(out.stdout) or {}


def check_offline(live: dict, recorded: dict) -> int:
    problems = []
    for chart, pin in sorted(live.items()):
        rec = recorded.get(chart)
        if rec is None:
            problems.append(
                f"{chart} is pinned ({pin['version']}, {pin['source']}) with no provenance "
                f"record. Nothing would notice if it were deprecated or re-scoped. Run --sync."
            )
            continue
        if rec.get("repo") != pin["repo"]:
            problems.append(
                f"{chart} is pinned from {pin['repo']} but recorded against {rec.get('repo')}. "
                f"A repository change is a change of maintainer — re-record it deliberately."
            )
        if not rec.get("description"):
            problems.append(f"{chart} has a provenance record with no description to compare against.")
        if rec.get("deprecated") is True:
            problems.append(
                f"{chart} is recorded as deprecated upstream and is still pinned. "
                f"Either migrate it or record why it stays."
            )
    for chart in sorted(set(recorded) - set(live)):
        problems.append(f"{chart} has a provenance record but is no longer pinned — drop the record.")

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for p in problems:
            print(f"        {p}")
        return 1
    print(f"OK    {len(live)} chart pin(s), each with a provenance record, none recorded deprecated.")
    return 0


def check_live() -> int:
    live = pins()
    recorded = load_records()
    problems = []
    for chart, pin in sorted(live.items()):
        meta = fetch(chart, pin["repo"], pin["version"])
        if "_error" in meta:
            problems.append(f"{chart}: could not read upstream metadata — {meta['_error']}")
            continue
        rec = recorded.get(chart, {})
        if meta.get("deprecated") is True:
            problems.append(f"{chart} {pin['version']} is marked deprecated by upstream ({pin['repo']}).")
        desc = (meta.get("description") or "").strip()
        was = (rec.get("description") or "").strip()
        if was and desc != was:
            problems.append(
                f"{chart} changed what it says it is.\n"
                f"            recorded: {was}\n"
                f"            upstream: {desc}\n"
                f"          A chart that redescribes itself may have changed product or "
                f"maintainer. Read the upstream notes, then --sync if it is still the chart "
                f"you want."
            )
    if problems:
        print(f"FAIL  {len(problems)} problem(s) across {len(live)} pinned chart(s):")
        for p in problems:
            print(f"        {p}")
        return 1
    print(f"OK    all {len(live)} pinned chart(s) match their record and none is deprecated.")
    return 0


def sync() -> int:
    live = pins()
    charts = {}
    for chart, pin in sorted(live.items()):
        meta = fetch(chart, pin["repo"], pin["version"])
        if "_error" in meta:
            die(f"{chart}: {meta['_error']}")
        charts[chart] = {
            "repo": pin["repo"],
            "description": (meta.get("description") or "").strip(),
            "deprecated": bool(meta.get("deprecated", False)),
        }
        print(f"  recorded {chart:32} {pin['version']:12} deprecated={charts[chart]['deprecated']}")
    RECORDS.write_text(
        json.dumps(
            {
                "_README": (
                    "What each pinned chart says it is, recorded so a change is visible. "
                    "check-chart-deprecation.py compares upstream against this on a schedule; "
                    "the blocking gate only checks that every pin has a record and every record "
                    "a pin. A description change means the chart redescribed itself — read the "
                    "upstream notes before running --sync, because that is the signal a chart "
                    "has changed product or maintainer without ever setting a deprecated flag."
                ),
                "charts": charts,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nwrote {RECORDS.relative_to(ROOT)} ({len(charts)} charts)")
    return 0


def self_test() -> int:
    real_pins, real_records = pins(), load_records()

    def run(p, r):
        with contextlib.redirect_stdout(io.StringIO()):
            return check_offline(p, r)

    name = sorted(real_pins)[0]
    p_extra = dict(real_pins)
    p_extra["ghost-chart"] = {"repo": "https://example.invalid", "version": "1.0.0", "source": "x"}
    r_stale = dict(real_records)
    r_stale["retired-chart"] = {"repo": "https://example.invalid", "description": "x", "deprecated": False}
    r_repo = json.loads(json.dumps(real_records))
    r_repo[name]["repo"] = "https://somewhere.else.invalid"
    r_dep = json.loads(json.dumps(real_records))
    r_dep[name]["deprecated"] = True
    r_nodesc = json.loads(json.dumps(real_records))
    r_nodesc[name]["description"] = ""

    breaks = [
        ("a pinned chart with no provenance record", p_extra, real_records),
        ("a record for a chart no longer pinned", real_pins, r_stale),
        ("the recorded repository differs from the pin", real_pins, r_repo),
        ("a chart recorded deprecated but still pinned", real_pins, r_dep),
        ("a record with no description", real_pins, r_nodesc),
    ]

    failures = []
    for label, p, r in breaks:
        if run(p, r) == 0:
            failures.append(label)
            print(f"  ACCEPTED  {label}   <-- not caught")
        else:
            print(f"  rejected  {label}")
    if run(real_pins, real_records) != 0:
        failures.append("the real stack does not pass")
        print("  ACCEPTED  (control) the shipped stack is rejected")
    else:
        print("  passed    (control) the shipped stack")

    if failures:
        print(f"\nFAIL  {len(failures)} break(s) not caught.")
        return 1
    print(f"\nOK    all {len(breaks)} breaks rejected, and the shipped stack passes.")
    return 0


def control_outcomes() -> dict:
    """What the controls actually exercised, for the suite-wide floor.

    Counted by running them and reading the outcome, never by matching source
    text. A floor that decides whether a gate has controls by looking for the
    word "control" is satisfied by a comment saying the controls were removed —
    which is the same defect one level up from the one the controls exist for.

    Both halves matter. A gate that rejects everything is as useless as one that
    rejects nothing, and either count alone passes a one-sided check.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = self_test()
    lines = buf.getvalue().splitlines()
    return {
        "ok": rc == 0,
        "rejected": sum(1 for line in lines if any(m in line for m in ('rejected  ',))),
        "accepted": sum(1 for line in lines if any(m in line for m in ('passed    ',))),
    }


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if "--sync" in sys.argv:
        return sync()
    # Always, before either check reports. The offline half is a set comparison
    # that would return no problems if it stopped comparing, and print the same
    # OK line it prints when every pin really is recorded.
    if self_test() != 0:
        print("\nRefusing to report with a gate that has not proven it rejects.")
        return 1
    print()
    if "--live" in sys.argv:
        return check_live()
    return check_offline(pins(), load_records())


if __name__ == "__main__":
    sys.exit(main())
