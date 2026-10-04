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

// Text to the clipboard: the Clipboard API where the page may use it, else the old way, through
// a hidden text box. Resolves true when it worked. Try it's Share button uses it too.
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* refused (no permission, or the page lost focus): try the old way */ }
  const box = document.createElement("textarea");
  box.value = text;
  box.setAttribute("readonly", "");
  box.setAttribute("aria-hidden", "true");
  box.className = "clipboard-box";
  const was = document.activeElement;
  document.body.append(box);
  box.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  box.remove();
  if (was && was.focus) was.focus({ preventScroll: true });
  return ok;
}
window.MagellanCopy = copyText;

// a "Copy" button on every code block, so a judge can copy the commands. It sits beside the
// block, not in it, so it stays in the corner when the block scrolls and is never copied.
function addCopyButtons() {
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  document.querySelectorAll("pre.code").forEach((pre) => {
    if (pre.parentElement.classList.contains("code-wrap")) return;
    const wrap = document.createElement("div");
    wrap.className = "code-wrap";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-button";
    button.textContent = "Copy";
    const said = document.createElement("span");      // what a screen reader hears
    said.className = "sr-only";
    said.setAttribute("role", "status");
    pre.before(wrap);
    wrap.append(pre, button, said);
    let timer = 0;
    button.addEventListener("click", async () => {
      const ok = await copyText(pre.textContent.trim());
      if (!ok) {                                       // no clipboard: select it for Ctrl+C
        const range = document.createRange();
        range.selectNodeContents(pre);
        getSelection().removeAllRanges();
        getSelection().addRange(range);
      }
      button.textContent = ok ? "Copied" : `Press ${mac ? "⌘" : "Ctrl+"}C`;
      button.classList.toggle("done", ok);
      said.textContent = ok ? "Copied to the clipboard." : "Selected: copy it with the keyboard.";
      clearTimeout(timer);
      timer = setTimeout(() => {
        button.textContent = "Copy";
        button.classList.remove("done");
        said.textContent = "";
      }, ok ? 1600 : 4000);
    });
  });
}

renderIncidents();
addCopyButtons();