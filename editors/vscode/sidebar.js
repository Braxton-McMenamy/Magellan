"use strict";
// The Magellan Lite sidebar, laid out like the full Magellan's: an icon in the Activity Bar
// (with a count of what needs a person) and three views -- Checklist (the findings), What it
// reaches (the blast radius, hottest first) and Team (this repository, then each teammate with
// their conflicts with your work) -- plus finding counts as badges on files in the Explorer.
// Clicking an item opens its line.

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
/** How hot a reached definition is: the colour of its flame. */
const heat = (score) => ["flame", score >= 0.7 ? "charts.red" : score >= 0.4 ? "charts.orange" : "charts.yellow"];

/** Register the views and badges. `state.report`, `state.team` and `state.repo` feed them;
 *  call `refresh()` after any of them changes (or `showLow()`, the magellanLite.showLow setting). */
function installSidebar(vscode, { folder, state, sub, showLow = () => true }) {
  const refreshers = [];
  const icon = ([id, color] = []) => id && new vscode.ThemeIcon(id, color ? new vscode.ThemeColor(color) : undefined);
  const markdown = (text) => {
    if (!vscode.MarkdownString) return text;
    const md = new vscode.MarkdownString(text);
    md.supportThemeIcons = true;
    return md;
  };
  const code = (s) => "`" + String(s).replace(/`/g, "'") + "`";

  const item = (label, description, { where, command, tooltip, iconOf, children = [] } = {}) => {
    const t = new vscode.TreeItem(label, children.length
      ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
    t.description = description;
    if (tooltip) t.tooltip = tooltip;
    if (iconOf) t.iconPath = icon(iconOf);
    if (where) t.command = { command: "magellanLite.openAt", title: "Open", arguments: [where.path, where.line] };
    if (command) t.command = command;
    t.children = children;
    return t;
  };

  /** A view; `decorate(treeView)` sets its header (description) and Activity Bar badge. */
  const view = (id, roots, decorate) => {
    const changed = new vscode.EventEmitter();
    const provider = {
      onDidChangeTreeData: changed.event,
      getTreeItem: (t) => t,
      getChildren: (t) => (t ? t.children : roots()),
    };
    const tree = vscode.window.createTreeView
      ? sub(vscode.window.createTreeView(id, { treeDataProvider: provider, showCollapseAll: false }))
      : (sub(vscode.window.registerTreeDataProvider(id, provider)), null);
    refreshers.push(() => {
      changed.fire();
      if (tree && decorate) decorate(tree);
    });
  };

  const finding = (f, prefix = "") => item(f.rule, `${f.path}:${f.line}`, {
    where: f, iconOf: SEVERITY_ICON[f.severity],
    tooltip: markdown(`**${String(f.severity).toUpperCase()}** · ${code(f.rule)} · ${code(`${f.path}:${f.line}`)}\n\n`
      + `${prefix}${f.message}${f.fix ? `\n\n$(lightbulb) ${f.fix}` : ""}`),
  });

  const findings = () => ((state.report && state.report.findings) || [])
    .filter((f) => f.severity !== "low" || showLow());
  view("magellanLite.checklist", () => findings().map((f) => finding(f)), (tree) => {
    const r = state.report;
    const serious = findings().filter((f) => f.severity === "critical" || f.severity === "high").length;
    tree.description = r ? `${String(r.verdict).toUpperCase()}${findings().length ? ` · ${findings().length}` : ""}` : "";
    tree.badge = serious ? { value: serious, tooltip: `Magellan Lite: ${plural(serious, "serious finding")}` } : undefined;
  });

  view("magellanLite.reach", () => ((state.report && state.report.affected) || []).map((a) =>
    item(a.name.split("@").pop().split(".").slice(-2).join("."), `${plural(a.hops, "hop")} · ${a.score.toFixed(2)}`, {
      where: a, iconOf: heat(a.score),
      tooltip: markdown(`${code(a.name)}\n\nscore **${a.score.toFixed(2)}**, ${plural(a.hops, "hop")} from the change\n\nbecause ${a.why}`),
    })), (tree) => {
    const n = ((state.report && state.report.affected) || []).length;
    tree.description = n ? `${n} reached` : "";
  });

  view("magellanLite.team", () => {
    const rows = [];
    if (state.repo) {
      rows.push(item(state.repo.slug, "open the Team suite", {
        iconOf: ["github"], command: { command: "magellanLite.openSuite", title: "Open the Team suite" },
        tooltip: markdown(`This workspace is ${code(state.repo.slug)} on GitHub (remote ${code(state.repo.remote)}).\n\n`
          + "Open the website's Team suite, already connected to it."),
      }));
    }
    if (!state.team) {
      if (rows.length) {
        rows.push(item("Share my work in progress", "", { iconOf: ["cloud-upload"],
          command: { command: "magellanLite.share", title: "Share" } }));
        rows.push(item("Check against my team", "", { iconOf: ["organization"],
          command: { command: "magellanLite.team", title: "Check" } }));
      }
      return rows;
    }
    return rows.concat(state.team.map((c) => item(c.name,
      c.verdict === "ok" ? "fine together" : `${c.verdict} · ${plural(c.findings.length, "conflict")}`, {
        iconOf: VERDICT_ICON[c.verdict], tooltip: `Your work together with ${c.name}'s work in progress`,
        children: c.findings.map((f) => finding(f, `With ${c.name}'s work in progress: `)),
      })));
  }, (tree) => {
    const bad = (state.team || []).filter((c) => c.verdict !== "ok").length;
    tree.description = state.team ? (bad ? `${plural(bad, "conflict")}` : "fine together") : "";
  });

  // finding counts on files in the Explorer, red when one of them is high or critical
  const badges = new vscode.EventEmitter();
  sub(vscode.window.registerFileDecorationProvider({
    onDidChangeFileDecorations: badges.event,
    provideFileDecoration: (uri) => {
      const f = folder();
      if (!f || !state.report) return undefined;
      const rel = path.relative(f.uri.fsPath, uri.fsPath).split(path.sep).join("/");
      const here = findings().filter((x) => x.path === rel);
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
