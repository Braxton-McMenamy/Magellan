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
  const seen = { commands: {}, diagnostics: {}, messages: [], onSave: null };
  class Range { constructor(...a) { this.a = a; } }
  class Diagnostic { constructor(range, message, severity) { Object.assign(this, { range, message, severity }); } }
  class ThemeColor { constructor(id) { this.id = id; } }
  const vscode = {
    seen, Range, Diagnostic, ThemeColor,
    Position: class { constructor(l, c) { this.l = l; this.c = c; } },
    DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
    StatusBarAlignment: { Left: 1 }, ViewColumn: { Beside: -2 },
    Uri: { file: (p) => ({ fsPath: p, toString: () => p }) },
    workspace: {
      workspaceFolders: [{ uri: { fsPath: ROOT } }],
      getConfiguration: () => ({ get: (k) => settings[k] }),
      onDidSaveTextDocument: (fn) => { seen.onSave = fn; return { dispose() {} }; },
    },
    window: {
      createOutputChannel: () => ({ appendLine() {}, dispose() {} }),
      createStatusBarItem: () => (seen.status = { text: "", show() {}, hide() {}, dispose() {} }),
      showInformationMessage: (m) => seen.messages.push(m),
    },
    languages: {
      createDiagnosticCollection: (name) => ({
        clear: () => { seen.diagnostics[name] = {}; },
        set: (uri, list) => { (seen.diagnostics[name] = seen.diagnostics[name] || {})[uri.fsPath] = list; },
        dispose() {},
      }),
    },
    commands: { registerCommand: (id, fn) => { seen.commands[id] = fn; return { dispose() {} }; } },
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

test("the status bar is red on a block (TODO(faidh) 1)", { skip: "TODO(faidh) 1: status bar colours" }, async () => {
  const vscode = fakeVscode();
  const ext = activate({ subscriptions: [], extensionPath: __dirname },
    { vscode, execFile: fakePython({ check: CHECK }).execFile });
  await ext.first;
  assert.equal(vscode.seen.status.backgroundColor.id, "statusBarItem.errorBackground");
});
