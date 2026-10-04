"use strict";
// Which GitHub repository a folder belongs to, with a fake Git extension and a fake git:
// node --test "editors/vscode/test/*.test.js"
const test = require("node:test");
const assert = require("node:assert/strict");
const repo = require("../repo");

test("GitHub remotes in every form git writes them give owner/name", () => {
  for (const url of [
    "https://github.com/Braxton-McMenamy/Magellan",
    "https://github.com/Braxton-McMenamy/Magellan.git",
    "https://github.com/Braxton-McMenamy/Magellan/",
    "https://github.com/Braxton-McMenamy/Magellan.git/",
    "http://github.com/Braxton-McMenamy/Magellan",
    "https://brayton@github.com/Braxton-McMenamy/Magellan.git",
    "https://ghp_abc123@github.com/Braxton-McMenamy/Magellan.git",
    "git@github.com:Braxton-McMenamy/Magellan.git",
    "git@github.com:Braxton-McMenamy/Magellan",
    "ssh://git@github.com/Braxton-McMenamy/Magellan.git",
    "ssh://git@github.com:22/Braxton-McMenamy/Magellan",
    "ssh://git@ssh.github.com:443/Braxton-McMenamy/Magellan.git",
    "git://github.com/Braxton-McMenamy/Magellan.git",
    "https://www.github.com/Braxton-McMenamy/Magellan",
    "git@www.github.com:Braxton-McMenamy/Magellan.git",
    "HTTPS://GitHub.com/Braxton-McMenamy/Magellan",
    "  https://github.com/Braxton-McMenamy/Magellan.git\n",
  ]) assert.equal(repo.githubRepo(url), "Braxton-McMenamy/Magellan", url);
});

test("a password or token in a remote never comes back", () => {
  for (const url of [
    "https://brayton:ghp_SECRET@github.com/acme/widgets.git",
    "https://x-access-token:ghs_SECRET@github.com/acme/widgets",
    "https://ghp_SECRET@github.com/acme/widgets",
    "https://user:p@ss_SECRET@github.com/acme/widgets",
  ]) assert.equal(repo.githubRepo(url), "acme/widgets", url);
});

test("anything not on github.com is not a GitHub repository", () => {
  for (const url of [
    "https://gitlab.com/acme/widgets.git", "git@gitlab.com:acme/widgets.git",
    "https://github.mycorp.com/acme/widgets", "git@github.mycorp.com:acme/widgets.git",
    "https://github.com.evil.example/acme/widgets", "https://evil.example/github.com/acme/widgets",
    "https://notgithub.com/acme/widgets", "ftp://github.com/acme/widgets",
    "C:\\Users\\me\\widgets", "/home/me/widgets", "../widgets", "file:///home/me/widgets",
    "github.com/acme/widgets",
    "https://github.com/acme", "https://github.com/acme/widgets/tree/main", "https://github.com/",
    "", "   ", "not a url", null, undefined, 42, {},
  ]) assert.equal(repo.githubRepo(url), null, String(url));
});

test("owners and names follow GitHub's rules", () => {
  const at = (owner, name) => repo.githubRepo(`https://github.com/${owner}/${name}`);
  assert.equal(at("a".repeat(39), "n"), `${"a".repeat(39)}/n`);
  assert.equal(at("a".repeat(40), "n"), null);
  assert.equal(at("-acme", "n"), null);
  assert.equal(at("ac_me", "n"), null);
  assert.equal(at("acme", "x".repeat(100)), `acme/${"x".repeat(100)}`);
  assert.equal(at("acme", "x".repeat(101)), null);
  assert.equal(at("acme", "my.repo_v2-final"), "acme/my.repo_v2-final");
  assert.equal(at("acme", ".github"), "acme/.github");
  for (const name of [".", "..", ".git", "wid gets", "wid%20gets"]) assert.equal(at("acme", name), null, name);
});

test("the Team suite link carries the repository, encoded", () => {
  assert.equal(repo.suiteUrl("Braxton-McMenamy/Magellan"),
    "https://magellan-code.pages.dev/suite.html?repo=Braxton-McMenamy%2FMagellan");
  assert.equal(repo.suiteUrl("acme/widgets", "http://localhost:8000/"),
    "http://localhost:8000/suite.html?repo=acme%2Fwidgets");
  assert.equal(new URL(repo.suiteUrl("a/b&c=d")).searchParams.get("repo"), "a/b&c=d");
});

