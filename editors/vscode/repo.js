"use strict";
// Which GitHub repository the open folder belongs to, with nothing to set up, so the sidebar can
// show it and open the website's Team suite already connected to it (suite.html reads
// ?repo=owner/name). It asks VS Code's own Git extension first, and runs `git remote -v` when that
// has nothing to say yet: the Git extension opens repositories a moment after VS Code starts.
// No VS Code here, so plain Node tests it: node --test "editors/vscode/test/*.test.js"

const path = require("path");
const { execFile } = require("child_process");

const SITE = "https://magellan-code.pages.dev";

// What GitHub allows: owners up to 39 letters, digits and hyphens, not starting with a hyphen;
// names up to 100 letters, digits, dots, underscores and hyphens, but not "." or "..".
const OWNER = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/;
const NAME = /^[A-Za-z0-9._-]{1,100}$/;

// github.com itself; ssh.github.com is GitHub's SSH over port 443.
const HOST = String.raw`(?:www\.|ssh\.)?github\.com`;
// scheme://[user[:password]@]github.com[:port]/owner/name, for https, http, ssh and git://
const WITH_SCHEME = new RegExp(String.raw`^(?:https?|ssh|git)://(?:[^/]*@)?${HOST}(?::\d*)?/(.*)$`, "i");
// [user@]github.com:owner/name, the form `git clone git@github.com:...` writes
const SHORT = new RegExp(String.raw`^(?:[^@/:\s]+@)?${HOST}:(.*)$`, "i");

/** A git remote URL's "owner/name" if it is on github.com, else null. Never keeps a password. */
function githubRepo(url) {
  if (typeof url !== "string") return null;
  const s = url.trim();
  const m = s.match(WITH_SCHEME) || s.match(SHORT);
  if (!m) return null;
  const parts = m[1].replace(/^\/+/, "").replace(/\/+$/, "").replace(/\.git$/, "").split("/");
  if (parts.length !== 2) return null;
  const [owner, name] = parts;
  if (!OWNER.test(owner) || !NAME.test(name) || name === "." || name === "..") return null;
  return `${owner}/${name}`;
}

/** The website's Team suite, connected to a repository. */
function suiteUrl(slug, site = SITE) {
  return `${String(site).replace(/\/+$/, "")}/suite.html?repo=${encodeURIComponent(slug)}`;
}

/** `git remote -v`'s lines (`origin<TAB>url (fetch)`) as `[{ name, fetchUrl, pushUrl }]`. */
function parseRemotes(text) {
  const byName = new Map();
  for (const line of String(text || "").split(/\r?\n/)) {
    // a partial clone adds its filter after "(fetch)", like "[blob:none]": ignore it
    const m = line.trim().match(/^(\S+)\s+(\S+)(?:\s+\((fetch|push)\))?/);
    if (!m) continue;
    const [, name, url, kind] = m;
    const r = byName.get(name) || { name };
    if (kind === "push") r.pushUrl = r.pushUrl || url;
    else r.fetchUrl = r.fetchUrl || url;
    byName.set(name, r);
  }
  return [...byName.values()];
}

/** The remote to use, as `{ slug, remote }`: origin if it is on GitHub, else the first that is. */
function pickRemote(remotes) {
  const onGithub = [];
  for (const r of remotes || []) {
    const slug = r && (githubRepo(r.fetchUrl) || githubRepo(r.pushUrl));
    if (slug) onGithub.push({ slug, remote: r.name });
  }
  return onGithub.find((r) => r.remote === "origin") || onGithub[0] || null;
}

/** A path ready to compare: forward slashes, no trailing one, lower case where case doesn't count. */
function comparable(p, insensitive) {
  const s = path.posix.normalize(String(p).replace(/\\/g, "/")).replace(/\/+$/, "");
  return insensitive ? s.toLowerCase() : s;
}

/** The repository whose root holds the folder; the deepest, when one repository is inside another. */
function repositoryFor(repositories, folderPath, platform) {
  const insensitive = platform === "win32";
  const folder = comparable(folderPath, insensitive);
  let best = null;
  let bestLength = -1;
  for (const r of repositories || []) {
    const root = r && r.rootUri && r.rootUri.fsPath;
    if (!root) continue;
    const at = comparable(root, insensitive);
    const holds = folder === at || folder.startsWith(at + "/");
    if (holds && at.length > bestLength) {
      best = r;
      bestLength = at.length;
    }
  }
  return best;
}

/** What VS Code's Git extension knows: `{ slug, remote }` or null. Throws if its API does. */
async function fromGitExtension(vscode, folderPath, platform) {
  const ext = vscode && vscode.extensions && vscode.extensions.getExtension("vscode.git");
  if (!ext) return null;
  const git = ext.isActive ? ext.exports : await ext.activate();
  const api = git && typeof git.getAPI === "function" ? git.getAPI(1) : null;
  const repo = repositoryFor(api && api.repositories, folderPath, platform);
  return repo ? pickRemote(repo.state && repo.state.remotes) : null;
}

/** What `git remote -v` says in the folder: `{ slug, remote }` or null. */
function fromGitCommand(run, folderPath) {
  return new Promise((resolve) => {
    run("git", ["remote", "-v"], { cwd: folderPath, timeout: 10000, windowsHide: true },
      (err, stdout) => resolve(err ? null : pickRemote(parseRemotes(stdout))));
  });
}

/**
 * The GitHub repository the folder belongs to: `{ slug, remote, url }`, or null. Never throws.
 * `run(exe, args, opts, cb)` is child_process.execFile; `platform` is for tests.
 */
async function detect({ vscode, folderPath, run = execFile, platform = process.platform } = {}) {
  if (!folderPath) return null;
  let found = null;
  try {
    found = await fromGitExtension(vscode, folderPath, platform);
  } catch {
    found = null;                         // Git turned off in VS Code, or its API changed: ask git
  }
  if (!found) {
    try {
      found = await fromGitCommand(run, folderPath);
    } catch {
      found = null;
    }
  }
  return found ? { slug: found.slug, remote: found.remote, url: `https://github.com/${found.slug}` } : null;
}

/** What the sidebar shows for what detect() found: `{ text, tip }`. */
function describe(found) {
  if (!found) {
    return { text: "No GitHub repository",
             tip: "This folder has no git remote on GitHub, so the Team suite can't connect to it on its own." };
  }
  return { text: found.slug, tip: `${found.url}, from the "${found.remote}" remote` };
}

module.exports = { SITE, githubRepo, suiteUrl, parseRemotes, detect, describe };
