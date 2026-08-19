# MarkItDown on the web

A static site that runs MarkItDown **in the browser**. There is no backend: the
page loads [Pyodide][pyodide] (CPython compiled to WebAssembly), installs a wheel
built from `packages/markitdown` in this repository, and converts files entirely
on the visitor's machine.

It is built to deploy to Cloudflare Pages, but the output is plain static files,
so any static host will serve it.

## Deploying to Cloudflare Pages

Nothing needs to be installed and no secrets are required — the build script uses
only the Python standard library, which the Pages build image already has.

### First time: connecting GitHub to Cloudflare

You only do this once per GitHub account. Cloudflare needs permission to read the
repository so it can build on every push.

1. **Create a Cloudflare account** at <https://dash.cloudflare.com/sign-up> if you
   do not have one, and confirm the verification email. The free plan is enough —
   this site is static assets, so there is no compute to pay for.

2. In the dashboard sidebar, open **Compute (Workers & Pages)**, then
   **Create → Pages → Connect to Git**.

3. Click **Connect GitHub**. Two authorisations happen back to back, and both are
   required:

   - GitHub asks you to **authorise Cloudflare Pages** (the OAuth consent screen).
   - GitHub then asks you to **install the Cloudflare Pages app** on an account.
     Choose the account that owns this repository. Under *Repository access*,
     **Only select repositories** and picking just this one is enough — Cloudflare
     does not need access to anything else.

   Click **Install & Authorize**. GitHub returns you to Cloudflare.

4. Back in Cloudflare, select the repository from the list and click
   **Begin setup**.

5. Fill in the build settings:

   | Setting | Value |
   | --- | --- |
   | Project name | anything — it becomes `<name>.pages.dev` |
   | Production branch | **the branch that actually contains `website/`** — see below |
   | Framework preset | None |
   | Build command | `python3 website/build.py` |
   | Build output directory | `website/dist` |
   | Root directory | *(leave blank — repository root)* |

   The production branch is the one thing worth pausing on. Cloudflare defaults it
   to the repository's default branch, and the build fails with
   `python3: can't open file 'website/build.py'` if that branch does not have this
   directory yet. Either point it at the branch that does, or merge to the default
   branch first and leave it alone.

6. Click **Save and Deploy**.

The first build takes a couple of minutes, most of it downloading the pinned
wheels; later builds reuse Cloudflare's cache and are quicker. When it finishes
the site is live at `https://<project>.pages.dev`.

From then on every push to the production branch redeploys the site, and pushes
to any other branch get their own preview URL.

### Python on the build image

The build script runs on `python3` and needs Python 3.8 or newer. Cloudflare's
current build image provides that by default. If a build ever fails with a syntax
error from `build.py`, an old image is being used — set an environment variable
in **Settings → Environment variables**:

| Variable | Value |
| --- | --- |
| `PYTHON_VERSION` | `3.12` |

### Without connecting GitHub

If you would rather not grant repository access, deploy the built directory
straight from your machine. Cloudflare only asks you to log in through the
browser once:

```bash
python3 website/build.py
npx wrangler pages deploy website/dist --project-name markitdown-web
```

The trade-off is that nothing redeploys on its own — you run this again after
every change.

## Running it locally

```bash
python3 website/build.py
python3 -m http.server 8788 --directory website/dist
```

Then open <http://127.0.0.1:8788>. Opening `index.html` from the filesystem does
**not** work — the page uses a Web Worker and `fetch`, both of which need a real
origin.

## What the build does

`website/build.py` writes `website/dist`:

1. **Builds a `markitdown` wheel** from `packages/markitdown/src/markitdown`, so
   the deployed site always runs this repository's code rather than a release
   from PyPI. The version comes from `__about__.py`.
2. **Builds two compatibility shims** from `website/shims` (see below).
3. **Downloads the pinned dependencies** listed in `website/wheels.lock`,
   verifying each against its recorded SHA-256 and caching them in
   `website/.wheel-cache/` so repeat builds do not re-download.
4. **Writes `vendor/manifest.json`**, which tells the browser which wheels to
   install up front and which to defer until a given format is converted.
5. **Copies the static site** from `website/src` and a few small sample
   documents from the test suite.

Both `website/dist/` and `website/.wheel-cache/` are gitignored.

## The two shims

Two of MarkItDown's dependencies cannot exist in WebAssembly. Rather than fork
the converters, the site substitutes import-compatible stand-ins:

**`magika`** — MarkItDown identifies a stream's type with Magika, which infers it
with a neural network via ONNX Runtime. ONNX Runtime has no WebAssembly build.
`website/shims/magika` keeps the same API but identifies types from magic-byte
signatures, and reports "unknown" whenever a signature does not match
confidently. MarkItDown already handles that case: it falls back to the filename
extension and MIME type, which the browser supplies for every uploaded file.

**`pypdfium2`** — `pdfplumber` imports it at module load to build its page-image
renderer, and it wraps the native PDFium library. MarkItDown only ever calls
`extract_text` and `extract_words`, so the rendering path is unreachable here.
`website/shims/pypdfium2` satisfies the import and raises a clear error if
anything ever does try to render a page.

Neither shim changes how a document is converted. The site was checked against
the repository's own conversion fixtures — all 15 vectors in
`packages/markitdown/tests/_test_vectors.py` produce the expected output under
WebAssembly.

## What works, and what does not

Everything MarkItDown converts with pure-Python dependencies works: PDF, Word,
PowerPoint, Excel (modern and legacy), Outlook `.msg`, EPUB, HTML, CSV, JSON,
XML/RSS, Jupyter notebooks, zip archives, and plain text.

**Images and audio do not, and cannot.** MarkItDown reads media metadata by
shelling out to `exiftool`, transcribes audio with `ffmpeg` plus a speech
recognition backend, and describes images with a vision model. None of those are
programs a browser can run. The page detects these formats and says so rather
than handing back an empty document. Use the CLI for them:

```bash
pip install 'markitdown[all]'
markitdown photo.jpg
```

Azure Document Intelligence and LLM captioning are likewise unavailable here,
since both would require sending the document to a third party and shipping API
credentials to the browser.

## Cost and limits

The deployment is static assets, so Cloudflare Pages serves it on the free plan
with unlimited requests and no compute billing — all the work happens in the
visitor's browser.

Practical limits are the visitor's machine, not the host. The cold start is the
Pyodide runtime plus roughly 700 KB of wheels; heavier converters add a few MB
the first time a format is used, after which the browser caches them. Very large
documents are bounded by WebAssembly memory, so files in the hundreds of
megabytes are better handled by the CLI.

## Updating dependencies

`website/wheels.lock` pins every third-party wheel by URL and SHA-256.
Regenerate it with:

```bash
python3 website/tools/relock.py
```

Check the notes in `website/build.py` before bumping `PYODIDE_VERSION`: the
pinned wheels must stay compatible with the versions of `lxml`, `pandas`,
`Pillow` and `cryptography` that the chosen Pyodide release bundles. `pdfplumber`
in particular has a `Pillow` floor that newer releases raise past what Pyodide
ships.

[pyodide]: https://pyodide.org
