"use strict";
// Which GitHub repository the open folder belongs to, with nothing to set up, so the sidebar can
// show it and open the website's Team suite already connected to it (suite.html reads
// ?repo=owner/name, or owner/name/folder when the open folder is one inside the repository, so
// the suite shows that folder alone: a demo in a big repository's subfolder). It asks VS Code's own Git extension first, and runs `git remote -v` when that
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

/** The website's Team suite, connected to a repository, or to one folder of it. */
function suiteUrl(slug, site = SITE, folder = "") {
  const where = folder ? `${slug}/${folder}` : slug;
  return `${String(site).replace(/\/+$/, "")}/suite.html?repo=${encodeURIComponent(where)}`;
}

/** A folder inside a repository as the website takes it ("demo/live"), or "" if it isn't one. */
function cleanFolder(text) {
  const parts = String(text || "").trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").split("/");
  return parts.every((p) => NAME.test(p) && p !== "." && p !== "..") ? parts.join("/") : "";
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

/** Where the folder is inside a repository's root: "demo/live", or "" at the root. */
function inside(root, folderPath, platform) {
  const insensitive = platform === "win32";
  const at = comparable(root, insensitive);
  const folder = comparable(folderPath, insensitive);
  if (folder === at || !folder.startsWith(at + "/")) return "";
  return cleanFolder(comparable(folderPath, false).slice(at.length + 1));   // the folder's own spelling
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
  const picked = repo ? pickRemote(repo.state && repo.state.remotes) : null;
  return picked && { ...picked, folder: inside(repo.rootUri.fsPath, folderPath, platform) };
}

/** What `git remote -v` (and `git rev-parse --show-prefix`, for the folder) say: `{ slug, remote,
 * folder }` or null. */
function fromGitCommand(run, folderPath) {
  const opts = { cwd: folderPath, timeout: 10000, windowsHide: true };
  return new Promise((resolve) => {
    run("git", ["remote", "-v"], opts, (err, stdout) => {
      const picked = err ? null : pickRemote(parseRemotes(stdout));
      if (!picked) return resolve(null);
      run("git", ["rev-parse", "--show-prefix"], opts, (err2, prefix) => {
        const one = !err2 && /^[^\r\n]*\r?\n?$/.test(String(prefix || "")) ? String(prefix || "") : "";
        resolve({ ...picked, folder: cleanFolder(one) });
      });
    });
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
  if (!found) return null;
  const out = { slug: found.slug, remote: found.remote, url: `https://github.com/${found.slug}` };
  if (found.folder) out.folder = found.folder;
  return out;
}

/** What the sidebar shows for what detect() found: `{ text, tip }`. */
function describe(found) {
  if (!found) {
    return { text: "No GitHub repository",
             tip: "This folder has no git remote on GitHub, so the Team suite can't connect to it on its own." };
  }
  const folder = found.folder ? ` · ${found.folder}` : "";
  return { text: found.slug + folder,
           tip: `${found.url}, from the "${found.remote}" remote` +
                (found.folder ? `; the Team suite shows ${found.folder} alone` : "") };
}

module.exports = { SITE, githubRepo, suiteUrl, parseRemotes, detect, describe };
