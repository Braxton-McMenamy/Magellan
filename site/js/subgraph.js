/*
 * Sub-graphs: one part of a map, as a map of its own. Right-click a dot (or press "Show
 * sub-graph" beside it) and its neighbourhood opens as a scene of its own: the dot, what depends
 * on it a hop or two back, and what it uses. A file's sub-graph is everything defined in the
 * file, what calls or reads it, and what it calls or reads. The Scene, the Team suite and the
 * VS Code map panel draw the result with the same renderers as the whole map (js/map.js,
 * js/scene3d.js); js/subgraph-ui.js is the menu and the tabs.
 *
 * Pure functions on Lite's maps ({ nodes, edges }, magellan_lite/web.py), no DOM, so Node can
 * test them (tests/js/subgraph.test.js):
 *
 *   const sub = MagellanSubgraph.of(map, ["sensor.channel.parse_record"], { in: 2, out: 1 });
 *   sub.nodes, sub.edges          // a map: the seeds (seed: true), their dependents, their uses
 *   sub.capped                    // more than the cap was near: only the nearest are kept
 *   MagellanSubgraph.title(sub)   // "parse_record and its neighbourhood · 12"
 *   MagellanSubgraph.ofFile(map, "sensor/channel.py")
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MagellanSubgraph = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Big enough for any neighbourhood worth reading, small enough that 3D stays quick and Flow
  // stays a page or two tall. A hub that more depends on is cut to its nearest.
  const CAP = 300;
  const NODE = { in: 2, out: 1 };       // a dot: what breaks two hops back, what it leans on
  const FILE = { in: 1, out: 1 };       // a file: what touches it directly, either way

  const hops = (v, fallback) => (Number.isFinite(Number(v)) && v !== null ? Math.max(0, Math.floor(Number(v))) : fallback);
  const push = (m, k, v) => (m.get(k) || m.set(k, []).get(k)).push(v);
  const short = (id) => String(id).split(".").pop();
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

  /** How far each node is from the seeds, walking ``next`` (id -> ids) at most ``limit`` times. */
  function walk(seeds, next, limit) {
    const dist = new Map(seeds.map((id) => [id, 0]));
    let frontier = seeds;
    for (let h = 1; h <= limit && frontier.length; h++) {
      const more = [];
      for (const id of frontier) {
        for (const n of next.get(id) || []) if (!dist.has(n)) { dist.set(n, h); more.push(n); }
      }
      frontier = more;
    }
    return dist;
  }

  /** Who calls (or reads) whom, both ways, over the edges whose ends are both in the map. */
  function links(nodes, edges) {
    const known = new Set(nodes.map((n) => n.id));
    const callers = new Map(), callees = new Map();
    for (const e of edges) {
      if (e.src === e.dst || !known.has(e.src) || !known.has(e.dst)) continue;
      push(callers, e.dst, e.src);
      push(callees, e.src, e.dst);
    }
    return { known, callers, callees };
  }

  /**
   * The seeds, everything that depends on them up to ``opts.in`` hops (what calls or reads them,
   * then what calls those), everything they use up to ``opts.out`` hops, and the edges among
   * those nodes. Seeds are marked ``seed: true``; every other field is kept as it was. Unknown
   * seeds are ignored: none known gives an empty map. Past ``opts.cap`` nodes (300) only the
   * nearest are kept -- the seeds, then one hop either way, then two -- and ``capped`` says so.
   */
  function of(map, seedIds, opts = {}) {
    const nodes = (map && map.nodes) || [], edges = (map && map.edges) || [];
    const up = hops(opts.in, NODE.in), down = hops(opts.out, NODE.out);
    const cap = Math.max(1, hops(opts.cap, CAP));
    const { known, callers, callees } = links(nodes, edges);
    const seeds = [...new Set([].concat(seedIds || []).map(String))].filter((id) => known.has(id));
    const dependents = walk(seeds, callers, up), uses = walk(seeds, callees, down);

    // nearest first; at the same distance, what depends on the seeds before what they use (the
    // question a sub-graph answers first is what a change to it would break)
    const dist = new Map(dependents);
    for (const [id, d] of uses) if (!dist.has(id) || d < dist.get(id)) dist.set(id, d);
    const line = new Map(nodes.map((n) => [n.id, Number(n.line) || 0]));
    const ranked = [...dist.keys()].sort((a, b) => dist.get(a) - dist.get(b)
      || dependents.has(b) - dependents.has(a) || line.get(a) - line.get(b) || (a < b ? -1 : a > b ? 1 : 0));
    const kept = new Set(ranked.slice(0, cap));
    const seedSet = new Set(seeds.filter((id) => kept.has(id)));

    const out = nodes.filter((n) => kept.has(n.id)).map((n) => {
      const { seed, ...rest } = n;                      // a sub-graph of a sub-graph: only its own seeds
      return seedSet.has(n.id) ? { ...rest, seed: true } : rest;
    });
    const count = (side) => out.filter((n) => !n.seed && side.has(n.id)).length;
    return {
      nodes: out,
      edges: edges.filter((e) => kept.has(e.src) && kept.has(e.dst)).map((e) => ({ ...e })),
      seeds: [...seedSet],
      dependents: count(dependents),
      uses: out.filter((n) => !n.seed && uses.has(n.id) && !dependents.has(n.id)).length,
      hops: { in: up, out: down },
      total: dist.size,
      capped: dist.size > kept.size,
    };
  }

  /** A file's sub-graph: every definition in ``path`` is a seed (one hop either way, by default). */
  function ofFile(map, path, opts = {}) {
    const file = String(path || "");
    const seeds = ((map && map.nodes) || []).filter((n) => file && n.path === file).map((n) => n.id);
    return { ...of(map, seeds, { ...FILE, ...opts }), file };
  }

  /** The seed's name, or the file's path: what the sub-graph is of. */
  function subject(sub) {
    if (sub.file) return sub.file;
    const id = (sub.seeds || [])[0];
    const node = (sub.nodes || []).find((n) => n.id === id);
    return node ? String(node.label || short(node.id)) : id ? short(id) : "";
  }

  /** A tab's name: "parse_record and its neighbourhood · 12", "sensor/channel.py · 9". */
  function title(sub) {
    const n = sub && sub.nodes ? sub.nodes.length : 0;
    if (!n) return "Nothing here";
    const count = sub.capped ? `${n} of ${sub.total}` : String(n);
    if (sub.file) return `${sub.file} · ${count}`;
    if (sub.seeds.length > 1) return `${plural(sub.seeds.length, "definition")} and their neighbourhood · ${count}`;
    return `${subject(sub)} and its neighbourhood · ${count}`;
  }

  /**
   * One line on what is in it, and what was left out when it was capped:
   * "check_files: 72 definitions depend on it within 2 hops; it uses 2."
   */
  function describe(sub) {
    if (!sub || !sub.nodes || !sub.nodes.length) return "Nothing in the map has that name.";
    const one = sub.seeds.length === 1, it = one ? "it" : "them", they = one ? "it" : "they";
    const d = sub.dependents, u = sub.uses;
    const back = sub.hops.in === 1 ? "directly" : `within ${plural(sub.hops.in, "hop")}`;
    const ahead = sub.hops.out === 1 ? "" : ` within ${plural(sub.hops.out, "hop")}`;
    const what = sub.file
      ? `The ${plural(sub.seeds.length, "definition")} in ${sub.file}: ${d ? `${plural(d, "definition")} ${d === 1 ? "calls or reads" : "call or read"} ${it}`
        : `nothing calls or reads ${it}`}; ${u ? `${they} ${one ? "calls or reads" : "call or read"} ${u} more` : `${they} ${one ? "uses" : "use"} nothing else`}.`
      : `${one ? subject(sub) : plural(sub.seeds.length, "definition")}: ${d ? `${plural(d, "definition")} ${d === 1 ? "depends" : "depend"} on ${it} ${back}`
        : `nothing depends on ${it}`}; ${u ? `${they} ${one ? "uses" : "use"} ${u}${ahead}` : `${they} ${one ? "uses" : "use"} nothing else`}.`;
    return sub.capped ? `${what} Only the nearest ${sub.nodes.length} of ${sub.total} are shown.` : what;
  }

  /**
   * The sub-graph as the Flow view (js/map.js) reads a map: the seeds in the middle where "the
   * change" goes, what depends on them hop by hop to the right (scored 0.9 a hop, as a call
   * fades), what they use on the left. The real change's marks are dropped: here the question
   * is what hangs on the seeds.
   */
  function flow(sub) {
    const nodes = (sub && sub.nodes) || [], edges = (sub && sub.edges) || [];
    const { callers } = links(nodes, edges);
    const dist = walk(nodes.filter((n) => n.seed).map((n) => n.id), callers, Infinity);
    return {
      ...sub,
      edges,
      nodes: nodes.map((n) => {
        if (n.seed) return { ...n, change: n.change || "picked", score: 0, hops: 0 };
        const h = dist.get(n.id) || 0;
        return { ...n, change: "", finding: false, hops: h, score: h ? Number((0.9 ** h).toFixed(2)) : 0 };
      }),
    };
  }

  /** What Flow's middle column is called for this sub-graph (map.js opts.heads). */
  const heads = (sub) => ({ 0: sub && sub.file ? "the file" : "picked" });

  /**
   * A sub-graph from what a page keeps of it: ``{ ids }`` (a dot's) or ``{ file }``, with
   * ``hops`` ({ in, out }) when not the default. Pages keep this rather than the nodes, so a
   * new check, or the team's next update, draws the same sub-graph from the new map.
   */
  function build(map, spec) {
    if (!spec) return of(map, []);
    if (spec.file) return ofFile(map, spec.file, spec.hops);
    return of(map, spec.ids || spec.seeds || [], spec.hops);
  }

  /** Two specs that make the same sub-graph (so a second "Show sub-graph" finds the tab). */
  const same = (a, b) => !!a && !!b && (a.file || "") === (b.file || "")
    && String(a.ids || "") === String(b.ids || "");

  return { of, ofFile, build, same, title, describe, subject, flow, heads, CAP };
});
