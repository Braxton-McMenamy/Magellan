"use strict";
/*
 * Magellan Lite for VS Code: the checklist where the damage lands.
 *
 *   saving a Python file             magellan-lite check: problems in the Problems panel (and
 *                                    squiggles on the lines), the verdict in the status bar
 *   Magellan Lite: Check this change the same, now
 *   the Magellan Lite sidebar        Checklist, What it reaches, Team (sidebar.js), and finding
 *                                    counts on files in the Explorer, like the full Magellan's
 *   Magellan Lite: Show the map      a panel: the change, what it reaches, the checklist
 *   Magellan Lite: Share my work...  magellan-lite share (your work in progress, for your team)
 *   magellanLite.shareOnSave         the same after you save (off unless you turn it on), so
 *                                    your team's Team suite on the website sees your work live
 *   Magellan Lite: Check against my team's work in progress
 *                                    magellan-lite team: what only your work + a teammate's breaks
 *
 *   Magellan Lite: Open the Team suite
 *                                    the website's live team view, already connected to this
 *                                    workspace's GitHub repository (found from its git remote)
 *
 * Nothing to set up: the extension carries Magellan Lite's engine and finds a Python 3.10+ by
 * itself (python.js), and finds the workspace's GitHub repository from git (repo.js).
 * `activate(context, deps)` takes optional `vscode` and `execFile` so tests can run it without
 * VS Code: see test/extension.test.js.
 */
const path = require("path");
const { execFile } = require("child_process");
const lite = require("./lite");
const py = require("./python");
const repos = require("./repo");
const { panelHtml, nonce } = require("./panel");
const { installSidebar } = require("./sidebar");

