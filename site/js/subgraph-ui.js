// The sub-graph's controls (js/subgraph.js), shared by the Scene, the Team suite and the VS Code
// map panel (copied there by editors/vscode/sync.js): the small menu a right-click on a dot
// opens, and the strip of tabs above the viewport -- the whole map first, then each sub-graph
// opened from it. Plain DOM built here: the pages' content security policy runs no inline
// script or handler. Names come from somebody's code, so they go in as text, never as HTML.
//
//   MagellanSubgraphUI.menu(event, node.label, [{ label: "Show sub-graph", run() {} }, ...]);
//   MagellanSubgraphUI.strip(el, [{ id, title, closable }], activeId, { onPick(tab), onClose(tab) });
//   MagellanSubgraphUI.keys((step) => ...);      // [ and ]: the previous and the next tab

window.MagellanSubgraphUI = (() => {
  let open = null;        // the menu on screen: { el, back } (back: where the keyboard was)
  let seq = 0;

  const make = (tag, cls, text) => {
    const x = document.createElement(tag);
    if (cls) x.className = cls;
    if (text !== undefined) x.textContent = text;
    return x;
  };
  const typing = (t) => !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ""));

  // -- the menu -----------------------------------------------------------------------------
  const outside = (e) => { if (open && !open.el.contains(e.target)) close(); };
  const lost = () => close();

  /** Close the menu, if one is open; ``refocus`` puts the keyboard back where it was. */
  function close(refocus = false) {
    if (!open) return;
    const { el, back } = open;
    open = null;
    el.remove();
    removeEventListener("pointerdown", outside, true);
    removeEventListener("blur", lost);
    removeEventListener("resize", lost);
    removeEventListener("scroll", lost, true);
    if (refocus && back && back.isConnected && back.focus) back.focus();
  }

  // the element the event was for: the dot's own element (not the circle inside it), the canvas
  const source = (event) => (event && (event.currentTarget || event.target)) || null;

  /** Where to open: the pointer, or (from the keyboard, which has none) under the element;
   *  ``above`` is where the menu ends when there is no room below. */
  function place(event) {
    if (event && (event.clientX || event.clientY)) return { x: event.clientX, y: event.clientY, above: event.clientY };
    const t = source(event);
    const r = t && t.getBoundingClientRect ? t.getBoundingClientRect() : { left: 20, top: 20, bottom: 20, width: 0 };
    return { x: r.left + Math.min(r.width / 2, 24), y: r.bottom, above: r.top };
  }

  /**
   * A menu at ``event``'s place: ``title`` (the dot's name) and a button per item
   * ({ label, detail?, run }). Up and Down move, Enter or a click chooses, Escape or Tab (or a
   * click anywhere else) closes and gives the keyboard back.
   */
  function menu(event, title, items) {
    close();
    const from = source(event);
    const back = (from && from.focus ? from : null) || document.activeElement;
    const el = make("div", "sg-menu");
    const id = `sg-menu-${++seq}`;
    const head = make("p", "sg-menu-title", title);
    head.id = id;
    const list = make("menu");
    list.setAttribute("role", "menu");
    list.setAttribute("aria-labelledby", id);
    const buttons = items.filter(Boolean).map((item) => {
      const li = make("li");
      li.setAttribute("role", "none");
      const b = make("button", "sg-menu-item", item.label);
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.tabIndex = -1;
      if (item.detail) { b.append(make("span", "sg-menu-detail", item.detail)); b.title = item.detail; }
      b.addEventListener("click", () => { close(true); item.run(); });
      li.append(b);
      list.append(li);
      return b;
    });
    el.append(head, list);
    el.addEventListener("contextmenu", (e) => e.preventDefault());    // the menu key lands here too
    el.addEventListener("keydown", (e) => {
      e.stopPropagation();                         // [ ] and the 3D view's keys stay out of it
      const i = buttons.indexOf(document.activeElement);
      const go = (j) => { e.preventDefault(); buttons[(j + buttons.length) % buttons.length].focus(); };
      if (e.key === "ArrowDown") go(i + 1);
      else if (e.key === "ArrowUp") go(i - 1);
      else if (e.key === "Home") go(0);
      else if (e.key === "End") go(buttons.length - 1);
      else if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); close(true); }
    });
    document.body.append(el);

    // on screen: below and right of the pointer, or above and left where there is no room
    const at = place(event), w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = `${Math.max(8, Math.min(at.x, innerWidth - w - 8))}px`;
    el.style.top = `${at.y + h > innerHeight - 8 ? Math.max(8, at.above - h) : at.y}px`;

    open = { el, back };
    addEventListener("pointerdown", outside, true);
    addEventListener("blur", lost);
    addEventListener("resize", lost);
    addEventListener("scroll", lost, true);
    if (buttons.length) buttons[0].focus();
    return el;
  }

  // -- the tabs -----------------------------------------------------------------------------
  /**
   * The strip of tabs: ``tabs`` is [{ id, title, hint?, closable }], the whole map first. Hidden
   * while that is the only one. A tab's × closes it, as do Delete on it and a middle click.
   * ``on.away()`` names where the keyboard goes when the strip it was on hides (the last
   * sub-graph closed): the viewport, say.
   */
  function strip(el, tabs, activeId, on) {
    const had = el.contains(document.activeElement);
    let current = null;
    el.replaceChildren(...tabs.map((t) => {
      const shown = t.id === activeId;
      const item = make("span", `sg-tab${shown ? " on" : ""}${t.closable ? "" : " home"}`);
      const name = make("button", "sg-tab-name", t.title);
      name.type = "button";
      name.title = t.hint || t.title;
      name.setAttribute("aria-pressed", String(shown));
      name.addEventListener("click", () => on.onPick(t));
      item.append(name);
      if (shown) current = name;
      if (t.closable) {
        const x = make("button", "sg-tab-x", "×");
        x.type = "button";
        x.title = "Close";
        x.setAttribute("aria-label", `Close ${t.title}`);
        x.addEventListener("click", () => on.onClose(t));
        name.addEventListener("keydown", (e) => { if (e.key === "Delete") { e.preventDefault(); on.onClose(t); } });
        item.addEventListener("auxclick", (e) => { if (e.button === 1) { e.preventDefault(); on.onClose(t); } });
        item.append(x);
      }
      return item;
    }));
    el.hidden = tabs.length < 2;
    if (had && el.hidden && on.away) { const to = on.away(); if (to && to.focus) to.focus(); }
    if (!current) return;
    if (had) current.focus();                        // the keyboard stays on the tabs
    const box = current.parentElement;               // and the shown tab stays in sight
    if (box.offsetLeft < el.scrollLeft || box.offsetLeft + box.offsetWidth > el.scrollLeft + el.clientWidth) {
      el.scrollLeft = Math.max(0, box.offsetLeft - 24);
    }
  }

  /** [ and ] anywhere on the page, except while typing: ``step(-1)`` and ``step(1)``. */
  function keys(step) {
    addEventListener("keydown", (e) => {
      if ((e.key !== "[" && e.key !== "]") || e.defaultPrevented || typing(e.target)) return;
      if (e.metaKey || (e.ctrlKey && !e.altKey)) return;     // Ctrl+Alt is AltGr: [ on many keyboards
      e.preventDefault();
      close();
      step(e.key === "]" ? 1 : -1);
    });
  }

  return { menu, close, strip, keys };
})();
