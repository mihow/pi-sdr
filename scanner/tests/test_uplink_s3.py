"""The S3 uplink target: a SigV4 signer checked against the AWS documented example, and
one real round trip against MinIO in Docker (skipped when Docker or the image is absent)."""

from __future__ import annotations

import gzip
import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

from sdrscan.config import UplinkConfig
from sdrscan.store import Store
from sdrscan.uplink import DigestPublisher, Ledger, S3Target, load_secrets, make_target, sign_v4

MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2024-11-07T00-52-20Z"
# Test-only credentials for a throwaway local MinIO container.
MINIO_USER, MINIO_PASS = "sdrscan-test", "sdrscan-test-password"


def test_sign_v4_matches_the_aws_documented_get_object_example():
    """AWS S3 API reference, "Signature Calculations for the Authorization Header",
    example GET Object (examplebucket, /test.txt, Range bytes=0-9, 2013-05-24)."""
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    auth = sign_v4(
        method="GET",
        path="/test.txt",
        headers={
            "host": "examplebucket.s3.amazonaws.com",
            "range": "bytes=0-9",
            "x-amz-content-sha256": empty,
            "x-amz-date": "20130524T000000Z",
        },
        payload_sha256=empty,
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
    )
    assert auth == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_missing_s3_settings_are_named_in_the_error(tmp_path):
    env = tmp_path / "uplink.env"
    env.write_text("SDRSCAN_S3_BUCKET=b\n")
    with pytest.raises(ValueError) as e:
        make_target(UplinkConfig(target="s3", secrets_env=env))
    msg = str(e.value)
    assert "SDRSCAN_S3_ENDPOINT" in msg and "SDRSCAN_S3_SECRET_KEY" in msg
    assert "SDRSCAN_S3_BUCKET" not in msg


# -- MinIO round trip --------------------------------------------------------------------
def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", MINIO_IMAGE], capture_output=True)
    return r.returncode == 0


@pytest.fixture(scope="module")
def minio():
    if not _docker_ok():
        pytest.skip(f"docker or the {MINIO_IMAGE} image is not available")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"sdrscan-minio-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:9000",
         "-e", f"MINIO_ROOT_USER={MINIO_USER}", "-e", f"MINIO_ROOT_PASSWORD={MINIO_PASS}",
         MINIO_IMAGE, "server", "/data"],
        check=True, capture_output=True,
    )  # fmt: skip
    endpoint = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{endpoint}/minio/health/live", timeout=1).close()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            pytest.fail("MinIO did not become ready")
        yield endpoint
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _env(tmp_path, endpoint, secret=MINIO_PASS):
    env = tmp_path / "uplink.env"
    env.write_text(
        f"SDRSCAN_S3_ENDPOINT={endpoint}\nSDRSCAN_S3_BUCKET=rig\nSDRSCAN_S3_PREFIX=site-a\n"
        f"SDRSCAN_S3_ACCESS_KEY={MINIO_USER}\nSDRSCAN_S3_SECRET_KEY={secret}\n"
    )
    return env


def _get(target: S3Target, key: str):
    req = target.signed_request("GET", f"/{target.bucket}/{key}", b"", {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(), dict(r.headers)


@pytest.mark.docker
def test_digest_and_page_round_trip_through_minio(minio, tmp_path, vclock):
    """The real off-site path: sign, upload, and read back what a browser would get."""
    vclock.set(time.time())  # SigV4 rejects requests more than 15 minutes off
    target = make_target(UplinkConfig(target="s3", secrets_env=_env(tmp_path, minio)))
    assert isinstance(target, S3Target)
    with urllib.request.urlopen(target.signed_request("PUT", "/rig", b"", {}), timeout=10):
        pass  # create the bucket
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    ucfg = UplinkConfig(target="s3")
    ledger = Ledger(store, ucfg, vclock)
    pub = DigestPublisher(target, ledger, tmp_path / "outbox", vclock, ucfg)
    page = b"<!doctype html><title>t</title><p>hello</p>"
    assert pub.publish_files({"index.html": page}) == ["index.html"]
    res = pub.publish({"v": 1, "generated_at": int(vclock.now())}, vclock.now())
    assert res.status == "sent", res
    body, headers = _get(target, "site-a/digest.json.gz")
    assert headers.get("Content-Encoding") == "gzip"  # S3 replays the stored encoding
    assert json.loads(gzip.decompress(body))["v"] == 1
    body, headers = _get(target, "site-a/index.html")
    assert headers["Content-Type"].startswith("text/html")
    plain = body if headers.get("Content-Encoding") != "gzip" else gzip.decompress(body)
    assert plain == page
    assert ledger.month_bytes() > len(page)
    store.close()


@pytest.mark.docker
def test_a_wrong_secret_fails_the_push_without_raising(minio, tmp_path, vclock):
    vclock.set(time.time())
    env = _env(tmp_path, minio, secret="not-the-secret")
    target = S3Target.from_secrets(load_secrets(env))
    store = Store.open(tmp_path / "scan.sqlite", vclock)
    store.migrate()
    ucfg = UplinkConfig(target="s3")
    pub = DigestPublisher(target, Ledger(store, ucfg, vclock), tmp_path / "ob", vclock, ucfg)
    assert pub.publish({"v": 1}, vclock.now()).status == "failed"
    store.close()
