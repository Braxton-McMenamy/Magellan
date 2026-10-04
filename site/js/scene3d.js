// The 3D view of a Magellan Lite map: the whole project as clusters on a sphere (js/graph3d.js),
// coloured by the change. It takes the same map as the 2D flow view (js/map.js) and the same
// options, so a page can switch between the two:
//
//   const view = MagellanScene3D.render(box, map, { people, onPick(node), onOpen(node), onContext(node, event), label });
//   view.destroy();
//
// The 2D flow view reads the change hop by hop; this one shows where it sits in the whole
// project. Drag to turn, wheel to fly, double-click to open, F to fly to the picked dot,
// Home to see everything again. The skull lights up code nothing in the project calls or reads
// (opts.onDead(list) hears about it, to list it beside the view). opts.onContext hears a
// right-click on a dot -- a click, not a right-drag, which pans -- or the menu key on the picked
// one, so the page can offer its sub-graph (js/subgraph-ui.js).

window.MagellanScene3D = (() => {
  const G = window.MagellanGraph3D;
  const KIND = { function: "function", method: "method", class: "class", constant: "global_var" };
  const CLUSTERS = [["language", "Language"], ["directory", "Folder"], ["community", "Who calls whom"]];
  const SKULL = "#c9b6ff";

  /**
   * Definitions that are probably dead. The engine marks them (``unused``: the name appears
   * nowhere else in the project, magellan_lite/web.py); a map from before it did is judged here
   * from its edges: nothing in the project calls or reads them, and they are not something used
   * from outside. In Python that means private ones (``_name``): a public
   * function with no caller here is often the API, and attributes read as ``obj.field`` leave no
   * trace in the map. In the other languages, functions nobody calls, except entry points
   * (``main``, a JNI export, a COBOL program).
   */
  function orphans(map) {
    const all = (map && map.nodes) || [];
    const order = (a, b) => `${a.path}:${String(a.line).padStart(6, "0")}`.localeCompare(`${b.path}:${String(b.line).padStart(6, "0")}`);
    if (all.some((n) => "unused" in n)) return all.filter((n) => n.unused && !n.removed).sort(order);
    const used = new Set();
    for (const e of (map && map.edges) || []) used.add(e.dst);
    const classes = new Set(((map && map.nodes) || []).filter((n) => n.kind === "class").map((n) => n.id));
    const dead = (n) => {
      if (used.has(n.id) || n.removed) return false;
      const short = String(n.label || n.id).split(".").pop().split("(")[0];
      const lang = n.lang || "python";
      if (lang === "python") {
        if (!short.startsWith("_") || /^__\w+__$/.test(short)) return false;
        if (/(^|\/)(tests?\/|test_|conftest)/.test(n.path || "")) return false;
        const parent = String(n.id).slice(0, String(n.id).lastIndexOf("."));
        return !(n.kind === "constant" && classes.has(parent));        // a class's field, read as obj.field
      }
      if (n.kind !== "function" && n.kind !== "method") return false;
      if (/^(main|Java_)/.test(short) || short === "<init>") return false;
      return !(lang === "cobol" && !String(n.id).split("@").pop().includes("."));   // a PROGRAM-ID
    };
    return ((map && map.nodes) || []).filter(dead)
      .sort((a, b) => `${a.path}:${String(a.line).padStart(6, "0")}`.localeCompare(`${b.path}:${String(b.line).padStart(6, "0")}`));
  }

  /** Lite's map as graph3d's data: names, how many depend on each node, the change's centre. */
  function toData(map) {
    const nodes = (map && map.nodes) || [], edges = (map && map.edges) || [];
    const into = new Map();
    for (const e of edges) into.set(e.dst, (into.get(e.dst) || 0) + 1);
    const out = nodes.map((n) => ({
      ...n, name: n.label || String(n.id).split(".").pop(), kind: KIND[n.kind] || n.kind,
      dependents: into.get(n.id) || 0, impact: n.change ? 0 : n.score || 0,
    }));
    // a sub-graph (js/subgraph.js) centres on its seed: the most depended-on one of a file's
    const seeds = nodes.filter((n) => n.seed && !n.removed).sort((a, b) => (into.get(b.id) || 0) - (into.get(a.id) || 0));
    const centre = seeds[0] || nodes.find((n) => n.change && !n.removed) || nodes.find((n) => n.finding);
    return {
      nodes: out,
      edges: edges.map((e) => ({ src: e.src, dst: e.dst, kind: e.kind, confidence: e.guess ? 0.5 : 1 })),
      center: centre ? centre.id : null,
    };
  }

  /**
   * A right-click on a dot, for opts.onContext. A right-drag pans (graph3d), so only a press and
   * release in the same place counts, judged on the release: some systems send "contextmenu" on
   * the press, before anyone knows it is a drag. From the keyboard, the menu key or Shift+F10
   * asks about the picked dot, and the menu opens where it is drawn.
   */
  function context(canvas, view, fn) {
    let down = null;
    canvas.addEventListener("mousedown", (e) => {
      if (e.button !== 2) return;
      const r = canvas.getBoundingClientRect();
      down = { x: e.clientX, y: e.clientY, node: view.hit(e.clientX - r.left, e.clientY - r.top) };
    });
    canvas.addEventListener("mouseup", (e) => {
      const d = down;
      down = null;
      if (e.button !== 2 || !d || !d.node || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 4) return;
      fn(d.node, e);
    });
    canvas.addEventListener("keydown", (e) => {
      if (!(e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) || !view.selected) return;
      e.preventDefault();
      const n = view.selected, p = view.project([n.x, n.y, n.z]), r = canvas.getBoundingClientRect();
      fn(n, { type: "keyboard", target: canvas,
        clientX: r.left + (p ? p.x : r.width / 2), clientY: r.top + (p ? p.y : r.height / 2) });
    });
  }

  const el = (tag, cls, text) => {
    const x = document.createElement(tag);
    if (cls) x.className = cls;
    if (text !== undefined) x.textContent = text;
    return x;
  };

  function render(box, map, opts = {}) {
    if (box._scene3d) box._scene3d.destroy();
    box.replaceChildren();
    const data = toData(map);
    if (!data.nodes.length) {
      box.append(el("p", "map-empty", "Nothing to draw yet."));
      return { destroy() {}, play() {}, finish() {} };
    }
    const changed = data.nodes.some((n) => n.change || n.score > 0 || n.finding);
    const langs = new Set(data.nodes.map((n) => G.languageOf(n)));
    let cluster = opts.cluster || (langs.size > 1 ? "language" : "community");
    let colour = changed ? "change" : "cluster";

    const wrap = el("div", "scene3d");
    const canvas = el("canvas", "scene3d-canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", opts.label || "The project in 3D: drag to turn, wheel to fly, double-click to open");
    canvas.tabIndex = 0;
    wrap.append(canvas);
    box.append(wrap);

    const byId = new Map(((map && map.nodes) || []).map((n) => [n.id, n]));
    const view = new G.GraphView3D(canvas, {
      onSelect: (n) => opts.onPick && n && opts.onPick(byId.get(n.id) || n, null),
      onOpen: (n) => (opts.onOpen || opts.onPick) && n && (opts.onOpen || opts.onPick)(byId.get(n.id) || n, null),
    }, { cluster, colorBy: colour, people: opts.people || null });
    view.setData(data);

    // the toolbar: fit, how to group, how to colour, spin
    const bar = el("div", "scene3d-bar");
    const button = (text, title, fn) => {
      const b = el("button", "scene3d-btn", text);
      b.type = "button"; b.title = title;
      b.addEventListener("click", fn);
      bar.append(b);
      return b;
    };
    button("Fit", "See everything (Home)", () => view.fit());
    const group = el("select", "scene3d-select");
    group.title = "Group the dots by";
    for (const [k, label] of CLUSTERS) {
      const o = el("option", "", label); o.value = k; o.selected = k === cluster; group.append(o);
    }
    group.addEventListener("change", () => { cluster = group.value; view.setCluster(cluster); legend(); });
    bar.append(group);
    const tint = button(colour === "change" ? "Colour: change" : "Colour: groups", "Colour by the change, or by group", () => {
      colour = colour === "change" ? "cluster" : "change";
      view.setColorBy(colour);
      tint.textContent = colour === "change" ? "Colour: change" : "Colour: groups";
      legend();
    });
    if (!changed) tint.hidden = true;
    // the skull: code nothing calls or reads
    const dead = orphans(map);
    let skullOn = false;
    const skull = button(`\u{1F480} ${dead.length}`, `Probably dead code: ${dead.length} definitions nothing in the project uses`, () => {
      skullOn = !skullOn;
      skull.setAttribute("aria-pressed", String(skullOn));
      view.setSpotlight(skullOn ? dead.map((n) => n.id) : null, SKULL);
      legend();
      if (opts.onDead) opts.onDead(skullOn ? dead : null);
    });
    skull.classList.add("scene3d-skull");
    skull.setAttribute("aria-pressed", "false");
    skull.setAttribute("aria-label", `Show probably dead code: ${dead.length} definitions`);
    if (!dead.length) skull.disabled = true;
    const spin = button("Spin", "Turn slowly on its own", () => {
      view.setAutoRotate(!view.autoRotate);
      spin.setAttribute("aria-pressed", String(view.autoRotate));
    });
    spin.setAttribute("aria-pressed", "false");
    wrap.append(bar);

    // the legend: what the colours mean right now
    const key = el("div", "scene3d-legend");
    wrap.append(key);
    const swatch = (colourValue, text) => {
      const item = el("span", "scene3d-key");
      const dot = el("i");
      dot.style.setProperty("--c", colourValue);
      item.append(dot, text);
      key.append(item);
    };
    function legend() {
      key.replaceChildren();
      if (skullOn) {
        swatch(SKULL, `nothing in the project uses it · ${dead.length}`);
        key.append(el("span", "scene3d-key note", "an entry point, a plugin or a call by name may still use it"));
        return;
      }
      if (colour === "change") {
        if (opts.people) for (const [name, c] of Object.entries(opts.people)) swatch(c, `changed by ${name}`);
        else swatch(G.CHANGE.changed, "changed");
        swatch(G.CHANGE.hot, "reached by the change");
        const ring = el("span", "scene3d-key ring"); ring.append(el("i"), "a finding"); key.append(ring);
        swatch("#3a4a5e", "the rest of the project");
      } else {
        for (const c of view.legend().slice(0, 8)) swatch(c.color, `${c.key} · ${c.size}`);
      }
    }
    legend();
    wrap.append(el("p", "scene3d-hint", "drag to turn · wheel to fly · double-click to open · F to fly to the picked dot"
      + (opts.onContext ? " · right-click: sub-graph" : "")));
    if (opts.onContext) context(canvas, view, (n, at) => opts.onContext(byId.get(n.id) || n, at));

    // keep the canvas sized to its box
    const resize = () => view.resize();
    let watcher = null;
    if (typeof ResizeObserver === "function") { watcher = new ResizeObserver(resize); watcher.observe(wrap); }
    // the change's centre first: fly to it once the layout has settled a little
    // a big project: fly toward the change once the layout has settled a little (a small one is
    // all on screen already)
    if (data.center && data.nodes.length > 80 && opts.focus !== false) {
      setTimeout(() => {
        const n = view.model.byId.get(data.center);
        if (n) view.flyTo([n.x, n.y, n.z], Math.max(260, view.model.extent() * 0.9));
      }, 900);
    }

    const handle = {
      view,
      play() { view.start(); },
      finish() { view.start(); },
      destroy() { if (watcher) watcher.disconnect(); view.destroy(); delete box._scene3d; },
    };
    box._scene3d = handle;
    return handle;
  }

  return { render, toData, orphans };
})();
