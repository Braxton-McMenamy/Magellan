// "Try it": edit the version before and after a change, and Magellan Lite checks it for real
// (js/live.js). The examples come from data/showcase.js; demo/build_site.py checks each one
// gives the verdict it promises before it is published.

(() => {
  const box = document.getElementById("tryit");
  const showcase = window.MAGELLAN_SHOWCASE;
  if (!box || !showcase || !window.MagellanLive) return;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  box.innerHTML = `
    <div class="tryit-presets" aria-label="Examples">
      ${showcase.presets.map((p) => `<button class="chip-btn" data-preset="${esc(p.id)}">${esc(p.title)}</button>`).join("")}
    </div>
    <p class="tryit-blurb"></p>
    <div class="tryit-files" aria-label="Files"></div>
    <div class="tryit-editors">
      <label class="editor"><span>Before <small>the last commit</small></span>
        <textarea data-side="before" spellcheck="false" autocomplete="off" autocapitalize="off" wrap="off"></textarea></label>
      <label class="editor"><span>After <small>your change</small></span>
        <textarea data-side="after" spellcheck="false" autocomplete="off" autocapitalize="off" wrap="off"></textarea></label>
    </div>
    <div class="tryit-actions">
      <button class="button primary" data-act="run">Check this change</button>
      <span class="tryit-status muted small" role="status"></span>
    </div>
    <div class="tryit-result" aria-live="polite"></div>`;

  const $ = (s) => box.querySelector(s);
  const area = { before: $('[data-side="before"]'), after: $('[data-side="after"]') };
  const statusEl = $(".tryit-status");
  const result = $(".tryit-result");
  const status = (t) => { statusEl.textContent = t; };

  let files = {};     // {path: {before: text or null (no such file), after: text or null}}
  let open = null;
  let ran = false;

  function loadPreset(id) {
    const p = showcase.presets.find((x) => x.id === id) || showcase.presets[0];
    files = {};
    for (const path of new Set([...Object.keys(p.before), ...Object.keys(p.after)])) {
      files[path] = { before: p.before[path] ?? null, after: p.after[path] ?? null };
    }
    box.querySelectorAll("[data-preset]").forEach((b) => b.classList.toggle("active", b.dataset.preset === p.id));
    $(".tryit-blurb").textContent = p.blurb;
    result.innerHTML = "";
    openFile(files[p.open] ? p.open : Object.keys(files)[0]);
  }

  function drawTabs() {
    $(".tryit-files").innerHTML = Object.keys(files).sort().map((path) => {
      const f = files[path];
      const edited = (f.before ?? "") !== (f.after ?? "");
      return `<button class="file-tab${path === open ? " active" : ""}" data-file="${esc(path)}">
        ${esc(path)}${edited ? '<i class="edited" title="changed"></i>' : ""}</button>`;
    }).join("");
  }

  function openFile(path) {
    open = path;
    for (const side of ["before", "after"]) {
      area[side].value = files[path][side] ?? "";
      area[side].placeholder = files[path][side] === null ? "(no such file on this side)" : "";
    }
    drawTabs();
  }

  for (const side of ["before", "after"]) {
    area[side].addEventListener("input", () => {
      files[open][side] = area[side].value;
      drawTabs();
    });
    area[side].addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); }
    });
  }

  function versions() {
    const out = { before: {}, after: {} };
    for (const [path, f] of Object.entries(files)) {
      for (const side of ["before", "after"]) if (f[side] !== null) out[side][path] = f[side];
    }
    return out;
  }

  async function run() {
    ran = true;
    const btn = $('[data-act="run"]');
    btn.disabled = true;
    result.classList.add("busy");
    try {
      const { before, after } = versions();
      const report = await MagellanLive.check(before, after, status);
      draw(report);
      status(`Checked by ${report.engine}${report.ms !== undefined ? ` in ${report.ms} ms` : ""}. Edit either side and check again.`);
    } catch (err) {
      status(`Could not check: ${err.message}`);
    } finally {
      btn.disabled = false;
      result.classList.remove("busy");
    }
  }

  function draw(r) {
    const findings = r.findings || [], changes = r.changes || [], affected = r.affected || [];
    result.innerHTML = `
      <div class="result-head">
        <span class="verdict-big ${esc(r.verdict)}">${esc(r.verdict)}</span>
        <span>${plural(findings.length, "finding")} · ${plural(changes.length, "change")} in
          ${plural((r.files_changed || []).length, "file")}${affected.length ? ` · reaches ${plural(affected.length, "definition")} nobody edited` : ""}</span>
      </div>
      <div class="result-grid">
        <div class="result-list">
          <h4>Checklist</h4>
          ${findings.length ? `<ul class="findings">${findings.map((f, i) => `<li>
              <button class="finding-link" data-finding="${i}">
                <span class="sev ${esc(f.severity)}">${esc(f.severity)}</span>
                <code>${esc(f.rule)}</code> <span class="where">${esc(f.path)}:${f.line}</span></button>
              <p>${esc(f.message)}</p>
              ${f.detail ? `<p class="muted small">${esc(f.detail)}</p>` : ""}
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
    const map = MagellanMap.render(result.querySelector(".map-box"), r.map || MagellanMap.fromReport(r));
    map.play();
    result.querySelectorAll("[data-finding]").forEach((b) => b.addEventListener("click", () => {
      const f = findings[Number(b.dataset.finding)];
      if (files[f.path]) goTo(f.path, f.line);
    }));
  }

  // jump to a finding: open its file and select the line in "After"
  function goTo(path, line) {
    openFile(path);
    const lines = area.after.value.split("\n");
    const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
    area.after.focus();
    area.after.setSelectionRange(start, start + (lines[line - 1] || "").length);
    const lh = parseFloat(getComputedStyle(area.after).lineHeight) || 20;
    area.after.scrollTop = Math.max(0, (line - 4) * lh);
  }

  box.addEventListener("click", (e) => {
    const p = e.target.closest("[data-preset]");
    const f = e.target.closest("[data-file]");
    if (p) { loadPreset(p.dataset.preset); run(); }
    else if (f) openFile(f.dataset.file);
    else if (e.target.closest('[data-act="run"]')) run();
  });

  // TODO(site): a "Share" button that puts the current files in the address (the URL hash:
  //   JSON.stringify(files), then encodeURIComponent) and loads them when the page opens with
  //   one, so a judge can send a link to exactly what they tried. Done when reloading the
  //   shared link shows the same files and the same verdict.
  loadPreset(showcase.presets[0].id);

  // get the engine ready as the section comes near, then check the first example
  const io = new IntersectionObserver(async ([en]) => {
    if (!en.isIntersecting) return;
    io.disconnect();
    try {
      await MagellanLive.warm(status);
      status((await MagellanLive.hasServer())
        ? "Ready: the local server checks your change."
        : "Ready: Magellan Lite runs right here, in your browser. Nothing is uploaded.");
      if (!ran) run();
    } catch (err) {
      status(`Could not load the engine: ${err.message}. Check your connection, then press the button to retry.`);
    }
  }, { rootMargin: "300px" });
  io.observe(box);
})();
