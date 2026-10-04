#!/usr/bin/env node
/*
 * Extract declarations and resolved references from a TypeScript/JavaScript
 * project as JSON, using the TypeScript compiler's own parser and checker.
 *
 *   node ts_extract.js <root> [--check]
 *
 * Nothing is executed or emitted. The checker resolves every identifier to its
 * declaration (through imports, re-exports and aliases), which is exactly the
 * job the Python frontend does by hand -- here it comes for free and is exact.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const cp = require("child_process");
const crypto = require("crypto");

function loadTypeScript() {
  const tries = [() => require("typescript")];
  if (process.env.MAGELLAN_TS_PATH) tries.push(() => require(path.join(process.env.MAGELLAN_TS_PATH, "typescript")));
  tries.push(() => {
    const root = cp.execSync("npm root -g", { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    return require(path.join(root, "typescript"));
  });
  for (const t of tries) { try { return t(); } catch (e) { /* next */ } }
  return null;
}

const ts = loadTypeScript();
if (!ts) { console.error("typescript module not found"); process.exit(2); }
if (process.argv.includes("--check")) { console.log(ts.version); process.exit(0); }

const root = path.resolve(process.argv[2] || ".");
const SKIP_DIRS = new Set(["node_modules", "dist", "build", "out", "coverage", ".git", ".magellan", ".next", ".turbo", "target"]);
const EXT = /\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/;

function walk(dir, acc) {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    if (ent.name.startsWith(".") && ent.isDirectory()) continue;
    if (ent.isDirectory()) { if (!SKIP_DIRS.has(ent.name)) walk(path.join(dir, ent.name), acc); continue; }
    if (EXT.test(ent.name) && !/\.d\.(ts|mts|cts)$/.test(ent.name)) acc.push(path.join(dir, ent.name));
  }
  return acc;
}

const files = walk(root, []).sort();
let options = {
  allowJs: true, checkJs: false, noEmit: true, skipLibCheck: true, noResolve: false,
  target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler, jsx: ts.JsxEmit.Preserve,
  esModuleInterop: true, resolveJsonModule: true, strict: false, types: [],
};
const cfgPath = path.join(root, "tsconfig.json");
if (fs.existsSync(cfgPath)) {
  const raw = ts.readConfigFile(cfgPath, ts.sys.readFile);
  if (!raw.error) {
    const parsed = ts.parseJsonConfigFileContent(raw.config, ts.sys, root);
    options = Object.assign({}, parsed.options, { allowJs: true, noEmit: true, skipLibCheck: true,
      checkJs: false, types: parsed.options.types || [] });
  }
}
const program = ts.createProgram(files, options);
const checker = program.getTypeChecker();

const rel = (f) => path.relative(root, f).split(path.sep).join("/");
const inProject = new Set(files.map((f) => path.resolve(f)));
const isProjectFile = (sf) => inProject.has(path.resolve(sf.fileName));
const printer = ts.createPrinter({ removeComments: true });

function modQual(relpath) {
  return relpath.replace(EXT, "").split("/").join(".");
}
// line endings are not a change: git on Windows checks out CRLF but `git show` gives LF
const sha = (s) => crypto.createHash("sha256").update(s.replace(/\r\n?/g, "\n")).digest("hex").slice(0, 16);

const decls = [];           // emitted declarations
const refs = [];            // emitted references
const nodeIds = new Map();  // ts.Node -> declaration id
const modules = [];

