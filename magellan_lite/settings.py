"""A project's own settings, from the ``[tool.magellan-lite]`` table of its pyproject.toml.

    [tool.magellan-lite]
    fail-on = "review"                      # block (the default), review or never
    disable = ["compare-to-none"]           # rules whose findings are left out
    exclude = ["migrations/", "tests/fixtures/*.py"]   # paths whose findings are left out

``exclude`` takes paths relative to the pyproject.toml: a directory (``migrations/``) covers
everything under it, and ``*`` matches across ``/``. A flag on the command line beats the file.

Reading TOML needs ``tomllib``, new in Python 3.11. On 3.10 the file is not read and the
defaults apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from magellan_lite.report import VERDICTS, Report

FAIL_ON = (*VERDICTS[1:], "never")
KEYS = ("fail-on", "disable", "exclude")


class SettingsError(ValueError):
    """pyproject.toml cannot be read, or a setting has the wrong kind of value."""


@dataclass
class Settings:
    fail_on: str | None = None              # None: the command line's default (block)
    disable: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    #: the checked project's place under the pyproject.toml ("" when they are the same folder)
    prefix: str = ""
    #: settings that look like mistakes but do not stop the check (an unknown key or rule)
    warnings: list[str] = field(default_factory=list)

    def is_excluded(self, path: str) -> bool:
        path = f"{self.prefix}/{path}" if self.prefix else path
        for pattern in self.exclude:
            pattern = pattern.replace("\\", "/").removeprefix("./")
            if fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("/") + "/"):
                return True
        return False


def find_pyproject(root: Path) -> Path | None:
    """The nearest pyproject.toml at or above ``root``, not looking past the repository's
    top (the folder holding ``.git``)."""
    for folder in (root, *root.parents):
        if (folder / "pyproject.toml").is_file():
            return folder / "pyproject.toml"
        if (folder / ".git").exists():
            return None
    return None


def load(root: str | Path) -> Settings:
    """The settings for the project at ``root``; the defaults when there are none."""
    root = Path(root).resolve()
    path = find_pyproject(root)
    if path is None:
        return Settings()
    try:
        import tomllib
    except ImportError:                     # Python 3.10: no TOML reader in the standard library
        return Settings()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from None
    table = data.get("tool", {}).get("magellan-lite", {})
    if not isinstance(table, dict):
        raise SettingsError(f"{path}: [tool.magellan-lite] must be a table")

    where = f"{path} [tool.magellan-lite]"
    fail_on = table.get("fail-on")
    if fail_on is not None and fail_on not in FAIL_ON:
        raise SettingsError(f"{where}: fail-on must be one of {', '.join(FAIL_ON)}, "
                            f"not {fail_on!r}")
    disable, exclude = _strings(table, "disable", where), _strings(table, "exclude", where)

    prefix = root.relative_to(path.parent).as_posix() if root != path.parent else ""
    out = Settings(fail_on, disable, exclude, prefix)
    out.warnings += [f"{where}: unknown setting {k!r} (known: {', '.join(KEYS)})"
                     for k in table if k not in KEYS]
    import magellan_lite.rules  # noqa: F401  importing the package registers every rule
    from magellan_lite.findings import RULES
    out.warnings += [f"{where}: disable names no rule called {r!r} (see `magellan-lite rules`)"
                     for r in disable if r not in RULES]
    return out


def _strings(table: dict, key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SettingsError(f"{where}: {key} must be a list of strings")
    return tuple(value)


def apply(report: Report, settings: Settings) -> Report:
    """Leave out the findings of disabled rules, and the findings and changes in excluded
    paths. The verdict follows, since it is worked out from the findings."""
    report.findings = [f for f in report.findings
                       if f.rule not in settings.disable and not settings.is_excluded(f.path)]
    report.changes = [c for c in report.changes if not settings.is_excluded(c.path)]
    return report
