// The code map: every definition, who calls or reads whom, and what a change reaches.
// Drawn as SVG from a report's `map` (magellan_lite/web.py). `MagellanMap.render(el, map)`
// returns { play, finish }: play() animates the change spreading backwards through its
// callers, hop by hop; finish() jumps to the end. No dependencies.

window.MagellanMap = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const svg = (tag, attrs = {}, parent) => {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (parent) parent.append(el);
    return el;
  };

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

  // A small force layout, the same every time: files pull their definitions together,
  // calls are springs, everything pushes everything else apart.
  function layout(nodes, edges) {
    const files = [...new Set(nodes.map((n) => n.path))];
    const R = files.length > 1 ? 70 + 16 * files.length : 0;
    const degree = Object.fromEntries(nodes.map((n) => [n.id, 0]));
    for (const e of edges) { degree[e.src] += 1; degree[e.dst] += 1; }
    const anchor = Object.fromEntries(files.map((f, i) => {
      const a = (i / files.length) * Math.PI * 2 - Math.PI / 2;
      return [f, { x: Math.cos(a) * R * 1.5, y: Math.sin(a) * R }];
    }));
    const pos = nodes.map((n, i) => {
      const a = anchor[n.path];
      const k = nodes.filter((m, j) => j < i && m.path === n.path).length;
      return { x: a.x + Math.cos(k * 2.4) * (18 + 12 * k), y: a.y + Math.sin(k * 2.4) * (18 + 12 * k) };
    });
    const index = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
    const springs = edges.map((e) => [index[e.src], index[e.dst]]).filter(([a, b]) => a !== undefined && b !== undefined);
    for (let step = 0; step < 320; step++) {
      const cool = 1 - step / 340;
      const force = pos.map(() => ({ x: 0, y: 0 }));
      for (let i = 0; i < pos.length; i++) {
        for (let j = i + 1; j < pos.length; j++) {
          let dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y;
          const d2 = Math.max(dx * dx + dy * dy, 40);
          const f = 2600 / d2;
          const d = Math.sqrt(d2);
          dx /= d; dy /= d;
          force[i].x += dx * f; force[i].y += dy * f;
          force[j].x -= dx * f; force[j].y -= dy * f;
        }
      }
      for (const [a, b] of springs) {
        const dx = pos[b].x - pos[a].x, dy = pos[b].y - pos[a].y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const f = (d - 70) * 0.05;
        force[a].x += (dx / d) * f; force[a].y += (dy / d) * f;
        force[b].x -= (dx / d) * f; force[b].y -= (dy / d) * f;
      }
      nodes.forEach((n, i) => {
        const a = anchor[n.path];
        const pull = degree[n.id] ? 0.02 : 0.07;     // a definition nothing calls stays home
        force[i].x += (a.x - pos[i].x) * pull - pos[i].x * 0.004;
        force[i].y += (a.y - pos[i].y) * pull - pos[i].y * 0.006;
        const m = Math.hypot(force[i].x, force[i].y);
        const cap = 14 * cool;
        const s = m > cap ? cap / m : 1;
        pos[i].x += force[i].x * s * cool;
        pos[i].y += force[i].y * s * cool;
      });
    }
    return pos;
  }

  function render(container, map, opts = {}) {
    container.innerHTML = "";
    const edges = ((map && map.edges) || []).filter((e) => e.src !== e.dst);
    // leave out what is connected to nothing and has nothing to do with the change
    const linked = new Set(edges.flatMap((e) => [e.src, e.dst]));
    const all = (map && map.nodes) || [];
    const keep = all.filter((n) => linked.has(n.id) || n.change || n.score > 0 || n.finding);
    const nodes = keep.length >= 2 ? keep : all;
    if (!nodes.length) {
      container.innerHTML = '<p class="map-empty">No definitions to draw.</p>';
      return { play() {}, finish() {}, duration: () => 0 };
    }
    const raw = layout(nodes, edges);
    const index = Object.fromEntries(nodes.map((n, i) => [n.id, i]));

    // fit the layout to the box, one unit to one pixel, so labels read at the same size anywhere
    const W = Math.max(container.clientWidth - 12, 300);
    const padX = 64, padY = 34;
    const xs = raw.map((p) => p.x), ys = raw.map((p) => p.y);
    const bw = Math.max(Math.max(...xs) - Math.min(...xs), 1);
    const bh = Math.max(Math.max(...ys) - Math.min(...ys), 1);
    const s = Math.min(nodes.length <= 6 ? 3 : 1.5, (W - 2 * padX) / bw, (Math.min(460, W * 0.75) - 2 * padY) / bh);
    const H = Math.max(200, bh * s + 2 * padY);
    const ox = (W - bw * s) / 2 - Math.min(...xs) * s, oy = (H - bh * s) / 2 - Math.min(...ys) * s;
    const pos = raw.map((p) => ({ x: p.x * s + ox, y: p.y * s + oy }));

    const root = svg("svg", {
      class: "map", viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": opts.label || "The code map: definitions, the calls between them, and what the change reaches",
    });
    const marker = svg("marker", { id: `arrow-${uid}`, viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: "auto" }, svg("defs", {}, root));
    svg("path", { d: "M0,1 L9,5 L0,9 z", class: "edge-arrow" }, marker);

    // which edges carry the impact: a definition the change reaches, and the step it came by
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
    const level = (n) => (n.change && n.change !== "added" ? 0 : n.score > 0 ? n.hops : -1);
    const hot = (e) => {
      const s = byId[e.src], d = byId[e.dst];
      return s && d && s.score > 0 && level(d) >= 0 && level(d) === s.hops - 1;
    };

    const gEdges = svg("g", { class: "edges" }, root);
    const gNodes = svg("g", { class: "nodes" }, root);
    const edgeEls = edges.map((e) => {
      const a = pos[index[e.src]], b = pos[index[e.dst]];
      if (!a || !b) return null;
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      const r = 12;
      const isHot = hot(e);
      const el = svg("path", {
        d: `M${a.x + (dx / d) * r},${a.y + (dy / d) * r} L${b.x - (dx / d) * r},${b.y - (dy / d) * r}`,
        class: `edge ${e.kind}${e.guess ? " guess" : ""}${isHot ? " impact" : ""}`,
        "marker-end": `url(#arrow-${uid})`,
      }, gEdges);
      svg("title", {}, el).textContent = `${e.src} ${e.kind} ${e.dst}${e.guess ? " (a guess)" : ""}`;
      return { el, e, hops: isHot ? byId[e.src].hops : 0 };
    }).filter(Boolean);

    const nodeEls = nodes.map((n, i) => {
      const p = pos[i];
      const g = svg("g", {
        class: ["node", n.kind, n.change && `changed ${n.change}`, n.removed && "removed",
          n.score > 0 && "affected", n.finding && "finding"].filter(Boolean).join(" "),
        transform: `translate(${p.x},${p.y})`,
      }, gNodes);
      if (n.score > 0) g.style.setProperty("--heat", String(0.3 + 0.7 * n.score));
      svg("circle", { r: 18, class: "halo" }, g);
      if (n.kind === "class") svg("rect", { x: -8, y: -8, width: 16, height: 16, rx: 4, class: "dot" }, g);
      else if (n.kind === "constant") svg("rect", { x: -6, y: -6, width: 12, height: 12, class: "dot", transform: "rotate(45)" }, g);
      else svg("circle", { r: 7.5, class: "dot" }, g);
      const label = svg("text", { y: 22, class: "label" }, g);
      const name = n.label.split(".").pop();          // the method, not Class.method: hover for all
      label.textContent = name.length > 24 ? name.slice(0, 23) + "…" : name;
      if (n.score > 0) {
        svg("text", { y: -14, class: "score" }, g).textContent = n.score.toFixed(2);
      }
      if (n.finding) {
        const b = svg("g", { class: "badge", transform: "translate(10,-10)" }, g);
        svg("circle", { r: 7 }, b);
        svg("text", { y: 3.5 }, b).textContent = "!";
      }
      const what = n.change ? ` · ${n.change}` : n.score > 0 ? ` · reached, score ${n.score.toFixed(2)} (${n.hops} hop${n.hops === 1 ? "" : "s"})` : "";
      svg("title", {}, g).textContent = `${n.id}${what}${n.finding ? " · a finding is here" : ""}\n${n.path}:${n.line}`;
      return { g, n };
    });

    container.append(root);
    if (opts.legend !== false) {
      const legend = document.createElement("div");
      legend.className = "map-legend";
      legend.innerHTML = `<span><i class="lg changed"></i>changed</span><span><i class="lg affected"></i>reached by the change</span><span><i class="lg finding"></i>a finding</span><span><i class="lg edge"></i>calls</span><span><i class="lg reads"></i>reads</span>`;
      container.append(legend);
    }

    let timers = [];
    const later = (ms, fn) => timers.push(setTimeout(fn, ms));
    const clear = () => { timers.forEach(clearTimeout); timers = []; };

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
      // 1. the map appears; 2. the change pulses; 3. its callers light up, hop by hop
      nodeEls.forEach(({ g }, i) => later(40 + i * 28, () => g.classList.add("in")));
      edgeEls.forEach(({ el }, i) => later(260 + i * 18, () => el.classList.add("in")));
      const t0 = 500 + nodeEls.length * 28;
      nodeEls.filter(({ n }) => n.change).forEach(({ g }) => later(t0, () => g.classList.add("lit")));
      const maxHop = Math.max(0, ...nodes.map((n) => (n.score > 0 ? n.hops : 0)));
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
    return { play, finish, duration: () => 1100 + nodeEls.length * 28 + 850 * Math.max(0, ...nodes.map((n) => (n.score > 0 ? n.hops : 0))) };
  }

  let uid = 0;
  const api = {
    fromReport,
    render(container, map, opts) { uid += 1; return render(container, map, opts); },
  };
  return api;
})();