function line(sf, pos) { return sf.getLineAndCharacterOfPosition(pos).line + 1; }
function hasMod(node, kind) { return !!(node.modifiers && node.modifiers.some((m) => m.kind === kind)); }
function isExported(node) {
  return hasMod(node, ts.SyntaxKind.ExportKeyword) || hasMod(node, ts.SyntaxKind.DefaultKeyword);
}
function nameOf(node) {
  if (node.name) {
    if (ts.isIdentifier(node.name) || ts.isPrivateIdentifier(node.name) || ts.isStringLiteral(node.name)) return node.name.text;
    return node.name.getText();
  }
  return null;
}
function jsdoc(node) {
  const docs = ts.getJSDocCommentsAndTags ? node.jsDoc : null;
  return docs && docs.length ? docs.map((d) => d.getText()).join("\n") : "";
}
function paramInfo(p) {
  return {
    name: p.name.getText(),
    optional: !!p.questionToken,
    rest: !!p.dotDotDotToken,
    default: p.initializer ? p.initializer.getText() : null,
    type: p.type ? p.type.getText() : "",
  };
}
function sigOf(fn) {
  const params = (fn.parameters || []).filter((p) => !(p.name.getText() === "this")).map(paramInfo);
  const ret = fn.type ? fn.type.getText() : "";
  const isAsync = hasMod(fn, ts.SyntaxKind.AsyncKeyword);
  const text = `${isAsync ? "async " : ""}(${params.map((p) => `${p.rest ? "..." : ""}${p.name}${p.optional ? "?" : ""}${p.type ? ": " + p.type : ""}${p.default ? " = " + p.default : ""}`).join(", ")})${ret ? ": " + ret : ""}`;
  return { params, ret, text, async: isAsync };
}
function bodyText(fn) {
  const body = fn.body || fn.initializer;
  return body ? printer.printNode(ts.EmitHint.Unspecified, body, fn.getSourceFile()) : "";
}
function returnsValue(fn) {
  let found = false;
  const visit = (n) => {
    if (found) return;
    if (ts.isFunctionLike(n) && n !== fn) return;
    if (ts.isReturnStatement(n) && n.expression) { found = true; return; }
    ts.forEachChild(n, visit);
  };
  if (!fn.body) return false;
  if (!ts.isBlock(fn.body)) return true;    // concise arrow body
  visit(fn.body);
  return found;
}
function countStatements(fn) {
  return fn.body && ts.isBlock(fn.body) ? fn.body.statements.length : (fn.body ? 1 : 0);
}
function decorators(node) {
  const ds = ts.canHaveDecorators && ts.canHaveDecorators(node) ? ts.getDecorators(node) : node.decorators;
  return (ds || []).map((d) => d.expression.getText());
}

function emit(d) { decls.push(d); return d; }

