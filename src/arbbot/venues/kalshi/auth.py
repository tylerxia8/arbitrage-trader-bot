"""Signing credentialed requests to the venue.

Built, tested, and **inert**. Nothing in the system reaches it yet, and it
refuses to sign until a human has confirmed the scheme against the venue's
current documentation. That is the same discipline the fee schedule follows,
and for the same reason: a transcription that is probably right is not a fact,
and the cost of being wrong here is worse than a wrong number. A malformed
signature is a stream of rejected requests from an address that has just been
unblocked for exactly that kind of behaviour.

The scheme as understood: sign ``timestamp_ms + METHOD + path`` with RSA-PSS
over SHA-256, using the largest salt the key allows, and send

* ``KALSHI-ACCESS-KEY`` -- the API key id
* ``KALSHI-ACCESS-SIGNATURE`` -- the base64 signature
* ``KALSHI-ACCESS-TIMESTAMP`` -- the same millisecond timestamp that was signed

Three deliberate constraints.

**The private key never comes from an environment variable inline.** It is read
from a file whose path is configured. Environment variables leak into process
listings, crash dumps, and every log line that dumps the environment; a key
that has leaked is not a key. The configuration layer accepts a PEM directly
for tests only, which is why the loader here takes a path.

**The signed path must be exactly what is sent.** Signing ``/orderbook`` and
requesting ``/orderbook?depth=0`` fails at the venue, and the failure looks
like a credential problem rather than a construction problem, which is a very
expensive hour. The signer takes the path it is given and the caller is
responsible for handing it the same string the transport will use.

**Timestamps are milliseconds, and skew is the caller's problem to notice.** A
clock that drifts produces authentication failures that look exactly like a bad
key.
"""

from __future__ import annotations

import base64
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

__all__ = [
    "SCHEME_VERIFIED",
    "KalshiCredentials",
    "UnverifiedSigningScheme",
    "load_private_key",
]

#: Whether a human has confirmed this signing scheme against the venue's own
#: current documentation.
#:
#: ``False`` until someone does. Flipping it is a code change so the decision
#: lands in version control with a name attached, exactly as the fee schedule's
#: ``verified`` flag works. The scheme here was written from memory of the
#: venue's API docs while this address was blocked and unable to check.
SCHEME_VERIFIED: Final = False

_HEADER_KEY: Final = "KALSHI-ACCESS-KEY"
_HEADER_SIGNATURE: Final = "KALSHI-ACCESS-SIGNATURE"
_HEADER_TIMESTAMP: Final = "KALSHI-ACCESS-TIMESTAMP"


class UnverifiedSigningScheme(RuntimeError):
    """Signing was attempted before anyone confirmed the scheme."""


def load_private_key(path: Path, *, password: bytes | None = None) -> rsa.RSAPrivateKey:
    """Read an RSA private key from disk.

    From a file rather than from configuration, because a key passed as an
    environment variable is visible in process listings and in anything that
    dumps the environment on error.

    :raises ValueError: if the key is not RSA. The venue's scheme is RSA-PSS,
        and an EC key would fail at signing time with an error that reads like
        a venue problem.
    """
    key = serialization.load_pem_private_key(path.read_bytes(), password=password)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(
            f"{path} holds a {type(key).__name__}, but the venue's scheme is RSA-PSS; "
            f"an EC key would fail at signing time with an error that looks like the "
            f"venue rejecting the credential"
        )
    return key


@dataclass(frozen=True, slots=True)
class KalshiCredentials:
    """An API key id and the private key that signs for it."""

    key_id: str
    private_key: rsa.RSAPrivateKey

    def signing_payload(self, *, timestamp_ms: int, method: str, path: str) -> bytes:
        """The exact bytes the venue expects to have been signed.

        ``path`` must be the path the transport will actually request,
        including any prefix and excluding the query string. Signing a
        different string than the one sent produces an authentication failure
        that reads as a bad credential rather than as a construction error.
        """
        return f"{timestamp_ms}{method.upper()}{path}".encode()

    def sign(self, *, method: str, path: str, now: dt.datetime | None = None) -> dict[str, str]:
        """Headers authenticating one request.

        :raises UnverifiedSigningScheme: while :data:`SCHEME_VERIFIED` is
            ``False``. A malformed signature is a stream of rejected requests
            from an address whose last problem was exactly that.
        """
        if not SCHEME_VERIFIED:
            raise UnverifiedSigningScheme(
                "the venue signing scheme has not been confirmed against current "
                "documentation; set SCHEME_VERIFIED once a human has checked it, so the "
                "confirmation lands in version control rather than in someone's memory"
            )

        at = now or dt.datetime.now(dt.UTC)
        timestamp_ms = int(at.timestamp() * 1000)
        signature = self.private_key.sign(
            self.signing_payload(timestamp_ms=timestamp_ms, method=method, path=path),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                # DIGEST_LENGTH, not MAX_LENGTH: the venue's documented salt is
                # the digest length, and PSS verification fails on a salt the
                # verifier does not expect.
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            _HEADER_KEY: self.key_id,
            _HEADER_SIGNATURE: base64.b64encode(signature).decode("ascii"),
            _HEADER_TIMESTAMP: str(timestamp_ms),
        }
