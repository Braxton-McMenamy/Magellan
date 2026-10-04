// Magellan Lite's engine (data/engine.js) on Pyodide, in a worker: a team check runs several
// whole-project checks, and here they don't freeze the page. js/teamfeed.js talks to it:
//   -> { id, op: "team", base: "{json}", works: "{json}" }
//   <- { id, ok: true, result } | { id, ok: false, error } | { status: "Loading Python…" }
//
// data/engine.js reads Python. The other languages' frontends (C, Java, Fortran, COBOL...)
// are data/polyglot.js, over a megabyte more: loaded the first time a repository has files in
// those languages, before Python looks at them, and kept for the next checks. (C++ and
// TypeScript need libclang and Node.js, which a browser doesn't have: the report says so.)

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/";

// a bundle's files into Python's file system, under /engine (on sys.path)
function write(py, files) {
  for (const [path, source] of Object.entries(files)) {
    py.FS.mkdirTree(`/engine/${path.split("/").slice(0, -1).join("/")}`);
    py.FS.writeFile(`/engine/${path}`, source);
  }
}

let ready = null;           // a promise of { py, team, others }
function engine() {
  if (ready) return ready;
  ready = (async () => {
    postMessage({ status: "Loading Python (a few seconds, the first time)…" });
    importScripts(`${PYODIDE}pyodide.js`, "../data/engine.js");
    const py = await self.loadPyodide({ indexURL: PYODIDE });
    write(py, self.MAGELLAN_ENGINE.files);
    py.runPython([
      "import json, sys",
      "sys.path.insert(0, '/engine')",
      "from magellan_lite.languages import language",
      "from magellan_lite.web import team_live",
      "def _team(base, works):",
      "    return json.dumps(team_live(json.loads(base), json.loads(works)))",
      "def _others(paths):",        // does any file need another language's frontend?
      "    return any(language(p) not in ('', 'python') for p in json.loads(paths))",
    ].join("\n"));
    return { py, team: py.globals.get("_team"), others: py.globals.get("_others") };
  })();
  ready.catch(() => { ready = null; });       // a failed load can be retried
  return ready;
}

let languages = null;       // a promise: the other languages' frontends are in place
function polyglot(py) {
  if (languages) return languages;
  languages = (async () => {
    postMessage({ status: "Loading the readers for the other languages (the first time)…" });
    importScripts("../data/polyglot.js");
    write(py, self.MAGELLAN_POLYGLOT.files);
    // Python may have looked for them already and remembered the folder without them
    py.runPython("import importlib; importlib.invalidate_caches()");
  })();
  languages.catch(() => { languages = null; });
  return languages;
}

// every path in the base and in anyone's work
function paths(base, works) {
  const all = new Set(Object.keys(JSON.parse(base) || {}));
  for (const files of Object.values(JSON.parse(works) || {})) {
    for (const p of Object.keys(files || {})) all.add(p);
  }
  return [...all];
}

self.onmessage = async (ev) => {
  const { id, op, base, works } = ev.data || {};
  try {
    if (op !== "team") throw new Error(`unknown request ${op}`);
    const { py, team, others } = await engine();
    if (others(JSON.stringify(paths(base, works)))) await polyglot(py);
    postMessage({ status: "Checking everyone's work…" });
    const t0 = performance.now();
    const result = JSON.parse(team(base, works));
    postMessage({ id, ok: true, result, ms: Math.round(performance.now() - t0) });
  } catch (err) {
    postMessage({ id, ok: false, error: String((err && err.message) || err).split("\n").slice(-3).join("\n") });
  }
};
