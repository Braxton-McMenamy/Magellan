"use strict";
// The extension, run against a fake VS Code and a fake Python: node --test "editors/vscode/test/*.test.js"
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const { activate } = require("../extension");

const ROOT = path.resolve("/project");
const EXTENSION = path.resolve(__dirname, "..");        // editors/vscode: the repo's engine is two up

const CHECK = {
  verdict: "block", changes: [{ kind: "signature", name: "sensor.channel.parse_record" }],
  findings: [{ rule: "signature-break", severity: "critical", path: "sensor/collector.py", line: 11,
               message: "collect calls parse_record() the old way", fix: "Update the call." }],
  affected: [], map: { nodes: [], edges: [] },
};
const TEAM = [{ name: "braxton", verdict: "block", findings: [
  { rule: "signature-break", severity: "critical", path: "sensor/api.py", line: 5, message: "upload calls parse_record() the old way" }] }];

/** Just enough of VS Code for the extension to run, recording what it was asked to do. */
function fakeVscode(settings = {}) {
  const seen = { commands: {}, diagnostics: {}, messages: [], warnings: [], updates: [], answer: undefined,
                 onSave: null, onConfig: null, trees: {}, views: {}, context: {}, opened: [],
                 onSelection: null, panels: [], shown: [] };
  class Range { constructor(...a) { this.a = a; } }
  class Diagnostic { constructor(range, message, severity) { Object.assign(this, { range, message, severity }); } }
  class ThemeColor { constructor(id) { this.id = id; } }
  class EventEmitter { constructor() { this.event = () => ({ dispose() {} }); } fire() {} }
  class MarkdownString { constructor(value = "") { this.value = value; } }
  const vscode = {
    seen, Range, Diagnostic, ThemeColor, EventEmitter, MarkdownString,
    TreeItem: class { constructor(label, collapsibleState) { Object.assign(this, { label, collapsibleState }); } },
    TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
    ThemeIcon: class { constructor(id, color) { Object.assign(this, { id, color }); } },
    FileDecoration: class { constructor(badge, tooltip, color) { Object.assign(this, { badge, tooltip, color }); } },
    Position: class { constructor(l, c) { this.l = l; this.c = c; } },
    DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
    StatusBarAlignment: { Left: 1 }, ViewColumn: { Beside: -2 }, ConfigurationTarget: { Global: 1, Workspace: 2 },
    Uri: { file: (p) => ({ fsPath: p, toString: () => p }), parse: (u) => ({ toString: () => u }) },
    env: { openExternal: async (uri) => { seen.opened.push(uri.toString()); return true; } },
    workspace: {
      workspaceFolders: [{ uri: { fsPath: ROOT } }],
      getConfiguration: () => ({
        get: (k) => settings[k],
        inspect: (k) => ({ globalValue: settings[k] }),
        update: async (k, v, target) => { seen.updates.push([k, v, target]); settings[k] = v; },
      }),
      onDidSaveTextDocument: (fn) => { seen.onSave = fn; return { dispose() {} }; },
      onDidChangeConfiguration: (fn) => { seen.onConfig = fn; return { dispose() {} }; },
      openTextDocument: async (uri) => ({ uri }),
    },
    window: {
      createOutputChannel: () => ({ appendLine() {}, dispose() {} }),
      createStatusBarItem: () => (seen.status = { text: "", show() {}, hide() {}, dispose() {} }),
      showInformationMessage: (m) => seen.messages.push(m),
      showWarningMessage: (m) => { seen.warnings.push(m); return Promise.resolve(seen.answer); },
      createTreeView: (id, { treeDataProvider }) => {
        seen.trees[id] = treeDataProvider;
        return (seen.views[id] = { dispose() {} });
      },
      registerFileDecorationProvider: (p) => { seen.badges = p; return { dispose() {} }; },
      onDidChangeTextEditorSelection: (fn) => { seen.onSelection = fn; return { dispose() {} }; },
      activeTextEditor: undefined, visibleTextEditors: [],
      showTextDocument: async (doc, opts) => { seen.shown.push({ path: doc.uri.fsPath, ...opts }); },
      // a map panel: what the extension posts to it, and a way to post to the extension as it
      createWebviewPanel: () => {
        const panel = { posted: [], viewColumn: 2, hear: null, gone: null,
          webview: { cspSource: "vscode-resource:", asWebviewUri: (u) => u, html: "",
            postMessage: (m) => { panel.posted.push(m); return Promise.resolve(true); },
            onDidReceiveMessage: (fn) => { panel.hear = fn; return { dispose() {} }; } },
          onDidDispose: (fn) => { panel.gone = fn; return { dispose() {} }; },
          reveal() {}, dispose() { if (panel.gone) panel.gone(); } };
        seen.panels.push(panel);
        return panel;
      },
    },
    languages: {
      createDiagnosticCollection: (name) => ({
        clear: () => { seen.diagnostics[name] = {}; },
        set: (uri, list) => { (seen.diagnostics[name] = seen.diagnostics[name] || {})[uri.fsPath] = list; },
        dispose() {},
      }),
    },
    commands: {
      registerCommand: (id, fn) => { seen.commands[id] = fn; return { dispose() {} }; },
      executeCommand: (id, key, value) => { if (id === "setContext") seen.context[key] = value; },
    },
  };
  return vscode;
}