// ------------------------------------------------------------------ pass A
function declareFile(sf) {
  const relpath = rel(sf.fileName);
  const qual = modQual(relpath);
  const modId = `mod:${qual}`;
  const text = sf.text;
  const mod = emit({ id: modId, kind: "module", name: qual.split(".").pop(), qualname: qual, path: relpath,
    line: 1, endLine: line(sf, sf.end), parent: null, exported: true, statements: sf.statements.length });
  const shell = sf.statements.filter((st) => !(ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st) ||
      ts.isInterfaceDeclaration(st) || ts.isEnumDeclaration(st) || ts.isTypeAliasDeclaration(st) || ts.isVariableStatement(st)))
    .map((st) => printer.printNode(ts.EmitHint.Unspecified, st, sf)).join("\n");
  mod.bodyHash = sha(shell);
  mod.sigHash = sha(sf.statements.filter((st) => ts.isExportDeclaration(st) || ts.isExportAssignment(st))
    .map((st) => st.getText()).join("\n"));
  modules.push({ id: modId, qual, path: relpath });
  nodeIds.set(sf, modId);

  const exportedNames = new Set();
  for (const st of sf.statements) {           // export { a, b as c }
    if (ts.isExportDeclaration(st) && st.exportClause && ts.isNamedExports(st.exportClause) && !st.moduleSpecifier) {
      for (const el of st.exportClause.elements) exportedNames.add((el.propertyName || el.name).text);
    }
    if (ts.isExportAssignment(st) && ts.isIdentifier(st.expression)) exportedNames.add(st.expression.text);
  }

  function declareCallable(node, parentId, parentQual, name, kind, extra) {
    const s = sigOf(node);
    const qualname = `${parentQual}.${name}`;
    const id = `fn:${qualname}`;
    nodeIds.set(node, id);
    if (node.parent && ts.isVariableDeclaration(node.parent)) nodeIds.set(node.parent, id);
    emit(Object.assign({
      id, kind, name, qualname, path: relpath, line: line(sf, node.getStart()), endLine: line(sf, node.end),
      parent: parentId, async: s.async, sig: s, signature: `${name}${s.text}`,
      sigHash: sha(s.text), bodyHash: sha(bodyText(node)), docHash: sha(jsdoc(node)),
      decorators: decorators(node), returnsValue: returnsValue(node), statements: countStatements(node),
      hasParams: s.params.length > 0,
    }, extra || {}));
    return id;
  }

  function declareClass(node, parentId, parentQual, nameHint, exported) {
    const name = nameHint || nameOf(node) || "default";
    const qualname = `${parentQual}.${name}`;
    const id = `cls:${qualname}`;
    nodeIds.set(node, id);
    const isIface = ts.isInterfaceDeclaration(node);
    const isEnum = ts.isEnumDeclaration(node);
    const bases = [];
    const heritage = [];
    for (const h of node.heritageClauses || []) {
      for (const t of h.types) { bases.push(t.expression.getText()); heritage.push({ kind: h.token === ts.SyntaxKind.ImplementsKeyword ? "implements" : "extends", node: t.expression }); }
    }
    const declared = { id, kind: "class", name, qualname, path: relpath, line: line(sf, node.getStart()),
      endLine: line(sf, node.end), parent: parentId, exported, bases,
      tags: isIface ? ["interface"] : (isEnum ? ["enum"] : (hasMod(node, ts.SyntaxKind.AbstractKeyword) ? ["abstract"] : [])),
      decorators: decorators(node), sigHash: sha(bases.join(",") + decorators(node).join(",")),
      bodyHash: sha(printer.printNode(ts.EmitHint.Unspecified, node, sf)), docHash: sha(jsdoc(node)),
      heritage: [] };
    emit(declared);
    declared._heritage = heritage;
    for (const m of node.members || []) {
      const isStatic = hasMod(m, ts.SyntaxKind.StaticKeyword);
      const priv = hasMod(m, ts.SyntaxKind.PrivateKeyword) || hasMod(m, ts.SyntaxKind.ProtectedKeyword) ||
        (m.name && ts.isPrivateIdentifier(m.name));
      if (ts.isMethodDeclaration(m) || ts.isMethodSignature(m) || ts.isGetAccessor(m) || ts.isSetAccessor(m) || ts.isConstructorDeclaration(m)) {
        const mname = ts.isConstructorDeclaration(m) ? "constructor" : nameOf(m);
        if (!mname) continue;
        const tags = [];
        if (isStatic) tags.push("static");
        if (ts.isGetAccessor(m) || ts.isSetAccessor(m)) tags.push("property");
        if (hasMod(m, ts.SyntaxKind.AbstractKeyword) || ts.isMethodSignature(m)) tags.push("abstract");
        const mid = declareCallable(m, id, qualname, mname, "method",
          { exported: !priv && (exported || isIface), tags, isPrivate: !!priv });
        if (ts.isConstructorDeclaration(m)) {
          for (const p of m.parameters) {           // constructor(private x: T)
            if (p.modifiers && p.modifiers.some((k) => [ts.SyntaxKind.PrivateKeyword, ts.SyntaxKind.PublicKeyword, ts.SyntaxKind.ProtectedKeyword, ts.SyntaxKind.ReadonlyKeyword].includes(k.kind))) {
              const pn = p.name.getText();
              const pid = `iattr:${qualname}.${pn}`;
              nodeIds.set(p, pid);
              emit({ id: pid, kind: "instance_attr", name: pn, qualname: `${qualname}.${pn}`, path: relpath,
                line: line(sf, p.getStart()), endLine: line(sf, p.end), parent: id,
                exported: !p.modifiers.some((k) => k.kind === ts.SyntaxKind.PrivateKeyword),
                annotation: p.type ? p.type.getText() : "", value: "", sigHash: sha(p.type ? p.type.getText() : ""), bodyHash: "" });
            }
          }
        }
      } else if (ts.isPropertyDeclaration(m) || ts.isPropertySignature(m)) {
        const pn = nameOf(m);
        if (!pn) continue;
        const init = m.initializer;
        if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) {
          const cid = declareCallable(init, id, qualname, pn, "method",
            { exported: !priv && exported, tags: isStatic ? ["static"] : [], isPrivate: !!priv });
          nodeIds.set(m, cid);
          continue;
        }
        const kind = isStatic ? "class_attr" : "instance_attr";
        const pid = `${isStatic ? "attr" : "iattr"}:${qualname}.${pn}`;
        nodeIds.set(m, pid);
        emit({ id: pid, kind, name: pn, qualname: `${qualname}.${pn}`, path: relpath, line: line(sf, m.getStart()),
          endLine: line(sf, m.end), parent: id, exported: !priv && (exported || isIface),
          annotation: m.type ? m.type.getText() : "", value: init ? init.getText().slice(0, 120) : "",
          sigHash: sha(m.type ? m.type.getText() : ""), bodyHash: sha(init ? init.getText() : "") });
      }
    }
    return id;
  }

  // CommonJS (pre-ES-module JavaScript): `exports.x = ...`, `module.exports.x = ...`,
  // `module.exports = { x: ... }` and `module.exports = function ...` declare the module's
  // API. The checker resolves `require('./m').x` to these assignments, so each one becomes a
  // declaration and its nodes map to it (Express 3: lib/utils.js had none, and renaming an
  // exported helper went unseen).
  function exportName(target) {
    // `exports.x` / `module.exports.x` -> "x"; `module.exports` -> "" ; anything else -> null
    if (!ts.isPropertyAccessExpression(target)) return null;
    const obj = target.expression;
    if (ts.isIdentifier(obj) && obj.text === "exports") return target.name.text;
    if (ts.isPropertyAccessExpression(obj) && ts.isIdentifier(obj.expression) &&
        obj.expression.text === "module" && obj.name.text === "exports") return target.name.text;
    if (ts.isIdentifier(obj) && obj.text === "module" && target.name.text === "exports") return "";
    return null;
  }
  function declareExportValue(value, at, parentId, parentQual, name, extraNodes) {
    if (ts.isArrowFunction(value) || ts.isFunctionExpression(value)) {
      declareCallable(value, parentId, parentQual, name, "function", { exported: true });
      for (const n of extraNodes) nodeIds.set(n, `fn:${parentQual}.${name}`);
    } else if (ts.isClassExpression(value)) {
      declareClass(value, parentId, parentQual, name, true);
      for (const n of extraNodes) nodeIds.set(n, `cls:${parentQual}.${name}`);
    } else {
      const id = `var:${parentQual}.${name}`;
      if (!decls.some((d) => d.id === id)) {
        emit({ id, kind: "global_var", name, qualname: `${parentQual}.${name}`, path: relpath,
          line: line(sf, at.getStart()), endLine: line(sf, at.end), parent: parentId, exported: true,
          annotation: "", value: value.getText().slice(0, 120), isConst: false,
          sigHash: sha(""), bodyHash: sha(value.getText()) });
      }
      for (const n of extraNodes) nodeIds.set(n, id);
    }
  }
  function declareCommonJs(bin, parentId, parentQual) {
    // `exports.a = exports.b = value`: every name in the chain is the same value
    const names = [];
    let cur = bin;
    while (ts.isBinaryExpression(cur) && cur.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      const nm = exportName(cur.left);
      if (nm === null) return;
      names.push([nm, cur]);
      cur = cur.right;
    }
    for (const [nm, assign] of names) {
      if (nm === "") {
        if (ts.isObjectLiteralExpression(cur)) {
          for (const prop of cur.properties) {
            if (!prop.name || !ts.isIdentifier(prop.name)) continue;
            const pn = prop.name.text;
            if (ts.isMethodDeclaration(prop)) {
              declareCallable(prop, parentId, parentQual, pn, "function", { exported: true });
            } else if (ts.isPropertyAssignment(prop)) {
              declareExportValue(prop.initializer, prop, parentId, parentQual, pn, [prop]);
            }
          }
        } else if (ts.isArrowFunction(cur) || ts.isFunctionExpression(cur)) {
          declareCallable(cur, parentId, parentQual, "default", "function", { exported: true });
          nodeIds.set(assign, `fn:${parentQual}.default`);
        }
        continue;
      }
      declareExportValue(cur, assign, parentId, parentQual, nm, [assign, assign.left]);
    }
  }
  function visitTop(node, parentId, parentQual, inNamespace) {
    if (ts.isFunctionDeclaration(node)) {
      const name = nameOf(node) || "default";
      if (node.body || true) declareCallable(node, parentId, parentQual, name, "function",
        { exported: isExported(node) || exportedNames.has(name) });
    } else if (ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node) || ts.isEnumDeclaration(node)) {
      const name = nameOf(node) || "default";
      declareClass(node, parentId, parentQual, name, isExported(node) || exportedNames.has(name));
    } else if (ts.isVariableStatement(node)) {
      const exp = isExported(node);
      for (const d of node.declarationList.declarations) {
        if (!ts.isIdentifier(d.name)) continue;
        const name = d.name.text;
        const init = d.initializer;
        if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) {
          declareCallable(init, parentId, parentQual, name, "function", { exported: exp || exportedNames.has(name) });
          nodeIds.set(d, `fn:${parentQual}.${name}`);
        } else if (init && ts.isClassExpression(init)) {
          declareClass(init, parentId, parentQual, name, exp || exportedNames.has(name));
          nodeIds.set(d, `cls:${parentQual}.${name}`);
        } else {
          const id = `var:${parentQual}.${name}`;
          nodeIds.set(d, id);
          const isConst = (node.declarationList.flags & ts.NodeFlags.Const) !== 0;
          emit({ id, kind: "global_var", name, qualname: `${parentQual}.${name}`, path: relpath,
            line: line(sf, d.getStart()), endLine: line(sf, d.end), parent: parentId, exported: exp || exportedNames.has(name),
            annotation: d.type ? d.type.getText() : "", value: init ? init.getText().slice(0, 120) : "",
            isConst, sigHash: sha(d.type ? d.type.getText() : ""), bodyHash: sha(init ? init.getText() : "") });
        }
      }
    } else if (!inNamespace && ts.isExpressionStatement(node) && ts.isBinaryExpression(node.expression) &&
               node.expression.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      declareCommonJs(node.expression, parentId, parentQual);
    } else if (ts.isExportAssignment(node) && (ts.isArrowFunction(node.expression) || ts.isFunctionExpression(node.expression))) {
      declareCallable(node.expression, parentId, parentQual, "default", "function", { exported: true });
    } else if (ts.isModuleDeclaration(node) && node.body && ts.isModuleBlock(node.body)) {
      const ns = node.name.getText();
      for (const st of node.body.statements) visitTop(st, parentId, `${parentQual}.${ns}`, true);
    }
  }
  for (const st of sf.statements) visitTop(st, modId, qual, false);
  return { relpath, qual, modId };
}

