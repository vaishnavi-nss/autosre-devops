"""The images only contain the files their Dockerfile copies, so a new module that is imported
but not copied crashes the pod on start - and every in-process test still passes. This happened
once (second_opinion.py); the check below makes it impossible to repeat for either service."""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def copied(dockerfile: Path) -> set:
    names = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            names.update(t for t in line.split()[1:-1] if t.endswith(".py"))
    return names


def local_imports(service: Path, entry: str = "app.py") -> set:
    """Local modules reachable from the entry point through imports."""
    available = {p.name for p in service.glob("*.py")}
    seen, todo = set(), [entry]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = ast.parse((service / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                       else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            todo += [f"{m.split('.')[0]}.py" for m in modules if f"{m.split('.')[0]}.py" in available]
    return seen


def test_every_module_the_engine_imports_is_copied_into_its_image():
    service = REPO / "correlation-engine"
    missing = local_imports(service) - copied(service / "Dockerfile")
    assert not missing, f"imported by app.py but not COPYed into the image: {sorted(missing)}"


def test_every_module_the_scorer_imports_is_copied_into_its_image():
    service = REPO / "second-opinion"
    missing = local_imports(service) - copied(service / "Dockerfile")
    assert not missing, f"imported by app.py but not COPYed into the image: {sorted(missing)}"


def test_the_check_would_have_caught_the_original_bug(tmp_path):
    (tmp_path / "app.py").write_text("import helper\nimport newthing\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "newthing.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("COPY app.py helper.py ./\n", encoding="utf-8")
    assert local_imports(tmp_path) - copied(tmp_path / "Dockerfile") == {"newthing.py"}
