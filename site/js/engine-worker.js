// Magellan Lite's engine (data/engine.js) on Pyodide, in a worker: a team check runs several
// whole-project checks, and here they don't freeze the page. js/teamfeed.js talks to it:
//   -> { id, op: "team", base: "{json}", works: "{json}" }
//   <- { id, ok: true, result } | { id, ok: false, error } | { status: "Loading Python…" }

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/";

let ready = null;
function engine() {
  if (ready) return ready;
  ready = (async () => {
    postMessage({ status: "Loading Python (a few seconds, the first time)…" });
    importScripts(`${PYODIDE}pyodide.js`, "../data/engine.js");
    const py = await self.loadPyodide({ indexURL: PYODIDE });
    for (const [path, source] of Object.entries(self.MAGELLAN_ENGINE.files)) {
      py.FS.mkdirTree(`/engine/${path.split("/").slice(0, -1).join("/")}`);
      py.FS.writeFile(`/engine/${path}`, source);
    }
    return py.runPython([
      "import json, sys",
      "sys.path.insert(0, '/engine')",
      "from magellan_lite.web import team_live",
      "def _team(base, works):",
      "    return json.dumps(team_live(json.loads(base), json.loads(works)))",
      "_team",
    ].join("\n"));
  })();
  ready.catch(() => { ready = null; });       // a failed load can be retried
  return ready;
}

self.onmessage = async (ev) => {
  const { id, op, base, works } = ev.data || {};
  try {
    if (op !== "team") throw new Error(`unknown request ${op}`);
    const team = await engine();
    postMessage({ status: "Checking everyone's work…" });
    const t0 = performance.now();
    const result = JSON.parse(team(base, works));
    postMessage({ id, ok: true, result, ms: Math.round(performance.now() - t0) });
  } catch (err) {
    postMessage({ id, ok: false, error: String((err && err.message) || err).split("\n").slice(-3).join("\n") });
  }
};
