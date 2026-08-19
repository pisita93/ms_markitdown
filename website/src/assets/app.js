/*
 * Page logic: file intake, a conversion queue driven by the Pyodide worker,
 * and the output pane.
 *
 * The worker does all the real work; this module never touches Python. Files
 * are converted one at a time so a batch cannot start several multi-megabyte
 * conversions inside one WebAssembly heap at once.
 */

const $ = (selector) => document.querySelector(selector);

const dropzone   = $("#dropzone");
const fileInput  = $("#fileInput");
const logList    = $("[data-log]");
const bootLine   = $("[data-boot-line]");
const bootDot    = $("[data-boot-dot]");
const runtimeTag = $("[data-runtime-label]");
const resultsEl  = $("#results");
const queueEl    = $("[data-queue]");
const codeEl     = $("[data-markdown]").querySelector("code");
const codePre    = $("[data-markdown]");
const previewEl  = $("[data-preview]");
const outputMeta = $("[data-output-meta]");
const copyBtn    = $("[data-copy]");
const downloadBtn= $("[data-download]");
const clearBtn   = $("[data-clear]");
const matrixEl   = $("[data-matrix]");
const samplesEl  = $("[data-samples]");
const samplesRow = $("[data-samples-row]");
const footerMeta = $("[data-footer-meta]");
const versionSlot= $("[data-version-slot]");

let worker = null;
let ready = false;
let capabilities = null;

const jobs = new Map();
const pending = [];
let running = false;
let selectedId = null;
let nextId = 1;

/* ------------------------------------------------------------------ */
/* Console log                                                         */
/* ------------------------------------------------------------------ */

let activeEntry = null;

function logLine(message, detail = "") {
  if (activeEntry) activeEntry.classList.add("is-done");
  const li = document.createElement("li");
  li.dataset.mark = "›";
  li.innerHTML = "";
  const text = document.createElement("span");
  text.textContent = message;
  if (detail) {
    const em = document.createElement("span");
    em.className = "detail";
    em.textContent = ` ${detail}`;
    text.appendChild(em);
  }
  li.appendChild(text);
  logList.appendChild(li);
  logList.scrollTop = logList.scrollHeight;
  activeEntry = li;
  return li;
}

function logError(message) {
  const li = logLine(message);
  li.classList.add("is-error");
  li.classList.remove("is-done");
  activeEntry = null;
}

function settleLog() {
  if (activeEntry) activeEntry.classList.add("is-done");
  activeEntry = null;
}

/* ------------------------------------------------------------------ */
/* Worker                                                              */
/* ------------------------------------------------------------------ */

function startWorker() {
  worker = new Worker("assets/worker.js");
  worker.onmessage = (event) => handleWorkerMessage(event.data);
  worker.onerror = (event) => {
    bootDot.classList.add("is-error");
    bootLine.textContent = "The runtime failed to start.";
    logError(event.message || "Worker error");
  };
  worker.postMessage({ type: "boot" });
}

function handleWorkerMessage(message) {
  switch (message.type) {
    case "status":
      logLine(message.message, message.detail);
      break;

    case "ready":
      ready = true;
      capabilities = message;
      settleLog();
      logLine("Ready", "converting happens on this machine");
      bootDot.classList.add("is-ready");
      bootLine.textContent = "Running locally in your browser — no upload";
      runtimeTag.textContent = `markitdown ${message.markitdownVersion}`;
      versionSlot.textContent = `markitdown ${message.markitdownVersion}`;
      footerMeta.textContent =
        `markitdown ${message.markitdownVersion} · pyodide ${message.pyodideVersion} · static build`;
      renderMatrix(message);
      drain();
      break;

    case "result":
      finishJob(message.id, { markdown: message.markdown, title: message.title });
      break;

    case "failed":
      finishJob(message.id, { error: message.error, traceback: message.traceback });
      break;

    case "fatal":
      settleLog();
      logError(message.error);
      if (message.id != null) finishJob(message.id, { error: message.error });
      else {
        bootDot.classList.add("is-error");
        bootLine.textContent = "The runtime failed to start.";
      }
      break;
  }
}

/* ------------------------------------------------------------------ */
/* Queue                                                               */
/* ------------------------------------------------------------------ */

function extensionOf(name) {
  const match = /\.[^./\\]+$/.exec(name || "");
  return match ? match[0].toLowerCase() : "";
}

