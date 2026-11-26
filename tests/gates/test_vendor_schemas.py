"""vendor-schemas.py: nothing is vendored from a release that is not the one recorded.

The script fetches a Go module zip from the module proxy, checks it against the
hash the checksum database records, and writes the schema files it takes into
schemas/. These tests serve both from memory, so what they hold is the
script's own logic: the hash it computes is Go's, a mismatch writes nothing,
only the files its patterns name are taken, and --check finds any byte that
moved.
"""

import base64
import hashlib
import io
import json
import urllib.error
import zipfile

import pytest

SCRIPT = "scripts/vendor-schemas.py"
K8S = "k8s.io/kubernetes"
VERSION = "1.36.1"
SWAGGER = b'{"swagger": "2.0", "definitions": {}}'


@pytest.fixture
def vendor(load_script, tmp_path, monkeypatch):
    module = load_script(SCRIPT)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "VENDORED", tmp_path / "schemas")
    monkeypatch.setattr(module, "SOURCES", tmp_path / "schemas" / "sources.json")
    return module


def module_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buffer.getvalue()


def go_h1(files: dict[str, bytes]) -> str:
    """Hash1 as Go's dirhash package defines it, written out from its documentation."""
    lines = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n"
                    for name, body in sorted(files.items()))
    return "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()


def release(module=K8S, version=VERSION, files=None):
    """(the zip's files, its bytes, the checksum database's lookup body for it)."""
    prefix = f"{module}@v{version}/"
    files = files if files is not None else {
        prefix + "api/openapi-spec/swagger.json": SWAGGER,
        prefix + "README.md": b"not a schema",
    }
    body = module_zip(files)
    lookup = (f"12345\n{module} v{version} {go_h1(files)}\n"
              f"{module} v{version}/go.mod h1:notthemodulehash=\n\ngo.sum database tree\n")
    return files, body, lookup


def serve(module, monkeypatch, routes: dict[str, bytes]):
    def fetch(url):
        if url not in routes:
            raise AssertionError(f"unexpected fetch of {url}")
        return routes[url]
    monkeypatch.setattr(module, "fetch", fetch)


def routes_for(vendor, module=K8S, version=VERSION, body=None, lookup=None):
    _, zip_body, zip_lookup = release(module, version)
    esc = vendor.escape(module)
    return {f"{vendor.SUMDB}/lookup/{esc}@v{version}": (lookup or zip_lookup).encode(),
            f"{vendor.PROXY}/{esc}/@v/v{version}.zip": body or zip_body}


class TestPieces:
    def test_escape_spells_capitals_the_way_the_proxy_does(self, vendor):
        assert vendor.escape("github.com/Masterminds/semVer") == "github.com/!masterminds/sem!ver"
        assert vendor.escape(K8S) == K8S

    def test_h1_is_gos_dirhash(self, vendor):
        files, body, _ = release()
        assert vendor.h1(zipfile.ZipFile(io.BytesIO(body))) == go_h1(files)

    def test_h1_ignores_directory_entries_and_the_order_files_were_written(self, vendor):
        files, _, _ = release()
        reordered = dict(reversed(list(files.items())))
        with_dir = {f"{K8S}@v{VERSION}/api/": b"", **reordered}
        assert vendor.h1(zipfile.ZipFile(io.BytesIO(module_zip(with_dir)))) == go_h1(files)

    def test_h1_refuses_a_name_holding_a_newline(self, vendor):
        archive = zipfile.ZipFile(io.BytesIO(module_zip({"a\nb": b"x"})))
        with pytest.raises(SystemExit, match="holds a newline"):
            vendor.h1(archive)

    def test_the_recorded_hash_is_the_modules_not_its_go_mod(self, vendor, monkeypatch):
        files, _, lookup = release()
        serve(vendor, monkeypatch, {f"{vendor.SUMDB}/lookup/{K8S}@v{VERSION}": lookup.encode()})
        assert vendor.recorded_hash(K8S, VERSION) == go_h1(files)

    def test_a_release_the_database_does_not_record(self, vendor, monkeypatch):
        only_go_mod = f"1\n{K8S} v{VERSION}/go.mod h1:x=\n"
        serve(vendor, monkeypatch, {f"{vendor.SUMDB}/lookup/{K8S}@v{VERSION}": only_go_mod.encode()})
        with pytest.raises(SystemExit, match="records no module hash"):
            vendor.recorded_hash(K8S, VERSION)

    def test_fetch_returns_the_body(self, vendor, monkeypatch):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        monkeypatch.setattr(vendor.urllib.request, "urlopen",
                            lambda url, timeout: Response(b"body"))
        assert vendor.fetch("https://example.invalid/x") == b"body"

    @pytest.mark.parametrize("error", [urllib.error.URLError("no route"), TimeoutError("slow")],
                             ids=["unreachable", "timed-out"])
    def test_fetch_refuses_what_it_could_not_read(self, vendor, monkeypatch, error):
        def urlopen(url, timeout):
            raise error
        monkeypatch.setattr(vendor.urllib.request, "urlopen", urlopen)
        with pytest.raises(SystemExit, match="could not fetch https://example.invalid/x"):
            vendor.fetch("https://example.invalid/x")


