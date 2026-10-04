// The map panel: the last check's verdict, the map of the change and what it reaches, and the
// checklist. The map is drawn by the website's renderer (map.js), so it looks the same here,
// on the website's incident stories, and on the account pages to come.

(() => {
  const api = acquireVsCodeApi();
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  // TODO(braxton): the 3D and 2D views from the original Magellan, switchable here (and later
  //   on the website's personal and team pages, which use the same renderers).
  //   1. Copy magellan/interfaces/ui/graph3d.js (3D) and graph.js (2D) from the original
  //      Magellan repo into site/js/: the website holds the originals. Add both to
  //      editors/vscode/shared.json, add their names to SCRIPTS in editors/vscode/panel.js, and
  //      run `node editors/vscode/sync.js` to copy them here.
  //   2. They take `{ nodes, edges, center }`. A Magellan Lite map (report.map) is
  //      `{ nodes, edges }` with the same `id`, `path` and `kind` on nodes and `src`, `dst`,
  //      `kind` on edges. Set `center` to the id of the first node with a `change`, and give
  //      each node `impact: node.score` so what the change reaches glows.
  //   3. Above #graph, add a "Map · 2D · 3D" switch that swaps which view draws into #graph
  //      (the Map view is the MagellanMap.render call below).
  //   4. In the top README.md, under a "Code from before the event" heading, name the files you
  //      copied and say they come from Magellan, written before the hackathon.
  //   Done when the panel shows the CrowdStrike-class change in 3D and switches back.
  function drawMap(report) {
    const box = $("#graph");
    const map = report.map || MagellanMap.fromReport(report);
    MagellanMap.render(box, map).play();
  }

  function draw(report) {
    const findings = report.findings || [], affected = report.affected || [];
    const changes = report.changes || [];
    $("#head").innerHTML = `
      <span class="verdict ${esc(report.verdict)}">${esc(report.verdict)}</span>
      <span>${plural(findings.length, "finding")} · ${plural(changes.length, "change")}${
        affected.length ? ` · reaches ${plural(affected.length, "definition")} nobody edited` : ""}</span>`;
    drawMap(report);
    $("#lists").innerHTML = `
      <h2>Checklist</h2>
      ${findings.length ? `<ul class="findings">${findings.map((f, i) => `<li>
          <button data-i="${i}"><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span>
            <code>${esc(f.rule)}</code> <span class="where">${esc(f.path)}:${f.line}</span></button>
          <p>${esc(f.message)}</p>${f.fix ? `<p class="fix">fix: ${esc(f.fix)}</p>` : ""}</li>`).join("")}</ul>`
        : `<p class="muted">Nothing to check in what this change touched.</p>`}
      ${affected.length ? `<h2>What it reaches</h2><ul class="reach">${affected.slice(0, 12).map((a) => `<li>
          <button data-path="${esc(a.path)}" data-line="${a.line}"><b>${a.score.toFixed(2)}</b>
            <code>${esc(a.name)}</code> <span class="where">${a.hops} hop${a.hops === 1 ? "" : "s"}</span></button></li>`).join("")}</ul>` : ""}`;
    $("#lists").querySelectorAll("button[data-i]").forEach((b) => b.addEventListener("click", () => {
      const f = findings[Number(b.dataset.i)];
      api.postMessage({ type: "open", path: f.path, line: f.line });
    }));
    $("#lists").querySelectorAll("button[data-path]").forEach((b) => b.addEventListener("click", () => {
      api.postMessage({ type: "open", path: b.dataset.path, line: Number(b.dataset.line) });
    }));
  }

  window.addEventListener("message", (e) => {
    const d = e.data || {};
    if (d.type === "report" && d.report) draw(d.report);
  });
  api.postMessage({ type: "ready" });
})();
