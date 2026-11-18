"""패키징 정합성: pyproject.toml의 의존성과 도커 이미지용 requirements.txt가 같은 목록인지 확인한다."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _requirements(path: Path) -> set[str]:
    lines = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line and not line.startswith("-")}


def test_pyproject_and_requirements_list_the_same_packages():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = set(project["dependencies"]) | set(project["optional-dependencies"]["clip"])
    assert runtime == _requirements(ROOT / "requirements.txt")
    assert set(project["optional-dependencies"]["dev"]) == _requirements(ROOT / "requirements-dev.txt")


def test_license_metadata_follows_pep_639():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "MIT" and project["license-files"] == ["LICENSE"]
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
