"""Reading the chart pins in stack/*/*/install.sh."""

from __future__ import annotations

import dataclasses
import pathlib
import re

from .text import strip_comments

REPO_ADD = re.compile(r"^[ \t]*helm repo add\s+(\S+)\s+(\S+)", re.M)
# Both verbs, because a chart a slice renders and applies is pinned exactly as
# one it installs as a release: envoy-gateway-crds runs `helm template` into a
# server-side apply, since its chart is too large to store as a release. Either
# runs under the `deadline N` that bounds its chart fetch, which is read through.
HELM_INSTALL = re.compile(
    r"^[ \t]*(?:deadline[ \t]+\d+[ \t]+)?helm (?:upgrade --install|template)\s+(\S+)\s+(\S+)", re.M
)
VERSION_FLAG = re.compile(r"--version[ \t]+(\S+)")


def helm_block(text: str) -> tuple[tuple[int, int], str] | None:
    """(span, command) for the helm invocation that pins the chart, or None.

    Scoped to the invocation rather than the file, and read with comment bodies
    blanked. VERSION_FLAG is unanchored, so read over the whole file it takes the
    first `--version` anywhere, a superseded pin left in a comment above the
    live one included. The span is the command's own, continuation lines and
    all, in the blanked view, which keeps every offset of the file it came from.
    """
    blanked = strip_comments(text)
    m = HELM_INSTALL.search(blanked)
    if not m:
        return None
    start = m.start()
    end = len(blanked)
    for line_start in range(start, len(blanked)):
        if blanked[line_start] == "\n" and not blanked[:line_start].rstrip("\n").endswith("\\"):
            end = line_start
            break
    return (start, end), blanked[start:end]


@dataclasses.dataclass(frozen=True)
class Pin:
    """One install.sh's helm invocation, as it reads.

    `ref` is the chart as the command names it: `alias/chart`, an `oci://`
    reference, or a local path. `repo` is the URL the ref resolves to, either the
    `oci://` reference itself or what `helm repo add` maps the alias to, and None
    when neither applies. `version` is None for a chart the command does not pin.
    """

    script: pathlib.Path
    ref: str
    chart: str
    version: str | None
    repo: str | None


def install_pins(root: pathlib.Path) -> list[Pin]:
    """Every helm invocation in stack/*/*/install.sh under `root`, in path order."""
    pins = []
    for script in sorted(root.glob("stack/*/*/install.sh")):
        text = script.read_text()
        block = helm_block(text)
        if not block:
            continue  # the slice applies manifests rather than installing a chart
        _, cmd = block
        ref = HELM_INSTALL.search(cmd).group(2)
        version = VERSION_FLAG.search(cmd)
        if ref.startswith("oci://"):
            repo = ref
        else:
            repo = dict(REPO_ADD.findall(strip_comments(text))).get(ref.partition("/")[0])
        pins.append(Pin(script=script, ref=ref, chart=ref.rstrip("/").split("/")[-1],
                        version=version.group(1) if version else None, repo=repo))
    return pins
