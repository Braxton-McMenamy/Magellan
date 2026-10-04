"use strict";
// Running Magellan Lite without anyone configuring anything.
//
// The engine: Magellan Lite needs only Python's standard library, so the extension carries
// its source. A packaged extension has it in engine/ (bundle.js copies it in); run from this
// repository, the extension uses the repository's own magellan_lite/. Either way it goes on
// PYTHONPATH, so no `pip install` is needed, and a copy someone did install is ignored.
//
// The Python: any 3.10 or newer. Where it looks, in order: the magellanLite.pythonPath
// setting, if someone set one; the interpreter the Python extension has selected for this
// workspace; then the usual names on PATH (`py -3` on Windows, `python3`, `python`). Each is
// asked for its version before it is used: Windows' `python` can be a shortcut to the Store.
// No VS Code here, so plain Node tests it (test/python.test.js).

const fs = require("fs");
const path = require("path");

const MINIMUM = [3, 10];
const VERSION = "import sys; print('%d %d' % sys.version_info[:2])";

/** Where Magellan Lite's source is: bundled, or this repository's. null if neither. */
function engineDir(extensionPath, exists = fs.existsSync) {
  for (const dir of [path.join(extensionPath, "engine"), path.join(extensionPath, "..", "..")]) {
    if (exists(path.join(dir, "magellan_lite", "__init__.py"))) return path.resolve(dir);
  }
  return null;
}

/** The interpreters to try, in order, as `{ exe, pre }` (`pre`: arguments before ours). */
function candidates({ setting, selected, platform = process.platform }) {
  const out = [];
  const add = (exe, pre = []) => {
    if (exe && !out.some((c) => c.exe === exe && c.pre.join(" ") === pre.join(" "))) out.push({ exe, pre });
  };
  if (setting && setting !== "python") add(setting);         // "python" was the old default
  add(selected);
  if (platform === "win32") add("py", ["-3"]);
  add("python3");
  add("python");
  return out;
}

/** "3 12" -> [3, 12]; anything else -> null. */
function parseVersion(text) {
  const m = String(text || "").trim().match(/^(\d+) (\d+)$/);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

const newEnough = (v) => v && (v[0] > MINIMUM[0] || (v[0] === MINIMUM[0] && v[1] >= MINIMUM[1]));

/**
 * The first candidate that is a Python 3.10+: `{ exe, pre, version }`, or `{ error }` saying
 * what was found. `run(exe, args, opts, cb)` is child_process.execFile.
 */
async function find(list, run, cwd) {
  const seen = [];
  for (const c of list) {
    const v = await new Promise((resolve) => {
      run(c.exe, [...c.pre, "-c", VERSION], { cwd, timeout: 15000, windowsHide: true },
        (err, stdout) => resolve(err ? null : parseVersion(stdout)));
    });
    if (newEnough(v)) return { ...c, version: v.join(".") };
    if (v) seen.push(`${[c.exe, ...c.pre].join(" ")} is Python ${v.join(".")}`);
  }
  return {
    error: seen.length
      ? `Magellan Lite needs Python ${MINIMUM.join(".")} or newer; ${seen.join(", ")}.`
      : `Magellan Lite needs Python ${MINIMUM.join(".")} or newer, and none was found.`,
  };
}

/** The environment a run gets: the engine first on PYTHONPATH, and UTF-8 output. */
function environment(engine, base = process.env) {
  const env = { ...base, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" };
  if (engine) env.PYTHONPATH = [engine, base.PYTHONPATH].filter(Boolean).join(path.delimiter);
  return env;
}

/** The interpreter the Python extension has selected for this folder, if it says. */
async function selectedInterpreter(vscode, folderUri) {
  try {
    const ext = vscode.extensions && vscode.extensions.getExtension("ms-python.python");
    if (!ext) return null;
    const api = ext.isActive ? ext.exports : await ext.activate();
    const env = api && api.environments && api.environments.getActiveEnvironmentPath
      ? api.environments.getActiveEnvironmentPath(folderUri) : null;
    if (env && env.path && env.path !== "python") return env.path;
    const details = api && api.settings && api.settings.getExecutionDetails
      ? api.settings.getExecutionDetails(folderUri) : null;
    return details && details.execCommand && details.execCommand[0] || null;
  } catch {
    return null;                          // its API changed, or it is still starting: look on PATH
  }
}

module.exports = { MINIMUM, engineDir, candidates, parseVersion, find, environment, selectedInterpreter };