function addFiles(files) {
  const list = Array.from(files || []);
  if (list.length === 0) return;

  for (const file of list) {
    const id = nextId++;
    const job = { id, file, name: file.name, size: file.size, state: "queued" };
    jobs.set(id, job);
    pending.push(id);
    renderQueueItem(job);
  }

  resultsEl.hidden = false;
  if (selectedId === null) select(pending[0]);
  drain();
}

async function drain() {
  if (!ready || running) return;
  const id = pending.shift();
  if (id === undefined) return;

  const job = jobs.get(id);
  if (!job) return drain();

  running = true;
  job.state = "working";
  renderQueueItem(job);
  if (selectedId === id) renderOutput();

  try {
    const buffer = await job.file.arrayBuffer();
    worker.postMessage(
      { type: "convert", id, filename: job.name, bytes: new Uint8Array(buffer) },
    );
  } catch (error) {
    finishJob(id, { error: `Could not read the file: ${error.message}` });
  }
}

function finishJob(id, outcome) {
  const job = jobs.get(id);
  running = false;

  if (job) {
    Object.assign(job, outcome);
    job.state = outcome.error ? "failed" : "done";
    renderQueueItem(job);
    if (selectedId === id || selectedId === null) {
      selectedId = id;
      renderOutput();
      syncQueueSelection();
    }
    settleLog();
    if (outcome.error) logError(`${job.name} — ${outcome.error}`);
    else logLine(`${job.name}`, `${formatBytes((outcome.markdown || "").length)} of Markdown`);
  }

  drain();
}

function renderQueueItem(job) {
  let node = queueEl.querySelector(`[data-job="${job.id}"]`);
  if (!node) {
    node = document.createElement("li");
    node.innerHTML = `<button type="button" class="queue-item" data-job="${job.id}">
        <span class="queue-name"></span><span class="queue-state"></span></button>`;
    node = node.firstElementChild;
    node.addEventListener("click", () => select(job.id));
    const li = document.createElement("li");
    li.appendChild(node);
    queueEl.appendChild(li);
  }

  node.querySelector(".queue-name").textContent = job.name;
  const unsupported = capabilities && capabilities.unavailable[extensionOf(job.name)];
  const states = {
    queued:  "queued",
    working: "converting…",
    done:    unsupported && !(job.markdown || "").trim()
               ? `needs ${unsupported}`
               : `${formatBytes(job.size)} → markdown`,
    failed:  "failed",
  };
  node.querySelector(".queue-state").textContent = states[job.state] || job.state;
  node.classList.toggle("is-working", job.state === "working");
  node.classList.toggle("is-failed", job.state === "failed");
  syncQueueSelection();
}

function syncQueueSelection() {
  queueEl.querySelectorAll(".queue-item").forEach((node) => {
    node.classList.toggle("is-active", Number(node.dataset.job) === selectedId);
  });
}

function select(id) {
  selectedId = id;
  syncQueueSelection();
  renderOutput();
}

/* ------------------------------------------------------------------ */
/* Output                                                              */
/* ------------------------------------------------------------------ */

let view = "markdown";

function currentJob() {
  return selectedId === null ? null : jobs.get(selectedId);
}

function renderOutput() {
  const job = currentJob();
  previewEl.hidden = true;
  codePre.hidden = true;
  clearFailure();

  if (!job) return;

  if (job.state === "queued" || job.state === "working") {
    showPlaceholder(job.state === "working" ? "Converting…" : "Queued");
    setActions(false);
    outputMeta.textContent = job.name;
    return;
  }

  if (job.state === "failed") {
    showFailure(job);
    setActions(false);
    outputMeta.textContent = job.name;
    return;
  }

  const markdown = job.markdown || "";
  outputMeta.textContent = `${job.name} · ${markdown.length.toLocaleString()} chars`;
  setActions(markdown.length > 0);

  if (markdown.trim() === "") {
    // Images and audio convert "successfully" to nothing when the external
    // tools MarkItDown shells out to are absent, which is always the case in a
    // browser. Say why rather than showing a blank pane.
    const hint = hintFor(job);
    if (hint) {
      showNotice(`Nothing to extract from ${job.name} in the browser.`, hint);
    } else {
      showPlaceholder("MarkItDown produced an empty document for this file.");
    }
    return;
  }

  if (view === "preview") {
    previewEl.hidden = false;
    previewEl.innerHTML = renderMarkdown(markdown);
  } else {
    codePre.hidden = false;
    codeEl.textContent = markdown;
  }
}

function showPlaceholder(text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = text;
  node.dataset.transient = "1";
  $(".output-body").appendChild(node);
}

