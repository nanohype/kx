#!/usr/bin/env python3
"""Validate every rendered manifest against a real schema, including CRDs.

    scripts/check-rendered-schemas.py <render-dir>   # gate
    scripts/check-rendered-schemas.py --self-test    # prove the gate rejects

`helm template` proves a chart renders. It does not prove the result is a valid
Kubernetes object, and in a catalog mirror the objects that matter most are the
custom ones — ServiceMonitor, MutatingPolicy, Gateway, Platform. A render gate
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

Every schema is on disk, and each is held to a release kx pins. The custom
kinds take the CRDs the render ships. Cilium's take its own CRDs, vendored from
the pinned release, because its chart ships none and its operator registers
them at runtime. The built-in kinds take Kubernetes' own OpenAPI document,
vendored at the version the nodes run, converted the way kubeconform's
published strict schemas are: an object admits only the properties it names,
an optional field may be null, and a quantity or an int-or-string accepts a
string or a number. scripts/vendor-schemas.py writes the vendored sources, and
this gate refuses them when they are not the pinned release or not the files
schemas/sources.json records.
"""

import functools
import hashlib
import os
import json
import pathlib
import re
import shutil
import subprocess
import sys

import yaml

from gatelib import scratch
from gatelib.manifests import ManifestLoader
from gatelib.report import die as stop, guarded

# The tree this gate reads. Overridable so the suite-wide floor can point the
# gate at a fixture it wrote and observe the real exit status, rather than
# asking the gate to describe its own behaviour.
ROOT = pathlib.Path(os.environ.get("KX_GATE_ROOT", "") or pathlib.Path(__file__).resolve().parent.parent)


# The schemas this gate reads besides the render's own CRDs, vendored at the
# versions kx pins by scripts/vendor-schemas.py, which records every file's
# sha256 in sources.json. Nothing is fetched: a kind with no schema here or in
# the render is an error, never a lookup.
VENDORED = ROOT / "schemas"
SOURCES = VENDORED / "sources.json"
VENDOR_CMD = "scripts/vendor-schemas.py"

# Where each vendored source's pinned version is read from: the node image the
# cluster runs, and the chart that installs Cilium.
KIND_CONFIG = ROOT / "cluster" / "kind-config.yaml"
CILIUM_INSTALL = ROOT / "stack" / "core" / "cilium" / "install.sh"
NODE_IMAGE = re.compile(r"kindest/node:v(\d+\.\d+\.\d+)(?:@sha256:[0-9a-f]{64})?")
CHART_VERSION = re.compile(r"--version\s+v?(\d+\.\d+\.\d+)\b")

# The template every schema is written under, for the render's CRDs, Cilium's
# and the built-in kinds alike. kubeconform fills it from a resource's
# apiVersion: Group is the part before the slash, which for a core kind is the
# version itself, so a Pod's schema is v1/pod_v1.json.
SCHEMA_TEMPLATE = "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

# Two definitions Kubernetes reads from a string or a JSON number: a quantity,
# though its OpenAPI document declares a string, and an int-or-string. Each is
# held to what the API server accepts, as the schemas kubeconform publishes are,
# and written in place of a reference to it, so an optional one is not wrapped
# a second time.
EITHER = {
    "io.k8s.apimachinery.pkg.api.resource.Quantity": "number",
    "io.k8s.apimachinery.pkg.util.intstr.IntOrString": "integer",
}

# Longer than a healthy run and far short of the runner's own ceiling. Nothing
# is fetched, so a run that reaches it is kubeconform itself hanging.
KUBECONFORM_TIMEOUT_S = 300
SUMMARY_RE = re.compile(
    r"Valid:\s*(\d+).*?Invalid:\s*(\d+).*?Errors:\s*(\d+).*?Skipped:\s*(\d+)", re.S
)


die = functools.partial(stop, "check-rendered-schemas", code=2)


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


