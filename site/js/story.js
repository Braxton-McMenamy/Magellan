// Famous failures, as an animated story: what happened and what it cost, the change, what it
// reaches, the issue, and whether Magellan Lite catches it. Every scene is drawn from
// data/incidents.js, which demo/build_site.py writes by running the real engine.

(() => {
  const root = document.getElementById("story");
  const data = window.MAGELLAN_INCIDENTS;
  if (!root || !Array.isArray(data)) return;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

  // the strongest first: the one Magellan Lite catches today, then the famous ones
  const ORDER = ["sensor-signature-break", "knight-capital-2012", "cloudflare-waf-2019",
    "azure-leap-day-2012", "zune-2008", "therac-25-1986"];
  const rank = (inc) => (ORDER.includes(inc.name) ? ORDER.indexOf(inc.name) : ORDER.length);
  const incidents = data.filter((i) => i.story).sort((a, b) => rank(a) - rank(b));
  if (!incidents.length) return;
  // the address that opens one story: index.html#incident=zune-2008 (see "the address" below)
  const LINK = /^#incident=([A-Za-z0-9._-]{1,80})$/;

  const SCENES = [
    { id: "what", title: "What happened", ms: 9000 },
    { id: "change", title: "The change", ms: 8500 },
    { id: "reach", title: "What it reaches", ms: 9500 },
    { id: "issue", title: "The issue", ms: 9000 },
    { id: "verdict", title: "Caught?", ms: 8000 },
  ];

  const status = (inc) => {
    const v = Object.values(inc.story.rules);
    return v.includes("MISSED") ? "missed" : v.includes("waiting") ? "waiting"
      : v.length ? "caught" : "quiet";
  };
  const STATUS_TEXT = { caught: "caught", waiting: "rule in progress", missed: "missed", quiet: "quiet" };
  const short = (inc) => (inc.damage_of ? `${inc.damage_of.split(",")[0]}-class bug`
    : inc.title.split(/[,:]/)[0].replace(/^Microsoft /, ""));
  const year = (inc) => ((inc.date || inc.damage_of || "").match(/\b(19|20)\d\d\b/) || [""])[0];

  // -- the frame -------------------------------------------------------------------------
  root.innerHTML = `
    <div class="story-tabs" role="tablist" aria-label="Incidents">
      ${incidents.map((inc, i) => `
        <button role="tab" class="story-tab" data-i="${i}" aria-selected="false">
          <span class="tab-year">${esc(year(inc))}</span>
          <span class="tab-name">${esc(short(inc))}</span>
          <span class="tab-status ${status(inc)}">${STATUS_TEXT[status(inc)]}</span>
        </button>`).join("")}
    </div>
    <div class="story-stage" aria-live="polite"></div>
    <div class="story-bar">
      <div class="story-ctls">
        <button class="ctl" data-act="prev" aria-label="Previous scene">&#8249;</button>
        <button class="ctl play" data-act="play" aria-label="Pause"></button>
        <button class="ctl" data-act="next" aria-label="Next scene">&#8250;</button>
      </div>
      <ol class="story-steps">
        ${SCENES.map((s, i) => `<li><button data-scene="${i}"><span class="fill"></span>
          <span class="step-n">${i + 1}</span> <span class="t">${esc(s.title)}</span></button></li>`).join("")}
      </ol>
    </div>`;
  const stage = root.querySelector(".story-stage");
  const tabs = [...root.querySelectorAll(".story-tab")];
  const stepBtns = [...root.querySelectorAll(".story-steps button")];
  const playBtn = root.querySelector('[data-act="play"]');

  // -- the scenes ------------------------------------------------------------------------
  const STATE_TEXT = { changed: "edited in this change", untouched: "nobody touched this file",
    added: "new file", deleted: "deleted in this change" };

  function codePanel(file, opts = {}) {
    const around = opts.around;
    let at = 0;     // a deleted line has no number of its own: it sits after the line before it
    const lines = file.lines.filter(([, n]) => {
      at = n || at;
      return !around || Math.abs(at - around) <= 5;
    });
    const marks = new Set(opts.marks || file.marks || []);
    return `<figure class="code-panel ${file.state}${around ? " focus" : ""}">
      <figcaption><span class="path">${esc(file.path)}</span>
        <span class="file-state">${STATE_TEXT[file.state] || ""}</span></figcaption>
      <pre class="code-lines">${lines.map(([op, n, text]) => `<span class="ln ${
        op === "+" ? "add" : op === "-" ? "del" : ""}${marks.has(n) ? " hit" : ""}"${
        n ? ` data-line="${n}"` : ""}><i class="no">${n ?? ""}</i><b class="op">${
        op === " " ? "" : op}</b>${esc(text) || " "}</span>${
        marks.has(n) && opts.note ? `<span class="ln-note">${opts.note}</span>` : ""}`).join("")}</pre>
    </figure>`;
  }

  function sceneWhat(inc) {
    const dmg = inc.damage || [];
    return `<div class="scene-grid">
      <div>
        ${inc.date ? `<p class="story-date">${esc(inc.date)}</p>` : `<p class="story-date">A worked example</p>`}
        <h3 class="story-title">${esc(inc.title)}</h3>
        <p class="story-text">${esc(inc.what_happened)}</p>
      </div>
      <div class="damage">
        <p class="damage-head">${inc.damage_of ? esc(inc.damage_of.split(",").slice(0, 2).join(",")) : "What it cost"}</p>
        ${dmg.map((d) => `<div class="stat-tile">
          <div class="stat-num" ${d.text ? "" : `data-value="${d.value}"`}
               data-prefix="${esc(d.prefix || "")}" data-suffix="${esc(d.suffix || "")}">${
            d.text ? esc(d.text) : `${esc(d.prefix || "")}${d.value}${esc(d.suffix || "")}`}</div>
          <div class="stat-label">${esc(d.label)}</div></div>`).join("")}
        ${inc.damage_note ? `<p class="damage-note">${esc(inc.damage_note)}</p>` : ""}
        ${inc.damage_of ? `<p class="damage-note">${esc(inc.damage_of)}</p>` : ""}
      </div>
    </div>
    <p class="story-sources">Sources: ${(inc.sources || []).map(esc).join(" · ")}</p>`;
  }

  function sceneChange(inc) {
    const s = inc.story;
    return `<p class="scene-lede">${esc(s.what)}</p>
      <div class="code-panels n${s.files.length}">${s.files.map((f) => codePanel(f, { marks: [] })).join("")}</div>
      <p class="scene-foot">${esc(inc.language || "")}</p>`;
  }

  function sceneReach(inc) {
    const a = inc.story.affected;
    const edited = inc.story.changes.filter((c) => c.kind !== "added").length;
    return `<div class="reach-grid">
      <div class="map-box"></div>
      <div class="reach-side">
        <p class="scene-lede">${a.length
          ? `The change edits ${edited} definition${edited === 1 ? "" : "s"}. Its effect spreads to <em>whatever calls it</em>, then to their callers: ${a.length} definition${a.length === 1 ? "" : "s"} nobody edited can break.`
          : "Nothing else in the project calls the code this change edits."}</p>
        ${a.length ? `<ol class="reach-list">${a.slice(0, 4).map((x) => `<li>
          <b class="score" style="--heat:${0.3 + 0.7 * x.score}">${x.score.toFixed(2)}</b>
          <span><code>${esc(x.name.split(".").slice(-2).join("."))}</code>
          <small>${x.hops} hop${x.hops === 1 ? "" : "s"} · ${esc(x.path)}:${x.line}</small></span></li>`).join("")}</ol>
          <p class="scene-foot">Each hop fades the score: a call ×0.9, reading a value ×0.85.</p>` : ""}
      </div>
    </div>`;
  }

  // where the problem is: the first finding, or the line the rule-in-progress looks at
  function problem(inc) {
    const f = inc.story.findings[0];
    if (f) return { path: f.path, line: f.line, rule: f.rule, severity: f.severity,
      text: f.message, detail: f.detail, fix: f.fix, found: true };
    const c = inc.catch || {};
    const rule = Object.keys(inc.story.rules)[0] || c.rule;
    return { path: c.path, line: c.line, rule, text: c.says, fix: c.fix, found: false };
  }

  function sceneIssue(inc) {
    const p = problem(inc);
    const file = inc.story.files.find((f) => f.path === p.path);
    return `<div class="issue-grid">
      ${file ? codePanel(file, { around: p.line, marks: [p.line], note: "" }) : ""}
      <div class="issue-card ${p.found ? "found" : "pending"}">
        <p class="issue-head">${p.found
          ? `<span class="sev ${esc(p.severity)}">${esc(p.severity)}</span> <code>${esc(p.rule)}</code>`
          : `What <code>${esc(p.rule)}</code> looks for`}</p>
        <p class="issue-where">${esc(p.path || "")}${p.line ? `:${p.line}` : ""}</p>
        <p class="issue-text">${esc(p.text)}</p>
        ${p.found && p.detail ? `<p class="issue-detail">${esc(p.detail)}</p>` : ""}
      </div>
    </div>`;
  }

  function sceneVerdict(inc) {
    const st = status(inc);
    const p = problem(inc);
    const first = (inc.damage || [])[0];
    const cost = first ? (first.text || `${first.prefix || ""}${first.value}${first.suffix || ""}`) : "";
    const where = p.path ? `<code>${esc(p.path)}:${p.line}</code>` : "the problem";
    const alone = `It finds it from the code alone, before anything runs${
      inc.story.files.some((f) => f.state === "untouched") ? ", in a file the change never touched" : ""}.`;
    const head = {
      caught: inc.story.verdict === "block"
        ? `<div class="stamp block">block</div>
          <h3>Magellan Lite stops this commit.</h3>
          <p>As a pre-commit check, <code>magellan-lite check</code> refuses the change until
            ${where} is fixed. ${alone}</p>`
        : `<div class="stamp review">review</div>
          <h3>Magellan Lite flags this change before it ships.</h3>
          <p>The checklist puts ${where} in front of a reviewer, high severity; run the check
            with <code>--fail-on review</code> and the commit is refused until it is fixed.
            ${alone}</p>`,
      waiting: `<div class="stamp waiting">rule in progress</div>
        <h3>The rule that catches this is being written: <code>${esc(p.rule)}</code></h3>
        <p>When it lands, Magellan Lite flags this change before it is committed. Today it
          already maps the change and shows what it reaches.</p>`,
      missed: `<div class="stamp missed">missed</div>
        <h3><code>${esc(p.rule)}</code> exists but stayed quiet here.</h3>
        <p>That is a bug in the rule, and the team's next fix.</p>`,
      quiet: `<div class="stamp quiet">quiet</div><h3>Nothing to flag in this change.</h3>`,
    }[st];
    return `<div class="verdict-stage ${st}">
      ${head}
      ${p.fix ? `<p class="fix"><b>The fix:</b> ${esc(p.fix)}</p>` : ""}
      ${cost && st === "caught" ? `<p class="versus"><span>${esc(cost)}</span> ${esc(first.label)}
        <em>vs</em> <span>&lt; 1 s</span> for the check, before the commit</p>` : ""}
      ${inc.out_of_reach ? `<p class="out-of-reach"><b>What no code check can see:</b> ${esc(inc.out_of_reach)}</p>` : ""}
    </div>`;
  }

  const BUILD = { what: sceneWhat, change: sceneChange, reach: sceneReach, issue: sceneIssue,
    verdict: sceneVerdict };

  // -- entering a scene: its animation -------------------------------------------------------
  let map = null;
  const ENTER = {
    what(el) {
      el.querySelectorAll(".stat-num[data-value]").forEach((n, i) => {
        const v = Number(n.dataset.value), dec = v % 1 ? 1 : 0;
        const show = (x) => { n.textContent = `${n.dataset.prefix}${x.toLocaleString("en-US", {
          minimumFractionDigits: dec, maximumFractionDigits: dec })}${n.dataset.suffix}`; };
        if (reduced()) return show(v);
        show(0);
        const t0 = performance.now() + i * 250, dur = 1700;
        const tick = (t) => {
          const k = Math.min(1, Math.max(0, (t - t0) / dur));
          show(v * (1 - Math.pow(1 - k, 3)));
          if (k < 1 && el.classList.contains("active")) requestAnimationFrame(tick);
          else show(v);
        };
        requestAnimationFrame(tick);
        setTimeout(() => show(v), i * 250 + dur + 100);   // lands even if frames are throttled
      });
    },
    change(el) {
      const lines = [...el.querySelectorAll(".ln.add, .ln.del")];
      el.querySelectorAll(".code-panel pre").forEach((pre) => {
        const first = pre.querySelector(".ln.add, .ln.del");
        if (first) pre.scrollTop = Math.max(0, first.offsetTop - pre.clientHeight / 3);
      });
      if (reduced()) return;
      lines.forEach((l) => l.classList.add("pending"));
      lines.forEach((l, i) => setTimeout(() => l.classList.remove("pending"), 500 + i * 140));
    },
    reach(el, inc) {
      const box = el.querySelector(".map-box");
      if (!box.firstChild) map = MagellanMap.render(box, inc.story.map, { legend: true });
      map && map.play();
    },
    issue(el) {
      const hit = el.querySelector(".ln.hit");
      el.querySelector(".issue-card")?.classList.remove("in");
      if (hit) hit.classList.remove("flash");
      setTimeout(() => {
        hit?.classList.add("flash");
        el.querySelector(".issue-card")?.classList.add("in");
      }, reduced() ? 0 : 500);
    },
    verdict(el) {
      const s = el.querySelector(".stamp");
      if (!s) return;
      s.classList.remove("in");
      setTimeout(() => s.classList.add("in"), reduced() ? 0 : 250);
    },
  };

  // -- playing ---------------------------------------------------------------------------
  let cur = 0, scene = 0, playing = !reduced(), visible = false, elapsed = 0, last = 0;
  let scenes = [];

  // why: "visitor" (they picked it), "auto" (the player moved on), "start"
  function showIncident(i, why = "auto") {
    cur = (i + incidents.length) % incidents.length;
    const inc = incidents[cur];
    if (why === "visitor" || (why === "auto" && LINK.test(location.hash))) remember();
    tabs.forEach((t, j) => {
      t.classList.toggle("active", j === cur);
      t.setAttribute("aria-selected", String(j === cur));
    });
    // on a narrow screen the tabs scroll sideways: keep the current one in sight
    const strip = tabs[cur].parentElement, a = tabs[cur].getBoundingClientRect(), s = strip.getBoundingClientRect();
    if (a.left < s.left || a.right > s.right) strip.scrollLeft += a.left - s.left - 8;
    map = null;
    stage.innerHTML = SCENES.map((s) => `<section class="scene scene-${s.id}" data-scene="${s.id}"
      aria-label="${esc(s.title)}">${BUILD[s.id](inc)}</section>`).join("");
    scenes = [...stage.querySelectorAll(".scene")];
    showScene(0);
  }

  function showScene(k) {
    scene = Math.max(0, Math.min(SCENES.length - 1, k));
    elapsed = 0;
    scenes.forEach((el, j) => el.classList.toggle("active", j === scene));
    stepBtns.forEach((b, j) => {
      b.classList.toggle("done", j < scene);
      b.classList.toggle("active", j === scene);
      b.querySelector(".fill").style.width = j < scene ? "100%" : "0%";
    });
    ENTER[SCENES[scene].id](scenes[scene], incidents[cur]);
  }

  const duration = () => (SCENES[scene].id === "reach" && map
    ? Math.max(SCENES[scene].ms, map.duration() + 3500) : SCENES[scene].ms);

  function advance(dir, why = "auto") {
    if (scene + dir >= SCENES.length) return showIncident(cur + 1, why);
    if (scene + dir < 0) { showIncident(cur - 1, why); return showScene(SCENES.length - 1); }
    showScene(scene + dir);
  }

  function setPlaying(on) {
    playing = on;
    playBtn.classList.toggle("paused", !on);
    playBtn.setAttribute("aria-label", on ? "Pause" : "Play");
  }

  // the clock: a plain timer, so it keeps time wherever animation frames are throttled
  function tick() {
    const t = performance.now();
    const dt = last ? t - last : 0;
    last = t;
    if (playing && visible && !document.hidden) {
      elapsed += dt;
      const k = Math.min(1, elapsed / duration());
      stepBtns[scene].querySelector(".fill").style.width = `${k * 100}%`;
      if (k >= 1) advance(1);
    }
  }

  root.addEventListener("click", (e) => {
    const tab = e.target.closest(".story-tab");
    const step = e.target.closest("[data-scene]");
    const act = e.target.closest("[data-act]");
    if (tab) showIncident(Number(tab.dataset.i), "visitor");
    else if (step && step.tagName === "BUTTON") showScene(Number(step.dataset.scene));
    else if (act) {
      const a = act.dataset.act;
      if (a === "play") setPlaying(!playing);
      else advance(a === "next" ? 1 : -1, "visitor");
    }
  });
  root.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      if (e.target.closest(".story-tabs")) return;
      advance(e.key === "ArrowRight" ? 1 : -1, "visitor");
    }
  });
  new IntersectionObserver(([en]) => { visible = en.isIntersecting; },
    { threshold: 0.35 }).observe(root);

  // -- the address ---------------------------------------------------------------------------
  // index.html#incident=zune-2008 opens the player on that story, so a presenter can jump
  // straight to it; picking another incident puts that one in the address, ready to share.
  // The page's own anchors (#try, #checklist) are left alone: the player rewrites the address
  // only when the visitor picks an incident, or when the address already names one.
  function linked() {
    const m = location.hash.match(LINK);
    return m ? incidents.findIndex((inc) => inc.name === m[1]) : -1;
  }
  function remember() {
    const want = `#incident=${incidents[cur].name}`;
    if (location.hash !== want) history.replaceState(history.state, "", want);
  }
  function openLinked(smooth) {
    const i = linked();
    if (i < 0) return false;
    showIncident(i, "start");
    bringIntoView(root, smooth && !reduced());
    return true;
  }
  // scroll to just below the sticky nav (two rows tall on a phone); offsetTop, not the
  // bounding box: the section may still be sliding in (.reveal)
  function bringIntoView(el, smooth) {
    let y = 0;
    for (let n = el; n; n = n.offsetParent) y += n.offsetTop;
    const nav = document.querySelector(".nav");
    const top = Math.max(0, y - (nav ? nav.offsetHeight : 0) - 12);
    try { scrollTo({ top, behavior: smooth ? "smooth" : "instant" }); } catch { scrollTo(0, top); }
  }
  window.addEventListener("hashchange", () => openLinked(true));   // a link on this page

  setPlaying(playing);
  if (!openLinked(false)) showIncident(0, "start");
  setInterval(tick, 100);
})();
