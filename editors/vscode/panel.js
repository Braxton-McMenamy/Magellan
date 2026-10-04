"use strict";
// The map panel's page. Its scripts and styles come from media/: map.js and map.css are the
// website's own renderer (copied by sync.js), panel.js and panel.css are the panel's. The
// content security policy lets in exactly those files and nothing else.

const SCRIPTS = ["map.js", "panel.js"];
const STYLES = ["map.css", "panel.css"];

/** The page: `uri(file)` turns a media/ file into a webview URL; `cspSource` is the
 *  webview's own origin; `nonce` marks our scripts as ours. */
function panelHtml({ uri, cspSource, nonce }) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${cspSource} data:; style-src ${cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1">
${STYLES.map((f) => `<link rel="stylesheet" href="${uri(f)}">`).join("\n")}
<title>Magellan Lite map</title>
</head>
<body>
<header id="head"><p class="muted">Save a Python file, or run <b>Magellan Lite: Check this change</b>.</p></header>
<div id="graph" class="map-box"></div>
<section id="lists"></section>
${SCRIPTS.map((f) => `<script nonce="${nonce}" src="${uri(f)}"></script>`).join("\n")}
</body>
</html>`;
}

/** A random nonce for the page's scripts. */
function nonce() {
  return Array.from({ length: 24 }, () => Math.floor(Math.random() * 36).toString(36)).join("");
}

module.exports = { panelHtml, nonce, SCRIPTS, STYLES };
