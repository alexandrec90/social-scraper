"""CI container images must not come from a registry whose anonymous quota is per IP.

A GitHub-hosted runner pulls unauthenticated, on a shared IP, so the pull budget is
spent by strangers: the PR gate went red on `main` (2026-10-09, run 37991214249) with
`toomanyrequests` while pulling `postgres:18` from Docker Hub for its service container,
before a single test ran.

ECR Public, the first replacement, has the same shape: #90's gate failed every attempt
over eight hours on 2026-10-18 (run 39137365237) with `toomanyrequests: Data limit
exceeded` pulling `public.ecr.aws/docker/library/postgres:18`. `mirror.gcr.io`, Google's
pull-through cache of the Docker Official Images, is what the workflows name now.
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


PER_IP_QUOTA = (None, "docker.io", "registry-1.docker.io", "public.ecr.aws")


def test_workflow_images_avoid_a_per_ip_quota() -> None:
    quota = [
        f"{name}: {image}"
        for name, image in workflow_images()
        if registry_host(image) in PER_IP_QUOTA
    ]
    assert not quota, f"pulled anonymously from a per-IP quota in CI: {quota}"
