"use strict";
// Following the code (follow.js): node --test "editors/vscode/test/*.test.js"
const test = require("node:test");
const assert = require("node:assert/strict");
const { definitionAt, endOf, follower } = require("../follow");

const PY = [
  "import os",                            // 1
  "",                                     // 2
  "class Channel:",                       // 3
  "    rate = 4",                         // 4
  "",                                     // 5
  "    def parse(self, rows):",           // 6
  "        out = []",                     // 7
  "# a note at the margin, still inside", // 8
  "        return out",                   // 9
  "",                                     // 10
  "    def close(self):",                 // 11
  "        pass",                         // 12
  "",                                     // 13
  "LIMIT = {",                            // 14
  "    'a': 1,",                          // 15
  "}",                                    // 16
  "print(LIMIT)",                         // 17
];
const NODES = [
  { id: "sensor.channel.Channel", path: "sensor/channel.py", line: 3, kind: "class" },
  { id: "sensor.channel.Channel.rate", path: "sensor/channel.py", line: 4, kind: "constant" },
  { id: "sensor.channel.Channel.parse", path: "sensor/channel.py", line: 6, kind: "method" },
  { id: "sensor.channel.Channel.close", path: "sensor/channel.py", line: 11, kind: "method" },
  { id: "sensor.channel.LIMIT", path: "sensor/channel.py", line: 14, kind: "constant" },
  { id: "sensor.api.upload", path: "sensor/api.py", line: 6, kind: "function" },
  { id: "sensor.channel.gone", path: "sensor/channel.py", line: 9, kind: "function", removed: true },
];
const at = (line, lines = PY, nodes = NODES) => {
  const n = definitionAt(nodes, "sensor/channel.py", line, lines);
  return n && n.id.split(".").slice(2).join(".");
};

test("the cursor is in the innermost definition whose lines hold it", () => {
  assert.equal(at(7), "Channel.parse");
  assert.equal(at(6), "Channel.parse");                  // on its own first line
  assert.equal(at(9), "Channel.parse");                  // past a comment at the margin
  assert.equal(at(12), "Channel.close");
  assert.equal(at(4), "Channel.rate");
  assert.equal(at(5), "Channel");                        // in the class, between its members
  assert.equal(at(15), "LIMIT");                         // a constant over three lines...
  assert.equal(at(16), "LIMIT");                         // ...its closing brace included
});

test("outside every definition the map knows, it is in none", () => {
  assert.equal(at(1), null);                             // an import above them all
  assert.equal(at(13), null);                            // a blank line between two
  assert.equal(at(17), null);                            // a statement after them
  assert.equal(definitionAt(NODES, "sensor/other.py", 7, PY), null);
  assert.equal(definitionAt([], "sensor/channel.py", 7, PY), null);
  assert.equal(definitionAt(undefined, "sensor/channel.py", 7, PY), null);
});

test("a definition the change deleted is not in the file any more", () => {
  const nodes = NODES.filter((n) => n.id !== "sensor.channel.Channel.parse");
  assert.equal(at(9, PY, nodes), "Channel");             // not "gone", though it starts closer
});

test("brace languages: the body's brace on a line of its own, and the closing brace", () => {
  const c = ["static int", "parse(char *s)", "{", "    return 0;", "}", "", "int main(void) {", "    return parse(0);", "}"];
  assert.equal(endOf(c, 2), 5);
  assert.equal(endOf(c, 7), 9);
  const nodes = [{ id: "parse", path: "a.c", line: 2, lang: "c" }, { id: "main", path: "a.c", line: 7, lang: "c" }];
  assert.equal(definitionAt(nodes, "a.c", 4, c).id, "parse");
  assert.equal(definitionAt(nodes, "a.c", 6, c), null);
  assert.equal(definitionAt(nodes, "a.c", 8, c).id, "main");
});

test("where indentation says nothing (fixed-form Fortran), a definition runs to the next", () => {
  const f = ["      SUBROUTINE A", "      X = 1", "      END", "      SUBROUTINE B", "      Y = 2", "      END"];
  const nodes = [{ id: "a", path: "a.f", line: 1, lang: "fortran" }, { id: "b", path: "a.f", line: 4, lang: "fortran" }];
  assert.equal(definitionAt(nodes, "a.f", 2, f).id, "a");
  assert.equal(definitionAt(nodes, "a.f", 3, f).id, "a");
  assert.equal(definitionAt(nodes, "a.f", 6, f).id, "b");
});

