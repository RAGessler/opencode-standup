#!/usr/bin/env python3
"""Validate that a built distribution contains only publishable project files."""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path


FORBIDDEN_PATH_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"(?:ghp|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"][^'\"]{12,}"),
]


def read_members(artifact: Path) -> list[tuple[str, bytes]]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return [(name, archive.read(name)) for name in archive.namelist()]
    with tarfile.open(artifact, "r:gz") as archive:
        members: list[tuple[str, bytes]] = []
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                members.append((member.name, extracted.read()))
        return members


def main() -> int:
    artifacts = [Path(arg) for arg in sys.argv[1:]]
    if not artifacts:
        print("usage: audit_package.py DIST [DIST ...]", file=sys.stderr)
        return 2

    failed = False
    for artifact in artifacts:
        for name, content in read_members(artifact):
            parts = set(Path(name).parts)
            if parts & FORBIDDEN_PATH_PARTS:
                print(f"{artifact}: forbidden path: {name}", file=sys.stderr)
                failed = True
            text = content.decode("utf-8", errors="ignore")
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    print(f"{artifact}: possible secret in {name}", file=sys.stderr)
                    failed = True
    if failed:
        return 1
    print(f"Audited {len(artifacts)} artifact(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
