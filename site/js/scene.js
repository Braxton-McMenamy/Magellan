// The Scene (scene.html): one map, as big as the screen, laid out like a 3D editor -- what to
// show on the left, the viewport in the middle, the picked dot's properties on the right. The
// whole map is 3D (the whole project, js/scene3d.js): as a flow, a whole project is too big to
// read. A neighbourhood -- a sub-graph, or what depends on a dot -- can also be read as a Flow
// (hop by hop, js/map.js). What it shows:
//   #team            your team's work in progress, everyone at once (the Team suite's connection)
//   #team/<name>     one person's work
//   #itself          Magellan Lite's own code (data/project.js): there before anything is connected
//
// Pick a dot, then "What depends on it": Flow shows everything that would feel a change to it,
// hop by hop -- the question to ask before changing old code.
//
// Right-click a dot (or pick it and press "Show sub-graph") and its neighbourhood opens as a
// scene of its own, in a tab above the viewport (js/subgraph.js): its own map, camera and view,
// the same tools. A file's sub-graph is everything in the file and what touches it. The first
// tab is the whole map; [ and ] step through the tabs. The address follows the tab shown, so a
// sub-graph can be linked to:
//   #itself/sub=<id>       one definition's sub-graph (the id URL-encoded)
//   #team/file=<path>      a file's

