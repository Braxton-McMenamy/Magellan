"use strict";
// Sub-graphs (site/js/subgraph.js): a dot's or a file's neighbourhood as a map of its own, for the
// Scene, the Team suite and the VS Code map panel. Run: node --test "tests/js/*.test.js"
// (tests/test_site_js.py runs it with the Python tests when Node is installed).
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");

const SG = require(path.join(__dirname, "..", "..", "site", "js", "subgraph.js"));

// A small project, as Lite's maps describe one (magellan_lite/web.py):
//
//     d -> c -> b -> a -> x -> y        (src calls dst: d calls c, ... x calls y)
//     a2 (beside a in pkg/one.py) reads k; z is on its own
const node = (id, file, extra = {}) => ({
  id, label: id.split(".").pop(), path: file, line: 1, kind: "function", lang: "python",
  change: "", removed: false, score: 0, hops: 0, finding: false, unused: false, ...extra,
});
const edge = (src, dst, kind = "calls") => ({ src, dst, kind, guess: false });
function project() {
  return {
    nodes: [
      node("pkg.one.a", "pkg/one.py", { line: 3, change: "body" }),
      node("pkg.one.a2", "pkg/one.py", { line: 9 }),
      node("pkg.two.b", "pkg/two.py", { score: 0.9, hops: 1, by: ["alice"] }),
      node("pkg.two.c", "pkg/two.py", { line: 7 }),
      node("app.d", "app.py"),
      node("lib.x", "lib.py"),
      node("lib.y", "lib.py", { line: 5 }),
      node("lib.k", "lib.py", { kind: "constant", line: 2 }),
      node("app.z", "app.py", { line: 20, unused: true }),
    ],
    edges: [
      edge("app.d", "pkg.two.c"), edge("pkg.two.c", "pkg.two.b"), edge("pkg.two.b", "pkg.one.a"),
      edge("pkg.one.a", "lib.x"), edge("lib.x", "lib.y"), edge("pkg.one.a2", "lib.k", "reads"),
    ],
  };
}
const ids = (sub) => sub.nodes.map((n) => n.id).sort();

test("what depends on a dot goes back `in` hops, what it uses goes ahead `out` hops", () => {
  const map = project();
  assert.deepEqual(ids(SG.of(map, ["pkg.one.a"], { in: 2, out: 1 })),
    ["lib.x", "pkg.one.a", "pkg.two.b", "pkg.two.c"]);
  assert.deepEqual(ids(SG.of(map, ["pkg.one.a"], { in: 1, out: 2 })),
    ["lib.x", "lib.y", "pkg.one.a", "pkg.two.b"]);
  assert.deepEqual(ids(SG.of(map, ["pkg.one.a"], { in: 0, out: 0 })), ["pkg.one.a"]);
  // the default is two hops back and one ahead: what breaks, and what it leans on
  assert.deepEqual(ids(SG.of(map, ["pkg.one.a"])), ids(SG.of(map, ["pkg.one.a"], { in: 2, out: 1 })));
  // and "back" never turns around: the uses' own callers (a2 is a's neighbour, not its caller) stay out
  const sub = SG.of(map, ["pkg.one.a"], { in: 5, out: 5 });
  assert.ok(!ids(sub).includes("pkg.one.a2") && !ids(sub).includes("app.z"));
  assert.equal(sub.dependents, 3);
  assert.equal(sub.uses, 2);
});

