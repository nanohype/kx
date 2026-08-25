#!/usr/bin/env python3
"""Validate every rendered manifest against a real schema, including CRDs.

    scripts/check-rendered-schemas.py <render-dir>   # gate
    scripts/check-rendered-schemas.py --self-test    # prove the gate rejects

`helm template` proves a chart renders. It does not prove the result is a valid
Kubernetes object, and in a catalog mirror the objects that matter most are the
custom ones — ServiceMonitor, ClusterPolicy, Gateway, Platform. A render gate
alone accepts a ServiceMonitor with a misspelled field and every check stays
green until the API server refuses it on a cluster.

The obvious way to add schema validation is the wrong one. `kubeconform
-ignore-missing-schemas` makes every unrecognised kind a SKIP and a skip counts
as success, so pointed at a stack of custom resources it reports every one of
them skipped and exits 0 — a green check that validated nothing. This gate runs
WITHOUT that flag: an unresolvable kind is an error, and the schemas for custom
kinds are built from the CRDs the stack itself renders. `--self-test` restores
the flag and asserts the skip count moves, so the claim is checked rather than
believed.

Two passes are required and the order is not incidental. A custom resource in
one slice is defined by a CRD shipped in another — kube-prometheus-stack's
ServiceMonitors are validated by prometheus-operator-crds' CRD — so nothing can
be validated until every slice has rendered.
"""

import contextlib
import io
import os
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

# The tree this gate reads. Overridable so the suite-wide floor can point the
# gate at a fixture it wrote and observe the real exit status, rather than
# asking the gate to describe its own behaviour.
ROOT = pathlib.Path(os.environ.get("KX_GATE_ROOT", "") or pathlib.Path(__file__).resolve().parent.parent)


class ManifestLoader(yaml.SafeLoader):
    """SafeLoader that tolerates a bare `=` in rendered CRDs.

    prometheus-operator-crds ships an Alertmanager matchType enum listing the
    match operators, and `- =` unquoted is YAML 1.1's "default value" tag rather
    than the string. SafeLoader refuses it outright, which would take the whole
    document — and with it every CRD in that file — out of the schema store.
    Losing schemas silently is the failure this gate exists to prevent, so the
    tag is resolved to the string it plainly means.
    """


