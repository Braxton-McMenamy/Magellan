"use strict";
// Which findings are in which dot on the map (media/findings.js): node --test "editors/vscode/test/*.test.js"
const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("../media/findings");

const NODES = [
  { id: "sensor.collector.Collector", path: "sensor/collector.py", line: 3, kind: "class", finding: false },
  { id: "sensor.collector.Collector.collect", path: "sensor/collector.py", line: 7, kind: "method", finding: true },
  { id: "sensor.collector.sweep", path: "sensor/collector.py", line: 17, kind: "function", finding: false },
  { id: "sensor.api.upload", path: "sensor/api.py", line: 4, kind: "function", finding: true },
  { id: "sensor.api.old", path: "sensor/api.py", line: 30, kind: "function", removed: true, finding: false },
];
const FINDINGS = [
  { rule: "debug-leftover", severity: "low", path: "sensor/collector.py", line: 12, message: "print() left in" },
  { rule: "signature-break", severity: "critical", path: "sensor/collector.py", line: 11, message: "collect calls parse_record() the old way" },
  { rule: "bare-except", severity: "medium", path: "sensor/api.py", line: 33, message: "catches everything" },
];
const node = (id) => NODES.find((n) => n.id === id);
const rules = (list) => list.map((f) => f.rule);

test("a dot's findings are the ones in it, worst first", () => {
  assert.deepEqual(rules(F.of(node("sensor.collector.Collector.collect"), FINDINGS, NODES)), ["signature-break", "debug-leftover"]);
  assert.deepEqual(rules(F.of(node("sensor.api.upload"), FINDINGS, NODES)), ["bare-except"]);
});

test("a dot without a finding has none: a class leaves its method's to the method", () => {
  assert.deepEqual(F.of(node("sensor.collector.Collector"), FINDINGS, NODES), []);
  assert.deepEqual(F.of(node("sensor.collector.sweep"), FINDINGS, NODES), []);
  assert.deepEqual(F.of(node("sensor.api.old"), FINDINGS, NODES), []);            // deleted
  assert.deepEqual(F.of({ id: "lib", label: "json.loads" }, FINDINGS, NODES), []);   // no file
  assert.deepEqual(F.of(null, FINDINGS, NODES), []);
});

test("a map without the marks: the definition that starts last before the finding, never a class", () => {
  const plain = NODES.map(({ finding, ...n }) => n);
  assert.deepEqual(rules(F.of(plain[1], FINDINGS, plain)), ["signature-break", "debug-leftover"]);
  assert.deepEqual(F.of(plain[0], FINDINGS, plain), []);
});

test("all the findings, worst first, then by file and line", () => {
  assert.deepEqual(rules(F.sorted(FINDINGS)), ["signature-break", "bare-except", "debug-leftover"]);
  assert.deepEqual(F.sorted(undefined), []);
});
