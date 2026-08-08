#!/usr/bin/env python3
"""Hold kx's chart pins to the eks-gitops catalog they mirror.

    python3 scripts/mirror-check.py check       # blocking gate, at the pinned ref
    python3 scripts/mirror-check.py sync        # rewrite kx's pins from upstream
    python3 scripts/mirror-check.py freshness   # has upstream moved past the pin? (scheduled)

kx is the local kind workspace for the eks-gitops catalog. "Mirrors eks-gitops"
was true when each slice was written and had nothing holding it true
afterwards: eks-gitops has Renovate watching every chart pin, kx has none, so
one side advances on its own and the other never moves. A mirror where only one
side can advance drifts by construction, silently, in the direction of the side
that moves.

The comparison runs in both directions, because they catch different failures:

  * every chart kx pins is checked against upstream's pin — catches kx falling
    behind, which is what happened.
  * every chart upstream pins is checked for presence here — catches an addon
    landing in the catalog that never reaches the local workspace. Iterating
    kx's own slices cannot find this: the set being walked doesn't change when
    upstream grows.

Divergence is allowed and has to be declared. `stack/upstream.json` carries a
reason per entry, so "kx runs kube-prometheus-stack because the OTLP waist
needs a cluster the kind workspace doesn't have" is a decision on the record
rather than an omission indistinguishable from one.

`check` reads upstream AT THE PINNED REF, so its verdict is a function of the
commit under test. Whether that pin is behind is a different question with a
different answer every day, and asking it here would turn a required check red
because Renovate merged in another repository — `freshness` asks it on a
schedule.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "stack" / "upstream.json"


def die(msg):
    print(f"mirror-check: {msg}", file=sys.stderr)
    sys.exit(1)


def load_manifest():
    with MANIFEST.open() as fh:
        return json.load(fh)


def upstream_dir(manifest, ref):
    """The eks-gitops checkout to read, verified to be at `ref` unless ref is None."""
    import os

    path = Path(os.environ.get("EKS_GITOPS_DIR", ROOT.parent / "eks-gitops"))
    if not path.is_dir():
        die(
            f"no eks-gitops checkout at {path} — set EKS_GITOPS_DIR to one, "
            "or clone it beside this repository"
        )
    if ref is not None:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if head != ref:
            die(
                f"{path} is on {head[:12]} but the pin is {ref[:12]} — check that "
                "commit out, or run `freshness` if you meant to compare against HEAD"
            )
    return path


# ── reading the two sides ───────────────────────────────────────────────────


def walk(node):
    """Yield every dict in a parsed YAML document."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def upstream_pins(gitops):
    """{chart: version} for every chart the eks-gitops ApplicationSets pin.

    Two shapes carry a pin: a list-generator element with `chart` +
    `chartVersion`, and a template source with `chart` + a literal
    `targetRevision`. Templated values (`{{ .chartVersion }}`) resolve from the
    first shape, so they are skipped rather than recorded as a version.
    """
    appsets = gitops / "applicationsets"
    if not appsets.is_dir():
        die(f"{appsets} does not exist — is EKS_GITOPS_DIR really an eks-gitops checkout?")

    pins = {}
    for path in sorted(appsets.glob("*.yaml")):
        with path.open() as fh:
            for doc in yaml.safe_load_all(fh):
                for node in walk(doc):
                    chart = node.get("chart")
                    if not isinstance(chart, str) or "{{" in chart:
                        continue
                    version = node.get("chartVersion") or node.get("targetRevision")
                    if not isinstance(version, str) or "{{" in version:
                        continue
                    version = version.strip()
                    if version in ("main", "HEAD"):
                        continue
                    if chart in pins and pins[chart] != version:
                        die(
                            f"eks-gitops pins {chart} at both {pins[chart]} and {version} "
                            f"({path.name}) — resolve upstream before mirroring it"
                        )
                    pins[chart] = version
    if not pins:
        die(f"read no chart pins out of {appsets} — the parser and the catalog disagree")
    return pins


HELM_INSTALL = re.compile(r"^helm upgrade --install\s+(\S+)\s+(\S+)", re.M)
VERSION_FLAG = re.compile(r"--version\s+(\S+)")


def kx_pins():
    """{chart: (version, install_script)} for every kx slice with a helm pin."""
    pins = {}
    for script in sorted(ROOT.glob("stack/*/*/install.sh")):
        text = script.read_text()
        install = HELM_INSTALL.search(text)
        if not install:
            continue  # kubectl-apply slices (gateway-api-crds) and local builds
        chart = install.group(2).split("/")[-1]
        version = VERSION_FLAG.search(text)
        if not version:
            continue
        pins[chart] = (version.group(1), script)
    if not pins:
        die("read no chart pins out of stack/*/*/install.sh — the parser and the tree disagree")
    return pins


