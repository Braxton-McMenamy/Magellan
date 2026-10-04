/*
 * The 3D map: a whole project as clusters arranged on a sphere, each facing outward. From the
 * full Magellan (magellan/interfaces/ui/graph3d.js, written before the hackathon), restyled for
 * Magellan Lite's dark stage: bigger nodes, brighter edges, and a "change" colouring -- what
 * changed in orange (or in its author's colour on a team map), what the change reaches in red by
 * how hard it is hit, a red ring where a finding is. js/scene3d.js feeds it Lite's maps; the
 * website's Scene and Team suite and the VS Code map panel draw with it. No dependencies.
 *
 *   const view = new MagellanGraph3D.GraphView3D(canvas, { onSelect, onOpen }, { cluster: "language" });
 *   view.setData({ nodes, edges, center });   // same shape as GET /graph, and as the 2D view
 *   view.setCluster("community");            // language | layer | directory | community
 *
 * Every node belongs to a cluster: its language, its layer, its top directory, or a community
 * found in the call graph by label propagation. Each cluster gets a direction on a sphere
 * (Fibonacci-spread, so clusters are evenly apart) and a quaternion that turns +z onto that
 * direction. Its members are seeded as a lens of file lenses (FLAT deep for their width) in
 * the cluster's own frame -- rotated by that quaternion -- and a 3D force layout then pulls
 * connected nodes together across clusters while a weak anchor keeps each cluster in its place.
 * Cross-cluster edges (a Java native method calling C, Python calling a module in another
 * directory) are the long chords through the middle of the sphere.
 *
 * The camera is a quaternion too (arcball): drag to rotate, shift-drag to pan, wheel to zoom,
 * click to select, double-click to open. Rendering is canvas 2D with perspective and depth
 * fade, drawn back to front.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MagellanGraph3D = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---------------------------------------------------------------- quaternions [w, x, y, z]
  const Quat = {
    identity: () => [1, 0, 0, 0],
    normalize(q) { const l = Math.hypot(q[0], q[1], q[2], q[3]) || 1; return [q[0] / l, q[1] / l, q[2] / l, q[3] / l]; },
    mul(a, b) {
      return [a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
              a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
              a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
              a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0]];
    },
    conj: (q) => [q[0], -q[1], -q[2], -q[3]],
    fromAxisAngle(axis, angle) {
      const l = Math.hypot(axis[0], axis[1], axis[2]) || 1, s = Math.sin(angle / 2);
      return [Math.cos(angle / 2), (axis[0] / l) * s, (axis[1] / l) * s, (axis[2] / l) * s];
    },
    /** The shortest rotation taking unit vector ``a`` onto unit vector ``b``. */
    fromTo(a, b) {
      const d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
      if (d < -0.999999) {                         // opposite: any perpendicular axis, 180 degrees
        const ax = Math.abs(a[0]) < 0.9 ? cross([1, 0, 0], a) : cross([0, 1, 0], a);
        return Quat.fromAxisAngle(ax, Math.PI);
      }
      const c = cross(a, b);
      return Quat.normalize([1 + d, c[0], c[1], c[2]]);
    },
    /** Rotate vector ``v`` by unit quaternion ``q``. */
    rotate(q, v) {
      const u = [q[1], q[2], q[3]], w = q[0];
      const t = cross(u, v).map((x) => 2 * x);
      const ut = cross(u, t);
      return [v[0] + w * t[0] + ut[0], v[1] + w * t[1] + ut[1], v[2] + w * t[2] + ut[2]];
    },
  };
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

  function hash(str) {                                    // stable pseudo-random from an id
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return ((h >>> 0) % 100000) / 100000;
  }

  /** ``k`` directions spread evenly over the unit sphere (a Fibonacci lattice). */
  function sphereDirections(k) {
    const out = [], golden = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < k; i++) {
      const y = 1 - (2 * (i + 0.5)) / k, r = Math.sqrt(Math.max(0, 1 - y * y)), phi = i * golden;
      out.push([Math.cos(phi) * r, y, Math.sin(phi) * r]);
    }
    return out;
  }

  // ---------------------------------------------------------------- clustering
  const ID_LANG = { c: "C", cpp: "C++", java: "Java", fortran: "Fortran", cobol: "COBOL" };
  const SUFFIX_LANG = [
    [/\.py$/i, "Python"], [/\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/i, "TypeScript/JS"],
    [/\.(cc|cpp|cxx|c\+\+|hpp|hh|hxx|h\+\+|ipp|tpp)$/i, "C++"], [/\.(c|h)$/i, "C"],
    [/\.java$/i, "Java"], [/\.(f|for|f77|ftn|fpp|f90|f95|f03|f08|f18|f23|fi|fh)$/i, "Fortran"],
    [/\.(cbl|cob|cpy|cobol|dcl)$/i, "COBOL"],
  ];

  /** A node's language: the frontend's id marker (``fn:cobol@...``), else its file suffix. */
  const LITE_LANG = { python: "Python", typescript: "TypeScript/JS", ...ID_LANG };
  function languageOf(n) {
    if (n.lang && LITE_LANG[n.lang]) return LITE_LANG[n.lang];
    const m = /^(?:[a-z]+:)?([a-z]+)@/.exec(n.id || "");
    if (m && ID_LANG[m[1]]) return ID_LANG[m[1]];
    for (const [rx, name] of SUFFIX_LANG) if (rx.test(n.path || "")) return name;
    return n.path ? "other" : "external";
  }

  function directoryOf(n, depth = 1) {
    const parts = (n.path || "").split("/").filter(Boolean);
    if (!parts.length) return "external";
    if (parts.length === 1) return "(root)";
    return parts.slice(0, Math.min(depth, parts.length - 1)).join("/");
  }

  /** The shallowest directory depth at which no directory holds most of the code. */
  function directoryDepth(nodes) {
    for (let d = 1; d <= 4; d++) {
      const size = new Map();
      for (const n of nodes) { const k = directoryOf(n, d); size.set(k, (size.get(k) || 0) + 1); }
      if (Math.max(...size.values()) <= nodes.length * 0.5 || size.size >= 8) return d;
    }
    return 4;
  }

  /**
   * Communities by label propagation: each node repeatedly takes the label most of its
   * neighbours carry (weighted by edge confidence; imports count half). Deterministic: a fixed
   * visiting order, and ties go to the smaller label.
   */
  function communities(nodes, edges) {
    const nb = new Map(nodes.map((n) => [n.id, []]));
    for (const e of edges) {
      const w = (e.confidence == null ? 1 : e.confidence) * (e.kind === "imports" ? 0.5 : 1);
      nb.get(e.s.id).push([e.t.id, w]); nb.get(e.t.id).push([e.s.id, w]);
    }
    const label = new Map(nodes.map((n) => [n.id, n.id]));
    const order = [...nodes].sort((a, b) => hash(a.id) - hash(b.id) || (a.id < b.id ? -1 : 1));
    for (let round = 0; round < 30; round++) {
      let changed = 0;
      for (const n of order) {
        const tally = new Map();
        for (const [m, w] of nb.get(n.id)) tally.set(label.get(m), (tally.get(label.get(m)) || 0) + w);
        if (!tally.size) continue;
        let best = label.get(n.id), bw = tally.get(best) || 0;
        for (const [l, w] of tally) if (w > bw + 1e-9 || (Math.abs(w - bw) <= 1e-9 && l < best)) { best = l; bw = w; }
        if (best !== label.get(n.id)) { label.set(n.id, best); changed++; }
      }
      if (!changed) break;
    }
    // name each community after its most depended-on member
    const members = new Map();
    for (const n of nodes) (members.get(label.get(n.id)) || members.set(label.get(n.id), []).get(label.get(n.id))).push(n);
    // name each community after the file most of it lives in (and its best-known member)
    const name = new Map(), used = new Map();
    for (const [l, ms] of members) {
      const files = new Map();
      for (const n of ms) if (n.path) files.set(n.path, (files.get(n.path) || 0) + 1);
      const [file, k] = [...files.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))[0] || ["", 0];
      const top = ms.reduce((a, b) => ((b.dependents || 0) > (a.dependents || 0) ? b : a));
      let base = file ? file.split("/").pop() + (files.size > 1 && k < ms.length ? ` +${files.size - 1}` : "")
        : top.name;
      const seen = used.get(base) || 0; used.set(base, seen + 1);
      if (seen) base += ` · ${top.name}`;            // two communities in one file
      name.set(l, base);
    }
    return new Map(nodes.map((n) => [n.id, name.get(label.get(n.id))]));
  }

  const MAX_CLUSTERS = 24;

  /** ``node id -> cluster key`` under ``mode``; the smallest clusters fold into "other". */
  function clusterNodes(nodes, edges, mode) {
    let key;
    if (mode === "community") key = communities(nodes, edges);
    else {
      const depth = mode === "directory" ? directoryDepth(nodes) : 1;
      const f = mode === "layer" ? (n) => n.layer || "unknown"
        : mode === "directory" ? (n) => directoryOf(n, depth) : languageOf;
      key = new Map(nodes.map((n) => [n.id, f(n)]));
    }
    let size = new Map();
    for (const k of key.values()) size.set(k, (size.get(k) || 0) + 1);
    if (mode === "community") {
      // a "community" of one or two is code nothing much calls: one group, not dozens
      for (const [id, k] of key) if (size.get(k) < 3) key.set(id, "unconnected");
      size = new Map();
      for (const k of key.values()) size.set(k, (size.get(k) || 0) + 1);
    }
    const keep = new Set([...size.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
      .slice(0, MAX_CLUSTERS - 1).map(([k]) => k));
    if (size.size <= MAX_CLUSTERS) for (const k of size.keys()) keep.add(k);
    for (const [id, k] of key) if (!keep.has(k)) key.set(id, "other");
    return key;
  }

  // ---------------------------------------------------------------- layout
  const REST = { calls: 60, instantiates: 60, inherits: 50, overrides: 50, reads: 80, writes: 70,
                 mutates: 70, imports: 110, decorates: 60, raises: 90 };
  const SPACING = 20;
  //: a cluster with more members than this is laid out as one ball per file
  const GROUP_MIN = 30;
  const GOLDEN = 2.399963229728653;
  //: clusters are lenses, not balls: depth (along the sphere's normal) over width. A flat disc
  //: hides its middle edge-on; a ball inside a sphere is clutter; this is in between
  const FLAT = 0.6;
  const baseName = (p) => String(p || "").split("/").pop() || "(no file)";
  const radiusOf = (n) => Math.min(20, 6 + Math.sqrt(Math.max(0, n.dependents || 0)) * 1.9 + Math.sqrt(Math.max(0, (n.members || 0)) / 4))
    * (n.change || n.finding ? 1.25 : 1);

  /**
   * Lay one cluster's members out as a ball of file balls: one per file, packed in 3D inside
   * the cluster's ball. Sets ``n.group`` on each member and returns ``{ groups, ballR }`` with
   * each group's local centre (relative to the cluster's anchor) and radius. A small cluster
   * is one group.
   */
  function packGroups(members) {
    const byFile = new Map();
    for (const n of members) {
      n.group = members.length > GROUP_MIN ? (n.path || "(no file)") : "";
      (byFile.get(n.group) || byFile.set(n.group, []).get(n.group)).push(n);
    }
    const groups = new Map();
    const order = [...byFile.entries()].sort((a, b) => b[1].length - a[1].length || (a[0] < b[0] ? -1 : 1));
    const dirs = sphereDirections(Math.max(1, order.length - 1));
    let vol = 0, ballR = 0;
    order.forEach(([key, ms], i) => {
      const r = ballRadius(ms.length) + 10;
      const v = r * r * r;
      // a 3D sunflower of unequal balls: the largest in the middle, each next one out along
      // a spread direction, at the radius that holds the volume of everything before it plus
      // half its own; 1.3 leaves room between them
      const rho = i === 0 ? 0 : 1.3 * Math.cbrt(vol + v / 2);
      const d = i === 0 ? [0, 0, 0] : dirs[i - 1];
      groups.set(key, { key, label: key ? baseName(key) : "", size: ms.length, r,
                        local: [d[0] * rho, d[1] * rho, d[2] * rho * FLAT], center: [0, 0, 0] });
      vol += v; ballR = Math.max(ballR, rho + r);
    });
    return { groups, ballR };
  }
  /** Radius of a ball that holds ``count`` elements at SPACING apart. */
  const ballRadius = (count) => SPACING * 0.95 * Math.cbrt(Math.max(1, count)) + 6;


  class GraphModel3D {
    constructor(opts = {}) {
      this.nodes = []; this.edges = []; this.byId = new Map();
      this.clusters = new Map();                    // key -> { key, dir, quat, anchor, size, ballR }
      this.mode = opts.cluster || "language";
      this.alpha = 0; this.center = null;
      this.repulsion = opts.repulsion || 1800;
      // in a ball, a 170 reach touches hundreds of neighbours; past ~4 spacings repulsion is
      // negligible and the clusters are held in place by their anchors anyway
      this.cutoff = opts.cutoff || 80;
      this.raw = { nodes: [], edges: [] };
    }

    setData(data, mode) {
      if (mode) this.mode = mode;
      this.raw = data;
      const old = this.byId, next = new Map(), nodes = [];
      for (const raw of data.nodes || []) {
        const p = old.get(raw.id);
        const n = Object.assign({}, raw, {
          x: p ? p.x : NaN, y: p ? p.y : NaN, z: p ? p.z : NaN,
          vx: 0, vy: 0, vz: 0, pinned: false, r: radiusOf(raw), cluster: p ? p.cluster : undefined,
        });
        next.set(n.id, n); nodes.push(n);
      }
      const edges = [];
      for (const e of data.edges || []) {
        const s = next.get(e.src), t = next.get(e.dst);
        if (!s || !t || s === t) continue;
        edges.push({ s, t, kind: e.kind, confidence: e.confidence == null ? 1 : e.confidence, weight: e.weight || 1 });
      }
      this.nodes = nodes; this.edges = edges; this.byId = next;
      this.center = data.center && next.has(data.center) ? next.get(data.center) : null;
      this._cluster();
      this._seed(old.size ? old : null);
      this.alpha = Math.max(this.alpha, old.size ? 0.35 : 1);       // a gentle nudge on refresh
    }

    /** Re-cluster in place (keeps the data; moves every node into its new cluster's ball). */
    setCluster(mode) {
      if (mode === this.mode) return;
      this.mode = mode;
      this._cluster();
      for (const n of this.nodes) n.x = n.y = n.z = NaN;
      this._seed(null);
      this.alpha = 1;
    }

    _cluster() {
      const key = clusterNodes(this.nodes, this.edges, this.mode);
      const members = new Map();
      for (const n of this.nodes) {
        const was = n.cluster;
        n.cluster = key.get(n.id);
        if (was !== undefined && was !== n.cluster) n.x = n.y = n.z = NaN;   // re-seed only movers
        (members.get(n.cluster) || members.set(n.cluster, []).get(n.cluster)).push(n);
      }
      // directions by sorted key, so a cluster keeps its place while the set of clusters holds
      const keys = [...members.keys()].sort();
      const dirs = sphereDirections(keys.length);
      // inside a large cluster, one ball per file, packed in 3D (largest at the centre), so
      // the middle of a big cluster is files you can read, not one solid mass
      const layouts = new Map();
      for (const [k, ms] of members) layouts.set(k, packGroups(ms));
      const maxBall = Math.max(40, ...[...layouts.values()].map((l) => l.ballR));
      const R = keys.length < 2 ? 0 : (2.4 * maxBall) / Math.sqrt((4 * Math.PI) / keys.length);
      this.clusters = new Map();
      keys.forEach((k, i) => {
        const dir = dirs[i], quat = Quat.fromTo([0, 0, 1], dir), lay = layouts.get(k);
        const anchor = [dir[0] * R, dir[1] * R, dir[2] * R];
        for (const g of lay.groups.values()) {
          const w = Quat.rotate(quat, g.local);
          g.center = [anchor[0] + w[0], anchor[1] + w[1], anchor[2] + w[2]];
        }
        this.clusters.set(k, {
          key: k, dir, quat, size: members.get(k).length, anchor, ballR: lay.ballR,
          groups: lay.groups, color: CLUSTER_COLORS[i % CLUSTER_COLORS.length],
        });
      });
      this.radius = R;
    }

    /** Place every node without a position: beside a placed neighbour, else in its cluster's ball. */
    _seed(old) {
      const adj = new Map();
      for (const e of this.edges) {
        (adj.get(e.s.id) || adj.set(e.s.id, []).get(e.s.id)).push(e.t);
        (adj.get(e.t.id) || adj.set(e.t.id, []).get(e.t.id)).push(e.s);
      }
      const index = new Map();
      for (const n of [...this.nodes].sort((a, b) => (b.dependents || 0) - (a.dependents || 0) || (a.id < b.id ? -1 : 1))) {
        const c = this.clusters.get(n.cluster), g = c.groups.get(n.group);
        const slot = n.cluster + "\u0000" + n.group;
        const i = index.get(slot) || 0; index.set(slot, i + 1);
        if (Number.isFinite(n.x)) continue;
        const placed = old ? (adj.get(n.id) || []).find((m) => Number.isFinite(m.x) && m.cluster === n.cluster) : null;
        if (placed) {
          const a = hash(n.id) * Math.PI * 2, b = hash(n.id + "b") * Math.PI;
          n.x = placed.x + Math.cos(a) * Math.sin(b) * 30; n.y = placed.y + Math.sin(a) * Math.sin(b) * 30; n.z = placed.z + Math.cos(b) * 30;
          continue;
        }
        // a golden spiral through its file's ball: the most depended-on at the core, the rest
        // in shells outward, turned by the cluster's quaternion
        const cnt = g.size, rad = g.r * 0.9 * Math.cbrt((i + 0.5) / cnt);
        const z = 1 - (2 * ((i * 0.618034) % 1)), ring = Math.sqrt(Math.max(0, 1 - z * z)), ang = i * GOLDEN;
        const local = [g.local[0] + rad * ring * Math.cos(ang), g.local[1] + rad * ring * Math.sin(ang), g.local[2] + rad * z * FLAT];
        const w = Quat.rotate(c.quat, local);
        n.x = c.anchor[0] + w[0]; n.y = c.anchor[1] + w[1]; n.z = c.anchor[2] + w[2];
      }
    }

    reheat(a = 0.5) { this.alpha = Math.max(this.alpha, a); }
    get settled() { return this.alpha < 0.004; }

    step() {
      if (this.settled || !this.nodes.length) return;
      const nodes = this.nodes, cut = this.cutoff, cut2 = cut * cut, a = this.alpha;
      const grid = new Map(), cell = (v) => Math.floor(v / cut);
      for (const n of nodes) {
        const k = cell(n.x) + "," + cell(n.y) + "," + cell(n.z);
        (grid.get(k) || grid.set(k, []).get(k)).push(n);
      }
      for (const n of nodes) {
        const cx = cell(n.x), cy = cell(n.y), cz = cell(n.z);
        for (let gx = cx - 1; gx <= cx + 1; gx++) for (let gy = cy - 1; gy <= cy + 1; gy++) for (let gz = cz - 1; gz <= cz + 1; gz++) {
          const bucket = grid.get(gx + "," + gy + "," + gz);
          if (!bucket) continue;
          for (const m of bucket) {
            if (m === n) continue;
            let dx = n.x - m.x, dy = n.y - m.y, dz = n.z - m.z, d2 = dx * dx + dy * dy + dz * dz;
            if (d2 > cut2) continue;
            if (d2 < 1) { dx = hash(n.id + m.id) - 0.5; dy = hash(m.id + n.id) - 0.5; dz = hash(n.id) - 0.5; d2 = 1; }
            const d = Math.sqrt(d2);
            // repulsion, plus a hard push when two nodes' discs would overlap (hubs are big)
            const overlap = (n.r + m.r) * 1.6 - d;
            const f = (this.repulsion * a) / d2 + (overlap > 0 ? overlap * 0.25 : 0);
            n.vx += (dx / d) * f; n.vy += (dy / d) * f; n.vz += (dz / d) * f;
          }
        }
      }
      for (const e of this.edges) {
        const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, dz = e.t.z - e.s.z;
        const d = Math.hypot(dx, dy, dz) || 1;
        const same = e.s.cluster === e.t.cluster, sameFile = same && e.s.group === e.t.group;
        const rest = same ? (REST[e.kind] || 70) + e.s.r + e.t.r : this.radius * 0.6;
        // springs across clusters (and across files) are weak: they bend groups toward each
        // other, they do not merge them
        const k = 0.05 * a * (e.kind === "imports" ? 0.5 : 1) * (0.4 + 0.6 * e.confidence)
          * (sameFile ? 1 : same ? 0.05 : 0.06);
        const f = (d - rest) * k, fx = (dx / d) * f, fy = (dy / d) * f, fz = (dz / d) * f;
        e.s.vx += fx; e.s.vy += fy; e.s.vz += fz; e.t.vx -= fx; e.t.vy -= fy; e.t.vz -= fz;
      }
      for (const n of nodes) {
        const c = this.clusters.get(n.cluster);
        if (c) {
          // held in its file's lens (an ellipsoid squashed along the cluster's normal): free
          // inside, pulled back firmly once outside; the normal part is FLAT^-2 stiffer
          const g = c.groups.get(n.group), t = g ? g.center : c.anchor, lim = g ? g.r : c.ballR;
          const ox = n.x - t[0], oy = n.y - t[1], oz = n.z - t[2];
          const sn = ox * c.dir[0] + oy * c.dir[1] + oz * c.dir[2];
          const tx = ox - c.dir[0] * sn, ty = oy - c.dir[1] * sn, tz = oz - c.dir[2] * sn;
          const d = Math.sqrt(tx * tx + ty * ty + tz * tz + (sn / FLAT) ** 2) || 1;
          const pull = (0.012 + (d > lim ? 0.15 * (d - lim) / d : 0)) * a, pn = pull / (FLAT * FLAT);
          n.vx -= tx * pull + c.dir[0] * sn * pn; n.vy -= ty * pull + c.dir[1] * sn * pn; n.vz -= tz * pull + c.dir[2] * sn * pn;
        }
        if (n === this.center) { n.vx -= n.x * 0.05 * a; n.vy -= n.y * 0.05 * a; n.vz -= n.z * 0.05 * a; }
        if (n.pinned) { n.vx = n.vy = n.vz = 0; continue; }
        n.vx *= 0.82; n.vy *= 0.82; n.vz *= 0.82;
        const speed = Math.hypot(n.vx, n.vy, n.vz);
        if (speed > 40) { n.vx *= 40 / speed; n.vy *= 40 / speed; n.vz *= 40 / speed; }
        n.x += n.vx; n.y += n.vy; n.z += n.vz;
      }
      this.alpha *= 0.985;
    }

    settle(maxSteps = 600) { let i = 0; while (!this.settled && i++ < maxSteps) this.step(); return i; }

    extent() {
      let r = 1;
      for (const n of this.nodes) r = Math.max(r, Math.hypot(n.x, n.y, n.z) + n.r);
      return r;
    }

    neighbours(node) {
      const out = new Set();
      for (const e of this.edges) { if (e.s === node) out.add(e.t); else if (e.t === node) out.add(e.s); }
      return out;
    }
  }

  const CLUSTER_COLORS = ["#5b9cff", "#36c98a", "#b48cff", "#2fc6d6", "#e070b0", "#8fb8ff",
                          "#7fd36b", "#c9a7ff", "#5fd0c4", "#ff8fb8", "#a7c4e0", "#9aa53a"];
  //: Lite's change colouring, matching the website (css/style.css, css/live.css)
  const CHANGE = { changed: "#f2a93b", hot: "#ff6b5e", finding: "#ff3b30", calm: "#5d6d80", removed: "#8a6a3a" };
  const LAYER_COLORS = {
    entrypoint: "#e5534b", interface: "#f0883e", orchestration: "#d29922", domain: "#3fb950",
    state: "#a371f7", data_access: "#39c5cf", io_boundary: "#db61a2", config: "#8b949e",
    util: "#768390", test: "#6cb6ff", unknown: "#57606a",
  };

  // ---------------------------------------------------------------- shapes
  const EDGE_COLORS = {
    calls: "#a9b8cc", instantiates: "#a9b8cc", inherits: "#b48cff", overrides: "#b48cff",
    reads: "#39c5cf", writes: "#f0883e", mutates: "#e5534b", imports: "#6e7681",
    decorates: "#d29922", raises: "#e5534b",
  };

  /** "parser.py · 193" -- a group's name and how many elements it holds (none for one). */
  function countLabel(name, size) { return size > 1 ? `${name} · ${size}` : name; }

  /** ``hex`` lightened (f > 0) or darkened (f < 0) by ``|f|`` toward white or black. */
  function shade(hex, f) {
    const v = parseInt(hex.slice(1), 16), to = f < 0 ? 0 : 255, k = Math.abs(f);
    const ch = (s) => Math.round(((v >> s) & 255) + (to - ((v >> s) & 255)) * k);
    return `rgb(${ch(16)},${ch(8)},${ch(0)})`;
  }

  /** The solid a node is drawn as, by what it is. */
  function shapeOf(n) {
    const k = n.kind || "";
    if (k === "external" || (n.id || "").startsWith("ext:")) return "ring";
    if (k === "class") return "cube";
    if (k === "package") return "cylinder";
    if (k === "module") {
      // a file is drawn as what it mostly holds: a package's index a cylinder, a file of
      // classes a cube, of data a diamond, of functions (or of nothing yet) a sphere
      const base = ((n.path || "").split("/").pop() || "").replace(/\.[^.]*$/, "");
      if (base === "__init__" || base === "index" || base === "mod" || base === "package-info") return "cylinder";
      const mk = n.makeup || {};
      const classes = mk["class"] || 0;
      const funcs = mk["function"] || 0;          // methods belong to their classes
      const data = (mk["global_var"] || 0) + (mk["class_attr"] || 0) + (mk["instance_attr"] || 0);
      if (classes && classes * 3 >= funcs) return "cube";
      if (data > funcs + classes) return "diamond";
      return "sphere";
    }
    if (k === "global_var" || k === "class_attr" || k === "instance_attr" || k === "local_var") return "diamond";
    return "sphere";                              // functions and methods
  }

  /**
   * Draw a shaded solid of "radius" r at (x, y), lit from the upper left. Canvas 2D only:
   * a sphere is a radial gradient; a cube three faces; a cylinder a body and an elliptic top;
   * a diamond (octahedron) four faces; a library a ring.
   */
  function drawShape(ctx, shape, x, y, r, color) {
    if (shape === "sphere") {
      const g = ctx.createRadialGradient ? ctx.createRadialGradient(x - r * 0.35, y - r * 0.4, r * 0.1, x, y, r) : null;
      if (g) { g.addColorStop(0, shade(color, 0.55)); g.addColorStop(0.55, color); g.addColorStop(1, shade(color, -0.45)); }
      ctx.fillStyle = g || color;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    } else if (shape === "cube") {
      const a = r * 0.86, h = r * 0.5;            // an isometric cube that fits the circle of radius r
      const face = (pts, c) => { ctx.fillStyle = c; ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
        for (const q of pts.slice(1)) ctx.lineTo(q[0], q[1]); ctx.closePath(); ctx.fill(); };
      face([[x, y - r], [x + a, y - h], [x, y], [x - a, y - h]], shade(color, 0.35));                // top
      face([[x - a, y - h], [x, y], [x, y + r], [x - a, y + h]], color);                              // left
      face([[x + a, y - h], [x, y], [x, y + r], [x + a, y + h]], shade(color, -0.35));               // right
    } else if (shape === "cylinder") {
      const w = r * 0.9, e = r * 0.32, top = y - r * 0.6, bot = y + r * 0.6;
      const g = ctx.createLinearGradient ? ctx.createLinearGradient(x - w, 0, x + w, 0) : null;
      if (g) { g.addColorStop(0, shade(color, 0.25)); g.addColorStop(0.6, color); g.addColorStop(1, shade(color, -0.4)); }
      ctx.fillStyle = g || color;
      ctx.beginPath(); ctx.moveTo(x - w, top); ctx.lineTo(x - w, bot);
      if (ctx.ellipse) ctx.ellipse(x, bot, w, e, 0, Math.PI, 0, true); else ctx.lineTo(x + w, bot);
      ctx.lineTo(x + w, top); ctx.closePath(); ctx.fill();
      ctx.fillStyle = shade(color, 0.4);
      ctx.beginPath(); if (ctx.ellipse) ctx.ellipse(x, top, w, e, 0, 0, Math.PI * 2); else ctx.arc(x, top, e, 0, Math.PI * 2);
      ctx.fill();
    } else if (shape === "diamond") {
      const face = (pts, c) => { ctx.fillStyle = c; ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
        for (const q of pts.slice(1)) ctx.lineTo(q[0], q[1]); ctx.closePath(); ctx.fill(); };
      const w = r * 0.8;
      face([[x, y - r], [x - w, y], [x, y]], shade(color, 0.35));
      face([[x, y - r], [x + w, y], [x, y]], shade(color, 0.05));
      face([[x - w, y], [x, y + r], [x, y]], shade(color, -0.1));
      face([[x + w, y], [x, y + r], [x, y]], shade(color, -0.4));
    } else {                                      // ring: something outside the project
      ctx.strokeStyle = color; ctx.lineWidth = Math.max(1, r * 0.35);
      ctx.beginPath(); ctx.arc(x, y, Math.max(1, r * 0.75), 0, Math.PI * 2); ctx.stroke();
    }
  }

  // ---------------------------------------------------------------- live-edit pulses
  const PULSE_MS = 3600;
  //: added, changed and removed elements, as a save lands
  const PULSE_COLORS = { added: "#3fb950", changed: "#d29922", removed: "#f85149" };
  function addPulses(view, items) {
    const t0 = Date.now();
    view.pulses = (view.pulses || []).concat((items || []).map((q) => Object.assign({ t0 }, q)));
    view.dirty = true; if (view.start) view.start();
  }
  /** Draw each pulse as three rings spreading out and fading; `place` maps a pulse to x/y/r. */
  function drawPulses(view, place) {
    if (!view.pulses || !view.pulses.length) return;
    const ctx = view.ctx, now = Date.now();
    view.pulses = view.pulses.filter((q) => now - q.t0 < PULSE_MS);
    for (const q of view.pulses) {
      const at = place(q);
      if (!at) continue;
      const age = (now - q.t0) / PULSE_MS, color = PULSE_COLORS[q.kind] || q.color || PULSE_COLORS.changed;
      ctx.strokeStyle = color;
      for (let k = 0; k < 3; k++) {
        const ph = (age * 3 + k / 3) % 1;
        ctx.globalAlpha = (1 - ph) * (1 - age) * 0.9;
        ctx.lineWidth = at.lw || 2;
        ctx.beginPath(); ctx.arc(at.x, at.y, at.r + 2 + ph * at.grow, 0, Math.PI * 2); ctx.stroke();
      }
      if (q.kind === "removed") {                    // a ghost where it was
        ctx.globalAlpha = 0.5 * (1 - age); ctx.fillStyle = color;
        ctx.beginPath(); ctx.arc(at.x, at.y, at.r * 0.7, 0, Math.PI * 2); ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
    if (view.pulses.length) view.dirty = true;       // keep animating while any is alive
  }

  // ---------------------------------------------------------------- view
  class GraphView3D {
    constructor(canvas, handlers = {}, opts = {}) {
      this.canvas = canvas; this.ctx = canvas.getContext("2d");
      this.model = new GraphModel3D(opts); this.h = handlers;
      this.colorBy = opts.colorBy || "cluster";     // cluster | layer | change
      this.people = opts.people || null;            // name -> colour, for a team's map
      this.themeOverride = opts.theme || null;
      // A free camera: a position, an orientation (camera -> world) and the distance to the
      // point it orbits, straight ahead. It flies anywhere, through the middle included.
      this.camQ = Quat.fromAxisAngle([1, 0, 0], 0.35); this.camP = [0, 0, 800]; this.orbitDist = 800;
      this.focal = 700; this.zoom = 1; this.tx = 0; this.ty = 0; this.flight = null;
      this.heat = {}; this.flagged = new Set();
      this.hover = null; this.selected = null; this.drag = null; this.dirty = true;
      this.running = false; this.autoRotate = !!opts.autoRotate; this.proj = [];
      this.colors = this._readTheme();
      this._bind(); this.resize();
    }

    _readTheme() {
      // the stage is dark wherever it is drawn: the website's Scene and the VS Code panel alike
      return Object.assign({ bg: "#0b131e", fg: "#dce4ee", edge: "#7f93ad", chord: "#f2a93b",
                             halo: "rgba(255,107,94,", ring: "#e6edf3", muted: "#7d8b9c" }, this.themeOverride || {});
    }

    setData(data) {
      const first = this.model.nodes.length === 0;
      this.model.setData(data);
      if (first) { this.model.settle(150); this.fit(); }
      this.dirty = true; this.start();
    }
    setCluster(mode) { this.model.setCluster(mode); this.model.settle(150); this.fit(); this.dirty = true; this.start(); }
    setColorBy(mode) { this.colorBy = mode; this.dirty = true; }
    setHeat(scores) { this.heat = scores || {}; this.dirty = true; }
    setFlagged(paths) { this.flagged = new Set(paths || []); this.dirty = true; }
    setSelected(id) { this.selected = id ? this.model.byId.get(id) || null : null; this.dirty = true; }
    /** Light up only these node ids, in ``colour`` (the rest fade back); null lights everything. */
    setSpotlight(ids, colour = "#c9b6ff") { this.spot = ids ? new Set(ids) : null; this.spotColour = colour; this.dirty = true; this.start(); }
    /** Mark elements that just changed: [{ id } | { at: [x, y, z] }, color]; they ring for a few seconds. */
    pulse(items) { addPulses(this, items); }
    setAutoRotate(on) { this.autoRotate = !!on; this.start(); }

    /** The clusters and their colours, for a legend. */
    legend() {
      return [...this.model.clusters.values()].sort((a, b) => b.size - a.size)
        .map((c) => ({ key: c.key, size: c.size, color: c.color }));
    }

    /** Back to the overview: outside the sphere, looking at its centre, with the largest
     *  cluster facing the camera. */
    fit() {
      const d = Math.max(260, this.model.extent() * 2.25);     // a little closer than the original
      const big = [...this.model.clusters.values()].sort((a, b) => b.size - a.size)[0];
      this.camQ = big && this.model.radius > 0 ? Quat.fromTo([0, 0, 1], big.dir) : Quat.fromAxisAngle([1, 0, 0], 0.35);
      this.camP = Quat.rotate(this.camQ, [0, 0, d]); this.orbitDist = d;
      this.zoom = 1; this.tx = this.ty = 0; this.flight = null; this.dirty = true;
    }

    _forward() { return Quat.rotate(this.camQ, [0, 0, -1]); }
    _pivot() { const f = this._forward(); return [0, 1, 2].map((i) => this.camP[i] + f[i] * this.orbitDist); }
    _step() { return Math.max(this.orbitDist * 0.08, this.model.extent() * 0.015, 6); }

    /** Orbit the point ahead about a world axis (the scene appears to turn by ``-angle``). */
    _orbit(axisW, angle) {
      // the sphere of clusters turns about its own centre, the origin, wherever the camera has
      // flown to: the scene stays put as a whole and you swing around it
      const pv = [0, 0, 0], R = Quat.fromAxisAngle(axisW, angle);
      this.camQ = Quat.normalize(Quat.mul(R, this.camQ));
      const rel = Quat.rotate(R, [0, 1, 2].map((i) => this.camP[i] - pv[i]));
      this.camP = [0, 1, 2].map((i) => pv[i] + rel[i]);
      this.dirty = true;
    }

    /** Move the camera by ``v`` given in its own frame (x right, y up, z back). */
    move(v) {
      const w = Quat.rotate(this.camQ, v);
      this.camP = [0, 1, 2].map((i) => this.camP[i] + w[i]);
      this.flight = null; this.dirty = true;
    }

    /** Fly to look at ``point`` from ``dist`` away, keeping the current direction. */
    flyTo(point, dist = 160) {
      const f = this._forward();
      this.flight = { to: [0, 1, 2].map((i) => point[i] - f[i] * dist), orbit: dist };
      this.start();
    }
    flyToNode(n) { if (n) this.flyTo([n.x, n.y, n.z], 120 + n.r * 6); }
    /** Fly to a cluster and turn to face it (from outside the sphere, looking in). */
    flyToCluster(key) {
      const c = this.model.clusters.get(key);
      if (!c) return;
      const f = this._forward(), want = [-c.dir[0], -c.dir[1], -c.dir[2]];
      this.camQ = Quat.normalize(Quat.mul(Quat.fromTo(f, want), this.camQ));
      this.flyTo(c.anchor, Math.max(160, c.ballR * 2.6));
    }

    resize() {
      const dpr = (typeof devicePixelRatio === "number" && devicePixelRatio) || 1;
      const w = this.canvas.clientWidth || 600, h = this.canvas.clientHeight || 400;
      this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      this.dpr = dpr; this.focal = Math.min(w, h) * 1.1; this.dirty = true;
    }

    start() {
      if (this.running || typeof requestAnimationFrame !== "function") return;
      this.running = true;
      const frame = () => {
        if (!this.running) return;
        if (!this.model.settled) { this.model.step(); this.dirty = true; }
        if (this.flight) {                           // ease toward the destination
          const fl = this.flight, k = 0.18;
          this.camP = [0, 1, 2].map((i) => this.camP[i] + (fl.to[i] - this.camP[i]) * k);
          this.orbitDist += (fl.orbit - this.orbitDist) * k;
          if (Math.hypot(...[0, 1, 2].map((i) => fl.to[i] - this.camP[i])) < 0.5) { this.camP = fl.to; this.flight = null; }
          this.dirty = true;
        }
        if (this.autoRotate && !this.drag && !this.flight) this._orbit([0, 1, 0], -0.003);
        if (this.dirty) { this.draw(); this.dirty = false; }
        requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
    }
    stop() { this.running = false; }

    /** Screen position (CSS px) and depth of a world point; null behind the camera. */
    project(p) {
      const v = Quat.rotate(Quat.conj(this.camQ), [p[0] - this.camP[0], p[1] - this.camP[1], p[2] - this.camP[2]]);
      const depth = -v[2];
      if (depth < 4) return null;                    // behind the camera, or touching it
      const s = (this.focal * this.zoom) / depth, w = this.canvas.clientWidth || 600, h = this.canvas.clientHeight || 400;
      return { x: w / 2 + this.tx + v[0] * s, y: h / 2 + this.ty - v[1] * s, s, depth };
    }

    hit(px, py) {
      let best = null, bd = Infinity;
      for (const p of this.proj) {
        const d = Math.hypot(p.x - px, p.y - py), rr = Math.max(4, p.n.r * p.s) + 3;
        if (d <= rr && p.depth < bd) { best = p.n; bd = p.depth; }
      }
      return best;
    }

    _bind() {
      const c = this.canvas;
      const pos = (e) => { const r = c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
      if (c.tabIndex !== undefined && c.tabIndex < 0) c.tabIndex = 0;     // so it takes keys
      c.addEventListener("mousedown", (e) => {
        if (c.focus) c.focus();
        const [px, py] = pos(e);
        this.drag = { px, py, lx: px, ly: py, pan: e.shiftKey || e.button === 1 || e.button === 2, moved: false, node: this.hit(px, py) };
      });
      c.addEventListener("contextmenu", (e) => e.preventDefault());
      this._up = () => {
        const d = this.drag; this.drag = null;
        if (!d) return;
        if (!d.moved) { if (d.node) this._select(d.node); else { this.selected = null; this.dirty = true; } }
      };
      if (typeof window !== "undefined") window.addEventListener("mouseup", this._up);
      c.addEventListener("mousemove", (e) => {
        const [px, py] = pos(e);
        if (this.drag) {
          const dx = px - this.drag.lx, dy = py - this.drag.ly;
          if (Math.hypot(px - this.drag.px, py - this.drag.py) > 3) this.drag.moved = true;
          this.drag.lx = px; this.drag.ly = py;
          if (this.drag.pan) {                       // slide the camera in its own plane
            const u = this.orbitDist / (this.focal * this.zoom);
            this.move([-dx * u, dy * u, 0]);
          } else if (dx || dy) {                     // orbit the point ahead, about the axis across the drag
            this._orbit(Quat.rotate(this.camQ, [dy, dx, 0]), -Math.hypot(dx, dy) * 0.0035);
          }
          this.dirty = true; return;
        }
        const n = this.hit(px, py);
        if (n !== this.hover) { this.hover = n; c.style.cursor = n ? "pointer" : "grab"; this.dirty = true; }
      });
      c.addEventListener("dblclick", (e) => { const [px, py] = pos(e), n = this.hit(px, py); if (n && this.h.onOpen) this.h.onOpen(n); });
      // Wheel flies toward whatever is under the cursor -- through the middle of the sphere if
      // that is the way -- and slows as it gets close. Ctrl/Cmd+wheel magnifies instead.
      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        const [px, py] = pos(e), w = c.clientWidth || 600, h = c.clientHeight || 400;
        const f = Math.exp(-Math.max(-120, Math.min(120, e.deltaY)) * 0.0009);   // gentle, and a trackpad's bursts are capped
        if (e.ctrlKey || e.metaKey) {
          const z = Math.max(0.2, Math.min(40, this.zoom * f)), k = z / this.zoom;
          this.tx = px - w / 2 - (px - w / 2 - this.tx) * k;
          this.ty = py - h / 2 - (py - h / 2 - this.ty) * k;
          this.zoom = z; this.dirty = true;
          return;
        }
        this.wheelFly(px, py, f);
      }, { passive: false });
      c.addEventListener("keydown", (e) => {
        const k = e.key.toLowerCase(), s = this._step();
        const moves = { w: [0, 0, -s], arrowup: [0, 0, -s], s: [0, 0, s], arrowdown: [0, 0, s],
                        a: [-s, 0, 0], arrowleft: [-s, 0, 0], d: [s, 0, 0], arrowright: [s, 0, 0],
                        e: [0, s, 0], q: [0, -s, 0] };
        if (moves[k]) this.move(moves[k]);
        else if (k === "f" && (this.selected || this.hover)) this.flyToNode(this.selected || this.hover);
        else if (k === "home" || k === "0") this.fit();
        else return;
        e.preventDefault();
      });
      if (typeof window !== "undefined") window.addEventListener("resize", () => this.resize());
    }

    /**
     * Which node names to draw, and where. The focus, the centre, the selection and the
     * focus's neighbours always; otherwise any node drawn large enough to read -- so zooming
     * in names more of them -- nearest and most depended-on first, skipping any label that
     * would overlap one already placed. Text grows with closeness and fades with depth.
     */
    labels(proj, focus, near) {
      const m = this.model;
      if (!proj.length) return [];
      let dmin = Infinity, dmax = -Infinity;
      for (const p of proj) { dmin = Math.min(dmin, p.depth); dmax = Math.max(dmax, p.depth); }
      const must = (n) => n === focus || n === m.center || n === this.selected || (near && near.has(n))
        || (this.spot && this.spot.has(n.id));
      const size = (p) => Math.max(1.5, p.n.r * p.s);
      const W = this.canvas.clientWidth || 600, H = this.canvas.clientHeight || 400;
      const onScreen = (p) => p.x > -20 && p.x < W && p.y > -10 && p.y < H + 10;      // don't spend the budget off-screen
      const important = (n) => n.change || n.finding || n.score >= 0.4;
      const cands = proj.filter((p) => onScreen(p) && (must(p.n) || important(p.n) || size(p) >= 4.5 || (size(p) >= 3 && (p.n.dependents || 0) >= 5)));
      cands.sort((a, b) => (must(b.n) - must(a.n)) || (!!important(b.n) - !!important(a.n)) || (size(b) - size(a)) || ((b.n.dependents || 0) - (a.n.dependents || 0)));
      const ctx = this.ctx, placed = [], out = [];
      const fits = (box) => !placed.some((q) => box[0] < q[2] && box[2] > q[0] && box[1] < q[3] && box[3] > q[1]);
      // file names first, once a file's ball is big enough on screen to be worth naming
      const files = [];
      for (const c of m.clusters.values()) {
        if (!c.groups || c.groups.size < 2) continue;
        for (const g of c.groups.values()) {
          const p = g.label ? this.project(g.center) : null;
          if (!p) continue;
          const pr = g.r * p.s;
          if (pr >= 22) files.push({ g, c, p, pr });
        }
      }
      files.sort((a, b) => b.pr - a.pr);
      for (const f of files.slice(0, 80)) {
        const px = Math.max(10, Math.min(16, 8 + f.pr * 0.04));
        ctx.font = `600 ${px}px system-ui, sans-serif`;
        const text = countLabel(f.g.label, f.g.size), w = ctx.measureText ? ctx.measureText(text).width : text.length * px * 0.55;
        const x = f.p.x - w / 2, y = f.p.y - Math.min(f.pr, 400) - px * 0.6, h = px * 1.15;
        const box = [x - 2, y - h / 2, x + w + 2, y + h / 2];
        if (!fits(box)) continue;
        placed.push(box);
        const far = (f.p.depth - dmin) / Math.max(1, dmax - dmin);
        out.push({ text, x, y, size: px, strong: true, alpha: 0.9 - 0.5 * Math.max(0, Math.min(1, far)), color: f.c.color });
      }
      const occluded = (box, depth, self) => {
        if (!this.occluders) return false;
        const CELL = 48;
        for (let gx = Math.floor(box[0] / CELL); gx <= Math.floor(box[2] / CELL); gx++)
          for (let gy = Math.floor(box[1] / CELL); gy <= Math.floor(box[3] / CELL); gy++)
            for (const o of this.occluders.get(gx + "," + gy) || []) {
              if (o.n === self || o.depth >= depth - 1) continue;
              const cx = Math.max(box[0], Math.min(o.x, box[2])), cy = Math.max(box[1], Math.min(o.y, box[3]));
              if ((cx - o.x) ** 2 + (cy - o.y) ** 2 < o.r * o.r) return true;      // a nearer element covers it
            }
        return false;
      };
      for (const p of cands) {
        if (out.length >= 160) break;
        if (p.inside) continue;                    // already named inside its element
        const strong = must(p.n) || !!important(p.n);
        const px = Math.max(10, Math.min(15, 8 + size(p) * 0.6));
        ctx.font = `${strong ? "600 " : ""}${px}px system-ui, sans-serif`;
        const text = p.n.name, w = ctx.measureText ? ctx.measureText(text).width : text.length * px * 0.55;
        const x = p.x + size(p) + 3, y = p.y, h = px * 1.15;
        const box = [x - 2, y - h / 2, x + w + 2, y + h / 2];
        if (!strong && (!fits(box) || occluded(box, p.depth, p.n))) continue;   // the highlighted chain shows through
        placed.push(box);
        const far = (p.depth - dmin) / Math.max(1, dmax - dmin);
        const tint = this.colorBy === "change" && p.n.change ? CHANGE.changed : this.colorBy === "change" && p.n.score >= 0.4 ? CHANGE.hot : null;
        out.push({ n: p.n, text, x, y, size: px, strong, color: tint, alpha: strong ? 1 : 1 - 0.5 * far });
      }
      return out;
    }

    /** One wheel notch toward (``f`` > 1) or away from the point under ``(px, py)``. */
    wheelFly(px, py, f) {
      const w = this.canvas.clientWidth || 600, h = this.canvas.clientHeight || 400, fz = this.focal * this.zoom;
      const ray = [(px - w / 2 - this.tx) / fz, -(py - h / 2 - this.ty) / fz, -1];
      const len = Math.hypot(...ray), dir = ray.map((x) => x / len);
      // the step shrinks with the distance ahead, but never to nothing: you can always fly on
      const amount = Math.sign(f - 1) * Math.max(Math.abs(this.orbitDist * (1 - 1 / f)), this.model.extent() * 0.02);
      this.move(dir.map((x) => x * amount));
      this.orbitDist = Math.max(40, this.orbitDist / f);
    }

    /** Detach the window listeners (the canvas is replaced when switching views). */
    destroy() { this.stop(); if (typeof window !== "undefined") window.removeEventListener("mouseup", this._up); }

    _select(n) { this.selected = n; this.dirty = true; if (this.h.onSelect) this.h.onSelect(n); }

    _color(n) {
      if (this.colorBy === "change") {
        const who = n.by && n.by.length && this.people && this.people[n.by[0]];
        if (n.removed) return CHANGE.removed;
        if (n.change) return who || CHANGE.changed;
        if (n.score > 0) return n.score >= 0.7 ? CHANGE.hot : n.score >= 0.4 ? "#ff8a6a" : "#ffb08f";
        const c = this.model.clusters.get(n.cluster);
        return c ? shade(c.color, -0.45) : CHANGE.calm;     // the rest of the project, quiet
      }
      if (this.colorBy === "layer") return LAYER_COLORS[n.layer] || LAYER_COLORS.unknown;
      const c = this.model.clusters.get(n.cluster);
      return c ? c.color : "#8b949e";
    }

    draw() {
      const ctx = this.ctx, m = this.model, C = this.colors, W = this.canvas.width, H = this.canvas.height;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      const proj = [];
      for (const n of m.nodes) { const p = this.project([n.x, n.y, n.z]); if (p) { p.n = n; proj.push(p); } }
      this.proj = proj;
      if (!proj.length) return;
      const at = new Map(proj.map((p) => [p.n, p]));
      let dmin = Infinity, dmax = -Infinity;
      for (const p of proj) { dmin = Math.min(dmin, p.depth); dmax = Math.max(dmax, p.depth); }
      const fade = (depth) => 1 - 0.5 * ((depth - dmin) / Math.max(1, dmax - dmin));    // far is fainter
      const focus = this.hover || this.selected;
      const near = focus ? m.neighbours(focus) : null;

      // Edges between clusters are drawn as one chord per pair of clusters, as thick as the
      // number of edges it stands for; a node in focus shows its own edges instead.
      const bundles = new Map();
      for (const e of m.edges) {
        if (e.s.cluster === e.t.cluster || (focus && (e.s === focus || e.t === focus))) continue;
        const k = e.s.cluster < e.t.cluster ? e.s.cluster + "\u0000" + e.t.cluster : e.t.cluster + "\u0000" + e.s.cluster;
        bundles.set(k, (bundles.get(k) || 0) + 1);
      }
      for (const [k, count] of bundles) {
        const [ka, kb] = k.split("\u0000"), ca = m.clusters.get(ka), cb = m.clusters.get(kb);
        if (!ca || !cb) continue;
        const a = this.project(ca.anchor), b = this.project(cb.anchor);
        if (!a || !b) continue;
        ctx.globalAlpha = (focus || this.spot ? 0.05 : 0.2) * fade((a.depth + b.depth) / 2);
        ctx.strokeStyle = C.chord; ctx.lineWidth = Math.min(6, 0.8 + Math.log2(1 + count) * 0.8);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
      const picked = this.selected, pickedFile = picked && picked.path;
      const strong = [];                           // the picked / hovered node's edges, drawn last
      for (const e of m.edges) {
        const a = at.get(e.s), b = at.get(e.t);
        if (!a || !b) continue;
        if (focus && (e.s === focus || e.t === focus)) { strong.push([e, a, b, 1]); continue; }
        if (pickedFile && (e.s.path === pickedFile || e.t.path === pickedFile)) { strong.push([e, a, b, 0.55]); continue; }
        const cross = e.s.cluster !== e.t.cluster;
        if (cross) continue;                                                       // bundled above
        const dim = focus && e.s !== focus && e.t !== focus;
        const otherFile = !cross && e.s.group !== e.t.group;                     // between files: in the background
        const live = this.colorBy === "change" && (e.s.change || e.s.score > 0) && (e.t.change || e.t.score > 0);
        ctx.globalAlpha = (dim ? 0.06 : (0.3 + 0.45 * e.confidence) * (otherFile && !focus ? 0.5 : 1)) * fade((a.depth + b.depth) / 2)
          * (this.spot ? 0.25 : 1);
        ctx.strokeStyle = live ? C.halo + "1)" : C.edge;
        ctx.lineWidth = dim ? 0.7 : live ? 2.4 : 1.3;
        ctx.setLineDash(e.confidence < 0.6 ? [4, 3] : []);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
      ctx.setLineDash([]);
      // edges of the picked node (and, fainter, of the rest of its file): a glow, then a line
      // coloured by what the edge is (calls, reads, writes, inherits...)
      for (const [e, a, b, w] of strong) {
        const col = EDGE_COLORS[e.kind] || "#8b949e";
        ctx.globalAlpha = 0.25 * w; ctx.strokeStyle = col; ctx.lineWidth = 6 * w + 1;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        ctx.globalAlpha = 0.95 * w; ctx.lineWidth = 2.2 * w + 0.6;
        ctx.setLineDash(e.confidence < 0.6 ? [5, 3] : []);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
      ctx.setLineDash([]);

      proj.sort((a, b) => b.depth - a.depth);                                         // back to front
      // what each label may be hidden behind: the screen discs of nearer nodes
      this.occluders = new Map();
      const CELL = 48;
      const radiusOn = (p) => Math.max(1.5, p.n.r * p.s * (p.n === m.center ? 1.3 : 1) * (this.spot && this.spot.has(p.n.id) ? 1.7 : 1));
      // where every element sits on screen, so a name can tell how much room it has
      const spots = new Map(), SPOT = 96;
      for (const p of proj) {
        p.rr = radiusOn(p);
        const key = Math.floor(p.x / SPOT) + "," + Math.floor(p.y / SPOT);
        (spots.get(key) || spots.set(key, []).get(key)).push(p);
      }
      /** Half the width a name centred on ``p`` may take before it runs into a neighbour. */
      const roomAt = (p, band) => {
        let room = 240;
        const kx = Math.floor(p.x / SPOT), ky = Math.floor(p.y / SPOT);
        for (let gx = kx - 3; gx <= kx + 3; gx++) for (let gy = ky - 1; gy <= ky + 1; gy++) {
          for (const q of spots.get(gx + "," + gy) || []) {
            if (q === p || Math.abs(q.y - p.y) > band + q.rr) continue;   // not on the name's line
            room = Math.min(room, Math.abs(q.x - p.x) - q.rr - 4);
          }
        }
        return Math.max(0, room);
      };
      for (const p of proj) {
        const n = p.n, rr = p.rr;
        const lit = this.spot ? this.spot.has(n.id) : null;
        const dim = (focus && n !== focus && !near.has(n)) || lit === false;
        ctx.globalAlpha = (dim ? (lit === false ? 0.12 : 0.2) : 1) * fade(p.depth);
        const h = Math.max(this.heat[n.id] || 0, n.impact && n !== m.center ? n.impact : 0);
        if (h > 0.05) {
          ctx.fillStyle = C.halo + Math.min(0.75, 0.15 + h * 0.6) + ")";
          ctx.beginPath(); ctx.arc(p.x, p.y, rr + 3 + Math.min(h * 8 * p.s, rr * 0.7), 0, Math.PI * 2); ctx.fill();
        }
        if (n.change && this.colorBy === "change") {          // what changed glows
          const g = ctx.createRadialGradient ? ctx.createRadialGradient(p.x, p.y, rr * 0.6, p.x, p.y, rr * 2.4) : null;
          if (g) {
            g.addColorStop(0, "rgba(242,169,59,0.45)"); g.addColorStop(1, "rgba(242,169,59,0)");
            ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, rr * 2.4, 0, Math.PI * 2); ctx.fill();
          }
        }
        drawShape(ctx, shapeOf(n), p.x, p.y, rr, lit ? this.spotColour : this._color(n));
        if (lit) {
          ctx.lineWidth = 1.6; ctx.strokeStyle = this.spotColour; ctx.setLineDash([3, 3]);
          ctx.beginPath(); ctx.arc(p.x, p.y, rr + 4, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
        }
        const flagged = n.finding || this.flagged.has(n.path);
        if (n === m.center || n === this.selected || flagged) {
          ctx.lineWidth = flagged && n !== this.selected ? 2.4 : 1.8;
          ctx.strokeStyle = flagged && n !== this.selected ? CHANGE.finding : C.ring;
          ctx.beginPath(); ctx.arc(p.x, p.y, rr + 2.5, 0, Math.PI * 2); ctx.stroke();
        }
        // close enough, the name goes inside the element, drawn with it in depth order: a
        // nearer element painted later covers it, as it should
        p.inside = false;
        if (rr >= 16 && ctx.measureText) {
          const px = Math.max(9, Math.min(15, rr * 0.42));
          ctx.font = `600 ${px}px system-ui, sans-serif`;
          // the whole name when there is room for it, even past the element's edge; cut short
          // only when a neighbour is close enough that the full name would run into it
          const fit = Math.max(rr * 0.85, Math.min(roomAt(p, px * 0.6), 240));
          let text = n.name;
          while (text.length > 3 && ctx.measureText(text).width > fit * 2) text = text.slice(0, -2) + "…";
          if (ctx.measureText(text).width <= fit * 2 + 1) {
            ctx.textAlign = "center"; ctx.textBaseline = "middle";
            ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.45)"; ctx.strokeText(text, p.x, p.y);
            ctx.fillStyle = "#fff"; ctx.fillText(text, p.x, p.y);
            ctx.textAlign = "left"; p.inside = true;
          }
        }
        const k0 = Math.floor(p.x / CELL), k1 = Math.floor(p.y / CELL), span = Math.ceil(rr / CELL);
        for (let gx = k0 - span; gx <= k0 + span; gx++) for (let gy = k1 - span; gy <= k1 + span; gy++) {
          const key = gx + "," + gy;
          (this.occluders.get(key) || this.occluders.set(key, []).get(key)).push({ x: p.x, y: p.y, r: rr, depth: p.depth, n });
        }
      }

      // cluster names just outside each ball; node names for the focus and its neighbours
      ctx.globalAlpha = 1; ctx.textBaseline = "middle"; ctx.textAlign = "center";
      for (const c of m.clusters.values()) {
        const out = c.ballR + 24;
        const p = this.project([c.anchor[0] + c.dir[0] * out, c.anchor[1] + c.dir[1] * out, c.anchor[2] + c.dir[2] * out]);
        if (!p) continue;
        ctx.globalAlpha = 0.35 + 0.65 * fade(p.depth);
        ctx.font = `600 ${Math.max(10, Math.min(16, 13 * p.s * 1.6))}px system-ui, sans-serif`;
        const name = countLabel(c.key, c.size);
        ctx.lineWidth = 3; ctx.strokeStyle = C.bg; ctx.strokeText(name, p.x, p.y);
        ctx.fillStyle = c.color; ctx.fillText(name, p.x, p.y);
      }
      ctx.textAlign = "left"; ctx.globalAlpha = 1; ctx.font = "11px system-ui, sans-serif"; ctx.fillStyle = C.fg;
      ctx.lineWidth = 3; ctx.strokeStyle = C.bg;
      for (const lab of this.labels(proj, focus, near)) {
        ctx.globalAlpha = lab.alpha; ctx.fillStyle = lab.color || C.fg;
        ctx.font = `${lab.strong ? "600 " : ""}${lab.size}px system-ui, sans-serif`;
        ctx.strokeText(lab.text, lab.x, lab.y); ctx.fillText(lab.text, lab.x, lab.y);
      }
      ctx.globalAlpha = 1;
      // live edits: rings spreading from what just changed
      drawPulses(this, (q) => {
        const n = q.id && m.byId.get(q.id);
        const p = this.project(n ? [n.x, n.y, n.z] : q.at || [0, 0, 0]);
        return p && { x: p.x, y: p.y, r: Math.max(4, (n ? n.r : 6) * p.s), grow: 26 };
      });
    }
  }

  return { GraphModel3D, GraphView3D, Quat, sphereDirections, clusterNodes, communities, languageOf, directoryOf, shapeOf, drawShape,
           addPulses, drawPulses, PULSE_COLORS,
           directoryDepth, CLUSTER_COLORS, CHANGE };
});
