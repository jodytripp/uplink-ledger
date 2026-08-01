#!/usr/bin/env python3
"""Validate local Markdown links, fences, and the ordered guide set."""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REQUIRED_GUIDES = [
    "README.md",
    "01-evidence-model.md",
    "02-installation.md",
    "03-configuration.md",
    "04-operations.md",
    "05-reading-results.md",
    "06-architecture.md",
    "07-data-and-api.md",
    "08-security.md",
    "09-troubleshooting.md",
    "10-development.md",
]


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if ".git" not in path.parts
    )


def link_path(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        value = value.split(maxsplit=1)[0]
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or value.startswith(("#", "//")):
        return None
    return urllib.parse.unquote(parsed.path) or None


def main() -> int:
    errors: list[str] = []
    files = markdown_files()
    for path in files:
        text = path.read_text(encoding="utf-8")
        fences = sum(line.startswith("```") for line in text.splitlines())
        if fences % 2:
            errors.append(f"{path.relative_to(ROOT)}: unbalanced code fences")
        for match in LINK_RE.finditer(text):
            relative = link_path(match.group(1))
            if relative is None:
                continue
            target = (path.parent / relative).resolve()
            if not target.exists():
                errors.append(
                    f"{path.relative_to(ROOT)}: missing link target {relative}"
                )

    actual_guides = sorted(path.name for path in (ROOT / "docs").glob("*.md"))
    if actual_guides != sorted(REQUIRED_GUIDES):
        errors.append(
            "docs/: expected ordered guide set "
            + ", ".join(REQUIRED_GUIDES)
        )

    if errors:
        print("Documentation validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Documentation validation passed for {len(files)} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
