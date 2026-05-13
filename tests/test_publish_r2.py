"""Tests for :mod:`dota_deals.publish.r2`.

Uses moto's S3 mock to avoid a real network. Each test starts a fresh
mock environment; nothing leaks between tests. The atomic-upload test
asserts the .tmp → COPY → delete sequence is actually what happens —
the rename pattern is the whole point of having the client.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from dota_deals.config import Settings
from dota_deals.publish.r2 import (
    R2BucketMissing,
    R2Client,
    R2ConfigError,
    R2CredentialsError,
)

_BUCKET = "dota-deals-test"
_ENDPOINT = "https://example.r2.cloudflarestorage.com"
_KEY = "dota_deals.db"


@pytest.fixture(autouse=True)
def _configure_moto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tell moto to intercept requests to the R2 endpoint.

    moto v5 only mocks recognized AWS-shaped hosts by default;
    Cloudflare's custom endpoint needs to be added explicitly via env var.
    """
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", _ENDPOINT)


def _settings_with_r2(db_path: Path) -> Settings:
    return Settings(
        db_path=db_path,
        r2_endpoint=_ENDPOINT,
        r2_bucket=_BUCKET,
        r2_access_key_id="test-access-key",
        r2_secret_access_key="test-secret-key",
        r2_db_key=_KEY,
    )


def _bucket_setup() -> None:
    """Create the test bucket inside the active moto mock.

    Uses ``region_name="us-east-1"`` for the setup client because moto's
    ``CreateBucket`` validates regions more strictly than real R2 does
    (R2 wants ``"auto"`` but moto rejects unknown locations). The
    production ``R2Client`` still uses ``"auto"`` for actual operations —
    those don't go through CreateBucket's location check.
    """
    client = boto3.client(
        "s3",
        endpoint_url=_ENDPOINT,
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="us-east-1",
    )
    client.create_bucket(Bucket=_BUCKET)


# ----------------------------- configuration ----------------------------------


def test_missing_credentials_raises_config_error(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "x.db")  # no R2 fields set
    with pytest.raises(R2ConfigError) as exc_info:
        R2Client(settings)
    msg = str(exc_info.value)
    assert "R2_ENDPOINT" in msg
    assert "R2_BUCKET" in msg


# ----------------------------- download_db ------------------------------------


@mock_aws
def test_download_db_happy_path(tmp_path: Path) -> None:
    _bucket_setup()
    s3 = boto3.client(
        "s3",
        endpoint_url=_ENDPOINT,
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="auto",
    )
    s3.put_object(Bucket=_BUCKET, Key=_KEY, Body=b"hello sqlite")

    local = tmp_path / "dota_deals.db"
    R2Client(_settings_with_r2(local)).download_db(local)

    assert local.read_bytes() == b"hello sqlite"


@mock_aws
def test_download_db_missing_object_creates_empty_file(tmp_path: Path) -> None:
    """First-run case: object missing → empty local file, no exception."""
    _bucket_setup()
    local = tmp_path / "subdir" / "dota_deals.db"
    R2Client(_settings_with_r2(local)).download_db(local)

    assert local.exists()
    assert local.read_bytes() == b""


@mock_aws
def test_download_db_missing_bucket_raises(tmp_path: Path) -> None:
    """The bucket isn't created, so the download should surface
    R2BucketMissing (or R2ObjectMissing — moto returns NoSuchBucket here)."""
    local = tmp_path / "dota_deals.db"
    with pytest.raises((R2BucketMissing, R2CredentialsError)):
        # moto sometimes maps a missing bucket to 403 instead of 404; the
        # client surfaces it as either R2BucketMissing or R2CredentialsError
        # depending on what botocore translates the error to.
        R2Client(_settings_with_r2(local)).download_db(local)


# ----------------------------- upload_db --------------------------------------