function showNotice(headline, detail) {
  const node = document.createElement("div");
  node.className = "failure";
  node.dataset.transient = "1";
  node.textContent = headline;
  const note = document.createElement("span");
  note.className = "note";
  note.textContent = detail;
  node.appendChild(note);
  $(".output-body").appendChild(node);
}

function showFailure(job) {
  const node = document.createElement("div");
  node.className = "failure";
  node.dataset.transient = "1";
  node.textContent = job.error || "Conversion failed.";

  const hint = hintFor(job);
  if (hint) {
    const note = document.createElement("span");
    note.className = "note";
    note.textContent = hint;
    node.appendChild(note);
  }
  $(".output-body").appendChild(node);
}

function hintFor(job) {
  const extension = extensionOf(job.name);
  const needs = capabilities && capabilities.unavailable[extension];
  if (needs === "exiftool") {
    return "Image files carry only metadata worth extracting, and MarkItDown reads it with " +
           "exiftool — an external program with no WebAssembly build. Captions additionally " +
           "need a vision model. Install markitdown locally with exiftool on your PATH.";
  }
  if (needs === "ffmpeg") {
    return "Audio needs ffmpeg to decode and a speech recognition backend to transcribe. " +
           "Neither runs in a browser. Install markitdown[all] locally instead.";
  }
  return "";
}

function clearFailure() {
  $(".output-body").querySelectorAll("[data-transient]").forEach((n) => n.remove());
}

function setActions(enabled) {
  copyBtn.disabled = !enabled;
  downloadBtn.disabled = !enabled;
}

/* ------------------------------------------------------------------ */
/* Minimal Markdown preview                                            */
/*                                                                     */
/* Deliberately small: the product here is the Markdown itself, and    */
/* the preview only has to make it legible. HTML is escaped before any */
/* markup is introduced, so converted documents cannot inject nodes.   */
/* ------------------------------------------------------------------ */

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function inline(text) {
  return text
    .replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g, (_, alt, src) =>
      /^(https?:|data:image\/)/i.test(src) ? `<img src="${src}" alt="${alt}">` : alt)
    .replace(/\[([^\]]+)\]\(([^)\s]+)[^)]*\)/g, (whole, label, href) =>
      /^(https?:|mailto:|#)/i.test(href)
        ? `<a href="${href}" rel="noopener noreferrer" target="_blank">${label}</a>`
        : label)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}

