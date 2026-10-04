"""A pure-Python Java parser: tokenizer and recursive descent, Java 1.2 through 21.

No dependencies, no JDK. It parses what Magellan needs and nothing it does not:

* every declaration (packages, imports, classes, interfaces, enums, records,
  annotation types, nested, local and anonymous classes, fields, methods,
  constructors, initializers), with modifiers, annotations, generics, ``throws``
  and ``permits``;
* method and initializer bodies down to expressions, so call sites, object
  creation, field access, assignments, switch statements and expressions, try/catch
  and lambdas are all visible with their positions.

Types are kept as written (``Map.Entry<K, V>[]``); nothing is resolved here. That is
:mod:`magellan_lite.polyglot.java.resolve`'s job.

Robustness matters more than strictness: this runs on code that does not compile yet.
A statement that fails to parse is skipped up to its ``;`` or closing brace and counted
in :attr:`CompilationUnit.errors`; a member that fails is skipped the same way. Only a
file whose top level cannot be parsed at all raises :class:`ParseError`.

Generics are handled by tokenizing every ``>`` on its own and gluing adjacent ones back
into shift operators inside expressions, so ``List<List<String>>`` never confuses the
tokenizer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
KEYWORDS = frozenset("""
abstract assert boolean break byte case catch char class const continue default do double
else enum extends final finally float for goto if implements import instanceof int interface
long native new package private protected public return short static strictfp super switch
synchronized this throw throws transient try void volatile while true false null
""".split())
PRIMITIVES = frozenset("boolean byte char short int long float double void".split())
MODIFIERS = frozenset("""public protected private static abstract final native synchronized
transient volatile strictfp default sealed non-sealed""".split())

_TOKEN_RE = re.compile(r"""
    (?P<ws>[ \t\f\r\n]+)
  | (?P<lcomment>//[^\n]*)
  | (?P<bcomment>/\*.*?\*/)
  | (?P<text>\"\"\"[ \t\f]*\r?\n(?:\\.|[^\\])*?\"\"\")
  | (?P<str>"(?:\\.|[^"\\\n])*")
  | (?P<chr>'(?:\\.|[^'\\\n])+')
  | (?P<num>0[xX][0-9a-fA-F_]*(?:\.[0-9a-fA-F_]*)?(?:[pP][+-]?[0-9_]+)?[lLfFdD]?
          |0[bB][01_]+[lL]?
          |(?:[0-9][0-9_]*\.?[0-9_]*|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9_]+)?[fFdDlL]?)
  | (?P<ident>(?:[^\W\d]|\$)[\w$]*)
  | (?P<op>>>>=|<<=|\.\.\.|->|::|\+\+|--|&&|\|\||==|!=|<=|<<|\+=|-=|\*=|/=|%=|&=|\|=|\^=
          |[{}()\[\];,.@=<>!~?:+\-*/&|^%])
