# Magellan Lite for VS Code

Know what your change breaks before you commit it, without leaving the editor.

- **On save**, Magellan Lite checks your change against the last commit: each finding lands in
  the Problems panel and as a squiggle on its line, and the verdict (ok, review, block) sits in
  the status bar.
- **The Magellan Lite sidebar** (its icon is in the Activity Bar, like the full Magellan's) has
  three views: **Checklist** (the findings), **What it reaches** (the blast radius, hop by hop,
  with scores) and **Team** (each teammate, with their conflicts with your work). Click an item
  to open its line. Files with findings show a count in the Explorer.
- **Magellan Lite: Show the map** opens a panel with the change and what it reaches, hop by hop,
  drawn by the same renderer as the website. Click a finding to jump to its line.
- **Magellan Lite: Share my work in progress with my team** and **Check against my team's work
  in progress** run `magellan-lite share` and `magellan-lite team`: problems that only your work
  plus a teammate's has show up on the line, saying whose work they come from.
- **Share On Save** (the `magellanLite.shareOnSave` setting, off until you turn it on): after
  you save, your work in progress is shared for you (at most once every 15 seconds), so your
  team's [Team suite](https://magellan-code.pages.dev/suite.html) follows your work live.
  Whoever can read the repository can read what you share: on a public repository, everyone.

## Try it

The extension runs `python -m magellan_lite`, so install Magellan Lite into the Python it uses:

```sh
pip install -e .                        # in the Magellan Lite repository
```

Then, from the repository root, either open a VS Code window with it loaded (the path must be
a full one: with a relative path, VS Code on Windows looks for `\editors\vscode` at the root of
the drive and silently opens a window without the extension):

```powershell
code --extensionDevelopmentPath="$PWD\editors\vscode" "$PWD"       # PowerShell
```

```sh
code --extensionDevelopmentPath="$(pwd)/editors/vscode" "$(pwd)"   # bash
```

The window's title starts with **[Extension Development Host]**, and **Magellan Lite** appears
at the left of its status bar.

or package it and install the package:

```sh
cd editors/vscode && npm run package    # makes magellan-lite-0.1.0.vsix
code --install-extension magellan-lite-0.1.0.vsix
```

If you have the full Magellan extension installed, disable it first (Extensions view → Magellan
→ Disable) so the two don't both mark the same files. Settings: `magellanLite.pythonPath`
(the Python that has Magellan Lite), `magellanLite.checkOnSave`, `magellanLite.against`,
`magellanLite.shareOnSave`.

## How it's built

| File | What |
|---|---|
| `extension.js` | commands, check on save, the Problems panel, the status bar, the map panel |
| `sidebar.js` | the sidebar's three views and the Explorer badges |
| `lite.js` | what to do with Magellan Lite's JSON (no VS Code in it, so plain Node tests it) |
| `panel.js`, `media/panel.*` | the map panel's page |
| `media/map.js`, `media/map.css` | the website's map renderer, copied by `sync.js` (edit `site/`, then `npm run sync`) |
| `test/` | `npm test`: the extension against a fake VS Code and a fake Python |

## Tasks

Every task is a `TODO(<name>)` where the work goes, with numbered steps.

**Faidh**, in this order (each is small, and the first two have a test waiting):

1. `TODO(faidh) 1` in `extension.js`: the status bar turns red on block and yellow on review.
2. `TODO(faidh) 2` in `lite.js`: a `magellanLite.showLow` setting to hide low-severity findings.
3. `TODO(faidh) 3` in `extension.js`: an icon for the Extensions view.
4. TODO(faidh) 4, here: once it runs for you, take a screenshot of a squiggle and the status bar
   (the CrowdStrike-class example in `demo/incidents/sensor-signature-break` makes a good one),
   save it as `media/readme/check.png`, and show it at the top of this README with
   `![Magellan Lite in VS Code](media/readme/check.png)`.

**Braxton**: `TODO(braxton)` in `media/panel.js`: the 3D and 2D views from the original
Magellan, switchable in the map panel. They go in `site/js/` first, so the website's account
pages can use them too.