test("seeds are marked, and every other field is kept as it was", () => {
  const map = project();
  const before = JSON.stringify(map);
  const sub = SG.of(map, ["pkg.one.a"]);
  const a = sub.nodes.find((n) => n.id === "pkg.one.a");
  const b = sub.nodes.find((n) => n.id === "pkg.two.b");
  assert.equal(a.seed, true);
  assert.equal(b.seed, undefined);
  assert.deepEqual(sub.seeds, ["pkg.one.a"]);
  const { seed, ...rest } = a;
  assert.deepEqual(rest, map.nodes[0]);                 // the change, the score, who: all still there
  assert.deepEqual(b, map.nodes[2]);
  assert.equal(JSON.stringify(map), before, "the map it came from is untouched");
  // a sub-graph of a sub-graph carries only its own seed
  const again = SG.of(sub, ["pkg.two.b"]);
  assert.deepEqual(again.nodes.filter((n) => n.seed).map((n) => n.id), ["pkg.two.b"]);
});

test("the edges are only those among the nodes kept", () => {
  const sub = SG.of(project(), ["pkg.one.a"], { in: 1, out: 1 });
  const kept = new Set(ids(sub));
  for (const e of sub.edges) assert.ok(kept.has(e.src) && kept.has(e.dst), `${e.src} -> ${e.dst}`);
  assert.deepEqual(sub.edges.map((e) => `${e.src}>${e.dst}`).sort(), ["pkg.one.a>lib.x", "pkg.two.b>pkg.one.a"]);
});

test("a file's sub-graph is everything in the file, and what calls, reads or is used by it", () => {
  const map = project();
  const sub = SG.ofFile(map, "pkg/one.py");
  assert.equal(sub.file, "pkg/one.py");
  assert.deepEqual([...sub.seeds].sort(), ["pkg.one.a", "pkg.one.a2"]);
  assert.deepEqual(ids(sub), ["lib.k", "lib.x", "pkg.one.a", "pkg.one.a2", "pkg.two.b"]);   // one hop either way
  assert.equal(SG.title(sub), "pkg/one.py · 5");
  assert.match(SG.describe(sub), /^The 2 definitions in pkg\/one\.py: 1 definition calls or reads them; they call or read 2 more\.$/);
  assert.deepEqual(ids(SG.build(map, { file: "pkg/one.py" })), ids(sub));
});

test("tab names and the line under them say what the sub-graph is of", () => {
  const sub = SG.of(project(), ["pkg.one.a"]);
  assert.equal(SG.title(sub), "a and its neighbourhood · 4");
  assert.equal(SG.describe(sub), "a: 2 definitions depend on it within 2 hops; it uses 1.");
  const alone = SG.of(project(), ["app.z"]);
  assert.equal(SG.describe(alone), "z: nothing depends on it; it uses nothing else.");
});

test("past the cap only the nearest are kept, and it says so", () => {
  // a hub that 250 definitions call, each of them called by one more: 501 within two hops
  const nodes = [node("hub", "hub.py")], edges = [];
  for (let i = 0; i < 250; i++) {
    const near = `near${String(i).padStart(3, "0")}`, far = `far${String(i).padStart(3, "0")}`;
    nodes.push(node(near, "near.py"), node(far, "far.py"));
    edges.push(edge(near, "hub"), edge(far, near));
  }
  const sub = SG.of({ nodes, edges }, ["hub"]);
  assert.equal(SG.CAP, 300);
  assert.equal(sub.nodes.length, 300);
  assert.equal(sub.total, 501);
  assert.equal(sub.capped, true);
  assert.ok(sub.nodes.some((n) => n.id === "hub" && n.seed), "the seed always stays");
  assert.equal(sub.nodes.filter((n) => n.id.startsWith("near")).length, 250, "one hop away before two");
  const kept = new Set(ids(sub));
  for (const e of sub.edges) assert.ok(kept.has(e.src) && kept.has(e.dst));
  assert.equal(SG.title(sub), "hub and its neighbourhood · 300 of 501");
  assert.match(SG.describe(sub), /Only the nearest 300 of 501 are shown\.$/);
  assert.equal(SG.of({ nodes, edges }, ["hub"], { cap: 10 }).nodes.length, 10);
  assert.equal(SG.of({ nodes, edges }, ["hub"], { in: 1 }).capped, false);
});

