// The checklist section: a few of the rules Magellan Lite runs (data/showcase.js), the ones with
// a story a person knows, then "+ many more" for the rest; the heading keeps the full count.

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

  // the ones a person recognises: a broken caller, deleted code still used, old COBOL, and the
  // famous failures (Azure's leap day, Cloudflare's regex, Apple's goto fail, the Zune's loop)
  const FEATURED = ["signature-break", "removed-still-referenced", "copybook-layout-changed",
    "leap-day-date", "regex-catastrophic-backtracking", "unreachable-statement", "loop-without-progress"];
  const byId = new Map(rules.map((r) => [r.id, r]));
  let shown = FEATURED.map((id) => byId.get(id)).filter(Boolean);
  if (shown.length < 4) shown = rules.slice(0, FEATURED.length);   // the data changed: the worst first
  const rest = rules.length - shown.length + waiting.size;

  list.innerHTML = shown.map((r) => `
    <article class="rule-card">
      <p class="rule-top"><span class="sev ${esc(r.severity)}">${esc(r.severity)}</span>
        ${r.blocking ? '<span class="tag block">blocks the commit</span>' : ""}
        <span class="tag">${r.kind === "change" ? "reads the whole change" : "one file at a time"}</span></p>
      <h3><code>${esc(r.id)}</code></h3>
      <p class="muted small">${esc(r.fix)}</p>
    </article>`).join("") + (rest > 0 ? `
    <article class="rule-card more">
      <h3>+ many more</h3>
      <p class="muted small">${rest} more rules, from mutable defaults to COBOL moves that drop
        digits. <code>magellan-lite rules</code> lists them all.</p>
    </article>` : "");

  const count = document.getElementById("rule-count");
  if (count) {
    count.textContent = waiting.size ? `${rules.length} rules today, ${waiting.size} on the way`
      : `${rules.length} rules, every famous failure caught`;
  }
})();
