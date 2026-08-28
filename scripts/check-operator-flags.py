#!/usr/bin/env python3
"""Every flag this slice hands the operator reaches a binary that defines it.

    scripts/check-operator-flags.py --render <dir> --chart-source <dir>
    scripts/check-operator-flags.py --self-test

`stack/ai-platform/operator/values.yaml` passes flags through the chart's
`extraArgs`. That path is uninstrumented on both sides, and the two sides say so
in opposite directions. The chart declares `extraArgs` as an untyped array, so
helm renders whatever is there verbatim and the render gate has nothing to hold
it to. The sibling's own args check states that `.Values.extraArgs` is out of its
scope because a values schema is the control for that path — and the schema
constrains the type and not the contents. The chart's default is an empty list,
so the sibling exercises the path nowhere: this workspace is the only tree that
puts anything in it, which makes this the only place it can be checked.

Two conditions, because a flag can be lost in two independent ways and each is
silent on its own.

DELIVERED — the flag appears in the rendered operator Deployment's container
arguments. One `with .Values.extraArgs` block in the chart's Deployment template
is the whole of that path; a template that stops reading it renders cleanly and
drops the flag with no error anywhere. Nothing else in this workspace reads a
container's arguments, so a dropped `--disable-aws` leaves the operator
reconciling against AWS on a laptop with every check green.

ACCEPTED — the flag is defined in the flag source `stack/upstream.json` names,
in the checkout at the ref this commit pins. Go builds `flag.CommandLine` with
ExitOnError, so an unrecognised flag exits non-zero before the manager starts.

Both authorities arrive as argv paths rather than through the environment. The
suite floor runs a probed gate with the ambient environment inherited and only
`KX_GATE_ROOT` overridden, so a gate resolving `KX_EKS_AGENT_PLATFORM_DIR` would
read a developer's real checkout instead of the fixture it was handed. An argv
path is formatted into the probe and cannot be steered.

Scope, as a limit rather than a reassurance:

* It never reads the sibling's default branch. Whether the pin is behind is not
  measured here and is not measured anywhere in this workspace. Age is not a
  property of a commit, and the pin exists so this verdict stays a fact about
  the commit under test.
* It compares names. A flag whose meaning inverts, whose default moves, or that
  becomes a no-op keeps its name and passes.
* It sees flags registered by a `flag.*` call in the file the manifest names.
  Flags bound through an options struct are invisible to it, so a flag passed
  here that such a struct registers would read as undefined.
* It says nothing about the other values keys this slice sets. Whether the chart
  still accepts `image.tag` or `networkPolicy.engine` is helm's verdict, reached
  when the render gate templates this slice against the chart's schema at the
  same ref.
* It never compiles, builds or runs the operator. `operators/` is the image
  build context; a drift that changes only the operator's behaviour passes.
* It watches one direction. The sibling's local installer names this workspace's
  task targets and resolves a checkout of it, and nothing on either side
  observes that.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import re
import sys
import tempfile

import yaml

# The tree this gate reads. Overridable so the suite-wide floor can point the
# gate at a fixture it wrote and observe the real exit status, rather than
# asking the gate to describe its own behaviour.
#
# The CHECKOUT is not resolved this way and takes an argv path instead. The
# floor runs a probed gate with the ambient environment inherited and only this
# variable overridden, so a checkout resolved from the environment would be the
# developer's own — the fixture would be graded against a tree the floor never
# wrote.
ROOT = pathlib.Path(
    os.environ.get("KX_GATE_ROOT", "") or pathlib.Path(__file__).resolve().parent.parent
)


class ManifestLoader(yaml.SafeLoader):
    """SafeLoader that tolerates a bare `=` in rendered CRDs.

    prometheus-operator-crds ships an Alertmanager matchType enum listing the
    match operators, and `- =` unquoted is YAML 1.1's "default value" tag rather
    than the string. SafeLoader refuses it outright, taking the whole document
    with it — and a document lost while reading the render is a container this
    gate never examines, which is the failure it exists to prevent.
    """


ManifestLoader.add_constructor(
    "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
)

# The slice whose values are under test, and the sibling entry that names where
# its flags are defined. Both are paths in the tree, not in the checkout.
VALUES = "stack/ai-platform/operator/values.yaml"
MANIFEST = "stack/upstream.json"
SIBLING = "eks-agent-platform"

# How the operator Deployment is found in a render. The repository that builds
# this image is the one whose flags are being checked, so the reference is the
# join between the two halves.
OPERATOR_IMAGE = "ghcr.io/nanohype/eks-agent-platform/operator"

# A flag as `extraArgs` and a rendered argument list carry it: `--name`,
# `--name=value`. The name is what is compared, because a value is the chart's
# business and a name is the binary's.
FLAG = re.compile(r"^--([A-Za-z0-9][A-Za-z0-9._-]*)")

# Go's flag package, in the two shapes that register a name: the pointer-binding
# `flag.StringVar(&x, "name", ...)` forms and the value-returning
# `flag.String("name", ...)` forms.
FLAG_DEF = re.compile(
    r'flag\.[A-Za-z0-9]+Var\(\s*&[^,]+,\s*"([^"]+)"'
    r'|flag\.(?:Bool|String|Int|Int64|Uint|Uint64|Float64|Duration|Func|TextVar)\(\s*"([^"]+)"'
)


def die(msg: str) -> int:
    print(f"check-operator-flags: {msg}", file=sys.stderr)
    return 1


def flag_names(entries) -> list[str]:
    """The flag names in a list of command-line arguments, in order."""
    names = []
    for entry in entries or []:
        m = FLAG.match(str(entry).strip())
        if m:
            names.append(m.group(1))
    return names


def flags_set(values_text: str) -> list[str]:
    """The flags this slice's values hand the operator through `extraArgs`."""
    doc = yaml.safe_load(values_text) or {}
    if not isinstance(doc, dict):
        return []
    return flag_names(doc.get("extraArgs"))


