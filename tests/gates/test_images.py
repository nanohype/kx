"""check-images.py: a scan the gate cannot read is a scan that did not happen.

trivy's report is the only evidence the gate has about an image, and a clean
image's report holds no findings. So a report read as no findings for want of
reading it would pass an image nobody scanned. These tests serve scan() what
trivy might print, through a stand-in for the process, and hold it to naming an
image it could not read as unscanned rather than clean.
"""

import json
import subprocess

import pytest

GATE = "scripts/check-images.py"

REPORT = json.dumps({"Results": [{"Vulnerabilities": [
    {"VulnerabilityID": "CVE-1", "PkgName": "libx", "Severity": "CRITICAL", "FixedVersion": "2"},
    {"VulnerabilityID": "CVE-2", "Severity": "HIGH"},
]}, {"Target": "a layer with nothing to report"}]})


@pytest.fixture
def images(load_script):
    return load_script(GATE)


@pytest.fixture
def trivy(images, monkeypatch):
    """Stand in for the trivy process: what it prints, how it exits, or how it fails."""

    def serve(*, stdout="", stderr="", returncode=0, raises=None):
        def run(cmd, **_):
            assert cmd[:2] == ["trivy", "image"]
            if raises:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        monkeypatch.setattr(images.subprocess, "run", run)

    return serve


class TestScan:
    def test_a_report_is_read_into_its_findings(self, images, trivy):
        trivy(stdout=REPORT)
        assert images.scan("r/i:1") == ([
            {"id": "CVE-1", "pkg": "libx", "severity": "CRITICAL", "fixed": "2"},
            {"id": "CVE-2", "pkg": "?", "severity": "HIGH", "fixed": ""},
        ], None)

    def test_a_clean_report(self, images, trivy):
        trivy(stdout=json.dumps({"Results": None}))
        assert images.scan("r/i:1") == ([], None)

    @pytest.mark.parametrize("stdout,error", [
        ("", "trivy's report is not JSON (Expecting value, at character 0)"),
        ('{"Results": [', "trivy's report is not JSON (Expecting value, at character 13)"),
        ("[]", "trivy's report is a JSON array, not the object trivy writes"),
        ("null", "trivy's report is JSON null, not the object trivy writes"),
        ('"text"', "trivy's report is a JSON string, not the object trivy writes"),
    ], ids=["empty", "truncated", "array", "null", "string"])
    def test_a_report_it_cannot_read_is_an_error_not_a_clean_scan(self, images, trivy, stdout,
                                                                  error):
        trivy(stdout=stdout)
        assert images.scan("r/i:1") == ([], error)

    def test_a_scan_that_outran_its_bound(self, images, trivy):
        trivy(raises=subprocess.TimeoutExpired(["trivy"], 5))
        assert images.scan("r/i:1", timeout=5) == ([], "trivy did not finish within 5s")

    def test_no_scanner(self, images, trivy):
        trivy(raises=FileNotFoundError("trivy"))
        assert images.scan("r/i:1") == ([], "trivy is not installed")

    @pytest.mark.parametrize("stdout,stderr,error", [
        ("", "fatal: the first line\nfatal: the last line\n", "fatal: the last line"),
        ("only on stdout\n", "", "only on stdout"),
        ("", "", "trivy failed with no output"),
    ], ids=["stderr", "stdout", "silent"])
    def test_a_scan_that_failed_says_why(self, images, trivy, stdout, stderr, error):
        trivy(stdout=stdout, stderr=stderr, returncode=1)
        assert images.scan("r/i:1") == ([], error)
