"""Find the failures this codebase swallows without telling anyone.

Three separate bugs reached the live app in one evening, and they were the same
bug wearing different clothes:

  1. A malformed connection string made `database_url()` raise. The caller
     caught it, passed, and opened an empty local SQLite file instead.
  2. The empty board that resulted said "run `fcc sync` first" - a confident,
     wrong diagnosis, because nothing had recorded WHY the data was missing.
  3. A cached SQLite connection pinged healthy forever, so correcting the
     secret changed nothing at all.

Every one of them is the same shape: something failed, a handler decided that
was survivable, and the program carried on in a degraded state that LOOKED like
a working state. No test catches this, because from the outside nothing is
wrong - which is precisely the problem. The user finds it, on draft night.

So this checker takes a position: an exception handler must do at least one of

    raise / re-raise      - it was not survivable after all
    log something         - it was survivable, and now there is a record
    tell the user         - st.error, st.warning, print

A handler that does none of those is a silent fallback, and is reported.

There ARE legitimately silent handlers - a best-effort cleanup, an optional
import, a cache read that is allowed to miss. Those are fine, and are declared
in place rather than argued about here:

    except Exception:  # silent: streamlit is optional outside the dashboard
        pass

The marker is deliberately a nuisance to type. Writing it is a decision that
this failure genuinely does not matter, made once, in the open, next to the
code - instead of a habit of `except Exception: pass` that nobody re-reads.

Run:  python tools/check_degradation.py [paths...]
Exit: 0 clean, 1 undeclared silent handlers found.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

#: A handler body containing any of these is reporting the failure somehow.
_REPORTING_CALLS = (
    "log", "logger", "logging",           # log.info(...), logger.warning(...)
    "print", "traceback",                 # traceback.print_exc()
    "warn", "warning", "error", "exception", "critical", "debug", "info",
    "st",                                 # st.error / st.warning in the UI
)

MARKER = "# silent:"

#: Only BROAD handlers are reported by default, and that is a deliberate aim
#: rather than a compromise. All three shipped bugs were `except Exception` -
#: a handler that cannot say what it expected to go wrong, and therefore cannot
#: distinguish "the value was missing" from "the database was unreachable".
#:
#: A narrow handler is a different act. `except (TypeError, ValueError)` around
#: a `float()` names the exact failure it is absorbing and could not absorb a
#: connection error if it tried, so it degrades nothing. Reporting those too
#: buries the dangerous handlers among dozens of harmless ones, and a checker
#: whose output nobody reads is a checker nobody runs. `--strict` reports them.
BROAD = {"Exception", "BaseException"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """A bare `except:`, or one that catches Exception/BaseException."""
    if handler.type is None:
        return True
    names = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return any(isinstance(n, ast.Name) and n.id in BROAD for n in names)


def _mentions_reporting(node: ast.ExceptHandler) -> bool:
    """Does this handler body report the failure in any form?

    Three forms count, and the third is the one worth explaining. A handler
    that binds the exception and then USES it - `return {"ok": False, "error":
    str(exc)}`, `last_error = exc` - is not swallowing anything: it has
    converted the failure into a value and handed it to its caller, which is
    what a health check or a retry loop is supposed to do. Only a handler that
    binds nothing and reports nothing has genuinely lost the information.
    """
    if node.name:
        used = any(
            isinstance(child, ast.Name) and child.id == node.name
            for child in ast.walk(node)
        )
        if used:
            return True

    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            return True
        if isinstance(child, ast.Call):
            func = child.func
            # log.info(...) / logger.warning(...) / st.error(...)
            if isinstance(func, ast.Attribute):
                root = func.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id in _REPORTING_CALLS:
                    return True
                if func.attr in _REPORTING_CALLS:
                    return True
            elif isinstance(func, ast.Name) and func.id in _REPORTING_CALLS:
                return True
    return False


def _declared_silent(lines: list[str], handler: ast.ExceptHandler) -> bool:
    """Is there a `# silent: reason` marker near the handler?

    The window reaches a few lines ABOVE the `except` as well as below it,
    because a reason worth writing is often too long for a trailing comment and
    belongs on its own line above - and a checker that refuses to see a marker
    written the readable way teaches people to write it the unreadable way.
    """
    start = max(handler.lineno - 4, 0)
    end = min(handler.lineno + 2, len(lines))
    return any(MARKER in line for line in lines[start:end])


def check_file(path: Path, strict: bool = False) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:                      # a broken file is a failure
        return [f"{path}:{exc.lineno}: could not parse ({exc.msg})"]

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not strict and not _is_broad(node):
            continue
        if _declared_silent(lines, node):
            continue
        if _mentions_reporting(node):
            continue
        caught = ast.unparse(node.type) if node.type else "bare except"
        problems.append(
            f"{path}:{node.lineno}: `except {caught}` swallows the failure "
            "without logging, raising or telling the user. Add a log line, "
            f"re-raise, or declare it: `{MARKER} <why this cannot matter>`"
        )
    return problems


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--strict"]
    strict = "--strict" in argv
    targets = args or ["src", "dashboard.py", "fcc.py"]
    files: list[Path] = []
    for target in targets:
        path = Path(target)
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])

    problems = []
    for path in files:
        problems.extend(check_file(path, strict=strict))

    if problems:
        print(f"{len(problems)} silent failure handler(s):\n")
        for problem in problems:
            print(f"  {problem}\n")
        print(
            "Each of these is a place the program can carry on in a degraded "
            "state without anyone finding out until it matters."
        )
        return 1

    scope = "handlers" if strict else "broad handlers"
    print(f"No undeclared silent {scope} in {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
