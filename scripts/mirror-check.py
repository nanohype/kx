#!/usr/bin/env python3
"""Hold kx's chart pins to the eks-gitops catalog they mirror.

    python3 scripts/mirror-check.py check       # blocking gate, at the pinned ref
    python3 scripts/mirror-check.py sync        # rewrite kx's pins from upstream
    python3 scripts/mirror-check.py freshness   # has upstream moved past the pin? (scheduled)

kx is the local kind workspace for the eks-gitops catalog. Nothing else holds
the two sides equal: eks-gitops has Renovate watching every chart pin and kx
has none, so one side advances on its own. A mirror where only one side can
advance drifts by construction, silently, in the direction of the side that
moves.

The comparison runs in both directions, because they catch different failures:

  * every chart kx pins is checked against upstream's pin — catches kx falling
    behind.
  * every chart upstream pins is checked for presence here — catches an addon
    landing in the catalog that never reaches the local workspace. Iterating
    kx's own slices cannot find this: the set being walked doesn't change when
    upstream grows.

Divergence is allowed and has to be declared. `stack/upstream.json` carries a
reason per entry, so "kx runs kube-prometheus-stack because the OTLP waist
needs a cluster the kind workspace doesn't have" is a decision on the record
rather than an omission indistinguishable from one.

What is compared is the chart PIN, not the chart VALUES. That scope is
deliberate — kx must not copy values that assume IRSA, ENI or an NLB — but it
is narrower than "kx matches the catalog" sounds, and the gap is real: upstream
can change resource bounds, a security context or a replica count and every
check here stays green. Resource bounds in particular are not AWS-specific, so
the reasoning that justifies ignoring values does not cover them. When a values
difference matters, it has to be carried as a comment in the addon's own
values.yaml, because this manifest has no slot for it and this script would not
notice it.

`check` reads upstream AT THE PINNED REF, so its verdict is a function of the
commit under test. Whether that pin is behind is a different question with a
different answer every day, and asking it here would turn a required check red
because Renovate merged in another repository — `freshness` asks it on a
schedule.
"""

import json
import os
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
    # Charts seen only in a shape this parser skips. A skip is correct — a
    # templated pin resolves from the list generator, and a branch-tracking
    # source has no version to compare — but a chart that appears in NO other
    # shape leaves the comparison silently smaller than upstream's catalog, and
    # the verdict is a count over whatever survived. Recorded here, checked below.
    skipped = {}
    for path in sorted(appsets.glob("*.yaml")):
        with path.open() as fh:
            for doc in yaml.safe_load_all(fh):
                for node in walk(doc):
                    chart = node.get("chart")
                    if not isinstance(chart, str) or "{{" in chart:
                        continue
                    version = node.get("chartVersion") or node.get("targetRevision")
                    if not isinstance(version, str) or "{{" in version:
                        skipped.setdefault(chart, "templated version")
                        continue
                    version = version.strip()
                    if version in ("main", "HEAD"):
                        skipped.setdefault(chart, f"tracks {version}")
                        continue
                    if chart in pins and pins[chart] != version:
                        die(
                            f"eks-gitops pins {chart} at both {pins[chart]} and {version} "
                            f"({path.name}) — resolve upstream before mirroring it"
                        )
                    pins[chart] = version
    if not pins:
        die(f"read no chart pins out of {appsets} — the parser and the catalog disagree")
    unresolved = {c: why for c, why in skipped.items() if c not in pins}
    if unresolved:
        for chart, why in sorted(unresolved.items()):
            print(f"mirror-check: {chart} appears upstream only as {why} — "
                  f"no version to compare against", file=sys.stderr)
        die(f"{len(unresolved)} upstream chart(s) carry no comparable version. The "
            "comparison would be a count over the ones that happened to parse.")

    return pins


HELM_INSTALL = re.compile(r"^helm upgrade --install\s+(\S+)\s+(\S+)", re.M)
VERSION_FLAG = re.compile(r"--version\s+(\S+)")


# Slices that install a chart from a path rather than a pinned remote, so there
# is no version for the mirror to compare. Named, because the alternative is a
# silent drop: the verdict below is a count over whatever the parser recognised,
# and a slice that falls out of the population is reported as neither matching
# nor diverging.
NO_REMOTE_PIN = {
    "stack/ai-platform/operator": "installs the chart from the sibling eks-agent-platform "
                                  "checkout, so the version is that working tree",
    "stack/data/druid": "renders the eks-gitops chart from a sibling checkout through a "
                        "post-renderer; the catalog pins the chart, not kx",
}


