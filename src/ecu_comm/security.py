"""SecurityAccess (0x27) key computation with pluggable algorithms.

Secret key material never lives in this repository. ``load_secret`` reads it at run
time from a key file (``--key-file`` or ``security.key_file`` in a profile) or from
the ``ECU_COMM_SECURITY_KEY`` environment variable, as hex text.

An algorithm is a function ``(seed: bytes, level: int, secret: bytes | None) -> bytes``.
Built-in names are listed in ``ALGORITHMS``; any other function can be named as
``package.module:function``.
"""

import importlib
import os
from collections.abc import Callable
from pathlib import Path

KEY_ENV = "ECU_COMM_SECURITY_KEY"

KeyAlgorithm = Callable[[bytes, int, bytes | None], bytes]


def aes128_ecb(seed: bytes, level: int, secret: bytes | None) -> bytes:
    """Key = AES-128-ECB(secret, seed). The seed length must be a multiple of 16 bytes."""
    if secret is None:
        raise ValueError(f"the aes128-ecb algorithm needs a secret key: use --key-file or set {KEY_ENV}")
    if len(secret) != 16:
        raise ValueError("the aes128-ecb algorithm needs a 16-byte secret key")
    if not seed or len(seed) % 16:
        raise ValueError(f"the aes128-ecb algorithm needs a seed of 16-byte blocks, got {len(seed)} bytes")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError(
            "the aes128-ecb algorithm needs the 'cryptography' package: pip install -e .[security]"
        ) from exc
    encryptor = Cipher(algorithms.AES(secret), modes.ECB()).encryptor()
    return encryptor.update(seed) + encryptor.finalize()


ALGORITHMS: dict[str, KeyAlgorithm] = {"aes128-ecb": aes128_ecb}


def resolve_algorithm(name: str) -> KeyAlgorithm:
    """Return a built-in algorithm by name, or import ``package.module:function``."""
    if name in ALGORITHMS:
        return ALGORITHMS[name]
    module_name, _, function_name = name.partition(":")
    if not module_name or not function_name:
        raise ValueError(f"unknown key algorithm {name!r}: use {', '.join(ALGORITHMS)} or package.module:function")
    try:
        function = getattr(importlib.import_module(module_name), function_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"cannot load key algorithm {name!r}: {exc}") from exc
    if not callable(function):
        raise ValueError(f"key algorithm {name!r} is not callable")
    return function


def parse_secret(text: str) -> bytes:
    """Parse hex text; spaces, newlines, colons and a 0x prefix are ignored."""
    cleaned = "".join(text.split()).replace(":", "")
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    try:
        secret = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError("the secret key must be hex text") from exc
    if not secret:
        raise ValueError("the secret key is empty")
    return secret


def load_secret(key_file: str | os.PathLike | None = None) -> bytes | None:
    """Read the secret from ``key_file``, else from $ECU_COMM_SECURITY_KEY, else None."""
    if key_file is not None:
        return parse_secret(Path(key_file).read_text(encoding="utf-8"))
    text = os.environ.get(KEY_ENV)
    return parse_secret(text) if text else None
