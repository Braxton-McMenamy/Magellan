// The code map of a change, read left to right the way the change spreads:
//
//     what it uses  ->  the change  ->  1 hop  ->  2 hops  ->  ...
//
// Each column after "the change" holds the definitions that call (or read) the column before
// it: the code the change can break, hop by hop, with its score. Drawn as SVG from a report's
// `map` (magellan_lite/web.py). `MagellanMap.render(el, map, opts)` returns { play, finish }:
// play() lights the change and then each hop in turn; finish() jumps to the end.
//
// A team's map (web.team_live) says whose change each node is (`by`): pass
// opts.people = MagellanMap.colours(names) to colour each person's changes their own colour.
// opts.onPick(node) makes the nodes clickable (and reachable with Tab and Enter).

window.MagellanMap = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const svg = (tag, attrs = {}, parent) => {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (parent) parent.append(el);
    return el;
  };
  const MAX_USES = 4;                     // the "uses" column: the change's main dependencies
  // one colour per person: distinct from each other, from the accent and from the "hot" red
  const PALETTE = ["#3d8bfd", "#2fbf71", "#b26bff", "#ff8a3d", "#14b8c4", "#e0559b", "#9aa53a", "#7c8cff"];
  const colours = (names) => Object.fromEntries([...names].map((n, i) => [n, PALETTE[i % PALETTE.length]]));

  // A report without a map (a report.json from the command line): the change and what it
  // reaches, joined by the "why" of each affected definition ("src calls dst (path:line)").
  function fromReport(report) {
    const nodes = new Map();
    const add = (id, extra) => {
      const old = nodes.get(id) || { id, label: id.split(".").pop(), path: "", line: 0,
        kind: "function", change: "", removed: false, score: 0, hops: 0, finding: false };
      nodes.set(id, { ...old, ...extra });
    };
    for (const c of report.changes || []) {
      add(c.name, { change: c.kind, path: c.path, line: c.line, removed: c.kind === "removed" });
    }
    const edges = [];
    for (const a of report.affected || []) {
      add(a.name, { path: a.path, line: a.line, score: a.score, hops: a.hops });
      const [src, kind, dst] = String(a.why).split(" ");
      if (src && dst) {
        if (!nodes.has(dst)) add(dst, {});
        edges.push({ src, dst, kind: kind === "reads" ? "reads" : "calls", guess: /a guess/.test(a.why) });
      }
    }
    for (const f of report.findings || []) {
      for (const n of nodes.values()) if (n.path === f.path && n.line <= f.line) n.finding = true;
    }
    return { nodes: [...nodes.values()], edges };
  }

  // Which column each definition belongs in: -1 what the change uses, 0 the change, n the
  // n-th hop. Everything else in the project is left out: it is not part of this story.
  function columns(map, withUses) {
    const all = (map && map.nodes) || [];
    const edges = ((map && map.edges) || []).filter((e) => e.src !== e.dst);
    const byId = Object.fromEntries(all.map((n) => [n.id, n]));
    const level = new Map();
    for (const n of all) {
      if (n.change || n.finding) level.set(n.id, 0);
      if (n.score > 0 && !n.change) level.set(n.id, n.hops);
    }
    if (!level.size) return null;
    let more = 0;
    if (withUses) {
      const uses = {};
      for (const e of edges) {
        if (level.get(e.src) === 0 && !level.has(e.dst) && byId[e.dst]) uses[e.dst] = (uses[e.dst] || 0) + 1;
      }
      const top = Object.keys(uses).sort((a, b) => uses[b] - uses[a] || a.localeCompare(b));
      top.slice(0, MAX_USES).forEach((id) => level.set(id, -1));
      more = Math.max(0, top.length - MAX_USES);
    }
    const shown = edges.filter((e) => level.has(e.src) && level.has(e.dst));
    // a changed definition connected to nothing (a deleted helper, an unused constant) adds
    // rows, not meaning: leave it out, unless that would leave nothing to show
    const linked = new Set(shown.flatMap((e) => [e.src, e.dst]));
    let nodes = all.filter((n) => level.has(n.id) && (linked.has(n.id) || n.finding));
    if (!nodes.some((n) => level.get(n.id) === 0)) nodes = all.filter((n) => level.has(n.id));
    return { nodes, level, more, edges: shown };
  }

  // Order each column so lines cross as little as possible: by where its neighbours sit.
  function order(cols, edges, level) {
    const nb = new Map();
    for (const e of edges) {
      if (!nb.has(e.src)) nb.set(e.src, []);
      if (!nb.has(e.dst)) nb.set(e.dst, []);
      nb.get(e.src).push(e.dst);
      nb.get(e.dst).push(e.src);
    }
    const rank = new Map();
    const settle = (col) => col.forEach((n, i) => rank.set(n.id, i / Math.max(col.length - 1, 1)));
    cols.forEach((col) => {
      col.sort((a, b) => (b.removed ? 0 : 1) - (a.removed ? 0 : 1) || a.label.localeCompare(b.label));
      settle(col);
    });
    const centre = (n, lv) => {
      const near = (nb.get(n.id) || []).filter((id) => level.get(id) === lv && rank.has(id));
      return near.length ? near.reduce((s, id) => s + rank.get(id), 0) / near.length : rank.get(n.id);
    };
    for (let pass = 0; pass < 3; pass++) {
      for (let i = 1; i < cols.length; i++) {         // left to right, then back
        const lv = level.get(cols[i - 1][0].id);
        cols[i].sort((a, b) => centre(a, lv) - centre(b, lv));
        settle(cols[i]);
      }
      for (let i = cols.length - 2; i >= 0; i--) {
        const lv = level.get(cols[i + 1][0].id);
        cols[i].sort((a, b) => centre(a, lv) - centre(b, lv));
        settle(cols[i]);
      }
    }
  }

  function render(container, map, opts = {}) {
    container.innerHTML = "";
    const width = Math.max(container.clientWidth - 12, 300);
    const picked = columns(map, width >= 520 && opts.uses !== false);
    if (!picked) {
      container.innerHTML = '<p class="map-empty">No definition changed, so there is nothing to follow.</p>';
      return { play() {}, finish() {}, duration: () => 0 };
    }
    const { nodes, level, more, edges } = picked;
    const levels = [...new Set(level.values())].sort((a, b) => a - b);
    const cols = levels.map((lv) => nodes.filter((n) => level.get(n.id) === lv));
    order(cols, edges, level);

    // geometry: columns spread across the box, rows tall enough for a name and a file
    const narrow = width < 520;                         // a phone: short headings, slim margins
    const padX = narrow ? 40 : 64, top = 34, rowH = 64;
    const colW = Math.min(200, (width - 2 * padX) / Math.max(cols.length - 1, 1));
    const x0 = (width - colW * (cols.length - 1)) / 2;
    const tallest = Math.max(...cols.map((c) => c.length));
    const height = top + tallest * rowH + 10;
    const pos = new Map();
    cols.forEach((col, c) => col.forEach((n, i) => pos.set(n.id, {
      x: x0 + c * colW, y: top + (tallest - col.length) * rowH / 2 + i * rowH + 16, c,
    })));
    const fit = Math.max(6, Math.floor((colW - 8) / 6.6));   // characters a label may use
    const clip = (s) => (s.length > fit ? s.slice(0, fit - 1) + "…" : s);

    const root = svg("svg", {
      class: "map", viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": opts.label || "The change and the code it reaches, hop by hop",
    });
    const heads = narrow ? { "-1": "uses", 0: "change" } : { "-1": "what it uses", 0: "the change" };
    levels.forEach((lv, c) => {
      svg("text", { x: x0 + c * colW, y: 14, class: "col-head" }, root).textContent =
        heads[lv] || `${lv} hop${lv === 1 ? "" : "s"}${narrow ? "" : " away"}`;
    });

    // which edges carry the change: a definition reached at hop h, and the one at h-1 it uses
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
    const hot = (e) => {
      const s = byId[e.src], d = byId[e.dst];
      const ld = level.get(e.dst);
      return s && d && s.score > 0 && ld >= 0 && ld === s.hops - 1 && !s.change;
    };

    const gEdges = svg("g", { class: "edges" }, root);
    const gNodes = svg("g", { class: "nodes" }, root);
    const edgeEls = edges.map((e) => {
      let a = pos.get(e.dst), b = pos.get(e.src);       // left: what is used; right: its user
      if (a.c > b.c) [a, b] = [b, a];
      const r = 10;
      const d = a.c === b.c
        ? `M${a.x + r},${a.y} C${a.x + 48},${a.y} ${b.x + 48},${b.y} ${b.x + r},${b.y}`
        : `M${a.x + r},${a.y} C${(a.x + b.x) / 2},${a.y} ${(a.x + b.x) / 2},${b.y} ${b.x - r},${b.y}`;
      const isHot = hot(e);
      const el = svg("path", { d, class: `edge ${e.kind}${e.guess ? " guess" : ""}${isHot ? " impact" : ""}` }, gEdges);
      svg("title", {}, el).textContent = `${e.src} ${e.kind} ${e.dst}${e.guess ? " (a guess)" : ""}`;
      return { el, hops: isHot ? byId[e.src].hops : 0, col: Math.max(a.c, b.c) };
    });

    const nodeEls = nodes.map((n) => {
      const p = pos.get(n.id);
      const g = svg("g", {
        class: ["node", n.kind, n.change && `changed ${n.change}`, n.removed && "removed",
          n.score > 0 && "affected", n.finding && "finding", level.get(n.id) < 0 && "context"]
          .filter(Boolean).join(" "),
        transform: `translate(${p.x},${p.y})`,
      }, gNodes);
      if (n.score > 0) g.style.setProperty("--heat", String(0.3 + 0.7 * n.score));
      const by = (n.by || []).filter((who) => opts.people && opts.people[who]);
      if (by.length) {                    // set through the DOM, not a style="" attribute
        g.classList.add("by");
        g.style.setProperty("--who", opts.people[by[0]]);
        if (by.length > 1) {
          g.classList.add("by-many");     // two people changed it: a ring in the second colour
          g.style.setProperty("--who2", opts.people[by[1]]);
        }
      }
      if (opts.onPick) {
        g.classList.add("pickable");
        g.setAttribute("tabindex", "0");
        g.setAttribute("role", "button");
        g.addEventListener("click", () => opts.onPick(n, g));
        g.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); opts.onPick(n, g); }
        });
      }
      svg("circle", { r: 17, class: "halo" }, g);
      if (n.kind === "class") svg("rect", { x: -8, y: -8, width: 16, height: 16, rx: 4, class: "dot" }, g);
      else if (n.kind === "constant") svg("rect", { x: -6, y: -6, width: 12, height: 12, class: "dot", transform: "rotate(45)" }, g);
      else svg("circle", { r: 8, class: "dot" }, g);
      svg("text", { y: 23, class: "label" }, g).textContent = clip(n.label.split(".").pop());
      svg("text", { y: 35, class: "file" }, g).textContent = clip(n.path.split("/").pop());
      if (n.score > 0) svg("text", { x: 12, y: -8, class: "score" }, g).textContent = n.score.toFixed(2);
      if (n.finding) {
        const b = svg("g", { class: "badge", transform: "translate(-11,-10)" }, g);
        svg("circle", { r: 7 }, b);
        svg("text", { y: 3.5 }, b).textContent = "!";
      }
      const what = n.change ? ` · ${n.change}` : n.score > 0
        ? ` · reached, score ${n.score.toFixed(2)} (${n.hops} hop${n.hops === 1 ? "" : "s"})` : " · used by the change";
      const who = by.length ? ` · changed by ${by.join(" and ")}` : "";
      svg("title", {}, g).textContent = `${n.id}${what}${who}${n.finding ? " · a finding is here" : ""}\n${n.path}:${n.line}`;
      return { g, n, col: p.c };
    });
    if (more) {
      const c = levels.indexOf(-1);
      svg("text", { x: x0 + c * colW, y: height - 4, class: "file" }, root).textContent = `+ ${more} more`;
    }

    container.append(root);
    if (opts.legend !== false) {
      const legend = document.createElement("div");
      legend.className = "map-legend";
      legend.innerHTML = `<span><i class="lg changed"></i>changed</span><span><i class="lg affected"></i>reached by the change (its score)</span><span><i class="lg finding"></i>a finding</span><span><i class="lg spread"></i>the change spreading to its callers</span>`;
      if (opts.people) {                  // names come from the repository: text, never HTML
        legend.firstElementChild.remove();
        for (const [name, colour] of Object.entries(opts.people).reverse()) {
          const item = document.createElement("span");
          const dot = document.createElement("i");
          dot.className = "lg person";
          dot.style.setProperty("--who", colour);
          item.append(dot, `changed by ${name}`);
          legend.prepend(item);
        }
      }
      container.append(legend);
    }

    let timers = [];
    const later = (ms, fn) => timers.push(setTimeout(fn, ms));
    const clear = () => { timers.forEach(clearTimeout); timers = []; };
    const maxHop = Math.max(0, ...nodes.map((n) => (n.score > 0 && !n.change ? n.hops : 0)));

    function finish() {
      clear();
      root.classList.remove("playing");
      root.classList.add("shown", "lit");
      nodeEls.forEach(({ g }) => g.classList.add("in", "lit"));
      edgeEls.forEach(({ el }) => el.classList.add("in", "lit"));
    }

    function play() {
      if (reduced()) return finish();
      clear();
      root.classList.remove("lit");
      root.classList.add("playing", "shown");
      nodeEls.forEach(({ g }) => g.classList.remove("in", "lit"));
      edgeEls.forEach(({ el }) => el.classList.remove("in", "lit"));
      // 1. the columns appear, left to right; 2. the change pulses; 3. each hop lights up
      nodeEls.forEach(({ g, col }, i) => later(60 + col * 160 + (i % 6) * 25, () => g.classList.add("in")));
      edgeEls.forEach(({ el, col }) => later(200 + col * 160, () => el.classList.add("in")));
      const t0 = 500 + cols.length * 160;
      nodeEls.filter(({ n }) => n.change).forEach(({ g }) => later(t0, () => g.classList.add("lit")));
      for (let h = 1; h <= maxHop; h++) {
        const t = t0 + 500 + (h - 1) * 850;
        edgeEls.filter((x) => x.hops === h).forEach(({ el }) => later(t, () => el.classList.add("lit")));
        nodeEls.filter(({ n }) => n.score > 0 && n.hops === h).forEach(({ g }) => later(t + 450, () => g.classList.add("lit")));
      }
      const tEnd = t0 + 600 + maxHop * 850;
      nodeEls.filter(({ n }) => n.finding).forEach(({ g }) => later(tEnd, () => g.classList.add("lit")));
      later(tEnd + 200, () => { root.classList.remove("playing"); root.classList.add("lit"); });
      return tEnd;
    }

    if (opts.animate === false) finish();
    return { play, finish, duration: () => 1300 + cols.length * 160 + 850 * maxHop };
  }

  return { fromReport, render, colours };
})();
