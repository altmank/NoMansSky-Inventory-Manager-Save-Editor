"""`python -m nms_sorter` -> nms_sorter.cli.main.

The Python version is checked here, before `cli` (and therefore `server`,
`planner` and the codec) is imported, because the declared floor is 3.9 and the
modules below use syntax that a 3.8 interpreter cannot even parse: a
SyntaxError naming a line in `server.py` tells the operator nothing, while one
sentence about their Python tells them everything (GOAL.md P2-2).

Nothing above the check may use 3.9-only syntax, and nothing above it may
import from this package beyond `__init__`, which is a docstring and a version
string. Exit code 4 is "your Python is too old"; 3 is "no free port".
"""
import sys

#: the floor declared in pyproject.toml. `server.py` merges dicts with `|`,
#: which is 3.9.
MIN_PYTHON = (3, 9)

TOO_OLD = ("This needs Python %s or newer and this is Python %s; install a "
           "current Python from https://www.python.org/downloads/ and run it "
           "again.")


def check_python(version=None, out=None):
    """-> 0 if this interpreter is new enough, else 4 after one sentence.

    Takes the version rather than reading `sys.version_info` directly so that
    the refusal itself is testable on a machine that is, by definition, running
    a Python new enough for the test suite.
    """
    version = tuple(version or sys.version_info)[:3]
    if version >= MIN_PYTHON:
        return 0
    (out or sys.stderr).write(
        (TOO_OLD % (".".join(str(p) for p in MIN_PYTHON),
                    ".".join(str(p) for p in version))) + "\n")
    return 4


def main(argv=None):
    """The one start path: check the interpreter, then hand over to `cli`.

    `python -m nms_sorter` reaches this, and so does `packaging/entry.py` in
    the frozen build, so there is exactly one place where "is this Python new
    enough" happens and exactly one place where the import of `cli` happens.

    In a frozen build the check is moot -- the interpreter is inside the
    executable and is whatever built it -- and it is left in rather than
    branched around, because a check that passes costs a tuple comparison and
    a branch costs a way for the two start paths to differ.
    """
    code = check_python(out=sys.stdout)
    if code:
        return code
    from .cli import main as serve                             # noqa: E402
    return serve(argv)


if __name__ == "__main__":
    sys.exit(main())
