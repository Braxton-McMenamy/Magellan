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
      </div>
    </div>
    <div class="tryit-files" aria-label="Files"></div>
    <div class="editor-wrap">
      <div class="band" aria-hidden="true"></div>
      <pre class="gutter" aria-hidden="true"></pre>
      <textarea spellcheck="false" autocomplete="off" autocapitalize="off" wrap="off"></textarea>
    </div>
    <p class="tryit-blurb muted small"></p>
    <p class="tryit-status muted small" role="status"></p>
    <div class="tryit-result" aria-live="polite"></div>`;

  const $ = (s) => box.querySelector(s);
  const area = $("textarea"), gutter = $(".gutter"), band = $(".band");
  const result = $(".tryit-result"), statusEl = $(".tryit-status");
  const status = (t) => { statusEl.textContent = t; };

  let ex = null;          // the example
  let files = {};         // its files as the visitor has them now
  let open = null;        // the file in the editor
  let timer = 0, runId = 0;

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
    if (tab) choose(Number(tab.dataset.example));
    else if (file) openFile(file.dataset.file);
    else if (act && act.dataset.act === "apply") {
      files = { ...ex.files, [ex.file]: ex.files[ex.file].replace(ex.find, ex.replace) };
      openFile(ex.file);
      update();
    } else if (act && act.dataset.act === "reset") {
      files = { ...ex.files };
      openFile(ex.file);
      update();
    }
  });

  // TODO(site): a "Share" button that puts the current files in the address (the URL hash:
  //   JSON.stringify(files), then encodeURIComponent) and loads them when the page opens with
  //   one, so a judge can send a link to exactly what they tried. Done when reloading the
  //   shared link shows the same files and the same verdict.
  choose(0);

  // get the engine ready as the section comes near, so the first check is instant
  const io = new IntersectionObserver(async ([en]) => {
    if (!en.isIntersecting) return;
    io.disconnect();
    try {
      await MagellanLive.warm(status);
      status((await MagellanLive.hasServer())
        ? "Ready: the local server checks your change."
        : "Ready: Magellan Lite runs right here, in your browser. Nothing is uploaded.");
    } catch (err) {
      status(`Could not load the engine: ${err.message}. Check your connection; editing retries.`);
    }
  }, { rootMargin: "300px" });
  io.observe(box);
})();