/** VS Code with just its Git extension, holding these repositories. */
function fakeVscode(repositories, { active = true } = {}) {
  const seen = { activated: 0, version: null };
  const exports = { getAPI: (version) => { seen.version = version; return { repositories }; } };
  const ext = { isActive: active, exports: active ? exports : undefined,
                activate: async () => { seen.activated += 1; return exports; } };
  return { seen, extensions: { getExtension: (id) => (id === "vscode.git" ? ext : undefined) } };
}
const repository = (root, remotes) => ({ rootUri: { fsPath: root }, state: { remotes } });
const remote = (name, fetchUrl, pushUrl = fetchUrl) => ({ name, fetchUrl, pushUrl });

/** A fake child_process.execFile for `git remote -v`: prints `answer`, or fails with it if an Error. */
function fakeGit(answer) {
  const calls = [];
  const run = (exe, args, opts, cb) => {
    calls.push({ exe, args, opts });
    setImmediate(() => (answer instanceof Error ? cb(answer, "", answer.message) : cb(null, answer, "")));
  };
  return { run, calls };
}

const REMOTE_V = [
  "upstream\thttps://github.com/acme/widgets.git (fetch)",
  "upstream\thttps://github.com/acme/widgets.git (push)",
  "origin\thttps://brayton:ghp_SECRET@github.com/Braxton-McMenamy/Magellan.git (fetch) [blob:none]",
  "origin\thttps://brayton:ghp_SECRET@github.com/Braxton-McMenamy/Magellan.git (push)",
  "",
].join("\r\n");

test("detect asks the Git extension, and the deepest repository holding the folder wins", async () => {
  const vscode = fakeVscode([
    repository("/work", [remote("origin", "https://github.com/acme/monorepo.git")]),
    repository("/work/app", [remote("origin", "git@github.com:acme/app.git")]),
    repository("/work/app/vendor/lib", [remote("origin", "https://github.com/other/lib")]),
    repository("/work/app-old", [remote("origin", "https://github.com/acme/app-old")]),
  ]);
  const git = fakeGit(REMOTE_V);
  const at = (folderPath) => repo.detect({ vscode, folderPath, run: git.run, platform: "linux" });
  assert.deepEqual(await at("/work/app/src"),
    { slug: "acme/app", remote: "origin", url: "https://github.com/acme/app" });
  assert.equal((await at("/work/app")).slug, "acme/app");
  assert.equal((await at("/work/app/vendor/lib/src")).slug, "other/lib");
  assert.equal((await at("/work/app-old/x")).slug, "acme/app-old");   // not /work/app's
  assert.equal((await at("/work/docs")).slug, "acme/monorepo");
  assert.equal(vscode.seen.version, 1);
  assert.equal(git.calls.length, 0);
});

test("on Windows, the folder matches its repository whatever the case and slashes", async () => {
  const vscode = fakeVscode([
    repository("c:\\users\\me\\code", [remote("origin", "https://github.com/me/code")]),
    repository("c:\\users\\me\\code\\magellan", [remote("origin", "https://github.com/Braxton-McMenamy/Magellan.git")]),
    repository("c:\\users\\me\\code\\magellan-fork", [remote("origin", "https://github.com/me/magellan-fork")]),
  ]);
  const git = fakeGit(new Error("git should not run"));
  for (const folderPath of ["C:\\Users\\Me\\Code\\Magellan\\editors\\vscode\\", "C:/Users/Me/Code/Magellan"]) {
    const found = await repo.detect({ vscode, folderPath, run: git.run, platform: "win32" });
    assert.equal(found && found.slug, "Braxton-McMenamy/Magellan", folderPath);
  }
  assert.equal(git.calls.length, 0);

  const linux = fakeVscode([repository("/home/me/Code", [remote("origin", "https://github.com/me/code")])]);
  assert.equal(await repo.detect({ vscode: linux, folderPath: "/home/me/code",
    run: fakeGit(new Error("not a git repository")).run, platform: "linux" }), null);   // case counts here
});

