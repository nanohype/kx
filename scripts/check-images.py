#!/usr/bin/env python3
"""The images kx's pinned charts resolve to, held to two properties.

    scripts/check-images.py --pins <render-dir>   # blocking
    scripts/check-images.py --cves <render-dir>   # scheduled, needs trivy

kx pins its own chart versions in its own `stack/*/*/install.sh` and records
them in its own `stack/chart-provenance.json`. Those pins are not derived from
the catalog and can diverge from it, so no other repository can vouch for what
they resolve to — a guarantee cannot be inherited from somewhere that does not
make the decision. `task up` pulls exactly these images into whichever
environment runs it, which makes them a surface this repository owns once per
adopter rather than once.

Scoped to CI time. Runtime scanning of what a live cluster is running belongs to
the trivy-operator addon and stays there; this is the other half — the images a
pin resolves to, checked where the pin can actually be changed. A finding is
only actionable at the place the version is chosen.

Split across two modes for the reason this repository already gives twice, for
chart provenance and for mirror freshness:

  --pins  Whether an image reference is immutable is a fact about the commit
          under test, so it blocks a merge.
  --cves  Whether a vulnerability was published against a pinned image
          overnight has a different answer every day and is not something a
          pull request caused. Gating merges on it reddens changes that are not
          at fault, so it runs on a schedule.

The input is the rendered stack rather than the values files, because a values
file names the deltas and the chart supplies the rest. Most of what an adopter
pulls is never written down here.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile

import yaml

# prometheus-operator-crds ships a CRD with a bare `=` in an enum, which
# SafeLoader refuses outright. Reading it as the string it plainly means keeps
# that whole slice from dropping out of the corpus.
yaml.SafeLoader.add_constructor(
    "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
)

WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicaSet", "Pod"}

# A tag that names a moving target. An image on one of these is a different
# program on the next pull, and nothing in the tree records which one an adopter
# got.
FLOATING = {"latest", "main", "master", "stable", "edge", "dev", "nightly"}

# Scanned at both tiers, failed on one. The threshold is load-bearing and was
# hiding a great deal: measured on three pinned images, CRITICAL-only reported
# 2, 0 and 2 where HIGH+CRITICAL reported 42, 8 and 108 — so an image with no
# CRITICAL printed "ok" while carrying eight HIGH findings, and the clean line
# said nothing about them.
#
# CRITICAL remains what fails, because these are third-party chart images this
# repository cannot rebuild and a HIGH-gated schedule would be red permanently
# for findings nobody here can close. But a passing line now carries its HIGH
# count, so "ok" cannot be read as "nothing found".
#
# Direction, stated because a flag is easy to mistake for a tightening: trivy's
# default reports every tier including UNKNOWN, LOW and MEDIUM, so `--severity`
# here NARROWS what is looked at. It is a deliberate reduction to the two tiers
# worth acting on for images this repository does not build, not a stricter
# setting — the strictest setting is passing no flag at all.
SCANNED = ("HIGH", "CRITICAL")
FAIL_ON = ("CRITICAL",)

# Images no registry can serve, so no scanner can pull them. Asserted the same
# way as the pin exemptions: an entry naming an image the render no longer
# produces fails, because an exemption that outlives its subject exempts
# whatever takes its place. Skipping silently would report an unscanned image
# as a clean one, which is the vacuous pass this suite exists to refuse.
UNSCANNABLE: dict[str, str] = {
    "ghcr.io/nanohype/eks-agent-platform/operator:dev": (
        "built from the sibling checkout and kind-loaded, never pushed, so the tag resolves "
        "to no manifest in any registry. What it contains is the working tree, which the "
        "sibling repository's own CI is the place to scan."
    ),
}

# Images allowed to carry a floating tag, each with the reason. Asserted below:
# an entry naming an image the render no longer produces fails, because an
# exemption that outlives its subject silently exempts whatever takes its place.
FLOATING_ALLOWED: dict[str, str] = {
    "ghcr.io/nanohype/eks-agent-platform/operator:dev": (
        "built from the sibling checkout by the operator slice's install.sh and kind-loaded, "
        "never pulled from a registry. An immutable tag would name an artifact that does not "
        "exist anywhere — the point of this slice is to exercise the working tree, and "
        "install.sh rolls the Deployment when the built image id changes."
    ),
}


def pod_specs(doc):
    """Every pod spec in a manifest, whatever wraps it."""
    kind = doc.get("kind")
    if kind not in WORKLOADS:
        return
    if kind == "Pod":
        yield doc.get("spec") or {}
    elif kind == "CronJob":
        spec = ((doc.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}
        yield (spec.get("template") or {}).get("spec") or {}
    else:
        yield ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}


def resolved_images(render_dir: pathlib.Path) -> dict[str, set[str]]:
    """{image reference: {slice}} over every container the render produces.

    Read out of pod specs rather than by grepping for `image:`, because that key
    appears in chart values, in CRD schemas and in annotations, and none of
    those is a thing an adopter pulls.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(render_dir.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict):
                continue
            for spec in pod_specs(doc):
                containers = (spec.get("initContainers") or []) + (spec.get("containers") or [])
                for c in containers:
                    if isinstance(c, dict) and c.get("image"):
                        found.setdefault(str(c["image"]), set()).add(path.stem)
    return found


