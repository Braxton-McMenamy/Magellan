"use strict";
// The map panel's page. Its scripts and styles come from media/: map.js and map.css are the
// website's own renderer (copied by sync.js), panel.js and panel.css are the panel's. The
// content security policy lets in exactly those files and nothing else.
//
// The page has two states: a welcome (before the first check) and the report (the verdict,
// the map on its dark stage, the checklist, what the change reaches and what changed).
// media/panel.js fills the report in; everything here is fixed text, never report data.

const SCRIPTS = ["map.js", "graph3d.js", "scene3d.js", "panel.js"];
const STYLES = ["map.css", "panel.css"];

// the ◆ logo mark (media/magellan-lite.svg), inline so it takes the theme's colours
const LOGO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9.5"/><path d="M12 6.2 17.8 12 12 17.8 6.2 12z" fill="currentColor"/></svg>`;

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
  <p class="brand"><span class="mark" aria-hidden="true">◆</span> Magellan Lite
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
      <div id="graph" class="map-box"></div>
      <p class="stage-foot">Click a definition to open it. Each hop fades the score: a call ×0.9, reading a value ×0.85.</p>
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
