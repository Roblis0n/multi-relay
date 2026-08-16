#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import socket
import ssl
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.failure import (  # noqa: E402
    MAX_ERROR_BODY_BYTES,
    FailureClass,
    classify_http_failure,
    classify_transport_failure,
    parse_retry_after,
)
from multi_relay.protocols.base import ProviderErrorMetadata  # noqa: E402


class RecordingBody(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class FailureClassificationTests(unittest.TestCase):
    def test_deepseek_insufficient_balance_is_quota_exhausted(self) -> None:
        failure = classify_http_failure(
            402,
            b'{"error":{"code":"invalid_request_error","message":"Insufficient Balance"}}',
            headers={"Content-Type": "application/json"},
            provider_id="deepseek",
        )

        self.assertEqual(failure.failure_class, FailureClass.QUOTA_EXHAUSTED)
        self.assertTrue(failure.retry.failover_allowed)
        self.assertFalse(failure.retry.retry_same_target)

    def test_openai_compatible_rate_limit_honors_retry_after_seconds(self) -> None:
        failure = classify_http_failure(
            429,
            b'{"error":{"type":"rate_limit_error","message":"slow down"}}',
            headers={"content-type": "application/json", "retry-after": "12"},
            provider_id="openai-compatible",
        )

        self.assertEqual(failure.failure_class, FailureClass.RATE_LIMITED)
        self.assertEqual(failure.retry.retry_after_seconds, 12.0)
        self.assertTrue(failure.retry.retry_same_target)
        self.assertTrue(failure.retry.failover_allowed)

    def test_retry_after_http_date_uses_injected_utc_clock(self) -> None:
        now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

        seconds = parse_retry_after("Sun, 16 Aug 2026 12:00:45 GMT", now=now)

        self.assertEqual(seconds, 45.0)
        self.assertEqual(
            parse_retry_after("Sun, 16 Aug 2026 11:59:00 GMT", now=now),
            0.0,
        )
        self.assertIsNone(parse_retry_after("not-a-date", now=now))

    def test_anthropic_rate_limit_envelope_extracts_stable_type(self) -> None:
        failure = classify_http_failure(
            429,
            b'{"type":"error","error":{"type":"rate_limit_error","message":"limited"}}',
            headers={"Content-Type": "application/json; charset=utf-8"},
            provider_id="anthropic",
        )

        self.assertEqual(failure.failure_class, FailureClass.RATE_LIMITED)
        self.assertEqual(failure.code, "rate_limit_error")

    def test_specific_quota_code_beats_rate_limit_status(self) -> None:
        failure = classify_http_failure(
            429,
            b"{}",
            headers={"Content-Type": "application/json"},
            provider_error=ProviderErrorMetadata(code="insufficient_quota"),
        )

        self.assertEqual(failure.failure_class, FailureClass.QUOTA_EXHAUSTED)
        self.assertFalse(failure.retry.retry_same_target)

    def test_auth_status_disables_only_the_failed_credential(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                failure = classify_http_failure(status, b"", provider_id="vendor")
                self.assertEqual(failure.failure_class, FailureClass.AUTH_INVALID)
                self.assertTrue(failure.retry.disable_credential)
                self.assertFalse(failure.retry.retry_same_target)
                self.assertTrue(failure.retry.failover_allowed)

    def test_model_not_found_code_beats_generic_status(self) -> None:
        metadata = ProviderErrorMetadata(
            code="model_not_found",
            message="requested model is unavailable",
        )

        failure = classify_http_failure(
            400,
            b"{}",
            headers={"Content-Type": "application/json"},
            provider_error=metadata,
        )

        self.assertEqual(failure.failure_class, FailureClass.MODEL_UNAVAILABLE)

    def test_model_status_and_non_retryable_request_statuses_are_stable(self) -> None:
        self.assertEqual(
            classify_http_failure(404, b"").failure_class,
            FailureClass.MODEL_UNAVAILABLE,
        )
        for status in (400, 413, 422):
            with self.subTest(status=status):
                failure = classify_http_failure(status, b"")
                self.assertEqual(failure.failure_class, FailureClass.REQUEST_INVALID)
                self.assertFalse(failure.retry.failover_allowed)

    def test_rate_limit_without_retry_after_fails_over_without_same_target_wait(self) -> None:
        failure = classify_http_failure(429, b"")

        self.assertEqual(failure.failure_class, FailureClass.RATE_LIMITED)
        self.assertIsNone(failure.retry.retry_after_seconds)
        self.assertFalse(failure.retry.retry_same_target)
        self.assertTrue(failure.retry.failover_allowed)

    def test_transient_server_statuses_are_provider_unavailable(self) -> None:
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                failure = classify_http_failure(status, b"")
                self.assertEqual(
                    failure.failure_class,
                    FailureClass.PROVIDER_UNAVAILABLE,
                )
                self.assertTrue(failure.retry.retry_same_target)
                self.assertTrue(failure.retry.failover_allowed)

    def test_transport_failures_are_normalized_without_raw_exception_text(self) -> None:
        secret = "sk-transport-secret"
        errors = (
            socket.gaierror(f"dns leaked {secret}"),
            ConnectionRefusedError(f"refused {secret}"),
            ssl.SSLError(f"tls leaked {secret}"),
            TimeoutError(f"timeout leaked {secret}"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                failure = classify_transport_failure(
                    error,
                    provider_id="vendor",
                    secrets=(secret,),
                )
                self.assertEqual(
                    failure.failure_class,
                    FailureClass.PROVIDER_UNAVAILABLE,
                )
                self.assertNotIn(secret, failure.message)
                self.assertNotIn(secret, repr(dict(failure.details)))

    def test_invalid_request_context_and_policy_are_not_failover_candidates(self) -> None:
        cases = (
            (
                ProviderErrorMetadata(code="invalid_request_error"),
                FailureClass.REQUEST_INVALID,
            ),
            (
                ProviderErrorMetadata(code="context_length_exceeded"),
                FailureClass.CONTEXT_EXCEEDED,
            ),
            (
                ProviderErrorMetadata(code="content_policy_violation"),
                FailureClass.POLICY_BLOCKED,
            ),
        )
        for metadata, expected in cases:
            with self.subTest(code=metadata.code):
                failure = classify_http_failure(
                    400,
                    b"{}",
                    headers={"Content-Type": "application/json"},
                    provider_error=metadata,
                )
                self.assertEqual(failure.failure_class, expected)
                self.assertFalse(failure.retry.retry_same_target)
                self.assertFalse(failure.retry.failover_allowed)

    def test_malformed_json_is_a_protocol_error_when_no_status_rule_applies(self) -> None:
        failure = classify_http_failure(
            418,
            b'{"error":',
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(failure.failure_class, FailureClass.PROTOCOL_ERROR)
        self.assertTrue(failure.retry.failover_allowed)
        self.assertTrue(failure.details["malformed_body"])

    def test_error_body_read_is_bounded_and_reports_truncation(self) -> None:
        body = RecordingBody(b"x" * (MAX_ERROR_BODY_BYTES + 100))

        failure = classify_http_failure(
            418,
            body,
            headers={"Content-Type": "text/plain"},
        )

        self.assertEqual(body.read_sizes, [MAX_ERROR_BODY_BYTES + 1])
        self.assertEqual(failure.failure_class, FailureClass.PROTOCOL_ERROR)
        self.assertTrue(failure.details["body_truncated"])

    def test_unknown_content_type_is_a_protocol_error_without_echoing_body(self) -> None:
        secret = "sk-content-type-secret"

        failure = classify_http_failure(
            418,
            f"binary-ish {secret}".encode(),
            headers={"Content-Type": "application/octet-stream"},
            secrets=(secret,),
        )

        self.assertEqual(failure.failure_class, FailureClass.PROTOCOL_ERROR)
        self.assertEqual(failure.details["content_type"], "application/octet-stream")
        self.assertNotIn(secret, failure.message)
        self.assertNotIn(secret, repr(dict(failure.details)))

    def test_provider_body_and_metadata_are_redacted(self) -> None:
        secret = "sk-provider-secret"
        metadata = ProviderErrorMetadata(
            code="unknown_vendor_error",
            message=f"upstream repeated {secret}",
            details={"diagnostic": f"credential={secret}"},
        )

        failure = classify_http_failure(
            418,
            f'{{"error":{{"message":"{secret}"}}}}'.encode(),
            headers={"Content-Type": "application/json"},
            provider_error=metadata,
            secrets=(secret,),
        )

        self.assertNotIn(secret, failure.message)
        self.assertNotIn(secret, failure.code)
        self.assertNotIn(secret, repr(dict(failure.details)))

    def test_secret_shaped_provider_text_is_redacted_even_without_a_secret_hint(self) -> None:
        secret = "sk-automatic-redaction"
        failure = classify_http_failure(
            418,
            b"{}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
            },
            provider_error=ProviderErrorMetadata(
                code="unknown_vendor_error",
                message=f"provider repeated {secret}",
                details={
                    "authorization": f"Bearer {secret}",
                    "api_key": "vendor-token-without-a-prefix",
                },
            ),
        )

        serialized = json.dumps(failure.to_dict(), ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("vendor-token-without-a-prefix", serialized)
        self.assertNotIn("Authorization", serialized)

    def test_normalized_failure_is_deterministic_and_json_safe(self) -> None:
        arguments = {
            "status": 503,
            "body": b'{"error":{"type":"overloaded_error"}}',
            "headers": {"Content-Type": "application/json"},
            "provider_id": "anthropic",
        }

        first = classify_http_failure(**arguments)
        second = classify_http_failure(**arguments)

        self.assertEqual(first, second)
        self.assertEqual(
            json.loads(json.dumps(first.to_dict()))["failure_class"],
            "provider_unavailable",
        )

    def test_classification_priority_is_code_then_status_then_finite_pattern(self) -> None:
        code_wins = classify_http_failure(
            429,
            b"{}",
            headers={"Content-Type": "application/json"},
            provider_error=ProviderErrorMetadata(code="context_length_exceeded"),
        )
        status_wins = classify_http_failure(
            401,
            b'{"error":{"message":"insufficient balance"}}',
            headers={"Content-Type": "application/json"},
        )
        pattern_fallback = classify_http_failure(
            418,
            b'{"error":{"message":"Insufficient Balance"}}',
            headers={"Content-Type": "application/json"},
        )
        broad_text_is_not_quota = classify_http_failure(
            418,
            b'{"error":{"message":"quota tracking metadata is invalid"}}',
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(code_wins.failure_class, FailureClass.CONTEXT_EXCEEDED)
        self.assertEqual(status_wins.failure_class, FailureClass.AUTH_INVALID)
        self.assertEqual(pattern_fallback.failure_class, FailureClass.QUOTA_EXHAUSTED)
        self.assertEqual(broad_text_is_not_quota.failure_class, FailureClass.PROTOCOL_ERROR)

    def test_committed_failure_is_never_replayed_or_resumed(self) -> None:
        failure = classify_http_failure(
            402,
            b'{"error":{"code":"insufficient_quota"}}',
            headers={"Content-Type": "application/json"},
            committed=True,
        )

        self.assertTrue(failure.committed)
        self.assertFalse(failure.resumable)
        self.assertFalse(failure.retry.retry_same_target)
        self.assertFalse(failure.retry.failover_allowed)
        self.assertFalse(failure.details["resumable"])


if __name__ == "__main__":
    unittest.main()
