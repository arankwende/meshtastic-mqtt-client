import pytest

from meshtastic_mqtt_client.crypto import (
    DEFAULT_KEY,
    aead_decrypt,
    aead_encrypt,
    channel_hash,
    crypt,
    expand_key,
)


def test_default_key_shorthand():
    assert expand_key("AQ==") == DEFAULT_KEY
    assert expand_key(b"\x02") == DEFAULT_KEY[:-1] + b"\x02"


def test_empty_key_means_no_encryption():
    assert expand_key("") == b""
    assert expand_key("AA==") == b""


def test_full_keys_pass_through():
    key = bytes(range(32))
    assert expand_key(key) == key
    assert expand_key(key[:16]) == key[:16]


def test_channel_hash_matches_firmware():
    # Reference values from the meshtastic python library's test suite.
    assert channel_hash("LongFast", DEFAULT_KEY) == 8
    assert channel_hash("MediumFast", DEFAULT_KEY) == 31


def test_crypt_round_trip_and_nonce_dependence():
    plain = b"hello mesh"
    enc = crypt(DEFAULT_KEY, 0x1234, 0xC0FFEE01, plain)
    assert enc != plain
    assert crypt(DEFAULT_KEY, 0x1234, 0xC0FFEE01, enc) == plain
    assert crypt(DEFAULT_KEY, 0x1235, 0xC0FFEE01, enc) != plain


def test_short_keys_are_zero_padded_like_the_firmware():
    assert expand_key(b"\x01\x02") == b"\x01\x02" + bytes(14)
    assert expand_key(bytes(20)) == bytes(32)
    with pytest.raises(ValueError):
        expand_key(bytes(33))


def test_aead_channel_hash_differs():
    assert channel_hash("LongFast", DEFAULT_KEY, aead=True) == 8 ^ 0xAE


def test_aead_round_trip_and_authentication():
    plain = b"hello aead"
    enc = aead_encrypt(DEFAULT_KEY, 0xBEEF1234, 0x42424242, 0x2468ACE0, plain)
    assert len(enc) == len(plain) + 12
    assert aead_decrypt(DEFAULT_KEY, 0xBEEF1234, 0x42424242, 0x2468ACE0, enc) == plain
    # The destination is authenticated, as are the nonce inputs and the bytes.
    assert aead_decrypt(DEFAULT_KEY, 0xBEEF1234, 0x42424242, 0xFFFFFFFF, enc) is None
    assert aead_decrypt(DEFAULT_KEY, 0xBEEF1235, 0x42424242, 0x2468ACE0, enc) is None
    tampered = bytes([enc[0] ^ 1]) + enc[1:]
    assert aead_decrypt(DEFAULT_KEY, 0xBEEF1234, 0x42424242, 0x2468ACE0, tampered) is None
    assert aead_decrypt(DEFAULT_KEY, 1, 2, 3, bytes(12)) is None


def test_aead_works_with_256_bit_keys():
    key = bytes.fromhex("603DEB1015CA71BE2B73AEF0857D77811F352C073B6108D72D9810A30914DFF4")
    enc = aead_encrypt(key, 0x0102030405060708, 0xDEADBEEF, 0xFFFFFFFF, b"Hello Meshtastic AEAD!")
    assert aead_decrypt(key, 0x0102030405060708, 0xDEADBEEF, 0xFFFFFFFF, enc) == b"Hello Meshtastic AEAD!"
