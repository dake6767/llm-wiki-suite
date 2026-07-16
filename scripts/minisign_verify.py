"""Small, dependency-free Minisign verifier for signed release artifacts.

The Tauri updater embeds a Minisign public key and publishes prehashed
signatures.  Keeping verification here avoids turning a release install into a
package-manager bootstrap just to verify the package-manager-independent app.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path


_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)
_IDENTITY = (0, 1, 1, 0)


def _recover_x(y: int, sign: int) -> int:
    if y >= _Q:
        raise ValueError("invalid Ed25519 point")
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q:
        x = x * _I % _Q
    if (x * x - xx) % _Q:
        raise ValueError("invalid Ed25519 point")
    if x & 1 != sign:
        x = _Q - x
    if x == 0 and sign:
        raise ValueError("invalid Ed25519 point sign")
    return x


def _decode_point(encoded: bytes) -> tuple[int, int, int, int]:
    if len(encoded) != 32:
        raise ValueError("invalid Ed25519 point length")
    value = int.from_bytes(encoded, "little")
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, value >> 255)
    return x, y, 1, x * y % _Q


def _add(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = left
    x2, y2, z2, t2 = right
    a = (y1 - x1) * (y2 - x2) % _Q
    b = (y1 + x1) * (y2 + x2) % _Q
    c = 2 * _D * t1 * t2 % _Q
    d = 2 * z1 * z2 % _Q
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q


def _scalar_multiply(
    point: tuple[int, int, int, int], scalar: int
) -> tuple[int, int, int, int]:
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


_BASE_Y = 4 * pow(5, _Q - 2, _Q) % _Q
_BASE_X = _recover_x(_BASE_Y, 0)
_BASE = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % _Q)


def verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a strict Ed25519 signature using only Python's standard library."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        public_point = _decode_point(public_key)
        r_point = _decode_point(signature[:32])
    except ValueError:
        return False
    scalar = int.from_bytes(signature[32:], "little")
    if scalar >= _L:
        return False
    challenge = int.from_bytes(
        hashlib.sha512(signature[:32] + public_key + message).digest(), "little"
    ) % _L
    left = _scalar_multiply(_BASE, scalar)
    right = _add(r_point, _scalar_multiply(public_point, challenge))
    return (
        left[0] * right[2] - right[0] * left[2]
    ) % _Q == 0 and (
        left[1] * right[2] - right[1] * left[2]
    ) % _Q == 0


def _strict_b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid base64 in Minisign data") from exc


def _decode_tauri_box(value: str, kind: str) -> list[str]:
    try:
        text = _strict_b64(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid UTF-8 in Tauri {kind}") from exc
    lines = text.splitlines()
    expected = 2 if kind == "public key" else 4
    if len(lines) != expected:
        raise ValueError(f"invalid Tauri {kind} line count")
    return lines


def verify_tauri_minisign(path: Path, public_key_box: str, signature_box: str) -> None:
    """Raise ``ValueError`` unless a Tauri Minisign signature is valid."""
    public_lines = _decode_tauri_box(public_key_box, "public key")
    signature_lines = _decode_tauri_box(signature_box, "signature")
    if not public_lines[0].startswith("untrusted comment: "):
        raise ValueError("invalid Minisign public-key comment")
    public_packet = _strict_b64(public_lines[1].strip())
    if len(public_packet) != 42 or public_packet[:2] != b"Ed":
        raise ValueError("unsupported Minisign public key")

    if not signature_lines[0].startswith("untrusted comment: "):
        raise ValueError("invalid Minisign signature comment")
    signature_packet = _strict_b64(signature_lines[1].strip())
    if len(signature_packet) != 74 or signature_packet[:2] != b"ED":
        raise ValueError("release signature must use Minisign prehash mode")
    if signature_packet[2:10] != public_packet[2:10]:
        raise ValueError("Minisign key id mismatch")
    trusted_prefix = "trusted comment: "
    if not signature_lines[2].startswith(trusted_prefix):
        raise ValueError("invalid Minisign trusted comment")
    trusted_comment = signature_lines[2][len(trusted_prefix):].strip().encode("utf-8")
    global_signature = _strict_b64(signature_lines[3].strip())
    if len(global_signature) != 64:
        raise ValueError("invalid Minisign comment signature")

    public_key = public_packet[10:]
    primary_signature = signature_packet[10:]
    if not verify_ed25519(
        public_key, primary_signature + trusted_comment, global_signature
    ):
        raise ValueError("Minisign trusted-comment verification failed")

    digest = hashlib.blake2b(digest_size=64)
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    if not verify_ed25519(public_key, digest.digest(), primary_signature):
        raise ValueError("Minisign release signature verification failed")