@mock_aws
def test_upload_db_writes_object(tmp_path: Path) -> None:
    _bucket_setup()
    local = tmp_path / "dota_deals.db"
    local.write_bytes(b"sqlite content")

    R2Client(_settings_with_r2(local)).upload_db(local)

    s3 = boto3.client(
        "s3",
        endpoint_url=_ENDPOINT,
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="auto",
    )
    body = s3.get_object(Bucket=_BUCKET, Key=_KEY)["Body"].read()
    assert body == b"sqlite content"


@mock_aws
def test_upload_db_atomic_no_leftover_tmp(tmp_path: Path) -> None:
    """After a successful upload there should be exactly one object — the
    canonical key. The .tmp sibling must be gone.
    """
    _bucket_setup()
    local = tmp_path / "dota_deals.db"
    local.write_bytes(b"x" * 100)

    R2Client(_settings_with_r2(local)).upload_db(local)

    s3 = boto3.client(
        "s3",
        endpoint_url=_ENDPOINT,
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        region_name="auto",
    )
    keys = [obj["Key"] for obj in s3.list_objects_v2(Bucket=_BUCKET).get("Contents", [])]
    assert keys == [_KEY]


@mock_aws
def test_upload_db_does_intermediate_tmp_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The atomic recipe: upload to .tmp, COPY to canonical, delete .tmp.

    We verify the COPY happened by intercepting the boto3 client's
    ``copy_object`` and recording the source key — if the implementation
    ever regressed to a direct PUT, this would fail.
    """
    _bucket_setup()
    local = tmp_path / "dota_deals.db"
    local.write_bytes(b"x")

    client = R2Client(_settings_with_r2(local))

    real_copy = client._s3.copy_object
    captured: list[dict[str, object]] = []

    def recording_copy(**kwargs: object) -> object:
        captured.append(dict(kwargs))
        return real_copy(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(client._s3, "copy_object", recording_copy)

    client.upload_db(local)
    assert len(captured) == 1
    source = captured[0]["CopySource"]
    assert isinstance(source, dict)
    assert source["Key"] == f"{_KEY}.tmp"


@mock_aws
def test_upload_db_missing_local_file_raises(tmp_path: Path) -> None:
    _bucket_setup()
    local = tmp_path / "does_not_exist.db"
    with pytest.raises(FileNotFoundError):
        R2Client(_settings_with_r2(local)).upload_db(local)


@mock_aws
def test_upload_db_missing_bucket_raises(tmp_path: Path) -> None:
    """No bucket setup → upload fails fast with R2BucketMissing/credentials.

    moto's behavior here can be either NoSuchBucket or 403 AccessDenied
    depending on version; both are non-transient, neither should retry,
    and the test accepts either.
    """
    local = tmp_path / "dota_deals.db"
    local.write_bytes(b"x")
    with pytest.raises((R2BucketMissing, R2CredentialsError)):
        R2Client(_settings_with_r2(local)).upload_db(local)


# ----------------------------- error mapping (no moto) ------------------------


def test_client_error_with_invalid_key_maps_to_credentials_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct unit test of the error mapping: an InvalidAccessKeyId
    ClientError surfaces as R2CredentialsError, not the generic R2Error.

    Skips the moto layer because moto doesn't reliably emit this code.
    """
    local = tmp_path / "dota_deals.db"
    local.write_bytes(b"x")
    sleeps: list[float] = []
    client = R2Client(_settings_with_r2(local), sleep=sleeps.append)

    def raise_creds(*_args: object, **_kwargs: object) -> None:
        # The typeshed ResponseMetadata TypedDict demands fields we don't
        # care about for an error-mapping test; build the dict dynamically
        # so mypy doesn't try to validate it.
        error_response: dict[str, object] = {
            "Error": {"Code": "InvalidAccessKeyId", "Message": "bad key"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        }
        raise ClientError(error_response, "PutObject")  # type: ignore[arg-type]

    monkeypatch.setattr(client._s3, "upload_file", raise_creds)
    with pytest.raises(R2CredentialsError):
        client.upload_db(local)
    assert sleeps == []  # credential errors must not retry
