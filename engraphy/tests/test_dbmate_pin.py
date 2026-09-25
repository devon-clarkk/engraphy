"""The dbmate pin: the admin image and both CI jobs install one exact dbmate
release, each binary verified against a pinned SHA-256 before it is made
executable.

The admin image runs `engraphy-admin migrate` against the production database as
the Postgres superuser, so the migration runner it carries is a reviewed input.
It pins one digest per supported architecture and selects the one for the
platform it is building. CI installs the same release on amd64 with the amd64
digest, so the migrations CI proves are applied by the runner the image ships.
Pure file parsing, no database.
"""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOCKERFILE = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
_CI = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

_IMAGE_VERSION = re.compile(r"^ARG DBMATE_VERSION=(v\d+\.\d+\.\d+)$", re.MULTILINE)
_IMAGE_DIGEST = re.compile(r"^ARG DBMATE_SHA256_(AMD64|ARM64)=([0-9a-f]{64})$", re.MULTILINE)
_CI_DOWNLOAD = re.compile(r"releases/download/(v\d+\.\d+\.\d+)/dbmate-linux-amd64")
_CI_CHECK = re.compile(r'echo "([0-9a-f]{64})  /usr/local/bin/dbmate" \| sha256sum -c -')


def _image_pin() -> tuple[str, dict[str, str]]:
    versions = _IMAGE_VERSION.findall(_DOCKERFILE)
    assert len(versions) == 1, "the admin image declares exactly one ARG DBMATE_VERSION=vX.Y.Z"
    digests = dict(_IMAGE_DIGEST.findall(_DOCKERFILE))
    assert set(digests) == {"AMD64", "ARM64"}, "the admin image pins one digest per supported arch"
    assert len(set(digests.values())) == 2, "each architecture carries its own digest"
    return versions[0], digests


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
    select = _position(_DOCKERFILE, 'arch="$(dpkg --print-architecture)"')
    download = _position(_DOCKERFILE, "releases/download/${DBMATE_VERSION}/dbmate-linux-${arch}")
    check = _position(_DOCKERFILE, 'echo "${dbmate_sha256}  /usr/local/bin/dbmate" | sha256sum -c -')
    chmod = _position(_DOCKERFILE, "chmod +x /usr/local/bin/dbmate")
    assert select < download < check < chmod


def test_each_architecture_selects_its_own_digest_and_any_other_fails_the_build():
    _position(_DOCKERFILE, 'amd64) dbmate_sha256="$DBMATE_SHA256_AMD64" ;;')
    _position(_DOCKERFILE, 'arm64) dbmate_sha256="$DBMATE_SHA256_ARM64" ;;')
    _position(_DOCKERFILE, '*) echo "no pinned dbmate digest for $arch" >&2; exit 1 ;;')


def test_every_ci_install_matches_the_image_pin():
    version, digests = _image_pin()
    downloads = _CI_DOWNLOAD.findall(_CI)
    checks = _CI_CHECK.findall(_CI)
    assert downloads, "ci.yml installs dbmate"
    assert set(downloads) == {version}, f"CI installs {sorted(set(downloads))}, the image {version}"
    assert len(checks) == len(downloads), "every CI install verifies the digest"
    assert set(checks) == {digests["AMD64"]}, "CI verifies a different digest from the image's amd64 pin"
