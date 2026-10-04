// The checklist section: every rule Magellan Lite runs today (data/showcase.js), then the
// rules still being written, each with the famous failure it will catch (data/incidents.js).

(() => {
  const list = document.getElementById("rule-list");
  const showcase = window.MAGELLAN_SHOWCASE;
  if (!list || !showcase) return;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const order = { critical: 0, high: 1, medium: 2, low: 3 };
  const rules = [...showcase.rules].sort((a, b) =>
    (order[a.severity] - order[b.severity]) || a.id.localeCompare(b.id));

  // rules the incidents expect that nobody has written yet, and which failures they catch
  const waiting = new Map();
  for (const inc of window.MAGELLAN_INCIDENTS || []) {
    for (const step of inc.steps) {
      for (const [rule, how] of Object.entries(step.rules)) {
        if (how !== "waiting") continue;
        if (!waiting.has(rule)) waiting.set(rule, new Set());
        waiting.get(rule).add(inc.title.split(":")[0]);
      }
    }
  }

  list.innerHTML = rules.map((r) => `
    <article class="rule-card">
      <p class="rule-top"><span class="sev ${esc(r.severity)}">${esc(r.severity)}</span>
        ${r.blocking ? '<span class="tag block">blocks the commit</span>' : ""}
        <span class="tag">${r.kind === "change" ? "reads the whole change" : "one file at a time"}</span></p>
      <h3><code>${esc(r.id)}</code></h3>
      <p class="muted small">${esc(r.fix)}</p>
    </article>`).join("") + [...waiting].map(([rule, incs]) => `
    <article class="rule-card waiting">
      <p class="rule-top"><span class="tag">rule in progress</span></p>
      <h3><code>${esc(rule)}</code></h3>
      <p class="muted small">Catches: ${[...incs].map(esc).join("; ")}</p>
    </article>`).join("");

  const count = document.getElementById("rule-count");
  if (count) count.textContent = `${rules.length} rules today, ${waiting.size} on the way`;
})();