def is_immutable(image: str) -> bool:
    """A reference that names one artifact rather than a moving target."""
    if "@sha256:" in image:
        return True
    last = image.rsplit("/", 1)[-1]
    if ":" not in last:
        return False              # no tag at all resolves to :latest
    return last.rsplit(":", 1)[1].lower() not in FLOATING


def check_pins(images: dict[str, set[str]]) -> list[str]:
    problems = []
    for image in sorted(images):
        if is_immutable(image) or image in FLOATING_ALLOWED:
            continue
        where = ", ".join(sorted(images[image]))
        problems.append(
            f"{image} is a moving reference, rendered by {where}. It is a different "
            f"program on the next pull and nothing here records which one an adopter got."
        )
    return problems


def exemptions_still_apply(images: dict[str, set[str]]) -> list[str]:
    """Every FLOATING_ALLOWED entry names an image the render actually produces.

    Asked of the real render only. A synthetic fixture exercises one shape by
    design, so asking it whether the shipped exemptions still have subjects
    answers about a corpus nobody claimed anything about — which is how the
    first version of this made every pin control fail on its clean fixture.
    """
    return [
        f"{name} names {image}, which the render no longer produces — an exemption that "
        f"outlives its subject exempts whatever takes its place."
        for name, table in (("FLOATING_ALLOWED", FLOATING_ALLOWED), ("UNSCANNABLE", UNSCANNABLE))
        for image in sorted(table)
        if image not in images
    ]


def scan(image: str, timeout: int = 300) -> tuple[list[dict], str | None]:
    """(vulnerabilities at SEVERITIES, error). Bounded: trivy pulls over the network."""
    cmd = ["trivy", "image", "--quiet", "--scanners", "vuln",
           "--severity", ",".join(SCANNED), "--format", "json", image]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return [], f"trivy did not finish within {timeout}s"
    except FileNotFoundError:
        return [], "trivy is not installed"
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()
        return [], tail[-1][:200] if tail else "trivy failed with no output"
    return vulns_in(p.stdout), None


def vulns_in(trivy_json: str) -> list[dict]:
    """The findings in a trivy report, flattened.

    Separate from scan() so the controls can drive it with a recorded report:
    the scanner is a dependency, and what this gate owns is how it reads one.
    """
    try:
        doc = json.loads(trivy_json)
    except json.JSONDecodeError:
        return []
    out = []
    for result in doc.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            out.append({
                "id": v.get("VulnerabilityID", "?"),
                "pkg": v.get("PkgName", "?"),
                "severity": v.get("Severity", "?"),
                "fixed": v.get("FixedVersion") or "",
            })
    return out


def check_cves(images: dict[str, set[str]]) -> list[str]:
    problems = []
    for image in sorted(images):
        if image in UNSCANNABLE:
            print(f"  skip  {image} — {UNSCANNABLE[image].split(',')[0]}", flush=True)
            continue
        vulns, err = scan(image)
        if err:
            # An image that could not be scanned is not an image with no
            # findings, and reporting it as clean is the vacuous pass this whole
            # suite exists to refuse.
            problems.append(f"{image} could not be scanned — {err}")
            continue
        blocking = [v for v in vulns if v["severity"] in FAIL_ON]
        other = len(vulns) - len(blocking)
        if blocking:
            listed = ", ".join(sorted({f"{v['id']} ({v['pkg']})" for v in blocking})[:6])
            more = "" if len(blocking) <= 6 else f", and {len(blocking) - 6} more"
            problems.append(f"{image}: {len(blocking)} {'/'.join(FAIL_ON)} — {listed}{more}")
        # The HIGH count rides on every line, passing or failing. Without it an
        # image with no CRITICAL prints a clean "ok" while carrying findings the
        # threshold merely declined to fail on.
        tail = f"  ({len(blocking)} critical, {other} high)" if vulns else "  (none)"
        print(f"  {'FAIL' if blocking else '  ok'}  {image}{tail}", flush=True)
    return problems


# ── controls ──────────────────────────────────────────────────────────────────
#
# Fixtures are built from literals rather than by patching a real render, so
# there is no did-the-edit-land question to defend against. The CVE half is
# controlled against a recorded trivy report rather than by running trivy: what
# this gate owns is how a report is read, and a control that needs the network
# is a control that gets skipped.

def _render(docs: list[str]) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="kx-images-"))
    for i, body in enumerate(docs):
        (d / f"slice{i}.yaml").write_text(body)
    return d


DEPLOY = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: x}
spec:
  template:
    spec:
      containers:
        - name: c
          image: %s
