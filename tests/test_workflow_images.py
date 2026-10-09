"""CI container images must not come from Docker Hub anonymously.

A GitHub-hosted runner pulls from Docker Hub unauthenticated, on a shared IP, so the
pull budget is spent by strangers: the PR gate went red on `main` (2026-10-09, run
37991214249) with `toomanyrequests` while pulling `postgres:18` for its service
container, before a single test ran. Naming a registry host -- ECR Public's mirror of
the Docker Official Images, `public.ecr.aws/docker/library/...` -- takes the pull off
that budget.
"""

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

_IMAGE = re.compile(r"^\s*(?:-\s*)?image:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)


def registry_host(image: str) -> str | None:
    """The registry an image reference names, or None when it means Docker Hub.

    Docker's own rule: the first `/`-separated component is a registry only if it
    contains a `.` or a `:`, or is `localhost`. `postgres:18` and `library/postgres`
    are both Docker Hub.
    """
    first, sep, _ = image.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first
    return None


def workflow_images() -> list[tuple[str, str]]:
    return [
        (path.name, match.group(1))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for match in _IMAGE.finditer(path.read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("postgres:18", None),
        ("library/postgres:18", None),
        ("docker.io/library/postgres:18", "docker.io"),
        ("public.ecr.aws/docker/library/postgres:18", "public.ecr.aws"),
        ("localhost/postgres", "localhost"),
        ("registry:5000/postgres", "registry:5000"),
    ],
)
def test_registry_host(image: str, expected: str | None) -> None:
    assert registry_host(image) == expected


def test_workflows_define_images() -> None:
    # Guards the test below against passing vacuously if the pattern stops matching.
    assert workflow_images()


def test_workflow_images_avoid_docker_hub() -> None:
    hub = [
        f"{name}: {image}"
        for name, image in workflow_images()
        if registry_host(image) in (None, "docker.io", "registry-1.docker.io")
    ]
    assert not hub, f"pulled anonymously from Docker Hub in CI: {hub}"