def kx_pins():
    """{chart: (version, install_script)} for every kx slice with a helm pin.

    Dies on a slice that installs a chart with no comparable version and is not
    named in NO_REMOTE_PIN. Skipping it silently would shrink the set the
    verdict is computed over without shrinking the verdict.
    """
    pins = {}
    unpinned = []
    for script in sorted(ROOT.glob("stack/*/*/install.sh")):
        slice_name = str(script.parent.relative_to(ROOT))
        text = script.read_text()
        install = HELM_INSTALL.search(text)
        if not install:
            continue  # applies manifests rather than installing a chart
        version = VERSION_FLAG.search(text)
        if not version:
            if slice_name not in NO_REMOTE_PIN:
                unpinned.append(slice_name)
            continue
        chart = install.group(2).split("/")[-1]
        pins[chart] = (version.group(1), script)
    if unpinned:
        for slice_name in unpinned:
            print(f"mirror-check: {slice_name} installs a chart with no --version — "
                  f"nothing to compare against the catalog", file=sys.stderr)
        die(f"{len(unpinned)} slice(s) install a chart this parser cannot pin. Either pin "
            "them, or record why they have no remote pin in NO_REMOTE_PIN.")
    stale = sorted(s for s in NO_REMOTE_PIN if not (ROOT / s / "install.sh").is_file())
    if stale:
        die(f"NO_REMOTE_PIN names {', '.join(stale)}, which no longer exists — an exemption "
            "that outlives its slice silently exempts whatever takes that path next.")
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
    """{appset filename: (repo, path, revision)} for git-source Applications whose path names CRDs.

    Matched by the path containing `crd`, which is how the catalog names them.
    That is a convention rather than a guarantee: an installer under a path
    called `definitions/` or `bootstrap/` is not seen here, so a catalog that
    renames one moves it out of this comparison without either side reporting
    it. Widen the match when upstream adopts another name — do not assume this
    finds installers it has no way to recognise.

    These carry no `chart` key — they are git sources pointing at a directory of
    CRD manifests — so upstream_pins() cannot see them and neither direction of
    compare() walks them. The catalog installs the argoproj.io CRDs this way
    because the chart's own default fetches them from raw.githubusercontent.com
    at sync time, and neither fact is visible to a version comparison.

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


def compare_pins(declared, upstream, local):
    """(mismatched, missing_here, extra_here, stale) over three dicts.

    Pure, so the self-test exercises this rather than a copy of it: a suite
    written against a restatement of the comparison passes while the shipped one
    is broken, which is the failure the suite exists to catch.
    """
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


def compare(manifest, gitops):
    """compare_pins over the two sides read from disk."""
    return compare_pins(
        {d["chart"]: d for d in manifest.get("divergences", [])},
        upstream_pins(gitops),
        kx_pins(),
    )


def report(manifest, gitops):
    mismatched, missing_here, extra_here, stale = compare(manifest, gitops)
    unanswered, stale_installers = unanswered_crd_installers(manifest, gitops)

    for name, (repo, src, rev) in unanswered:
        print(f"  ✗ {name}: eks-gitops installs CRDs from {repo} ({src} @ {rev})")
        print("      and stack/upstream.json says nothing about how kx gets those kinds.")
        print("      Add a crdInstallers entry recording the local mechanism.")
    for name in stale_installers:
        print(f"  ✗ {name}: declared in crdInstallers but no longer exists upstream")

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


def self_test() -> int:
    """Prove the comparison still reports a divergence, without a checkout.

    compare_pins() is pure over three dicts, so the breaks below drive the
    shipped function directly with no checkout. That is the half worth proving:
    the parsers die loudly when they read nothing, while a comparison that
    stopped reporting would return empty lists and print a clean verdict.
    """
    upstream = {"cert-manager": "v1.21.1", "cilium": "1.19.6"}
    local = {"cert-manager": ("v1.21.1", Path("x")), "cilium": ("1.19.6", Path("x"))}

    def cmp(declared, up=None, loc=None):
        return compare_pins(
            declared,
            upstream if up is None else up,
            local if loc is None else loc,
        )

    breaks = [
        ("kx pinned behind the catalog", cmp({}, up={**upstream, "cilium": "1.20.0"})),
        ("a catalog chart with no kx slice", cmp({}, up={**upstream, "velero": "12.1.0"})),
        ("a kx chart the catalog does not pin",
         cmp({}, loc={**local, "ingress-nginx": ("4.15.1", Path("x"))})),
        ("a divergence declared gitops-only that kx now has",
         cmp({"cilium": {"chart": "cilium", "kind": "gitops-only"}})),
        ("a divergence declared kx-only that the catalog now pins",
         cmp({"cert-manager": {"chart": "cert-manager", "kind": "kx-only"}})),
        ("a version divergence whose pins agree",
         cmp({"cilium": {"chart": "cilium", "kind": "version"}})),
        ("a divergence naming a chart neither side pins",
         cmp({"ghost": {"chart": "ghost", "kind": "version"}})),
    ]

    failures = 0
    for label, (mismatched, missing, extra, stale) in breaks:
        if not (mismatched or missing or extra or stale):
            print(f"  ACCEPTED  {label}   <-- not caught")
            failures += 1
        else:
            print(f"  rejected  {label}")

    control = cmp({})
    if any(control):
        print(f"  ACCEPTED  (control) two agreeing sides are reported as diverging: {control}")
        failures += 1
    else:
        print("  passed    (control) two agreeing sides")

    if failures:
        print(f"\nFAIL  {failures} case(s) wrong.")
        return 1
    print(f"\nOK    {len(breaks) + 1} case(s) behave as specified.")
    return 0


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    commands = {"check": cmd_check, "sync": cmd_sync, "freshness": cmd_freshness}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        die("usage: mirror-check.py {check|sync|freshness|--self-test}")
    commands[sys.argv[1]](load_manifest())


if __name__ == "__main__":
    main()
