# Live demo: Magellan Lite in VS Code, live on the website

A small shipping back office (`fleet/`) with three marked places to change. Each one is a
two-key swap: the "before" line, with the "after" line commented right under it.

| Step | File | What changes | Magellan Lite says |
|---|---|---|---|
| DEMO 1, mild | `fleet/voyage.py` | `plan_route(..., stops=None)` becomes `stops=[]` | **medium** `mutable-default-argument`; verdict **review** |
| DEMO 2, high | `fleet/voyage.py` | a charter renews on "the same day next year" | **high** `leap-day-date` (Azure's 2012 leap-day outage); verdict **block** |
| DEMO 3, critical | `fleet/cargo.py` | `total_weight(items)` gains a required `unit` | **critical** `signature-break` in `fleet/fees.py`, a file nobody opened; the blast radius goes from 3 to 8 definitions |

To make a swap: put the cursor on the line under the `# DEMO` comment, press **Ctrl+Shift+K**
(delete the line), then **Ctrl+/** on the commented line that moved up, then **Ctrl+S**.

## Before going on stage

1. Open this folder in VS Code. Magellan Lite's sidebar shows the repository it found from
   `git remote` and the verdict **ok**.
2. Settings → `magellanLite.shareOnSave` → on. Every save now publishes the work in progress
   to `refs/wip/Brayton` on GitHub (no commit, no branch), at most once every 15 seconds.
3. Command Palette → **Magellan Lite: Open the Team suite**. The website opens connected to this
   repository. Paste a GitHub token there: it polls every 15 seconds with one, every 90 without.
   A classic token with no scopes can read a public repository. It stays in that tab only.
4. Put VS Code on the left and the browser on the right. Close every file except
   `fleet/voyage.py` and `fleet/cargo.py`.
5. Do one practice save and press **Look now** on the website: your name appears with **ok**.

## On stage, about 60 seconds

1. **DEMO 1** in `voyage.py`, save: a squiggle under `[]`; the status bar turns **review**.
2. **DEMO 2** in `voyage.py`, save: the leap-day line is flagged **high**; the status bar turns **block**.
3. **DEMO 3** in `cargo.py`, save: a **critical** finding in `fees.py`, a file that isn't open.
   Click it in the sidebar to jump there. Then **Magellan Lite: Show the map**: the change and
   everything it reaches light up. Right-click `total_weight` → **Show sub-graph**.
4. Switch to the browser and press **Look now**: your work in progress is there, same three
   findings, the same nodes lit on the 3D map, without a commit or a push of a branch.

## Reset

```
git checkout -- fleet
```

Then Command Palette → **Magellan Lite: Share my work in progress with my team**, so the website goes back to
**ok** too.

Run the tests with `python -m unittest`: they pass now, and two of them fail after DEMO 3. That
is the point. Magellan Lite said so before anything ran.
