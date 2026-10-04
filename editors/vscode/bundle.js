"use strict";
// Copy Magellan Lite's engine (the repository's magellan_lite/, with the other languages'
// frontends) into engine/, so the packaged extension runs it with any Python 3.10+: nobody
// installs anything (python.js). `npm run package` runs this; engine/ is not committed.
//
//     node editors/vscode/bundle.js

const fs = require("fs");
const path = require("path");

const HERE = __dirname;
const SRC = path.join(HERE, "..", "..", "magellan_lite");
const DST = path.join(HERE, "engine", "magellan_lite");
// the data files some frontends read besides their .py files
const KEEP = /\.py$|[\\/]typescript[\\/]ts_extract\.js$|[\\/]legacy_include[\\/][^\\/]+\.h$/;

function copy(from, to) {
  let n = 0;
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    if (entry.name === "__pycache__" || entry.name.startsWith(".")) continue;
    const a = path.join(from, entry.name), b = path.join(to, entry.name);
    if (entry.isDirectory()) {
      n += copy(a, b);
    } else if (KEEP.test(a)) {
      fs.mkdirSync(to, { recursive: true });
      fs.copyFileSync(a, b);
      n += 1;
    }
  }
  return n;
}

fs.rmSync(path.join(HERE, "engine"), { recursive: true, force: true });
const n = copy(SRC, DST);
console.log(`bundled ${n} files of Magellan Lite into editors/vscode/engine/magellan_lite`);
