"""Signing credentialed requests.

The module is inert on purpose, so most of these test the refusals rather than
the signatures. A signer that works is worth less here than a signer that will
not run on a scheme nobody has confirmed: the last time this address made
malformed requests at volume, it was blocked for a day and counting.
"""

from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from arbbot.venues.kalshi import auth
from arbbot.venues.kalshi.auth import (
    KalshiCredentials,
    UnverifiedSigningScheme,
    load_private_key,
)

T0 = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def credentials(key: rsa.RSAPrivateKey) -> KalshiCredentials:
    return KalshiCredentials(key_id="test-key-id", private_key=key)


class TestVerificationGate:
    def test_signing_is_refused_while_the_scheme_is_unconfirmed(
        self, credentials: KalshiCredentials
    ) -> None:
        """A malformed signature is a stream of rejected requests from an
        address whose last problem was exactly that."""
        assert auth.SCHEME_VERIFIED is False
        with pytest.raises(UnverifiedSigningScheme, match="version control"):
            credentials.sign(method="GET", path="/trade-api/v2/portfolio/balance")

    def test_the_payload_can_still_be_inspected(self, credentials: KalshiCredentials) -> None:
        """Building the string to be signed is not signing, and being able to
        check it without arming anything is the point of separating them."""
        payload = credentials.signing_payload(
            timestamp_ms=1_700_000_000_000, method="get", path="/trade-api/v2/markets"
        )
        assert payload == b"1700000000000GET/trade-api/v2/markets"


class TestSigning:
    """Exercised with the gate lifted, so the scheme is pinned even though
    nothing may use it yet. If the venue confirms a different construction,
    these fail loudly rather than the venue failing quietly."""

    def test_the_headers_verify_against_the_public_key(
        self, credentials: KalshiCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(auth, "SCHEME_VERIFIED", True)
        headers = credentials.sign(method="GET", path="/trade-api/v2/markets", now=T0)

        expected = f"{int(T0.timestamp() * 1000)}GET/trade-api/v2/markets".encode()
        credentials.private_key.public_key().verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
            expected,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def test_the_sent_timestamp_is_the_signed_one(
        self, credentials: KalshiCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sending a timestamp other than the one signed fails at the venue in
        a way that reads as a bad key rather than as a bug here."""
        monkeypatch.setattr(auth, "SCHEME_VERIFIED", True)
        headers = credentials.sign(method="GET", path="/x", now=T0)
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == str(int(T0.timestamp() * 1000))

    def test_the_key_id_is_sent(
        self, credentials: KalshiCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(auth, "SCHEME_VERIFIED", True)
        assert credentials.sign(method="GET", path="/x", now=T0)["KALSHI-ACCESS-KEY"] == (
            "test-key-id"
        )

    def test_a_different_path_produces_a_different_signature(
        self, credentials: KalshiCredentials, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Signing the path without its prefix, or without the query the
        transport will add, is the classic way to spend an hour blaming the
        venue for a credential that is fine."""
        monkeypatch.setattr(auth, "SCHEME_VERIFIED", True)
        a = credentials.sign(method="GET", path="/trade-api/v2/markets", now=T0)
        b = credentials.sign(method="GET", path="/markets", now=T0)
        assert a["KALSHI-ACCESS-SIGNATURE"] != b["KALSHI-ACCESS-SIGNATURE"]

    def test_the_method_is_case_normalised(self, credentials: KalshiCredentials) -> None:
        lower = credentials.signing_payload(timestamp_ms=1, method="get", path="/x")
        upper = credentials.signing_payload(timestamp_ms=1, method="GET", path="/x")
        assert lower == upper


class TestKeyLoading:
    def test_an_rsa_key_loads(self, key: rsa.RSAPrivateKey, tmp_path: Path) -> None:
        path = tmp_path / "key.pem"
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        assert isinstance(load_private_key(path), rsa.RSAPrivateKey)

    def test_a_non_rsa_key_is_refused(self, tmp_path: Path) -> None:
        """An EC key would fail at signing time with an error that looks like
        the venue rejecting the credential."""
        from cryptography.hazmat.primitives.asymmetric import ec

        path = tmp_path / "ec.pem"
        path.write_bytes(
            ec.generate_private_key(ec.SECP256R1()).private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        with pytest.raises(ValueError, match="RSA-PSS"):
            load_private_key(path)