// ------------------------------------------------------------------ pass B
const GROWERS = new Set(["push", "unshift", "add", "set", "splice", "concat"]);
const MUTATORS = new Set(["push", "pop", "shift", "unshift", "splice", "sort", "reverse", "add", "delete", "clear", "set", "fill", "copyWithin"]);

function targetDecl(sym) {
  if (!sym) return null;
  if (sym.flags & ts.SymbolFlags.Alias) { try { sym = checker.getAliasedSymbol(sym); } catch (e) { return null; } }
  const ds = sym.declarations || [];
  return { sym, decl: ds[0] || null, decls: ds };
}
function idOfDecl(decl) {
  if (!decl) return null;
  if (nodeIds.has(decl)) return nodeIds.get(decl);
  // shorthand / export specifier / class expression variable
  if (ts.isVariableDeclaration(decl) && nodeIds.has(decl)) return nodeIds.get(decl);
  return null;
}
function extName(sym, decl) {
  if (!decl) return null;
  const sf = decl.getSourceFile();
  const file = sf.fileName;
  if (program.isSourceFileDefaultLibrary(sf) || /node_modules\/typescript\/lib\//.test(file) ||
      /\/lib\.[\w.]+\.d\.ts$/.test(file)) return `global.${sym.getName()}`;
  const m = file.match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//);
  if (m) return `${m[1].replace(/^@types\//, "")}.${sym.getName()}`;
  return null;
}

