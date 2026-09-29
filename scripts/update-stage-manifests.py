#!/usr/bin/env python3
"""Update only CodeJourney stage image digests and source-revision annotations."""

from __future__ import annotations

import argparse
import pathlib
import re


IMAGE_PATTERN = re.compile(
    r"^([ ]+image: )192\.168\.0\.(?:45|47):30080/"
    # An optional tag (e.g. :stage-4d91a2bbecec) may precede the digest; the digest
    # is what containerd pulls.
    r"(codejourney-stage-(?:web|api))(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?@sha256:[0-9a-f]{64}$",
    re.MULTILINE,
)
REVISION_PATTERN = re.compile(
    r'^(\s+codejourney\.homelab/source-revision: ")[0-9a-f]{7,40}("$)',
    re.MULTILINE,
)


def update(path: pathlib.Path, repository: str, image: str, revision: str) -> None:
    text = path.read_text()
    matches = [match for match in IMAGE_PATTERN.finditer(text) if match.group(2) == repository]
    if len(matches) != 1:
        raise SystemExit(f"expected one {repository} image in {path}, found {len(matches)}")
    if len(REVISION_PATTERN.findall(text)) != 1:
        raise SystemExit(f"expected one source-revision annotation in {path}")

    text = IMAGE_PATTERN.sub(
        lambda match: f"{match.group(1)}{image}" if match.group(2) == repository else match.group(0),
        text,
    )
    text = REVISION_PATTERN.sub(rf"\g<1>{revision}\g<2>", text)
    path.write_text(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gitops-dir", required=True, type=pathlib.Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--api-image", required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", args.source_revision):
        raise SystemExit("source revision must be a full lowercase Git SHA")
    for image, repository in (
        (args.web_image, "codejourney-stage-web"),
        (args.api_image, "codejourney-stage-api"),
    ):
        expected = rf"192\.168\.0\.(?:45|47):30080/{repository}(?::stage-[0-9a-f]{{12}})?@sha256:[0-9a-f]{{64}}"
        if not re.fullmatch(expected, image):
            raise SystemExit(f"invalid immutable image reference: {image}")

    base = args.gitops_dir / "clusters/stage/applications/codejourney"
    update(base / "web.yaml", "codejourney-stage-web", args.web_image, args.source_revision)
    update(base / "api.yaml", "codejourney-stage-api", args.api_image, args.source_revision)


if __name__ == "__main__":
    main()
