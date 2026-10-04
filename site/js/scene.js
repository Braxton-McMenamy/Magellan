// The Scene (scene.html): one map, as big as the screen, laid out like a 3D editor -- what to
// show on the left, the viewport in the middle, the picked dot's properties on the right. Two
// views of the same map: 3D (the whole project, js/scene3d.js) and Flow (the change, hop by
// hop, js/map.js). What it shows:
//   #team            your team's work in progress, everyone at once (the Team suite's connection)
//   #team/<name>     one person's work
//   #itself          Magellan Lite's own code (data/project.js): there before anything is connected
//
// Pick a dot, then "What depends on it": Flow shows everything that would feel a change to it,
// hop by hop -- the question to ask before changing old code.

(() => {
  const { h, fill, verdict, plural, finding } = MagellanDom;
  const $ = (id) => document.getElementById(id);

  const VIEWS = {
    "3d": { label: "3D", draw: (box, map, opts) => MagellanScene3D.render(box, map, opts) },
    flow: { label: "Flow", draw: (box, map, opts) => MagellanMap.render(box, map, opts) },
  };
  const remember = {
    get() { try { return sessionStorage.getItem("magellan-scene-view") || ""; } catch { return ""; } },
    set(v) { try { sessionStorage.setItem("magellan-scene-view", v); } catch { /* private mode */ } },
  };
  let view = VIEWS[remember.get()] ? remember.get() : "3d";

  const itself = window.MAGELLAN_PROJECT || null;
  let team = null, people = {}, current = null, shown = null;
  let following = null;          // a picked dot whose dependents Flow shows: its node

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
        h("p", { class: "small" }, "Pick a dot to see what it is."));
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
      h("p", {}, h("button", { type: "button", class: "follow-btn", onclick: () => follow(node) },
        "What depends on it →")),
      h("p", {}, h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); properties(src); } }, "← the whole checklist")));
  }

  /** The skull's list: code nothing in the project uses. Each one flies the 3D view to it. */
  function deadPanel(src, list) {
    const fly = (n) => {
      const v = shown && shown.view;
      const node = v && v.model.byId.get(n.id);
      if (node) { v.setSelected(n.id); v.flyToNode(node); }
    };
    fill($("scene-side"),
      h("h2", {}, "\u{1F480} Probably dead: ", String(list.length)),
      h("p", { class: "small" }, "Nothing in the project mentions these names except where they are defined. "
        + "An entry point, a plugin, or a call by a computed name may still use them: check before deleting."),
      h("ul", { class: "dead-list" }, list.map((n) => h("li", {},
        h("button", { type: "button", onclick: () => fly(n) },
          h("span", { class: "dead-name" }, n.label || n.id),
          h("span", { class: "where" }, `${n.path}:${n.line}`))))),
      h("p", {}, h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); properties(src); } }, "← the whole checklist")));
  }

  /** Flow, from one dot: everything that depends on it. */
  function follow(node) {
    following = node;
    view = "flow";
    remember.set(view);
    draw();
  }

  // -- the viewport ---------------------------------------------------------------------------------
  function draw(animate = true) {
    const fallback = team && team.team ? "team" : itself ? "itself" : "";
    let key = decodeURIComponent(location.hash.slice(1)) || fallback;
    let src = source(key);
    if (!src && !key.startsWith("team")) src = source((key = fallback));
    current = src ? key : current;
    outliner();
    switcher();
    const box = $("scene-map");
    if (shown && shown.destroy) shown.destroy();
    shown = null;
    if (!src) {
      $("scene-title").textContent = "Scene";
      $("scene-sub").textContent = key.startsWith("team") ? "Waiting for your team's work…" : "";
      fill(box);
      fill($("scene-side"));
      return;
    }
    let map = src.report.map;
    $("scene-title").textContent = src.title;
    $("scene-sub").textContent = src.sub;
    if (view === "flow" && following && map.nodes.some((n) => n.id === following.id)) {
      map = reachFrom(map, following.id);
      $("scene-title").textContent = `What depends on ${following.label || following.id}`;
      $("scene-sub").textContent = `${plural(map.dependents, "definition")} would feel a change to it · ${following.path}:${following.line}`;
    } else if (view === "flow" && !map.nodes.some((n) => n.change || n.score > 0)) {
      $("scene-sub").textContent = "Nothing changed here: pick a dot in 3D, then \u201cWhat depends on it\u201d.";
    }
    document.title = `${src.title} · Scene · Magellan Lite`;
    shown = VIEWS[view].draw(box, map, {
      people: src.people, label: src.title, animate,
      onDead: (list) => (list ? deadPanel(src, list) : properties(src)),
      onPick: (n, g) => {
        box.querySelectorAll(".picked").forEach((x) => x.classList.remove("picked"));
        if (g) g.classList.add("picked");
        properties(src, n);
      },
    });
    if (animate && shown && shown.play) shown.play();
    properties(src, view === "flow" && following ? following : undefined);
  }

  function switcher() {
    fill($("views"), ...Object.entries(VIEWS).map(([k, v]) => h("button", {
      type: "button", "aria-pressed": String(k === view),
      title: k === "3d" ? "The whole project in 3D" : "The change, hop by hop",
      onclick: () => { view = k; remember.set(k); if (k === "3d") following = null; draw(); },
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
          if (!location.hash || location.hash.startsWith("#team")) draw(); else outliner();
        },
        onError: (err) => { $("scene-status").textContent = err.message; },
      });
    } catch (err) {
      $("scene-status").textContent = err.message;
    }
  }

  addEventListener("hashchange", () => { following = null; draw(); });
  let resized = null;
  addEventListener("resize", () => {
    if (view !== "flow") return;                      // the 3D view resizes itself
    clearTimeout(resized); resized = setTimeout(() => draw(false), 200);
  });
  draw();
  window.MagellanScene = { VIEWS, redraw: draw };
})();
