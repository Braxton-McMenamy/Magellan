// A team's shared work in progress, kept up to date: reads GitHub (js/github.js) every so
// often and, when something moved, has the engine (js/engine-worker.js) check everyone's work
// alone, two by two and all together (magellan_lite/web.py: team_live). The Team suite and the
// Scene both use it, so they show the same thing.
//
//   const feed = MagellanTeam.connect({ repo: "owner/name", token, onStatus, onResult, onError });
//   feed.now(); feed.stop();
//
// The connection (the repository, and the token if there is one) is kept in this tab's
// sessionStorage, so moving between the suite and the scene keeps it. MagellanTeam.saved()
// reads it back; MagellanTeam.forget() clears it.

window.MagellanTeam = (() => {
  const KEY_REPO = "magellan-team-repo";
  const KEY_TOKEN = "magellan-team-token";
  const EVERY = { token: 15_000, none: 90_000 };     // GitHub's hourly limits: 5000 vs 60

  const store = {
    get(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
    set(k, v) { try { if (v) sessionStorage.setItem(k, v); else sessionStorage.removeItem(k); } catch { /* private mode */ } },
  };
  const saved = () => ({ repo: store.get(KEY_REPO), token: store.get(KEY_TOKEN) });
  function forget() { store.set(KEY_TOKEN, ""); store.set(KEY_REPO, ""); }

  // the engine, one worker for the page
  let worker = null, seq = 0;
  const waiting = new Map();
  let onWorkerStatus = () => {};
  function engine() {
    if (worker) return worker;
    worker = new Worker("js/engine-worker.js");
    worker.onmessage = (ev) => {
      const m = ev.data || {};
      if (m.status) return onWorkerStatus(m.status);
      const w = waiting.get(m.id);
      if (!w) return;
      waiting.delete(m.id);
      if (m.ok) w.resolve({ ...m.result, ms: m.ms });
      else w.reject(new Error(m.error));
    };
    worker.onerror = (ev) => {
      ev.preventDefault();
      for (const w of waiting.values()) w.reject(new Error("The engine stopped. Reload the page to try again."));
      waiting.clear();
      worker.terminate();
      worker = null;
    };
    return worker;
  }
  function check(base, works) {
    const id = ++seq;
    return new Promise((resolve, reject) => {
      waiting.set(id, { resolve, reject });
      engine().postMessage({ id, op: "team", base: JSON.stringify(base), works: JSON.stringify(works) });
    });
  }

  function connect({ repo, token = "", onStatus = () => {}, onResult = () => {}, onError = () => {} }) {
    const where = MagellanGitHub.parseRepo(repo);
    if (!where) throw new MagellanGitHub.GitHubError("Write the repository as owner/name, or paste its GitHub address.");
    const gh = new MagellanGitHub.Repo(where, token);       // checks the token's shape
    store.set(KEY_REPO, gh.full);
    store.set(KEY_TOKEN, token);
    onWorkerStatus = onStatus;

    let timer = null, busy = false, stopped = false, last = null, nextAt = 0;
    const every = token ? EVERY.token : EVERY.none;

    async function tick(force = false) {
      if (stopped || busy) return;
      clearTimeout(timer);
      busy = true;
      try {
        onStatus(last ? "Looking for new work…" : `Reading ${gh.full}…`);
        const got = await gh.refresh(force);
        onStatus("");
        if (got) {
          if (!Object.keys(got.works).length) {
            last = { repo: gh.full, info: gh.info, people: [], members: [], pairs: [], team: null, at: Date.now() };
          } else {
            const result = await check(got.base, got.works);
            last = { repo: gh.full, info: gh.info, people: got.people, ...result, at: Date.now() };
          }
          onResult(last, gh.rate);
        } else {
          onStatus(`Nothing new since ${new Date(last ? last.at : Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
        }
      } catch (err) {
        onError(err, gh.rate);
      } finally {
        busy = false;
        if (!stopped) {
          nextAt = Date.now() + every;
          timer = setTimeout(() => (document.hidden ? null : tick()), every);
        }
      }
    }

    // a hidden tab doesn't spend the hourly allowance; coming back looks at once
    const onVisible = () => { if (!document.hidden && Date.now() >= nextAt) tick(); };
    document.addEventListener("visibilitychange", onVisible);
    tick(true);

    return {
      repo: gh.full,
      now: () => tick(),
      next: () => Math.max(0, nextAt - Date.now()),
      rate: () => gh.rate,
      last: () => last,
      stop() {
        stopped = true;
        clearTimeout(timer);
        document.removeEventListener("visibilitychange", onVisible);
      },
    };
  }

  // "3 min ago"
  function ago(t, now = Date.now()) {
    if (!t) return "";
    const s = Math.max(0, Math.round((now - t) / 1000));
    return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago`
      : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`;
  }

  return { connect, saved, forget, ago };
})();
