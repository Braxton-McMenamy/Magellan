// The map panel: the last check's verdict in a line, and the map, alone. The findings (and what
// the change reaches, and what changed) open beside the map in a drawer: from the Findings
// button, or by clicking a dot that has a finding. The map is drawn by the website's renderers
// (map.js, scene3d.js), so it looks the same here, on the website's Scene and in the Team suite.
//
// What the extension sends: { type: "report", report, repo } after each check,
// { type: "repo", repo } when it finds the project's GitHub repository ("owner/name" or null),
// and { type: "focus", id, quiet } when the editor's cursor rests in a definition on the map
// (quiet: the panel just opened, so fill the Local view in without bringing it on screen).
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

  // what the panel keeps while it lives (hidden and shown again): the Local view's Flow or 3D,
  // Local or Global, the lock, and whether the findings are open
  const saved = () => (api.getState && api.getState()) || {};
  const remember = (patch) => { if (api.setState) api.setState({ ...saved(), ...patch }); };

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
    block: "Don't commit yet: fix what Findings lists first.",
    review: "Look over the findings before you commit.",
    ok: "Nothing on the checklist fired for what this change touched.",
  };
  const MAX_REACH = 12;

  let current = null;       // the report on screen
  let lastVerdict = null;   // so the verdict only pops when it changes

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

  // -- the verdict, in a line --------------------------------------------------------------
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
        h("p", { class: "why" }, changes.length ? WHY[verdict] : "No definition changed, so there is nothing to check yet.")));
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

  // The map is a strip of tabs, each with its own box and view (a hidden 3D view keeps its
  // camera); [ and ] step through them:
  //   the whole map   Global: the whole project in 3D, coloured by the change. 3D only: as a
  //                   flow, a whole project is too big to read
  //   Local           the definition the editor's cursor is in, and its neighbourhood
  //                   (subgraph.js); one tab, which follows the cursor. Its own Flow or 3D
  //   sub-graphs      right-click a dot: it, what depends on it and what it uses, or a whole file
  //                   and what touches it. Each its own Flow or 3D
  //
  // As in the full Magellan's graph tab: Local follows the cursor and Global shows the whole
  // map; the lock stops clicks in the code from moving the map (L, or the padlock).
  const SG = MagellanSubgraph, UI = MagellanSubgraphUI, F = MagellanFindings;
  const MAIN_TITLE = $("#stage-title").textContent;
  let scenes = [];          // { id, kind: "whole" | "local" | "sub", spec, mode, pane, view, sub, title, line, stale, width }
  let shown = null;         // the scene on screen
  let visited = [];         // the order tabs were shown in, to go back to when one closes
  let seq = 0;
  let scope = saved().scope === "global" ? "global" : "local";   // local: a click in the code shows its neighbourhood
  let locked = saved().locked === true;
  let pending = null;       // the cursor's latest definition, kept while locked

  function newScene(kind, spec, mode) {
    const pane = h("div", { class: "scene-pane", hidden: true });
    $("#graph").append(pane);
    const scene = { id: `s${seq++}`, kind, spec, mode, pane, view: null, sub: null, title: "", line: "", stale: true, width: 0 };
    scenes.push(scene);
    return scene;
  }
  const whole = () => scenes[0] || newScene("whole", null, "3d");
  /** The Local tab, second after the whole map; made the first time it is needed. */
  function local() {
    let scene = scenes.find((s) => s.kind === "local");
    if (!scene) {
      whole();
      scene = newScene("local", null, saved().localView === "3d" ? "3d" : "flow");
      scenes = [scenes[0], scene, ...scenes.slice(1).filter((s) => s !== scene)];
    }
    return scene;
  }
  const wholeMap = () => current.map || MagellanMap.fromReport(current);

  /** A scene's map now (and its title and line): the report's, or a part of it. Null: Local
   *  before the cursor has been in a definition. */
  function mapOf(scene) {
    if (scene.kind === "whole") {
      scene.title = "Whole map";
      return wholeMap();
    }
    if (!scene.spec) {
      scene.title = "Local";
      scene.line = "It follows the cursor in the editor.";
      return null;
    }
    const sub = SG.build(wholeMap(), scene.spec);
    if (sub.nodes.length || !scene.sub) scene.sub = sub;        // gone from the new check: keep the last
    const gone = sub.nodes.length ? "" : " (Not in the latest check.)";
    if (scene.kind === "local") {
      const n = scene.sub.nodes.length;
      scene.title = `Local: ${SG.subject(scene.sub)}${n ? ` · ${scene.sub.capped ? `${n} of ${scene.sub.total}` : n}` : ""}`;
    } else scene.title = SG.title(scene.sub);
    scene.line = SG.describe(scene.sub) + gone;
    return scene.mode === "flow" ? SG.flow(scene.sub) : scene.sub;
  }

  /** The whole map's own node: a sub-graph's Flow redraws them (its "picked", its hops). */
  const original = (node) => (current && wholeMap().nodes.find((n) => n.id === node.id)) || node;
  const findingsOf = (node) => (current ? F.of(original(node), current.findings || [], wholeMap().nodes) : []);

  /** A click on a dot: its findings, when it has any; otherwise, in Flow, its file. */
  function clicked(node, from, openIt) {
    if (findingsOf(node).length) return openDrawer({ kind: "node", id: node.id }, from);
    if (openIt) open(node.path, node.line);
    return null;
  }

  /** Draw a scene into its box (on screen: the renderers size to it). */
  function drawScene(scene, animate) {
    if (scene.view && scene.view.destroy) scene.view.destroy();
    scene.view = null;
    const map = mapOf(scene);
    const box = scene.pane;
    scene.width = box.clientWidth;
    scene.stale = false;
    if (!map) {
      fill(box, h("p", { class: "local-empty" }, "Click into a function or class in the editor: it and its neighbourhood show here."));
      return;
    }
    const onContext = (node, event, g) => menu(scene, node, event, g);
    if (scene.mode === "3d") {
      // a click picks (and shows a dot's findings); a double-click opens the file
      scene.view = MagellanScene3D.render(box, map, {
        label: scene.kind === "whole" ? "The whole project in 3D, coloured by the change" : scene.title,
        onPick(node) { clicked(node, scene.pane.querySelector("canvas"), false); },
        onOpen(node) { open(node.path, node.line); },
        onContext,
      });
      return;
    }
    scene.view = MagellanMap.render(box, map, {
      animate,
      heads: scene.kind === "local" ? { 0: "at the cursor" } : SG.heads(scene.sub),
      // a click (or Enter) on a definition opens its file at its line, or its findings
      onPick(node, g) {
        mark(box, g);
        clicked(node, g, true);
      },
      onContext,
    });
    if (animate) scene.view.play();
  }
  const mark = (box, g) => {
    box.querySelectorAll(".node.picked").forEach((n) => n.classList.remove("picked"));
    if (g) g.classList.add("picked");
  };

  /** Put a scene on screen: its box, the switches, its title; draw it if it is out of date. */
  function show(scene, animate = false) {
    UI.close();
    if (shown && shown !== scene) {
      shown.pane.hidden = true;
      if (shown.view && shown.view.view) shown.view.view.stop();     // a hidden 3D view rests
    }
    shown = scene;
    visited = visited.filter((s) => s !== scene).concat(scene);
    scene.pane.hidden = false;
    $("#graph").classList.toggle("is-3d", scene.mode === "3d");
    if (scene.stale || !scene.view) drawScene(scene, animate);
    else if (scene.view && scene.view.view) { scene.view.view.resize(); scene.view.view.dirty = true; scene.view.view.start(); }
    $("#stage-title").textContent = scene.kind === "whole" ? MAIN_TITLE : scene.title;
    if (scene.kind === "whole") drawLede(current.changes || [], current.affected || []);
    else fill($("#stage-lede"), scene.line);
    chrome();
    strip();
  }

  /** The switches and words around the map, for the scene on screen and the lock. */
  function chrome() {
    const scene = shown;
    if (!scene) return;
    // Local or Global: which of the two is on screen (neither, on a sub-graph)
    $("#scope").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed",
      String(b.dataset.scope === (scene.kind === "local" ? "local" : scene.kind === "whole" ? "global" : ""))));
    // Flow is for a neighbourhood: the whole map is 3D only
    $("#views").hidden = scene.kind === "whole";
    $("#views").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === scene.mode)));
    $("#replay").hidden = scene.mode !== "flow" || !scene.view;
    // the lock: open, the map follows the editor's cursor; shut, clicking around the code
    // leaves it alone (a new check still redraws it)
    const lock = $("#lock");
    lock.setAttribute("aria-pressed", String(locked));
    lock.title = locked ? "Locked: clicking in the editor leaves the map alone. Click to unlock (L)"
      : "Unlocked: the map follows the editor's cursor. Click to lock it (L)";
    $("#lockbadge").hidden = !locked;
    $("#stage-foot").textContent = scene.mode === "3d"
      ? "Click a dot with a red ring for its findings; double-click a dot to open it; right-click it for its sub-graph."
      : "Click a definition to open it, or to see its findings when it has any; right-click it for its sub-graph. Each hop fades the score: a call ×0.9, reading a value ×0.85.";
  }

  /** Open a sub-graph in a tab of its own, or show the tab that has it. */
  function openSub(spec) {
    const had = scenes.find((s) => s.kind === "sub" && SG.same(s.spec, spec));
    if (had) return show(had);
    if (!SG.build(wholeMap(), spec).nodes.length) return;
    show(newScene("sub", spec, shown ? shown.mode : "3d"), true);
  }

  /** Close a sub-graph's tab, back to the one shown before it. */
  function closeScene(scene) {
    if (!scene || scene.kind !== "sub") return;
    if (scene.view && scene.view.destroy) scene.view.destroy();
    scene.pane.remove();
    scenes = scenes.filter((s) => s !== scene);
    visited = visited.filter((s) => s !== scene);
    if (shown === scene) { shown = null; show(visited[visited.length - 1] || whole()); } else strip();
  }

  /** A tab chosen (a click, or [ and ]): the whole map is Global, Local is Local. */
  function pickScene(scene) {
    if (!scene) return;
    if (scene.kind !== "sub") setScope(scene.kind === "local" ? "local" : "global", false);
    show(scene);
  }

  function strip() {
    UI.strip($("#scenes"), scenes.map((s) => ({
      id: s.id, closable: s.kind === "sub", title: s.title || (s.kind === "whole" ? "Whole map" : "Local"),
      hint: s.kind === "whole" ? "Global: the whole project, coloured by the change"
        : s.kind === "local" ? `Local: follows the cursor in the editor${locked ? " (locked)" : ""}. ${s.line}` : s.line,
    })), shown && shown.id, {
      onPick: (t) => pickScene(scenes.find((s) => s.id === t.id)),
      onClose: (t) => closeScene(scenes.find((s) => s.id === t.id)),
      away: () => shown && shown.pane.querySelector("canvas, .node[tabindex]"),
    });
    // the Local tab gets its own mark (subgraph-ui.js gives every tab that can't close the
    // whole map's)
    const i = scenes.findIndex((s) => s.kind === "local");
    const tab = i >= 0 && $("#scenes").children[i];
    if (tab) { tab.classList.add("local"); tab.classList.toggle("locked", locked); }
  }
  UI.keys((step) => {
    if (scenes.length < 2 || !shown) return;
    pickScene(scenes[(scenes.indexOf(shown) + step + scenes.length) % scenes.length]);
  });

  /** The right-click menu on a definition: its sub-graph, its file's, its findings, opening it. */
  function menu(scene, node, event, g) {
    mark(scene.pane, g);
    const file = node.path ? node.path.split("/").pop() : "";
    const found = findingsOf(node).length;
    UI.menu(event, node.label || node.id, [
      { label: "Show sub-graph", run: () => openSub({ ids: [node.id] }) },
      node.path && { label: "Sub-graph of its file", detail: file, run: () => openSub({ file: node.path }) },
      found && { label: "Show its findings", detail: String(found), run: () => openDrawer({ kind: "node", id: node.id }, g) },
      node.path && { label: "Open file", detail: `line ${node.line}`, run: () => open(node.path, node.line) },
    ]);
  }

  // -- Local and Global, and the lock ------------------------------------------------------
  /** Local: show the cursor's definition (and follow it); Global: the whole map. */
  function setScope(to, showIt = true) {
    scope = to === "global" ? "global" : "local";
    remember({ scope });
    if (showIt && current) show(scope === "local" ? local() : whole());
  }

  /** The cursor rests in a definition on the map (the extension found which). */
  function onFocus(d) {
    if (!d || !d.id) return;
    if (locked) { pending = d; return; }
    follow(d);
  }
  function follow(d) {
    pending = null;
    const scene = local();
    const spec = { ids: [String(d.id)] };
    if (!SG.same(scene.spec, spec)) {
      scene.spec = spec;
      scene.sub = null;
      scene.stale = true;
    }
    if (!current) return;
    // on screen when following (unless a sub-graph tab is: that was opened on purpose)
    if (shown === scene || (!d.quiet && scope === "local" && (!shown || shown.kind !== "sub"))) show(scene);
    else { mapOf(scene); strip(); }
  }

  function setLocked(on) {
    locked = !!on;
    remember({ locked });
    chrome();
    strip();
    if (!locked && pending) follow(pending);        // catch up with the cursor
  }

  $("#scope").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => setScope(b.dataset.scope)));
  $("#lock").addEventListener("click", () => setLocked(!locked));
  // L locks and unlocks, as in the full Magellan's graph tab (not while typing, nor with a
  // modifier: Ctrl+L and the like are VS Code's)
  addEventListener("keydown", (e) => {
    if (String(e.key || "").toLowerCase() !== "l" || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || "")) return;
    if (!current) return;
    e.preventDefault();
    setLocked(!locked);
  });

  /** A new report: every scene draws from it, the one on screen now and the rest when shown. */
  function drawMap() {
    scenes.forEach((s) => { s.stale = true; });
    show(shown || whole());
  }

  $("#replay").addEventListener("click", () => shown && shown.view && shown.view.play && shown.view.play());
  $("#views").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    if (!shown || shown.kind === "whole" || shown.mode === b.dataset.view) return;
    shown.mode = b.dataset.view;
    if (shown.kind === "local") remember({ localView: shown.mode });
    shown.stale = true;
    if (current) show(shown, shown.mode === "flow");
  }));

  // the panel was resized (a side panel often is): draw the map again for the new width,
  // standing still. Small changes, like a scroll bar appearing, just scale the drawing.
  let resizeTimer = 0;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!current || !shown || shown.mode === "3d" || !shown.view) return;     // the 3D view sizes itself
      const w = shown.pane.clientWidth;
      if (w && Math.abs(w - shown.width) > 24) drawScene(shown, false);
    }, 150);
  }).observe($("#graph"));

  // -- the drawer: the findings, beside the map ----------------------------------------------
  // { kind: "all" } (the Findings button: every finding, then what the change reaches and what
  // changed) or { kind: "node", id } (a dot's own findings). Escape or × closes it and gives
  // the keyboard back to what opened it.
  let drawer = null, opener = null;

  function openDrawer(what, from) {
    drawer = what;
    opener = from && from.focus ? from : document.activeElement;
    remember({ drawer: what.kind === "all" ? "findings" : `node:${what.id}` });
    drawDrawer();
    $("#drawer-title").focus({ preventScroll: true });
    return what;
  }

  function closeDrawer(refocus = true) {
    if (!drawer) return;
    drawer = null;
    remember({ drawer: null });
    drawDrawer();
    if (refocus && opener && opener.isConnected && opener.focus) opener.focus({ preventScroll: true });
    opener = null;
  }

  function drawDrawer() {
    const box = $("#drawer"), btn = $("#findings-btn");
    const findings = (current && current.findings) || [];
    // the button: how many, coloured by the worst
    const worst = F.sorted(findings)[0];
    $("#findings-n").textContent = String(findings.length);
    btn.className = `btn findings-btn${worst ? ` worst-${SEVERITIES.includes(worst.severity) ? worst.severity : "low"}` : ""}`;
    btn.setAttribute("aria-expanded", String(!!drawer && drawer.kind === "all"));
    btn.setAttribute("aria-label", `Findings: ${findings.length}`);
    btn.hidden = !current;
    $("#map-area").classList.toggle("has-drawer", !!drawer);
    box.hidden = !drawer || !current;
    if (box.hidden) return;
    if (drawer.kind === "node") {
      const node = wholeMap().nodes.find((n) => n.id === drawer.id);
      if (node) return nodeDrawer(node);
      drawer = { kind: "all" };                       // gone from the new check: all of them
    }
    allDrawer(findings);
  }

  // every finding, worst first; then what the change reaches and what changed, folded
  function allDrawer(findings) {
    const affected = current.affected || [], changes = current.changes || [];
    $("#drawer-title").textContent = `Findings · ${findings.length}`;
    $("#drawer-close").setAttribute("aria-label", "Close the findings");
    fill($("#drawer-body"),
      h("ul", { class: "chips", "aria-label": "In numbers" }, chips(findings, changes, affected)),
      findings.length ? h("ul", { class: "findings" }, F.sorted(findings).map(finding))
        : h("p", { class: "quiet" }, "Nothing to check in what this change touched."),
      fold("What it reaches", affected.length, affected.length
        ? [h("ol", { class: "reach" }, affected.slice(0, MAX_REACH).map(reachRow)),
          affected.length > MAX_REACH && h("p", { class: "quiet more" }, `and ${affected.length - MAX_REACH} more, further away`)]
        : h("p", { class: "quiet" }, "Nothing else in the project calls the code this change edits.")),
      fold("What changed", changes.length, changes.length
        ? h("ul", { class: "changes" }, changes.map(changeRow))
        : h("p", { class: "quiet" }, "No definition changed.")));
  }

  // one dot's findings, and a way to its code and to all the findings
  function nodeDrawer(node) {
    const mine = findingsOf(node), all = ((current && current.findings) || []).length;
    const file = String(node.path || "");
    $("#drawer-title").textContent = node.label || short(node.id);
    $("#drawer-close").setAttribute("aria-label", `Close the findings in ${node.label || short(node.id)}`);
    fill($("#drawer-body"),
      h("p", { class: "d-where" }, h("code", {}, node.id), h("small", {}, `${file}:${node.line} · ${node.kind || "definition"}`)),
      file && h("button", { type: "button", class: "btn primary d-open", onclick: () => open(file, node.line) },
        `Open ${file.split("/").pop()} at line ${node.line}`),
      mine.length ? h("ul", { class: "findings" }, mine.map(finding))
        : h("p", { class: "quiet" }, "No findings here in the latest check."),
      all > mine.length && h("button", { type: "button", class: "link d-all", onclick: () => openDrawer({ kind: "all" }, opener) },
        `All findings (${all}) →`));
  }

  const fold = (title, n, body) => h("details", { class: "fold" },
    h("summary", {}, title, h("span", { class: "count" }, String(n))), body);

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

  $("#findings-btn").addEventListener("click", (e) => {
    if (drawer && drawer.kind === "all") closeDrawer();
    else openDrawer({ kind: "all" }, e.currentTarget);
  });
  $("#drawer-close").addEventListener("click", () => closeDrawer());
  $("#drawer").addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopPropagation();
    closeDrawer();
  });

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
    const first = !current;
    current = report;
    busy(false);
    const findings = report.findings || [], affected = report.affected || [];
    const changes = report.changes || [];
    $("#welcome").hidden = true;
    $("#report").hidden = false;
    $("#again").hidden = false;
    drawVerdict(report, findings, changes, affected);
    drawMap();
    if (first && pending && !locked) follow(pending);       // the cursor arrived before the map
    if (first) reopen();
    drawDrawer();
    drawErrors(report.errors || []);
  }

  // the panel was hidden with the findings open: open them again
  function reopen() {
    const was = saved().drawer;
    if (was === "findings") drawer = { kind: "all" };
    else if (typeof was === "string" && was.startsWith("node:")) drawer = { kind: "node", id: was.slice(5) };
  }

  window.addEventListener("message", (e) => {
    const d = e.data || {};
    if (d.type === "report" && d.report) {
      if ("repo" in d) setRepo(d.repo);
      draw(d.report);
    }
    if (d.type === "repo") setRepo(d.repo);
    if (d.type === "focus") {
      if (current) onFocus(d);
      else pending = d;                               // followed once the first report is drawn
    }
  });
  api.postMessage({ type: "ready" });
})();
