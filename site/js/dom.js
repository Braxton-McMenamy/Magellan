// Building pages from data that came out of somebody's repository (names, file paths, the
// messages findings quote from code): as text nodes, never as HTML, so nothing in a repository
// can put markup or script on the page.
//
//   h("p", { class: "muted" }, "shared ", h("b", {}, name))      -> <p class="muted">shared <b>…</b></p>

window.MagellanDom = (() => {
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v === null || v === undefined) continue;
      if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : String(kid));
    }
    return el;
  }

  // replace an element's contents; null and false (an "if" that didn't hold) add nothing
  function fill(el, ...kids) {
    el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
    return el;
  }

  const verdict = (v) => h("span", { class: `verdict ${["ok", "review", "block"].includes(v) ? v : "review"}` }, v);
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

  // one finding as the checklist shows it: severity, rule, where, what, the fix
  function finding(f) {
    return h("li", { class: "finding-item" },
      h("div", { class: "finding-head" },
        h("span", { class: `sev ${f.severity}` }, f.severity),
        h("code", {}, f.rule),
        h("span", { class: "where" }, `${f.path}:${f.line}`)),
      h("div", {}, f.message),
      f.fix ? h("div", { class: "fix" }, "Fix: ", f.fix) : null);
  }

  return { h, fill, verdict, plural, finding };
})();
