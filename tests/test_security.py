import pytest

from ecu_comm.security import KEY_ENV, aes128_ecb, load_secret, parse_secret, resolve_algorithm

# FIPS-197 appendix C.1
FIPS_KEY = bytes(range(16))
FIPS_PLAIN = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CIPHER = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")


def test_aes128_ecb_matches_fips_197():
    assert aes128_ecb(FIPS_PLAIN, 1, FIPS_KEY) == FIPS_CIPHER
    assert aes128_ecb(FIPS_PLAIN * 2, 1, FIPS_KEY) == FIPS_CIPHER * 2


@pytest.mark.parametrize(
    "seed,secret,match",
    [(FIPS_PLAIN, None, KEY_ENV), (FIPS_PLAIN, bytes(15), "16-byte secret"), (bytes(15), FIPS_KEY, "16-byte blocks")],
)
def test_aes128_ecb_rejects_bad_input(seed, secret, match):
    with pytest.raises(ValueError, match=match):
        aes128_ecb(seed, 1, secret)


@pytest.mark.parametrize("text", ["000102030405060708090a0b0c0d0e0f", "0x00 01 02 03 04 05 06 07\n08 09 0A 0B 0C 0D 0E 0F\n",
                                  "00:01:02:03:04:05:06:07:08:09:0a:0b:0c:0d:0e:0f"])
def test_parse_secret_formats(text):
    assert parse_secret(text) == FIPS_KEY


@pytest.mark.parametrize("text", ["", "xyz"])
def test_parse_secret_rejects_bad_text(text):
    with pytest.raises(ValueError):
        parse_secret(text)


def test_load_secret_prefers_file_over_environment(tmp_path, monkeypatch):
    key_file = tmp_path / "board.key"
    key_file.write_text(FIPS_KEY.hex())
    monkeypatch.setenv(KEY_ENV, "ff" * 16)
    assert load_secret(key_file) == FIPS_KEY
    assert load_secret() == b"\xff" * 16
    monkeypatch.delenv(KEY_ENV)
    assert load_secret() is None


def xor_algorithm(seed, level, secret):
    return bytes(b ^ level for b in seed)


def test_resolve_algorithm_builtin_and_plugin():
    assert resolve_algorithm("aes128-ecb") is aes128_ecb
    assert resolve_algorithm("test_security:xor_algorithm")(b"\x00\x01", 3, None) == b"\x03\x02"
    for name in ("nope", "no_such_module:f", "test_security:missing"):
        with pytest.raises(ValueError):
            resolve_algorithm(name)