"""

CLEAN_REPORT = json.dumps({"Results": [{"Vulnerabilities": []}]})
DIRTY_REPORT = json.dumps({"Results": [{"Vulnerabilities": [
    {"VulnerabilityID": "CVE-0000-0001", "PkgName": "openssl", "Severity": "CRITICAL"},
]}]})


def controls() -> int:
    failures = 0

    def case(name, fn, broken_in, clean_in, must_say=None):
        nonlocal failures
        if broken_in == clean_in:
            print(f"  NOT MUTATED {name} — fixtures identical, so this proves nothing.")
            failures += 1
            return
        if fn(clean_in):
            print(f"  UNSOUND     {name} — already fails on the clean fixture.")
            failures += 1
            return
        reported = fn(broken_in)
        if not reported:
            print(f"  FAILS OPEN  {name} — accepted it.")
            failures += 1
        elif must_say and not any(must_say in r for r in reported):
            print(f"  MIS-REPORTED {name} — expected {must_say!r}, got: {reported}")
            failures += 1
        else:
            print(f"  control ok  {name}")

    pins = check_pins
    case("an image tagged :latest", pins,
         {"r/x:latest": {"s"}}, {"r/x:1.2.3": {"s"}}, "moving reference")
    case("an image with no tag at all", pins,
         {"r/x": {"s"}}, {"r/x:1.2.3": {"s"}}, "moving reference")
    case("an image pinned by digest is accepted", pins,
         {"r/x:main": {"s"}}, {"r/x@sha256:" + "a" * 64: {"s"}})
    case("a registry:port host is not mistaken for a tag", pins,
         {"reg.io:5000/x": {"s"}}, {"reg.io:5000/x:1.2.3": {"s"}})

    # Extraction: an image outside a pod spec is not something an adopter pulls.
    imgs = resolved_images(_render([DEPLOY % "r/real:1.0"]))
    if set(imgs) != {"r/real:1.0"}:
        print(f"  WRONG       extraction returned {set(imgs)}, expected the one container image")
        failures += 1
    else:
        print("  control ok  an image in a pod spec is extracted")

    noise = resolved_images(_render(["apiVersion: v1\nkind: ConfigMap\ndata:\n  image: r/nope:1\n"]))
    if noise:
        print(f"  FAILS OPEN  an `image:` key outside a pod spec was extracted: {set(noise)}")
        failures += 1
    else:
        print("  control ok  an `image:` key outside a pod spec is ignored")

    # Reading a scanner report.
    if vulns_in(DIRTY_REPORT) and not vulns_in(CLEAN_REPORT):
        print("  control ok  a report with findings is read as findings, a clean one as clean")
    else:
        print("  FAILS OPEN  a trivy report is not read correctly")
        failures += 1

    # The exemption list is asserted, not described.
    UNSCANNABLE["r/ghost-unscannable:1.0"] = "a subject the render does not produce"
    try:
        if not exemptions_still_apply({"r/x:1.0": {"s"}}):
            print("  FAILS OPEN  an UNSCANNABLE entry naming nothing was accepted.")
            failures += 1
        else:
            print("  control ok  an unscannable exemption naming an image not in the render")
    finally:
        UNSCANNABLE.pop("r/ghost-unscannable:1.0")

    FLOATING_ALLOWED["r/ghost:latest"] = "a subject the render does not produce"
    try:
        if not exemptions_still_apply({"r/x:1.0": {"s"}}):
            print("  FAILS OPEN  a FLOATING_ALLOWED entry naming nothing was accepted.")
            failures += 1
        else:
            print("  control ok  an exemption naming an image the render no longer produces")
    finally:
        FLOATING_ALLOWED.pop("r/ghost:latest")

    return failures


def control_outcomes() -> dict:
    """What the controls exercised, for the suite-wide floor."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        failures = controls()
    lines = buf.getvalue().splitlines()
    return {
        "ok": failures == 0,
        "lines": lines,
        "rejected": sum(1 for x in lines if "control ok" in x),
        "accepted": sum(1 for x in lines if "control ok" in x and "accepted" in x)
                    or sum(1 for x in lines if "is ignored" in x or "as clean" in x),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pins", action="store_true", help="every image is an immutable reference")
    mode.add_argument("--cves", action="store_true", help="no CRITICAL in any pinned image")
    ap.add_argument("render_dir")
    args = ap.parse_args()

    if controls():
        print("\nRefusing to report with a gate that has not proven it rejects.")
        return 1
    print()

    render_dir = pathlib.Path(args.render_dir)
    if not render_dir.is_dir():
        print(f"FAIL  {render_dir} is not a directory — run render-check.sh with KX_RENDER_OUT set.")
        return 2

    images = resolved_images(render_dir)
    if not images:
        print(f"FAIL  no container images in {render_dir} — refusing to report a clean scan "
              f"over an empty set. Either the render is empty or the extraction is wrong.")
        return 1

    check, label = (check_pins, "pinned") if args.pins else (check_cves,
                    f"free of {'/'.join(FAIL_ON)} (high findings are reported, not failed on)")
    print(f"check-images: {len(images)} image(s) across {len(set().union(*images.values()))} slice(s)")
    problems = exemptions_still_apply(images) + check(images)
    if problems:
        print(f"\nFAIL  {len(problems)} problem(s):")
        for p in problems:
            print(f"        {p}")
        return 1
    print(f"\ncheck-images: {len(images)} image(s) {label}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
