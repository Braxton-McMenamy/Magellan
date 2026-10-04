"use strict";
// The Team suite's GitHub reader (site/js/github.js), against a fake GitHub: what it accepts,
// where the token goes, and how little it asks for. Run: node --test "tests/js/*.test.js"
// (tests/test_site_js.py runs it with the Python tests when Node is installed).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "..", "..", "site", "js", "github.js"), "utf8");

/** github.js in a fresh page, with `fetch` answered by `answer(url, init)`; records each call. */
function load(answer) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push({ url, headers: init.headers || {}, init });
    const [status, body] = answer(url, init) || [404, { message: "Not Found" }];
    const text = typeof body === "string" ? body : JSON.stringify(body);
    return {
      ok: status >= 200 && status < 300, status,
      headers: { get: (k) => ({ "x-ratelimit-remaining": "59", "x-ratelimit-limit": "60", "x-ratelimit-reset": "2000000000" })[k.toLowerCase()] ?? null },
      json: async () => JSON.parse(text), text: async () => text,
    };
  };
  const window = {};
  vm.runInNewContext(SOURCE, { window, fetch, atob, TextDecoder, Uint8Array, Date, Promise, Map, Array, Object, Number, JSON, Math, encodeURIComponent });
  return { gh: window.MagellanGitHub, calls };
}

// a public repository: main has two files; alice's shared work adds one
const MAIN = "a".repeat(40), ALICE = "b".repeat(40);
const API = "https://api.github.com/repos/team/demo";
function github(url) {
  if (url === API) return [200, { default_branch: "main", private: false, full_name: "team/demo" }];
  if (url === `${API}/git/matching-refs/wip`) return [200, [{ ref: "refs/wip/alice", object: { sha: ALICE, type: "commit" } }]];
  if (url === `${API}/git/ref/heads/main`) return [200, { object: { sha: MAIN } }];
  if (url === `${API}/git/trees/${MAIN}?recursive=1`) {
    return [200, { truncated: false, tree: [
      { path: "app/m.py", type: "blob", sha: "1".repeat(40), size: 20 },
      { path: "app/u.py", type: "blob", sha: "2".repeat(40), size: 20 },
      { path: "README.md", type: "blob", sha: "3".repeat(40), size: 5 },
      { path: ".venv/lib/x.py", type: "blob", sha: "4".repeat(40), size: 5 },
    ] }];
  }
  if (url === `${API}/compare/${MAIN}...${ALICE}`) {
    return [200, { files: [{ filename: "app/new.py", status: "added", sha: "5".repeat(40) }],
                   commits: [{ commit: { committer: { date: "2026-10-04T12:00:00Z" } } }] }];
  }
  const raw = url.match(/^https:\/\/raw\.githubusercontent\.com\/team\/demo\/([0-9a-f]{40})\/(.+)$/);
  if (raw) return [200, { "app/m.py": "def f(x):\n    return x\n", "app/u.py": "from app.m import f\n", "app/new.py": "x = 1\n" }[raw[2]]];
  return null;
}

test("repositories are taken as owner/name or their GitHub address, and nothing else", () => {
  const { gh } = load(() => null);
  const ok = (s) => { const r = gh.parseRepo(s); return r && `${r.owner}/${r.repo}`; };
  assert.equal(ok("Braxton-McMenamy/Magellan"), "Braxton-McMenamy/Magellan");
  assert.equal(ok("https://github.com/Braxton-McMenamy/Magellan.git"), "Braxton-McMenamy/Magellan");
  assert.equal(ok(" https://www.github.com/a/b/ "), "a/b");
  for (const bad of ["", "a", "a/b/c", "../x", "a/..", "https://evil.example/a/b", "a/b?x=1",
                     "a b/c", "javascript:alert(1)//a/b", "-a/b", "a/b#c"]) {
    assert.equal(gh.parseRepo(bad), null, bad);
  }
});

