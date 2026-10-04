// The hero: a terminal that types out a real `magellan-lite check` (data/showcase.js), over a
// quiet background of a code map where changes ripple out from one definition to its callers.
// Also fades sections in as they scroll into view. All of it stands still for visitors who
// ask their system for reduced motion.

(() => {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // -- the terminal ----------------------------------------------------------------------
  const term = document.getElementById("hero-terminal");
  const hero = window.MAGELLAN_SHOWCASE && window.MAGELLAN_SHOWCASE.hero;

  const tone = (line) => {
    if (/^magellan-lite: BLOCK/.test(line)) return "t-block";
    if (/^magellan-lite: REVIEW/.test(line)) return "t-review";
    if (/^magellan-lite: OK/.test(line)) return "t-ok";
    if (/^ {2}\S/.test(line)) return "t-head";
    if (/^\s+\[ \] (CRITICAL|HIGH)/.test(line)) return "t-block";
    if (/^\s+\[ \] MEDIUM/.test(line)) return "t-review";
    if (/^\s+fix:/.test(line)) return "t-ok";
    if (/^\s+because /.test(line)) return "t-dim";
    if (/^\s+0\.\d\d /.test(line)) return "t-score";
    if (/^\s+del /.test(line)) return "t-del";
    if (/^\s+sig /.test(line)) return "t-review";
    if (/^\s+new /.test(line)) return "t-ok";
    return "";
  };
  const lineHtml = (l) => `<span class="${tone(l)}">${esc(l) || " "}</span>\n`;

  let typing = 0;
  async function play() {
    if (!term || !hero) return;
    const run = ++typing;
    const lines = hero.output.split("\n");
    const prompt = '<span class="t-prompt">~/sensor-agent $</span> ';
    if (reduced) {
      term.innerHTML = `${prompt}${esc(hero.command)}\n${lines.map(lineHtml).join("")}`;
      return;
    }
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    term.innerHTML = prompt;
    const cmd = document.createElement("span");
    term.append(cmd);
    for (const ch of hero.command) {
      if (run !== typing) return;
      cmd.textContent += ch;
      await wait(55 + Math.random() * 50);
    }
    await wait(450);
    term.insertAdjacentHTML("beforeend", "\n");
    for (const l of lines) {
      if (run !== typing) return;
      term.insertAdjacentHTML("beforeend", lineHtml(l));
      term.scrollTop = term.scrollHeight;
      await wait(l.trim() ? 45 : 140);
    }
    term.insertAdjacentHTML("beforeend", `${prompt}<span class="t-cursor"></span>`);
    term.scrollTop = term.scrollHeight;
  }
  if (term && hero) {
    play();
    const again = document.getElementById("hero-replay");
    if (again) again.addEventListener("click", play);
  }

  // -- the background: a code map, changes rippling to their callers ---------------------
  const canvas = document.getElementById("hero-canvas");
  if (canvas && canvas.getContext) {
    const ctx = canvas.getContext("2d");
    let seed = 7;
    const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    const N = 54;
    const nodes = Array.from({ length: N }, () => ({ x: rand(), y: rand(), p: rand() * 6.28, heat: 0 }));
    const edges = [];
    nodes.forEach((a, i) => {
      const near = nodes.map((b, j) => [Math.hypot(a.x - b.x, (a.y - b.y) * 0.6), j])
        .filter(([, j]) => j !== i).sort((x, y) => x[0] - y[0]).slice(0, 2);
      near.forEach(([, j]) => { if (!edges.some(([p, q]) => (p === j && q === i))) edges.push([i, j]); });
    });
    const nbrs = nodes.map((_, i) => edges.filter(([a, b]) => a === i || b === i).map(([a, b]) => (a === i ? b : a)));
    let colors = {};
    const readColors = () => {
      const cs = getComputedStyle(document.documentElement);
      colors = { line: cs.getPropertyValue("--map-line").trim() || "rgba(120,140,160,.25)",
        dot: cs.getPropertyValue("--muted").trim() || "#889", hot: cs.getPropertyValue("--accent").trim() || "#f2a93b" };
    };
    readColors();
    matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", readColors);

    let W = 0, H = 0;
    const size = () => {
      const r = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = r.width; H = r.height;
      canvas.width = W * dpr; canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    size();
    addEventListener("resize", size);

    const waves = [];
    const ripple = () => {
      const start = Math.floor(rand() * N);
      const seen = new Map([[start, 0]]);
      let frontier = [start];
      for (let hop = 1; hop <= 4; hop++) {
        const next = [];
        for (const i of frontier) for (const j of nbrs[i]) if (!seen.has(j)) { seen.set(j, hop); next.push(j); }
        frontier = next;
      }
      waves.push({ t0: performance.now(), seen });
    };

    let on = true, lastRipple = 0;
    const draw = (t) => {
      ctx.clearRect(0, 0, W, H);
      for (const n of nodes) n.heat = 0;
      for (const w of waves) {
        const age = (t - w.t0) / 1000;
        for (const [i, hop] of w.seen) {
          const k = age - hop * 0.55;
          if (k > 0 && k < 2.2) nodes[i].heat = Math.max(nodes[i].heat, Math.pow(0.85, hop) * (k < 0.25 ? k / 0.25 : 1 - (k - 0.25) / 1.95));
        }
      }
      while (waves.length && t - waves[0].t0 > 5000) waves.shift();
      const xy = (n) => [n.x * W + Math.sin(t / 4000 + n.p) * 6, n.y * H + Math.cos(t / 5000 + n.p) * 5];
      ctx.lineWidth = 1;
      for (const [a, b] of edges) {
        const [x1, y1] = xy(nodes[a]), [x2, y2] = xy(nodes[b]);
        const h = Math.min(nodes[a].heat, nodes[b].heat);
        ctx.strokeStyle = h > 0.05 ? colors.hot : colors.line;
        ctx.globalAlpha = h > 0.05 ? 0.25 + h * 0.6 : 1;
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      }
      for (const n of nodes) {
        const [x, y] = xy(n);
        ctx.globalAlpha = 0.35 + n.heat * 0.65;
        ctx.fillStyle = n.heat > 0.05 ? colors.hot : colors.dot;
        ctx.beginPath(); ctx.arc(x, y, 2.2 + n.heat * 3.5, 0, 6.283); ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (!reduced && t - lastRipple > 2600) { ripple(); lastRipple = t; }
      if (on && !reduced) requestAnimationFrame(draw);
    };
    if (reduced) requestAnimationFrame(draw);
    else {
      new IntersectionObserver(([en]) => {
        const was = on;
        on = en.isIntersecting;
        if (on && !was) requestAnimationFrame(draw);
      }).observe(canvas);
      requestAnimationFrame(draw);
    }
  }

  // -- sections fade in as they arrive ----------------------------------------------------
  const io = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
  }), { threshold: 0.12 });
  document.querySelectorAll(".reveal").forEach((el) => (reduced ? el.classList.add("in") : io.observe(el)));
})();
