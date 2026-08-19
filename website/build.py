#!/usr/bin/env python3
"""
Build the MarkItDown web playground into ``website/dist``.

The playground runs MarkItDown itself in the browser under Pyodide (CPython
compiled to WebAssembly), so there is no server component. This script gathers
everything the browser needs:

  1. Builds a wheel of ``markitdown`` straight from ``packages/markitdown``, so
     the deployed site always runs this repository's code.
  2. Builds the two compatibility shims in ``website/shims`` (see their
     docstrings for why they exist).
  3. Downloads the pinned pure-Python dependencies listed in
     ``website/wheels.lock``, verifying each against its recorded SHA-256.
  4. Writes ``vendor/manifest.json`` describing which wheels the browser should
     load for each file format.
  5. Copies the static site from ``website/src``.

Only the standard library is used, so this runs on a bare Cloudflare Pages build
image with nothing installed.

Usage:
    python3 website/build.py [--output DIR] [--offline]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
MARKITDOWN_SRC = REPO / "packages" / "markitdown" / "src" / "markitdown"

# Pyodide release whose bundled packages the manifest is written against.
# Bumping this requires re-checking that lxml/pandas/Pillow/cryptography still
# satisfy the pinned wheels in wheels.lock (notably pdfplumber's Pillow floor).
PYODIDE_VERSION = "0.28.3"
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"

# Packages loaded from Pyodide's own distribution rather than vendored.
PYODIDE_BASE = ["micropip", "beautifulsoup4", "charset-normalizer", "requests"]

# Dependency groups, resolved lazily in the browser the first time a format
# that needs them is converted. Keeping these off the initial load keeps the
# cold start to the core runtime plus a few hundred KB of wheels.
FEATURES = {
    "pdf": {
        "label": "PDF",
        "extensions": [".pdf"],
        "shows": ["pdfminer.six", "pdfplumber", "cryptography"],
        "pyodide": ["cryptography", "Pillow"],
        # Dependencies before dependents: pdfplumber pins pdfminer.six exactly
        # and requires pypdfium2, and micropip checks those constraints even
        # when it is told not to resolve dependencies itself.
        "wheels": ["pdfminer.six", "pypdfium2", "pdfplumber"],
    },
    "docx": {
        "label": "Word",
        "extensions": [".docx"],
        "shows": ["mammoth", "lxml"],
        "pyodide": ["lxml"],
        "wheels": ["mammoth", "cobble"],
    },
    "pptx": {
        "label": "PowerPoint",
        "extensions": [".pptx"],
        "shows": ["python-pptx", "lxml", "Pillow"],
        "pyodide": ["lxml", "Pillow"],
        "wheels": ["python-pptx", "XlsxWriter"],
    },
    "xlsx": {
        "label": "Excel (xlsx)",
        "extensions": [".xlsx"],
        "shows": ["pandas", "openpyxl"],
        "pyodide": ["pandas"],
        "wheels": ["openpyxl", "et_xmlfile"],
    },
    "xls": {
        "label": "Excel (legacy xls)",
        "extensions": [".xls"],
        "shows": ["pandas", "xlrd"],
        "pyodide": ["pandas", "xlrd"],
        "wheels": [],
    },
    "outlook": {
        "label": "Outlook message",
        "extensions": [".msg"],
        "shows": ["olefile"],
        "pyodide": [],
        "wheels": ["olefile"],
    },
}

# Formats MarkItDown handles with no dependency beyond the core install.
CORE_EXTENSIONS = [
    ".html", ".htm", ".csv", ".json", ".ipynb", ".xml", ".rss", ".atom",
    ".txt", ".md", ".markdown", ".epub", ".zip",
]

# Small fixtures from the test suite, shipped so the page has something to
# demonstrate when a visitor has no file to hand.
SAMPLES = [
    ("test.docx", "Word document", "A conference paper with headings, a table and an image."),
    ("test.xlsx", "Excel workbook", "Two sheets, rendered as Markdown tables."),
    ("test.pdf", "PDF", "The same paper as a PDF, extracted with pdfminer."),
    ("test_notebook.ipynb", "Jupyter notebook", "Markdown and code cells."),
]

# Formats that genuinely cannot work in the browser. MarkItDown shells out to
# exiftool for media metadata and needs ffmpeg plus a speech recognition
# backend for transcription; none of those exist in WebAssembly. Listing them
# lets the UI say so plainly instead of returning a confusing empty result.
UNAVAILABLE = {
    ".jpg": "exiftool", ".jpeg": "exiftool", ".png": "exiftool",
    ".gif": "exiftool", ".tiff": "exiftool", ".tif": "exiftool",
    ".webp": "exiftool", ".bmp": "exiftool",
    ".mp3": "ffmpeg", ".wav": "ffmpeg", ".m4a": "ffmpeg", ".flac": "ffmpeg",
}


# --------------------------------------------------------------------------
# Wheel construction
# --------------------------------------------------------------------------

def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{name},sha256={digest.decode()},{len(data)}"


def build_wheel(out_dir: Path, dist: str, version: str, members: dict[str, bytes],
                requires: list[str] | None = None, summary: str = "") -> Path:
    """Write a pure-Python wheel. Uses only the standard library."""
    normalized = dist.replace("-", "_")
    distinfo = f"{normalized}-{version}.dist-info"
    files = dict(members)

    metadata = ["Metadata-Version: 2.1", f"Name: {dist}", f"Version: {version}"]
    if summary:
        metadata.append(f"Summary: {summary}")
    for requirement in requires or []:
        metadata.append(f"Requires-Dist: {requirement}")
    files[f"{distinfo}/METADATA"] = ("\n".join(metadata) + "\n").encode()
    files[f"{distinfo}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: markitdown-website-build\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode()

    record = [_record_line(name, blob) for name, blob in sorted(files.items())]
    record.append(f"{distinfo}/RECORD,,")
    files[f"{distinfo}/RECORD"] = ("\n".join(record) + "\n").encode()

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{normalized}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            # Fixed timestamp so rebuilds are byte-identical.
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return path


def _collect_package(package_root: Path, package_name: str) -> dict[str, bytes]:
    """Read a package directory into wheel members, skipping caches."""
    members: dict[str, bytes] = {}
    for path in sorted(package_root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        arcname = f"{package_name}/{path.relative_to(package_root).as_posix()}"
        members[arcname] = path.read_bytes()
    return members


def build_markitdown_wheel(out_dir: Path) -> tuple[Path, str]:
    about = (MARKITDOWN_SRC / "__about__.py").read_text()
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', about)
    if not match:
        raise SystemExit("could not read __version__ from markitdown/__about__.py")
    version = match.group(1)

    members = _collect_package(MARKITDOWN_SRC, "markitdown")
    if "markitdown/__init__.py" not in members:
        raise SystemExit(f"markitdown sources not found under {MARKITDOWN_SRC}")

    path = build_wheel(
        out_dir, "markitdown", version, members,
        requires=["beautifulsoup4", "requests", "markdownify", "charset-normalizer", "defusedxml"],
        summary="Convert files to Markdown (built from this repository).",
    )
    return path, version


def build_shim_wheels(out_dir: Path) -> list[Path]:
    shims = [
        ("magika", "0.6.1", "Signature-based stand-in for magika, for WebAssembly."),
        ("pypdfium2", "4.30.0", "Import stub for pypdfium2, for WebAssembly."),
    ]
    built = []
    for name, version, summary in shims:
        members = _collect_package(ROOT / "shims" / name, name)
        if not members:
            raise SystemExit(f"shim sources missing for {name}")
        built.append(build_wheel(out_dir, name, version, members, summary=summary))
    return built


# --------------------------------------------------------------------------
# Third-party wheels
# --------------------------------------------------------------------------

def fetch_pinned_wheels(out_dir: Path, cache: Path, offline: bool) -> dict[str, str]:
    lock = json.loads((ROOT / "wheels.lock").read_text())
    out_dir.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, str] = {}

    for entry in lock["wheels"]:
        filename = entry["filename"]
        cached = cache / filename
        if not cached.exists():
            if offline:
                raise SystemExit(f"--offline set but {filename} is not cached in {cache}")
            print(f"  fetching {filename}")
            with urllib.request.urlopen(entry["url"]) as response:
                cached.write_bytes(response.read())

        actual = hashlib.sha256(cached.read_bytes()).hexdigest()
        if actual != entry["sha256"]:
            cached.unlink(missing_ok=True)
            raise SystemExit(
                f"checksum mismatch for {filename}\n"
                f"  expected {entry['sha256']}\n  actual   {actual}"
            )

        shutil.copy2(cached, out_dir / filename)
        resolved[entry["name"]] = filename

    return resolved


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def copy_samples(dist: Path) -> list[dict[str, str]]:
    source = REPO / "packages" / "markitdown" / "tests" / "test_files"
    target = dist / "samples"
    target.mkdir(parents=True, exist_ok=True)
    entries = []
    for filename, label, description in SAMPLES:
        path = source / filename
        if not path.exists():
            print(f"  skipping missing sample {filename}")
            continue
        shutil.copy2(path, target / filename)
        entries.append({
            "filename": filename,
            "label": label,
            "description": description,
            "bytes": path.stat().st_size,
        })
    return entries


def write_manifest(dist: Path, wheel_names: dict[str, str], markitdown_version: str,
                   samples: list[dict[str, str]]) -> None:
    base = ["markitdown", "magika", "markdownify", "defusedxml"]
    missing = [n for n in base if n not in wheel_names]
    if missing:
        raise SystemExit(f"missing base wheels: {missing}")

    features = {}
    for key, spec in FEATURES.items():
        wheels = []
        for name in spec["wheels"]:
            if name not in wheel_names:
                raise SystemExit(f"feature {key!r} needs unknown wheel {name!r}")
            wheels.append(wheel_names[name])
        features[key] = {
            "label": spec["label"],
            "extensions": spec["extensions"],
            "pyodide": spec["pyodide"],
            "wheels": wheels,
            # Names to show in the support matrix. Transitive packages and the
            # pypdfium2 stub are left out; they would only add noise.
            "shows": spec["shows"],
        }

    manifest = {
        "markitdownVersion": markitdown_version,
        "pyodideVersion": PYODIDE_VERSION,
        "pyodideIndexUrl": PYODIDE_CDN,
        "pyodideBase": PYODIDE_BASE,
        "baseWheels": [wheel_names[n] for n in base],
        "wheelBase": "vendor/wheels/",
        "coreExtensions": CORE_EXTENSIONS,
        "features": features,
        "unavailable": UNAVAILABLE,
        "samples": samples,
    }
    (dist / "vendor" / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "dist"), help="build output directory")
    parser.add_argument("--offline", action="store_true",
                        help="fail instead of downloading; use the wheel cache only")
    args = parser.parse_args()

    dist = Path(args.output).resolve()
    wheels = dist / "vendor" / "wheels"
    cache = ROOT / ".wheel-cache"

    print(f"building into {dist}")
    if dist.exists():
        shutil.rmtree(dist)

    print("copying static site")
    shutil.copytree(ROOT / "src", dist)

    print("building markitdown wheel from packages/markitdown")
    markitdown_wheel, version = build_markitdown_wheel(wheels)
    print(f"  {markitdown_wheel.name}")

    print("building compatibility shims")
    for path in build_shim_wheels(wheels):
        print(f"  {path.name}")

    print("resolving pinned dependencies")
    resolved = fetch_pinned_wheels(wheels, cache, args.offline)

    resolved["markitdown"] = markitdown_wheel.name
    resolved["magika"] = f"magika-0.6.1-py3-none-any.whl"
    resolved["pypdfium2"] = f"pypdfium2-4.30.0-py3-none-any.whl"

    print("copying samples")
    samples = copy_samples(dist)

    print("writing manifest")
    write_manifest(dist, resolved, version, samples)

    total = sum(p.stat().st_size for p in wheels.iterdir())
    print(f"\ndone: markitdown {version}, {len(list(wheels.iterdir()))} wheels, "
          f"{total / 1024 / 1024:.1f} MB vendored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
