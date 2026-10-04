# The verdict, every time you save

Magellan Lite maps your code from its syntax trees, finds what your change touched, and runs a
checklist built from real software failures on it.

| Verdict | Means |
|---|---|
| **ok** | nothing in what you touched needs a person |
| **review** | worth a look before you commit |
| **block** | this breaks something: a caller, a layout, a loop that never ends |

Each finding sits on its line, with a squiggle and the fix, in the Problems panel and in the
**Checklist** view of the Magellan Lite sidebar.

Nothing to install: the extension brings its own engine and finds a Python 3.10+ on this
computer by itself.