function referencePass(sf, info) {
  const relpath = info.relpath;
  const modId = info.modId;
  const importedRoots = new Set();

  function specRoot(spec) {
    if (spec.startsWith(".") || spec.startsWith("/")) return null;
    spec = spec.replace(/^node:/, "");
    return spec.startsWith("@") ? spec.split("/").slice(0, 2).join("/") : spec.split("/")[0];
  }
  function addImport(spec, at) {
    const root_ = specRoot(spec);
    const res = ts.resolveModuleName(spec, sf.fileName, options, ts.sys).resolvedModule;
    if (res && inProject.has(path.resolve(res.resolvedFileName))) {
      const target = rel(res.resolvedFileName);
      refs.push({ src: modId, dst: `mod:${modQual(target)}`, kind: "imports", path: relpath, line: line(sf, at.getStart()), confidence: 1.0 });
    } else if (root_) {
      importedRoots.add(root_);
      refs.push({ src: modId, dst: `ext:${root_}`, kind: "imports", path: relpath, line: line(sf, at.getStart()), confidence: 0.9, ext: root_ });
    }
  }
  for (const st of sf.statements) {
    if (ts.isImportDeclaration(st) && ts.isStringLiteral(st.moduleSpecifier)) addImport(st.moduleSpecifier.text, st);
    else if (ts.isExportDeclaration(st) && st.moduleSpecifier && ts.isStringLiteral(st.moduleSpecifier)) {
      addImport(st.moduleSpecifier.text, st);
      // export { x } from "./y" / export * from "./y": the module republishes y's names
      const res = ts.resolveModuleName(st.moduleSpecifier.text, sf.fileName, options, ts.sys).resolvedModule;
      if (res && inProject.has(path.resolve(res.resolvedFileName))) {
        refs.push({ src: modId, dst: `mod:${modQual(rel(res.resolvedFileName))}`, kind: "reexports", path: relpath, line: line(sf, st.getStart()), confidence: 1.0 });
      }
    }
  }

  const stack = [];         // (holderId) of enclosing declared callable
  let cond = 0;
  const ownFlags = new Map(); // holder -> {throws, exits}

  function holder() { return stack.length ? stack[stack.length - 1] : modId; }
  function push(kind, dst, at, extra) {
    if (!dst) return;
    refs.push(Object.assign({ src: holder(), dst, kind, path: relpath, line: line(sf, at.getStart()), confidence: 1.0, conditional: cond > 0 }, extra || {}));
  }

  function resolveExprTarget(expr) {
    let e = expr;
    if (ts.isParenthesizedExpression(e) || ts.isNonNullExpression(e) || ts.isAsExpression(e)) e = e.expression;
    let sym = null;
    if (ts.isIdentifier(e)) sym = checker.getSymbolAtLocation(e);
    else if (ts.isPropertyAccessExpression(e)) sym = checker.getSymbolAtLocation(e.name);
    else if (ts.isElementAccessExpression(e) && e.argumentExpression && ts.isStringLiteralLike(e.argumentExpression)) {
      sym = checker.getSymbolAtLocation(e.argumentExpression);
    }
    return targetDecl(sym);
  }

  function callTarget(call) {
    // prefer the resolved signature's declaration (handles overloads and typed receivers)
    let sig = null;
    try { sig = checker.getResolvedSignature(call); } catch (e) { sig = null; }
    let t = null;
    if (sig && sig.declaration) {
      const d = sig.declaration;
      const id = idOfDecl(d) || (d.parent && ts.isVariableDeclaration(d.parent) ? idOfDecl(d.parent) : null);
      if (id) return { id, decl: d };
    }
    t = resolveExprTarget(call.expression);
    if (!t) return importedExternal(call.expression);
    let id = null;
    for (const d of t.decls) { id = idOfDecl(d) || (d.parent && ts.isVariableDeclaration(d.parent) ? idOfDecl(d.parent) : null); if (id) return { id, decl: d }; }
    const ext = extName(t.sym, t.decl) || importedExternal(call.expression, true);
    return ext && ext.ext ? ext : (typeof ext === "string" ? { ext, decl: t.decl } : null);
  }

  // `fs.readFileSync(...)` / `readFileSync(...)` where the import has no type
  // information: name it by the package that was imported.
  function importedExternal(expr) {
    let e = expr, last = null;
    while (ts.isPropertyAccessExpression(e)) { if (!last) last = e.name.text; e = e.expression; }
    if (!ts.isIdentifier(e)) return null;
    const sym = checker.getSymbolAtLocation(e);
    const d = sym && sym.declarations && sym.declarations[0];
    if (!d) return null;
    let imp = d;
    while (imp && !ts.isImportDeclaration(imp)) imp = imp.parent;
    if (!imp || !ts.isStringLiteral(imp.moduleSpecifier)) return null;
    const root_ = specRoot(imp.moduleSpecifier.text);
    if (!root_) return null;
    const name = last || (ts.isImportSpecifier(d) ? (d.propertyName || d.name).text : e.text);
    return { ext: `${root_}.${name}` };
  }

  function isWriteTarget(n) {
    const p = n.parent;
    if (!p) return null;
    if (ts.isBinaryExpression(p) && p.left === n) {
      const op = p.operatorToken.kind;
      if (op === ts.SyntaxKind.EqualsToken) return "=";
      if (op >= ts.SyntaxKind.FirstCompoundAssignment && op <= ts.SyntaxKind.LastCompoundAssignment) return ts.tokenToString(op);
    }
    if ((ts.isPrefixUnaryExpression(p) || ts.isPostfixUnaryExpression(p)) &&
        (p.operator === ts.SyntaxKind.PlusPlusToken || p.operator === ts.SyntaxKind.MinusMinusToken)) return "++";
    if (ts.isElementAccessExpression(p) && p.expression === n) {
      const pp = p.parent;
      if (pp && ts.isBinaryExpression(pp) && pp.left === p && pp.operatorToken.kind === ts.SyntaxKind.EqualsToken) return "[]=";
    }
    return null;
  }

  function declaresHolder(node) {
    return ts.isFunctionLike(node) && nodeIds.has(node) ? nodeIds.get(node)
      : (ts.isVariableDeclaration(node) && nodeIds.has(node) && node.initializer &&
         (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer)) ? nodeIds.get(node) : null);
  }

  function visit(node) {
    const h = declaresHolder(node);
    const isBranch = ts.isIfStatement(node) || ts.isConditionalExpression(node) || ts.isForStatement(node) ||
      ts.isForOfStatement(node) || ts.isForInStatement(node) || ts.isWhileStatement(node) || ts.isTryStatement(node) ||
      ts.isCatchClause(node) || ts.isSwitchStatement(node);
    if (h && ts.isVariableDeclaration(node)) { /* the arrow is visited as a child */ }
    if (h && !ts.isVariableDeclaration(node)) stack.push(h);
    if (isBranch) cond++;

    if (ts.isClassLike(node) && nodeIds.has(node)) {
      const cid = nodeIds.get(node);
      for (const hc of node.heritageClauses || []) {
        for (const t of hc.types) {
          const tgt = resolveExprTarget(t.expression);
          const id = tgt && tgt.decls.map(idOfDecl).find(Boolean);
          if (id) refs.push({ src: cid, dst: id, kind: "inherits", path: relpath, line: line(sf, t.getStart()), confidence: 1.0, implements: hc.token === ts.SyntaxKind.ImplementsKeyword });
        }
      }
      for (const dec of (ts.canHaveDecorators && ts.canHaveDecorators(node) ? ts.getDecorators(node) || [] : [])) {
        const tgt = resolveExprTarget(ts.isCallExpression(dec.expression) ? dec.expression.expression : dec.expression);
        const id = tgt && tgt.decls.map(idOfDecl).find(Boolean);
        if (id) refs.push({ src: id, dst: cid, kind: "decorates", path: relpath, line: line(sf, dec.getStart()), confidence: 1.0 });
      }
    }

    if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      const base = node.expression.getText();
      const key = ts.isPropertyAccessExpression(node) ? node.name.text
        : (node.argumentExpression && ts.isStringLiteralLike(node.argumentExpression) ? node.argumentExpression.text : null);
      if (key && /^(process\.env|import\.meta\.env)$/.test(base)) {
        const p = node.parent;
        const isWrite = p && ts.isBinaryExpression(p) && p.left === node && p.operatorToken.kind === ts.SyntaxKind.EqualsToken;
        let def = null;
        if (p && ts.isBinaryExpression(p) && p.left === node &&
            (p.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken || p.operatorToken.kind === ts.SyntaxKind.BarBarToken)) def = p.right.getText().slice(0, 60);
        refs.push({ src: holder(), dst: `ext:env.${key}`, kind: isWrite ? "writes" : "reads", path: relpath,
          line: line(sf, node.getStart()), confidence: 1.0, conditional: cond > 0, ext: `env.${key}`,
          env_var: key, default: def, required: false });
      }
    }
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) &&
        node.expression.getText() === "Deno.env.get" && node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])) {
      refs.push({ src: holder(), dst: `ext:env.${node.arguments[0].text}`, kind: "reads", path: relpath,
        line: line(sf, node.getStart()), confidence: 1.0, conditional: cond > 0, ext: `env.${node.arguments[0].text}`,
        env_var: node.arguments[0].text, default: null, required: false });
    }

    if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
      const isNew = ts.isNewExpression(node);
      const args = node.arguments ? node.arguments.length : 0;
      if (node.expression.kind === ts.SyntaxKind.ImportKeyword && node.arguments && node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])) {
        addImport(node.arguments[0].text, node);
      } else if (ts.isIdentifier(node.expression) && node.expression.text === "require" && node.arguments && node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])) {
        addImport(node.arguments[0].text, node);
      } else {
        const t = isNew ? resolveExprTarget(node.expression) : null;
        if (isNew && t) {
          const id = t.decls.map(idOfDecl).find(Boolean);
          if (id) push("instantiates", id, node, { args });
          else { const en = extName(t.sym, t.decl); if (en) push("instantiates", `ext:${en}`, node, { ext: en, args }); }
        } else if (!isNew) {
          const tg = callTarget(node);
          if (tg && tg.id) push("calls", tg.id, node, { args, callee: tg.id.split(".").pop() });
          else if (tg && tg.ext) push("calls", `ext:${tg.ext}`, node, { ext: tg.ext, args, callee: tg.ext.split(".").pop() });
          // mutation through a known mutator on a tracked container
          const ce = node.expression;
          if (ts.isPropertyAccessExpression(ce) && MUTATORS.has(ce.name.text)) {
            const recv = resolveExprTarget(ce.expression);
            const rid = recv && recv.decls.map(idOfDecl).find(Boolean);
            if (rid && /^(var|attr|iattr):/.test(rid)) push("mutates", rid, node, { method: ce.name.text, grows: GROWERS.has(ce.name.text) });
          }
          if (ts.isPropertyAccessExpression(ce) && ce.expression.kind === ts.SyntaxKind.Identifier &&
              ce.expression.text === "process" && ce.name.text === "exit") { const f = ownFlags.get(holder()) || {}; f.exits = true; ownFlags.set(holder(), f); }
        }
      }
    } else if (ts.isThrowStatement(node) && node.expression && ts.isNewExpression(node.expression)) {
      const t = resolveExprTarget(node.expression.expression);
      const id = t && t.decls.map(idOfDecl).find(Boolean);
      if (id) push("raises", id, node);
    } else if ((ts.isIdentifier(node) || ts.isPropertyAccessExpression(node)) &&
               !(node.parent && ts.isPropertyAccessExpression(node.parent) && node.parent.name === node && false)) {
      const parent = node.parent;
      const isCallee = parent && (ts.isCallExpression(parent) || ts.isNewExpression(parent)) && parent.expression === node;
      const isDeclName = parent && (ts.isVariableDeclaration(parent) || ts.isFunctionLike(parent) || ts.isClassLike(parent) ||
        ts.isPropertyDeclaration(parent) || ts.isParameter(parent) || ts.isImportSpecifier(parent) || ts.isPropertySignature(parent) ||
        ts.isImportClause(parent) || ts.isNamespaceImport(parent) || ts.isExportSpecifier(parent) ||
        ts.isInterfaceDeclaration(parent) || ts.isEnumDeclaration(parent) || ts.isTypeAliasDeclaration(parent) ||
        ts.isModuleDeclaration(parent) || ts.isEnumMember(parent) || ts.isMethodSignature(parent)) && parent.name === node;
      const inTypePosition = parent && (ts.isTypeNode(parent) || ts.isHeritageClause(parent) ||
        ts.isExpressionWithTypeArguments(parent) || ts.isTypeQueryNode(parent) || ts.isQualifiedName(parent));
      const isInnerAccess = ts.isIdentifier(node) && parent && ts.isPropertyAccessExpression(parent) && parent.name === node;
      if (!isCallee && !isDeclName && !isInnerAccess && !inTypePosition && !(ts.isPropertyAccessExpression(node) && node.parent && ts.isPropertyAccessExpression(node.parent) && node.parent.expression === node && false)) {
        const t = resolveExprTarget(node);
        const id = t && t.decls.map(idOfDecl).find(Boolean);
        if (id && /^(var|attr|iattr):/.test(id) && id !== holder()) {
          const w = isWriteTarget(node);
          if (w === "=") push("writes", id, node);
          else if (w) { push("mutates", id, node, { op: w, in_place: true }); push("reads", id, node); }
          else if (w === "[]=") push("mutates", id, node, { method: "__setitem__" });
          else push("reads", id, node);
        } else if (id && /^(fn|cls):/.test(id) && id !== holder() && !isCallee) {
          // a function or class passed around as a value: it can be called from anywhere
          if (!(parent && (ts.isExportSpecifier(parent) || ts.isImportSpecifier(parent)))) push("reads", id, node);
        }
      }
    }

    ts.forEachChild(node, visit);
    if (isBranch) cond--;
    if (h && !ts.isVariableDeclaration(node)) stack.pop();
  }
  visit(sf);
  return { importedRoots: [...importedRoots], flags: [...ownFlags.entries()] };
}

// ------------------------------------------------------------------ run
const infos = [];
for (const sf of program.getSourceFiles()) {
  if (!isProjectFile(sf)) continue;
  infos.push({ sf, info: declareFile(sf) });
}
const perFile = [];
for (const { sf, info } of infos) {
  const r = referencePass(sf, info);
  perFile.push({ path: info.relpath, module: info.qual, moduleId: info.modId, importedRoots: r.importedRoots, flags: r.flags, source: undefined });
}
const clean = decls.map((d) => { const c = Object.assign({}, d); delete c._heritage; return c; });
process.stdout.write(JSON.stringify({
  version: 1, tsVersion: ts.version, root,
  files: perFile.map((f) => Object.assign(f, { lines: fs.readFileSync(path.join(root, f.path), "utf8").split("\n").length })),
  decls: clean, refs,
}));
