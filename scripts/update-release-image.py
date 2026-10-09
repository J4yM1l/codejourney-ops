#!/usr/bin/env python3
"""Replace one Zot image digest and the source-revision annotation in one
release-state Deployment file (homelab-infra ADR-021).

Generic counterpart of update-stage-manifests.py, used by applications other than
CodeJourney. The file must already exist and contain exactly one image line for the
repository and exactly one `homelab/source-revision` annotation; nothing else in the
file is touched.
"""

from __future__ import annotations

import argparse
import pathlib
import re

REGISTRY = r"192\.168\.0\.45:30080"
REVISION_PATTERN = re.compile(
    r'^(\s+homelab/source-revision: ")[0-9a-f]{7,40}("$)',
    re.MULTILINE,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=pathlib.Path)
    parser.add_argument("--repository", required=True, help="Zot repository name")
    parser.add_argument("--image", required=True, help="Zot reference with tag and digest")
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.repository):
        raise SystemExit(f"invalid repository name: {args.repository}")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_revision):
        raise SystemExit("source revision must be a full lowercase Git SHA")
    repo = re.escape(args.repository)
    if not re.fullmatch(rf"{REGISTRY}/{repo}:stage-[0-9a-f]{{12}}@sha256:[0-9a-f]{{64}}", args.image):
        raise SystemExit(f"invalid immutable image reference: {args.image}")

    image_pattern = re.compile(
        rf"^([ ]+image: ){REGISTRY}/{repo}(?::[A-Za-z0-9_][A-Za-z0-9._-]{{0,127}})?@sha256:[0-9a-f]{{64}}$",
        re.MULTILINE,
    )
    text = args.file.read_text()
    if len(image_pattern.findall(text)) != 1:
        raise SystemExit(f"expected one {args.repository} image in {args.file}")
    if len(REVISION_PATTERN.findall(text)) != 1:
        raise SystemExit(f"expected one homelab/source-revision annotation in {args.file}")

    text = image_pattern.sub(lambda m: f"{m.group(1)}{args.image}", text)
    text = REVISION_PATTERN.sub(rf"\g<1>{args.source_revision}\g<2>", text)
    args.file.write_text(text)


if __name__ == "__main__":
    main()
