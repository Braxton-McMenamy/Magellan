// "Try it": three small projects, each with one line to change ("change this here ->"). The
// visitor edits the code and Magellan Lite checks the change for real as they type
// (js/live.js). The examples come from data/showcase.js; demo/build_site.py checks that each
// is ok unchanged and gives the verdict it promises once the line is changed.

(() => {
  const box = document.getElementById("tryit");
  const showcase = window.MAGELLAN_SHOWCASE;
  if (!box || !showcase || !showcase.examples || !window.MagellanLive) return;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const examples = showcase.examples;

  box.innerHTML = `
    <div class="tryit-examples" role="tablist" aria-label="Examples">
      ${examples.map((e, i) => `<button role="tab" class="example-tab" data-example="${i}">
        <span class="example-n">${i + 1}</span> ${esc(e.title)}</button>`).join("")}
    </div>
    <div class="tryit-task">
      <div>
        <p class="task-label">Change this here <span aria-hidden="true">&rarr;</span></p>
        <p class="task-hint"></p>
        <p class="task-diff"><del></del> <span aria-hidden="true">&rarr;</span> <ins></ins></p>
      </div>
      <div class="task-actions">
        <button class="button primary" data-act="apply">Do it for me</button>
        <button class="button" data-act="reset">Reset</button>
        <button class="button" data-act="share" title="A link to exactly these files">Share</button>
      </div>
    </div>
    <div class="tryit-files" aria-label="Files"></div>
    <div class="editor-wrap">
      <div class="band" aria-hidden="true"></div>
      <pre class="gutter" aria-hidden="true"></pre>
      <textarea spellcheck="false" autocomplete="off" autocapitalize="off" wrap="off"></textarea>
    </div>
    <p class="tryit-blurb muted small"></p>
    <p class="tryit-note small" role="status" hidden></p>
    <p class="tryit-status muted small" role="status"></p>
    <div class="tryit-result" aria-live="polite"></div>`;

  const $ = (s) => box.querySelector(s);
  const area = $("textarea"), gutter = $(".gutter"), band = $(".band");
  const result = $(".tryit-result"), statusEl = $(".tryit-status");
  const status = (t) => { statusEl.textContent = t; };
  // a shared link's news, on its own line so the engine's status doesn't write over it
  const noteEl = $(".tryit-note");
  const note = (t, bad = false) => {
    noteEl.textContent = t;
    noteEl.hidden = !t;
    noteEl.classList.toggle("bad", bad);
  };

  let ex = null;          // the example
  let files = {};         // its files as the visitor has them now
  let open = null;        // the file in the editor
  let timer = 0, runId = 0;
  let checked = false;    // a check has been drawn (a shared link checks as the page opens)

  const changed = () => Object.keys(files).some((p) => files[p] !== ex.files[p]);

  function choose(i) {
    ex = examples[i];
    files = { ...ex.files };
    box.querySelectorAll("[data-example]").forEach((b, j) => {
      b.classList.toggle("active", j === i);
      b.setAttribute("aria-selected", String(j === i));
    });
    $(".task-hint").textContent = ex.hint;
    $(".task-diff del").textContent = ex.find.trim();
    $(".task-diff ins").textContent = ex.replace.trim();
    $(".tryit-blurb").textContent = ex.blurb;
    openFile(ex.file);
    update();
  }

  function openFile(path) {
    open = path;
    area.value = files[path];
    area.setAttribute("aria-label", `${path}, editable`);
    tabs();
    paint();
  }

  function tabs() {
    $(".tryit-files").innerHTML = Object.keys(files).sort().map((p) => `
      <button class="file-tab${p === open ? " active" : ""}" data-file="${esc(p)}">${esc(p)}${
        files[p] !== ex.files[p] ? '<i class="edited" title="changed"></i>' : ""}${
        p === ex.file ? '<span class="here" title="the line to change is here">&larr;</span>' : ""}</button>`).join("");
  }

  // the line to change: where `find` still is, or where `replace` now is
  function target() {
    if (open !== ex.file) return null;
    const lines = area.value.split("\n");
    let i = lines.indexOf(ex.find);
    let done = false;
    if (i < 0) { i = lines.findIndex((l) => l.trim() === ex.replace.trim()); done = i >= 0; }
    if (i < 0) i = ex.files[ex.file].split("\n").indexOf(ex.find);
    return { line: i + 1, done };
  }

  function paint() {
    const n = area.value.split("\n").length;
    const t = target();
    gutter.innerHTML = Array.from({ length: n }, (_, k) => (t && k + 1 === t.line
      ? `<b>${t.done ? "&check;" : "&rarr;"}</b>` : String(k + 1))).join("\n");
    // as tall as the code (a few lines of room to type), unless the visitor has resized it
    if (!area.dataset.resized) area.style.height = `${Math.max(140, (n + 3) * 20 + 24)}px`;
    gutter.scrollTop = area.scrollTop;
    band.hidden = !t;
    if (t) {
      const lh = parseFloat(getComputedStyle(area).lineHeight) || 20;
      const pad = parseFloat(getComputedStyle(area).paddingTop) || 12;
      band.style.top = `${pad + (t.line - 1) * lh - area.scrollTop}px`;
      band.style.height = `${lh}px`;
      band.classList.toggle("done", t.done);
    }
  }

  area.addEventListener("scroll", paint);
  area.addEventListener("mouseup", () => {        // dragged the corner: keep their height
    if (area.style.height && area.offsetHeight !== parseInt(area.style.height, 10)) area.dataset.resized = "1";
  });
  area.addEventListener("input", () => {     // never reset the text here: the caret would jump
    note("");
    files[open] = area.value;
    tabs();
    paint();
    update();
  });

  // check as they type, a moment after they stop
  function update() {
    clearTimeout(timer);
    if (!changed()) {
      runId += 1;
      result.innerHTML = `<p class="tryit-waiting">Nothing has changed yet. Edit the line at the
        arrow (or press <b>Do it for me</b>) and Magellan Lite checks the change as you type.</p>`;
      return;
    }
    timer = setTimeout(run, 350);
  }

  async function run() {
    const id = ++runId;
    result.classList.add("busy");
    try {
      const report = await MagellanLive.check(ex.files, files, status);
      if (id !== runId) return;            // a newer edit is on its way
      draw(report);
      checked = true;
      status(`Checked by ${report.engine}${report.ms !== undefined ? ` in ${report.ms} ms` : ""}.`);
    } catch (err) {
      if (id === runId) status(`Could not check: ${err.message}`);
    } finally {
      if (id === runId) result.classList.remove("busy");
    }
  }

  function draw(r) {
    const findings = r.findings || [], changes = r.changes || [], affected = r.affected || [];
    result.innerHTML = `
      <div class="result-head">
        <span class="verdict-big ${esc(r.verdict)}">${esc(r.verdict)}</span>
        <span>${plural(findings.length, "finding")} · ${plural(changes.length, "change")}${
          affected.length ? ` · reaches ${plural(affected.length, "definition")} nobody edited` : ""}</span>
      </div>
      <div class="result-grid">
        <div class="result-list">
          <h4>Checklist</h4>
          ${findings.length ? `<ul class="findings">${findings.map((f, i) => `<li>
              <button class="finding-link" data-finding="${i}">
                <span class="sev ${esc(f.severity)}">${esc(f.severity)}</span>
                <code>${esc(f.rule)}</code> <span class="where">${esc(f.path)}:${f.line}</span></button>
              <p>${esc(f.message)}</p>
              ${f.fix ? `<p class="fix small"><b>fix:</b> ${esc(f.fix)}</p>` : ""}</li>`).join("")}</ul>`
            : `<p class="muted">Nothing to check in what this change touched.</p>`}
          <h4>Changes</h4>
          ${changes.length ? `<ul class="changes">${changes.map((c) => `<li><span class="kind ${esc(c.kind)}">${esc(c.kind)}</span>
              <code>${esc(c.name)}</code><span class="muted small">${esc(c.detail)}</span></li>`).join("")}</ul>`
            : `<p class="muted">No definition changed: formatting, comments and docstrings don't count.</p>`}
          ${(r.errors || []).length ? `<h4>Not checked</h4><ul class="muted small">${r.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
        </div>
        <div class="result-map"><h4>The map</h4><div class="map-box"></div></div>
      </div>`;
    MagellanMap.render(result.querySelector(".map-box"), r.map || MagellanMap.fromReport(r)).play();
    result.querySelectorAll("[data-finding]").forEach((b) => b.addEventListener("click", () => {
      const f = findings[Number(b.dataset.finding)];
      if (files[f.path] !== undefined) goTo(f.path, f.line);
    }));
  }

  // jump to a finding: open its file and select the line
  function goTo(path, line) {
    openFile(path);
    const lines = area.value.split("\n");
    const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
    area.focus();
    area.setSelectionRange(start, start + (lines[line - 1] || "").length);
  }

  box.addEventListener("click", (e) => {
    const tab = e.target.closest("[data-example]");
    const file = e.target.closest("[data-file]");
    const act = e.target.closest("[data-act]");
    if (tab) { choose(Number(tab.dataset.example)); unlink(); note(""); }
    else if (file) openFile(file.dataset.file);
    else if (act && act.dataset.act === "apply") {
      files = { ...ex.files, [ex.file]: ex.files[ex.file].replace(ex.find, ex.replace) };
      openFile(ex.file);
      update();
    } else if (act && act.dataset.act === "reset") {
      files = { ...ex.files };
      openFile(ex.file);
      update();
      unlink();
      note("");
    } else if (act && act.dataset.act === "share") share(act);
  });

  // -- Share: the files in the address ------------------------------------------------------
  // index.html#try=z.<data>: the example and the files the visitor changed, as JSON, squeezed
  // with the browser's own deflate (CompressionStream) and written in base64url ("u." instead
  // of "z." when the browser can't compress). Opening the link puts them back, so a judge can
  // send exactly what they tried. A link is something anyone could have written: it is read
  // with limits (size, file count, .py names), and its text only ever goes into the editor's
  // value and text nodes, never into HTML.
  const SHARE = { link: 24_000, bytes: 200_000, files: 24, path: 160 };
  const PY_PATH = /^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*\/)*[A-Za-z0-9_][A-Za-z0-9_.-]*\.py$/;
  class LinkError extends Error {}         // what is wrong with a link, in words for the visitor

  function b64url(bytes) {
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  const unb64url = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));

  const canSqueeze = () => {
    try { return typeof CompressionStream === "function" && Boolean(new CompressionStream("deflate-raw")); } catch { return false; }
  };
  const squeeze = async (bytes) => new Uint8Array(await new Response(
    new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate-raw"))).arrayBuffer());

  // inflate, but stop as soon as it is bigger than a link may hold (a tiny link can inflate
  // to gigabytes)
  async function unsqueeze(bytes) {
    const reader = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw")).getReader();
    const parts = [];
    let n = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      n += value.length;
      if (n > SHARE.bytes) { reader.cancel().catch(() => {}); throw new LinkError("it is too big"); }
      parts.push(value);
    }
    const out = new Uint8Array(n);
    parts.reduce((at, p) => { out.set(p, at); return at + p.length; }, 0);
    return out;
  }

  async function encode() {
    const edits = {};
    for (const p of Object.keys(files).sort()) if (files[p] !== ex.files[p]) edits[p] = files[p];
    const bytes = new TextEncoder().encode(JSON.stringify({ v: 1, e: ex.id, f: edits }));
    if (canSqueeze()) {
      try { return `#try=z.${b64url(await squeeze(bytes))}`; } catch { /* the plain way below */ }
    }
    return `#try=u.${b64url(bytes)}`;
  }

  // the address -> { i, files } or null when there is no link; throws when there is one but
  // it is damaged, too big, or names something that isn't a Python file
  async function decode(hash) {
    if (!hash.startsWith("#try=")) return null;
    if (hash.length > SHARE.link) throw new LinkError("it is too long");
    const m = hash.match(/^#try=([zu])\.([A-Za-z0-9_-]+)$/);
    if (!m) throw new LinkError("it is damaged");
    let bytes = unb64url(m[2]);
    if (m[1] === "z") {
      if (typeof DecompressionStream !== "function") throw new LinkError("this browser can't unpack it");
      bytes = await unsqueeze(bytes);
    } else if (bytes.length > SHARE.bytes) throw new LinkError("it is too big");
    const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    const plain = (o) => o !== null && typeof o === "object" && !Array.isArray(o);
    if (!plain(data) || data.v !== 1 || !plain(data.f)) throw new LinkError("it isn't a Try-it link");
    const i = examples.findIndex((x) => x.id === data.e);
    if (i < 0) throw new LinkError("its example isn't on this page");
    const out = { ...examples[i].files };
    for (const [path, text] of Object.entries(data.f)) {
      // a path like pkg/mod.py: every part starts with a letter, digit or _, so no .. or /x
      if (path.length > SHARE.path || !PY_PATH.test(path)) throw new LinkError("it names a file that isn't a Python file");
      if (typeof text !== "string") throw new LinkError("it is damaged");
      out[path] = text;
    }
    if (Object.keys(out).length > SHARE.files) throw new LinkError("it has too many files");
    return { i, files: out };
  }

  // the visitor left the shared files (another example, or Reset): the address forgets them
  function unlink() {
    if (location.hash.startsWith("#try=")) history.replaceState(history.state, "", location.pathname + location.search);
  }

  let shareTimer = 0;
  async function share(button) {
    const hash = await encode();
    if (hash.length > SHARE.link) {
      note("That's too much code to fit in a link: share fewer changes.", true);
      return;
    }
    history.replaceState(history.state, "", hash);
    const copied = window.MagellanCopy ? await window.MagellanCopy(location.href) : false;
    note(copied ? "Link copied: it opens Try it with exactly these files."
      : "The address now holds these files: copy it from the address bar.");
    button.textContent = copied ? "Link copied" : "Link in the address";
    clearTimeout(shareTimer);
    shareTimer = setTimeout(() => { button.textContent = "Share"; }, 2000);
  }

  // open a shared link: on page load, or when one is pasted into this tab's address
  async function openShared(smooth) {
    let got;
    try {
      got = await decode(location.hash);
      if (!got) return;
      choose(got.i);
      files = got.files;
      const edited = Object.keys(files).sort().filter((p) => files[p] !== ex.files[p]);
      openFile(edited.includes(ex.file) || !edited.length ? ex.file : edited[0]);
      update();
      note(edited.length ? `Opened from a shared link: ${plural(edited.length, "changed file")}.`
        : "Opened from a shared link: the example as it is.");
    } catch (err) {
      const why = err instanceof LinkError ? err.message : "it is damaged";
      note(`That Try-it link couldn't be opened (${why}): here is the example as it is.`, true);
    }
    // to just below the sticky nav (two rows tall on a phone); offsetTop, not the bounding box:
    // the section may still be sliding in (.reveal)
    let y = 0;
    for (let n = box; n; n = n.offsetParent) y += n.offsetTop;
    const nav = document.querySelector(".nav");
    const top = Math.max(0, y - (nav ? nav.offsetHeight : 0) - 12);
    const glide = smooth && !matchMedia("(prefers-reduced-motion: reduce)").matches;
    try { scrollTo({ top, behavior: glide ? "smooth" : "instant" }); } catch { scrollTo(0, top); }
  }
  window.addEventListener("hashchange", () => openShared(true));

  choose(0);
  openShared(false);

  // get the engine ready as the section comes near, so the first check is instant
  const io = new IntersectionObserver(async ([en]) => {
    if (!en.isIntersecting) return;
    io.disconnect();
    try {
      await MagellanLive.warm(status);
      const ready = (await MagellanLive.hasServer())
        ? "Ready: the local server checks your change."
        : "Ready: Magellan Lite runs right here, in your browser. Nothing is uploaded.";
      if (!checked) status(ready);           // never over "Checked by ..."
    } catch (err) {
      status(`Could not load the engine: ${err.message}. Check your connection; editing retries.`);
    }
  }, { rootMargin: "300px" });
  io.observe(box);
})();
