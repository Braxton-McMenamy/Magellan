// Which of a report's findings are in which definition on the map: the map panel's drawer
// (panel.js) lists a definition's findings when its dot is clicked. No DOM here, so plain Node
// tests it (test/findings.test.js).
//
//   MagellanFindings.of(node, report.findings, map.nodes)   // that definition's, worst first
//   MagellanFindings.sorted(report.findings)                // all of them, worst first

(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MagellanFindings = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const SEVERITIES = ["critical", "high", "medium", "low"];
  const rank = (f) => SEVERITIES.indexOf(f && f.severity) + 1 || SEVERITIES.length + 1;

  /** Worst first; the same severity in file and line order. */
  function sorted(findings) {
    return [...(findings || [])].sort((a, b) => rank(a) - rank(b)
      || String(a.path).localeCompare(String(b.path)) || (Number(a.line) || 0) - (Number(b.line) || 0));
  }

  /**
   * The definition a finding is in. The map marks each definition that holds a finding
   * (``finding: true``, from its first and last lines: magellan_lite/web.py) but has only first
   * lines, so of the marked ones in the finding's file it is the one that starts last at or
   * before the finding's line. A map without the marks: any definition but a class (a class's
   * finding is in one of its methods).
   */
  function owner(f, nodes) {
    const before = (nodes || []).filter((n) => n && !n.removed && n.path === f.path
      && Number(n.line) > 0 && Number(n.line) <= Number(f.line));
    const marked = before.filter((n) => n.finding);
    const pool = marked.length ? marked : before.filter((n) => n.kind !== "class");
    return pool.reduce((best, n) => (!best || Number(n.line) > Number(best.line) ? n : best), null);
  }

  /** The findings in ``node`` (a dot on the map), worst first: none for a dot without any. */
  function of(node, findings, nodes) {
    if (!node || !node.path || node.removed) return [];
    const all = nodes && nodes.length ? nodes : [node];
    return sorted((findings || []).filter((f) => {
      if (f.path !== node.path) return false;
      const o = owner(f, all);
      return !!o && o.id === node.id;
    }));
  }

  return { of, owner, sorted, SEVERITIES };
});