# ── the gate ────────────────────────────────────────────────────────────────


def stale_divergences(declared, upstream, local):
    """Declared divergences whose reason no longer holds.

    A divergence is a written-down decision, and the value of writing one down is
    that it can be reviewed later. That only works if the list describes the
    present: an entry that outlives its cause reads as a live decision while
    silently exempting the chart from the check it was carved out of. Closing a
    gap and leaving its entry behind would re-permit the gap.
    """
    stale = []
    for chart, entry in sorted(declared.items()):
        kind = entry.get("kind")
        if kind == "gitops-only" and chart in local:
            stale.append((chart, "declared gitops-only, but kx has a slice for it now"))
        elif kind == "kx-only" and chart in upstream:
            stale.append((chart, "declared kx-only, but eks-gitops pins it now"))
        elif kind == "version" and upstream.get(chart) == local.get(chart, (None,))[0]:
            stale.append((chart, "declared a version divergence, but the pins agree now"))
        elif chart not in upstream and chart not in local:
            stale.append((chart, "names a chart neither side pins"))
    return stale


def crd_installers(gitops):
    """{appset filename: (repo, path, revision)} for every CRD-only Application.

    These carry NO `chart` key — they are git sources pointing at a directory of
    CRD manifests — so upstream_pins() cannot see them and neither direction of
    compare() has ever walked them. That blind spot is not hypothetical: the
    argo-workflows CRD installer exists upstream precisely because the chart's
    default fetches its CRDs from raw.githubusercontent.com at sync time, and kx
    went on doing exactly that while every version comparison reported a match.

    A CRD installer is a decision about how a kind reaches the cluster, and kx
    has to make the same decision by a different mechanism (it has no ArgoCD and
    no sync waves). So each one must be answered in stack/upstream.json rather
    than silently inherited or silently ignored.
    """
    appsets = gitops / "applicationsets"
    found = {}
    for path in sorted(appsets.glob("*.yaml")):
        with path.open() as fh:
            for doc in yaml.safe_load_all(fh):
                for node in walk(doc):
                    repo, src = node.get("repoURL"), node.get("path")
                    if not isinstance(repo, str) or not isinstance(src, str):
                        continue
                    if "chart" in node or "{{" in src:
                        continue
                    if "crd" not in src.lower():
                        continue
                    found[path.name] = (repo, src, node.get("targetRevision"))
    return found


def unanswered_crd_installers(manifest, gitops):
    """CRD installers upstream that stack/upstream.json does not account for."""
    answered = {e["appset"]: e for e in manifest.get("crdInstallers", [])}
    upstream = crd_installers(gitops)
    if not upstream:
        die("read no CRD installers out of the catalog — the parser and the "
            "catalog disagree, so this check is asserting nothing")
    missing = [(name, meta) for name, meta in sorted(upstream.items()) if name not in answered]
    stale = [name for name in sorted(answered) if name not in upstream]
    return missing, stale


def compare(manifest, gitops):
    """(mismatched, missing_here, extra_here, stale) after applying declared divergences."""
    declared = {d["chart"]: d for d in manifest.get("divergences", [])}
    upstream = upstream_pins(gitops)
    local = kx_pins()

    mismatched, missing_here, extra_here = [], [], []

    # Direction 1 — what kx pins, against upstream. Catches kx falling behind.
    for chart, (version, script) in sorted(local.items()):
        if chart not in upstream:
            if declared.get(chart, {}).get("kind") != "kx-only":
                extra_here.append((chart, version, script))
            continue
        if upstream[chart] != version and declared.get(chart, {}).get("kind") != "version":
            mismatched.append((chart, version, upstream[chart], script))

    # Direction 2 — what upstream pins, against kx. Catches a new catalog addon
    # never reaching the workspace. Direction 1 is structurally blind to this:
    # the set it walks doesn't grow when upstream does.
    for chart, version in sorted(upstream.items()):
        if chart not in local and declared.get(chart, {}).get("kind") != "gitops-only":
            missing_here.append((chart, version))

    return mismatched, missing_here, extra_here, stale_divergences(declared, upstream, local)


