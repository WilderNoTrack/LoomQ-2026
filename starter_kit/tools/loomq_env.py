"""Load ``starter_kit/secrets.env`` into the environment, without echoing it.

Only the tools under ``tools/`` call this.  ``adapter.py``, ``evaluator.py`` and
everything else that runs during scoring read the environment the organisers
inject and never touch this file.

Values are never printed: :func:`report` shows which variables are set and how
long they are, which is enough to debug a typo and useless to anyone reading a
terminal over your shoulder.
"""

import os
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "secrets.env"
)

#: Variables whose value must never be shown, only measured.
_SECRET = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def load(path: Optional[str] = None, override: bool = False) -> Dict[str, str]:
    """Read ``KEY=value`` lines into ``os.environ``. Returns the names loaded."""
    target = path or DEFAULT_PATH
    loaded = {}  # type: Dict[str, str]
    if not os.path.isfile(target):
        return loaded

    with open(target, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if not name or not value:
                continue
            if override or not os.environ.get(name):
                os.environ[name] = value
            loaded[name] = value
    return loaded


def is_secret(name: str) -> bool:
    return any(marker in name.upper() for marker in _SECRET)


def describe(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        return "not set"
    if is_secret(name):
        return "set (%d characters)" % len(value)
    return value


def report(names: Iterable[str]) -> List[Tuple[str, bool, str]]:
    """``(name, present, description)`` for each variable, never the raw value."""
    rows = []
    for name in names:
        rows.append((name, bool(os.environ.get(name)), describe(name)))
    return rows


def require(names: Iterable[str]) -> List[str]:
    """Names that are still missing."""
    return [name for name in names if not os.environ.get(name)]


def print_report(title: str, names: Iterable[str]) -> None:
    print(title)
    for name, present, description in report(names):
        print("  [%s] %-28s %s" % ("ok" if present else "  ", name, description))


__all__ = ["DEFAULT_PATH", "describe", "load", "print_report", "report", "require"]
