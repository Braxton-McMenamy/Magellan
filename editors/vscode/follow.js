"use strict";
// Following the code: which definition on the map the editor's cursor is in, and when to tell
// the map panel, as the full Magellan's graph tab follows the cursor. Free of VS Code, so plain
// Node tests it: `node --test "editors/vscode/test/*.test.js"`.
//
// The full Magellan asks its bridge "what is at path:line", which knows each definition's last
// line from the syntax tree. A Lite map gives each definition's first line only
// (magellan_lite/web.py), so the last one is read from the file's own text: a definition's
// block runs until the code comes back out to the indent the definition starts at.

// a line that is only a comment does not end a block (a comment at the margin inside a
// function, say)
const COMMENT = /^(#|\/\/|\/\*|\*|!)/;

/** How far a line is indented (a tab counts as four spaces). */
const indent = (text) => {
  let n = 0;
  for (const c of text) {
    if (c === " ") n += 1;
    else if (c === "\t") n += 4;
    else break;
  }
  return n;
};

/**
 * The last line (1-based) of the definition that starts on line ``start`` of ``lines`` (the
 * file's text, one string per line). Blank lines and comments don't decide; a deeper line is
 * inside; at the definition's own indent, a "{" opens its body (a brace on a line of its own),
 * a "}" or "end ..." closes it and is its last line, and anything else is the next statement.
 */
function endOf(lines, start) {
  const head = lines[start - 1];
  if (head === undefined) return start;
  const base = indent(head);
  let end = start;
  for (let i = start; i < lines.length; i++) {
    const text = lines[i], code = text.trim();
    if (!code || COMMENT.test(code)) continue;
    if (indent(text) > base || code.startsWith("{")) { end = i + 1; continue; }
    if (/^[}\])]/.test(code) || /^end\b/i.test(code)) end = i + 1;
    break;
  }
  return end;
}

/** Where a definition ends: the map's end line when it has one, else read from the text. */
function extent(node, starts, lines) {
  const start = Number(node.line);
  const given = Number(node.end_line || node.end);
  if (given >= start) return given;
  // the next definition's first line bounds this one when there is no text to read, and when
  // indentation says nothing (fixed-form Fortran and COBOL start every statement in one column)
  const next = starts.find((s) => s > start);
  const before = next ? next - 1 : Infinity;
  if (!lines) return before;
  const end = endOf(lines, start);
  return end === start && node.lang && node.lang !== "python" ? before : end;
}

/**
 * The definition on the map that ``line`` (1-based) of ``path`` is in: the innermost one (the
 * one that starts last) whose lines hold it, as the full Magellan's bridge picks (serve.py
 * node_at). ``path`` is project-relative with "/" (as the map has it); ``lines`` is the file's
 * text, one string per line, when there is one. Definitions the change deleted are not in the
 * file any more. Null when the line is outside every definition the map knows.
 */
function definitionAt(nodes, path, line, lines) {
  const here = (nodes || []).filter((n) => n && n.path === path && !n.removed && Number(n.line) > 0);
  const starts = [...new Set(here.map((n) => Number(n.line)))].sort((a, b) => a - b);
  // latest start first; on one line, the longest name (a method before its class)
  here.sort((a, b) => Number(b.line) - Number(a.line) || String(b.id).length - String(a.id).length);
  for (const n of here) {
    if (Number(n.line) <= line && line <= extent(n, starts, lines)) return n;
  }
  return null;
}

/**
 * When to tell the panel. ``cursor(where)`` on every move of the cursor ({ path, line,
 * lines() }); once it has rested ``delay`` ms, ``find(where)`` names the definition there and
 * ``send(node)`` tells the panel, only when it is not the one told last.
 *
 * ``opened(path, line)``: the panel itself just opened that place in the editor (a click on
 * the map, a finding's link). The cursor landing there is not a click in the code, so it does
 * not move the map; without this, opening a definition from a local map would move the map to
 * that definition's own neighbourhood. Any other move clears it, as does ``quiet`` ms passing.
 */
function follower({ find, send, delay = 250, quiet = 1500, now = Date.now,
  timers = { set: setTimeout, clear: clearTimeout } }) {
  let timer = null, last = null, expect = null;

  function settle(where, extra) {
    const ours = expect && now() - expect.at <= quiet
      && where.path === expect.path && where.line === expect.line;
    expect = null;
    if (ours) return null;
    const node = find(where);
    if (!node || node.id === last) return null;
    last = node.id;
    send(node, extra);
    return node;
  }

  return {
    cursor(where) {
      timers.clear(timer);
      timer = timers.set(() => { timer = null; settle(where); }, delay);
    },
    /** Now, without waiting (a new report; a panel that just opened, with ``extra``). */
    now(where, extra) {
      timers.clear(timer);
      timer = null;
      return where ? settle(where, extra) : null;
    },
    opened(path, line) { expect = { path, line: Number(line) || 1, at: now() }; },
    /** A new panel has heard nothing yet. */
    reset() { last = null; expect = null; },
    dispose() { timers.clear(timer); timer = null; },
  };
}

module.exports = { definitionAt, endOf, follower };
