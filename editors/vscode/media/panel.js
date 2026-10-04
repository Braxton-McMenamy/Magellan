// The map panel: the last check's verdict, the map of the change and what it reaches, and the
// checklist. The map is drawn by the website's renderer (map.js), so it looks the same here,
// on the website's incident stories, and on the account pages to come.
//
// What the extension sends: { type: "report", report, repo } after each check, and
// { type: "repo", repo } when it finds the project's GitHub repository ("owner/name" or null).
// What the page sends: { type: "ready" } once it has loaded, { type: "open", path, line } to
// open a file at a line, and { type: "run", command } from its buttons.
//
// Report text comes from the user's code, so it goes into the page as text (textContent and
// DOM nodes, through `h` below), never as HTML.

(() => {
  const api = acquireVsCodeApi();
  const $ = (s) => document.querySelector(s);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const short = (name) => String(name ?? "").split(".").slice(-2).join(".");   // module.function

  // an element with attributes and children: strings become text nodes, never HTML
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "onclick") el.addEventListener("click", v);
      else el.setAttribute(k, String(v));
    }
    for (const kid of kids.flat()) {
      if (kid !== undefined && kid !== null && kid !== false && kid !== "") el.append(kid);
    }
    return el;
  }
  // replace an element's children, skipping the empty ones (as h does)
  const fill = (el, ...kids) => el.replaceChildren(
    ...kids.flat().filter((k) => k !== undefined && k !== null && k !== false && k !== ""));

  const SEVERITIES = ["critical", "high", "medium", "low"];
  const KINDS = ["removed", "signature", "renamed", "value", "body", "added"];
  const WHY = {
    block: "Don't commit yet: fix what the checklist found first.",
    review: "Look over what the checklist found before you commit.",
    ok: "Nothing on the checklist fired for what this change touched.",
  };
  const MAX_REACH = 12;

  let current = null;       // the report on screen
  let lastVerdict = null;   // so the verdict only pops when it changes
  let lastChanges = null;   // so the map only replays when the change itself is new

  // -- talking to the extension ------------------------------------------------------------
  const open = (path, line) => {
    if (path) api.postMessage({ type: "open", path, line: Number(line) || 1 });
  };
  const link = (path, line) => h("button", {
    type: "button", class: "link", title: `Open ${path}:${line}`, onclick: () => open(path, line),
  }, `${path}:${line}`);

  // the buttons: "Check again", "Team", "Open the Team suite" (data-run says which command)
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-run]");
    if (!b || b.disabled) return;
    api.postMessage({ type: "run", command: b.dataset.run });
    if (b.dataset.run === "magellanLite.check") busy(true);
  });

  // while a check runs: the check buttons say so and the report dims. The next report ends
  // it; a check that fails sends none, so it also ends on its own after a while.
  let busyTimer = 0;
  function busy(on) {
    clearTimeout(busyTimer);
    document.body.classList.toggle("busy", on);
    for (const b of document.querySelectorAll('[data-run="magellanLite.check"]')) {
      const t = b.querySelector(".t");
      b.dataset.idle = b.dataset.idle || t.textContent;
      t.textContent = on ? "Checking…" : b.dataset.idle;
      b.disabled = on;
    }
    if (on) busyTimer = setTimeout(() => busy(false), 30000);
  }

  function setRepo(slug) {
    const repo = typeof slug === "string" && slug ? slug : "";
    $("#repo").textContent = repo;
    $("#repo").hidden = !repo;
    $("#suite").hidden = !repo;      // the Team suite reads the repository from GitHub
  }

  // -- the verdict -------------------------------------------------------------------------
  function drawVerdict(report, findings, changes, affected) {
    const verdict = WHY[report.verdict] ? report.verdict : "review";
    const pop = verdict !== lastVerdict;
    lastVerdict = verdict;
    const summary = `${plural(findings.length, "finding")} · ${plural(changes.length, "change")}${
      affected.length ? ` · reaches ${plural(affected.length, "definition")} nobody edited` : ""}`;
    const card = $("#verdict");
    card.className = `verdict-card ${verdict}`;
    fill(card,
      h("span", { class: `verdict${pop ? " pop" : ""}` }, verdict),
      h("div", { class: "verdict-text" },
        h("p", { class: "summary" }, summary),
        h("p", { class: "why" }, changes.length ? WHY[verdict] : "No definition changed, so there is nothing to check yet."),
        h("ul", { class: "chips", "aria-label": "In numbers" }, chips(findings, changes, affected))));
  }

  // small counts: findings by severity, changes by kind, files touched, how far it reaches
  function chips(findings, changes, affected) {
    const chip = (n, label, cls = "") => h("li", { class: `chip ${cls}` },
      cls && h("i", { class: "dot", "aria-hidden": "true" }), h("b", {}, String(n)), ` ${label}`);
    const out = [];
    for (const s of SEVERITIES) {
      const n = findings.filter((f) => f.severity === s).length;
      if (n) out.push(chip(n, s, `sev-${s}`));
    }
    const kinds = new Map();
    for (const c of changes) kinds.set(String(c.kind), (kinds.get(String(c.kind)) || 0) + 1);
    const place = (k) => (KINDS.includes(k) ? KINDS.indexOf(k) : KINDS.length);
    for (const k of [...kinds.keys()].sort((a, b) => place(a) - place(b))) {
      out.push(chip(kinds.get(k), k, KINDS.includes(k) ? `kind-${k}` : "kind-other"));
    }
    const files = new Set(changes.map((c) => c.path)).size;
    if (files) out.push(chip(files, files === 1 ? "file" : "files"));
    const deepest = Math.max(0, ...affected.map((a) => Number(a.hops) || 0));
    if (deepest) out.push(chip(deepest, deepest === 1 ? "hop deep" : "hops deep"));
    return out;
  }

  // -- the map, on its stage ---------------------------------------------------------------
  function drawLede(changes, affected) {
    const edited = changes.filter((c) => c.kind !== "added").length || changes.length;
    fill($("#stage-lede"), affected.length
      ? [`The change edits ${plural(edited, "definition")}. Its effect spreads to `,
        h("em", {}, "whatever calls it"),
        `, then to their callers: ${plural(affected.length, "definition")} nobody edited can break.`]
      : [changes.length ? "Nothing else in the project calls the code this change edits."
        : "No definition changed, so there is nothing to follow yet."]);
  }

  // Two views of the same map, as on the website: Flow (the change, hop by hop: map.js) and 3D
  // (the whole project as clusters on a sphere: graph3d.js through scene3d.js). The whole map's
  // choice is kept in the webview's state, so it survives the panel being hidden and shown.
  //
  // Right-click a definition for its sub-graph (subgraph.js): it, what depends on it and what it
  // uses, or a whole file and what touches it, in a tab of its own above the map. Each tab keeps
  // its own map, view and box (a hidden 3D view keeps its camera); [ and ] step through them.
  const SG = MagellanSubgraph, UI = MagellanSubgraphUI;
  const MAIN_TITLE = $("#stage-title").textContent;
  let scenes = [];          // the whole map first: { id, spec, mode, pane, view, sub, title, line, stale, width }
  let shown = null;         // the scene on screen
  let visited = [];         // the order tabs were shown in, to go back to when one closes
  let seq = 0;

  function newScene(spec, mode) {
    const pane = h("div", { class: "scene-pane", hidden: true });
    $("#graph").append(pane);
    const scene = { id: `s${seq++}`, spec, mode, pane, view: null, sub: null, title: "", line: "", stale: true, width: 0 };
    scenes.push(scene);
    return scene;
  }
  const main = () => scenes[0] || newScene(null, (api.getState && api.getState() && api.getState().view) || "flow");
  const wholeMap = () => current.map || MagellanMap.fromReport(current);

  /** A scene's map now: the report's, or the sub-graph drawn from it (Flow reads it its way). */
  function mapOf(scene) {
    if (!scene.spec) return wholeMap();
    const sub = SG.build(wholeMap(), scene.spec);
    if (sub.nodes.length || !scene.sub) scene.sub = sub;        // gone from the new check: keep the last
    scene.title = SG.title(scene.sub);
    scene.line = SG.describe(scene.sub) + (sub.nodes.length ? "" : " (Not in the latest check.)");
    return scene.mode === "flow" ? SG.flow(scene.sub) : scene.sub;
  }

  /** Draw a scene into its box (on screen: the renderers size to it). */
  function drawScene(scene, animate) {
    if (scene.view && scene.view.destroy) scene.view.destroy();
    const map = mapOf(scene);
    const box = scene.pane;
    scene.width = box.clientWidth;
    scene.stale = false;
    const onContext = (node, event, g) => menu(scene, node, event, g);
    if (scene.mode === "3d") {
      // exploring turns and picks; a double-click opens the file
      scene.view = MagellanScene3D.render(box, map, {
        label: scene.spec ? scene.title : "The whole project in 3D, coloured by the change",
        onPick() {},
        onOpen(node) { open(node.path, node.line); },
        onContext,
      });
      return;
    }
    scene.view = MagellanMap.render(box, map, {
      animate,
      heads: scene.spec ? SG.heads(scene.sub) : undefined,
      // a click (or Enter) on a definition opens its file at its line
      onPick(node, g) {
        mark(box, g);
        open(node.path, node.line);
      },
      onContext,
    });
    if (animate) scene.view.play();
  }
  const mark = (box, g) => {
    box.querySelectorAll(".node.picked").forEach((n) => n.classList.remove("picked"));
    if (g) g.classList.add("picked");
  };

  /** Put a scene on screen: its box, its view's switch, its title; draw it if it is out of date. */
  function show(scene, animate = false) {
    UI.close();
    if (shown && shown !== scene) {
      shown.pane.hidden = true;
      if (shown.view && shown.view.view) shown.view.view.stop();     // a hidden 3D view rests
    }
    shown = scene;
    visited = visited.filter((s) => s !== scene).concat(scene);
    scene.pane.hidden = false;
    $("#replay").hidden = scene.mode === "3d";
    $("#graph").classList.toggle("is-3d", scene.mode === "3d");
    $("#views").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === scene.mode)));
    if (scene.stale || !scene.view) drawScene(scene, animate);
    else if (scene.view.view) { scene.view.view.resize(); scene.view.view.dirty = true; scene.view.view.start(); }
    $("#stage-title").textContent = scene.spec ? scene.title : MAIN_TITLE;
    if (scene.spec) fill($("#stage-lede"), scene.line);
    else drawLede(current.changes || [], current.affected || []);
    strip();
  }

  /** Open a sub-graph in a tab of its own, or show the tab that has it. */
  function openSub(spec) {
    const had = scenes.find((s) => SG.same(s.spec, spec));
    if (had) return show(had);
    if (!SG.build(wholeMap(), spec).nodes.length) return;
    show(newScene(spec, shown ? shown.mode : main().mode), true);
  }

  /** Close a sub-graph's tab, back to the one shown before it. */
  function closeScene(scene) {
    if (!scene || !scene.spec) return;
    if (scene.view && scene.view.destroy) scene.view.destroy();
    scene.pane.remove();
    scenes = scenes.filter((s) => s !== scene);
    visited = visited.filter((s) => s !== scene);
    if (shown === scene) { shown = null; show(visited[visited.length - 1] || main()); } else strip();
  }

  function strip() {
    UI.strip($("#scenes"), scenes.map((s) => ({
      id: s.id, closable: !!s.spec, title: s.spec ? s.title : "Whole map",
      hint: s.spec ? s.line : "The change, and the whole project around it",
    })), shown && shown.id, {
      onPick: (t) => show(scenes.find((s) => s.id === t.id)),
      onClose: (t) => closeScene(scenes.find((s) => s.id === t.id)),
      away: () => shown && shown.pane.querySelector("canvas, .node[tabindex]"),
    });
  }
  UI.keys((step) => {
    if (scenes.length < 2 || !shown) return;
    show(scenes[(scenes.indexOf(shown) + step + scenes.length) % scenes.length]);
  });

  /** The right-click menu on a definition: its sub-graph, its file's, and opening it. */
  function menu(scene, node, event, g) {
    mark(scene.pane, g);
    const file = node.path ? node.path.split("/").pop() : "";
    UI.menu(event, node.label || node.id, [
      { label: "Show sub-graph", run: () => openSub({ ids: [node.id] }) },
      node.path && { label: "Sub-graph of its file", detail: file, run: () => openSub({ file: node.path }) },
      node.path && { label: "Open file", detail: `line ${node.line}`, run: () => open(node.path, node.line) },
    ]);
  }

  /** A new report: every scene draws from it, the one on screen now and the rest when shown. */
  function drawMap(report, animate) {
    scenes.forEach((s) => { s.stale = true; });
    const scene = shown || main();
    show(scene, animate && !scene.spec);
  }

  $("#replay").addEventListener("click", () => shown && shown.view && shown.view.play && shown.view.play());
  $("#views").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    if (!shown || shown.mode === b.dataset.view) return;
    shown.mode = b.dataset.view;
    if (!shown.spec && api.setState) api.setState({ ...(api.getState && api.getState()), view: shown.mode });
    shown.stale = true;
    if (current) show(shown, shown.mode === "flow");
  }));

  // the panel was resized (a side panel often is): draw the map again for the new width,
  // standing still. Small changes, like a scroll bar appearing, just scale the drawing.
  let resizeTimer = 0;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!current || !shown || shown.mode === "3d") return;     // the 3D view sizes itself
      const w = shown.pane.clientWidth;
      if (w && Math.abs(w - shown.width) > 24) drawScene(shown, false);
    }, 150);
  }).observe($("#graph"));

  // -- the checklist -----------------------------------------------------------------------
  const heading = (text, n) => h("h2", {}, text, h("span", { class: "count" }, String(n)));

  function drawChecklist(findings) {
    const rank = (f) => (SEVERITIES.indexOf(f.severity) + 1 || 9);
    const sorted = [...findings].sort((a, b) => rank(a) - rank(b));
    fill($("#checklist"), heading("Checklist", findings.length), findings.length
      ? h("ul", { class: "findings" }, sorted.map(finding))
      : h("p", { class: "quiet" }, "Nothing to check in what this change touched."));
  }

  // one finding: a card with its severity's colour down the side
  function finding(f) {
    const sev = SEVERITIES.includes(f.severity) ? f.severity : "low";
    return h("li", { class: `issue ${sev}` },
      h("div", { class: "f-top" },
        h("span", { class: "sev" }, sev),
        h("code", { class: "rule" }, f.rule),
        f.path && link(f.path, f.line)),
      h("p", { class: "f-msg" }, f.message),
      f.detail && h("p", { class: "f-detail" }, f.detail),
      f.fix && h("p", { class: "f-fix" }, h("b", {}, "Fix"), f.fix));
  }

  // -- what it reaches ---------------------------------------------------------------------
  function drawReach(affected) {
    const more = affected.length - MAX_REACH;
    fill($("#reach"), heading("What it reaches", affected.length), affected.length
      ? h("ol", { class: "reach" }, affected.slice(0, MAX_REACH).map(reachRow))
      : h("p", { class: "quiet" }, "Nothing else in the project calls the code this change edits."),
      more > 0 && h("p", { class: "quiet more" }, `and ${more} more, further away`));
  }

  // one definition the change reaches: its name and file, a heat bar for its score, its hops
  function reachRow(a) {
    const score = Math.max(0, Math.min(1, Number(a.score) || 0));
    const fill = h("i");
    fill.style.setProperty("--score", String(score));     // CSSOM, not a style="" attribute
    return h("li", {}, h("button", {
      type: "button", class: "reach-item", onclick: () => open(a.path, a.line),
      title: `${a.why || a.name}\nOpen ${a.path}:${a.line}`,
    },
    h("span", { class: "r-name" }, h("code", {}, short(a.name)), h("small", {}, `${a.path}:${a.line}`)),
    h("span", { class: "heat" }, h("span", { class: "bar", "aria-hidden": "true" }, fill),
      h("b", { class: "score" }, score.toFixed(2))),
    h("span", { class: "hops" }, plural(Number(a.hops) || 0, "hop"))));
  }

  // -- what changed ------------------------------------------------------------------------
  function drawChanges(changes) {
    fill($("#changes"), heading("What changed", changes.length), changes.length
      ? h("ul", { class: "changes" }, changes.map(changeRow))
      : h("p", { class: "quiet" }, "No definition changed."));
  }

  // a change's detail, when it says more than its kind: a new signature reads old -> new
  const PLAIN = new Set(["removed", "added", "body"]);
  function detail(c) {
    if (!c.detail || PLAIN.has(c.kind)) return null;
    const [before, after] = String(c.detail).split(/\s{2}->\s{2}/);
    return h("span", { class: "c-detail" }, after === undefined ? before
      : [h("del", {}, before), h("span", { class: "arrow", "aria-label": "becomes" }, " → "), h("ins", {}, after)]);
  }

  function changeRow(c) {
    const kind = KINDS.includes(c.kind) ? c.kind : "other";
    return h("li", {}, h("button", {
      type: "button", class: "change", onclick: () => open(c.path, c.line),
      title: `${c.name}\nOpen ${c.path}:${c.line}`,
    },
    h("span", { class: `kind ${kind}` }, c.kind || "changed"),
    h("span", { class: "c-name" }, h("code", {}, short(c.name)), h("small", {}, `${c.path}:${c.line}`)),
    detail(c)));
  }

  // -- what the check could not do: quiet, at the bottom -----------------------------------
  function drawErrors(errors) {
    const text = errors.map((e) => (typeof e === "string" ? e : (e && e.message) || JSON.stringify(e)));
    const box = $("#errors");
    box.hidden = !text.length;
    fill(box, text.length && h("details", {},
      h("summary", {}, `${plural(text.length, "thing")} the check could not do`),
      h("ul", {}, text.map((t) => h("li", {}, t)))));
  }

  // -- the whole report --------------------------------------------------------------------
  function draw(report) {
    current = report;
    busy(false);
    const findings = report.findings || [], affected = report.affected || [];
    const changes = report.changes || [];
    $("#welcome").hidden = true;
    $("#report").hidden = false;
    $("#again").hidden = false;
    drawVerdict(report, findings, changes, affected);
    drawLede(changes, affected);
    // the map plays the change spreading the first time, and again when the change is new
    const key = changes.map((c) => `${c.kind} ${c.name}`).sort().join("\n");
    drawMap(report, key !== lastChanges);
    lastChanges = key;
    drawChecklist(findings);
    drawReach(affected);
    drawChanges(changes);
    drawErrors(report.errors || []);
  }

  window.addEventListener("message", (e) => {
    const d = e.data || {};
    if (d.type === "report" && d.report) {
      if ("repo" in d) setRepo(d.repo);
      draw(d.report);
    }
    if (d.type === "repo") setRepo(d.repo);
  });
  api.postMessage({ type: "ready" });
})();
