"""Load ECU-specific settings (bus, ISO-TP addressing, UDS timing) from a TOML file.

Command-line options override profile values. Example:

    [bus]
    interface = "pcan"
    bitrate = 500000

    [isotp]
    tx_id = 0x7E0
    rx_id = 0x7E8
    padding = 0xCC   # false disables padding

    [uds]
    timeout = 2.0
"""

import tomllib
from pathlib import Path

SECTIONS = {
    "bus": {
        "interface": str, "channel": str, "bitrate": int, "fd": bool, "data_bitrate": int,
        "nominal_sample_point": (int, float), "data_sample_point": (int, float),
    },
    "isotp": {
        "tx_id": int, "rx_id": int, "extended": bool, "fd": bool, "brs": bool,
        "padding": (int, bool), "stmin": int, "blocksize": int,
    },
    "uds": {"timeout": (int, float)},
}


def load_profile(path: str | Path) -> dict[str, dict]:
    """Return ``{"bus": {...}, "isotp": {...}, "uds": {...}}`` with checked key names and types."""
    with open(path, "rb") as file:
        try:
            data = tomllib.load(file)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path}: {exc}") from exc

    unknown = set(data) - set(SECTIONS)
    if unknown:
        raise ValueError(f"{path}: unknown section(s): {', '.join(sorted(unknown))}")

    profile = {}
    for section, schema in SECTIONS.items():
        values = data.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"{path}: [{section}] must be a table")
        for key, value in values.items():
            expected = schema.get(key)
            if expected is None:
                raise ValueError(f"{path}: unknown key {section}.{key}")
            # bool is a subclass of int, so only accept it where the schema names bool.
            bool_allowed = expected is bool or (isinstance(expected, tuple) and bool in expected)
            if not isinstance(value, expected) or (isinstance(value, bool) and not bool_allowed):
                raise ValueError(f"{path}: {section}.{key} has the wrong type")
        if section == "isotp" and values.get("padding") is True:
            raise ValueError(f"{path}: isotp.padding must be a byte value or false")
        profile[section] = dict(values)
    if profile["isotp"].get("padding") is False:
        profile["isotp"]["padding"] = None
    return profile