test("an unknown seed gives an empty map, without throwing", () => {
  const map = project();
  for (const sub of [SG.of(map, ["nope"]), SG.of(map, []), SG.of(map, null), SG.of(null, ["pkg.one.a"]),
    SG.of({}, ["pkg.one.a"]), SG.ofFile(map, "missing.py"), SG.ofFile(map, ""), SG.build(map, null)]) {
    assert.deepEqual(sub.nodes, []);
    assert.deepEqual(sub.edges, []);
    assert.deepEqual(sub.seeds, []);
    assert.equal(sub.capped, false);
    assert.equal(SG.title(sub), "Nothing here");
    assert.equal(typeof SG.describe(sub), "string");
    assert.deepEqual(SG.flow(sub).nodes, []);
  }
  // a known seed beside an unknown one: the unknown one is ignored
  assert.deepEqual(SG.of(map, ["nope", "app.z"]).seeds, ["app.z"]);
});

test("a cycle is walked once", () => {
  const map = { nodes: [node("m.p", "m.py"), node("m.q", "m.py")],
    edges: [edge("m.p", "m.q"), edge("m.q", "m.p"), edge("m.p", "m.p")] };
  const sub = SG.of(map, ["m.p"], { in: 9, out: 9 });
  assert.deepEqual(ids(sub), ["m.p", "m.q"]);
  assert.equal(sub.edges.length, 3);                      // kept as they were, the self-call too
  assert.equal(sub.dependents, 1);
  assert.equal(sub.uses, 0);                              // q is counted once, as a dependent
});

test("flow puts the seed where the change goes and its dependents hop by hop", () => {
  const flow = SG.flow(SG.of(project(), ["pkg.one.a"], { in: 2, out: 1 }));
  const by = Object.fromEntries(flow.nodes.map((n) => [n.id, n]));
  assert.equal(by["pkg.one.a"].change, "body");           // a real change keeps its name
  assert.equal(SG.flow(SG.of(project(), ["pkg.two.c"])).nodes.find((n) => n.seed).change, "picked");
  assert.deepEqual([by["pkg.two.b"].hops, by["pkg.two.b"].score, by["pkg.two.b"].change], [1, 0.9, ""]);
  assert.deepEqual([by["pkg.two.c"].hops, by["pkg.two.c"].score], [2, 0.81]);
  assert.deepEqual([by["lib.x"].hops, by["lib.x"].score], [0, 0]);   // what it uses: the left column
  assert.deepEqual(by["pkg.two.b"].by, ["alice"]);         // whose it is still shows
  assert.deepEqual(SG.heads(SG.ofFile(project(), "pkg/one.py")), { 0: "the file" });
  assert.deepEqual(SG.heads(SG.of(project(), ["pkg.one.a"])), { 0: "picked" });
});

test("what a page keeps of a sub-graph draws it again from a newer map", () => {
  const map = project();
  assert.deepEqual(ids(SG.build(map, { ids: ["pkg.one.a"] })), ids(SG.of(map, ["pkg.one.a"])));
  assert.deepEqual(ids(SG.build(map, { ids: ["pkg.one.a"], hops: { in: 1, out: 0 } })), ["pkg.one.a", "pkg.two.b"]);
  assert.ok(SG.same({ ids: ["pkg.one.a"] }, { ids: ["pkg.one.a"] }));
  assert.ok(SG.same({ file: "pkg/one.py" }, { file: "pkg/one.py" }));
  assert.ok(!SG.same({ ids: ["pkg.one.a"] }, { file: "pkg/one.py" }));
  assert.ok(!SG.same({ ids: ["pkg.one.a"] }, null));
  // the dot is gone from the newer map: nothing, not an error
  const newer = { nodes: map.nodes.filter((n) => n.id !== "pkg.one.a"), edges: map.edges };
  assert.deepEqual(SG.build(newer, { ids: ["pkg.one.a"] }).nodes, []);
});
