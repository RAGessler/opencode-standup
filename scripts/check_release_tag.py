#!/usr/bin/env python3
"""Ensure a release tag matches the package version before publishing."""

from __future__ import annotations

import os
import re
import sys

from opencode_standup import __version__


def main() -> int:
    tag = os.environ.get("RELEASE_TAG") or os.environ.get("GITHUB_REF_NAME", "")
    match = re.fullmatch(r"v(.+)", tag)
    if match is None:
        print(f"Expected a v-prefixed release tag, got {tag!r}.", file=sys.stderr)
        return 1
    if match.group(1) != __version__:
        print(
            f"Release tag {tag} does not match package version {__version__}.",
            file=sys.stderr,
        )
        return 1
    print(f"Release tag {tag} matches package version {__version__}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
