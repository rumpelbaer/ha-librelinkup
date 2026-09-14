"""Static guards against leaking credentials or health data.

Checked on the source rather than by exercising every code path, so a new log
line or a new exception cannot quietly reintroduce a leak that no test happens to
cover. The API's own error objects are the reason: a ClientResponseError renders
the request URL, and its repr() contains the Authorization header.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "librelinkup"
)

SOURCE_FILES = sorted(COMPONENT_DIR.glob("*.py"))

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}

# Names that must never be interpolated into a log line or an error message.
FORBIDDEN_NAMES = {
    "email",
    "_email",
    "password",
    "_password",
    "token",
    "_token",
    "token_used",
    "account_id",
    "_account_id",
    "patient_id",
    "measurement",
    "measurements",
    "connection",
    "connections",
    "snapshot",
    "payload",
    "result",
    "response",
    "data",
    "err",
    "error",
    "exception",
}


def _parsed():
    for path in SOURCE_FILES:
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _log_calls():
    for path, tree in _parsed():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"_LOGGER", "logger"}
            ):
                yield path, node


def _raises():
    for path, tree in _parsed():
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                yield path, node.exc


def test_there_are_log_calls_and_raises_to_check() -> None:
    """Guards the guard: an empty scan would pass every assertion below."""
    assert SOURCE_FILES
    assert list(_log_calls())
    assert list(_raises())


@pytest.mark.parametrize("kind", ["log", "raise"])
def test_messages_are_literal_strings(kind) -> None:
    """A message must be written out, never built from runtime values."""
    calls = _log_calls() if kind == "log" else _raises()

    for path, call in calls:
        if not call.args:
            continue

        message = call.args[0]

        if isinstance(message, ast.Constant) and isinstance(message.value, str):
            continue

        if isinstance(message, ast.JoinedStr):
            # An f-string is only acceptable if every interpolation is safe.
            for value in message.values:
                if isinstance(value, ast.Constant):
                    continue

                assert isinstance(value, ast.FormattedValue)
                names = {
                    node.id
                    for node in ast.walk(value)
                    if isinstance(node, ast.Name)
                } | {
                    node.attr
                    for node in ast.walk(value)
                    if isinstance(node, ast.Attribute)
                }

                assert not names & FORBIDDEN_NAMES, (
                    f"{path.name}: f-string interpolates {names & FORBIDDEN_NAMES}"
                )

            continue

        raise AssertionError(
            f"{path.name}:{message.lineno}: message is not a literal string"
        )


def test_no_forbidden_value_is_interpolated_into_a_log_line() -> None:
    for path, call in _log_calls():
        for argument in call.args[1:]:
            names = {
                node.id for node in ast.walk(argument) if isinstance(node, ast.Name)
            } | {
                node.attr
                for node in ast.walk(argument)
                if isinstance(node, ast.Attribute)
            }

            assert not names & FORBIDDEN_NAMES, (
                f"{path.name}:{call.lineno}: log argument uses "
                f"{names & FORBIDDEN_NAMES}"
            )


def test_nothing_is_ever_rendered_with_repr() -> None:
    """repr() of an aiohttp error carries the Authorization header."""
    for path, tree in _parsed():
        source = path.read_text(encoding="utf-8")

        assert "%r" not in source, f"{path.name} formats a value with %r"

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "repr"
            ):
                raise AssertionError(f"{path.name}:{node.lineno} calls repr()")


def test_api_errors_are_chained_away_from_the_original() -> None:
    """"from None" on the paths where the cause would carry a token or a URL."""
    source = (COMPONENT_DIR / "api.py").read_text(encoding="utf-8")

    # ContentTypeError and the 401/403 retry path are the two that matter.
    assert source.count("from None") >= 3

    coordinator = (COMPONENT_DIR / "coordinator.py").read_text(encoding="utf-8")

    assert "from None" in coordinator


def test_diagnostics_never_read_the_snapshot_itself() -> None:
    """Only counts may come out of coordinator.data."""
    source = (COMPONENT_DIR / "diagnostics.py").read_text(encoding="utf-8")

    assert "coordinator.data" in source
    # ...and only inside len().
    for line in source.splitlines():
        if "coordinator.data" in line:
            assert "len(" in line, f"diagnostics reads the snapshot: {line.strip()}"


def test_repair_issue_ids_are_keyed_by_config_entry() -> None:
    """Repair issues are persisted in .storage, so their ID must name no person.

    Checked across every source file rather than in one of them by name: which
    module holds the helper is a structural detail, that nothing but a config
    entry ID ever keys an issue is the invariant.
    """
    definitions = []
    calls = []

    for path, tree in _parsed():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_issue_id":
                definitions.append((path, node))

            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_issue_id"
            ):
                calls.append((path, node))

    # One definition, taking the kind and a config entry ID and nothing else.
    assert len(definitions) == 1, f"_issue_id defined {len(definitions)} times"
    _, definition = definitions[0]
    assert [argument.arg for argument in definition.args.args] == [
        "kind",
        "entry_id",
    ]

    # And every call site keys the issue by exactly that.
    assert calls

    for path, call in calls:
        assert len(call.args) == 2 and not call.keywords

        names = {
            node.id for node in ast.walk(call.args[1]) if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(call.args[1])
            if isinstance(node, ast.Attribute)
        }

        assert names <= {"entry", "entry_id"}, (
            f"{path.name}:{call.lineno}: issue ID keyed by {names}"
        )
