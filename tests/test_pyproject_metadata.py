"""Regression tests for PEP 621 metadata recovered from local work."""

from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


def _project_metadata() -> dict:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_optional_dependencies_are_a_standard_table() -> None:
    optional = _project_metadata()["optional-dependencies"]
    assert isinstance(optional, dict)
    assert {"engraphis", "embeddings", "test"} <= optional.keys()
    assert all(isinstance(requirements, list) for requirements in optional.values())
    for requirements in optional.values():
        for requirement in requirements:
            Requirement(requirement)


@pytest.mark.parametrize("python_version, needs_tomli", [("3.10", True), ("3.11", False), ("3.12", False)])
def test_test_extra_declares_the_metadata_parser(python_version, needs_tomli) -> None:
    environment = default_environment()
    environment.update(python_version=python_version, python_full_version=python_version + ".0", extra="test")
    requirements = (Requirement(value) for value in _project_metadata()["optional-dependencies"]["test"])
    selected = {requirement.name for requirement in requirements if requirement.marker is None or requirement.marker.evaluate(environment)}
    assert "pytest" in selected
    assert ("tomli" in selected) is needs_tomli