function renderMarkdown(source) {
  const lines = escapeHtml(source).split("\n");
  const out = [];
  let listType = null;
  let inCode = false;
  let paragraph = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${inline(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  };
  const closeList = () => {
    if (listType) { out.push(`</${listType}>`); listType = null; }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (/^\s*```/.test(line)) {
      flushParagraph(); closeList();
      out.push(inCode ? "</code></pre>" : "<pre><code>");
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(line, "\n"); continue; }

    if (line.trim() === "") { flushParagraph(); closeList(); continue; }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph(); closeList();
      const level = Math.min(heading[1].length, 6);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushParagraph(); closeList();
      out.push("<hr>");
      continue;
    }

    // A table: a header row followed by a separator row of dashes.
    if (line.includes("|") && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1] || "")) {
      flushParagraph(); closeList();
      const cells = (row) =>
        row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
      const head = cells(line);
      out.push("<table><thead><tr>");
      head.forEach((c) => out.push(`<th>${inline(c)}</th>`));
      out.push("</tr></thead><tbody>");
      i += 1;
      while (i + 1 < lines.length && lines[i + 1].includes("|")) {
        i += 1;
        out.push("<tr>");
        cells(lines[i]).forEach((c) => out.push(`<td>${inline(c)}</td>`));
        out.push("</tr>");
      }
      out.push("</tbody></table>");
      continue;
    }

    const quote = /^\s*&gt;\s?(.*)$/.exec(line);
    if (quote) {
      flushParagraph(); closeList();
      out.push(`<blockquote><p>${inline(quote[1])}</p></blockquote>`);
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const wanted = bullet ? "ul" : "ol";
      if (listType !== wanted) { closeList(); out.push(`<${wanted}>`); listType = wanted; }
      out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      continue;
    }

    closeList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  closeList();
  if (inCode) out.push("</code></pre>");
  return out.join("");
}

/* ------------------------------------------------------------------ */
/* Support matrix                                                      */
/* ------------------------------------------------------------------ */

function listWords(items) {
  if (!items || items.length === 0) return "its dependencies";
  if (items.length === 1) return items[0];
  return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
}

function renderMatrix({ coreExtensions, features, unavailable }) {
  matrixEl.innerHTML = "";

  const card = (kind, heading, extensions, note) => {
    const el = document.createElement("div");
    el.className = `card is-${kind}`;
    const h = document.createElement("h3");
    h.textContent = heading;
    const exts = document.createElement("div");
    exts.className = "exts";
    extensions.forEach((ext) => {
      const span = document.createElement("span");
      span.className = "ext";
      span.textContent = ext;
      exts.appendChild(span);
    });
    const p = document.createElement("p");
    p.textContent = note;
    el.append(h, exts, p);
    matrixEl.appendChild(el);
  };

  card("core", "Ready immediately", coreExtensions,
       "Handled by the base install — nothing further to download.");

  for (const spec of Object.values(features)) {
    card("lazy", spec.label, spec.extensions,
         `Fetches ${listWords(spec.shows)} the first time you convert one.`);
  }

  const byTool = {};
  for (const [ext, tool] of Object.entries(unavailable)) {
    (byTool[tool] ||= []).push(ext);
  }
  if (byTool.exiftool) {
    card("off", "Images — not here", byTool.exiftool,
         "MarkItDown reads image metadata with exiftool and describes pictures with a vision " +
         "model. Neither exists in a browser, so use the CLI for these.");
  }
  if (byTool.ffmpeg) {
    card("off", "Audio — not here", byTool.ffmpeg,
         "Transcription needs ffmpeg and a speech recognition backend, which cannot run in " +
         "WebAssembly. Use the CLI for these.");
  }
}

/* ------------------------------------------------------------------ */
/* Samples                                                             */
/* ------------------------------------------------------------------ */

async function loadSampleList() {
  try {
    const manifest = await (await fetch("vendor/manifest.json", { cache: "no-cache" })).json();
    if (!manifest.samples || manifest.samples.length === 0) return;

    samplesEl.hidden = false;
    for (const sample of manifest.samples) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chip";
      button.textContent = sample.filename;
      button.title = `${sample.label} — ${sample.description}`;
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const response = await fetch(`samples/${sample.filename}`);
          const blob = await response.blob();
          addFiles([new File([blob], sample.filename)]);
        } catch (error) {
          logError(`Could not load sample ${sample.filename}`);
        } finally {
          button.disabled = false;
        }
      });
      samplesRow.appendChild(button);
    }
  } catch {
    /* Samples are a convenience; their absence is not worth reporting. */
  }
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });

let dragDepth = 0;
["dragenter", "dragover"].forEach((type) =>
  document.addEventListener(type, (event) => {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes("Files")) return;
    event.preventDefault();
    if (type === "dragenter") dragDepth += 1;
    dropzone.classList.add("is-dragging");
  }));

document.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dropzone.classList.remove("is-dragging");
});

document.addEventListener("drop", (event) => {
  if (!event.dataTransfer) return;
  event.preventDefault();
  dragDepth = 0;
  dropzone.classList.remove("is-dragging");
  addFiles(event.dataTransfer.files);
});

document.addEventListener("paste", (event) => {
  const files = event.clipboardData && event.clipboardData.files;
  if (files && files.length) { event.preventDefault(); addFiles(files); }
});

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    view = tab.dataset.view;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    renderOutput();
  }));

copyBtn.addEventListener("click", async () => {
  const job = currentJob();
  if (!job || !job.markdown) return;
  try {
    await navigator.clipboard.writeText(job.markdown);
    const original = copyBtn.textContent;
    copyBtn.textContent = "Copied";
    setTimeout(() => { copyBtn.textContent = original; }, 1400);
  } catch {
    logError("The browser refused clipboard access.");
  }
});

downloadBtn.addEventListener("click", () => {
  const job = currentJob();
  if (!job || !job.markdown) return;
  const blob = new Blob([job.markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = job.name.replace(/\.[^./\\]+$/, "") + ".md";
  link.click();
  URL.revokeObjectURL(url);
});

clearBtn.addEventListener("click", () => {
  jobs.clear();
  pending.length = 0;
  selectedId = null;
  queueEl.innerHTML = "";
  resultsEl.hidden = true;
  clearFailure();
  codeEl.textContent = "";
  previewEl.innerHTML = "";
});

// The paste hint should name the key the visitor actually presses.
const pasteKey = /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent) ? "\u2318V" : "Ctrl+V";
const hintEl = document.querySelector(".sheet-hint");
if (hintEl) hintEl.innerHTML = hintEl.innerHTML.replace("\u2318V", pasteKey);

setActions(false);
loadSampleList();
startWorker();
