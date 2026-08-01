#!/usr/bin/env python3
"""Validate local Markdown links, fences, and the expected guide set."""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
REQUIRED_GUIDES = {
    "README.md",
    "evidence-model.md",
    "installation.md",
    "configuration.md",
    "operations.md",
    "interpreting-results.md",
    "architecture.md",
    "data-and-api.md",
    "security.md",
    "troubleshooting.md",
    "development.md",
}


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

    actual_guides = {path.name for path in (ROOT / "docs").glob("*.md")}
    if actual_guides != REQUIRED_GUIDES:
        missing = sorted(REQUIRED_GUIDES - actual_guides)
        extra = sorted(actual_guides - REQUIRED_GUIDES)
        if missing:
            errors.append("docs/: missing guides " + ", ".join(missing))
        if extra:
            errors.append("docs/: unexpected guides " + ", ".join(extra))

    if errors:
        print("Documentation validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Documentation validation passed for {len(files)} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