test("origin first; otherwise any remote on GitHub, by its fetch or push URL", async () => {
  const at = (remotes) => repo.detect({ vscode: fakeVscode([repository("/w", remotes)]), folderPath: "/w",
    run: fakeGit("").run, platform: "linux" });
  assert.deepEqual(await at([remote("origin", "https://gitlab.com/acme/widgets.git"),
                             remote("github", "git@github.com:acme/widgets.git")]),
    { slug: "acme/widgets", remote: "github", url: "https://github.com/acme/widgets" });
  assert.equal((await at([remote("upstream", "https://github.com/acme/widgets"),
                          remote("origin", "https://github.com/me/widgets")])).remote, "origin");
  assert.equal((await at([remote("mirror", "/srv/git/widgets.git", "git@github.com:acme/widgets.git")])).slug,
    "acme/widgets");
});

test("a Git extension that hasn't started yet is started", async () => {
  const vscode = fakeVscode([repository("/w", [remote("origin", "https://github.com/acme/widgets")])], { active: false });
  const found = await repo.detect({ vscode, folderPath: "/w", run: fakeGit("").run, platform: "linux" });
  assert.equal(found.slug, "acme/widgets");
  assert.equal(vscode.seen.activated, 1);
});

test("before the Git extension has opened any repository, detect asks git itself", async () => {
  const git = fakeGit(REMOTE_V);
  const found = await repo.detect({ vscode: fakeVscode([]), folderPath: "/w", run: git.run });
  assert.deepEqual(found,
    { slug: "Braxton-McMenamy/Magellan", remote: "origin", url: "https://github.com/Braxton-McMenamy/Magellan" });
  assert.doesNotMatch(JSON.stringify(found), /SECRET|ghp_/);
  assert.deepEqual(git.calls,
    [{ exe: "git", args: ["remote", "-v"], opts: { cwd: "/w", timeout: 10000, windowsHide: true } }]);
});

test("no Git extension, a turned-off one, or nothing on GitHub in it: git itself answers", async () => {
  const off = { extensions: { getExtension: () => ({ isActive: true,
    exports: { getAPI() { throw new Error("Git model not found"); } } }) } };
  const broken = { extensions: { getExtension: () => ({ isActive: false,
    activate: () => Promise.reject(new Error("failed to activate")) }) } };
  for (const vscode of [
    undefined,
    { extensions: { getExtension: () => undefined } },
    off,
    broken,
    fakeVscode([repository("/elsewhere", [remote("origin", "https://github.com/x/y")])]),
    fakeVscode([repository("/w", [remote("origin", "https://gitlab.com/x/y")])]),
  ]) {
    const found = await repo.detect({ vscode, folderPath: "/w", run: fakeGit(REMOTE_V).run, platform: "linux" });
    assert.equal(found && found.slug, "Braxton-McMenamy/Magellan");
  }
});

test("when git fails, or no remote is on GitHub, detect is null and never throws", async () => {
  const notRepo = Object.assign(new Error("fatal: not a git repository"), { code: 128 });
  const noGit = Object.assign(new Error("spawn git ENOENT"), { code: "ENOENT" });
  const throws = () => { throw new Error("spawn failed"); };
  for (const run of [fakeGit(notRepo).run, fakeGit(noGit).run, throws, fakeGit("").run,
                     fakeGit("origin\thttps://gitlab.com/x/y.git (fetch)\n").run]) {
    assert.equal(await repo.detect({ vscode: fakeVscode([]), folderPath: "/w", run }), null);
  }
  assert.equal(await repo.detect({ vscode: fakeVscode([]), folderPath: "", run: fakeGit(REMOTE_V).run }), null);
  assert.equal(await repo.detect(), null);
});

test("the sidebar shows owner/name, and says where it came from", () => {
  const d = repo.describe({ slug: "Braxton-McMenamy/Magellan", remote: "origin",
                            url: "https://github.com/Braxton-McMenamy/Magellan" });
  assert.equal(d.text, "Braxton-McMenamy/Magellan");
  assert.equal(d.tip, 'https://github.com/Braxton-McMenamy/Magellan, from the "origin" remote');
  assert.equal(repo.describe(null).text, "No GitHub repository");
  assert.match(repo.describe(null).tip, /no git remote on GitHub/);
});
