"use strict";
// The map panel's page. Its scripts and styles come from media/: map.js, graph3d.js, scene3d.js,
// subgraph.js, subgraph-ui.js and map.css are the website's own (copied by sync.js), panel.js
// and panel.css are the panel's. The content security policy lets in exactly those files and
// nothing else.
//
// The page has two states: a welcome (before the first check) and the report (the verdict,
// the map on its dark stage, the checklist, what the change reaches and what changed).
// media/panel.js fills the report in; everything here is fixed text, never report data.

const SCRIPTS = ["map.js", "graph3d.js", "scene3d.js", "subgraph.js", "subgraph-ui.js", "panel.js"];
const STYLES = ["map.css", "panel.css"];

// the logo, the full Magellan's compass over a map (media/icon.png), inline so it stays sharp at
// any size; and its one-colour mark (media/magellan-lite.svg, the Activity Bar's), which takes
// the theme's accent
const LOGO = `<svg viewBox="0 0 256 256" aria-hidden="true"><rect width="256" height="256" rx="56" fill="#0E2238"/><circle cx="128" cy="128" r="86" fill="none" stroke="#2C4A6B" stroke-width="2"/><circle cx="128" cy="128" r="96" fill="none" stroke="#E9DCC0" stroke-width="5"/><g stroke="#E9DCC0" stroke-width="5" stroke-linecap="round"><line x1="128" y1="22" x2="128" y2="40"/><line x1="128" y1="216" x2="128" y2="234"/><line x1="22" y1="128" x2="40" y2="128"/><line x1="216" y1="128" x2="234" y2="128"/></g><g stroke="#6F8FB0" stroke-width="3.5" fill="none" stroke-linecap="round"><line x1="66" y1="104" x2="128" y2="128"/><line x1="66" y1="104" x2="110" y2="192"/><line x1="128" y1="128" x2="110" y2="192"/><line x1="128" y1="128" x2="190" y2="170"/><line x1="110" y1="192" x2="190" y2="170"/></g><g fill="#6F8FB0"><circle cx="66" cy="104" r="10"/><circle cx="110" cy="192" r="10"/><circle cx="190" cy="170" r="10"/></g><polygon points="192,64 139,139 117,117" fill="#F2A93B"/><polygon points="80,176 117,117 139,139" fill="#E9DCC0"/><circle cx="128" cy="128" r="12" fill="#0E2238" stroke="#F2A93B" stroke-width="5"/></svg>`;
const MARK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9.5"/><path d="M18 6l-4.6 7.4L10.6 10.6z" fill="currentColor"/><path d="M7.5 16.5l3.1-5.9 2.8 2.8z"/><circle cx="6.2" cy="9.8" r="1.2" fill="currentColor" stroke="none"/><circle cx="16.8" cy="16" r="1.2" fill="currentColor" stroke="none"/></svg>`;

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
<header class="top">
  <p class="brand"><span class="mark" aria-hidden="true">${MARK}</span> Magellan Lite
    <span id="repo" class="repo" hidden></span></p>
  <nav class="actions" aria-label="Magellan Lite">
    <button type="button" class="btn" id="again" data-run="magellanLite.check" title="Check this change again" hidden><span class="ico" aria-hidden="true">↻</span><span class="t">Check again</span></button>
    <button type="button" class="btn" data-run="magellanLite.team" title="Check against your team's work in progress"><span class="t">Team</span></button>
    <button type="button" class="btn" id="suite" data-run="magellanLite.openSuite" title="Open the Team suite in your browser" hidden><span class="t">Open the Team suite</span><span class="ico" aria-hidden="true">↗</span></button>
  </nav>
</header>

<main>
  <section id="welcome" class="welcome" aria-label="Welcome">
    <div class="logo">${LOGO}</div>
    <p>Save a Python file or check this change to see what it breaks.</p>
    <button type="button" class="btn primary big" data-run="magellanLite.check"><span class="t">Check this change</span></button>
  </section>

  <div id="report" class="report" hidden>
    <section id="verdict" class="verdict-card" aria-live="polite"></section>

    <section class="stage story-stage" aria-labelledby="stage-title">
      <div class="stage-head">
        <h2 id="stage-title">The change and what it reaches</h2>
        <div class="stage-tools">
          <div class="view-switch" id="views" role="group" aria-label="View">
            <button type="button" data-view="flow" aria-pressed="true" title="The change, hop by hop">Flow</button>
            <button type="button" data-view="3d" aria-pressed="false" title="The whole project in 3D">3D</button>
          </div>
          <button type="button" id="replay" class="pill" title="Play the change spreading again">Replay</button>
        </div>
      </div>
      <p id="stage-lede" class="stage-lede"></p>
      <div id="scenes" class="sg-tabs" role="group" aria-label="The whole map and its sub-graphs ([ and ] to switch)" hidden></div>
      <div id="graph" class="map-box"></div>
      <p class="stage-foot">Click a definition to open it; right-click it for its sub-graph. Each hop fades the score: a call ×0.9, reading a value ×0.85.</p>
    </section>

    <div class="cols">
      <section id="checklist" class="part" aria-label="Checklist"></section>
      <section id="reach" class="part" aria-label="What it reaches"></section>
    </div>
    <section id="changes" class="part" aria-label="What changed"></section>
    <section id="errors" class="errors" hidden></section>
  </div>
</main>

<footer class="foot">Static analysis: a clean report is not proof.</footer>
${SCRIPTS.map((f) => `<script nonce="${nonce}" src="${uri(f)}"></script>`).join("\n")}
</body>
</html>`;
}

/** A random nonce for the page's scripts. */
function nonce() {
  return Array.from({ length: 24 }, () => Math.floor(Math.random() * 36).toString(36)).join("");
}

module.exports = { panelHtml, nonce, SCRIPTS, STYLES };