ManifestLoader.add_constructor(
    "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
)
# kubeconform resolves unknown kinds over the network, so this gate is not
# hermetic and the two facts that follow from that are bounded rather than
# ignored: the subprocess carries a timeout, and an unreachable host is reported
# as an unreachable host instead of as a manifest failure.
CRDS_CATALOG_HOST = "raw.githubusercontent.com"
CRDS_CATALOG = (
    f"https://{CRDS_CATALOG_HOST}/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

# Longer than a healthy run and far short of the runner's own ceiling. The
# number that matters is that one exists: without it an unreachable schema host
# holds the job open for six hours and the gate reports nothing at all.
KUBECONFORM_TIMEOUT_S = 300
SUMMARY_RE = re.compile(
    r"Valid:\s*(\d+).*?Invalid:\s*(\d+).*?Errors:\s*(\d+).*?Skipped:\s*(\d+)", re.S
)


def die(msg, code=2):
    print(f"check-rendered-schemas: {msg}", file=sys.stderr)
    sys.exit(code)


def check_crd_structure(doc):
    """A CRD is the gate's schema source, so a malformed one is not a cosmetic
    problem — every custom resource validated against it becomes meaningless.

    kubeconform cannot check these: the upstream schema store publishes no
    CustomResourceDefinition schema, so a CRD is the ONE kind excluded from it.
    Excluded is not unchecked — the properties asserted here are the ones the
    rest of the gate depends on to build its schema store. Every other
    unresolvable kind stays a hard error.
    """
    spec = doc.get("spec") or {}
    name = (doc.get("metadata") or {}).get("name", "<unnamed>")
    problems = []
    if not spec.get("group"):
        problems.append("spec.group is missing")
    if not (spec.get("names") or {}).get("kind"):
        problems.append("spec.names.kind is missing")
    versions = spec.get("versions") or []
    if not versions:
        problems.append("spec.versions is empty")
    for v in versions:
        if not v.get("name"):
            problems.append("a version has no name")
        elif not ((v.get("schema") or {}).get("openAPIV3Schema")):
            problems.append(f"version {v['name']} carries no openAPIV3Schema")
    return name, problems


def crd_schemas(render_dir):
    """Every CRD in the rendered output, as kubeconform-locatable JSON schemas.

    Returns the schema dir and the number of schemas written. The count is
    returned rather than inferred later: a converter that silently emitted
    nothing would leave every custom resource resolving to no schema, which is
    skipping again with new machinery and a green check on top.
    """
    schema_dir = pathlib.Path(tempfile.mkdtemp(prefix="kx-crd-schemas-"))
    filtered = pathlib.Path(tempfile.mkdtemp(prefix="kx-manifests-"))
    written = 0
    crds = 0
    crd_problems = []
    for path in sorted(render_dir.glob("*.yaml")):
        keep = []
        for doc in yaml.load_all(path.read_text(), Loader=ManifestLoader):
            if not isinstance(doc, dict) or not doc.get("kind"):
                continue
            if doc.get("kind") != "CustomResourceDefinition":
                keep.append(doc)
                continue
            crds += 1
            cname, problems = check_crd_structure(doc)
            crd_problems += [f"{path.name}: CRD {cname}: {pr}" for pr in problems]
            spec = doc.get("spec", {})
            group = spec.get("group")
            kind = (spec.get("names") or {}).get("kind")
            if not group or not kind:
                continue
            for ver in spec.get("versions") or []:
                name = ver.get("name")
                schema = ((ver.get("schema") or {}).get("openAPIV3Schema"))
                if not name or not schema:
                    continue
                out = schema_dir / group / f"{kind.lower()}_{name}.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(schema))
                written += 1
        if keep:
            (filtered / path.name).write_text(
                "\n---\n".join(yaml.safe_dump(d) for d in keep)
            )
    return schema_dir, written, filtered, crds, crd_problems


def run_kubeconform(schema_dir, targets, ignore_missing=False):
    """`ignore_missing` exists only so the self-test can restore the defect.

    Nothing in the gate path sets it. It is a parameter rather than something
    the self-test seds into this file, because a sed whose pattern misses leaves
    the source unmutated and the suite green — recording "the assertion fires"
    when it never ran.
    """
    cmd = ["kubeconform", "-summary", "-strict"]
    if ignore_missing:
        cmd.append("-ignore-missing-schemas")
    cmd += [
        # Order is load-bearing. The stack's OWN rendered CRDs come first, so a
        # kind this catalog pins is validated against the definition at the
        # pinned chart version rather than whatever a community mirror holds.
        "-schema-location",
        f"{schema_dir}/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json",
        # Then the CRDs-catalog, for kinds whose CRD is never a manifest at all.
        # Cilium is the case that forced this: its chart ships zero CRDs and the
        # operator registers them programmatically at runtime, so the operator
        # slice's CiliumNetworkPolicy has nothing in-tree to validate against.
        # A fallback, not a shortcut — anything unresolvable by all three
        # locations is still a hard error.
        #
        # THIS CATALOG IS NOT VERSION-MATCHED TO THE PINNED CHART. Cilium's real
        # schema at 1.19.6 is whatever that operator registers at runtime; the
        # catalog holds a snapshot, which may be ahead or behind it. So a
        # CiliumNetworkPolicy failure here is not automatically a bad manifest —
        # triage schema skew first, by checking the policy against the CRD the
        # running operator actually registered. Kinds with an in-tree CRD are
        # unaffected: the rendered schema above wins for those.
        "-schema-location",
        CRDS_CATALOG,
        "-schema-location",
        "default",
    ]
    cmd += [str(t) for t in targets]
    # Bounded because kubeconform resolves unknown kinds over the network. An
    # unreachable schema host would otherwise hold the job open to the runner's
    # own six-hour ceiling, and a gate that hangs is a gate that reports nothing.
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=KUBECONFORM_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return 1, (
            f"kubeconform did not finish within {KUBECONFORM_TIMEOUT_S}s. It resolves "
            f"unknown kinds over the network, so this is usually {CRDS_CATALOG_HOST} "
            f"being unreachable rather than a manifest problem."
        )
    return p.returncode, (p.stdout + p.stderr)


def summarise(output):
    m = SUMMARY_RE.search(output)
    if not m:
        return None
    valid, invalid, errors, skipped = (int(x) for x in m.groups())
    return {"valid": valid, "invalid": invalid, "errors": errors, "skipped": skipped}


def gate(render_dir):
    render_dir = pathlib.Path(render_dir)
    if not render_dir.is_dir():
        die(f"{render_dir} is not a directory — run render-check.sh with KX_RENDER_OUT set.")

    manifests = sorted(render_dir.glob("*.yaml"))
    # A render that produced nothing would otherwise validate an empty set and
    # report success. This is the failure mode the gate is least able to notice
    # from its own output, so it is asserted first.
    if not manifests:
        die(f"no rendered manifests in {render_dir} — refusing to report success over an empty set.")

    schema_dir, count, filtered, crds, crd_problems = crd_schemas(render_dir)
    if crd_problems:
        for pr in crd_problems:
            print(f"  CRD FAIL  {pr}", file=sys.stderr)
        die(f"{len(crd_problems)} malformed CRD(s) — the schema source is wrong, so "
            "anything validated against it would be meaningless.", code=1)
    if count == 0:
        die("no CRD schemas were derived from the rendered output — every custom "
            "resource would resolve to no schema. Refusing to validate.")

    try:
        targets = sorted(filtered.glob("*.yaml"))
        if not targets:
            die("every rendered document was a CRD — nothing else to validate.")
        rc, output = run_kubeconform(schema_dir, targets)
        summary = summarise(output)
        print(output.strip())

        if summary is None:
            die("could not parse kubeconform's summary — refusing to infer a verdict.")
        if summary["skipped"] != 0:
            die(f"{summary['skipped']} resource(s) were SKIPPED. A skip counts as success, "
                "which is the defect this gate exists to prevent. Every kind needs a schema.")
        if rc != 0:
            print(f"check-rendered-schemas: FAIL — {summary['invalid']} invalid, "
                  f"{summary['errors']} error(s).", file=sys.stderr)
            return 1

        print(f"check-rendered-schemas: OK — {summary['valid']} resource(s) valid against "
              f"{count} CRD schema(s) + upstream, none skipped; "
              f"{crds} CRD(s) structurally checked.")
        return 0
    finally:
        shutil.rmtree(schema_dir, ignore_errors=True)
        shutil.rmtree(filtered, ignore_errors=True)


def self_test():
    """Prove the gate rejects, and that its skip assertion actually fires."""
    fixtures = ROOT / "tests" / "schema"
    failures = 0
    checked = 0

    schema_dir, count, filtered, _crds, _problems = crd_schemas(fixtures / "crds")
    if count == 0:
        die("the converter derived no schemas from tests/schema/crds/")
    if not (schema_dir / "kx.test" / "widget_v1.json").is_file():
        die(f"the Widget schema is missing; {count} other schema(s) present.")

    for fixture in sorted((fixtures / "accept").glob("*.yaml")):
        checked += 1
        rc, out = run_kubeconform(schema_dir, [fixture])
        s = summarise(out) or {}
        if rc != 0:
            print(f"FAIL  {fixture.name} must be admitted but was rejected:\n{out}")
            failures += 1
        elif s.get("skipped") != 0:
            print(f"FAIL  {fixture.name} was admitted but {s.get('skipped')} skipped:\n{out}")
            failures += 1
        else:
            print(f"  admitted  {fixture.name}")

    for fixture in sorted((fixtures / "reject").glob("*.yaml")):
        checked += 1
        rc, out = run_kubeconform(schema_dir, [fixture])
        s = summarise(out) or {}
        if rc == 0:
            print(f"FAIL  {fixture.name} was ADMITTED but must be rejected:\n{out}")
            failures += 1
        elif s.get("skipped") != 0:
            print(f"FAIL  {fixture.name} was not admitted, but only because it was SKIPPED "
                  f"— the exact defect this gate replaces:\n{out}")
            failures += 1
        else:
            print(f"  rejected  {fixture.name}")

    # Restore the defect and confirm the unknown kind goes back to
    # skipped-with-exit-0. If the mutation changes nothing, the reject assertion
    # above proved nothing — the fixture was never resolving to a schema at all.
    checked += 1
    unknown = fixtures / "reject" / "unknown-kind.yaml"
    rc_strict, out_strict = run_kubeconform(schema_dir, [unknown])
    rc_broken, out_broken = run_kubeconform(schema_dir, [unknown], ignore_missing=True)
    s_strict = summarise(out_strict) or {}
    s_broken = summarise(out_broken) or {}

    if s_strict.get("skipped") == s_broken.get("skipped"):
        print("FAIL  the mutation changed nothing — skipped count identical with and without "
              f"-ignore-missing-schemas ({s_strict.get('skipped')}). The assertion above did "
              "not fire and this suite proves nothing.")
        failures += 1
    elif not (rc_broken == 0 and s_broken.get("skipped", 0) >= 1):
        print(f"FAIL  with -ignore-missing-schemas the unknown kind should be admitted as a "
              f"skip, got rc={rc_broken} summary={s_broken}")
        failures += 1
    else:
        print(f"  mutation  -ignore-missing-schemas turns the unknown kind into rc=0 "
              f"Skipped:{s_broken['skipped']} (strict: rc={rc_strict} "
              f"Skipped:{s_strict.get('skipped')}) — the assertion fires")

    shutil.rmtree(schema_dir, ignore_errors=True)
    if failures:
        print(f"\n{failures} failure(s) across {checked} case(s).")
        return 1
    print(f"\nOK    {checked} case(s) behave as specified; the skip assertion is live.")
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
        # The evidence, not just a tally of it. The floor derives the counts
        # from these lines rather than trusting a number a gate could return
        # without having run anything.
        "lines": lines,
        "rejected": sum(1 for line in lines if any(m in line for m in ('rejected  ',))),
        "accepted": sum(1 for line in lines if any(m in line for m in ('admitted  ',))),
    }


def main():
    if len(sys.argv) != 2:
        die("usage: check-rendered-schemas.py {<render-dir>|--self-test}")
    if sys.argv[1] == "--self-test":
        return self_test()
    # Always, before validating anything. This gate's whole thesis is that a
    # skip counts as success, so a gate that stopped rejecting would report the
    # same clean summary it reports when everything is genuinely valid.
    if self_test() != 0:
        die("refusing to validate a render with a gate that has not proven it rejects", code=1)
    print()
    return gate(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
