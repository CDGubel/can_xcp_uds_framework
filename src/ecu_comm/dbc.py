"""Decode CAN frames into named signals with a DBC database (``cantools``).

Several DBC files can be loaded together. A frame is looked up by its ID and
whether the ID is 11-bit or 29-bit, so 0x100 and 0x00000100 are different
messages. Two files that define the same ID are an error, not a silent overwrite.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import can
import cantools
from cantools.database.can import Message, Signal

# cantools logs a warning for every overwritten message; duplicates are reported here instead.
logging.getLogger("cantools").setLevel(logging.ERROR)


@dataclass
class Network:
    """The messages of one or more DBC files, looked up by (ID, extended)."""

    messages: dict[tuple[int, bool], "Message"] = field(default_factory=dict)
    files: dict[tuple[int, bool], str] = field(default_factory=dict)

    def find(self, name: str) -> "Message":
        for message in self.messages.values():
            if message.name == name:
                return message
        raise ValueError(f"no message named {name} in the DBC files")

    def sorted_messages(self) -> list["Message"]:
        return [self.messages[key] for key in sorted(self.messages, key=lambda k: (k[1], k[0]))]


@dataclass(frozen=True)
class Decoded:
    """A decoded frame: the message and its signal values, or the reason it could not be decoded."""

    message: "Message"
    values: dict[str, object]
    error: str | None = None


def load_dbc(paths: list[str | Path]) -> Network:
    """Load DBC files into one network; raise ValueError for a missing file, a bad file or a duplicate ID."""
    network = Network()
    for path in paths:
        path = Path(path)
        try:
            database = cantools.database.load_file(path, database_format="dbc", strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"DBC file not found: {path}") from exc
        except Exception as exc:  # cantools raises several parse error types
            raise ValueError(f"{path}: {exc}") from exc
        for message in database.messages:
            key = (message.frame_id, message.is_extended_frame)
            if key in network.messages:
                raise ValueError(f"{path}: {_format_id(*key)} ({message.name}) is already defined as "
                                 f"{network.messages[key].name} in {network.files[key]}")
            network.messages[key] = message
            network.files[key] = str(path)
    return network


def decode(network: Network, frame: can.Message) -> Decoded | None:
    """Decode a frame, or return None when no DBC message has its ID."""
    message = network.messages.get((frame.arbitration_id, frame.is_extended_id))
    if message is None or frame.is_remote_frame or frame.is_error_frame:
        return None
    try:
        values = message.decode(bytes(frame.data), decode_choices=True, scaling=True)
    except Exception as exc:  # cantools.database.DecodeError and friends
        return Decoded(message, {}, str(exc))
    return Decoded(message, dict(values))


def encode(message: "Message", values: dict[str, object]) -> bytes:
    """Encode physical values (numbers, or choice names) into the message's payload."""
    try:
        return message.encode(values, scaling=True, strict=True)
    except Exception as exc:
        raise ValueError(f"{message.name}: {exc}") from exc


def format_value(signal: "Signal", value: object) -> str:
    """A value with as many decimals as the signal's scale and offset need, and its unit."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        decimals = max(_decimals(signal.scale), _decimals(signal.offset))
        if decimals:
            text = f"{value:.{decimals}f}"
        elif float(value).is_integer():
            text = str(int(value))
        else:
            text = f"{value:g}"
        return f"{text} {signal.unit}" if signal.unit else text
    return str(value)  # a choice name (NamedSignalValue) or raw bytes


def format_decoded(decoded: Decoded, signals: set[str] | None = None) -> str:
    """``Name Signal=value unit, ...``; only the given signals when a set is passed."""
    if decoded.error is not None:
        return f"{decoded.message.name} (cannot decode: {decoded.error})"
    parts = []
    for name, value in decoded.values.items():
        if signals is not None and name not in signals:
            continue
        parts.append(f"{name}={format_value(decoded.message.get_signal_by_name(name), value)}")
    return f"{decoded.message.name} {', '.join(parts)}".rstrip()


def describe_message(message: "Message") -> list[str]:
    """Lines for ``ecu-can dbc show``: the message header, then one line per signal."""
    flags = ["EXT" if message.is_extended_frame else "STD", "FD" if message.is_fd else "CAN"]
    header = f"{_format_id(message.frame_id, message.is_extended_frame)} {' '.join(flags)} [{message.length}] {message.name}"
    details = []
    if message.cycle_time:
        details.append(f"cycle {message.cycle_time} ms")
    if message.senders:
        details.append(f"from {', '.join(message.senders)}")
    if details:
        header += f" ({'; '.join(details)})"
    lines = [header]
    for signal in message.signals:
        lines.append("  " + describe_signal(signal))
    return lines


def describe_signal(signal: "Signal") -> str:
    order = "Intel" if signal.byte_order == "little_endian" else "Motorola"
    kind = "signed" if signal.is_signed else "unsigned"
    if signal.is_float:
        kind = "float"
    text = f"{signal.name}: bit {signal.start}, {signal.length} bit{'s' if signal.length != 1 else ''}, {order}, {kind}"
    scaling = []  # physical = raw x scale + offset
    if signal.scale != 1:
        scaling.append(f"x{_number(signal.scale)}")
    if signal.offset != 0:
        scaling.append(f"{'+' if signal.offset > 0 else '-'}{_number(abs(signal.offset))}")
    if scaling:
        text += ", " + " ".join(scaling)
    if signal.minimum is not None or signal.maximum is not None:
        text += f", [{_number(signal.minimum)} .. {_number(signal.maximum)}]"
    if signal.unit:
        text += f" {signal.unit}"
    if signal.is_multiplexer:
        text += ", multiplexer"
    if signal.multiplexer_ids:
        text += f", when {signal.multiplexer_signal} = {', '.join(str(i) for i in signal.multiplexer_ids)}"
    if signal.choices:
        text += ", " + " ".join(f"{key}={value}" for key, value in signal.choices.items())
    return text


def _format_id(frame_id: int, extended: bool) -> str:
    return f"0x{frame_id:08X}" if extended else f"0x{frame_id:03X}"


def _decimals(number: float | int) -> int:
    exponent = Decimal(str(number)).normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _number(value: float | int | None) -> str:
    if value is None:
        return "?"
    return f"{value:g}" if isinstance(value, float) and not float(value).is_integer() else f"{int(value)}"
