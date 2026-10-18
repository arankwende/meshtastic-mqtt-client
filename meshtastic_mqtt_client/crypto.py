"""Meshtastic channel encryption (AES-CTR or AES-CCM) and channel hashing."""

import base64
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

# The well-known default channel key that the "AQ==" shorthand expands to.
DEFAULT_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")

# AEAD (AES-CCM) channels, added after firmware 2.8.1: the hash is XORed with
# this so they never collide with an AES-CTR channel using the same name and key.
AEAD_HASH_MASK = 0xAE
AEAD_TAG_SIZE = 12
AEAD_NONCE_SIZE = 13  # CCM with a 2-byte length field


def expand_key(psk: str | bytes) -> bytes:
    """Turn a channel PSK (base64 string or raw bytes) into an AES key.

    Mirrors the firmware: an empty key means no encryption, a single byte N
    means "default key number N", and short keys are zero-padded to 16 or 32.
    """
    if isinstance(psk, str):
        psk = base64.b64decode(psk.replace("-", "+").replace("_", "/"))
    if len(psk) == 0:
        return b""
    if len(psk) == 1:
        if psk[0] == 0:
            return b""
        return DEFAULT_KEY[:-1] + psk
    if len(psk) > 32:
        raise ValueError(f"Channel key can be at most 32 bytes long, got {len(psk)}")
    return psk.ljust(16 if len(psk) <= 16 else 32, b"\x00")


def _xor_bytes(data: bytes) -> int:
    result = 0
    for byte in data:
        result ^= byte
    return result


def channel_hash(channel_name: str, key: bytes, aead: bool = False) -> int:
    """The one-byte channel number radios put in MeshPacket.channel."""
    h = _xor_bytes(channel_name.encode("utf-8")) ^ _xor_bytes(key)
    return h ^ AEAD_HASH_MASK if aead else h


def _nonce(packet_id: int, from_node: int) -> bytes:
    return struct.pack("<QI", packet_id, from_node) + b"\x00" * 4


def crypt(key: bytes, packet_id: int, from_node: int, data: bytes) -> bytes:
    """AES-CTR encrypt or decrypt a packet payload (CTR mode is symmetric)."""
    cipher = Cipher(algorithms.AES(key), modes.CTR(_nonce(packet_id, from_node)))
    ctx = cipher.encryptor()
    return ctx.update(data) + ctx.finalize()


def _aead_params(packet_id: int, from_node: int, to_node: int) -> tuple[bytes, bytes]:
    nonce = _nonce(packet_id, from_node)[:AEAD_NONCE_SIZE]
    aad = struct.pack("<II", from_node, to_node)
    return nonce, aad


def aead_encrypt(key: bytes, packet_id: int, from_node: int, to_node: int, data: bytes) -> bytes:
    """AES-CCM encrypt; returns ciphertext followed by the 12-byte tag."""
    nonce, aad = _aead_params(packet_id, from_node, to_node)
    return AESCCM(key, tag_length=AEAD_TAG_SIZE).encrypt(nonce, data, aad)


def aead_decrypt(key: bytes, packet_id: int, from_node: int, to_node: int, data: bytes) -> bytes | None:
    """AES-CCM decrypt; None if the tag doesn't verify (wrong key or tampered)."""
    if len(data) <= AEAD_TAG_SIZE:
        return None
    nonce, aad = _aead_params(packet_id, from_node, to_node)
    try:
        return AESCCM(key, tag_length=AEAD_TAG_SIZE).decrypt(nonce, data, aad)
    except InvalidTag:
        return None
