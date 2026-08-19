/*
 * Runs MarkItDown inside a Web Worker, on top of Pyodide (CPython → WebAssembly).
 *
 * Everything happens on the visitor's machine: files are never uploaded. The
 * worker keeps the main thread free so the page stays responsive while Pyodide
 * boots (a few seconds) and while large documents are converted.
 *
 * Dependencies load in two stages. The base stage is what `import markitdown`
 * needs plus the converters that require nothing extra (HTML, CSV, JSON,
 * notebooks, EPUB, XML, zip). Heavier converters pull their dependencies in on
 * demand the first time you convert that format, keyed by the manifest the
 * build script emits.
 */

// Relative URLs inside a worker resolve against the worker script, which lives
// in /assets/. Resolve site assets against the site root instead.
const SITE_ROOT = new URL("../", self.location.href);

let manifest = null;
let pyodide = null;
let micropip = null;

// Feature keys whose dependencies are already installed.
const loadedFeatures = new Set();
// Pyodide-distribution packages already loaded.
const loadedPyodidePackages = new Set();

function post(type, payload = {}) {
  self.postMessage({ type, ...payload });
}

function status(message, detail = "") {
  post("status", { message, detail });
}

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */

async function loadManifest() {
  const response = await fetch(new URL("vendor/manifest.json", SITE_ROOT), { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(
      `Could not load vendor/manifest.json (HTTP ${response.status}). ` +
      "Run `python3 website/build.py` before serving the site."
    );
  }
  return response.json();
}

async function loadPyodidePackages(names) {
  const pending = names.filter((name) => !loadedPyodidePackages.has(name));
  if (pending.length === 0) return;
  await pyodide.loadPackage(pending, { messageCallback: () => {} });
  pending.forEach((name) => loadedPyodidePackages.add(name));
}

async function installWheels(filenames) {
  if (filenames.length === 0) return;
  const urls = filenames.map((name) => new URL(manifest.wheelBase + name, SITE_ROOT).href);
  // deps:false because the manifest already pins the full dependency closure;
  // letting micropip resolve would send it to PyPI for packages Pyodide
  // already provides, and it would reject `magika` (its real build needs
  // onnxruntime, which has no WebAssembly build).
  await micropip.install(urls, { deps: false });
}

async function boot() {
  manifest = await loadManifest();

  status("Downloading Python runtime", `Pyodide ${manifest.pyodideVersion}`);
  // Absolute in a normal build (a CDN), but resolve it anyway so a self-hosted
  // relative path works too.
  const pyodideBase = new URL(manifest.pyodideIndexUrl, SITE_ROOT).href;
  importScripts(pyodideBase + "pyodide.js");
  pyodide = await self.loadPyodide({ indexURL: pyodideBase });

  status("Loading core packages");
  await loadPyodidePackages(manifest.pyodideBase);
  micropip = pyodide.pyimport("micropip");

  status("Installing MarkItDown", `version ${manifest.markitdownVersion}`);
  await installWheels(manifest.baseWheels);

  // Import once up front so the first conversion is not paying for it, and so
  // an install problem surfaces during boot rather than mid-conversion.
  importMarkItDown();

  post("ready", {
    markitdownVersion: manifest.markitdownVersion,
    pyodideVersion: manifest.pyodideVersion,
    coreExtensions: manifest.coreExtensions,
    features: manifest.features,
    unavailable: manifest.unavailable,
  });
}

/* ------------------------------------------------------------------ */
/* Conversion                                                          */
/* ------------------------------------------------------------------ */

/*
 * Each converter module records at import time whether its dependencies were
 * importable, in a module-level `_dependency_exc_info`. A converter whose
 * dependencies arrive later therefore stays disabled until the package is
 * imported again, so drop MarkItDown from sys.modules and re-import it after
 * installing a new feature. Re-importing is cheap: the bytecode is cached.
 */
function importMarkItDown() {
  pyodide.runPython(`
import sys
for _name in [n for n in sys.modules if n == "markitdown" or n.startswith("markitdown.")]:
    del sys.modules[_name]
from markitdown import MarkItDown
_markitdown = MarkItDown(enable_plugins=False)
`);
}

function extensionOf(filename) {
  const match = /\.[^./\\]+$/.exec(filename || "");
  return match ? match[0].toLowerCase() : "";
}

function featureFor(extension) {
  for (const [key, spec] of Object.entries(manifest.features)) {
    if (spec.extensions.includes(extension)) return key;
  }
  return null;
}

async function ensureFeature(extension) {
  const key = featureFor(extension);
  if (!key || loadedFeatures.has(key)) return false;

  const spec = manifest.features[key];
  status(`Loading ${spec.label} support`, "first time only");
  await loadPyodidePackages(spec.pyodide);
  await installWheels(spec.wheels);
  loadedFeatures.add(key);
  return true;
}

async function convert({ id, filename, bytes, sourceUrl }) {
  const extension = extensionOf(filename);

  const installed = await ensureFeature(extension);
  if (installed) importMarkItDown();

  status("Converting", filename);

  // Hand the bytes to Python without a copy on the JS side.
  pyodide.globals.set("_payload", pyodide.toPy(bytes));
  pyodide.globals.set("_extension", extension || null);
  pyodide.globals.set("_filename", filename || null);
  pyodide.globals.set("_source_url", sourceUrl || null);

  const result = pyodide.runPython(`
import io, json, traceback
from markitdown import StreamInfo

try:
    _stream = io.BytesIO(bytes(_payload))
    _info = StreamInfo(
        extension=_extension,
        filename=_filename,
        url=_source_url,
    )
    _result = _markitdown.convert_stream(_stream, stream_info=_info)
    _payload_out = {
        "ok": True,
        "markdown": _result.markdown,
        "title": _result.title,
        "converter": type(_result).__name__,
    }
except Exception as exc:
    _payload_out = {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc(),
    }
finally:
    try:
        _stream.close()
    except Exception:
        pass

json.dumps(_payload_out)
`);

  // Release the reference so a large file is not pinned in the Python heap.
  pyodide.runPython("_payload = None")

  const parsed = JSON.parse(result);
  if (parsed.ok) {
    post("result", { id, filename, markdown: parsed.markdown, title: parsed.title });
  } else {
    post("failed", { id, filename, error: parsed.error, traceback: parsed.traceback });
  }
}

/* ------------------------------------------------------------------ */

self.onmessage = async (event) => {
  const message = event.data;
  try {
    if (message.type === "boot") {
      await boot();
    } else if (message.type === "convert") {
      await convert(message);
    }
  } catch (error) {
    post("fatal", {
      id: message.id,
      error: (error && error.message) || String(error),
    });
  }
};
