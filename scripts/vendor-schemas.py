#!/usr/bin/env python3
"""Vendor the schemas the render gate validates against, from the pinned releases.

    scripts/vendor-schemas.py kubernetes <version>   # the node image's Kubernetes, e.g. 1.36.1
    scripts/vendor-schemas.py cilium <version>       # the Cilium chart's pin, e.g. 1.19.6
    scripts/vendor-schemas.py --check                # re-fetch every recorded release, compare

check-rendered-schemas.py validates every rendered resource without the network,
against two sources vendored at the versions kx pins: Kubernetes' own OpenAPI
document, for the built-in kinds, and Cilium's CRDs, which its chart does not
ship. Cilium's operator registers them at runtime, so the render holds nothing
to validate a CiliumNetworkPolicy against.

Both are taken verbatim from the Go module of the pinned release, as
proxy.golang.org serves it, and only after the module's hash matches the one
sum.golang.org records for that release. schemas/sources.json records the module,
that hash and each file's sha256, and the gate refuses a vendored file that no
longer matches its record. The transparency log's signature is not verified
here: the recorded hash is read over TLS from sum.golang.org itself.

Run on a workstation when a pin moves. The render gate names the command when
the node image or the Cilium chart moves past what is vendored, and nothing in
CI runs it: CI stays off the network for schemas.
"""

import base64
import hashlib
import io
import json
import pathlib
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDORED = ROOT / "schemas"
SOURCES = VENDORED / "sources.json"
PROXY = "https://proxy.golang.org"
SUMDB = "https://sum.golang.org"

# The Cilium module zip is tens of megabytes; this bounds a stalled transfer,
# not a slow one.
FETCH_TIMEOUT_S = 300

# Each source's module, and the files taken from it, as paths inside the module.
# The patterns are exact: only a file they match is written, and only under its
# own base name, so nothing in the archive chooses where anything lands.
MODULES = {
    "kubernetes": {
        "module": "k8s.io/kubernetes",
        "files": re.compile(r"api/openapi-spec/swagger\.json"),
    },
    "cilium": {
        "module": "github.com/cilium/cilium",
        "files": re.compile(r"pkg/k8s/apis/cilium\.io/client/crds/v2(?:alpha1)?/[a-z0-9]+\.yaml"),
    },
}

VERSION = re.compile(r"\d+\.\d+\.\d+")

README = ("Written by scripts/vendor-schemas.py; do not edit by hand. Each source is a Go "
          "module at the release kx pins, checked against the hash sum.golang.org records "
          "for it before any file was taken; each file's sha256 is checked by "
          "check-rendered-schemas.py before it validates anything against it.")


def fail(msg: str):
    raise SystemExit(f"vendor-schemas: {msg}")


def escape(module: str) -> str:
    """A module path as the proxy and the checksum database spell it."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group().lower(), module)


def fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as e:
        fail(f"could not fetch {url}: {getattr(e, 'reason', e)}")


def recorded_hash(module: str, version: str) -> str:
    """The h1 hash sum.golang.org records for a release's module zip."""
    body = fetch(f"{SUMDB}/lookup/{escape(module)}@v{version}").decode()
    for line in body.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[:2] == [module, f"v{version}"]:
            return parts[2]
    fail(f"sum.golang.org records no module hash for {module}@v{version}")


def h1(archive: zipfile.ZipFile) -> str:
    """Go's dirhash over a module zip: the hash the checksum database records."""
    summary = hashlib.sha256()
    for name in sorted(n for n in archive.namelist() if not n.endswith("/")):
        if "\n" in name:
            fail(f"a file name in the module zip holds a newline: {name!r}")
        summary.update(f"{hashlib.sha256(archive.read(name)).hexdigest()}  {name}\n".encode())
    return "h1:" + base64.b64encode(summary.digest()).decode()