test("an end line on the map wins; with no text, the next definition bounds each one", () => {
  const nodes = [{ id: "a", path: "m.py", line: 1, end_line: 3 }, { id: "b", path: "m.py", line: 10 }];
  assert.equal(definitionAt(nodes, "m.py", 3).id, "a");
  assert.equal(definitionAt(nodes, "m.py", 5), null);
  assert.equal(definitionAt(nodes, "m.py", 40).id, "b");
});

/** Timers by hand: run() fires what is due. */
function clock() {
  let t = 0, due = [];
  return {
    now: () => t,
    timers: {
      set: (fn, ms) => { const x = { fn, at: t + ms }; due.push(x); return x; },
      clear: (x) => { due = due.filter((d) => d !== x); },
    },
    tick(ms) {
      t += ms;
      for (const d of due.filter((x) => x.at <= t)) { due = due.filter((x) => x !== d); d.fn(); }
    },
  };
}
const where = (path, line) => ({ path, line, lines: () => PY });
const makeFollower = () => {
  const c = clock(), sent = [];
  const f = follower({ delay: 250, quiet: 1500, now: c.now, timers: c.timers, send: (n, extra) => sent.push([n.id, extra]),
    find: (w) => definitionAt(NODES, w.path, w.line, w.lines()) });
  return { c, f, sent, ids: () => sent.map(([id]) => id.split(".").pop()) };
};

test("the panel hears where the cursor rests, once it rests, and only when it moves to another definition", () => {
  const { c, f, ids } = makeFollower();
  f.cursor(where("sensor/channel.py", 7));
  c.tick(100);
  f.cursor(where("sensor/channel.py", 12));               // still moving
  c.tick(100);
  assert.deepEqual(ids(), []);
  c.tick(200);
  assert.deepEqual(ids(), ["close"]);                     // the last place, once
  f.cursor(where("sensor/channel.py", 11));               // the same definition: nothing new
  c.tick(300);
  f.cursor(where("sensor/channel.py", 1));                // outside them all: the map stays
  c.tick(300);
  assert.deepEqual(ids(), ["close"]);
  f.cursor(where("sensor/channel.py", 9));
  c.tick(300);
  assert.deepEqual(ids(), ["close", "parse"]);
});

test("the cursor landing where the panel opened code does not move the map; the next click does", () => {
  const { c, f, ids } = makeFollower();
  f.cursor(where("sensor/channel.py", 12));
  c.tick(300);
  f.opened("sensor/channel.py", 6);                        // a click on parse, on the map
  f.cursor(where("sensor/channel.py", 6));
  c.tick(300);
  assert.deepEqual(ids(), ["close"]);                     // stayed on close's neighbourhood
  f.cursor(where("sensor/channel.py", 7));                // then a click in parse, in the code
  c.tick(300);
  assert.deepEqual(ids(), ["close", "parse"]);
});

test("the open is only excused once, and not for long", () => {
  const { c, f, ids } = makeFollower();
  f.opened("sensor/channel.py", 6);
  c.tick(2000);                                           // the editor never went there
  f.cursor(where("sensor/channel.py", 6));
  c.tick(300);
  assert.deepEqual(ids(), ["parse"]);
  const g = makeFollower();
  g.f.opened("sensor/channel.py", 12);
  g.f.cursor(where("sensor/channel.py", 7));              // went somewhere else instead
  g.c.tick(300);
  g.f.cursor(where("sensor/channel.py", 12));
  g.c.tick(300);
  assert.deepEqual(g.ids(), ["parse", "close"]);
});

test("now() tells at once, with what the caller adds; reset() forgets what was told", () => {
  const { f, sent } = makeFollower();
  f.now(where("sensor/channel.py", 7), { quiet: true });
  f.now(where("sensor/channel.py", 7));
  assert.deepEqual(sent, [["sensor.channel.Channel.parse", { quiet: true }]]);
  f.reset();
  f.now(where("sensor/channel.py", 7));
  assert.equal(sent.length, 2);
  assert.equal(f.now(null), null);
});