def write_crd(doc, schema_dir):
    """Write each version of one CRD's schema where SCHEMA_TEMPLATE finds it.

    Returns the number written. A later write of the same group, kind and
    version replaces an earlier one, which is what lets the render's own CRDs
    win over a vendored one.
    """
    spec = doc.get("spec") or {}
    group = spec.get("group")
    kind = (spec.get("names") or {}).get("kind")
    if not group or not kind:
        return 0
    written = 0
    for ver in spec.get("versions") or []:
        name = ver.get("name")
        schema = (ver.get("schema") or {}).get("openAPIV3Schema")
        if not name or not schema:
            continue
        out = schema_dir / group / f"{kind.lower()}_{name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(schema))
        written += 1
    return written


def crd_schemas(render_dir, schema_dir, filtered):
    """Every CRD in the rendered output, as kubeconform-locatable JSON schemas.

    Returns the number of schemas written, with the CRDs read and any structural
    problems; every other document is written to `filtered`. The count is
    returned rather than inferred later: a converter that silently emitted
    nothing would leave every custom resource resolving to no schema, which is
    skipping again with new machinery and a green check on top. Written into
    `schema_dir` when one is given, after whatever it already holds, so the
    CRD a pinned chart renders is the one its resources are held to.
    """
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
            written += write_crd(doc, schema_dir)
        if keep:
            (filtered / path.name).write_text(
                "\n---\n".join(yaml.safe_dump(d) for d in keep)
            )
    return written, crds, crd_problems


def _nullable(schema):
    """An optional field, which a manifest may set to null as readily as leave out."""
    if isinstance(schema.get("type"), str):
        return {**schema, "type": [schema["type"], "null"]}
    if "oneOf" in schema:
        return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _either(other):
    """A field Kubernetes reads from a string or from a JSON `other`."""
    return {"oneOf": [{"type": ["string", "null"]}, {"type": [other, "null"]}]}


def _strict(schema, reached):
    """One OpenAPI schema as kubeconform's own strict schemas hold its kind.

    An object that names its properties admits no others, an optional field may
    be null, and a field Kubernetes reads from a string or a number accepts
    either. `$ref`s are kept and their targets collected in `reached`, rather
    than inlined, so a recursive definition stays finite.
    """
    if "$ref" in schema:
        target = schema["$ref"].rsplit("/", 1)[-1]
        if target in EITHER:
            return _either(EITHER[target])
        reached.add(target)
        return {"$ref": schema["$ref"]}
    if schema.get("format") == "int-or-string":
        return _either("integer")
    nested = ("properties", "items", "additionalProperties", "allOf", "anyOf", "oneOf")
    out = {k: v for k, v in schema.items() if k != "description" and k not in nested}
    if "properties" in schema:
        required = set(schema.get("required") or [])
        props = {name: _strict(sub, reached) for name, sub in schema["properties"].items()}
        out["properties"] = {name: sub if name in required else _nullable(sub)
                             for name, sub in props.items()}
        if "additionalProperties" not in schema and \
                not schema.get("x-kubernetes-preserve-unknown-fields"):
            out["additionalProperties"] = False
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            out[key] = _nullable(_strict(schema[key], reached))
        elif key in schema and key not in out:
            out[key] = schema[key]
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            out[key] = [_strict(sub, reached) for sub in schema[key]]
    return out


def pinned_versions():
    """({source: version}, problems): the releases the vendored schemas must match.

    Kubernetes is the version every node image in the kind config runs, and
    Cilium the version its install pins. Each has to be exactly one version: a
    kind config whose nodes disagree, or an install with no pin, leaves nothing
    to hold the schemas to.
    """
    pins, problems = {}, []
    for source, path, pattern in (("kubernetes", KIND_CONFIG, NODE_IMAGE),
                                  ("cilium", CILIUM_INSTALL, CHART_VERSION)):
        found = set(pattern.findall(path.read_text())) if path.is_file() else set()
        if len(found) == 1:
            pins[source] = found.pop()
        else:
            problems.append(f"{path.relative_to(ROOT)} pins {sorted(found) or 'no'} {source} "
                            f"version(s); the vendored schemas are held to exactly one.")
    return pins, problems