""", re.S | re.X)


@dataclass(slots=True)
class Tok:
    kind: str            # ident kw num str chr text op eof
    text: str
    line: int
    col: int
    pos: int             # offset of the first character
    end: int             # offset past the last character
    doc: str = ""        # the /** ... */ comment right before this token, if any


class ParseError(Exception):
    def __init__(self, msg: str, tok: Tok | None = None) -> None:
        where = f" at line {tok.line}:{tok.col}" if tok is not None else ""
        super().__init__(msg + where)
        self.tok = tok


def tokenize(src: str) -> list[Tok]:
    """Tokens with 1-based lines and columns. Comments are dropped; javadoc is kept on
    the token that follows it. Unknown characters are skipped, not fatal."""
    out: list[Tok] = []
    line, line_start = 1, 0
    pos, n = 0, len(src)
    doc = ""
    if src.startswith("﻿"):
        pos = 1
    while pos < n:
        m = _TOKEN_RE.match(src, pos)
        if m is None:
            # a stray character (a unicode escape in an odd place, a `#` in a template)
            if src[pos] == "\n":
                line += 1
                line_start = pos + 1
            pos += 1
            continue
        kind = m.lastgroup or ""
        text = m.group()
        end = m.end()
        if kind in ("ws", "lcomment", "bcomment", "text"):
            if kind == "bcomment" and text.startswith("/**") and text != "/**/":
                doc = text
            if kind == "text":
                out.append(Tok("str", text, line, pos - line_start + 1, pos, end, doc))
                doc = ""
            nl = text.count("\n")
            if nl:
                line += nl
                line_start = pos + text.rfind("\n") + 1
            pos = end
            continue
        if kind == "ident" and text in KEYWORDS:
            kind = "kw"
        out.append(Tok(kind, text, line, pos - line_start + 1, pos, end, doc))
        doc = ""
        pos = end
    out.append(Tok("eof", "", line, pos - line_start + 1, pos, pos))
    return out


# --------------------------------------------------------------------------
# syntax tree
# --------------------------------------------------------------------------
@dataclass
class TypeRef:
    name: str                                    # dotted, as written: "Map.Entry"
    args: list["TypeRef"] = field(default_factory=list)
    dims: int = 0
    wildcard: str = ""                           # "?", "? extends", "? super"
    varargs: bool = False

    @property
    def primitive(self) -> bool:
        return self.name in PRIMITIVES

    def text(self) -> str:
        if self.wildcard:
            return self.wildcard + ((" " + self.args[0].text()) if self.args else "")
        s = self.name
        if self.args:
            s += "<" + ",".join(a.text() for a in self.args) + ">"
        s += "[]" * self.dims
        return s + ("..." if self.varargs else "")

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.text()


@dataclass
class Annotation:
    name: str
    args: str = ""                               # raw text between the parentheses
    line: int = 0

    def text(self) -> str:
        return f"{self.name}({self.args})" if self.args else self.name


@dataclass
class Param:
    type: TypeRef
    name: str
    varargs: bool = False
    final: bool = False
    annotations: list[Annotation] = field(default_factory=list)
    line: int = 0


@dataclass
class Node:
    """Base for statements and expressions: a position."""
    line: int = 0
    col: int = 0


# ---- expressions ---------------------------------------------------------
@dataclass
class Name(Node):
    name: str = ""


@dataclass
class Literal(Node):
    kind: str = ""                               # int long float double char str bool null text
    value: str = ""


@dataclass
class FieldAccess(Node):
    target: Any = None
    name: str = ""


@dataclass
class MethodCall(Node):
    target: Any = None                           # None: unqualified call
    name: str = ""                               # "this"/"super" for explicit ctor calls
    args: list = field(default_factory=list)


@dataclass
class New(Node):
    type: TypeRef | None = None
    args: list = field(default_factory=list)
    body: "TypeDecl | None" = None               # anonymous class
    outer: Any = None                            # outer.new Inner()


@dataclass
class NewArray(Node):
    type: TypeRef | None = None
    dims: list = field(default_factory=list)
    init: "ArrayInit | None" = None


@dataclass
class ArrayInit(Node):
    items: list = field(default_factory=list)


@dataclass
class This(Node):
    qualifier: str = ""


@dataclass
class Super(Node):
    qualifier: str = ""


@dataclass
class Cast(Node):
    type: TypeRef | None = None
    expr: Any = None


@dataclass
class InstanceOf(Node):
    expr: Any = None
    type: TypeRef | None = None
    binding: str = ""
    pattern: Any = None


@dataclass
class Binary(Node):
    op: str = ""
    left: Any = None
    right: Any = None


@dataclass
class Unary(Node):
    op: str = ""
    expr: Any = None
    postfix: bool = False


@dataclass
class Assign(Node):
    op: str = "="
    target: Any = None
    value: Any = None


@dataclass
class Conditional(Node):
    cond: Any = None
    then: Any = None
    other: Any = None


@dataclass
class Lambda(Node):
    params: list[Param] = field(default_factory=list)
    body: Any = None                             # expression or Block


@dataclass
class MethodRef(Node):
    target: Any = None                           # expression, or TypeRef
    name: str = ""                               # "new" for constructor refs


@dataclass
class ArrayAccess(Node):
    array: Any = None
    index: Any = None


@dataclass
class ClassLit(Node):
    type: TypeRef | None = None


@dataclass
class SwitchCase(Node):
    labels: list = field(default_factory=list)   # expressions or Pattern
    default: bool = False
    guard: Any = None
    arrow: bool = False
    body: list = field(default_factory=list)     # statements (arrow bodies wrapped)


@dataclass
class Switch(Node):
    selector: Any = None
    cases: list[SwitchCase] = field(default_factory=list)
    is_expr: bool = False
    end_line: int = 0


@dataclass
class Pattern(Node):
    type: TypeRef | None = None
    binding: str = ""
    subpatterns: list = field(default_factory=list)   # record deconstruction


# ---- statements ----------------------------------------------------------
@dataclass
class Block(Node):
    stmts: list = field(default_factory=list)
    end_line: int = 0


@dataclass
class LocalVar(Node):
    type: TypeRef | None = None                  # None for `var`
    names: list[tuple[str, int, Any]] = field(default_factory=list)   # (name, dims, init)
    final: bool = False


@dataclass
class LocalClass(Node):
    decl: "TypeDecl | None" = None


@dataclass
class ExprStmt(Node):
    expr: Any = None


@dataclass
class If(Node):
    cond: Any = None
    then: Any = None
    other: Any = None


@dataclass
class Loop(Node):
    kind: str = ""                               # while do for foreach
    init: list = field(default_factory=list)
    cond: Any = None
    update: list = field(default_factory=list)
    var: "LocalVar | None" = None                # foreach variable
    iter: Any = None
    body: Any = None


@dataclass
class Return(Node):
    expr: Any = None


@dataclass
class Throw(Node):
    expr: Any = None


@dataclass
class Yield(Node):
    expr: Any = None


@dataclass
class Catch(Node):
    types: list[TypeRef] = field(default_factory=list)
    name: str = ""
    body: Block | None = None


@dataclass
class Try(Node):
    resources: list = field(default_factory=list)  # LocalVar or expression
    body: Block | None = None
    catches: list[Catch] = field(default_factory=list)
    final: Block | None = None


@dataclass
class Sync(Node):
    lock: Any = None
    body: Block | None = None


@dataclass
class Labeled(Node):
    label: str = ""
    stmt: Any = None


@dataclass
class Jump(Node):
    kind: str = ""                               # break continue
    label: str = ""


@dataclass
class Assert(Node):
    cond: Any = None
    msg: Any = None


@dataclass
class Unparsed(Node):
    """A statement the parser could not read; its tokens are kept for a crude scan."""
    toks: list = field(default_factory=list)


# ---- declarations --------------------------------------------------------
@dataclass
class Import:
    name: str
    static: bool = False
    wildcard: bool = False
    line: int = 0


@dataclass
class Member:
    line: int = 0
    end_line: int = 0
    col: int = 0
    modifiers: set[str] = field(default_factory=set)
    annotations: list[Annotation] = field(default_factory=list)
    doc: str = ""


@dataclass
class FieldDecl(Member):
    type: TypeRef | None = None
    names: list[tuple[str, int, Any, int]] = field(default_factory=list)  # name, dims, init, line
    init_text: dict[str, str] = field(default_factory=dict)
    init_toks: dict[str, str] = field(default_factory=dict)


@dataclass
class MethodDecl(Member):
    name: str = ""
    ctor: bool = False
    compact: bool = False                        # record compact canonical constructor
    type_params: list[str] = field(default_factory=list)
    type_params_text: str = ""
    ret: TypeRef | None = None
    params: list[Param] = field(default_factory=list)
    throws: list[TypeRef] = field(default_factory=list)
    body: Block | None = None
    body_text: str = ""                          # normalized tokens, for hashing
    default_value: str = ""                      # annotation element default
    unparsed: int = 0                            # statements skipped by recovery


@dataclass
class Initializer(Member):
    static: bool = False
    body: Block | None = None
    body_text: str = ""


@dataclass
class EnumConst:
    name: str
    args: list = field(default_factory=list)
    body: "TypeDecl | None" = None
    line: int = 0
    annotations: list[Annotation] = field(default_factory=list)
    doc: str = ""
    text: str = ""


@dataclass
class TypeDecl(Member):
    kind: str = "class"                          # class interface enum record annotation
    name: str = ""
    type_params: list[str] = field(default_factory=list)
    type_params_text: str = ""
    extends: list[TypeRef] = field(default_factory=list)
    implements: list[TypeRef] = field(default_factory=list)
    permits: list[TypeRef] = field(default_factory=list)
    components: list[Param] = field(default_factory=list)
    constants: list[EnumConst] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    anonymous: bool = False
    local: bool = False


@dataclass
class CompilationUnit:
    package: str = ""
    imports: list[Import] = field(default_factory=list)
    types: list[TypeDecl] = field(default_factory=list)
    errors: int = 0
    lines: int = 0
    module_info: bool = False
    package_annotations: list[Annotation] = field(default_factory=list)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
_ASSIGN_OPS = frozenset("= += -= *= /= %= &= |= ^= <<= >>= >>>=".split())
_BINARY_PREC = {
    "||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6,
    "<": 7, ">": 7, "<=": 7, ">=": 7, "instanceof": 7,
    "<<": 8, ">>": 8, ">>>": 8, "+": 9, "-": 9, "*": 10, "/": 10, "%": 10,
}
_LITERAL_KINDS = {"num", "str", "chr"}
_CAST_FOLLOW_KW = frozenset({"this", "super", "new", "true", "false", "null", "switch"}
                            | PRIMITIVES)


class Parser:
    def __init__(self, src: str) -> None:
        self.src = src
        self.toks = tokenize(src)
        self.i = 0
        self.errors = 0
        self.no_lambda = 0

    # ---- token helpers ---------------------------------------------------
    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Tok:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, *texts: str) -> bool:
        t = self.toks[self.i]
        return t.kind != "str" and t.kind != "chr" and t.text in texts

    def at_ident(self) -> bool:
        return self.toks[self.i].kind == "ident"

    def next(self) -> Tok:
        t = self.toks[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    def accept(self, text: str) -> bool:
        if self.at(text):
            self.i += 1
            return True
        return False

    def expect(self, text: str) -> Tok:
        if not self.at(text):
            raise ParseError(f"expected '{text}', found '{self.tok.text or 'EOF'}'", self.tok)
        return self.next()

    def ident(self) -> str:
        t = self.tok
        if t.kind != "ident":
            raise ParseError(f"expected identifier, found '{t.text or 'EOF'}'", t)
        self.i += 1
        return t.text

    def adjacent(self, k: int) -> bool:
        """Is token i+k glued to token i+k-1 (no whitespace)?"""
        a, b = self.peek(k - 1), self.peek(k)
        return a.end == b.pos

    def skip_balanced(self) -> None:
        """Skip one bracketed group starting at the current opener."""
        pairs = {"(": ")", "{": "}", "[": "]"}
        close = pairs[self.tok.text]
        depth = 0
        opener = self.tok.text
        while self.tok.kind != "eof":
            t = self.next()
            if t.kind == "op" and t.text == opener:
                depth += 1
            elif t.kind == "op" and t.text == close:
                depth -= 1
                if depth == 0:
                    return
        raise ParseError("unbalanced brackets", self.tok)

    def text_between(self, a: int, b: int) -> str:
        return " ".join(t.text for t in self.toks[a:b])

    # ---- compilation unit ------------------------------------------------
    def parse(self) -> CompilationUnit:
        cu = CompilationUnit(lines=self.src.count("\n") + 1)
        save = self.i
        annos = self.annotations()
        if self.at("package"):
            self.next()
            cu.package = self.qualified()
            self.expect(";")
            cu.package_annotations = annos
        else:
            self.i = save
        while self.at("import"):
            line = self.next().line
            static = self.accept("static")
            parts = [self.ident()]
            wildcard = False
            while self.accept("."):
                if self.accept("*"):
                    wildcard = True
                    break
                parts.append(self.ident())
            self.expect(";")
            cu.imports.append(Import(".".join(parts), static, wildcard, line))
            while self.accept(";"):
                pass
        while self.tok.kind != "eof":
            if self.accept(";"):
                continue
            if self.tok.kind == "ident" and self.tok.text in ("module", "open") and (
                    self.peek().kind == "ident" or self.peek().text == "module"):
                cu.module_info = True
                break
            start = self.i
            try:
                mods, annos, doc = self.modifiers()
                if not self.at_type_keyword():
                    raise ParseError(f"expected a type declaration, found '{self.tok.text}'",
                                     self.tok)
                cu.types.append(self.type_decl(mods, annos, doc, start))
            except ParseError:
                if self.i == start:
                    raise
                self.errors += 1
                self.recover()
        cu.errors = self.errors
        return cu

    def qualified(self) -> str:
        parts = [self.ident()]
        while self.at(".") and self.peek().kind == "ident":
            self.next()
            parts.append(self.ident())
        return ".".join(parts)

    # ---- modifiers and annotations ---------------------------------------
    def annotations(self) -> list[Annotation]:
        out = []
        while self.at("@") and not (self.peek().text == "interface"):
            out.append(self.annotation())
        return out

    def annotation(self) -> Annotation:
        line = self.expect("@").line
        name = self.qualified()
        args = ""
        if self.at("("):
            a = self.i
            self.skip_balanced()
            args = self.text_between(a + 1, self.i - 1)
        return Annotation(name, args, line)

    def modifiers(self) -> tuple[set[str], list[Annotation], str]:
        mods: set[str] = set()
        annos: list[Annotation] = []
        doc = self.tok.doc
        while True:
            t = self.tok
            if t.kind == "op" and t.text == "@" and self.peek().text != "interface":
                annos.append(self.annotation())
            elif t.kind == "kw" and t.text in MODIFIERS and t.text != "default":
                mods.add(self.next().text)
            elif t.kind == "kw" and t.text == "default" and self.peek().text not in (":", "->"):
                mods.add(self.next().text)
            elif t.kind == "ident" and t.text == "sealed" and self._modifier_context():
                mods.add(self.next().text)
            elif (t.kind == "ident" and t.text == "non" and self.peek().text == "-"
                  and self.peek(2).text == "sealed"):
                self.i += 3
                mods.add("non-sealed")
            else:
                break
        return mods, annos, doc

    def _modifier_context(self) -> bool:
        nxt = self.peek()
        return nxt.text in MODIFIERS or nxt.text in ("class", "interface", "abstract", "@") \
            or (nxt.kind == "ident" and nxt.text in ("record", "non", "sealed"))

    def at_type_keyword(self) -> bool:
        t = self.tok
        if t.kind == "kw" and t.text in ("class", "interface", "enum"):
            return True
        if t.text == "@" and self.peek().text == "interface":
            return True
        return (t.kind == "ident" and t.text == "record" and self.peek().kind == "ident"
                and self.peek(2).text in ("(", "<"))

    # ---- types -----------------------------------------------------------
    def type_ref(self, allow_var: bool = False) -> TypeRef:
        self.annotations()
        t = self.tok
        if t.kind == "kw" and t.text in PRIMITIVES:
            self.next()
            ref = TypeRef(t.text)
        elif t.kind == "ident":
            parts = [self.ident()]
            args: list[TypeRef] = []
            if self.at("<"):
                args = self.type_args()
            while self.at(".") and (self.peek().kind == "ident" or self.peek().text == "@"):
                self.next()
                self.annotations()
                parts.append(self.ident())
                if self.at("<"):
                    args = self.type_args()      # the innermost arguments win
            ref = TypeRef(".".join(parts), args)
        elif t.text == "?":
            self.next()
            if self.at("extends", "super"):
                kind = self.next().text
                return TypeRef("?", [self.type_ref()], wildcard=f"? {kind}")
            return TypeRef("?", wildcard="?")
        else:
            raise ParseError(f"expected a type, found '{t.text or 'EOF'}'", t)
        while True:
            save = self.i
            self.annotations()
            if self.at("[") and self.peek().text == "]":
                self.i += 2
                ref.dims += 1
            else:
                self.i = save
                break
        return ref

    def type_args(self) -> list[TypeRef]:
        self.expect("<")
        out: list[TypeRef] = []
        if self.accept(">"):
            return out                           # diamond
        while True:
            out.append(self.type_ref())
            while self.accept("&"):
                self.type_ref()
            if self.accept(","):
                continue
            self.expect(">")
            return out

    def type_params(self) -> tuple[list[str], str]:
        start = self.i
        self.expect("<")
        names = []
        while True:
            self.annotations()
            names.append(self.ident())
            if self.accept("extends"):
                self.type_ref()
                while self.accept("&"):
                    self.type_ref()
            if self.accept(","):
                continue
            self.expect(">")
            return names, self.text_between(start, self.i)

    def type_list(self) -> list[TypeRef]:
        out = [self.type_ref()]
        while self.accept(","):
            out.append(self.type_ref())
        return out

    # ---- type declarations -----------------------------------------------
    def type_decl(self, mods: set[str], annos: list[Annotation], doc: str,
                  start: int) -> TypeDecl:
        first = self.toks[start]
        t = self.next()
        if t.text == "@":
            self.expect("interface")
            kind = "annotation"
        else:
            kind = t.text
        decl = TypeDecl(line=first.line, col=first.col, modifiers=mods, annotations=annos,
                        doc=doc, kind=kind)
        decl.name = self.ident()
        if self.at("<"):
            decl.type_params, decl.type_params_text = self.type_params()
        if kind == "record":
            decl.components = self.params()
        while True:
            if self.accept("extends"):
                lst = self.type_list()
                if kind == "interface":
                    decl.extends += lst
                else:
                    decl.extends += lst[:1]
            elif self.accept("implements"):
                decl.implements += self.type_list()
            elif self.tok.kind == "ident" and self.tok.text == "permits":
                self.next()
                decl.permits += self.type_list()
            else:
                break
        self.class_body(decl)
        return decl

    def class_body(self, decl: TypeDecl) -> None:
        self.expect("{")
        if decl.kind == "enum":
            self.enum_constants(decl)
        while not self.at("}"):
            if self.tok.kind == "eof":
                raise ParseError("unterminated class body", self.tok)
            if self.accept(";"):
                continue
            start = self.i
            try:
                m = self.member(decl)
                if m is not None:
                    decl.members.append(m)
            except ParseError:
                self.errors += 1
                self.i = start
                self.recover()
        decl.end_line = self.next().line

    def recover(self) -> None:
        """Skip to the end of a broken member or statement: its `;` or its balanced body,
        stopping before the enclosing body's closer."""
        start = self.i
        depth = 0
        while self.tok.kind != "eof":
            t = self.tok
            if t.kind == "op" and t.text in "({[":
                depth += 1
            elif t.kind == "op" and t.text in ")]}":
                if depth == 0:
                    if self.i == start:
                        self.next()
                    return                       # the enclosing body's closer
                depth -= 1
                if depth == 0 and t.text == "}":
                    self.next()
                    return
            elif t.kind == "op" and t.text == ";" and depth == 0:
                self.next()
                return
            self.next()

    def enum_constants(self, decl: TypeDecl) -> None:
        while not self.at(";", "}"):
            doc = self.tok.doc
            start = self.i
            annos = self.annotations()
            line = self.tok.line
            name = self.ident()
            const = EnumConst(name, line=line, annotations=annos, doc=doc)
            if self.at("("):
                const.args = self.arguments()
            if self.at("{"):
                body = TypeDecl(line=self.tok.line, kind="class", name=name, anonymous=True)
                self.class_body(body)
                const.body = body
            const.text = self.text_between(start, self.i)
            decl.constants.append(const)
            if not self.accept(","):
                break
        self.accept(";")

    def member(self, owner: TypeDecl) -> Member | None:
        doc = self.tok.doc
        first = self.tok
        if self.at("{") or (self.at("static") and self.peek().text == "{"):
            static = self.accept("static")
            a = self.i
            body = self.block()
            return Initializer(line=first.line, end_line=body.end_line, col=first.col,
                               static=static, body=body,
                               body_text=self.text_between(a, self.i))
        start = self.i
        mods, annos, doc2 = self.modifiers()
        doc = doc or doc2
        if self.at_type_keyword():
            d = self.type_decl(mods, annos, doc, start)
            return d
        tparams: list[str] = []
        tptext = ""
        if self.at("<"):
            tparams, tptext = self.type_params()
        # constructor, or record compact constructor
        if self.at_ident() and self.tok.text == owner.name and self.peek().text in ("(", "{"):
            name_tok = self.next()
            m = MethodDecl(line=first.line, col=first.col, modifiers=mods, annotations=annos,
                           doc=doc, name="<init>", ctor=True, type_params=tparams,
                           type_params_text=tptext)
            if self.at("{"):
                m.compact = True
                m.params = list(owner.components)
            else:
                m.params = self.params()
            if self.accept("throws"):
                m.throws = self.type_list()
            self.method_body(m)
            del name_tok
            return m
        ret = self.type_ref()
        if self.at_ident() and self.peek().text == "(":
            name = self.ident()
            m = MethodDecl(line=first.line, col=first.col, modifiers=mods, annotations=annos,
                           doc=doc, name=name, type_params=tparams, type_params_text=tptext,
                           ret=ret)
            m.params = self.params()
            while self.at("[") and self.peek().text == "]":   # int foo()[] (legacy)
                self.i += 2
                ret.dims += 1
            if self.accept("throws"):
                m.throws = self.type_list()
            if self.accept("default"):
                a = self.i
                self.element_value_skip()
                m.default_value = self.text_between(a, self.i)
            self.method_body(m)
            return m
        # field
        f = FieldDecl(line=first.line, col=first.col, modifiers=mods, annotations=annos,
                      doc=doc, type=ret)
        while True:
            nline = self.tok.line
            name = self.ident()
            dims = 0
            while self.at("[") and self.peek().text == "]":
                self.i += 2
                dims += 1
            init = None
            if self.accept("="):
                a = self.i
                init = self.var_init()
                f.init_text[name] = self.src[self.toks[a].pos:self.toks[self.i - 1].end]
                f.init_toks[name] = self.text_between(a, self.i)
            f.names.append((name, dims, init, nline))
            if not self.accept(","):
                break
        f.end_line = self.expect(";").line
        return f

    def element_value_skip(self) -> None:
        while not self.at(";") and self.tok.kind != "eof":
            if self.at("(", "{", "["):
                self.skip_balanced()
            else:
                self.next()

    def method_body(self, m: MethodDecl) -> None:
        if self.at("{"):
            a = self.i
            errs = self.errors
            m.body = self.block()
            m.unparsed = self.errors - errs
            m.body_text = self.text_between(a, self.i)
            m.end_line = m.body.end_line
        else:
            m.end_line = self.expect(";").line

    def params(self) -> list[Param]:
        self.expect("(")
        out: list[Param] = []
        if self.accept(")"):
            return out
        while True:
            mods, annos, _ = self.modifiers()
            line = self.tok.line
            ty = self.type_ref()
            varargs = False
            self.annotations()
            if self.accept("..."):
                varargs = True
            if self.at("this"):                  # receiver parameter: not a real one
                self.next()
            elif self.at_ident() and self.peek().text == "." and self.peek(2).text == "this":
                self.i += 3
            else:
                name = self.ident()
                while self.at("[") and self.peek().text == "]":
                    self.i += 2
                    ty.dims += 1
                ty.varargs = varargs
                out.append(Param(ty, name, varargs, "final" in mods, annos, line))
            if self.accept(","):
                continue
            self.expect(")")
            return out

    # ---- statements ------------------------------------------------------
    def block(self) -> Block:
        b = Block(line=self.tok.line, col=self.tok.col)
        self.expect("{")
        while not self.at("}"):
            if self.tok.kind == "eof":
                raise ParseError("unterminated block", self.tok)
            start = self.i
            try:
                b.stmts.append(self.block_statement())
            except ParseError:
                self.errors += 1
                self.i = start
                self.recover()
                b.stmts.append(Unparsed(line=self.toks[start].line, col=self.toks[start].col,
                                        toks=self.toks[start:self.i]))
        b.end_line = self.next().line
        return b

    def block_statement(self) -> Any:
        t = self.tok
        # local class / record / interface / enum
        save = self.i
        if t.kind == "kw" and t.text in ("class", "interface", "enum", "abstract", "final",
                                          "static", "strictfp") or t.text == "@" \
                or (t.kind == "ident" and t.text in ("record", "sealed", "non")):
            mods, annos, doc = self.modifiers()
            if self.at_type_keyword():
                d = self.type_decl(mods, annos, doc, save)
                d.local = True
                return LocalClass(line=t.line, col=t.col, decl=d)
            self.i = save
        lv = self.try_local_var()
        if lv is not None:
            self.expect(";")
            return lv
        return self.statement()

    def try_local_var(self, foreach: bool = False) -> LocalVar | None:
        """Speculatively read `[final] Type name ...`; restore and return None if it is not."""
        save = self.i
        t = self.tok
        if not (t.kind == "ident" or t.text in PRIMITIVES or t.text in ("final", "@")):
            return None
        if t.kind == "kw" and t.text not in PRIMITIVES and t.text != "final":
            return None
        try:
            mods, _, _ = self.modifiers()
            line, col = self.tok.line, self.tok.col
            if self.tok.kind == "ident" and self.tok.text == "var" and self.peek().kind == "ident":
                self.next()
                ty = None
            else:
                ty = self.type_ref()
            if not self.at_ident():
                raise ParseError("not a declaration", self.tok)
            follow = self.peek().text
            if follow not in ("=", ";", ",", "[", ":") or (follow == ":" and not foreach):
                raise ParseError("not a declaration", self.tok)
        except ParseError:
            self.i = save
            return None
        lv = LocalVar(line=line, col=col, type=ty, final="final" in mods)
        while True:
            name = self.ident()
            dims = 0
            while self.at("[") and self.peek().text == "]":
                self.i += 2
                dims += 1
            init = None
            if not foreach and self.accept("="):
                init = self.var_init()
            lv.names.append((name, dims, init))
            if foreach or not self.accept(","):
                break
        return lv

    def var_init(self) -> Any:
        if self.at("{"):
            return self.array_init()
        return self.expression()

    def array_init(self) -> ArrayInit:
        a = ArrayInit(line=self.tok.line, col=self.tok.col)
        self.expect("{")
        while not self.at("}"):
            a.items.append(self.var_init())
            if not self.accept(","):
                break
        self.expect("}")
        return a

    def statement(self) -> Any:
        t = self.tok
        line, col = t.line, t.col
        if t.kind == "op":
            if t.text == "{":
                return self.block()
            if t.text == ";":
                self.next()
                return Block(line=line, col=col, end_line=line)
        if t.kind == "kw":
            k = t.text
            if k == "if":
                self.next()
                cond = self.par_expr()
                then = self.statement()
                other = self.statement() if self.accept("else") else None
                return If(line=line, col=col, cond=cond, then=then, other=other)
            if k == "while":
                self.next()
                cond = self.par_expr()
                return Loop(line=line, col=col, kind="while", cond=cond, body=self.statement())
            if k == "do":
                self.next()
                body = self.statement()
                self.expect("while")
                cond = self.par_expr()
                self.expect(";")
                return Loop(line=line, col=col, kind="do", cond=cond, body=body)
            if k == "for":
                return self.for_statement()
            if k == "try":
                return self.try_statement()
            if k == "switch":
                sw = self.switch()
                if self.at(".", "[", "::"):      # a switch expression used as a primary
                    expr = self.postfix(sw)
                    expr = self.binary_rest(expr, 0)
                    self.expect(";")
                    return ExprStmt(line=line, col=col, expr=expr)
                return sw
            if k == "return":
                self.next()
                e = None if self.at(";") else self.expression()
                self.expect(";")
                return Return(line=line, col=col, expr=e)
            if k == "throw":
                self.next()
                e = self.expression()
                self.expect(";")
                return Throw(line=line, col=col, expr=e)
            if k in ("break", "continue"):
                self.next()
                label = self.ident() if self.at_ident() else ""
                self.expect(";")
                return Jump(line=line, col=col, kind=k, label=label)
            if k == "synchronized":
                self.next()
                lock = self.par_expr()
                return Sync(line=line, col=col, lock=lock, body=self.block())
            if k == "assert":
                self.next()
                cond = self.expression()
                msg = self.expression() if self.accept(":") else None
                self.expect(";")
                return Assert(line=line, col=col, cond=cond, msg=msg)
        if t.kind == "ident":
            if self.peek().text == ":" and self.peek().kind == "op":
                self.i += 2
                return Labeled(line=line, col=col, label=t.text, stmt=self.statement())
            if t.text == "yield" and self.peek().text not in ("=", ".", "[", "++", "--", "(") \
                    or (t.text == "yield" and self.peek().text == "("
                        and self._yield_paren()):
                self.next()
                e = self.expression()
                self.expect(";")
                return Yield(line=line, col=col, expr=e)
        e = self.expression()
        self.expect(";")
        return ExprStmt(line=line, col=col, expr=e)

    def _yield_paren(self) -> bool:
        """`yield (x);` is a yield statement; `yield();` is not legal Java since 14."""
        return self.peek(2).text != ")"

    def par_expr(self) -> Any:
        self.expect("(")
        e = self.expression()
        self.expect(")")
        return e

    def for_statement(self) -> Loop:
        t = self.expect("for")
        self.expect("(")
        before = self.i
        lv = self.try_local_var(foreach=True)
        if lv is not None and self.accept(":"):
            it = self.expression()
            self.expect(")")
            return Loop(line=t.line, col=t.col, kind="foreach", var=lv, iter=it,
                        body=self.statement())
        init: list = []
        if lv is not None:
            # it was `Type a = ..., b = ...`; re-read as a normal declaration
            self.i = before
            lv = self.try_local_var()
            if lv is None:
                raise ParseError("bad for-init", self.tok)
            init.append(lv)
        elif not self.at(";"):
            init.append(self.expression())
            while self.accept(","):
                init.append(self.expression())
        self.expect(";")
        cond = None if self.at(";") else self.expression()
        self.expect(";")
        update: list = []
        if not self.at(")"):
            update.append(self.expression())
            while self.accept(","):
                update.append(self.expression())
        self.expect(")")
        return Loop(line=t.line, col=t.col, kind="for", init=init, cond=cond, update=update,
                    body=self.statement())

    def try_statement(self) -> Try:
        t = self.expect("try")
        tr = Try(line=t.line, col=t.col)
        if self.accept("("):
            while not self.at(")"):
                lv = self.try_local_var()
                if lv is not None:
                    tr.resources.append(lv)
                else:
                    tr.resources.append(self.expression())
                if not self.accept(";"):
                    break
            self.expect(")")
        tr.body = self.block()
        while self.at("catch"):
            c = self.next()
            self.expect("(")
            self.modifiers()
            types = [self.type_ref()]
            while self.accept("|"):
                types.append(self.type_ref())
            name = self.ident()
            self.expect(")")
            tr.catches.append(Catch(line=c.line, col=c.col, types=types, name=name,
                                    body=self.block()))
        if self.accept("finally"):
            tr.final = self.block()
        return tr

    def switch(self) -> Switch:
        t = self.expect("switch")
        sw = Switch(line=t.line, col=t.col)
        sw.selector = self.par_expr()
        self.expect("{")
        while not self.at("}"):
            c = SwitchCase(line=self.tok.line, col=self.tok.col)
            if self.accept("default"):
                c.default = True
            else:
                self.expect("case")
                self.no_lambda += 1
                try:
                    while True:
                        if self.accept("default"):
                            c.default = True
                        else:
                            c.labels.append(self.case_label())
                        if not self.accept(","):
                            break
                    if self.tok.kind == "ident" and self.tok.text == "when":
                        self.next()
                        c.guard = self.expression()
                finally:
                    self.no_lambda -= 1
            if self.accept("->"):
                c.arrow = True
                if self.at("{"):
                    c.body = [self.block()]
                elif self.at("throw"):
                    c.body = [self.statement()]
                else:
                    e = self.expression()
                    self.expect(";")
                    c.body = [ExprStmt(line=e.line if hasattr(e, "line") else c.line,
                                       col=getattr(e, "col", 0), expr=e)]
            else:
                if not self.accept(":"):
                    raise ParseError("expected ':' or '->' after case label", self.tok)
                while not self.at("case", "default", "}") or (
                        self.at("default") and self.peek().text not in (":", "->")):
                    if self.tok.kind == "eof":
                        raise ParseError("unterminated switch", self.tok)
                    start = self.i
                    try:
                        c.body.append(self.block_statement())
                    except ParseError:
                        self.errors += 1
                        self.i = start
                        self.recover()
                        c.body.append(Unparsed(line=self.toks[start].line,
                                               toks=self.toks[start:self.i]))
            sw.cases.append(c)
        sw.end_line = self.next().line
        return sw

    def case_label(self) -> Any:
        """A constant expression, an enum constant, `null`, or a (record) pattern."""
        save = self.i
        pat = self.try_pattern()
        if pat is not None:
            return pat
        self.i = save
        return self.ternary()

    def try_pattern(self) -> Pattern | None:
        save = self.i
        try:
            self.modifiers()
            line, col = self.tok.line, self.tok.col
            if not (self.at_ident() or self.tok.text in PRIMITIVES):
                raise ParseError("no pattern", self.tok)
            ty = self.type_ref()
            if self.at("("):                     # record pattern
                self.next()
                subs = []
                while not self.at(")"):
                    sub = self.try_pattern()
                    if sub is None:
                        raise ParseError("bad record pattern", self.tok)
                    subs.append(sub)
                    if not self.accept(","):
                        break
                self.expect(")")
                binding = self.ident() if self.at_ident() and self.tok.text != "when" else ""
                return Pattern(line=line, col=col, type=ty, binding=binding, subpatterns=subs)
            if self.at_ident() and self.tok.text != "when":
                return Pattern(line=line, col=col, type=ty, binding=self.ident())
            if ty.name == "var":
                raise ParseError("no pattern", self.tok)
            raise ParseError("no pattern", self.tok)
        except ParseError:
            self.i = save
            return None

    # ---- expressions -----------------------------------------------------
    def expression(self) -> Any:
        if self.no_lambda == 0:
            lam = self.try_lambda()
            if lam is not None:
                return lam
        left = self.ternary()
        op, width = self.assign_op()
        if op:
            t = self.tok
            self.i += width
            value = self.expression() if not self.at("{") else self.array_init()
            return Assign(line=getattr(left, "line", t.line), col=getattr(left, "col", t.col),
                          op=op, target=left, value=value)
        return left

    def assign_op(self) -> tuple[str, int]:
        t = self.tok
        if t.kind != "op":
            return "", 0
        if t.text in _ASSIGN_OPS:
            return t.text, 1
        if t.text == ">":                        # >>= arrives as glued '>' '>' '='
            n1, n2 = self.peek(), self.peek(2)
            if n1.text == ">" and n1.pos == t.end and n2.text == "=" and n2.pos == n1.end:
                return ">>=", 3
        return "", 0

    def try_lambda(self) -> Lambda | None:
        t = self.tok
        if t.kind == "ident" and self.peek().text == "->":
            self.i += 2
            p = Param(TypeRef("var"), t.text, line=t.line)
            return Lambda(line=t.line, col=t.col, params=[p], body=self.lambda_body())
        if t.text != "(" or t.kind != "op":
            return None
        # find the matching paren; a lambda iff '->' follows it
        depth = 0
        j = self.i
        while True:
            tj = self.toks[j]
            if tj.kind == "eof":
                return None
            if tj.kind == "op" and tj.text == "(":
                depth += 1
            elif tj.kind == "op" and tj.text == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if self.toks[j + 1].text != "->":
            return None
        self.next()
        params: list[Param] = []
        while not self.at(")"):
            mods, annos, _ = self.modifiers()
            line = self.tok.line
            if self.at_ident() and self.peek().text in (",", ")"):
                params.append(Param(TypeRef("var"), self.ident(), line=line))
            else:
                if self.tok.text == "var":
                    self.next()
                    ty = TypeRef("var")
                else:
                    ty = self.type_ref()
                va = self.accept("...")
                name = self.ident()
                while self.at("[") and self.peek().text == "]":
                    self.i += 2
                    ty.dims += 1
                params.append(Param(ty, name, va, "final" in mods, annos, line))
            if not self.accept(","):
                break
        self.expect(")")
        self.expect("->")
        return Lambda(line=t.line, col=t.col, params=params, body=self.lambda_body())

    def lambda_body(self) -> Any:
        if self.at("{"):
            return self.block()
        return self.expression()

    def ternary(self) -> Any:
        cond = self.binary(1)
        if self.at("?"):
            q = self.next()
            then = self.expression() if self.no_lambda == 0 else self.ternary()
            self.expect(":")
            if self.no_lambda == 0:
                lam = self.try_lambda()
                other = lam if lam is not None else self.ternary()
            else:
                other = self.ternary()
            return Conditional(line=getattr(cond, "line", q.line), col=getattr(cond, "col", q.col),
                               cond=cond, then=then, other=other)
        return cond

    def binary_op(self) -> tuple[str, int]:
        """The binary operator at the cursor and how many tokens it spans."""
        t = self.tok
        if t.kind == "kw" and t.text == "instanceof":
            return "instanceof", 1
        if t.kind != "op":
            return "", 0
        if t.text == ">":
            n1, n2 = self.peek(), self.peek(2)
            if n1.pos == t.end and n1.text == ">":
                if n2.pos == n1.end and n2.text == ">":
                    return ">>>", 3
                if n2.pos == n1.end and n2.text == "=":
                    return "", 0                # >>= is an assignment
                return ">>", 2
            if n1.pos == t.end and n1.text == "=":
                return ">=", 2
            return ">", 1
        if t.text in _BINARY_PREC:
            return t.text, 1
        return "", 0

    def binary(self, min_prec: int) -> Any:
        left = self.unary()
        return self.binary_rest(left, min_prec)

    def binary_rest(self, left: Any, min_prec: int) -> Any:
        while True:
            op, width = self.binary_op()
            if not op:
                return left
            prec = _BINARY_PREC[op]
            if prec < min_prec:
                return left
            t = self.tok
            self.i += width
            if op == "instanceof":
                self.accept("final")
                line, col = t.line, t.col
                pat = self.try_pattern()
                if pat is not None:
                    left = InstanceOf(line=getattr(left, "line", line),
                                      col=getattr(left, "col", col), expr=left, type=pat.type,
                                      binding=pat.binding, pattern=pat)
                else:
                    ty = self.type_ref()
                    left = InstanceOf(line=getattr(left, "line", line),
                                      col=getattr(left, "col", col), expr=left, type=ty)
                continue
            right = self.binary(prec + 1)
            left = Binary(line=getattr(left, "line", t.line), col=getattr(left, "col", t.col),
                          op=op, left=left, right=right)

    def unary(self) -> Any:
        t = self.tok
        if t.kind == "op" and t.text in ("+", "-", "!", "~", "++", "--"):
            self.next()
            return Unary(line=t.line, col=t.col, op=t.text, expr=self.unary())
        if t.kind == "op" and t.text == "(":
            cast = self.try_cast()
            if cast is not None:
                return cast
        return self.postfix(self.primary())

    def try_cast(self) -> Cast | None:
        save = self.i
        t = self.next()                          # (
        nxt = self.tok
        try:
            if nxt.kind == "kw" and nxt.text in PRIMITIVES:
                ty = self.type_ref()
                if not self.accept(")"):
                    raise ParseError("not a cast", self.tok)
                return Cast(line=t.line, col=t.col, type=ty, expr=self.unary())
            if nxt.kind != "ident" and nxt.text != "@":
                raise ParseError("not a cast", nxt)
            ty = self.type_ref()
            while self.accept("&"):
                self.type_ref()
            if not self.at(")"):
                raise ParseError("not a cast", self.tok)
            self.next()
            f = self.tok
            ok = (f.kind in ("ident", "num", "str", "chr")
                  or (f.kind == "kw" and f.text in _CAST_FOLLOW_KW)
                  or (f.kind == "op" and f.text in ("(", "!", "~")))
            if not ok:
                raise ParseError("not a cast", f)
            if self.no_lambda == 0:
                lam = self.try_lambda()
                if lam is not None:
                    return Cast(line=t.line, col=t.col, type=ty, expr=lam)
            return Cast(line=t.line, col=t.col, type=ty, expr=self.unary())
        except ParseError:
            self.i = save
            return None

    def arguments(self) -> list:
        self.expect("(")
        out: list = []
        if self.accept(")"):
            return out
        saved = self.no_lambda
        self.no_lambda = 0
        try:
            while True:
                out.append(self.expression())
                if self.accept(","):
                    continue
                self.expect(")")
                return out
        finally:
            self.no_lambda = saved

    def primary(self) -> Any:
        t = self.tok
        line, col = t.line, t.col
        if t.kind in _LITERAL_KINDS:
            self.next()
            kind = {"str": "str", "chr": "char"}.get(t.kind, "")
            if t.kind == "num":
                low = t.text.lower()
                if low.endswith("l"):
                    kind = "long"
                elif low.endswith("f") and not low.startswith("0x"):
                    kind = "float"
                elif (("." in low or "e" in low) and not low.startswith("0x")) or \
                        (low.endswith("d") and not low.startswith("0x")):
                    kind = "double"
                else:
                    kind = "int"
            return Literal(line=line, col=col, kind=kind, value=t.text)
        if t.kind == "kw":
            k = t.text
            if k in ("true", "false"):
                self.next()
                return Literal(line=line, col=col, kind="boolean", value=k)
            if k == "null":
                self.next()
                return Literal(line=line, col=col, kind="null", value=k)
            if k == "this":
                self.next()
                if self.at("("):
                    return MethodCall(line=line, col=col, name="this", args=self.arguments())
                return This(line=line, col=col)
            if k == "super":
                self.next()
                if self.at("("):
                    return MethodCall(line=line, col=col, name="super", args=self.arguments())
                return Super(line=line, col=col)
            if k == "new":
                return self.creator()
            if k == "switch":
                sw = self.switch()
                sw.is_expr = True
                return sw
            if k in PRIMITIVES:
                ty = self.type_ref()
                if self.accept("::"):
                    self.expect("new")
                    return MethodRef(line=line, col=col, target=ty, name="new")
                self.expect(".")
                self.expect("class")
                return ClassLit(line=line, col=col, type=ty)
        if t.kind == "op":
            if t.text == "(":
                self.next()
                saved = self.no_lambda
                self.no_lambda = 0
                try:
                    e = self.expression()
                finally:
                    self.no_lambda = saved
                self.expect(")")
                return e
            if t.text == "@":                    # annotated type in an expression: skip
                self.annotations()
                return self.primary()
            if t.text == "<":                    # <T>foo() generic call with implicit this
                self.type_args()
                name = self.ident()
                return MethodCall(line=line, col=col, name=name, args=self.arguments())
        if t.kind == "ident":
            if self.no_lambda == 0 and self.peek().text == "->":
                lam = self.try_lambda()
                if lam is not None:
                    return lam
            # array type literal / ctor ref:  String[].class, int[]::new, Foo[]::new
            if self.peek().text == "[" and self.peek(2).text == "]":
                save = self.i
                try:
                    ty = self.type_ref()
                    if self.accept("::"):
                        self.expect("new")
                        return MethodRef(line=line, col=col, target=ty, name="new")
                    self.expect(".")
                    self.expect("class")
                    return ClassLit(line=line, col=col, type=ty)
                except ParseError:
                    self.i = save
            # generic type method reference: List<String>::new
            if self.peek().text == "<":
                save = self.i
                try:
                    ty = self.type_ref()
                    if self.accept("::"):
                        name = "new" if self.accept("new") else self.ident()
                        return MethodRef(line=line, col=col, target=ty, name=name)
                except ParseError:
                    pass
                self.i = save
            self.next()
            if self.at("("):
                return MethodCall(line=line, col=col, name=t.text, args=self.arguments())
            return Name(line=line, col=col, name=t.text)
        raise ParseError(f"unexpected '{t.text or 'EOF'}'", t)

    def creator(self) -> Any:
        t = self.expect("new")
        line, col = t.line, t.col
        if self.at("<"):
            self.type_args()
        self.annotations()
        if self.tok.kind == "kw" and self.tok.text in PRIMITIVES:
            ty = TypeRef(self.next().text)
        else:
            parts = [self.ident()]
            args: list[TypeRef] = []
            if self.at("<"):
                args = self.type_args()
            while self.at("."):
                self.next()
                self.annotations()
                parts.append(self.ident())
                if self.at("<"):
                    args = self.type_args()
            ty = TypeRef(".".join(parts), args)
        if self.at("["):
            dims: list = []
            while self.at("["):
                self.next()
                if self.accept("]"):
                    dims.append(None)
                else:
                    dims.append(self.expression())
                    self.expect("]")
            ty.dims = len(dims)
            init = self.array_init() if self.at("{") else None
            return NewArray(line=line, col=col, type=ty, dims=dims, init=init)
        args_ = self.arguments()
        body = None
        if self.at("{"):
            body = TypeDecl(line=self.tok.line, col=self.tok.col, kind="class",
                            name="", anonymous=True, extends=[ty])
            self.class_body(body)
        return New(line=line, col=col, type=ty, args=args_, body=body)

    def postfix(self, e: Any) -> Any:
        while True:
            t = self.tok
            if t.kind != "op":
                return e
            if t.text == ".":
                self.next()
                n = self.tok
                if n.text == "<":
                    self.type_args()
                    n = self.tok
                if n.kind == "ident":
                    self.next()
                    if self.at("("):
                        e = MethodCall(line=n.line, col=n.col, target=e, name=n.text,
                                       args=self.arguments())
                    else:
                        e = FieldAccess(line=n.line, col=n.col, target=e, name=n.text)
                elif n.text == "new":
                    inner = self.creator()
                    if isinstance(inner, New):
                        inner.outer = e
                    e = inner
                elif n.text == "this":
                    self.next()
                    e = This(line=n.line, col=n.col, qualifier=_dotted(e))
                elif n.text == "super":
                    self.next()
                    q = _dotted(e)
                    if self.at("("):          # outer.super(...) in an inner-class ctor
                        e = MethodCall(line=n.line, col=n.col, name="super",
                                       args=self.arguments())
                    else:
                        e = Super(line=n.line, col=n.col, qualifier=q)
                elif n.text == "class":
                    self.next()
                    e = ClassLit(line=n.line, col=n.col, type=TypeRef(_dotted(e)))
                else:
                    raise ParseError(f"unexpected '{n.text}' after '.'", n)
            elif t.text == "[" and self.peek().text == "]" and _dotted(e):
                # qualified array type: Map.Entry[].class, Outer.Inner[]::new
                ty = TypeRef(_dotted(e))
                while self.at("[") and self.peek().text == "]":
                    self.i += 2
                    ty.dims += 1
                if self.accept("::"):
                    self.expect("new")
                    e = MethodRef(line=e.line, col=e.col, target=ty, name="new")
                else:
                    self.expect(".")
                    self.expect("class")
                    e = ClassLit(line=e.line, col=e.col, type=ty)
            elif t.text == "[":
                self.next()
                idx = self.expression()
                self.expect("]")
                e = ArrayAccess(line=t.line, col=t.col, array=e, index=idx)
            elif t.text in ("++", "--"):
                self.next()
                e = Unary(line=t.line, col=t.col, op=t.text, expr=e, postfix=True)
            elif t.text == "::":
                self.next()
                if self.at("<"):
                    self.type_args()
                name = "new" if self.accept("new") else self.ident()
                e = MethodRef(line=t.line, col=t.col, target=e, name=name)
            else:
                return e


