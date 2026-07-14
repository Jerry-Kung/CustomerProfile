from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from src.config.settings import TosConfig

_log = logging.getLogger(__name__)


@dataclass
class TosClient:
    """Client for ByteDance Volcano Engine TOS (Object Storage Service)."""

    config: TosConfig

    def __post_init__(self) -> None:
        """Initialize the underlying TOS client."""
        try:
            import tos
        except ImportError as e:
            raise RuntimeError("tos package not installed. Please install it with 'pip install tos'.") from e

        self._tos_client = tos.TosClientV2(
            ak=self.config.access_key_id,
            sk=self.config.access_key_secret,
            endpoint=self.config.endpoint,
            region=self.config.region,
        )

    @staticmethod
    def generate_object_key(
        *,
        customer_phonenumber: str,
        slot: str,
        version: int,
        filename: str,
    ) -> str:
        """
        Generate object key according to the naming convention.

        Args:
            customer_phonenumber: Customer phone number
            slot: Logical image slot name
            version: Task version number
            filename: Original filename

        Returns:
            Object key in TOS
        """
        # Clean phone number (remove spaces)
        clean_phone = customer_phonenumber.replace(" ", "")
        # Build path
        return f"business_data/customer_profile/{clean_phone}/{slot}/{version}/{filename}"

    def upload_file(
        self,
        *,
        file_path: Path,
        object_key: str,
        content_type: str | None = None,
    ) -> str:
        """
        Upload a local file to TOS with private access.

        Args:
            file_path: Path to the local file
            object_key: Object key in TOS
            content_type: Optional content type (MIME type)

        Returns:
            The object key

        Raises:
            TosClientError: Client-side error
            TosServerError: Server-side error
        """
        import tos

        _log.info(
            "tos_upload_start bucket=%s object_key=%s file_path=%s",
            self.config.bucket,
            object_key,
            file_path,
        )

        try:
            self._tos_client.put_object_from_file(
                bucket=self.config.bucket,
                key=object_key,
                file_path=str(file_path),
            )
            _log.info(
                "tos_upload_success bucket=%s object_key=%s",
                self.config.bucket,
                object_key,
            )
            return object_key
        except tos.exceptions.TosClientError as e:
            _log.error(
                "tos_upload_client_error bucket=%s object_key=%s message=%s cause=%s",
                self.config.bucket,
                object_key,
                e.message,
                e.cause,
            )
            raise
        except tos.exceptions.TosServerError as e:
            _log.error(
                "tos_upload_server_error bucket=%s object_key=%s code=%s request_id=%s message=%s",
                self.config.bucket,
                object_key,
                e.code,
                e.request_id,
                e.message,
            )
            raise

    def generate_presigned_url(
        self,
        *,
        object_key: str,
        expires_in_seconds: int | None = None,
    ) -> str:
        """
        Generate a pre-signed URL for GET access to an object.

        Args:
            object_key: Object key in TOS
            expires_in_seconds: Expiration time in seconds (defaults to config value)

        Returns:
            Pre-signed URL

        Raises:
            TosClientError: Client-side error
            TosServerError: Server-side error
        """
        import tos

        expires = expires_in_seconds or self.config.presigned_url_expires_seconds

        _log.info(
            "tos_presigned_url_start bucket=%s object_key=%s expires=%d",
            self.config.bucket,
            object_key,
            expires,
        )

        try:
            out = self._tos_client.pre_signed_url(
                tos.HttpMethodType.Http_Method_Get,
                bucket=self.config.bucket,
                key=object_key,
                expires=expires,
            )
            _log.info(
                "tos_presigned_url_success bucket=%s object_key=%s",
                self.config.bucket,
                object_key,
            )
            return out.signed_url
        except tos.exceptions.TosClientError as e:
            _log.error(
                "tos_presigned_url_client_error bucket=%s object_key=%s message=%s cause=%s",
                self.config.bucket,
                object_key,
                e.message,
                e.cause,
            )
            raise
        except tos.exceptions.TosServerError as e:
            _log.error(
                "tos_presigned_url_server_error bucket=%s object_key=%s code=%s request_id=%s message=%s",
                self.config.bucket,
                object_key,
                e.code,
                e.request_id,
                e.message,
            )
            raise

    def object_exists(self, *, object_key: str) -> bool:
        """
        Check if an object exists in TOS.

        Args:
            object_key: Object key in TOS

        Returns:
            True if object exists, False otherwise

        Raises:
            TosClientError: Client-side error (except 404)
            TosServerError: Server-side error (except 404)
        """
        import tos

        try:
            self._tos_client.head_object(
                bucket=self.config.bucket,
                key=object_key,
            )
            return True
        except tos.exceptions.TosServerError as e:
            if e.status_code == 404:
                return False
            raise