/** The subcommand of a `python [-3] -m magellan_lite <sub> ...` call. */
const sub = (args) => args[args.indexOf("magellan_lite") + 1];

/** A fake Python 3.12 (and git): `-c` (asking its version) says "3 12", `python -m
 *  magellan_lite <sub>` answers by subcommand, `git remote -v` with `answers.git`. Set
 *  `version` to make every interpreter older, or null to have none at all. */
function fakePython(answers, { version = "3 12" } = {}) {
  const calls = [];
  const execFile = (exe, args, opts, cb) => {
    calls.push({ exe, args, cwd: opts.cwd, env: opts.env });
    let a;
    if (exe === "git") a = answers.git === undefined ? new Error("not a git repository") : answers.git;
    else if (args.includes("-c")) a = version === null ? Object.assign(new Error("not found"), { code: "ENOENT" }) : version;
    else a = answers[sub(args)];
    setImmediate(() => (a instanceof Error ? cb(a, "", a.stderr || "") : cb(null, typeof a === "string" ? a : JSON.stringify(a), "")));
  };
  const checks = () => calls.filter((c) => c.exe !== "git" && !c.args.includes("-c"));
  return { execFile, calls, checks };
}

test("opening a folder checks it: problems on the right lines, the verdict in the status bar", async () => {
  const vscode = fakeVscode({ pythonPath: "py" });
  const py = fakePython({ check: CHECK });
  const ext = activate({ subscriptions: [], extensionPath: __dirname }, { vscode, execFile: py.execFile });
  await ext.first;
  assert.equal(py.checks()[0].exe, "py");                 // the setting, when someone sets one
  assert.equal(py.checks()[0].cwd, ROOT);
  const [d] = vscode.seen.diagnostics["magellan-lite"][path.join(ROOT, "sensor/collector.py")];
  assert.deepEqual(d.range.a, [10, 0, 10, 10000]);
  assert.equal(d.severity, vscode.DiagnosticSeverity.Error);
  assert.equal(d.code, "signature-break");
  assert.equal(vscode.seen.status.text, "$(error) Magellan Lite: block (1)");
});

