"use strict";
// Copy the website's shared map renderer into media/, so the map panel draws exactly what the
// website draws. The list is shared.json; the website (site/) holds the originals.
//
//     node editors/vscode/sync.js            copy
//     node editors/vscode/sync.js --check    exit 1 if a copy is out of date

const fs = require("fs");
const path = require("path");

const HERE = __dirname;
const REPO = path.join(HERE, "..", "..");
const { files } = JSON.parse(fs.readFileSync(path.join(HERE, "shared.json"), "utf8"));
const check = process.argv.includes("--check");

let stale = 0;
for (const [from, to] of Object.entries(files)) {
  const src = fs.readFileSync(path.join(REPO, from), "utf8").replace(/\r\n/g, "\n");
  const dst = path.join(HERE, to);
  const now = fs.existsSync(dst) ? fs.readFileSync(dst, "utf8").replace(/\r\n/g, "\n") : null;
  if (now === src) continue;
  stale += 1;
  if (check) {
    console.log(`out of date: editors/vscode/${to} (from ${from})`);
  } else {
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.writeFileSync(dst, src);
    console.log(`copied ${from} -> editors/vscode/${to}`);
  }
}
if (check && stale) console.log("run: node editors/vscode/sync.js");
if (!stale) console.log("media/ is up to date with site/");
process.exit(check && stale ? 1 : 0);
