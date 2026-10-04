"use strict";
// The Magellan Lite sidebar, laid out like the full Magellan's: an icon in the Activity Bar
// and three views -- Checklist (the findings), What it reaches (the blast radius) and Team
// (each teammate, with their conflicts with your work) -- plus finding counts as badges on
// files in the Explorer. Clicking an item opens its line.

const path = require("path");

const SEVERITY_ICON = {
  critical: ["error", "errorForeground"], high: ["error", "errorForeground"],
  medium: ["warning", "editorWarning.foreground"], low: ["info", "editorInfo.foreground"],
};
const VERDICT_ICON = {
  block: ["error", "errorForeground"], review: ["warning", "editorWarning.foreground"],
  ok: ["pass", "testing.iconPassed"],
};
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** Register the views and badges. `state.report` / `state.team` feed them; call
 *  `refresh()` after either changes. */
function installSidebar(vscode, { folder, state, sub }) {
  const refreshers = [];
  const icon = ([id, color] = []) => id && new vscode.ThemeIcon(id, color ? new vscode.ThemeColor(color) : undefined);

  const item = (label, description, { where, tooltip, iconOf, children = [] } = {}) => {
    const t = new vscode.TreeItem(label, children.length
      ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
    t.description = description;
    if (tooltip) t.tooltip = tooltip;
    if (iconOf) t.iconPath = icon(iconOf);
    if (where) t.command = { command: "magellanLite.openAt", title: "Open", arguments: [where.path, where.line] };
    t.children = children;
    return t;
  };

  const view = (id, roots) => {
    const changed = new vscode.EventEmitter();
    refreshers.push(() => changed.fire());
    sub(vscode.window.registerTreeDataProvider(id, {
      onDidChangeTreeData: changed.event,
      getTreeItem: (t) => t,
      getChildren: (t) => (t ? t.children : roots()),
    }));
  };

  const finding = (f, prefix = "") => item(f.rule, `${f.path}:${f.line}`, {
    where: f, iconOf: SEVERITY_ICON[f.severity],
    tooltip: `${prefix}${f.message}${f.fix ? `\nfix: ${f.fix}` : ""}`,
  });

  view("magellanLite.checklist", () => ((state.report && state.report.findings) || []).map((f) => finding(f)));

  view("magellanLite.reach", () => ((state.report && state.report.affected) || []).map((a) =>
    item(a.name.split(".").slice(-2).join("."), `${plural(a.hops, "hop")} · ${a.score.toFixed(2)}`,
      { where: a, tooltip: `${a.name}\nbecause ${a.why}`, iconOf: ["symbol-function"] })));

  view("magellanLite.team", () => (state.team || []).map((c) => item(c.name,
    c.verdict === "ok" ? "fine together" : `${c.verdict} · ${plural(c.findings.length, "conflict")}`, {
      iconOf: VERDICT_ICON[c.verdict], tooltip: `Your work together with ${c.name}'s work in progress`,
      children: c.findings.map((f) => finding(f, `With ${c.name}'s work in progress: `)),
    })));

  // finding counts on files in the Explorer, red when one of them is high or critical
  const badges = new vscode.EventEmitter();
  sub(vscode.window.registerFileDecorationProvider({
    onDidChangeFileDecorations: badges.event,
    provideFileDecoration: (uri) => {
      const f = folder();
      if (!f || !state.report) return undefined;
      const rel = path.relative(f.uri.fsPath, uri.fsPath).split(path.sep).join("/");
      const here = (state.report.findings || []).filter((x) => x.path === rel);
      if (!here.length) return undefined;
      const bad = here.some((x) => x.severity === "critical" || x.severity === "high");
      return new vscode.FileDecoration(String(Math.min(here.length, 99)),
        plural(here.length, "Magellan Lite finding"),
        new vscode.ThemeColor(bad ? "list.errorForeground" : "list.warningForeground"));
    },
  }));

  return {
    refresh() {
      refreshers.forEach((fire) => fire());
      badges.fire(undefined);                           // undefined: every file
    },
  };
}

module.exports = { installSidebar };