def vendored_sources(pins, root=None):
    """({source: directory}, problems) for the vendored schemas.

    A source is usable when its vendored version is the pinned one and every
    file in its directory is the one sources.json records, by sha256, with
    nothing unrecorded beside it. A hand edit to a vendored schema would change
    what every resource is held to while every check stayed green.
    """
    root = root or VENDORED
    try:
        sources = json.loads((root / SOURCES.name).read_text())
    except FileNotFoundError:
        return {}, [f"{SOURCES.relative_to(ROOT)} does not exist — run {VENDOR_CMD} for "
                    f"each source."]
    except json.JSONDecodeError as e:
        return {}, [f"{SOURCES.relative_to(ROOT)} does not parse: {e}"]
    dirs, problems = {}, []
    for source, version in sorted(pins.items()):
        record = sources.get(source) or {}
        remedy = f"run {VENDOR_CMD} {source} {version}"
        if record.get("version") != f"v{version}":
            problems.append(f"{source} {record.get('version') or '(nothing)'} is vendored, but "
                            f"kx pins v{version} — {remedy}.")
            continue
        directory = root / source / record["version"]
        listed = record.get("files") or {}
        present = {p.name for p in directory.iterdir()} if directory.is_dir() else set()
        for name in sorted(present - set(listed)):
            problems.append(f"{source}/{record['version']}/{name} is vendored but sources.json "
                            f"does not record it.")
        for name, entry in sorted(listed.items()):
            path = directory / name
            if not path.is_file():
                problems.append(f"{source}/{record['version']}/{name} is recorded but missing "
                                f"— {remedy}.")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
                problems.append(f"{source}/{record['version']}/{name} is not the file "
                                f"sources.json records — {remedy}.")
        dirs[source] = directory
    return dirs, problems


def vendored_crds(directory, schema_dir):
    """Every CRD vendored in `directory`, written as schemas. Returns the count."""
    written = 0
    for path in sorted(directory.glob("*.yaml")):
        for doc in yaml.load_all(path.read_text(), Loader=ManifestLoader):
            if isinstance(doc, dict) and doc.get("kind") == "CustomResourceDefinition":
                written += write_crd(doc, schema_dir)
    return written


def kinds_in(paths):
    """Every (apiVersion, kind) the manifests at `paths` hold."""
    kinds = set()
    for path in paths:
        for doc in yaml.load_all(pathlib.Path(path).read_text(), Loader=ManifestLoader):
            if isinstance(doc, dict) and doc.get("kind"):
                kinds.add((doc.get("apiVersion"), doc["kind"]))
    return kinds


def vendored_store(schema_dir, crd_dir, filtered, targets_of):
    """The whole schema store for one run, in `schema_dir`.

    Cilium's vendored CRDs first and the CRDs in `crd_dir` after them, so one a
    pinned chart renders wins; then the built-in kinds the targets hold, from
    Kubernetes' own document at the version the nodes run. `targets_of` maps the
    manifests crd_schemas filtered out to the files to validate. Dies on a
    vendoring problem, since a store built from the wrong release validates
    against something kx does not run.
    """
    pins, problems = pinned_versions()
    dirs, more = vendored_sources(pins)
    for problem in problems + more:
        print(f"  VENDOR FAIL  {problem}", file=sys.stderr)
    if problems or more:
        die("the vendored schemas do not match what kx pins — refusing to validate against "
            "them.", code=1)
    cilium = vendored_crds(dirs["cilium"], schema_dir)
    rendered, crds, crd_problems = crd_schemas(crd_dir, schema_dir, filtered)
    targets = targets_of(filtered)
    swagger = json.loads((dirs["kubernetes"] / "swagger.json").read_text())
    builtin = builtin_schemas(swagger, schema_dir, kinds_in(targets))
    counts = {"cilium": cilium, "rendered": rendered, "builtin": builtin,
              "crds": crds, "kubernetes": f"v{pins['kubernetes']}", "cilium_version":
              f"v{pins['cilium']}"}
    return targets, counts, crd_problems