class TestReleaseFiles:
    def test_only_the_files_the_pattern_names_are_taken(self, vendor, monkeypatch):
        serve(vendor, monkeypatch, routes_for(vendor))
        module_hash, files = vendor.release_files("kubernetes", VERSION)
        assert module_hash == go_h1(release()[0])
        assert files == {"swagger.json": ("api/openapi-spec/swagger.json", SWAGGER)}

    def test_a_zip_that_is_not_the_recorded_release_yields_nothing(self, vendor, monkeypatch):
        tampered = module_zip({f"{K8S}@v{VERSION}/api/openapi-spec/swagger.json": b"{}"})
        serve(vendor, monkeypatch, routes_for(vendor, body=tampered))
        with pytest.raises(SystemExit, match="refusing to vendor anything from it"):
            vendor.release_files("kubernetes", VERSION)

    def test_two_files_that_would_land_on_one_name(self, vendor, monkeypatch):
        prefix = "github.com/cilium/cilium@v1.19.6/pkg/k8s/apis/cilium.io/client/crds"
        files = {f"{prefix}/v2/ciliumnodes.yaml": b"a", f"{prefix}/v2alpha1/ciliumnodes.yaml": b"b"}
        _, body, lookup = release("github.com/cilium/cilium", "1.19.6", files)
        serve(vendor, monkeypatch, routes_for(vendor, "github.com/cilium/cilium", "1.19.6",
                                              body=body, lookup=lookup))
        with pytest.raises(SystemExit, match="holds two files named ciliumnodes.yaml"):
            vendor.release_files("cilium", "1.19.6")

    def test_a_release_holding_no_file_the_pattern_names(self, vendor, monkeypatch):
        files = {f"{K8S}@v{VERSION}/README.md": b"x", "elsewhere/api/openapi-spec/swagger.json": b"x"}
        _, body, lookup = release(files=files)
        serve(vendor, monkeypatch, routes_for(vendor, body=body, lookup=lookup))
        with pytest.raises(SystemExit, match="holds no file matching"):
            vendor.release_files("kubernetes", VERSION)


