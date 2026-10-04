"use strict";
// What the extension does with Magellan Lite's JSON, kept free of VS Code so plain Node can
// test it: `node --test "editors/vscode/test/*.test.js"`.

/** Magellan Lite's severities as VS Code's (by name: vscode.DiagnosticSeverity[name]). */
const SEVERITY = { critical: "Error", high: "Error", medium: "Warning", low: "Information" };

const run = (...args) => ["-m", "magellan_lite", ...args];

/** The files Magellan Lite reads (magellan_lite/languages.py; tests/test_extension.py checks
 *  the two lists agree): saving one of them checks the change again. */
const SUFFIXES = [".py", ".java", ".c", ".h", ".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx",
  ".h++", ".ipp", ".tpp", ".f", ".for", ".f77", ".ftn", ".fpp", ".f90", ".f95", ".f03", ".f08",
  ".f18", ".f23", ".F", ".FOR", ".F77", ".FTN", ".FPP", ".F90", ".F95", ".F03", ".F08", ".F18",
  ".F23", ".inc", ".INC", ".fi", ".fh", ".cbl", ".cob", ".cpy", ".cobol", ".dcl", ".CBL", ".COB",
  ".CPY", ".COBOL", ".DCL", ".Cbl", ".Cpy", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"];
const isSource = (file) => SUFFIXES.some((s) => file.endsWith(s));
const checkArgs = (against) =>
  run("check", ".", "--against", against || "git:HEAD", "--format", "json", "--fail-on", "never", "--map");
const teamArgs = () => run("team", ".", "--format", "json", "--fail-on", "never");
const shareArgs = () => run("share", ".");

/**
 * A report's findings as problems, by file:
 * ``{ "pkg/mod.py": [{ line (0-based), severity, message, rule, fix }] }``.
 * `showLow` false leaves out low-severity findings (the magellanLite.showLow setting).
 */
function problems(report, prefix = "", showLow = true) {
  const out = {};
  for (const f of (report && report.findings) || []) {
    if (!showLow && f.severity === "low") continue;
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
    return { error: "This copy of the extension is missing Magellan Lite's engine (its engine/ folder). Reinstall the extension; from the repository, run it with `npm run package` first." };
  }
  return { error: err.trim().split(/\r?\n/).pop() || "Magellan Lite gave no answer" };
}

module.exports = { SEVERITY, SUFFIXES, isSource, checkArgs, teamArgs, shareArgs, problems, teamProblems, statusFor, parseOutput };
