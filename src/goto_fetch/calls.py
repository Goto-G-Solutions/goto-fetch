"""Calls described once, then either made or written as Python source.

FetchPlan describes each piece of a fetch (the client, its auth, the
paging style, the tracker, the warehouse) as a Call. build() makes the
object to run the fetch now; code() writes the same call into a script.
Because both come from one description, a script can't drift from what
the plan runs.

A Secret builds as its value and is written as a read of an environment
variable, so secrets run but never reach a script. A Name is written as
itself, for names the script defines (client, shop, run).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LINE = 88
"""The longest line a written call may take before it splits, one argument a line."""


@dataclass(frozen=True)
class Secret:
    """A value that builds as itself and is written as os.environ[`variable`].

    With `optional`, the script reads os.environ.get(`variable`), and an
    empty value builds as None.
    """

    variable: str
    value: str = field(repr=False)
    optional: bool = False


@dataclass(frozen=True)
class Name:
    """A name a script defines, such as `client`; written as is, never built."""

    text: str


@dataclass(frozen=True)
class Call:
    """A call of `target` with `args` and `kwargs`, to make now or write as code.

    `target` is a function or class, or the text of a method a script calls
    (such as "client.records"), which can only be written. Arguments may
    be Calls, Secrets, Names, functions and classes (written by name), or
    plain values (written with repr).
    """

    target: Callable[..., Any] | str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    def build(self, **extra: Any) -> Any:
        """Make the call, with `extra` keyword arguments added."""
        if isinstance(self.target, str):
            raise TypeError(f"{self.target} can only be written, not built")
        args = [build(value) for value in self.args]
        kwargs = {name: build(value) for name, value in self.kwargs.items()}
        return self.target(*args, **kwargs, **extra)

    def code(self, level: int = 0) -> str:
        """The call as Python source, at `level` indents.

        On one line if it fits in LINE characters, else one argument a line.
        """
        name = self.target if isinstance(self.target, str) else self.target.__name__
        parts = [code(value, level + 1) for value in self.args]
        parts += [
            f"{key}={code(value, level + 1)}" for key, value in self.kwargs.items()
        ]
        one_line = f"{name}({', '.join(parts)})"
        if (
            all("\n" not in part for part in parts)
            and len(one_line) + 4 * level <= LINE
        ):
            return one_line
        inner = "    " * (level + 1)
        lines = [
            f"{name}(",
            *(f"{inner}{part}," for part in parts),
            "    " * level + ")",
        ]
        return "\n".join(lines)


def build(value: Any) -> Any:
    """`value` as a running fetch uses it: Calls made, Secrets as their values."""
    if isinstance(value, Call):
        return value.build()
    if isinstance(value, Secret):
        return (value.value or None) if value.optional else value.value
    if isinstance(value, Name):
        raise TypeError(f"{value.text} only exists in a script")
    if isinstance(value, list):
        return [build(item) for item in value]
    return value


def code(value: Any, level: int = 0) -> str:
    """`value` as Python source in a script."""
    if isinstance(value, Call):
        return value.code(level)
    if isinstance(value, Secret):
        if value.optional:
            return f"os.environ.get({value.variable!r})"
        return f"os.environ[{value.variable!r}]"
    if isinstance(value, Name):
        return value.text
    if isinstance(value, list):
        return "[" + ", ".join(code(item, level) for item in value) + "]"
    if isinstance(value, type) or callable(value):
        return value.__name__
    return repr(value)


def names(value: Any) -> set[str]:
    """The names of the functions and classes `value` uses, for the script's import."""
    if isinstance(value, Call):
        found = set() if isinstance(value.target, str) else {value.target.__name__}
        for item in [*value.args, *value.kwargs.values()]:
            found |= names(item)
        return found
    if isinstance(value, list):
        return set().union(*(names(item) for item in value))
    if isinstance(value, type) or callable(value):
        return {value.__name__}
    return set()


def reads_environment(value: Any) -> bool:
    """Whether `value` holds a Secret, so the script needs `import os`."""
    if isinstance(value, Secret):
        return True
    if isinstance(value, Call):
        return any(
            reads_environment(item) for item in [*value.args, *value.kwargs.values()]
        )
    if isinstance(value, list):
        return any(reads_environment(item) for item in value)
    return False
