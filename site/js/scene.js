// The Scene (scene.html): one map, as big as the screen. What it shows comes from the rail:
//   #team            your team's work in progress, everyone at once (the Team suite's connection)
//   #team/<name>     one person's work
//   #incident/<name> a famous failure, replayed (data/incidents.js)
// Pick a dot to see what it is, who changed it and what was found there.
//
// TODO(braxton): the 3D and 2D scene views. Each view is an entry in VIEWS below:
//   VIEWS["3d"] = { label: "3D", draw(box, map, opts) { ... } }
//   draw() gets the same map as MagellanMap.render (nodes with change/score/hops/finding/by,
//   and edges) and the same opts (people's colours, onPick). Write the view in its own file
//   (js/scene3d.js), add its <script> to scene.html, and the switch above the map appears by
//   itself once there are two views. Keep it in site/js/ so the extension's map panel can copy
//   it too (editors/vscode/shared.json).

(() => {
  const { h, fill, verdict, plural, finding } = MagellanDom;
  const $ = (id) => document.getElementById(id);

  const VIEWS = { "2d": { label: "Map", draw: (box, map, opts) => MagellanMap.render(box, map, opts) } };
  let view = "2d";

  const incidents = Array.isArray(window.MAGELLAN_INCIDENTS) ? window.MAGELLAN_INCIDENTS : [];
  let team = null, feed = null, people = {}, current = null;

  // -- what can be shown -------------------------------------------------------------------------
  // each source: { key, title, sub, report: { verdict, findings, map }, people? }
  function source(key) {
    const [kind, name] = key.split("/");
    if (kind === "incident") {
      const inc = incidents.find((i) => i.name === name);
      if (!inc) return null;
      const s = inc.story;
      const cost = (inc.damage || [])[0];
      const what = cost ? `${cost.text || `${cost.value}${cost.suffix || ""}`} ${cost.label}` : "";
      return { key, title: inc.title, sub: [s.what, what].filter(Boolean).join(" · "), report: s };
    }
    if (kind === "team" && team && team.team) {
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

  function rail() {
    const item = (key, label, v, colour) => {
      const dot = colour ? h("i", { class: "who-dot", "aria-hidden": "true" }) : null;
      if (dot) dot.style.setProperty("--who", colour);
      return h("li", {}, h("button", { type: "button", "aria-pressed": String(current === key),
        onclick: () => { location.hash = key; } }, dot, h("span", {}, label), v ? verdict(v) : null));
    };
    const saved = MagellanTeam.saved();
    fill($("rail-team"), ...(team && team.team
      ? [item("team", "Everyone at once", team.team.verdict),
        ...team.members.map((m) => item(`team/${m.name}`, m.name, m.verdict, people[m.name]))]
      : [h("li", { class: "note" }, saved.repo
        ? (team ? "Nobody is sharing work in progress yet." : `Reading ${saved.repo}…`)
        : h("span", {}, h("a", { href: "suite.html" }, "Connect your team's repository"), " in the Team suite, and its work shows here."))]));
    fill($("rail-incidents"), ...incidents.map((i) =>
      item(`incident/${i.name}`, i.title.split(":")[0], i.story && i.story.verdict)));
  }

  // -- drawing -----------------------------------------------------------------------------------
  function side(src, node) {
    const box = $("scene-side");
    const r = src.report;
    if (!node) {
      return fill(box, 
        h("h2", {}, "The checklist"),
        h("p", {}, verdict(r.verdict), " ", r.findings && r.findings.length ? plural(r.findings.length, "finding") : "nothing to check"),
        r.findings && r.findings.length ? h("ul", { class: "findings" }, r.findings.map(finding)) : null,
        h("p", { class: "small" }, "Pick a dot on the map to see what it is."));
    }
    const by = node.by || [], reached = node.reached_by || [];
    const here = (r.findings || []).filter((f) => f.path === node.path && f.line >= node.line);
    const who = (n) => {
      const d = h("i", { class: "who-dot", "aria-hidden": "true" });
      d.style.setProperty("--who", people[n] || "var(--stage-muted)");
      return [d, n];
    };
    fill(box, 
      h("h2", {}, node.id),
      h("p", { class: "where" }, `${node.path}:${node.line} · ${node.kind}`),
      h("p", {}, node.change ? `Changed: ${node.change}${node.removed ? " (deleted)" : ""}` : node.score > 0
        ? `Reached by the change, ${plural(node.hops, "hop")} away. Score ${node.score.toFixed(2)}.` : "Used by the change."),
      by.length ? h("p", {}, "Changed by ", ...by.flatMap((n, i) => [i ? " and " : "", ...who(n)])) : null,
      reached.length ? h("p", {}, "Reached by ", reached.join(" and "), "'s change") : null,
      node.finding && here.length ? h("ul", { class: "findings" }, here.slice(0, 4).map(finding)) : null,
      h("p", {}, h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); side(src); } }, "← the whole checklist")));
  }

  function draw() {
    const key = decodeURIComponent(location.hash.slice(1)) || (team && team.team ? "team" : incidents[0] ? `incident/${incidents[0].name}` : "");
    const src = source(key);
    current = src ? key : current;
    rail();
    if (!src) {
      $("scene-title").textContent = "Scene";
      $("scene-sub").textContent = key.startsWith("team") ? "Waiting for your team's work…" : "";
      fill($("scene-map"));
      fill($("scene-side"));
      return;
    }
    $("scene-title").textContent = src.title;
    $("scene-sub").textContent = src.sub;
    document.title = `${src.title} · Scene · Magellan Lite`;
    const box = $("scene-map");
    const shown = VIEWS[view].draw(box, src.report.map, {
      people: src.people, label: src.title,
      onPick: (n, g) => {
        box.querySelectorAll(".picked").forEach((x) => x.classList.remove("picked"));
        if (g) g.classList.add("picked");
        side(src, n);
      },
    });
    if (shown && shown.play) shown.play();
    side(src);
    views();
  }

  function views() {
    const names = Object.keys(VIEWS);
    $("views").hidden = names.length < 2;
    fill($("views"), ...names.map((k) => h("button", { type: "button", "aria-pressed": String(k === view),
      onclick: () => { view = k; draw(); } }, VIEWS[k].label)));
  }

  // -- the team, live, when the suite connected one in this tab ----------------------------------
  const saved = MagellanTeam.saved();
  if (saved.repo) {
    try {
      feed = MagellanTeam.connect({
        repo: saved.repo, token: saved.token,
        onStatus: (t) => { $("scene-status").textContent = t; },
        onResult: (r) => {
          team = r;
          people = MagellanMap.colours(r.people.map((p) => p.name));
          $("scene-status").textContent = `${r.repo} · updated ${new Date(r.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
          if (!current || current.startsWith("team") || !location.hash) draw(); else rail();
        },
        onError: (err) => { $("scene-status").textContent = err.message; },
      });
    } catch (err) {
      $("scene-status").textContent = err.message;
    }
  }

  addEventListener("hashchange", draw);
  let resized = null;
  addEventListener("resize", () => { clearTimeout(resized); resized = setTimeout(draw, 200); });
  draw();
  window.MagellanScene = { VIEWS, redraw: draw };     // for the 3D view to register itself
})();