function activate(context, deps = {}) {
  const vscode = deps.vscode || require("vscode");
  const run = deps.execFile || execFile;
  const cfg = () => vscode.workspace.getConfiguration("magellanLite");
  const showLow = () => cfg().get("showLow") !== false;
  const folder =() => (vscode.workspace.workspaceFolders || [])[0];
  const sub = (d) => { context.subscriptions.push(d); return d; };

  const out = sub(vscode.window.createOutputChannel("Magellan Lite"));
  const checks = sub(vscode.languages.createDiagnosticCollection("magellan-lite"));
  const conflicts = sub(vscode.languages.createDiagnosticCollection("magellan-lite-team"));
  const status = sub(vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50));
  status.command = "magellanLite.showMap";
  status.text = "$(sync~spin) Magellan Lite";                // visible from the first moment
  status.tooltip = folder() ? "Checking your change..." : "Open a folder to check it";
  status.show();
  out.appendLine(`Magellan Lite activated in ${folder() ? folder().uri.fsPath : "a window with no folder"}`);
  const state = { report: null, team: null, panel: null, timer: null, repo: null,
    shareTimer: null, sharing: false, lastShare: 0, shareFailed: false };
  const sidebar = installSidebar(vscode, { folder, state, sub, showLow });
  // the sidebar's empty views say which: checking, nofolder, failed (and how to fix it), ready
  const setState = (s) => Promise.resolve(vscode.commands.executeCommand("setContext", "magellanLite.state", s)).catch(() => {});
  setState(folder() ? "checking" : "nofolder");

  // the engine this extension carries (or this repository's), and a Python to run it with
  const engine = py.engineDir(context.extensionPath || __dirname);
  let interpreter = null;                 // a promise of { exe, pre, version } or { error }
  function findPython() {
    if (!interpreter) {
      interpreter = (async () => {
        const f = folder();
        const selected = await py.selectedInterpreter(vscode, f && f.uri);
        const found = await py.find(py.candidates({ setting: cfg().get("pythonPath"), selected }),
          run, f && f.uri.fsPath);
        out.appendLine(found.error || `Python ${found.version}: ${[found.exe, ...found.pre].join(" ")}`
          + ` · engine: ${engine || "the magellan_lite installed in that Python"}`);
        return found;
      })();
    }
    return interpreter;
  }

  /** Run `python -m magellan_lite ...` in the folder: `{ data }` (its JSON), `{ text }`, or
   *  `{ error }` with something a person can act on (`nopython` when there is no Python). */
  async function python(args, json = true) {
    const f = folder();
    if (!f) return { error: "Open a folder first." };
    const found = await findPython();
    if (found.error) {
      interpreter = null;                 // installed one since? the next run looks again
      return { error: found.error, nopython: true };
    }
    out.appendLine(`$ ${[found.exe, ...found.pre, ...args].join(" ")}`);
    return new Promise((resolve) => {
      run(found.exe, [...found.pre, ...args],
        { cwd: f.uri.fsPath, env: py.environment(engine), maxBuffer: 20 * 1024 * 1024,
          timeout: 120000, windowsHide: true },
        (err, stdout, stderr) => {
          if (stderr) out.appendLine(String(stderr).trim());
          if (!json) {
            return resolve(err ? lite.parseOutput("", stderr || err.message) : { text: String(stdout).trim() });
          }
          resolve(lite.parseOutput(stdout, err && !stdout ? stderr || err.message : stderr));
        });
    });
  }

  // which GitHub repository this is: for the Team view and the Team suite's link
  const findRepo = async () => {
    const f = folder();
    state.repo = f ? await repos.detect({ vscode, folderPath: f.uri.fsPath, run }).catch(() => null) : null;
    sidebar.refresh();
    if (state.panel) state.panel.webview.postMessage({ type: "repo", repo: state.repo && state.repo.slug });
    return state.repo;
  };
  async function openSuite() {
    const found = state.repo || await findRepo();
    if (!found) {
      vscode.window.showInformationMessage("Magellan Lite: this folder has no GitHub remote, so the Team suite can't read it. Push it to GitHub first (git remote add origin ...).");
      return null;
    }
    const url = repos.suiteUrl(found.slug, undefined, found.folder);   // a subfolder: that folder alone
    await vscode.env.openExternal(vscode.Uri.parse(url));
    return url;
  }

  /** Put problems (by project-relative file) into a diagnostics collection. */
  function show(collection, byFile) {
    collection.clear();
    const root = folder().uri.fsPath;
    for (const [rel, list] of Object.entries(byFile)) {
      collection.set(vscode.Uri.file(path.join(root, rel)), list.map((p) => {
        const d = new vscode.Diagnostic(new vscode.Range(p.line, 0, p.line, 10000),
          p.message + (p.fix ? `\nfix: ${p.fix}` : ""), vscode.DiagnosticSeverity[p.severity]);
        d.source = "Magellan Lite";
        d.code = p.rule;
        return d;
      }));
    }
  }

  function failed(why, nopython = false) {
    setState(nopython ? "nopython" : "failed");
    status.text = "$(alert) Magellan Lite";
    status.tooltip = why;
    status.backgroundColor = undefined;
    status.show();
    out.appendLine(why);
  }

  async function check() {
    status.text = "$(sync~spin) Magellan Lite";
    status.show();
    const r = await python(lite.checkArgs(cfg().get("against")));
    if (r.error) return failed(r.error, r.nopython), null;
    state.report = r.data;
    show(checks, lite.problems(r.data, "", showLow()));
    sidebar.refresh();
    setState("ready");
    const s = lite.statusFor(r.data);
    status.text = s.text;
    status.tooltip = s.tip + (state.lastShareText ? `\n${state.lastShareText}` : "");
    // coloured by verdict, so a block can't be missed: red on block, yellow on review
    const colour = VERDICT_BACKGROUND[r.data.verdict];
    status.backgroundColor = colour ? new vscode.ThemeColor(colour) : undefined;
    if (state.panel) state.panel.webview.postMessage({ type: "report", report: r.data, repo: state.repo && state.repo.slug });
    return r.data;
  }

  async function team() {
    const r = await python(lite.teamArgs());
    if (r.error) return failed(r.error, r.nopython), null;
    state.team = r.data;
    show(conflicts, lite.teamProblems(r.data));
    sidebar.refresh();
    const broken = r.data.filter((c) => c.verdict !== "ok");
    vscode.window.showInformationMessage(!r.data.length
      ? "Magellan Lite: nobody on your team has shared work in progress yet (they run \"Share my work in progress\")."
      : broken.length
        ? `Magellan Lite: your work and ${broken.map((c) => c.name).join(", ")}'s break together: see the Problems panel.`
        : `Magellan Lite: your work is fine together with ${r.data.map((c) => c.name).join(", ")}'s.`);
    return r.data;
  }

  async function share() {
    const r = await python(lite.shareArgs(), false);
    if (r.error) return failed(r.error, r.nopython), null;
    vscode.window.showInformationMessage(r.text.replace(/^magellan-lite: /, "Magellan Lite: "));
    return r.text;
  }

  // Share on save: after a save, push your work in progress -- once the saves stop for a
  // moment (SETTLE), and never more than once every EVERY. Quiet when it works (the output
  // channel and the status bar's tooltip say when); one warning when it fails, until it works.
  const EVERY = deps.shareEvery ?? 15000, SETTLE = deps.shareSettle ?? 2000;
  function shareSoon() {
    clearTimeout(state.shareTimer);
    const wait = Math.max(SETTLE, state.lastShare + EVERY - Date.now());
    state.shareTimer = setTimeout(async () => {
      if (state.sharing) return shareSoon();          // one push at a time
      state.sharing = true;
      state.lastShare = Date.now();
      try {
        const r = await python(lite.shareArgs(), false);
        if (r.error) {
          out.appendLine(`share on save: ${r.error}`);
          if (!state.shareFailed) {
            state.shareFailed = true;
            vscode.window.showWarningMessage(`Magellan Lite couldn't share your work in progress: ${r.error}`);
          }
        } else {
          state.shareFailed = false;
          out.appendLine(`share on save: ${r.text}`);
          state.lastShareText = `Shared your work in progress at ${new Date().toLocaleTimeString()}`;
        }
      } finally {
        state.sharing = false;
      }
    }, wait);
  }

  // turning it on says plainly who will be able to read your work
  async function warnShareOnSave() {
    const pick = await vscode.window.showWarningMessage(
      "Magellan Lite will now push your work in progress to refs/wip/<your name> on your git remote " +
      "after you save. Anyone who can read the repository can read it: on a public repository, " +
      "everyone. Every file git doesn't ignore goes, saved or not, so keep secrets in .gitignore'd files.",
      "OK", "Turn it off");
    if (pick === "Turn it off") {
      const where = cfg().inspect ? cfg().inspect("shareOnSave") : null;
      const target = where && where.workspaceValue !== undefined
        ? vscode.ConfigurationTarget.Workspace : vscode.ConfigurationTarget.Global;
      await cfg().update("shareOnSave", false, target);
    }
    return pick;
  }

  /** Open a project-relative file at a (1-based) line. */
  async function openAt(rel, line) {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(folder().uri.fsPath, rel)));
    const at = new vscode.Position(Math.max(0, (line || 1) - 1), 0);
    await vscode.window.showTextDocument(doc, { selection: new vscode.Range(at, at), preview: false });
  }

  function showMap() {
    if (state.panel) { state.panel.reveal(); return state.panel; }
    const media = vscode.Uri.file(path.join(context.extensionPath, "media"));
    const panel = vscode.window.createWebviewPanel("magellanLite.map", "Magellan Lite map",
      vscode.ViewColumn.Beside, { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [media] });
    state.panel = panel;
    panel.onDidDispose(() => { state.panel = null; });
    panel.webview.onDidReceiveMessage((m) => {
      if (m.type === "ready" && state.report) {
        panel.webview.postMessage({ type: "report", report: state.report, repo: state.repo && state.repo.slug });
      }
      if (m.type === "open") openAt(m.path, m.line).catch((e) => out.appendLine(`could not open ${m.path}: ${e.message}`));
      // the panel's buttons may run these, and only these
      if (m.type === "run" && PANEL_COMMANDS.has(m.command)) vscode.commands.executeCommand(m.command);
    });
    panel.webview.html = panelHtml({
      uri: (f) => panel.webview.asWebviewUri(vscode.Uri.file(path.join(media.fsPath, f))),
      cspSource: panel.webview.cspSource, nonce: nonce(),
    });
    if (!state.report) check();
    return panel;
  }

  sub(vscode.commands.registerCommand("magellanLite.check", check));
  sub(vscode.commands.registerCommand("magellanLite.team", team));
  sub(vscode.commands.registerCommand("magellanLite.share", share));
  sub(vscode.commands.registerCommand("magellanLite.showMap", showMap));
  sub(vscode.commands.registerCommand("magellanLite.showOutput", () => out.show(true)));
  sub(vscode.commands.registerCommand("magellanLite.openSuite", openSuite));
  sub(vscode.commands.registerCommand("magellanLite.openAt", (rel, line) =>
    openAt(rel, line).catch((e) => out.appendLine(`could not open ${rel}: ${e.message}`))));
  sub(vscode.workspace.onDidSaveTextDocument((doc) => {
    if (!lite.isSource(doc.fileName || "") && doc.languageId !== "python") return;
    if (cfg().get("shareOnSave") === true) shareSoon();
    if (cfg().get("checkOnSave") === false) return;
    clearTimeout(state.timer);
    state.timer = setTimeout(check, 300);               // a save-all is one check, not ten
  }));
  sub(vscode.workspace.onDidChangeConfiguration((e) => {
    if (e.affectsConfiguration("magellanLite.shareOnSave") && cfg().get("shareOnSave") === true) warnShareOnSave();
    if (e.affectsConfiguration("magellanLite.pythonPath")) { interpreter = null; check(); }
    if (e.affectsConfiguration("magellanLite.showLow") && state.report) {   // the last report again, no new check
      show(checks, lite.problems(state.report, "", showLow()));
      sidebar.refresh();
    }
  }));
  sub({ dispose: () => { clearTimeout(state.timer); clearTimeout(state.shareTimer); } });

  const first = folder() ? Promise.all([check(), findRepo()]).then(([r]) => r) : Promise.resolve(null);
  return { check, team, share, showMap, openSuite, findRepo, state, first, warnShareOnSave };
}

/** What the map panel's buttons may ask for. */
const PANEL_COMMANDS = new Set(["magellanLite.check", "magellanLite.team", "magellanLite.openSuite"]);

/** The status bar's background by verdict ("ok" has none). */
const VERDICT_BACKGROUND = { block: "statusBarItem.errorBackground", review: "statusBarItem.warningBackground" };

function deactivate() {}

module.exports = { activate, deactivate };