def flags_defined(go_source: str) -> set[str]:
    """The flag names a Go source file registers."""
    return {a or b for a, b in FLAG_DEF.findall(go_source)}


def flags_delivered(render_dir: pathlib.Path) -> tuple[set[str], int]:
    """The arguments the rendered operator containers carry, and how many there were.

    The count is returned rather than inferred from the set, because a render
    holding the Deployment with an empty argument list and a render holding no
    Deployment at all are different failures and only one of them is this gate's
    to report.
    """
    delivered: set[str] = set()
    containers = 0
    for path in sorted(render_dir.glob("*.yaml")):
        text = path.read_text(errors="ignore")
        # A file that never spells the image cannot hold a container running it,
        # and parsing the whole stack to learn that costs about a minute. The
        # direction is safe: a pre-filter that wrongly skipped the operator's own
        # file leaves nothing to report on, which is the refusal below, not a
        # clean verdict. Sound because a rendered `image:` value is a plain
        # one-line scalar.
        if OPERATOR_IMAGE not in text:
            continue
        with contextlib.suppress(yaml.YAMLError):
            for doc in yaml.load_all(text, Loader=ManifestLoader):
                if not isinstance(doc, dict):
                    continue
                spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
                if not isinstance(spec, dict):
                    continue
                for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
                    if not isinstance(c, dict):
                        continue
                    if str(c.get("image", "")).startswith(OPERATOR_IMAGE):
                        containers += 1
                        delivered.update(flag_names(c.get("args")))
    return delivered, containers


def report(setflags, defined, delivered) -> list[str]:
    """The flags that fail either condition, named with which one and why."""
    problems = []
    for name in setflags:
        if name not in delivered:
            problems.append(
                f"{VALUES} sets --{name} in extraArgs, and the operator Deployment the "
                f"render produced carries no such argument. The chart decides whether "
                f"extraArgs reaches the container; a template that stops reading it "
                f"renders cleanly and drops the flag with no error anywhere."
            )
        if name not in defined:
            near = sorted(
                d for d in defined
                if set(d.split("-")) & set(name.split("-"))
            )
            hint = f" Defined flags sharing a word with it: {', '.join('--' + n for n in near)}" if near else ""
            problems.append(
                f"{VALUES} sets --{name} in extraArgs, and the flag source defines no flag "
                f"by that name in the checkout at the ref {MANIFEST} pins. Go builds "
                f"flag.CommandLine with ExitOnError, so the operator would exit before the "
                f"manager starts.{hint}"
            )
    return problems