def builtin_schemas(swagger, schema_dir, wanted):
    """The built-in kinds in `wanted`, from Kubernetes' OpenAPI document.

    `wanted` is a set of (apiVersion, kind). Each kind's schema carries every
    definition it reaches, so a file is complete on its own. Returns the number
    written.
    """
    definitions = swagger.get("definitions") or {}
    converted = {}
    written = 0
    for name, definition in sorted(definitions.items()):
        for gvk in definition.get("x-kubernetes-group-version-kind") or []:
            group, version, kind = gvk.get("group", ""), gvk.get("version"), gvk.get("kind")
            api_version = f"{group}/{version}" if group else version
            if (api_version, kind) not in wanted:
                continue
            reached, pending, body = set(), [name], None
            while pending:
                current = pending.pop()
                if current in converted:
                    found = converted[current][1]
                else:
                    found = set()
                    converted[current] = (_strict(definitions[current], found), found)
                if current == name:
                    body = converted[current][0]
                for ref in found - reached:
                    reached.add(ref)
                    pending.append(ref)
            schema = {"$schema": "http://json-schema.org/schema#", **body,
                      "definitions": {ref: converted[ref][0] for ref in sorted(reached)}}
            out = schema_dir / (group or version) / f"{kind.lower()}_{version}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(schema))
            written += 1
    return written


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
    # One location, on disk. Every schema a resource can be held to is in
    # `schema_dir`: the render's own CRDs, Cilium's at its pinned release, and
    # the built-in kinds at the Kubernetes the nodes run. Nothing resolves over
    # the network, so a kind with no schema here is an error rather than a
    # lookup whose answer depends on the day the job ran.
    cmd += ["-schema-location", f"{schema_dir}/{SCHEMA_TEMPLATE}"]
    cmd += [str(t) for t in targets]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=KUBECONFORM_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return 1, f"kubeconform did not finish within {KUBECONFORM_TIMEOUT_S}s."
    return p.returncode, (p.stdout + p.stderr)


def summarise(output):
    m = SUMMARY_RE.search(output)
    if not m:
        return None
    valid, invalid, errors, skipped = (int(x) for x in m.groups())
    return {"valid": valid, "invalid": invalid, "errors": errors, "skipped": skipped}


def require_kubeconform():
    """The validator has to exist before anything is reported about a render.

    Without this the first subprocess raises FileNotFoundError and the gate dies
    with a Python traceback — exit non-zero, so it reads as a rejection, while
    blaming the interpreter for a missing tool. A gate that depends on a binary
    asserts it, or it can fail in a way that looks like a verdict.
    """
    if shutil.which("kubeconform") is None:
        die("kubeconform is not on PATH. This gate validates nothing without it, and "
            "reporting a clean render would be worse than failing.", code=2)
    probe = subprocess.run(["kubeconform", "-v"], capture_output=True, text=True,
                           timeout=30, check=False)
    if probe.returncode != 0:
        die(f"kubeconform is on PATH but will not run: "
            f"{(probe.stderr or probe.stdout).strip()[:200]}", code=2)


def gate(render_dir):
    require_kubeconform()
    render_dir = pathlib.Path(render_dir)
    if not render_dir.is_dir():
        die(f"{render_dir} is not a directory — run render-check.sh with KX_RENDER_OUT set.")

    manifests = sorted(render_dir.glob("*.yaml"))
    # A render that produced nothing would otherwise validate an empty set and
    # report success. This is the failure mode the gate is least able to notice
    # from its own output, so it is asserted first.
    if not manifests:
        die(f"no rendered manifests in {render_dir} — refusing to report success over an empty set.")

    with scratch.directory("kx-schemas-") as schema_dir, \
            scratch.directory("kx-manifests-") as filtered:
        targets, counts, crd_problems = vendored_store(
            schema_dir, render_dir, filtered, lambda kept: sorted(kept.glob("*.yaml")))
        if crd_problems:
            for pr in crd_problems:
                print(f"  CRD FAIL  {pr}", file=sys.stderr)
            die(f"{len(crd_problems)} malformed CRD(s) — the schema source is wrong, so "
                "anything validated against it would be meaningless.", code=1)
        if counts["rendered"] == 0:
            die("no CRD schemas were derived from the rendered output — every custom "
                "resource would resolve to no schema. Refusing to validate.")
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
              f"{counts['rendered']} rendered CRD schema(s), {counts['cilium']} of Cilium "
              f"{counts['cilium_version']}'s and {counts['builtin']} built-in kind(s) of "
              f"Kubernetes {counts['kubernetes']}, none skipped; "
              f"{counts['crds']} CRD(s) structurally checked.")
        return 0


