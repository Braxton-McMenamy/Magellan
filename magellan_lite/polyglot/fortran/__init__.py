"""Fortran support: F77 fixed form through Fortran 2018 free form.

Pure Python (:mod:`.source` reads lines, :mod:`.parser` finds units and
references, :mod:`.frontend` resolves names and emits the graph,
:mod:`.semantics` judges signature changes, :mod:`.hazards` reports standing
legacy hazards). No compiler is required.

Mapping onto the shared graph
-----------------------------

=====================================  ==========================================
Fortran                                Magellan
=====================================  ==========================================
source file                            MODULE ``mod:fortran@src.solver.f90``
``module m`` / ``submodule (m) s``     MODULE ``mod:fortran@m`` / ``mod:fortran@m.s``
                                       (a submodule IMPORTS its ancestor)
``INCLUDE 'x.inc'`` / ``#include``     MODULE (tag ``include``), IMPORTS edge from
                                       the including file or module
external ``subroutine``/``function``   FUNCTION ``fn:fortran@name`` (global linker
                                       name; ``abi_exports=["name_"]``); in a file
                                       with a main PROGRAM it is that executable's:
                                       ``fn:fortran@<file>.name``
module procedure                       FUNCTION ``fn:fortran@m.name``
separate module procedure (submodule)  FUNCTION ``fn:fortran@<ancestor>.name``; its
                                       arity is the ancestor's interface body
internal procedure (after CONTAINS)    FUNCTION ``fn:fortran@<host>.name``
``program p``                          FUNCTION ``fn:fortran@<file>.p`` (tag
                                       ``program``; labelled entrypoint)
``block data``                         FUNCTION (tag ``block-data``), WRITES the
                                       COMMON blocks it initializes
``ENTRY e``                            FUNCTION ``fn:fortran@e`` (tag ``entry``),
                                       CALLS its host
generic ``interface g``                FUNCTION (tag ``generic``) CALLS each specific
derived ``type t``                     CLASS ``cls:fortran@m.t``; ``extends(b)`` is
                                       INHERITS; components are CLASS_ATTR
type-bound ``procedure :: p => impl``  METHOD ``fn:fortran@m.t.p`` CALLS ``impl``;
                                       a rebinding in an extension OVERRIDES
module variable / PARAMETER            GLOBAL ``var:fortran@m.x`` (tag ``constant``);
                                       a derived type ANNOTATES it
enumeration type / enum, bind(c)       CLASS with an enumerator CLASS_ATTR each
coarray ``x[*]``                       GLOBAL (tag ``coarray``, shared mutable state)
``COMMON /blk/``                       GLOBAL ``var:fortran@/blk/``: every declaring
                                       unit READS it (the layout is a dependency);
                                       assigning a member WRITES it. Declarers in
                                       executables that never link together get one
                                       block each: ``var:fortran@/blk/@<program>``
SAVE'd local (explicit or implied by   GLOBAL ``var:fortran@<unit>.x`` (tag ``save``
an initializer)                        / ``implicit-save``)
``use m, only: a => b``                IMPORTS m; IMPORT_ALIAS ``ali:`` BINDS the
                                       renamed entity
``CALL s(...)``                        CALLS (``meta.args``, keyword names in
                                       ``meta.opaque``)
``f(x)`` resolved as a function        CALLS (``function_ref``); as an array: none
structure constructor ``t(...)``       INSTANTIATES
procedure passed as an argument        READS (``procedure_argument``)
call through a procedure pointer       CALLS to every ``p => target`` seen (0.5,
                                       dynamic)
``type(t)`` dummy argument             ANNOTATES t
``bind(C, name="x")``                  ``abi_exports=["x"]``; a call through a
                                       ``bind(C)`` interface goes to ``ext:abi:x``
unresolved external                    EXTERNAL ``ext:fortran@name`` with
                                       ``abi_import="name_"`` (links to C)
=====================================  ==========================================
"""
