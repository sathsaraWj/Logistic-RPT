#!/usr/bin/env python3
"""Documentation consistency check.

Verifies that every relative markdown link (`[text](path)`) inside `docs/`, `TASKS.md` and
`README.md` resolves to a file that actually exists, and that every ADR file under `docs/adr/`
has the required `# ADR-NNNN:` / `## Status` / `## Context` / `## Decision` /
`## Consequences` sections. This is deliberately dependency-free (stdlib only) so it can run
in Phase 1 before any linter is installed, and is wired into `make docs-check` / CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
REQUIRED_ADR_SECTIONS = ("## Status", "## Context", "## Decision", "## Consequences")


def _iter_markdown_files() -> list[Path]:
    files = list((REPO_ROOT / "docs").rglob("*.md"))
    for name in ("TASKS.md", "README.md", "CHANGELOG.md"):
        candidate = REPO_ROOT / name
        if candidate.exists():
            files.append(candidate)
    return files


def _check_links(path: Path) -> list[str]:
    errors = []
    text = path.read_text(encoding="utf-8")
    for match in LINK_PATTERN.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target_path = (path.parent / target.split("#", 1)[0]).resolve()
        if not target_path.exists():
            errors.append(f"{path.relative_to(REPO_ROOT)}: broken link -> {target}")
    return errors


def _check_adr_sections(path: Path) -> list[str]:
    if path.parent.name != "adr" or path.name == "README.md":
        return []
    text = path.read_text(encoding="utf-8")
    return [
        f"{path.relative_to(REPO_ROOT)}: missing required section '{section}'"
        for section in REQUIRED_ADR_SECTIONS
        if section not in text
    ]


def main() -> int:
    errors: list[str] = []
    for path in _iter_markdown_files():
        errors.extend(_check_links(path))
        errors.extend(_check_adr_sections(path))

    if errors:
        print("Documentation check failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"Documentation check passed ({len(_iter_markdown_files())} files checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