def _dotted(e: Any) -> str:
    if isinstance(e, Name):
        return e.name
    if isinstance(e, FieldAccess):
        base = _dotted(e.target)
        return f"{base}.{e.name}" if base else e.name
    return ""


def dotted(e: Any) -> str:
    """``a.b.c`` for a chain of names, else ``""``."""
    return _dotted(e)


def parse(src: str) -> CompilationUnit:
    return Parser(src).parse()


# --------------------------------------------------------------------------
# traversal
# --------------------------------------------------------------------------
_CHILD_FIELDS: dict[type, tuple[str, ...]] = {}


def children(n: Any) -> Iterator[Any]:
    """Direct child syntax nodes of a statement or expression (not into nested types)."""
    if isinstance(n, list):
        for x in n:
            yield from children(x) if isinstance(x, list) else ([x] if x is not None else [])
        return
    cls = type(n)
    names = _CHILD_FIELDS.get(cls)
    if names is None:
        names = tuple(f for f in getattr(cls, "__dataclass_fields__", {})
                      if f not in ("line", "col", "end_line", "toks"))
        _CHILD_FIELDS[cls] = names
    for f in names:
        v = getattr(n, f)
        if v is None or isinstance(v, (str, int, bool, TypeRef, TypeDecl)):
            continue
        if isinstance(v, list):
            for x in v:
                if isinstance(x, tuple):          # LocalVar.names
                    if len(x) >= 3 and x[2] is not None:
                        yield x[2]
                elif x is not None and not isinstance(x, (str, TypeRef, TypeDecl)):
                    yield x
        elif isinstance(v, tuple):
            continue
        else:
            yield v
