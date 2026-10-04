// Magellan Lite site: the incident scoreboard and the report viewer. No dependencies, and it
// works when index.html is opened straight from disk (the data comes in as a script, not fetch).

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cls = (s) => String(s).toLowerCase().replace(/\s+/g, "-");
const chip = (text, kind = text) => `<span class="status ${cls(kind)}">${esc(text)}</span>`;

function renderIncidents() {
  const list = document.getElementById("incident-list");
  const summary = document.getElementById("incidents-summary");
  const data = window.MAGELLAN_INCIDENTS;
  if (!Array.isArray(data)) {
    summary.textContent = "No results yet: run python demo/run.py";
    return;
  }
  const steps = data.flatMap((inc) => inc.steps);
  const expected = steps.filter((s) => Object.keys(s.rules).length);
  const caught = expected.filter((s) => s.status === "caught").length;
  summary.textContent = `${caught} of ${expected.length} changes caught so far`;

  list.innerHTML = data.map((inc) => {
    const worst = inc.steps.some((s) => s.status === "MISSED") ? "missed"
      : inc.steps.some((s) => s.status === "waiting") ? "waiting"
      : inc.steps.some((s) => s.status === "caught") ? "caught" : "quiet";
    const rows = inc.steps.map((s) => {
      const rules = Object.entries(s.rules).map(([r, how]) => `${r}: ${how}`).join(" · ");
      const top = s.findings[0];
      return `<div class="step-row">
        <div>${chip(s.status)}</div>
        <div>
          <div>${esc(s.what)} ${s.verdict !== "ok" ? `→ ${chip(s.verdict)}` : ""}</div>
          ${rules ? `<div class="rules">${esc(rules)}</div>` : ""}
          ${top ? `<div class="rules">${esc(top.path)}:${top.line} ${esc(top.message)}</div>` : ""}
          ${s.known_miss ? `<div class="muted small">Known miss: ${esc(s.known_miss)}</div>` : ""}
        </div>
      </div>`;
    }).join("");
    return `<details class="card incident">
      <summary><span>${esc(inc.title)}</span>${chip(worst)}</summary>
      <p class="what">${esc(inc.what_happened)}</p>
      <p class="muted small">${esc(inc.language)} · ${esc((inc.sources || []).join("; "))}</p>
      ${rows}
    </details>`;
  }).join("");
}

function renderReport(report) {
  const view = document.getElementById("report-view");
  const findings = report.findings || [];
  const changes = report.changes || [];
  view.innerHTML = `
    <p>${chip(report.verdict || "?")} ${findings.length} finding(s) ·
       ${changes.length} change(s) in ${(report.files_changed || []).length} file(s)</p>
    ${findings.length ? `<ul>${findings.map((f) => `<li>
      <strong>${esc(f.severity)}</strong> <code>${esc(f.rule)}</code>
      ${esc(f.path)}:${f.line}<br>${esc(f.message)}
      ${f.fix ? `<br><span class="muted">fix: ${esc(f.fix)}</span>` : ""}</li>`).join("")}</ul>`
      : `<p class="muted">Nothing to check in what this change touched.</p>`}
    ${(report.errors || []).length ? `<p class="muted small">Not checked: ${esc(report.errors.join("; "))}</p>` : ""}
    ${(report.affected || []).length ? `<p class="muted small">Reaches ${report.affected.length} definition(s) the change did not touch:</p><div class="map-box"></div>` : ""}`;
  const box = view.querySelector(".map-box");
  // a report from the command line has no map: MagellanMap draws the change and what it reaches
  if (box && window.MagellanMap) MagellanMap.render(box, report.map || MagellanMap.fromReport(report)).play();
}

document.getElementById("report-file").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      renderReport(JSON.parse(reader.result));
    } catch (err) {
      document.getElementById("report-view").textContent =
        `That file is not a magellan-lite JSON report (${err.message}).`;
    }
  };
  reader.readAsText(file);
});

// TODO(starter): a "Copy" button on every code block, so a judge can copy the commands.
//   1. Find the blocks: document.querySelectorAll("pre.code")
//   2. For each one, make a button: const b = document.createElement("button");
//      b.textContent = "Copy"; and put it in the block: pre.prepend(b)
//   3. On click: navigator.clipboard.writeText(pre.innerText.replace("Copy", "").trim())
//      then set b.textContent = "Copied!" for a second (setTimeout).
//   4. Style it in css/style.css: `pre.code { position: relative; }` and the button
//      `position: absolute; top: 8px; right: 8px;`.

renderIncidents();