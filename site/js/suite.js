// The Team suite (suite.html): connect a repository, then watch everyone's shared work in
// progress, checked alone, two by two and all together (js/teamfeed.js does the reading and
// the checking). Everything that came from the repository goes on the page as text (js/dom.js).

(() => {
  const { h, fill, verdict, plural, finding } = MagellanDom;
  const $ = (id) => document.getElementById(id);
  const DEMO = "Braxton-McMenamy/Magellan";
  const KEY_ME = "magellan-team-me";
  const RANK = { block: 0, review: 1, ok: 2 };

  let feed = null, result = null, people = {};

  // -- connecting ------------------------------------------------------------------------------
  function showError(text) {
    $("connect-error").textContent = text || "";
    $("connect-error").hidden = !text;
  }

  function start(repo, token) {
    showError("");
    if (feed) feed.stop();
    try {
      feed = MagellanTeam.connect({ repo, token, onStatus: status, onResult: show, onError: failed });
    } catch (err) {
      feed = null;
      return showError(err.message);
    }
    $("token").value = "";                       // the field doesn't keep it; the tab's session does
    $("connect-forget").hidden = false;
    $("live").hidden = false;
    fill($("live-repo"), h("a", { href: `https://github.com/${feed.repo}`, rel: "noopener" }, feed.repo));
    try { history.replaceState(null, "", `?repo=${encodeURIComponent(feed.repo)}`); } catch { /* file:// */ }
    status(`Reading ${feed.repo}…`);
  }

  function status(text) {
    $("live-status").textContent = text;
    const rate = feed && feed.rate();
    $("live-rate").textContent = rate && rate.limit
      ? `· GitHub: ${rate.remaining} of ${rate.limit} requests left this hour` : "";
  }

  function failed(err) {
    status("");
    if (!result) $("live").hidden = true;
    showError(err.message || String(err));
    if (err.status === 401) MagellanTeam.forget();
  }

  $("connect").addEventListener("submit", (ev) => {
    ev.preventDefault();
    start($("repo").value, $("token").value.trim());
  });
  $("connect-demo").addEventListener("click", () => {
    $("repo").value = DEMO;
    start(DEMO, $("token").value.trim());
  });
  $("connect-forget").addEventListener("click", () => {
    if (feed) feed.stop();
    feed = null;
    result = null;
    MagellanTeam.forget();
    $("live").hidden = true;
    $("connect-forget").hidden = true;
    $("repo").value = "";
    showError("");
    try { history.replaceState(null, "", location.pathname); } catch { /* file:// */ }
  });
  $("live-now").addEventListener("click", () => feed && feed.now());

  // -- tabs ----------------------------------------------------------------------------------------
  const tabs = [$("tab-team"), $("tab-you")];
  function select(tab) {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      $(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (result) (tab === tabs[0] ? drawTeamMap : drawYou)();   // maps size to a visible box
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (ev) => {
      const step = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
      if (step) { const next = tabs[(i + step + tabs.length) % tabs.length]; next.focus(); select(next); }
    });
  });

  // -- what came back --------------------------------------------------------------------------------
  function show(r) {
    result = r;
    people = MagellanMap.colours(r.people.map((p) => p.name));
    const none = !r.people.length;
    $("team-empty").hidden = !none;
    $("team-body").hidden = none;
    const took = r.ms === undefined ? "" : ` · checked in ${r.ms < 1000 ? `${r.ms} ms` : `${(r.ms / 1000).toFixed(1)} s`}, in this browser`;
    status(`Updated ${new Date(r.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}${took}`);
    fillMe();
    if (none) return drawYou();
    drawSummary();
    drawPeople();
    drawPairs();
    drawOverlap();
    if (!$("panel-team").hidden) drawTeamMap();
    if (!$("panel-you").hidden) drawYou();
  }

  const member = (name) => result.members.find((m) => m.name === name);
  const person = (name) => result.people.find((p) => p.name === name) || {};
  const dot = (name) => {
    const d = h("i", { class: "who-dot", "aria-hidden": "true" });
    d.style.setProperty("--who", people[name] || "var(--muted)");
    return d;
  };
  const worst = (vs) => vs.reduce((a, b) => (RANK[b] < RANK[a] ? b : a), "ok");

  function drawSummary() {
    const bad = result.pairs.filter((p) => p.verdict !== "ok");
    const v = worst([...result.pairs.map((p) => p.verdict), result.team.verdict]);
    const line = bad.length
      ? `${plural(bad.length, "pair")} of changes ${bad.length === 1 ? "breaks" : "break"} only when combined.`
      : result.people.length > 1 ? "Everyone's work fits together." : "One person is sharing: nothing to combine yet.";
    fill($("team-summary"), 
      h("div", { class: `summary-card card is-${v}` }, verdict(v),
        h("div", {}, h("strong", {}, line), " ",
          h("span", { class: "muted" }, `${plural(result.people.length, "person", "people")} sharing work in progress.`))));
  }

  function drawPeople() {
    fill($("people"), ...result.members.map((m) => {
      const p = person(m.name);
      const files = p.files || [];
      return h("article", { class: "card person-card" },
        h("div", { class: "person-head" }, dot(m.name), h("strong", {}, m.name), verdict(m.verdict)),
        h("p", { class: "muted small" }, `shared ${MagellanTeam.ago(p.when) || "—"} · ${plural(files.length, "file")}`),
        h("p", { class: "small" }, m.changes.length
          ? m.changes.slice(0, 4).map((c) => `${c.name.split(".").slice(-2).join(".")} (${c.kind})`).join(", ") +
            (m.changes.length > 4 ? ` and ${m.changes.length - 4} more` : "")
          : "no definitions changed"),
        m.findings.length ? h("p", { class: "small" }, `${plural(m.findings.length, "finding")} in their own work`) : null,
        p.many ? h("p", { class: "small muted" }, "More than 300 files changed: GitHub lists only the first 300.") : null);
    }));
  }

  function pairCard(p) {
    const fine = p.verdict === "ok" && !p.findings.length;
    return h("details", { class: `card pair is-${p.verdict}`, open: !fine && result.pairs.length <= 6 },
      h("summary", {},
        h("span", { class: "pair-names" }, dot(p.a), p.a, " + ", dot(p.b), p.b),
        fine ? h("span", { class: "muted small" }, "fine together") : verdict(p.verdict)),
      fine ? h("p", { class: "muted small" }, "No problem that only the combination has.")
        : h("ul", { class: "findings" }, p.findings.map(finding)),
      p.overlap.length ? h("p", { class: "small" }, "Both edit ", p.overlap.join(", "),
        ": the combined check takes ", p.a, "'s version of each.") : null);
  }

  function drawPairs() {
    const sorted = [...result.pairs].sort((x, y) => RANK[x.verdict] - RANK[y.verdict] || y.findings.length - x.findings.length);
    fill($("pairs"), ...(sorted.length ? sorted.map(pairCard)
      : [h("p", { class: "muted" }, "Pairs appear when two people are sharing.")]));
  }

  function drawOverlap() {
    const o = result.team.overlap || [];
    fill($("overlap"), ...(o.length ? [h("div", { class: "card overlap" },
      h("strong", {}, "Edited by more than one person"),
      h("ul", {}, o.map((x) => h("li", {}, h("code", {}, x.path), ": ", x.names.join(", ")))),
      h("p", { class: "muted small" }, "Expect a merge conflict here. The whole-team map uses the first name's version."))] : []));
  }

  // what a picked dot is: its place, who changed it, the findings in it
  function pickPanel(box, node, report) {
    const here = (report.findings || []).filter((f) => f.path === node.path && f.line >= node.line);
    const by = node.by || [], reached = node.reached_by || [];
    fill(box, 
      h("h4", {}, node.label),
      h("p", { class: "where small" }, `${node.path}:${node.line}`),
      h("p", { class: "small" }, node.change ? `Changed (${node.change})` : node.score > 0
        ? `Reached by the change, ${plural(node.hops, "hop")} away (score ${node.score.toFixed(2)})` : "Used by the change"),
      by.length ? h("p", { class: "small" }, "Changed by ", ...by.flatMap((n, i) => [i ? " and " : "", dot(n), n])) : null,
      reached.length ? h("p", { class: "small" }, "Reached by ", reached.join(", "), "'s change") : null,
      node.finding && here.length ? h("ul", { class: "findings" }, here.slice(0, 3).map(finding)) : null);
  }

  function drawTeamMap() {
    if (!result || !result.team) return;
    const box = $("team-map");
    MagellanMap.render(box, result.team.map, {
      people, animate: false, label: "Everyone's changes at once, and what they reach",
      onPick: (n, g) => {
        box.querySelectorAll(".picked").forEach((x) => x.classList.remove("picked"));
        g.classList.add("picked");
        pickPanel($("team-pick"), n, result.team);
      },
    });
  }

  // -- you -------------------------------------------------------------------------------------------
  function fillMe() {
    const names = result.people.map((p) => p.name);
    let me = "";
    try { me = sessionStorage.getItem(KEY_ME) || ""; } catch { /* private mode */ }
    const sel = $("me");
    fill(sel, h("option", { value: "" }, names.length ? "Pick your name" : "Nobody is sharing yet"),
      ...names.map((n) => h("option", { value: n, selected: n === me }, n)));
  }
  $("me").addEventListener("change", () => {
    try { sessionStorage.setItem(KEY_ME, $("me").value); } catch { /* private mode */ }
    drawYou();
  });

  function drawYou() {
    const body = $("you-body");
    const me = result && $("me").value;
    if (!me || !member(me)) {
      return fill(body, h("p", { class: "muted" },
        "Pick the name you share under (git's user.name, in lower case) to see your work against everyone else's."));
    }
    const m = member(me);
    const mine = result.pairs.filter((p) => p.a === me || p.b === me)
      .map((p) => ({ ...p, other: p.a === me ? p.b : p.a }))
      .sort((x, y) => RANK[x.verdict] - RANK[y.verdict]);
    const mapBox = h("div", { class: "map-box card" });
    fill(body, 
      h("div", { class: `summary-card card is-${m.verdict}` }, verdict(m.verdict),
        h("div", {}, h("strong", {}, "Your work on its own"), " ",
          h("span", { class: "muted" }, m.findings.length ? `${plural(m.findings.length, "finding")}.` : "nothing to check."))),
      m.findings.length ? h("ul", { class: "findings card" }, m.findings.map(finding)) : null,
      h("h3", {}, "Against each teammate"),
      mine.length ? h("div", { class: "pairs" }, mine.map((p) => h("div", { class: `card pair is-${p.verdict}` },
        h("div", { class: "pair-row" }, h("span", { class: "pair-names" }, "you + ", dot(p.other), p.other),
          p.verdict === "ok" && !p.findings.length ? h("span", { class: "muted small" }, "fine together") : verdict(p.verdict)),
        p.findings.length ? h("ul", { class: "findings" }, p.findings.map(finding)) : null)))
        : h("p", { class: "muted" }, "Nobody else is sharing yet."),
      h("h3", {}, "Your change and what it reaches"),
      mapBox);
    MagellanMap.render(mapBox, m.map, { animate: false, label: "Your change and the code it reaches" });
  }

  // -- start: a link with ?repo=, or this tab's earlier connection -----------------------------
  const asked = new URLSearchParams(location.search).get("repo");
  const was = MagellanTeam.saved();
  if (asked && MagellanGitHub.parseRepo(asked)) {
    $("repo").value = asked;
    start(asked, asked === was.repo ? was.token : "");
  } else if (was.repo) {
    $("repo").value = was.repo;
    start(was.repo, was.token);
  }
  let resized = null;
  addEventListener("resize", () => {
    clearTimeout(resized);
    resized = setTimeout(() => result && result.team && (!$("panel-team").hidden ? drawTeamMap() : drawYou()), 200);
  });
})();
