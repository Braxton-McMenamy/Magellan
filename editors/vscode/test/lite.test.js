"use strict";
// node --test "editors/vscode/test/*.test.js"
const test = require("node:test");
const assert = require("node:assert/strict");
const lite = require("../lite");

const REPORT = {
  verdict: "block",
  findings: [
    { rule: "signature-break", severity: "critical", message: "collect calls parse_record() the old way",
      path: "sensor/collector.py", line: 11, fix: "Update the call." },
    { rule: "debug-leftover", severity: "low", message: "print() left in", path: "sensor/api.py", line: 3 },
  ],
};

test("a check asks for JSON with the map, and never fails the process", () => {
  const args = lite.checkArgs("git:HEAD");
  assert.deepEqual(args.slice(0, 3), ["-m", "magellan_lite", "check"]);
  for (const a of ["--format", "json", "--map", "--fail-on", "never"]) assert.ok(args.includes(a), a);
});

test("findings become problems by file, on 0-based lines, with VS Code's severities", () => {
  const p = lite.problems(REPORT);
  assert.deepEqual(Object.keys(p).sort(), ["sensor/api.py", "sensor/collector.py"]);
  assert.deepEqual(p["sensor/collector.py"][0],
    { line: 10, severity: "Error", message: "collect calls parse_record() the old way",
      rule: "signature-break", fix: "Update the call." });
  assert.equal(p["sensor/api.py"][0].severity, "Information");
});

test("team problems say whose work they come from", () => {
  const p = lite.teamProblems([{ name: "braxton", verdict: "block", findings: [REPORT.findings[0]] },
                               { name: "faidh", verdict: "ok", findings: [] }]);
  assert.match(p["sensor/collector.py"][0].message, /^With braxton's work in progress: /);
});

test("the status bar shows the verdict and how many findings", () => {
  assert.equal(lite.statusFor(REPORT).text, "$(error) Magellan Lite: block (2)");
  assert.equal(lite.statusFor({ verdict: "ok", findings: [] }).text, "$(check) Magellan Lite: ok");
  assert.equal(lite.statusFor(null).text, "$(question) Magellan Lite");
});

test("Python's answer: JSON, or a reason a person can act on", () => {
  assert.deepEqual(lite.parseOutput('{"verdict": "ok"}', ""), { data: { verdict: "ok" } });
  assert.match(lite.parseOutput("", "C:\\py\\python.exe: No module named magellan_lite").error, /pip install -e/);
  assert.equal(lite.parseOutput("", "Traceback...\nGitError: not a git repository").error,
    "GitError: not a git repository");
});

test("low findings can be hidden (TODO(faidh) 2)", { skip: "TODO(faidh) 2: the showLow setting" }, () => {
  const p = lite.problems(REPORT, "", false);
  assert.deepEqual(Object.keys(p), ["sensor/collector.py"]);
});
