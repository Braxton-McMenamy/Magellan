"use strict";
// Finding a Python and the engine without anyone configuring them (python.js).
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const py = require("../python");

/** A fake execFile: each interpreter (by its command line) answers its version, or fails. */
function interpreters(versions) {
  const asked = [];
  const run = (exe, args, opts, cb) => {
    const key = [exe, ...args.slice(0, args.indexOf("-c"))].join(" ");
    asked.push(key);
    const v = versions[key];
    setImmediate(() => (v ? cb(null, `${v}\n`, "") : cb(Object.assign(new Error("not found"), { code: "ENOENT" }), "", "")));
  };
  return { run, asked };
}

test("where it looks: a set path, the Python extension's choice, then the usual names", () => {
  const names = (c) => c.map((x) => [x.exe, ...x.pre].join(" "));
  assert.deepEqual(names(py.candidates({ setting: "", selected: null, platform: "win32" })),
    ["py -3", "python3", "python"]);
  assert.deepEqual(names(py.candidates({ setting: "", selected: null, platform: "linux" })), ["python3", "python"]);
  assert.deepEqual(names(py.candidates({ setting: "/opt/py/bin/python", selected: "/venv/bin/python", platform: "linux" })),
    ["/opt/py/bin/python", "/venv/bin/python", "python3", "python"]);
  // "python" was the old setting's default: it is not a choice someone made
  assert.deepEqual(names(py.candidates({ setting: "python", selected: null, platform: "linux" })), ["python3", "python"]);
});

test("the first Python 3.10 or newer wins; older ones and missing ones are passed over", async () => {
  const { run, asked } = interpreters({ "py -3": "3 9", python3: "3 12" });
  const found = await py.find(py.candidates({ platform: "win32" }), run, ".");
  assert.deepEqual([found.exe, found.pre, found.version], ["python3", [], "3.12"]);
  assert.deepEqual(asked, ["py -3", "python3"]);
});

test("with no usable Python, the reason says what was found", async () => {
  const old = await py.find(py.candidates({ platform: "linux" }), interpreters({ python3: "3 8" }).run, ".");
  assert.match(old.error, /needs Python 3\.10 or newer; python3 is Python 3\.8/);
  const none = await py.find(py.candidates({ platform: "linux" }), interpreters({}).run, ".");
  assert.match(none.error, /none was found/);
});

test("the engine: bundled in engine/, else this repository's magellan_lite", () => {
  const ext = path.resolve("/x/ext");
  const has = (dirs) => (p) => dirs.some((d) => p === path.join(d, "magellan_lite", "__init__.py"));
  assert.equal(py.engineDir(ext, has([path.join(ext, "engine")])), path.join(ext, "engine"));
  assert.equal(py.engineDir(ext, has([path.resolve(ext, "..", "..")])), path.resolve(ext, "..", ".."));
  assert.equal(py.engineDir(ext, has([])), null);
});

test("the engine goes first on PYTHONPATH, and output is UTF-8", () => {
  const env = py.environment("/eng", { PYTHONPATH: "/theirs", PATH: "/bin" });
  assert.equal(env.PYTHONPATH, ["/eng", "/theirs"].join(path.delimiter));
  assert.equal(env.PYTHONIOENCODING, "utf-8");
  assert.equal(env.PATH, "/bin");
  assert.equal(py.environment(null, {}).PYTHONPATH, undefined);
});

test("versions are read strictly", () => {
  assert.deepEqual(py.parseVersion("3 12\n"), [3, 12]);
  assert.equal(py.parseVersion("Python was not found; run without arguments to install from the Microsoft Store"), null);
});

test("the Python extension's selected interpreter is used when it says, and ignored when it can't", async () => {
  const fake = (api) => ({ extensions: { getExtension: () => ({ isActive: true, exports: api }) } });
  assert.equal(await py.selectedInterpreter(fake({ environments: { getActiveEnvironmentPath: () => ({ path: "/venv/bin/python" }) } })),
    "/venv/bin/python");
  assert.equal(await py.selectedInterpreter(fake({ settings: { getExecutionDetails: () => ({ execCommand: ["/usr/bin/python3.12"] }) } })),
    "/usr/bin/python3.12");
  assert.equal(await py.selectedInterpreter(fake({ environments: { getActiveEnvironmentPath: () => { throw new Error("starting"); } } })), null);
  assert.equal(await py.selectedInterpreter({}), null);
});