test("the team check puts each conflict on the line, saying whose work it comes from", async () => {
  const vscode = fakeVscode();
  const py = fakePython({ check: CHECK, team: TEAM });
  const ext = activate({ subscriptions: [], extensionPath: __dirname }, { vscode, execFile: py.execFile });
  await ext.first;
  await vscode.seen.commands["magellanLite.team"]();
  const [d] = vscode.seen.diagnostics["magellan-lite-team"][path.join(ROOT, "sensor/api.py")];
  assert.match(d.message, /^With braxton's work in progress: upload calls/);
  assert.match(vscode.seen.messages.at(-1), /braxton's break together/);
});

test("without Magellan Lite installed, the status bar says how to fix it", async () => {
  const vscode = fakeVscode();
  const missing = Object.assign(new Error("exit 1"), { stderr: "python: No module named magellan_lite" });
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: fakePython({ check: missing }).execFile });
  await ext.first;
  assert.equal(vscode.seen.status.text, "$(alert) Magellan Lite");
  assert.match(vscode.seen.status.tooltip, /missing Magellan Lite's engine/);
});

test("the sidebar lists the checklist, what the change reaches, and the team", async () => {
  const vscode = fakeVscode();
  const report = { ...CHECK, affected: [{ name: "sensor.collector.sweep", path: "sensor/collector.py", line: 17,
                                          hops: 2, score: 0.77, why: "sweep calls collect" }] };
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: fakePython({ check: report, team: TEAM }).execFile });
  await ext.first;
  const trees = vscode.seen.trees;
  const [f] = trees["magellanLite.checklist"].getChildren();
  assert.deepEqual([f.label, f.description, f.command.arguments], ["signature-break", "sensor/collector.py:11", ["sensor/collector.py", 11]]);
  const [r] = trees["magellanLite.reach"].getChildren();
  assert.deepEqual([r.label, r.description], ["collector.sweep", "2 hops · 0.77"]);
  assert.equal(vscode.seen.context["magellanLite.state"], "ready");

  await vscode.seen.commands["magellanLite.team"]();
  const [mate] = trees["magellanLite.team"].getChildren();
  assert.deepEqual([mate.label, mate.description], ["braxton", "block · 1 conflict"]);
  assert.equal(trees["magellanLite.team"].getChildren(mate)[0].description, "sensor/api.py:5");

  const badge = vscode.seen.badges.provideFileDecoration({ fsPath: path.join(ROOT, "sensor", "collector.py") });
  assert.equal(badge.badge, "1");
  assert.equal(vscode.seen.badges.provideFileDecoration({ fsPath: path.join(ROOT, "sensor", "other.py") }), undefined);
});

test("when Python can't run Magellan Lite, the sidebar says so", async () => {
  const vscode = fakeVscode();
  const missing = Object.assign(new Error("exit 1"), { stderr: "No module named magellan_lite" });
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: fakePython({ check: missing }).execFile });
  await ext.first;
  assert.equal(vscode.seen.context["magellanLite.state"], "failed");
});

const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const saved = (vscode) => vscode.seen.onSave({ languageId: "python" });
const shares = (py) => py.calls.filter((c) => sub(c.args) === "share").length;

test("saving shares nothing unless Share On Save is on", async () => {
  const vscode = fakeVscode();
  const py = fakePython({ check: CHECK, share: "magellan-lite: shared" });
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: py.execFile, shareSettle: 5, shareEvery: 40 });
  await ext.first;
  saved(vscode);
  await pause(80);
  assert.equal(shares(py), 0);
});

test("Share On Save: a burst of saves is one share, and never more than one per interval", async () => {
  const vscode = fakeVscode({ shareOnSave: true });
  const py = fakePython({ check: CHECK, share: "magellan-lite: shared your work in progress as refs/wip/brayton" });
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: py.execFile, shareSettle: 5, shareEvery: 150 });
  await ext.first;
  saved(vscode); saved(vscode); saved(vscode);
  await pause(40);
  assert.equal(shares(py), 1);                       // the burst: once
  saved(vscode);
  await pause(40);
  assert.equal(shares(py), 1);                       // too soon after the last one: waits...
  await pause(160);
  assert.equal(shares(py), 2);                       // ...and then goes
  const args = py.calls.find((c) => sub(c.args) === "share").args;
  assert.deepEqual(args.slice(args.indexOf("-m")), ["-m", "magellan_lite", "share", "."]);
  assert.equal(vscode.seen.messages.length, 0);      // quiet when it works
});

