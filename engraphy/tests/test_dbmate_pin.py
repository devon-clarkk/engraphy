"""The dbmate pin: the admin image and both CI jobs install one exact dbmate
release, verified against one SHA-256 before it is made executable.

The admin image runs `engraphy-admin migrate` against the production database as
the Postgres superuser, so the migration runner it carries is a reviewed input.
CI installs the same release so that the migrations CI proves are applied by the
same runner the image ships. Pure file parsing, no database.
"""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOCKERFILE = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
_CI = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

_IMAGE_VERSION = re.compile(r"^ARG DBMATE_VERSION=(v\d+\.\d+\.\d+)$", re.MULTILINE)
_IMAGE_DIGEST = re.compile(r"^ARG DBMATE_SHA256=([0-9a-f]{64})$", re.MULTILINE)
_CI_DOWNLOAD = re.compile(r"releases/download/(v\d+\.\d+\.\d+)/dbmate-linux-amd64")
_CI_CHECK = re.compile(r'echo "([0-9a-f]{64})  /usr/local/bin/dbmate" \| sha256sum -c -')


def _image_pin() -> tuple[str, str]:
    versions = _IMAGE_VERSION.findall(_DOCKERFILE)
    digests = _IMAGE_DIGEST.findall(_DOCKERFILE)
    assert len(versions) == 1, "the admin image declares exactly one ARG DBMATE_VERSION=vX.Y.Z"
    assert len(digests) == 1, "the admin image declares exactly one ARG DBMATE_SHA256=<64 hex>"
    return versions[0], digests[0]


def _position(text: str, needle: str) -> int:
    assert needle in text, f"missing from the Dockerfile: {needle}"
    return text.index(needle)


def test_no_dbmate_download_follows_a_moving_release():
    for name, text in (("Dockerfile", _DOCKERFILE), ("ci.yml", _CI)):
        for line in text.splitlines():
            if "dbmate" in line:
                assert "releases/latest" not in line, f"{name}: {line.strip()}"


def test_the_admin_image_verifies_the_pinned_release_before_making_it_executable():
    _image_pin()
    download = _position(_DOCKERFILE, "releases/download/${DBMATE_VERSION}/dbmate-linux-amd64")
    check = _position(_DOCKERFILE, 'echo "${DBMATE_SHA256}  /usr/local/bin/dbmate" | sha256sum -c -')
    chmod = _position(_DOCKERFILE, "chmod +x /usr/local/bin/dbmate")
    assert download < check < chmod


def test_every_ci_install_matches_the_image_pin():
    version, digest = _image_pin()
    downloads = _CI_DOWNLOAD.findall(_CI)
    checks = _CI_CHECK.findall(_CI)
    assert downloads, "ci.yml installs dbmate"
    assert set(downloads) == {version}, f"CI installs {sorted(set(downloads))}, the image {version}"
    assert len(checks) == len(downloads), "every CI install verifies the digest"
    assert set(checks) == {digest}, "CI verifies a different digest from the image"
