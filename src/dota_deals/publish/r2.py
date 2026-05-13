"""Cloudflare R2 client.

R2 is S3-compatible; we use boto3 with a custom endpoint. Two operations:

* :meth:`R2Client.download_db` — pulls the SQLite file into a local path
  at the start of a GHA run. If the object doesn't exist (first run ever
  in a new bucket), creates an empty local file and logs ``first_run``
  so the operator sees the bootstrap explicitly rather than silently
  losing state.

* :meth:`R2Client.upload_db` — pushes the local SQLite back at the end of
  a run. Done in two phases: upload to a sibling ``.tmp`` key, copy to
  the canonical key, delete the ``.tmp``. R2's server-side COPY publishes
  the new bytes atomically at the destination — the live DB key is never
  observed half-written.

Error model
-----------
Credential / configuration mistakes fail fast (no point retrying with
the wrong access key). Transient network and 5xx errors are retried in
a hand-rolled loop mirroring :mod:`dota_deals.ingest.steam` — exponential
backoff with jitter, bounded so a CI run that's actually broken doesn't
hang forever.

Exception types are distinct so callers can tell "the operator forgot
to set ``R2_ACCESS_KEY_ID``" apart from "Cloudflare is having a moment".
All derive from :class:`R2Error`.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)
from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.logging import get_logger

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

_CREDENTIAL_ERROR_CODES = frozenset({"InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied"})
_MAX_TRANSIENT_ATTEMPTS = 3
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 10.0

SleepFn = Callable[[float], None]


# ----------------------------- exceptions -------------------------------------


class R2Error(Exception):
    """Base for any R2-layer failure."""


class R2ConfigError(R2Error):
    """One or more required R2 settings are missing or empty."""


class R2CredentialsError(R2Error):
    """Authentication or authorization failed.

    Distinct from :class:`R2Error` so the CLI can refuse to retry — fixing
    this means rotating credentials, not waiting.
    """


class R2BucketMissing(R2Error):
    """The configured bucket does not exist."""


class R2ObjectMissing(R2Error):
    """A specific object key does not exist.

    :meth:`R2Client.download_db` catches this internally and writes an
    empty local file (first-run case); other callers can let it propagate.
    """


class R2TransientError(R2Error):
    """A network or server error that retries would normally fix.

    Raised after exhausting the retry budget.
    """


# ----------------------------- client -----------------------------------------


class R2Client:
    """Thin wrapper around boto3's S3 client, configured for Cloudflare R2.

    Construction validates the four required Settings fields; if any are
    missing the constructor raises :class:`R2ConfigError` so the failure
    happens at the CLI boundary, not deep inside an upload. ``sleep`` is
    injectable for deterministic test timing.
    """

    def __init__(self, settings: Settings, *, sleep: SleepFn | None = None) -> None:
        missing: list[str] = []
        if not settings.r2_endpoint:
            missing.append("R2_ENDPOINT")
        if not settings.r2_bucket:
            missing.append("R2_BUCKET")
        if not settings.r2_access_key_id:
            missing.append("R2_ACCESS_KEY_ID")
        if not settings.r2_secret_access_key:
            missing.append("R2_SECRET_ACCESS_KEY")
        if missing:
            raise R2ConfigError(f"R2 client requires {', '.join(missing)} in environment / .env")

        # The asserts narrow Optional[str] → str for mypy; the runtime
        # check above already guarantees non-None.
        assert settings.r2_endpoint is not None
        assert settings.r2_access_key_id is not None
        assert settings.r2_secret_access_key is not None
        assert settings.r2_bucket is not None

        self._bucket: str = settings.r2_bucket
        self._db_key: str = settings.r2_db_key
        self._sleep: SleepFn = sleep if sleep is not None else time.sleep
        self._log = get_logger("dota_deals.publish.r2")
        self._s3: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
        )

    def download_db(self, local_path: Path) -> None:
        """Download the SQLite DB to ``local_path``.

        If the object doesn't exist (first run in a fresh bucket), creates
        an empty file at ``local_path`` and logs ``first_run=True`` so the
        operator sees the bootstrap explicitly rather than wondering why
        the pipeline started cold.

        :raises R2CredentialsError: if auth fails.
        :raises R2BucketMissing: if the bucket doesn't exist.
        :raises R2TransientError: if a transient error persists after retries.
        """
        log = self._log.bind(bucket=self._bucket, key=self._db_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._with_retries(
                lambda: self._s3.download_file(self._bucket, self._db_key, str(local_path)),
                op_name="download_db",
            )
            log.info("R2 download complete", local_path=str(local_path))
        except R2ObjectMissing:
            local_path.write_bytes(b"")
            log.info(
                "R2 object missing; created empty local DB",
                local_path=str(local_path),
                first_run=True,
            )

    def upload_db(self, local_path: Path) -> None:
        """Upload ``local_path`` to R2 atomically.

        Writes to a ``.tmp`` sibling key first, then server-side copies to
        the canonical key, then deletes the ``.tmp``. boto3's transfer
        manager picks multipart automatically once the file exceeds the
        S3 default threshold (8 MiB) — no manual gate needed.

        :raises FileNotFoundError: if ``local_path`` doesn't exist.
        :raises R2CredentialsError: if auth fails.
        :raises R2BucketMissing: if the bucket doesn't exist.
        :raises R2TransientError: if a transient error persists after retries.
        """
        if not local_path.exists():
            raise FileNotFoundError(f"upload_db: local file not found: {local_path}")

        tmp_key = f"{self._db_key}.tmp"
        log = self._log.bind(bucket=self._bucket, key=self._db_key, tmp_key=tmp_key)

        self._with_retries(
            lambda: self._s3.upload_file(str(local_path), self._bucket, tmp_key),
            op_name="upload_db.upload_tmp",
        )
        log.info("R2 upload-to-tmp complete")

        self._with_retries(
            lambda: self._s3.copy_object(
                Bucket=self._bucket,
                CopySource={"Bucket": self._bucket, "Key": tmp_key},
                Key=self._db_key,
            ),
            op_name="upload_db.copy_to_final",
        )
        log.info("R2 copy-tmp-to-final complete")

        # Best-effort cleanup. A leftover .tmp is not a correctness problem
        # (the canonical key is already updated); log and continue.
        try:
            self._s3.delete_object(Bucket=self._bucket, Key=tmp_key)
        except (ClientError, BotoCoreError) as e:
            log.warning("R2 tmp cleanup failed (non-fatal)", error=str(e))

    # ------------------------------------------------------------------ internals

    def _with_retries(self, op: Callable[[], object], *, op_name: str) -> None:
        """Run ``op`` once; retry on transient failures; map boto errors to ours.

        Hand-rolled loop (vs. tenacity) for the same reason as
        ``ingest.steam._get_json``: branching policy per error class is
        cleaner imperative than declarative.
        """
        log = self._log.bind(op=op_name)
        attempt = 0
        while True:
            attempt += 1
            try:
                op()
                return
            except NoCredentialsError as e:
                log.error("R2 credentials missing", error=str(e))
                raise R2CredentialsError(f"R2 credentials not configured for {op_name}: {e}") from e
            except S3UploadFailedError as e:
                # boto3's transfer manager wraps a ClientError into this;
                # the inner is chained via __context__.
                inner = e.__context__
                if isinstance(inner, ClientError):
                    if self._classify_client_error(inner, attempt=attempt, log=log):
                        continue
                    return  # unreachable; classifier raised on non-retry
                raise R2Error(f"R2 upload failed in {op_name}: {e}") from e
            except ClientError as e:
                if self._classify_client_error(e, attempt=attempt, log=log):
                    continue
                return  # unreachable; classifier raised on non-retry
            except EndpointConnectionError as e:
                if attempt < _MAX_TRANSIENT_ATTEMPTS:
                    self._backoff(attempt, log, reason="connection_error")
                    continue
                raise R2TransientError(f"R2 connection failed after {attempt} attempts: {e}") from e
            except BotoCoreError as e:
                # Catch-all for the rest of botocore's exception hierarchy
                # (DataNotFoundError, ParamValidationError, etc.) — none are
                # transient in any way we can fix by retrying.
                raise R2Error(f"R2 boto-core error in {op_name}: {e}") from e

    def _classify_client_error(self, e: ClientError, *, attempt: int, log: BoundLogger) -> bool:
        """Map a boto ``ClientError`` to the right R2 exception or signal retry.

        Returns ``True`` to tell the caller to ``continue`` the retry loop;
        otherwise raises the appropriate :class:`R2Error` subclass.
        """
        code = str(e.response.get("Error", {}).get("Code", ""))
        status = int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        if code in _CREDENTIAL_ERROR_CODES:
            log.error("R2 credentials rejected", code=code, status=status)
            raise R2CredentialsError(f"R2 rejected credentials ({code})") from e
        if code == "NoSuchBucket":
            raise R2BucketMissing(f"R2 bucket {self._bucket!r} does not exist") from e
        if code in ("NoSuchKey", "404") or status == 404:
            raise R2ObjectMissing(
                f"R2 object {self._db_key!r} not found in {self._bucket!r}"
            ) from e
        if 500 <= status < 600 and attempt < _MAX_TRANSIENT_ATTEMPTS:
            self._backoff(attempt, log, reason=f"5xx {status}")
            return True
        if 500 <= status < 600:
            raise R2TransientError(f"R2 5xx after {attempt} attempts: {code} ({status})") from e
        raise R2Error(f"R2 unexpected client error: code={code} status={status}") from e

    def _backoff(self, attempt: int, log: BoundLogger, *, reason: str) -> None:
        base = _INITIAL_BACKOFF_S * float(2 ** (attempt - 1))
        jitter = random.uniform(0.0, base * 0.25)
        delay = min(base + jitter, _MAX_BACKOFF_S)
        log.warning(
            "R2 transient failure, retrying",
            reason=reason,
            attempt=attempt,
            backoff_s=delay,
        )
        self._sleep(delay)
