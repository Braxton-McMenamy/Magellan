"use strict";
// What the extension does with Magellan Lite's JSON, kept free of VS Code so plain Node can
// test it: `node --test "editors/vscode/test/*.test.js"`.

/** Magellan Lite's severities as VS Code's (by name: vscode.DiagnosticSeverity[name]). */
const SEVERITY = { critical: "Error", high: "Error", medium: "Warning", low: "Information" };

const run = (...args) => ["-m", "magellan_lite", ...args];
const checkArgs = (against) =>
  run("check", ".", "--against", against || "git:HEAD", "--format", "json", "--fail-on", "never", "--map");
const teamArgs = () => run("team", ".", "--format", "json", "--fail-on", "never");
const shareArgs = () => run("share", ".");

/**
 * A report's findings as problems, by file:
 * ``{ "pkg/mod.py": [{ line (0-based), severity, message, rule, fix }] }``.
 */
function problems(report, prefix = "") {
  const out = {};
  for (const f of (report && report.findings) || []) {
    // TODO(faidh) 2: a setting to hide low-severity findings.
    //   1. In package.json, under "contributes" > "configuration" > "properties", add
    //      "magellanLite.showLow": { "type": "boolean", "default": true,
    //      "description": "Show low-severity findings (print() left in, == None, ...)." }
    //   2. Give this function a third parameter, `showLow = true`, and skip a finding here
    //      when `!showLow && f.severity === "low"` (use `continue`).
    //   3. In extension.js, find `lite.problems(` and pass `cfg().get("showLow") !== false`.
    //   4. In test/lite.test.js, delete `{ skip: ... }` from the "low findings can be hidden"
    //      test. Done when `node --test "editors/vscode/test/*.test.js"` passes.
    (out[f.path] = out[f.path] || []).push({
      line: Math.max(0, (f.line || 1) - 1),
      severity: SEVERITY[f.severity] || "Warning",
      message: prefix + f.message,
      rule: f.rule,
      fix: f.fix || "",
    });
  }
  return out;
}

/** `magellan-lite team` results as problems, each saying whose work it comes from. */
function teamProblems(results) {
  const out = {};
  for (const r of results || []) {
    const each = problems({ findings: r.findings }, `With ${r.name}'s work in progress: `);
    for (const [file, list] of Object.entries(each)) (out[file] = out[file] || []).push(...list);
  }
  return out;
}

const VERDICT = {
  block: { text: "$(error) Magellan Lite: block", tip: "This change breaks something: see the Problems panel" },
  review: { text: "$(warning) Magellan Lite: review", tip: "Worth a look before you commit: see the Problems panel" },
  ok: { text: "$(check) Magellan Lite: ok", tip: "Nothing to check in what this change touched" },
};

/** The status bar's text and tooltip for a report. */
function statusFor(report) {
  const v = VERDICT[report && report.verdict];
  if (!v) return { text: "$(question) Magellan Lite", tip: "No verdict yet" };
  const n = ((report && report.findings) || []).length;
  const tip = report.verdict === "ok" && n ? "Only low-severity notes: see the Problems panel" : v.tip;
  return { text: v.text + (n ? ` (${n})` : ""), tip: tip + " · click for the map" };
}

/** Python's output as JSON, or a reason a person can act on. */
function parseOutput(stdout, stderr) {
  try {
    return { data: JSON.parse(stdout) };
  } catch (e) { /* not JSON: say why below */ }
  const err = String(stderr || "");
  if (/No module named .?magellan_lite/.test(err)) {
    return { error: "Magellan Lite is not installed for this Python. Run `pip install -e <the Magellan Lite repo>`, or point the magellanLite.pythonPath setting at a Python that has it." };
  }
  return { error: err.trim().split(/\r?\n/).pop() || "Magellan Lite gave no answer" };
}

module.exports = { SEVERITY, checkArgs, teamArgs, shareArgs, problems, teamProblems, statusFor, parseOutput };