def release_files(source: str, version: str) -> tuple[str, dict[str, tuple[str, bytes]]]:
    """(the module hash, {base name: (path in the module, bytes)}) for one release."""
    module = MODULES[source]["module"]
    pattern = MODULES[source]["files"]
    want = recorded_hash(module, version)
    archive = zipfile.ZipFile(io.BytesIO(fetch(f"{PROXY}/{escape(module)}/@v/v{version}.zip")))
    got = h1(archive)
    if got != want:
        fail(f"{module}@v{version} hashes to {got}, but sum.golang.org records {want} — "
             f"refusing to vendor anything from it")
    prefix = f"{module}@v{version}/"
    files: dict[str, tuple[str, bytes]] = {}
    for name in archive.namelist():
        inner = name[len(prefix):] if name.startswith(prefix) else None
        if inner is None or not pattern.fullmatch(inner):
            continue
        base = inner.rsplit("/", 1)[-1]
        if base in files:
            fail(f"{module}@v{version} holds two files named {base}: {files[base][0]} and {inner}")
        files[base] = (inner, archive.read(name))
    if not files:
        fail(f"{module}@v{version} holds no file matching {pattern.pattern}")
    return want, files


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def load_sources() -> dict:
    return json.loads(SOURCES.read_text()) if SOURCES.is_file() else {"_README": README}


def vendor(source: str, version: str) -> int:
    module_hash, files = release_files(source, version)
    sources = load_sources()
    previous = (sources.get(source) or {}).get("version")
    dest = VENDORED / source / f"v{version}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    record = {"module": MODULES[source]["module"], "version": f"v{version}",
              "h1": module_hash, "files": {}}
    for base, (inner, body) in sorted(files.items()):
        (dest / base).write_bytes(body)
        record["files"][base] = {"from": inner, "sha256": sha256(body)}
    # One release per source: the gate validates against the pinned one only.
    if previous and previous != record["version"]:
        shutil.rmtree(VENDORED / source / previous, ignore_errors=True)
    sources["_README"] = README
    sources[source] = record
    SOURCES.write_text(json.dumps(sources, indent=2, sort_keys=True) + "\n")
    print(f"vendor-schemas: {len(files)} file(s) from {record['module']}@{record['version']} "
          f"({module_hash}) into {dest.relative_to(ROOT)}")
    return 0


def check() -> int:
    """Re-fetch every recorded release and confirm schemas/ is exactly what it holds."""
    sources = load_sources()
    recorded = {k: v for k, v in sources.items() if not k.startswith("_")}
    if not recorded:
        fail(f"{SOURCES.relative_to(ROOT)} records no source — nothing to check")
    problems = []
    for source, record in sorted(recorded.items()):
        if source not in MODULES:
            problems.append(f"{source}: not a source this script knows how to vendor")
            continue
        version = record.get("version", "").removeprefix("v")
        module_hash, files = release_files(source, version)
        if module_hash != record.get("h1"):
            problems.append(f"{source}: sum.golang.org records {module_hash}, "
                            f"sources.json {record.get('h1')}")
        listed = record.get("files") or {}
        if set(files) != set(listed):
            problems.append(f"{source}: the release holds {sorted(files)}, "
                            f"sources.json lists {sorted(listed)}")
        for base, (_inner, body) in sorted(files.items()):
            on_disk = VENDORED / source / record["version"] / base
            if (listed.get(base) or {}).get("sha256") != sha256(body):
                problems.append(f"{source}/{base}: sources.json records another sha256 "
                                f"than the release's file")
            if not on_disk.is_file() or sha256(on_disk.read_bytes()) != sha256(body):
                problems.append(f"{on_disk.relative_to(ROOT)} is not the release's file")
    for p in problems:
        print(f"FAIL  {p}", file=sys.stderr)
    if problems:
        return 1
    releases = ", ".join(f"{s} {r['version']}" for s, r in sorted(recorded.items()))
    print(f"vendor-schemas: every vendored file is its release's, for {releases}")
    return 0


USAGE = ("usage: vendor-schemas.py {kubernetes|cilium} <version>\n"
         "       vendor-schemas.py --check")


def main(argv: list[str]) -> int:
    if argv[1:] == ["--check"]:
        return check()
    if len(argv) == 3 and argv[1] in MODULES and VERSION.fullmatch(argv[2].removeprefix("v")):
        return vendor(argv[1], argv[2].removeprefix("v"))
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