def report(manifest, gitops):
    mismatched, missing_here, extra_here, stale = compare(manifest, gitops)
    unanswered, stale_installers = unanswered_crd_installers(manifest, gitops)

    for name, (repo, src, rev) in unanswered:
        print(f"  \u2717 {name}: eks-gitops installs CRDs from {repo} ({src} @ {rev})")
        print(f"      and stack/upstream.json says nothing about how kx gets those kinds.")
        print(f"      Add a crdInstallers entry recording the local mechanism.")
    for name in stale_installers:
        print(f"  \u2717 {name}: declared in crdInstallers but no longer exists upstream")

    for chart, mine, theirs, script in mismatched:
        rel = script.relative_to(ROOT)
        print(f"  ✗ {chart}: kx pins {mine}, eks-gitops pins {theirs}  ({rel})")
    for chart, version in missing_here:
        print(f"  ✗ {chart}: eks-gitops pins {version}, kx has no slice for it")
    for chart, version, script in extra_here:
        rel = script.relative_to(ROOT)
        print(f"  ✗ {chart}: kx pins {version}, eks-gitops has no entry for it  ({rel})")
    for chart, why in stale:
        print(f"  ✗ {chart}: stale divergence in stack/upstream.json — {why}")

    return mismatched, missing_here, extra_here, stale, unanswered, stale_installers


def cmd_check(manifest):
    ref = manifest["upstream"]["ref"]
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        die(f"upstream.ref is {ref}, which is not a commit sha")

    gitops = upstream_dir(manifest, ref)
    mismatched, missing_here, extra_here, stale, unanswered, stale_inst = report(manifest, gitops)
    total = (len(mismatched) + len(missing_here) + len(extra_here) + len(stale)
             + len(unanswered) + len(stale_inst))

    if total:
        print()
        print(f"mirror-check: {total} slice(s) disagree with eks-gitops@{ref[:12]}")
        print("mirror-check: adopt them with `python3 scripts/mirror-check.py sync`,")
        print("mirror-check: or declare the divergence with a reason in stack/upstream.json")
        sys.exit(1)

    charts = len(kx_pins())
    declared = len(manifest.get("divergences", []))
    print(
        f"mirror-check: {charts} chart(s) match eks-gitops@{ref[:12]} "
        f"({declared} declared divergence(s), "
        f"{len(manifest.get('crdInstallers', []))} CRD installer(s) answered)"
    )


def cmd_freshness(manifest):
    """Compare against whatever upstream is now, not against the pin."""
    gitops = upstream_dir(manifest, None)
    mismatched, missing_here, extra_here, stale, unanswered, stale_inst = report(manifest, gitops)
    total = (len(mismatched) + len(missing_here) + len(extra_here) + len(stale)
             + len(unanswered) + len(stale_inst))

    if not total:
        print("mirror-check: kx matches the eks-gitops catalog at its default branch")
        return

    print()
    print(f"mirror-check: {total} slice(s) behind the current eks-gitops catalog")
    print("mirror-check: re-pin with `python3 scripts/mirror-check.py sync`, then")
    print("mirror-check: render the affected slices before merging the bump")
    sys.exit(1)


def cmd_sync(manifest):
    """Rewrite kx's --version flags from upstream and move the pin with them."""
    gitops = upstream_dir(manifest, None)
    head = subprocess.run(
        ["git", "-C", str(gitops), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    upstream = upstream_pins(gitops)
    declared = {d["chart"]: d for d in manifest.get("divergences", [])}
    changed = 0

    for chart, (version, script) in sorted(kx_pins().items()):
        if chart not in upstream or declared.get(chart, {}).get("kind") == "version":
            continue
        if upstream[chart] == version:
            continue
        text = script.read_text()
        # Rewrite only the version this slice's own helm block declares. The
        # flag appears once per install.sh; count=1 keeps an unrelated later
        # match (a comment, a second chart) from being rewritten silently.
        new_text, n = VERSION_FLAG.subn(f"--version {upstream[chart]}", text, count=1)
        if n != 1:
            die(f"{script.relative_to(ROOT)}: expected one --version flag, rewrote {n}")
        script.write_text(new_text)
        print(f"  ↻ {chart}: {version} → {upstream[chart]}  ({script.relative_to(ROOT)})")
        changed += 1

    manifest["upstream"]["ref"] = head
    with MANIFEST.open("w") as fh:
        # ensure_ascii=False is load-bearing. Without it json.dump escapes every
        # non-ASCII character, so each em-dash in the divergence reasons comes
        # back as a \\u2014 escape and moving the pin rewrites prose it never
        # touched — a one-line change arriving as a diff across the whole file.
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"mirror-check: {changed} slice(s) re-pinned; upstream.ref now {head[:12]}")
    if changed:
        print("mirror-check: now run: ./scripts/render-check.sh")


def main():
    commands = {"check": cmd_check, "sync": cmd_sync, "freshness": cmd_freshness}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        die("usage: mirror-check.py {check|sync|freshness}")
    commands[sys.argv[1]](load_manifest())


if __name__ == "__main__":
    main()