test("a token is checked for shape before it goes anywhere", () => {
  const { gh } = load(() => null);
  assert.ok(gh.tokenOk("github_pat_" + "A1".repeat(20)));
  assert.ok(!gh.tokenOk("abc"));
  assert.ok(!gh.tokenOk("ghp_x\r\nX-Evil: 1" + "a".repeat(30)));
  assert.throws(() => new gh.Repo({ owner: "a", repo: "b" }, "not a token!"), /doesn't look like/);
});

test("the project's files are the engine's: Python and the other languages, outside tool and environment folders", () => {
  const { gh } = load(() => null);
  for (const p of ["pkg/mod.py", "src/parse.c", "include/parse.h", "geo/Shape.java", "lib/solve.f90",
                   "old/MAIN.F", "batch/PAYROLL.CBL", "copy/REC.cpy", "web/app.ts", "web/x.min.js",
                   "core/a.cpp", "core/a.hpp", ".eslintrc.js"]) {
    assert.ok(gh.projectFile(p), p);
  }
  for (const p of ["README.md", "Makefile", "data.json", "notes.txt", "a.pyc", "a.C++x", "c",
                   ".venv/x.py", "a/__pycache__/m.py", "node_modules/x.js", "a/.git/x.c",
                   "build/gen.c", "dist/bundle.js", "x.py/README", "dir.c/notes"]) {
    assert.ok(!gh.projectFile(p), p);
  }
  assert.ok(gh.SUFFIXES.has(".java") && gh.SUFFIXES.has(".cbl") && !gh.SUFFIXES.has(".py"));
});

test("another language's file too big for the browser is left out and listed; the rest is read", async () => {
  const big = "x".repeat(600_000);
  const { gh, calls } = load((url) => {
    if (url === `${API}/git/trees/${MAIN}?recursive=1`) {
      return [200, { truncated: false, tree: [
        { path: "app/m.py", type: "blob", sha: "1".repeat(40), size: 20 },
        { path: "app/u.py", type: "blob", sha: "2".repeat(40), size: 20 },
        { path: "site/data/bundle.js", type: "blob", sha: "6".repeat(40), size: big.length },
        { path: "native/parse.c", type: "blob", sha: "7".repeat(40), size: 30 },
      ] }];
    }
    if (url.endsWith(`/${MAIN}/native/parse.c`)) return [200, "int parse(int x) { return x; }\n"];
    if (url.endsWith(`/${ALICE}/site/data/bundle.js`)) return [200, big];
    if (url === `${API}/compare/${MAIN}...${ALICE}`) {
      return [200, { files: [{ filename: "app/new.py", status: "added", sha: "5".repeat(40) },
                             { filename: "site/data/bundle.js", status: "modified", sha: "8".repeat(40) }],
                     commits: [{ commit: { committer: { date: "2026-10-04T12:00:00Z" } } }] }];
    }
    return github(url);
  });
  const got = await new gh.Repo({ owner: "team", repo: "demo" }, "").refresh(true);
  assert.deepEqual(Object.keys(got.base).sort(), ["app/m.py", "app/u.py", "native/parse.c"]);
  assert.deepEqual(Object.keys(got.works.alice).sort(), ["app/m.py", "app/new.py", "app/u.py", "native/parse.c"]);
  assert.deepEqual(JSON.parse(JSON.stringify(got.skipped)), ["site/data/bundle.js"]);
  assert.ok(!calls.some((c) => c.url.endsWith(`/${MAIN}/site/data/bundle.js`)), "never downloaded from the base");

  // a Python file that size still stops the check: past it, the command line is the tool
  const { gh: gh2 } = load((url) => {
    if (url === `${API}/compare/${MAIN}...${ALICE}`) {
      return [200, { files: [{ filename: "app/huge.py", status: "added", sha: "9".repeat(40) }], commits: [] }];
    }
    if (url.endsWith(`/${ALICE}/app/huge.py`)) return [200, big];
    return github(url);
  });
  await assert.rejects(new gh2.Repo({ owner: "team", repo: "demo" }, "").refresh(true), /too big for the browser/);
});

test("reading the team: the base, each person's work, and a quiet look costs one request", async () => {
  const { gh, calls } = load(github);
  const repo = new gh.Repo({ owner: "team", repo: "demo" }, "");
  const got = await repo.refresh(true);
  assert.deepEqual(Object.keys(got.base).sort(), ["app/m.py", "app/u.py"]);
  assert.deepEqual(Object.keys(got.works.alice).sort(), ["app/m.py", "app/new.py", "app/u.py"]);
  assert.deepEqual(JSON.parse(JSON.stringify(got.people.map((p) => [p.name, p.files]))), [["alice", ["app/new.py"]]]);   // from the page's realm
  const apiCalls = calls.filter((c) => c.url.startsWith("https://api.github.com/")).length;

  assert.equal(await repo.refresh(), null);                              // nothing moved
  assert.equal(calls.filter((c) => c.url.startsWith("https://api.github.com/")).length, apiCalls + 1);
  assert.equal(calls.filter((c) => c.url.includes("raw.githubusercontent.com")).length, 3);   // each file once
});

test("the token goes to api.github.com and nowhere else, and no request carries cookies", async () => {
  const token = "github_pat_" + "Z9".repeat(20);
  const { gh, calls } = load(github);
  await new gh.Repo({ owner: "team", repo: "demo" }, token).refresh(true);
  for (const c of calls) {
    const auth = c.headers.Authorization;
    if (c.url.startsWith("https://api.github.com/")) assert.equal(auth, `Bearer ${token}`);
    else assert.equal(auth, undefined, c.url);
    assert.ok(!c.url.includes(token), "never in a URL");
    assert.equal(c.init.credentials, "omit");
  }
});

test("GitHub's answers become messages a person can act on", async () => {
  const { gh } = load((url) => (url.endsWith("/demo") ? [404, { message: "Not Found" }] : null));
  await assert.rejects(new gh.Repo({ owner: "team", repo: "demo" }).refresh(true), /If it's private, add a token/);
});