# Every case the shipped comparison has to get right, run before any verdict.
# The subjects are pure over text, so the proof drives the code that ships
# rather than a restatement of it.
CONTROLS = [
    ("a flag defined and delivered is accepted",
     ["disable-aws"], {"disable-aws", "leader-elect"}, {"disable-aws", "leader-elect"}, None),
    ("a flag the source does not define is rejected, and named",
     ["disable-aws"], {"aws-disabled", "leader-elect"}, {"disable-aws"}, "--disable-aws"),
    ("a renamed flag prints the near miss",
     ["disable-aws"], {"aws-disabled"}, {"disable-aws"}, "--aws-disabled"),
    ("a flag missing from the rendered arguments is rejected, and named",
     ["disable-aws"], {"disable-aws"}, {"leader-elect"}, "--disable-aws"),
    ("two flags, both defined and both delivered, do not fire",
     ["disable-aws", "leader-elect"], {"disable-aws", "leader-elect"},
     {"disable-aws", "leader-elect"}, None),
]


def self_test() -> int:
    problems = 0

    for name, setflags, defined, delivered, must_say in CONTROLS:
        found = report(setflags, defined, delivered)
        if must_say is None:
            if found:
                print(f"FAIL  {name} — reported {len(found)} problem(s) over a clean set")
                problems += 1
            else:
                print(f"  ok  {name}")
            continue
        if not found:
            print(f"FAIL  {name} — reported nothing")
            problems += 1
        elif not any(must_say in p for p in found):
            print(f"FAIL  {name} — rejected without naming {must_say!r}")
            problems += 1
        else:
            print(f"  ok  {name}")

    # The extractors, over the shapes the real inputs carry. A comparison proven
    # correct over sets it was handed says nothing about a parser that returns
    # the wrong sets.
    cases = [
        ("extraArgs yields the flag name without its value",
         flags_set("extraArgs:\n  - --disable-aws\n  - --region=us-west-2\n"),
         ["disable-aws", "region"]),
        ("a values file with no extraArgs yields nothing",
         flags_set("image:\n  tag: dev\n"), []),
        ("both Go registration shapes are seen",
         sorted(flags_defined(
             'flag.BoolVar(&d, "disable-aws", false, "")\n'
             'flag.DurationVar(&t, "tenant-requeue-interval", 0, "")\n'
             'flag.String("region", "", "")\n')),
         ["disable-aws", "region", "tenant-requeue-interval"]),
        ("a source registering nothing yields nothing",
         sorted(flags_defined("func main() { run() }\n")), []),
    ]
    for name, got, want in cases:
        if got != want:
            print(f"FAIL  {name} — got {got!r}, want {want!r}")
            problems += 1
        else:
            print(f"  ok  {name}")

    # The render reader, against a tree written here. A Deployment carrying some
    # other image must not contribute arguments, or the gate would credit a flag
    # to the operator that something else was passed.
    with tempfile.TemporaryDirectory() as d:
        r = pathlib.Path(d)
        (r / "op.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: op}\nspec:\n"
            "  template:\n    spec:\n      containers:\n        - name: c\n"
            f"          image: {OPERATOR_IMAGE}:dev\n"
            "          args: [--disable-aws, --region=us-west-2]\n"
            "---\napiVersion: apps/v1\nkind: Deployment\nmetadata: {name: other}\nspec:\n"
            "  template:\n    spec:\n      containers:\n        - name: c\n"
            "          image: r/other:1\n          args: [--not-the-operator]\n"
        )
        delivered, containers = flags_delivered(r)
        if delivered != {"disable-aws", "region"} or containers != 1:
            print(f"FAIL  the render reader took {sorted(delivered)} from {containers} "
                  f"container(s) — it must read the operator's and nothing else")
            problems += 1
        else:
            print("  ok  only the operator container's arguments are read")

        # A bare `=` in a rendered CRD takes the whole document with it under a
        # plain SafeLoader, and a lost document is a container never examined.
        (r / "crd.yaml").write_text("a:\n  - =\n")
        again, _ = flags_delivered(r)
        if again != delivered:
            print("FAIL  a document containing a bare `=` changed what the reader found")
            problems += 1
        else:
            print("  ok  a bare `=` in the render does not cost a document")

        # The pre-filter skips a file that never names the image. Proven against
        # a file that both names it and is the only one there, because a filter
        # that skipped everything would satisfy a test written the other way
        # round and report an empty render as clean.
        (r / "op.yaml").unlink()
        empty, none_seen = flags_delivered(r)
        if empty or none_seen:
            print(f"FAIL  the reader found {sorted(empty)} in {none_seen} container(s) with "
                  f"the operator's file removed")
            problems += 1
        else:
            print("  ok  a render naming the image nowhere yields nothing to report on")

    if problems:
        print(f"\n{problems} control(s) did not behave as specified.")
        return 1
    print(f"\nOK    {len(CONTROLS) + len(cases) + 3} case(s) behave as specified.")
    return 0


