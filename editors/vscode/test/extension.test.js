"use strict";
// The extension, run against a fake VS Code and a fake Python: node --test "editors/vscode/test/*.test.js"
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const { activate } = require("../extension");

const ROOT = path.resolve("/project");

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
                 onSave: null, onConfig: null, trees: {}, context: {} };
  class Range { constructor(...a) { this.a = a; } }
  class Diagnostic { constructor(range, message, severity) { Object.assign(this, { range, message, severity }); } }
  class ThemeColor { constructor(id) { this.id = id; } }
  class EventEmitter { constructor() { this.event = () => ({ dispose() {} }); } fire() {} }
  const vscode = {
    seen, Range, Diagnostic, ThemeColor, EventEmitter,
    TreeItem: class { constructor(label, collapsibleState) { Object.assign(this, { label, collapsibleState }); } },
    TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
    ThemeIcon: class { constructor(id, color) { Object.assign(this, { id, color }); } },
    FileDecoration: class { constructor(badge, tooltip, color) { Object.assign(this, { badge, tooltip, color }); } },
    Position: class { constructor(l, c) { this.l = l; this.c = c; } },
    DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
    StatusBarAlignment: { Left: 1 }, ViewColumn: { Beside: -2 }, ConfigurationTarget: { Global: 1, Workspace: 2 },
    Uri: { file: (p) => ({ fsPath: p, toString: () => p }) },
    workspace: {
      workspaceFolders: [{ uri: { fsPath: ROOT } }],
      getConfiguration: () => ({
        get: (k) => settings[k],
        inspect: (k) => ({ globalValue: settings[k] }),
        update: async (k, v, target) => { seen.updates.push([k, v, target]); settings[k] = v; },
      }),
      onDidSaveTextDocument: (fn) => { seen.onSave = fn; return { dispose() {} }; },
      onDidChangeConfiguration: (fn) => { seen.onConfig = fn; return { dispose() {} }; },
    },
    window: {
      createOutputChannel: () => ({ appendLine() {}, dispose() {} }),
      createStatusBarItem: () => (seen.status = { text: "", show() {}, hide() {}, dispose() {} }),
      showInformationMessage: (m) => seen.messages.push(m),
      showWarningMessage: (m) => { seen.warnings.push(m); return Promise.resolve(seen.answer); },
      registerTreeDataProvider: (id, provider) => { seen.trees[id] = provider; return { dispose() {} }; },
      registerFileDecorationProvider: (p) => { seen.badges = p; return { dispose() {} }; },
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

/** A fake `python -m magellan_lite ...`: answers by subcommand. */
function fakePython(answers) {
  const calls = [];
  const execFile = (exe, args, opts, cb) => {
    calls.push({ exe, args, cwd: opts.cwd });
    const a = answers[args[2]];
    setImmediate(() => (a instanceof Error ? cb(a, "", a.stderr || "") : cb(null, typeof a === "string" ? a : JSON.stringify(a), "")));
  };
  return { execFile, calls };
}

test("opening a folder checks it: problems on the right lines, the verdict in the status bar", async () => {
  const vscode = fakeVscode({ pythonPath: "py" });
  const py = fakePython({ check: CHECK });
  const ext = activate({ subscriptions: [], extensionPath: __dirname }, { vscode, execFile: py.execFile });
  await ext.first;
  assert.equal(py.calls[0].exe, "py");
  assert.equal(py.calls[0].cwd, ROOT);
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
  assert.match(vscode.seen.status.tooltip, /pip install -e/);
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
const shares = (py) => py.calls.filter((c) => c.args[2] === "share").length;

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
  assert.deepEqual(py.calls.find((c) => c.args[2] === "share").args, ["-m", "magellan_lite", "share", "."]);
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

test("the status bar is red on a block (TODO(faidh) 1)", { skip: "TODO(faidh) 1: status bar colours" }, async () => {
  const vscode = fakeVscode();
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: fakePython({ check: CHECK }).execFile });
  await ext.first;
  assert.equal(vscode.seen.status.backgroundColor.id, "statusBarItem.errorBackground");
});
