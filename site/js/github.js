// Reads a team's shared work in progress from GitHub, in the browser. Each person's
// `magellan-lite share` (or the VS Code extension's "share on save") pushes their working tree
// to refs/wip/<name>; this reads those refs, what each one changed against the default branch,
// and the files, so the engine (in a worker, js/teamfeed.js) can check them alone, two by two,
// and all together.
//
// What it asks GitHub for, per refresh: the list of refs/wip/* (one request). Only when
// something moved: the default branch's head, its file tree, and a comparison per person whose
// work changed. File contents come from raw.githubusercontent.com (no API quota) and are kept
// by their blob id, so a file is downloaded once. Without a token GitHub allows 60 API requests
// an hour from one address; with one, 5000.
//
// The token: kept in this tab's sessionStorage only (gone when the tab closes, or at "Forget"),
// and sent only to api.github.com. Never to raw.githubusercontent.com, never in a URL.

window.MagellanGitHub = (() => {
  const API = "https://api.github.com";
  const RAW = "https://raw.githubusercontent.com";
  // how much the browser takes on: past these, `magellan-lite team` on the command line is the tool
  const LIMITS = { members: 8, files: 400, bytes: 6_000_000, file: 500_000 };
  const SKIP = new Set(["__pycache__", "venv", "env", "build", "dist", "node_modules", "site-packages"]);
  // the other languages' files, by suffix: magellan_lite/languages.py LANGUAGE_OF (C, C++, Java,
  // Fortran, COBOL, TypeScript/JavaScript; tests/test_site.py keeps the two lists the same)
  const SUFFIXES = new Set([".java", ".f", ".for", ".f77", ".ftn", ".fpp", ".f90", ".f95",
    ".f03", ".f08", ".f18", ".f23", ".F", ".FOR", ".F77", ".FTN", ".FPP", ".F90", ".F95", ".F03",
    ".F08", ".F18", ".F23", ".inc", ".INC", ".fi", ".fh", ".cpp", ".cc", ".cxx", ".c++", ".hpp",
    ".hh", ".hxx", ".h++", ".ipp", ".tpp", ".c", ".h", ".cbl", ".cob", ".cpy", ".cobol", ".dcl",
    ".CBL", ".COB", ".CPY", ".COBOL", ".DCL", ".Cbl", ".Cpy", ".ts", ".tsx", ".mts", ".cts", ".js",
    ".jsx", ".mjs", ".cjs"]);
  const REFRESH_BASE_MS = 5 * 60_000;   // look at the default branch again at least this often

  // "owner/repo", "https://github.com/owner/repo", "...repo.git" -> { owner, repo } or null.
  // A folder after the name ("owner/repo/demo/live", or a GitHub folder link ".../tree/main/
  // demo/live", whose branch is ignored: the suite reads the default branch) adds { path }: only
  // that folder is read, as if it were the whole project (a demo inside a big repository).
  const SEGMENT = /^[A-Za-z0-9._-]{1,100}$/;
  function parseRepo(text) {
    const s = String(text || "").trim().replace(/\/+$/, "").replace(/^https?:\/\/(?:www\.)?github\.com\//, "");
    let [owner, repo, ...rest] = s.split("/");
    repo = String(repo || "").replace(/\.git$/, "");
    if (rest[0] === "tree" && rest.length > 2) rest = rest.slice(2);
    if (!/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(owner || "") || !SEGMENT.test(repo)) return null;
    if ([repo, ...rest].some((p) => !SEGMENT.test(p) || p === "." || p === "..")) return null;
    return rest.length ? { owner, repo, path: rest.join("/") } : { owner, repo };
  }

  // a personal access token's shape; anything else is refused before it goes anywhere
  const tokenOk = (t) => /^[A-Za-z0-9_]{20,255}$/.test(t);
  // a name under refs/wip/: what `magellan-lite share` writes (lower case, digits, dashes)
  const nameOk = (n) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/.test(n);
  // the same files the engine reads from a disk (magellan_lite/source.py): Python and the other
  // languages' sources, outside tool, environment and hidden folders
  const python = (path) => path.endsWith(".py");
  const projectFile = (path) => (python(path) || SUFFIXES.has(path.slice(path.lastIndexOf(".")))) &&
    !path.split("/").slice(0, -1).some((d) => SKIP.has(d) || d.startsWith("."));
  const encPath = (p) => p.split("/").map(encodeURIComponent).join("/");

  class GitHubError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }
  class TooBig extends GitHubError {}
  // another language's file too big for the browser (a generated bundle, a data table): left
  // out of the check and listed, where a Python file that size stops it, as before
  const leaveOut = (path, size) => !python(path) && size > LIMITS.file;

  // run fn over items, `width` at a time
  async function pool(items, width, fn) {
    const out = new Array(items.length);
    let next = 0;
    const lane = async () => {
      while (next < items.length) { const i = next++; out[i] = await fn(items[i], i); }
    };
    await Promise.all(Array.from({ length: Math.min(width, items.length) }, lane));
    return out;
  }

  class Repo {
    constructor({ owner, repo, path = "" }, token = "") {
      if (token && !tokenOk(token)) throw new GitHubError("That doesn't look like a GitHub token.");
      this.owner = owner;
      this.repo = repo;
      this.root = path;               // a folder to read alone ("demo/live"), or "" for everything
      this.token = token;
      this.rate = null;               // { remaining, limit, reset (ms) } from GitHub's answers
      this.info = null;               // the repository: default branch, private or not
      this.blobs = new Map();         // blob sha -> text
      this.base = null;               // { sha, files, at }
      this.works = new Map();         // name -> { sha, base, changes, when }
    }

    get full() { return `${this.owner}/${this.repo}`; }
    // what was asked for: the repository, or its folder ("owner/repo/demo/live"), and its page
    get where() { return this.root ? `${this.full}/${this.root}` : this.full; }
    get page() { return `https://github.com/${this.full}` + (this.root ? `/tree/HEAD/${this.root}` : ""); }

    // the repository's path to the project's ("demo/live/fleet/cargo.py" -> "fleet/cargo.py"),
    // or null for a file outside the folder
    local(path) {
      if (!this.root) return path;
      return path.startsWith(this.root + "/") ? path.slice(this.root.length + 1) : null;
    }
    mine(path) { const p = this.local(path); return p !== null && projectFile(p); }

    async api(path, cache = "default") {
      const headers = { Accept: "application/vnd.github+json" };
      if (this.token) headers.Authorization = `Bearer ${this.token}`;
      let r;
      try {
        r = await fetch(`${API}/repos/${encodeURIComponent(this.owner)}/${encodeURIComponent(this.repo)}${path}`,
          { headers, cache, credentials: "omit", referrerPolicy: "no-referrer" });
      } catch {
        throw new GitHubError("Couldn't reach GitHub. Check the connection and try again.");
      }
      const left = r.headers.get("x-ratelimit-remaining");
      if (left !== null) {
        this.rate = { remaining: Number(left), limit: Number(r.headers.get("x-ratelimit-limit")),
          reset: Number(r.headers.get("x-ratelimit-reset")) * 1000 };
      }
      if (r.ok) return r.json();
      if ((r.status === 403 || r.status === 429) && this.rate && this.rate.remaining === 0) {
        const at = new Date(this.rate.reset).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        throw new GitHubError(this.token
          ? `GitHub's limit for this token is used up until ${at}.`
          : `GitHub allows 60 requests an hour without a token, and they're used up until ${at}. Add a token to keep going.`, r.status);
      }
      if (r.status === 401) throw new GitHubError("GitHub refused the token. It may have expired: make a new one, or forget it.", 401);
      if (r.status === 404) {
        throw new GitHubError(this.token
          ? `No repository ${this.full} that this token can read.`
          : `No public repository ${this.full}. If it's private, add a token that can read it.`, 404);
      }
      throw new GitHubError(`GitHub answered ${r.status}.`, r.status);
    }

    async connect() {
      const info = await this.api("");
      this.info = { branch: info.default_branch, private: Boolean(info.private), name: info.full_name };
      return this.info;
    }

    // one file's text, by blob id: from raw.githubusercontent.com when the repository is public
    // (no quota, no token), through the API with the token when it's private
    async blob(path, sha, commit) {
      if (this.blobs.has(sha)) return this.blobs.get(sha);
      let text;
      if (this.info.private) {
        const b = await this.api(`/git/blobs/${sha}`);
        if (b.size > LIMITS.file) throw new TooBig(`${path} is too big for the browser.`);
        const bytes = Uint8Array.from(atob(b.content.replace(/\s/g, "")), (c) => c.charCodeAt(0));
        text = new TextDecoder("utf-8").decode(bytes);
      } else {
        let r;
        try {
          r = await fetch(`${RAW}/${encodeURIComponent(this.owner)}/${encodeURIComponent(this.repo)}/${commit}/${encPath(path)}`,
            { credentials: "omit", referrerPolicy: "no-referrer" });
        } catch {
          throw new GitHubError(`Couldn't download ${path}.`);
        }
        if (!r.ok) throw new GitHubError(`Couldn't download ${path} (${r.status}).`, r.status);
        text = await r.text();
        if (text.length > LIMITS.file) throw new TooBig(`${path} is too big for the browser.`);
      }
      this.blobs.set(sha, text);
      return text;
    }

    async loadBase(sha) {
      const tree = await this.api(`/git/trees/${sha}?recursive=1`);       // a sha: never changes
      if (tree.truncated) throw new GitHubError("This repository is too big to read in the browser: use `magellan-lite team`.");
      const all = tree.tree.filter((e) => e.type === "blob" && this.mine(e.path));
      const entries = all.filter((e) => !leaveOut(e.path, e.size || 0));
      const skipped = all.filter((e) => leaveOut(e.path, e.size || 0)).map((e) => this.local(e.path));
      if (this.root && !all.length) throw new GitHubError(`No source files in ${this.where}: check the folder's name.`);
      const bytes = entries.reduce((n, e) => n + (e.size || 0), 0);
      if (entries.length > LIMITS.files || bytes > LIMITS.bytes) {
        throw new GitHubError(`${entries.length} source files (${Math.round(bytes / 1e6)} MB) is more than the browser should take on: use \`magellan-lite team\`.`);
      }
      const files = {};
      await pool(entries, 8, async (e) => { files[this.local(e.path)] = await this.blob(e.path, e.sha, sha); });
      this.base = { sha, files, skipped, at: Date.now() };
    }

    // what one person's shared work changed against the default branch, as { path: text | null }
    async loadWork(name, sha) {
      const cmp = await this.api(`/compare/${this.base.sha}...${sha}`);   // both shas: never changes
      const files = (cmp.files || []).filter((f) => this.mine(f.filename) ||
        (f.previous_filename && this.mine(f.previous_filename)));
      const changes = {}, skipped = [];
      await pool(files, 6, async (f) => {
        if (f.previous_filename && this.mine(f.previous_filename)) changes[this.local(f.previous_filename)] = null;
        if (!this.mine(f.filename)) return;
        try {
          changes[this.local(f.filename)] = f.status === "removed" ? null : await this.blob(f.filename, f.sha, sha);
        } catch (err) {
          if (!(err instanceof TooBig) || python(f.filename)) throw err;
          skipped.push(this.local(f.filename));
        }
      });
      const last = (cmp.commits || []).at(-1);
      const when = last ? Date.parse(last.commit.committer.date) : 0;
      return { sha, base: this.base.sha, changes, skipped, when, many: (cmp.files || []).length >= 300 };
    }

    // Look again. Returns null when nothing moved since the last look, else
    // { base: {path: text}, works: {name: {path: text}}, people: [{name, when, files, many}],
    //   skipped: [paths too big for the browser, left out] }
    async refresh(force = false) {
      if (!this.info) await this.connect();
      const refs = await this.api("/git/matching-refs/wip", "no-cache");
      const wip = refs
        .filter((r) => r.ref.startsWith("refs/wip/") && r.object && r.object.type === "commit")
        .map((r) => ({ name: r.ref.slice("refs/wip/".length), sha: r.object.sha }))
        .filter((w) => nameOk(w.name))
        .sort((a, b) => a.name.localeCompare(b.name))
        .slice(0, LIMITS.members);
      const stale = !this.base || Date.now() - this.base.at > REFRESH_BASE_MS;
      const moved = wip.length !== this.works.size ||
        wip.some((w) => !this.works.has(w.name) || this.works.get(w.name).sha !== w.sha);
      if (!force && !stale && !moved) return null;

      const head = await this.api(`/git/ref/heads/${encPath(this.info.branch)}`, "no-cache");
      if (!this.base || this.base.sha !== head.object.sha) await this.loadBase(head.object.sha);
      else this.base.at = Date.now();

      const works = new Map();
      for (const w of wip) {
        const old = this.works.get(w.name);
        works.set(w.name, old && old.sha === w.sha && old.base === this.base.sha ? old : await this.loadWork(w.name, w.sha));
      }
      this.works = works;

      const out = { base: this.base.files, works: {}, people: [],
        skipped: [...new Set([...this.base.skipped, ...[...works.values()].flatMap((w) => w.skipped)])].sort() };
      for (const [name, w] of works) {
        const files = { ...this.base.files };
        for (const [path, text] of Object.entries(w.changes)) {
          if (text === null) delete files[path];
          else files[path] = text;
        }
        out.works[name] = files;
        out.people.push({ name, when: w.when, files: Object.keys(w.changes).sort(), many: w.many });
      }
      return out;
    }
  }

  return { parseRepo, tokenOk, projectFile, Repo, GitHubError, LIMITS, SUFFIXES };
})();