class TestVendorAndCheck:
    def test_vendor_writes_the_files_and_their_record(self, vendor, monkeypatch, capsys):
        serve(vendor, monkeypatch, routes_for(vendor))
        assert vendor.vendor("kubernetes", VERSION) == 0
        written = vendor.VENDORED / "kubernetes" / f"v{VERSION}" / "swagger.json"
        assert written.read_bytes() == SWAGGER
        record = json.loads(vendor.SOURCES.read_text())
        assert record["_README"] == vendor.README
        assert record["kubernetes"] == {
            "module": K8S, "version": f"v{VERSION}", "h1": go_h1(release()[0]),
            "files": {"swagger.json": {"from": "api/openapi-spec/swagger.json",
                                       "sha256": hashlib.sha256(SWAGGER).hexdigest()}},
        }
        assert "1 file(s) from k8s.io/kubernetes@v1.36.1" in capsys.readouterr().out

    def test_vendoring_a_new_release_removes_the_one_it_replaces(self, vendor, monkeypatch):
        serve(vendor, monkeypatch, {**routes_for(vendor), **routes_for(vendor, version="1.37.0")})
        vendor.vendor("kubernetes", VERSION)
        vendor.vendor("kubernetes", "1.37.0")
        assert not (vendor.VENDORED / "kubernetes" / f"v{VERSION}").exists()
        assert (vendor.VENDORED / "kubernetes" / "v1.37.0" / "swagger.json").is_file()

    def test_vendoring_the_same_release_again_replaces_its_directory(self, vendor, monkeypatch):
        serve(vendor, monkeypatch, routes_for(vendor))
        vendor.vendor("kubernetes", VERSION)
        stray = vendor.VENDORED / "kubernetes" / f"v{VERSION}" / "stray.json"
        stray.write_text("{}")
        vendor.vendor("kubernetes", VERSION)
        assert not stray.exists()

    def test_check_passes_what_vendor_wrote(self, vendor, monkeypatch, capsys):
        serve(vendor, monkeypatch, routes_for(vendor))
        vendor.vendor("kubernetes", VERSION)
        assert vendor.check() == 0
        assert "every vendored file is its release's, for kubernetes v1.36.1" in \
            capsys.readouterr().out

    def test_check_finds_a_vendored_file_that_moved(self, vendor, monkeypatch, capsys):
        serve(vendor, monkeypatch, routes_for(vendor))
        vendor.vendor("kubernetes", VERSION)
        (vendor.VENDORED / "kubernetes" / f"v{VERSION}" / "swagger.json").write_bytes(b"{}")
        assert vendor.check() == 1
        assert "schemas/kubernetes/v1.36.1/swagger.json is not the release's file" in \
            capsys.readouterr().err

    def test_check_finds_a_record_that_no_longer_matches(self, vendor, monkeypatch, capsys):
        serve(vendor, monkeypatch, routes_for(vendor))
        vendor.vendor("kubernetes", VERSION)
        record = json.loads(vendor.SOURCES.read_text())
        record["kubernetes"]["h1"] = "h1:someotherhash="
        record["kubernetes"]["files"]["swagger.json"]["sha256"] = "0" * 64
        record["kubernetes"]["files"]["extra.json"] = {"from": "x", "sha256": "0" * 64}
        record["dashboards"] = {"version": "v1.0.0"}
        vendor.SOURCES.write_text(json.dumps(record))
        assert vendor.check() == 1
        err = capsys.readouterr().err
        assert "kubernetes: sum.golang.org records h1:" in err
        assert "kubernetes: the release holds ['swagger.json'], sources.json lists" in err
        assert "kubernetes/swagger.json: sources.json records another sha256" in err
        assert "dashboards: not a source this script knows how to vendor" in err

    def test_check_refuses_a_record_of_nothing(self, vendor):
        with pytest.raises(SystemExit, match="records no source"):
            vendor.check()


class TestCommandLine:
    def test_a_source_and_version_vendor_it(self, vendor, monkeypatch):
        calls = []
        monkeypatch.setattr(vendor, "vendor", lambda source, version: calls.append(
            (source, version)) or 0)
        assert vendor.main(["vendor-schemas.py", "cilium", "v1.19.6"]) == 0
        assert calls == [("cilium", "1.19.6")]

    def test_check_runs_the_check(self, vendor, monkeypatch):
        monkeypatch.setattr(vendor, "check", lambda: 7)
        assert vendor.main(["vendor-schemas.py", "--check"]) == 7

    @pytest.mark.parametrize("args", [[], ["cilium"], ["helm", "1.0.0"], ["cilium", "latest"],
                                      ["--check", "now"]],
                             ids=["nothing", "no-version", "unknown-source", "not-a-version",
                                  "check-with-an-argument"])
    def test_anything_else_prints_the_usage(self, vendor, capsys, args):
        assert vendor.main(["vendor-schemas.py", *args]) == 2
        assert capsys.readouterr().err.startswith("usage: vendor-schemas.py")

    def test_run_as_a_script_it_exits_with_mains_status(self, run_script):
        run = run_script(SCRIPT)
        assert run.status == 2
        assert run.err.startswith("usage: vendor-schemas.py")