test("Share On Save warns once when sharing fails, not on every save", async () => {
  const vscode = fakeVscode({ shareOnSave: true });
  const refused = Object.assign(new Error("exit 2"), { stderr: "magellan-lite: Permission denied (publickey)" });
  const py = fakePython({ check: CHECK, share: refused });
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: py.execFile, shareSettle: 5, shareEvery: 10 });
  await ext.first;
  saved(vscode);
  await pause(40);
  saved(vscode);
  await pause(40);
  assert.equal(shares(py), 2);
  assert.equal(vscode.seen.warnings.length, 1);
  assert.match(vscode.seen.warnings[0], /couldn't share/);
});

test("turning Share On Save on says who can read the work, and can turn it back off", async () => {
  const settings = { shareOnSave: true };
  const vscode = fakeVscode(settings);
  vscode.seen.answer = "Turn it off";
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: fakePython({ check: CHECK }).execFile });
  await ext.first;
  await vscode.seen.onConfig({ affectsConfiguration: (k) => k === "magellanLite.checkOnSave" });
  assert.equal(vscode.seen.warnings.length, 0);      // another setting: nothing to say
  vscode.seen.onConfig({ affectsConfiguration: (k) => k === "magellanLite.shareOnSave" });
  await pause(10);
  assert.match(vscode.seen.warnings[0], /public repository, everyone/);
  assert.deepEqual(vscode.seen.updates, [["shareOnSave", false, vscode.ConfigurationTarget.Global]]);
  assert.equal(settings.shareOnSave, false);
});

test("nobody configures a Python: the extension finds one and brings its own engine", async () => {
  const vscode = fakeVscode();                           // no magellanLite.pythonPath setting
  const py = fakePython({ check: CHECK });
  const ext = activate({ subscriptions: [], extensionPath: EXTENSION }, { vscode, execFile: py.execFile });
  await ext.first;
  const [probe] = py.calls.filter((c) => c.args.includes("-c"));
  assert.equal(probe.exe, process.platform === "win32" ? "py" : "python3");
  const run = py.checks()[0];
  assert.equal(run.exe, probe.exe);
  assert.deepEqual(run.args.slice(run.args.indexOf("-m"), run.args.indexOf("-m") + 3), ["-m", "magellan_lite", "check"]);
  // the engine goes first: the bundled engine/ when it was packaged, else this repository's
  const engine = require("../python").engineDir(EXTENSION);
  assert.ok([path.join(EXTENSION, "engine"), path.resolve(EXTENSION, "..", "..")].includes(engine));
  assert.equal(run.env.PYTHONPATH.split(path.delimiter)[0], engine);
  assert.equal(vscode.seen.status.text, "$(error) Magellan Lite: block (1)");
});

test("an old Python is passed over, and with none at all the sidebar says how to get one", async () => {
  const vscode = fakeVscode();
  const ext = activate({ subscriptions: [], extensionPath: EXTENSION },
    { vscode, execFile: fakePython({ check: CHECK }, { version: "3 8" }).execFile });
  await ext.first;
  assert.equal(vscode.seen.context["magellanLite.state"], "nopython");
  assert.match(vscode.seen.status.tooltip, /needs Python 3\.10 or newer; .* is Python 3\.8/);

  const none = fakeVscode();
  await activate({ subscriptions: [], extensionPath: EXTENSION },
    { vscode: none, execFile: fakePython({ check: CHECK }, { version: null }).execFile }).first;
  assert.equal(none.seen.context["magellanLite.state"], "nopython");
  assert.match(none.seen.status.tooltip, /none was found/);
});