(() => {
  const { h, fill, verdict, plural, finding } = MagellanDom;
  const SG = MagellanSubgraph, UI = MagellanSubgraphUI;
  const $ = (id) => document.getElementById(id);

  const VIEWS = {
    "3d": { label: "3D", draw: (box, map, opts) => MagellanScene3D.render(box, map, opts) },
    flow: { label: "Flow", draw: (box, map, opts) => MagellanMap.render(box, map, opts) },
  };
  // the view last chosen for a neighbourhood (the whole map is always 3D)
  const remember = {
    get() { try { return sessionStorage.getItem("magellan-scene-view") || ""; } catch { return ""; } },
    set(v) { try { sessionStorage.setItem("magellan-scene-view", v); } catch { /* private mode */ } },
  };
  const firstView = () => (VIEWS[remember.get()] ? remember.get() : "3d");
  /** A tab whose map is a neighbourhood, which Flow can read: a sub-graph, or what depends on a dot. */
  const near = (tab) => !!tab && (!!tab.spec || !!tab.following);

  const itself = window.MAGELLAN_PROJECT || null;
  let team = null, people = {}, current = null;

  /** The map as if ``id`` had changed: what calls it, then what calls those, hop by hop. */
  function reachFrom(map, id) {
    const callers = new Map();
    for (const e of map.edges) (callers.get(e.dst) || callers.set(e.dst, []).get(e.dst)).push(e.src);
    const hops = new Map([[id, 0]]);
    let frontier = [id];
    for (let h = 1; h <= 8 && frontier.length; h++) {
      const next = [];
      for (const x of frontier) for (const c of callers.get(x) || []) if (!hops.has(c)) { hops.set(c, h); next.push(c); }
      frontier = next;
    }
    return {
      nodes: map.nodes.map((n) => {
        const h = hops.get(n.id);
        return { ...n, finding: false, change: n.id === id ? "picked" : "",
          hops: h || 0, score: h ? Number((0.9 ** h).toFixed(2)) : 0 };
      }),
      edges: map.edges,
      dependents: hops.size - 1,
    };
  }

  // -- what can be shown -------------------------------------------------------------------------
  // each source: { key, title, sub, report: { verdict, findings, map }, people? }
  function source(key) {
    const [kind, name] = key.split("/");
    if (kind === "itself" && itself) {
      return { key, title: "Magellan Lite itself",
        sub: `${itself.files} files, ${itself.map.nodes.length} definitions · connect your repository in the Team suite to see yours`,
        report: { verdict: "ok", findings: [], map: itself.map } };
    }
    if (kind === "team" && team && team.team) {
      if (!name && !team.people.length) {
        return { key, title: "Your project", sub: `${team.repo} · nobody is sharing work in progress yet`,
          report: team.team };
      }
      if (!name) {
        return { key, title: "Everyone at once", people,
          sub: `${team.repo} · ${plural(team.people.length, "person", "people")} sharing · each colour is one person's change`,
          report: team.team };
      }
      const m = team.members.find((x) => x.name === name);
      if (m) return { key, title: `${m.name}'s work in progress`, sub: team.repo, report: m };
    }
    return null;
  }
  const shownSource = () => (current ? source(current) : null);

  function outliner() {
    const item = (key, label, v, colour) => {
      const dot = colour ? h("i", { class: "who-dot", "aria-hidden": "true" }) : null;
      if (dot) dot.style.setProperty("--who", colour);
      return h("li", {}, h("button", { type: "button", "aria-pressed": String(current === key),
        onclick: () => { location.hash = key; } }, dot, h("span", {}, label), v ? verdict(v) : null));
    };
    const saved = MagellanTeam.saved();
    fill($("rail-team"), ...(team && team.team
      ? [item("team", team.people.length ? "Everyone at once" : "Your project", team.team.verdict),
        ...team.members.map((m) => item(`team/${m.name}`, m.name, m.verdict, people[m.name]))]
      : [h("li", { class: "note" }, saved.repo
        ? (team ? "Nobody is sharing work in progress yet." : `Reading ${saved.repo}…`)
        : h("span", {}, h("a", { href: "suite.html" }, "Connect your repository"), " in the Team suite, and it shows here."))]));
    fill($("rail-project"), itself ? item("itself", "Magellan Lite itself") : null);
  }

  // -- the properties panel ------------------------------------------------------------------------
  function properties(src, node) {
    const box = $("scene-side");
    const r = src.report;
    if (!node) {
      return fill(box,
        h("h2", {}, "The checklist"),
        h("p", {}, verdict(r.verdict), " ", r.findings && r.findings.length ? plural(r.findings.length, "finding") : "nothing to check"),
        r.findings && r.findings.length ? h("ul", { class: "findings" }, r.findings.map(finding)) : null,
        h("p", { class: "small" }, "Pick a dot to see what it is. Right-click one for its sub-graph."));
    }
    const by = node.by || [], reached = node.reached_by || [];
    const here = (r.findings || []).filter((f) => f.path === node.path && f.line >= node.line);
    const who = (n) => {
      const d = h("i", { class: "who-dot", "aria-hidden": "true" });
      d.style.setProperty("--who", people[n] || "var(--stage-muted)");
      return [d, n];
    };
    fill(box,
      h("h2", {}, node.label || node.id),
      h("p", { class: "where" }, `${node.path}:${node.line} · ${node.kind}${node.lang && node.lang !== "python" ? ` · ${node.lang}` : ""}`),
      h("p", { class: "small muted-id" }, node.id),
      h("p", {}, node.change ? `Changed: ${node.change}${node.removed ? " (deleted)" : ""}` : node.score > 0
        ? `Reached by the change, ${plural(node.hops, "hop")} away. Score ${node.score.toFixed(2)}.`
        : r.findings && r.findings.length ? "Not touched by the change." : "Part of the project."),
      by.length ? h("p", {}, "Changed by ", ...by.flatMap((n, i) => [i ? " and " : "", ...who(n)])) : null,
      reached.length ? h("p", {}, "Reached by ", reached.join(" and "), "'s change") : null,
      node.finding && here.length ? h("ul", { class: "findings" }, here.slice(0, 4).map(finding)) : null,
      h("div", { class: "node-actions" },
        h("button", { type: "button", class: "follow-btn", onclick: () => follow(node) }, "What depends on it →"),
        h("button", { type: "button", class: "sub-btn", title: "It, what depends on it and what it uses, in a tab of its own",
          onclick: () => openSub({ ids: [node.id] }) }, "Show sub-graph"),
        node.path ? h("button", { type: "button", class: "sub-btn", title: `Everything in ${node.path}, and what touches it`,
          onclick: () => openSub({ file: node.path }) }, "Sub-graph of its file") : null),
      h("p", {}, h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); pick(null); } },
        active && active.spec && active.spec.file ? `← ${active.spec.file.split("/").pop()}` : "← the whole checklist")));
  }

  /** Fly the shown 3D view to a dot and mark it (Flow has nowhere to fly). */
  function fly(n) {
    const v = active && active.shown && active.shown.view;
    const node = v && v.model.byId.get(n.id);
    if (node) { v.setSelected(n.id); v.flyToNode(node); }
  }

  /** The skull's list: code nothing in the project uses. Each one flies the 3D view to it. */
  function deadPanel(src, list) {
    fill($("scene-side"),
      h("h2", {}, "\u{1F480} Probably dead: ", String(list.length)),
      h("p", { class: "small" }, "Nothing in the project mentions these names except where they are defined. "
        + "An entry point, a plugin, or a call by a computed name may still use them: check before deleting."),
      h("ul", { class: "dead-list" }, list.map((n) => h("li", {},
        h("button", { type: "button", onclick: () => fly(n) },
          h("span", { class: "dead-name" }, n.label || n.id),
          h("span", { class: "where" }, `${n.path}:${n.line}`))))),
      h("p", {}, h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); overview(src); } }, "← the whole checklist")));
  }

  /** A file's sub-graph with nothing picked: what the file defines, each one a button to it. */
  function filePanel(src, tab) {
    const mine = tab.sub.nodes.filter((n) => n.seed).sort((a, b) => a.line - b.line);
    fill($("scene-side"),
      h("h2", {}, tab.spec.file),
      h("p", { class: "small" }, `What it defines: ${plural(mine.length, "definition")}. Pick one to see it.`),
      h("ul", { class: "dead-list" }, mine.map((n) => h("li", {},
        h("button", { type: "button", onclick: () => { fly(n); pick(n); } },
          h("span", { class: "def-name" }, n.label || n.id),
          h("span", { class: "where" }, `line ${n.line} · ${n.kind}`))))),
      h("p", {}, h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); properties(src); } }, "← the whole checklist")));
  }

  /** The side panel when nothing is picked: a file's definitions, or the checklist. */
  function overview(src, tab = active) {
    if (tab && tab.spec && tab.spec.file && tab.sub) return filePanel(src, tab);
    return properties(src);
  }

  /** The source map's own node: a view may have redrawn it (Flow's "picked", its hops). */
  function original(src, node) {
    return (node && src.report.map.nodes.find((n) => n.id === node.id)) || node;
  }

  /** A dot was picked in the shown tab (null: none): its properties, kept with the tab. */
  function pick(node, g) {
    const src = shownSource();
    if (!src || !active) return;
    active.pane.querySelectorAll(".picked").forEach((x) => x.classList.remove("picked"));
    if (g) g.classList.add("picked");
    active.picked = node ? original(src, node) : null;
    if (active.picked) properties(src, active.picked); else overview(src);
  }

  /** Flow, from one dot: everything in the whole map that depends on it. */
  function follow(node) {
    const main = tabs[0];
    if (!main) return;
    main.following = node;
    main.picked = node;
    main.view = "flow";
    main.stale = true;
    remember.set("flow");
    activate(main);
  }

  /** The right-click menu on a dot: its sub-graph, its file's, what depends on it. */
  function menu(node, event, g) {
    pick(node, g);
    const file = node.path ? node.path.split("/").pop() : "";
    UI.menu(event, node.label || node.id, [
      { label: "Show sub-graph", run: () => openSub({ ids: [node.id] }) },
      node.path && { label: "Sub-graph of its file", detail: file, run: () => openSub({ file: node.path }) },
      { label: "What depends on it", run: () => follow(original(shownSource(), node)) },
    ]);
  }

  // -- the tabs: the whole map, then each sub-graph opened from it ----------------------------------
  // a tab: { id, spec (null: the whole map; { ids } or { file }: a sub-graph), view ("3d" or
  // "flow"), pane (its own box: a hidden tab's camera stays where it was), shown (what the
  // renderer returned), sub (the sub-graph drawn), head ([title, line] above the viewport),
  // picked, following (the whole map's: the dot whose dependents Flow shows), stale }
  let tabs = [], active = null, tabsOf = null, seq = 0, visited = [];

  function newTab(spec, view) {
    const pane = h("div", { class: "scene-pane", hidden: true });
    $("scene-map").append(pane);
    return { id: `t${seq++}`, spec, view, pane, shown: null, sub: null, head: ["", ""],
      picked: null, following: null, stale: true };
  }

  function dropTab(t) {
    if (t.shown && t.shown.destroy) t.shown.destroy();
    t.pane.remove();
  }

  /** A new source: its whole map alone, in 3D. */
  function resetTabs(key) {
    tabs.forEach(dropTab);
    tabs = key ? [newTab(null, "3d")] : [];
    active = null;
    visited = [];
    tabsOf = key;
  }

  /** What a tab's map is now: the whole map, the dependents of a dot, or a sub-graph. */
  function mapFor(src, tab) {
    let map = src.report.map;
    if (tab.spec) {
      const sub = SG.build(map, tab.spec);
      if (sub.nodes.length || !tab.sub) tab.sub = sub;      // gone from a newer map: keep the last
      tab.head = [SG.title(tab.sub), SG.describe(tab.sub) + (sub.nodes.length ? "" : " (Not in the latest map.)")];
      return tab.view === "flow" ? SG.flow(tab.sub) : tab.sub;
    }
    tab.head = [src.title, src.sub];
    if (tab.view === "flow" && tab.following && map.nodes.some((n) => n.id === tab.following.id)) {
      map = reachFrom(map, tab.following.id);
      tab.head = [`What depends on ${tab.following.label || tab.following.id}`,
        `${plural(map.dependents, "definition")} would feel a change to it · ${tab.following.path}:${tab.following.line}`];
    } else tab.view = "3d";                               // the whole map: 3D only
    return map;
  }

  /** Draw a tab into its pane (it is on screen: the renderers size to it). */
  function render(tab, animate = true) {
    const src = shownSource();
    if (!src) return;
    if (tab.shown && tab.shown.destroy) tab.shown.destroy();
    const map = mapFor(src, tab);
    tab.shown = VIEWS[tab.view].draw(tab.pane, map, {
      people: src.people, label: tab.head[0], animate,
      heads: tab.spec ? SG.heads(tab.sub) : undefined,
      onDead: (list) => (list ? deadPanel(src, list) : tab.picked ? properties(src, tab.picked) : overview(src, tab)),
      onPick: (n, g) => pick(n, g),
      onContext: (n, ev, g) => menu(n, ev, g),
    });
    tab.stale = false;
    tab.width = tab.pane.clientWidth;
    if (animate && tab.shown && tab.shown.play) tab.shown.play();
  }

  /** Show a tab: its pane, its title, its view's switch, its picked dot; the address follows. */
  function activate(tab, animate = true) {
    UI.close();
    if (active && active !== tab) {
      active.pane.hidden = true;
      if (active.shown && active.shown.view) active.shown.view.stop();     // a hidden 3D view rests
    }
    active = tab;
    visited = visited.filter((t) => t !== tab).concat(tab);
    tab.pane.hidden = false;
    document.querySelector(".scene-stage").classList.toggle("is-3d", tab.view === "3d");
    if (tab.stale || !tab.shown) render(tab, animate);
    else if (tab.shown.view) { tab.shown.view.resize(); tab.shown.view.dirty = true; tab.shown.view.start(); }
    // the whole map falls back to 3D when the dot it followed left the map
    document.querySelector(".scene-stage").classList.toggle("is-3d", tab.view === "3d");
    const src = shownSource();
    $("scene-title").textContent = tab.head[0];
    $("scene-sub").textContent = tab.head[1];
    document.title = `${tab.head[0]} · Scene · Magellan Lite`;
    switcher();
    strip();
    const seed = tab.spec && !tab.spec.file && tab.sub && tab.sub.seeds.length === 1 ? original(src, { id: tab.sub.seeds[0] }) : null;
    if (tab.picked || seed) properties(src, tab.picked || seed); else overview(src, tab);
    address(tab);
  }

  /** Open a sub-graph in a tab (or show the tab that has it). False when nothing in the map matches. */
  function openSub(spec, animate = true) {
    const src = shownSource();
    if (!src || !tabs.length) return false;
    const had = tabs.find((t) => SG.same(t.spec, spec));
    if (had) { activate(had, animate); return had; }
    if (!SG.build(src.report.map, spec).nodes.length) return false;      // an unknown id: ignored
    const tab = newTab(spec, near(active) ? active.view : firstView());
    tabs.push(tab);
    activate(tab, animate);
    return tab;
  }

  /** Close a sub-graph's tab, back to the one shown before it. */
  function closeTab(tab) {
    if (!tab.spec) return;
    dropTab(tab);
    tabs = tabs.filter((t) => t !== tab);
    visited = visited.filter((t) => t !== tab);
    if (active === tab) { active = null; activate(visited[visited.length - 1] || tabs[0], false); } else strip();
  }

  function step(by) {
    if (tabs.length < 2 || !active) return;
    activate(tabs[(tabs.indexOf(active) + by + tabs.length) % tabs.length], false);
  }

  function strip() {
    const src = shownSource();
    UI.strip($("scene-tabs"), tabs.map((t) => ({
      id: t.id, closable: !!t.spec,
      title: t.spec ? t.head[0] || "Sub-graph" : "Whole map",
      hint: t.spec ? t.head[1] : `${src ? src.title : "The map"}: everything`,
    })), active && active.id, { onPick: (t) => activate(tabs.find((x) => x.id === t.id), false),
      onClose: (t) => closeTab(tabs.find((x) => x.id === t.id)),
      away: () => active && active.pane.querySelector("canvas, .node[tabindex]") });
    document.querySelector(".scene-layout").classList.toggle("has-tabs", tabs.length > 1);
  }

  // -- the address: #<source>[/sub=<id> | /file=<path>] ---------------------------------------------
  const decode = (s) => { try { return decodeURIComponent(s); } catch { return ""; } };

  function route() {
    const raw = location.hash.slice(1);
    const at = raw.search(/\/(sub|file)=/);
    const key = decode(at < 0 ? raw : raw.slice(0, at));
    if (at < 0) return { key, spec: null };
    const [kind, ...rest] = raw.slice(at + 1).split("=");
    const value = decode(rest.join("="));
    return { key, spec: !value ? null : kind === "file" ? { file: value } : { ids: [value] } };
  }

  function address(tab) {
    const spec = tab.spec;
    const want = `#${current.split("/").map(encodeURIComponent).join("/")}${!spec ? ""
      : spec.file ? `/file=${encodeURIComponent(spec.file)}` : `/sub=${encodeURIComponent(spec.ids[0])}`}`;
    if (location.hash === want) return;
    try { history.replaceState(null, "", want); } catch { /* opened from disk, in some browsers */ }
  }

  // -- the viewport ---------------------------------------------------------------------------------
  function draw(animate = true) {
    const fallback = team && team.team ? "team" : itself ? "itself" : "";
    let { key, spec } = route();
    key = key || fallback;
    let src = source(key);
    if (!src && !key.startsWith("team")) { src = source((key = fallback)); spec = null; }
    current = src ? key : current;
    outliner();
    if (!src) {
      resetTabs(null);
      strip();
      switcher();
      $("scene-title").textContent = "Scene";
      $("scene-sub").textContent = key.startsWith("team") ? "Waiting for your team's work…" : "";
      fill($("scene-side"));
      return;
    }
    if (tabsOf !== key) resetTabs(key);
    if (!spec || !openSub(spec, animate)) activate(tabs[0], animate);
  }

  /** Flow or 3D, for a neighbourhood; none for the whole map, which is 3D only. */
  function switcher() {
    $("views").hidden = !near(active);
    fill($("views"), ...Object.entries(VIEWS).map(([k, v]) => h("button", {
      type: "button", "aria-pressed": String(!!active && k === active.view), disabled: !active,
      title: k === "3d" ? (active && active.spec ? "In 3D" : "The whole project in 3D") : "Hop by hop",
      onclick: () => {
        if (!active) return;
        active.view = k;
        remember.set(k);
        if (k === "3d") active.following = null;
        active.stale = true;
        activate(active);
      },
    }, v.label)));
  }

  // -- the team, live, when the suite connected one in this tab --------------------------------------
  const saved = MagellanTeam.saved();
  if (saved.repo) {
    try {
      MagellanTeam.connect({
        repo: saved.repo, token: saved.token,
        onStatus: (t) => { $("scene-status").textContent = t; },
        onResult: (r) => {
          team = r;
          people = MagellanMap.colours(r.people.map((p) => p.name));
          $("scene-status").textContent = `${r.repo} · updated ${new Date(r.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
          if (tabsOf && tabsOf.startsWith("team")) tabs.forEach((t) => { t.stale = true; });
          if (!location.hash || location.hash.startsWith("#team")) draw(); else outliner();
        },
        onError: (err) => { $("scene-status").textContent = err.message; },
      });
    } catch (err) {
      $("scene-status").textContent = err.message;
    }
  }

  addEventListener("hashchange", () => {
    const main = tabs[0];
    if (main && main.following) { main.following = null; main.view = "3d"; main.stale = true; }
    draw();
  });
  UI.keys(step);
  let resized = null;
  addEventListener("resize", () => {
    if (!active || active.view !== "flow") return;     // the 3D view resizes itself
    clearTimeout(resized);
    resized = setTimeout(() => {
      if (active && active.view === "flow" && Math.abs(active.pane.clientWidth - active.width) > 24) render(active, false);
    }, 200);
  });
  draw();
  window.MagellanScene = { VIEWS, redraw: draw, openSub, get tabs() { return tabs; }, get active() { return active; } };
})();
