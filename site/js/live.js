// Runs Magellan Lite for "Try it". Two ways, the same answer (magellan_lite/web.py):
//   - served by `magellan-lite serve`: POST api/check to the local server (instant, offline)
//   - anywhere else (the public site, or index.html opened from disk): the engine's own Python
//     source (data/engine.js) runs in the browser on Pyodide, Python compiled to WebAssembly.
//     Nothing is uploaded: the code never leaves the visitor's machine.

window.MagellanLive = (() => {
  const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.29.5/full/";
  let server = null;          // null: not asked yet
  let python = null;          // a promise of the check function

  async function hasServer() {
    if (server !== null) return server;
    if (location.protocol === "file:") return (server = false);
    try {
      // a static host answers this with its 404 or index page, not with JSON saying ok
      const r = await fetch("api/health", { cache: "no-store" });
      const j = r.ok ? await r.json() : null;
      server = Boolean(j && j.ok === true);
    } catch {
      server = false;
    }
    return server;
  }

  const script = (src) => new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.append(s);
  });

  function loadPython(status) {
    if (python) return python;
    python = (async () => {
      status("Loading Python into this page (a few seconds, the first time)…");
      if (!window.loadPyodide) await script(`${PYODIDE}pyodide.js`);
      if (!window.MAGELLAN_ENGINE) await script("data/engine.js");
      const py = await window.loadPyodide({ indexURL: PYODIDE });
      for (const [path, source] of Object.entries(window.MAGELLAN_ENGINE.files)) {
        py.FS.mkdirTree(`/engine/${path.split("/").slice(0, -1).join("/")}`);
        py.FS.writeFile(`/engine/${path}`, source);
      }
      return py.runPython([
        "import json, sys",
        "sys.path.insert(0, '/engine')",
        "from magellan_lite.web import check_with_map",
        "def _check(before, after):",
        "    return json.dumps(check_with_map(json.loads(before), json.loads(after)))",
        "_check",
      ].join("\n"));
    })();
    python.catch(() => { python = null; });   // a failed load can be retried
    return python;
  }

  async function check(before, after, status = () => {}) {
    if (await hasServer()) {
      status("Checking on the local server…");
      const r = await fetch("api/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ before, after }),
      });
      const report = await r.json();
      if (!r.ok) throw new Error(report.error || `the server said ${r.status}`);
      return { ...report, engine: "the local server" };
    }
    const run = await loadPython(status);
    status("Checking…");
    const t0 = performance.now();
    const report = JSON.parse(run(JSON.stringify(before), JSON.stringify(after)));
    return { ...report, engine: "Python in your browser", ms: Math.round(performance.now() - t0) };
  }

  // start loading early, so the first check is quick
  async function warm(status = () => {}) {
    if (!(await hasServer())) await loadPython(status);
  }

  return { check, warm, hasServer };
})();