test("the workspace's GitHub repository is found from git, and the Team suite opens connected to it", async () => {
  const vscode = fakeVscode();
  const py = fakePython({ check: CHECK, git: "origin\tgit@github.com:Braxton-McMenamy/Magellan.git (fetch)\n"
    + "origin\tgit@github.com:Braxton-McMenamy/Magellan.git (push)\n" });
  const ext = activate({ subscriptions: [], extensionPath: EXTENSION }, { vscode, execFile: py.execFile });
  await ext.first;
  assert.equal(ext.state.repo.slug, "Braxton-McMenamy/Magellan");
  const [top] = vscode.seen.trees["magellanLite.team"].getChildren();
  assert.deepEqual([top.label, top.command.command], ["Braxton-McMenamy/Magellan", "magellanLite.openSuite"]);
  await vscode.seen.commands["magellanLite.openSuite"]();
  assert.deepEqual(vscode.seen.opened, ["https://magellan-code.pages.dev/suite.html?repo=Braxton-McMenamy%2FMagellan"]);
});

test("the sidebar shows the verdict and counts what needs a person on its icon", async () => {
  const vscode = fakeVscode();
  const ext = activate({ subscriptions: [], extensionPath: EXTENSION },
    { vscode, execFile: fakePython({ check: CHECK }).execFile });
  await ext.first;
  const checklist = vscode.seen.views["magellanLite.checklist"];
  assert.equal(checklist.description, "BLOCK · 1");
  assert.equal(checklist.badge.value, 1);
  const [f] = vscode.seen.trees["magellanLite.checklist"].getChildren();
  assert.match(f.tooltip.value, /\*\*CRITICAL\*\* · `signature-break`/);
});

test("the status bar is red on a block, yellow on review, plain when ok", async () => {
  const vscode = fakeVscode();
  const answers = { check: CHECK };
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: fakePython(answers).execFile });
  await ext.first;
  assert.equal(vscode.seen.status.backgroundColor.id, "statusBarItem.errorBackground");
  answers.check = { ...CHECK, verdict: "review" };
  await ext.check();
  assert.equal(vscode.seen.status.backgroundColor.id, "statusBarItem.warningBackground");
  answers.check = { ...CHECK, verdict: "ok", findings: [] };
  await ext.check();
  assert.equal(vscode.seen.status.backgroundColor, undefined);
});

test("showLow off hides low findings from Problems and the Checklist; turning it on needs no new check", async () => {
  const settings = { showLow: false };
  const vscode = fakeVscode(settings);
  const low = { rule: "debug-leftover", severity: "low", path: "sensor/api.py", line: 3, message: "print() left in" };
  const py = fakePython({ check: { ...CHECK, findings: [...CHECK.findings, low] } });
  const ext = activate({ subscriptions: [], extensionPath: __dirname }, { vscode, execFile: py.execFile });
  await ext.first;
  const files = () => Object.keys(vscode.seen.diagnostics["magellan-lite"]).sort();
  const rules = () => vscode.seen.trees["magellanLite.checklist"].getChildren().map((t) => t.label);
  assert.deepEqual(files(), [path.join(ROOT, "sensor/collector.py")]);
  assert.deepEqual(rules(), ["signature-break"]);
  assert.equal(vscode.seen.views["magellanLite.checklist"].description, "BLOCK · 1");

  settings.showLow = true;
  vscode.seen.onConfig({ affectsConfiguration: (k) => k === "magellanLite.showLow" });
  assert.deepEqual(files(), [path.join(ROOT, "sensor/api.py"), path.join(ROOT, "sensor/collector.py")].sort());
  assert.deepEqual(rules(), ["signature-break", "debug-leftover"]);
  assert.equal(py.checks().length, 1);                   // the last report again, not a new check
});

// -- the map follows the code ------------------------------------------------------------------
const COLLECTOR = ["import os", "", "def collect(rows):", "    out = parse_record(rows)", "    return out",
  "", "", "def sweep():", "    return collect([])", ""].join("\n");
const MAPPED = { ...CHECK, map: { nodes: [
  { id: "sensor.collector.collect", label: "collect", path: "sensor/collector.py", line: 3, kind: "function" },
  { id: "sensor.collector.sweep", label: "sweep", path: "sensor/collector.py", line: 8, kind: "function" },
], edges: [{ src: "sensor.collector.sweep", dst: "sensor.collector.collect", kind: "calls" }] } };