def self_test():
    """Prove the gate rejects, and that its skip assertion actually fires."""
    require_kubeconform()
    fixtures = ROOT / "tests" / "schema"
    failures = 0
    checked = 0

    # The store the gate builds, over the fixtures: Cilium's vendored CRDs, the
    # fixture CRD, and the built-in kinds the cases hold.
    cases = sorted((fixtures / "accept").glob("*.yaml")) + sorted((fixtures / "reject").glob("*.yaml"))
    with scratch.directory("kx-schemas-") as schema_dir, \
            scratch.directory("kx-manifests-") as filtered:
        _targets, counts, _problems = vendored_store(
            schema_dir, fixtures / "crds", filtered, lambda _kept: cases)
        if counts["rendered"] == 0:
            die("the converter derived no schemas from tests/schema/crds/")
        if not (schema_dir / "kx.test" / "widget_v1.json").is_file():
            die(f"the Widget schema is missing; {counts['rendered']} other schema(s) present.")
        for kind, path in (("Deployment", "apps/deployment_v1.json"),
                           ("CiliumNetworkPolicy", "cilium.io/ciliumnetworkpolicy_v2.json")):
            if not (schema_dir / path).is_file():
                die(f"the {kind} schema is missing from the vendored store, so the cases that "
                    f"hold one would prove nothing about it.")

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

    # The vendored store is refused, not used, when it is not what kx pins or
    # not what sources.json records.
    for name, problems in vendoring_cases():
        checked += 1
        if problems:
            print(f"  rejected  {name}: {problems[0]}")
        else:
            print(f"FAIL  {name} was ACCEPTED: the vendored store would be used as it is.")
            failures += 1

    if failures:
        print(f"\n{failures} failure(s) across {checked} case(s).")
        return 1
    print(f"\nOK    {checked} case(s) behave as specified; the skip assertion is live.")
    return 0


def vendoring_cases():
    """(name, problems) for each way the vendored store can be wrong.

    Run on a copy of the vendored Cilium source, so the hand edit and the stray
    file land somewhere the gate is not also reading.
    """
    pins, _ = pinned_versions()
    yield "a pin the vendored release does not match", vendored_sources({"kubernetes": "0.0.1"})[1]
    with scratch.directory("kx-vendored-") as copy:
        shutil.copy(SOURCES, copy / SOURCES.name)
        version = f"v{pins['cilium']}"
        shutil.copytree(VENDORED / "cilium" / version, copy / "cilium" / version)
        untouched = vendored_sources({"cilium": pins["cilium"]}, root=copy)[1]
        if untouched:
            die(f"an untouched copy of the vendored store is refused ({untouched[0]}), so the "
                f"cases below could not tell an edit from the copy.")
        edited = sorted((copy / "cilium" / version).glob("*.yaml"))[0]
        original = edited.read_bytes()
        edited.write_bytes(original + b"\n# edited by hand\n")
        yield "a vendored file edited by hand", vendored_sources({"cilium": pins["cilium"]}, root=copy)[1]
        edited.write_bytes(original)
        (copy / "cilium" / version / "extra.yaml").write_text("kind: CustomResourceDefinition\n")
        yield "a file beside the vendored ones that nothing records", vendored_sources(
            {"cilium": pins["cilium"]}, root=copy)[1]


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
    sys.exit(guarded(main, "check-rendered-schemas", 2))
