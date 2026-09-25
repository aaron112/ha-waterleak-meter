"""Meta-tests: the suite itself must not have blind spots.

A duplicated test name silently shadows the earlier definition, so coverage
still reports 100% while a whole regression test is never collected.
"""

import ast
import collections
from pathlib import Path

import pytest

TEST_FILES = sorted(Path(__file__).parent.glob("test_*.py"))


def _test_functions(path: Path) -> list[str]:
    # ast.walk, not tree.body: a duplicate nested under a class or an
    # `if TYPE_CHECKING` block shadows just the same.
    tree = ast.parse(path.read_text())
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


@pytest.mark.parametrize("path", TEST_FILES, ids=lambda p: p.name)
def test_no_duplicate_test_names(path: Path) -> None:
    duplicates = [
        name
        for name, count in collections.Counter(_test_functions(path)).items()
        if count > 1
    ]
    assert not duplicates, f"{path.name} redefines {duplicates}; the first is never collected"