/** An editor on sensor/collector.py with its cursor on ``line`` (1-based). */
const editorAt = (line, rel = "sensor/collector.py") => ({
  document: { uri: { scheme: "file", fsPath: path.join(ROOT, rel) }, getText: () => COLLECTOR },
  selection: { active: { line: line - 1, character: 0 } }, viewColumn: 1,
});
const moved = (vscode, line, rel) => vscode.seen.onSelection({ textEditor: editorAt(line, rel) });
const focuses = (panel) => panel.posted.filter((m) => m.type === "focus").map((m) => [m.id.split(".").pop(), !!m.quiet]);

test("the open map follows the cursor to the definition it rests in; a closed one is never opened for it", async () => {
  const vscode = fakeVscode();
  const ext = activate({ subscriptions: [], extensionPath: EXTENSION },
    { vscode, execFile: fakePython({ check: MAPPED }).execFile, followDelay: 5 });
  await ext.first;
  moved(vscode, 4);
  await pause(20);
  assert.equal(vscode.seen.panels.length, 0);            // no panel: nothing opens

  vscode.window.activeTextEditor = editorAt(4);
  ext.showMap();
  const [panel] = vscode.seen.panels;
  panel.hear({ type: "ready" });
  assert.equal(panel.posted[0].type, "report");
  // the panel just opened: it hears where the cursor is, quietly (it shows the whole map first)
  assert.deepEqual(focuses(panel), [["collect", true]]);

  moved(vscode, 9); moved(vscode, 8);                     // a burst of moves: one message
  await pause(30);
  assert.deepEqual(focuses(panel), [["collect", true], ["sweep", false]]);
  moved(vscode, 9);                                       // the same definition: nothing new
  moved(vscode, 1);                                       // outside them all: the map stays
  await pause(30);
  moved(vscode, 2, "sensor/elsewhere.py");                // a file the map doesn't know
  await pause(30);
  assert.equal(focuses(panel).length, 2);
});

test("opening code from the map opens it beside the panel, and does not move the map", async () => {
  const vscode = fakeVscode();
  const ext = activate({ subscriptions: [], extensionPath: EXTENSION },
    { vscode, execFile: fakePython({ check: MAPPED }).execFile, followDelay: 5 });
  await ext.first;
  vscode.window.visibleTextEditors = [editorAt(9)];
  ext.showMap();
  const [panel] = vscode.seen.panels;
  panel.hear({ type: "ready" });
  moved(vscode, 9);
  await pause(20);
  assert.deepEqual(focuses(panel), [["sweep", false]]);

  panel.hear({ type: "open", path: "sensor/collector.py", line: 3 });     // collect, clicked on the map
  await pause(5);
  assert.deepEqual(vscode.seen.shown.map((s) => [s.path, s.viewColumn]), [[path.join(ROOT, "sensor/collector.py"), 1]]);
  moved(vscode, 3);                                       // the cursor lands there: not a click in the code
  await pause(30);
  assert.deepEqual(focuses(panel), [["sweep", false]]);
  moved(vscode, 4);                                       // a click in collect, in the code: it follows
  await pause(30);
  assert.deepEqual(focuses(panel), [["sweep", false], ["collect", false]]);
});

test("a closed panel stops the following; a new one starts afresh", async () => {
  const vscode = fakeVscode();
  const ext = activate({ subscriptions: [], extensionPath: EXTENSION },
    { vscode, execFile: fakePython({ check: MAPPED }).execFile, followDelay: 5 });
  await ext.first;
  ext.showMap();
  const [first] = vscode.seen.panels;
  first.hear({ type: "ready" });
  moved(vscode, 4);
  await pause(20);
  first.dispose();
  assert.equal(ext.state.panel, null);
  moved(vscode, 9);
  await pause(20);
  assert.deepEqual(focuses(first), [["collect", false]]);
  vscode.window.activeTextEditor = editorAt(4);
  ext.showMap();
  const second = vscode.seen.panels[1];
  second.hear({ type: "ready" });
  assert.deepEqual(focuses(second), [["collect", true]]);   // told again, though it was told before
});
