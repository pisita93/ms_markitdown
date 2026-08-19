#!/usr/bin/env python3
"""
Regenerate ``website/wheels.lock`` from the pinned versions below.

Each entry is resolved against PyPI and recorded with its download URL and
SHA-256, so ``build.py`` can fetch it without a package manager and still detect
tampering or corruption.

Every dependency here must publish a pure-Python wheel: Pyodide can only install
platform-independent wheels, apart from the packages it builds itself. Anything
native has to come from Pyodide's own distribution instead — list it under a
feature's ``pyodide`` key in ``build.py``.

Usage:
    python3 website/tools/relock.py
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

# Versions are pinned deliberately rather than floated:
#
#   pdfplumber   0.11.9  — 0.11.10 raises its Pillow floor to 12.2, above the
#                          11.3 that Pyodide 0.28.x ships, and bumps pypdfium2
#                          past the range the shim declares.
#   pdfminer.six 20251230 — the exact version pdfplumber 0.11.9 requires, and
#                          the minimum markitdown's `pdf` extra accepts.
PINS = [
    ("markdownify",  "1.2.3"),
    ("defusedxml",   "0.7.1"),
    ("openpyxl",     "3.1.5"),
    ("et_xmlfile",   "2.0.0"),
    ("olefile",      "0.47"),
    ("mammoth",      "1.12.1"),
    ("cobble",       "0.1.4"),
    ("python-pptx",  "1.0.2"),
    ("XlsxWriter",   "3.2.9"),
    ("pdfminer.six", "20251230"),
    ("pdfplumber",   "0.11.9"),
]

PURE_SUFFIXES = ("py3-none-any.whl", "py2.py3-none-any.whl")


def resolve(name: str, version: str) -> dict[str, str]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json") as response:
        data = json.load(response)

    candidates = [
        url for url in data["urls"]
        if url["packagetype"] == "bdist_wheel"
        and url["filename"].endswith(PURE_SUFFIXES)
    ]
    if len(candidates) != 1:
        raise SystemExit(
            f"{name} {version}: expected exactly one pure-Python wheel, found "
            f"{[c['filename'] for c in candidates]}"
        )

    wheel = candidates[0]
    return {
        "name": name,
        "version": version,
        "filename": wheel["filename"],
        "url": wheel["url"],
        "sha256": wheel["digests"]["sha256"],
    }


def main() -> None:
    wheels = []
    for name, version in PINS:
        print(f"  resolving {name} {version}")
        wheels.append(resolve(name, version))

    target = Path(__file__).resolve().parent.parent / "wheels.lock"
    target.write_text(json.dumps({
        "_comment": "Pinned pure-Python wheels vendored into the site. "
                    "Regenerate with website/tools/relock.py.",
        "wheels": wheels,
    }, indent=2) + "\n")
    print(f"\nwrote {target} ({len(wheels)} wheels)")


if __name__ == "__main__":
    main()