class Unreachable(Exception):
    """A precondition this gate cannot reach a verdict without.

    Raised rather than returned so the chain below reads in the order the inputs
    are actually resolved, and caught in one place so the process still exits
    through a named sentence. A traceback exits non-zero exactly as a refusal
    does, which is why the floor rejects one as evidence of either.
    """


def gather(render_arg: str, source_arg: str):
    """The three sets this gate compares, or the sentence naming what stopped it."""
    # --chart-source first, and deliberately: it is the authority this gate has
    # that the commit does not contain, so it is the refusal the suite floor's
    # authority probe reaches. A precondition checked ahead of it would be the
    # one that fired, and that probe carries no expected text to tell them apart.
    source = pathlib.Path(source_arg)
    if not source.is_dir():
        raise Unreachable(f"no eks-agent-platform checkout at {source} — this gate reads the "
                          f"operator's flag definitions out of that repository and cannot "
                          f"reach a verdict from this one alone")

    render = pathlib.Path(render_arg)
    if not render.is_dir():
        raise Unreachable(f"no render at {render} — this gate reads the arguments the "
                          f"operator Deployment was rendered with, and there is nothing "
                          f"here to read")

    manifest_path = ROOT / MANIFEST
    if not manifest_path.is_file():
        raise Unreachable(f"no manifest at {manifest_path} — it names the file the "
                          f"operator's flags are defined in")
    manifest = json.loads(manifest_path.read_text())
    flag_source = ((manifest.get("siblings") or {}).get(SIBLING) or {}).get("flagSource")
    if not flag_source:
        raise Unreachable(f"{MANIFEST} names no flagSource for {SIBLING}. That is the file "
                          f"the operator's flags are defined in, so without it every flag "
                          f"this slice sets would be held to nothing")

    source_file = source / flag_source
    if not source_file.is_file():
        raise Unreachable(f"{flag_source} is not in the checkout at {source} — the "
                          f"operator's flag definitions have moved, and a gate that cannot "
                          f"find them would report every flag this slice sets as undefined")

    defined = flags_defined(source_file.read_text(errors="ignore"))
    if not defined:
        raise Unreachable(f"extracted no flag definitions from {flag_source} — the pattern "
                          f"that finds them has stopped matching, so every flag this slice "
                          f"sets would be reported undefined")

    values_path = ROOT / VALUES
    if not values_path.is_file():
        raise Unreachable(f"no values file at {values_path} — it is the subject of this gate")
    setflags = flags_set(values_path.read_text())
    if not setflags:
        raise Unreachable(f"{VALUES} sets no flag in extraArgs — refusing to report a clean "
                          f"fit over an empty set. The flag that starts the operator without "
                          f"AWS is passed there; moved to an install-time --set, nothing "
                          f"here sees it")

    delivered, containers = flags_delivered(render)
    if not containers:
        raise Unreachable(f"no Deployment running {OPERATOR_IMAGE} in the render at {render} "
                          f"— the render this reads did not include the operator slice, so "
                          f"there are no arguments to hold the flags to")

    return setflags, defined, delivered, flag_source


def main() -> int:
    ap = argparse.ArgumentParser(description="Flags this slice sets are delivered and accepted.")
    ap.add_argument("--render", help="directory render-check.sh wrote with KX_RENDER_OUT set")
    ap.add_argument("--chart-source", help="eks-agent-platform checkout at the pinned ref")
    ap.add_argument("--self-test", action="store_true", help="prove the gate rejects")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    with contextlib.redirect_stdout(io.StringIO()):
        proven = self_test() == 0
    if not proven:
        self_test()
        return die("refusing to report a fit with a gate that has not proven it rejects")

    if not args.render or not args.chart_source:
        return die("both --render and --chart-source are required")

    try:
        setflags, defined, delivered, flag_source = gather(args.render, args.chart_source)
    except Unreachable as e:
        return die(str(e))

    problems = report(setflags, defined, delivered)
    if problems:
        print(f"check-operator-flags: {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"        {p}", file=sys.stderr)
        return 1

    print(f"check-operator-flags: {len(setflags)} flag(s) in extraArgs, each carried by the "
          f"rendered operator Deployment's arguments and each among the {len(defined)} "
          f"{flag_source} defines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
