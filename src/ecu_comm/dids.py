"""Named data identifiers: decode ReadDataByIdentifier payloads into fields.

A profile describes DIDs in a ``[dids]`` table, one sub-table per DID. Numbers are
big-endian (as UDS sends them); ``ascii`` and ``bytes`` fields take a length, which
the last field may leave out to take the rest of the payload::

    [dids.0xFD10]
    name = "PhaseCurrents"
    fields = [
        { name = "U", type = "int16", scale = 0.1, unit = "A" },
        { name = "V", type = "int16", scale = 0.1, unit = "A" },
        { name = "W", type = "int16", scale = 0.1, unit = "A" },
    ]

    [dids.0xF186]
    name = "ActiveSession"
    fields = [{ name = "Session", type = "uint8", choices = { 1 = "default", 3 = "extended" } }]

``display = "hex"`` prints an unscaled integer field as ``0x...`` at its full width.
"""

from dataclasses import dataclass, field
from decimal import Decimal

INTEGER_TYPES = {
    "uint8": (1, False), "uint16": (2, False), "uint24": (3, False), "uint32": (4, False),
    "int8": (1, True), "int16": (2, True), "int24": (3, True), "int32": (4, True),
}
BYTE_TYPES = ("ascii", "bytes")
FIELD_KEYS = {"name", "type", "length", "scale", "offset", "unit", "choices", "display"}


@dataclass(frozen=True)
class DidField:
    name: str
    type: str
    length: int | None  # None: the rest of the payload (last ascii/bytes field only)
    scale: float = 1
    offset: float = 0
    unit: str = ""
    choices: dict[int, str] = field(default_factory=dict)
    hex: bool = False  # display = "hex": the raw value as 0x..., full width

    @property
    def signed(self) -> bool:
        return INTEGER_TYPES.get(self.type, (0, False))[1]

    def describe(self) -> str:
        size = "rest" if self.length is None else f"{self.length} byte{'s' if self.length != 1 else ''}"
        text = f"{self.name}: {self.type}"
        if self.type in BYTE_TYPES:
            text += f", {size}"
        if self.scale != 1:
            text += f", x{_number(self.scale)}"
        if self.offset:
            text += f", {'+' if self.offset > 0 else '-'}{_number(abs(self.offset))}"
        if self.unit:
            text += f" {self.unit}"
        if self.hex:
            text += ", hex"
        if self.choices:
            text += ", " + " ".join(f"{key}={value}" for key, value in self.choices.items())
        return text


@dataclass(frozen=True)
class DidDefinition:
    did: int
    name: str
    fields: tuple[DidField, ...] = ()

    @property
    def length(self) -> int | None:
        """The payload length, or None when the last field takes the rest."""
        if any(f.length is None for f in self.fields):
            return None
        return sum(f.length for f in self.fields)


DidTable = dict[int, DidDefinition]


def parse_table(data: dict) -> DidTable:
    """Check a profile's ``[dids]`` table and turn it into definitions; raise ValueError naming the entry."""
    table: DidTable = {}
    names: dict[str, int] = {}
    for key, entry in data.items():
        did = _parse_did(key)
        where = f"dids.{key}"
        if did in table:
            raise ValueError(f"{where}: 0x{did:04X} is defined twice")
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a table")
        unknown = set(entry) - {"name", "fields"}
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip() or name.strip() != name or " " in name:
            raise ValueError(f"{where}: name must be one word")
        if name.lower() in names:
            raise ValueError(f"{where}: name {name} is already used by 0x{names[name.lower()]:04X}")
        if _is_number(name):
            raise ValueError(f"{where}: name {name} looks like a number")
        raw_fields = entry.get("fields", [])
        if not isinstance(raw_fields, list):
            raise ValueError(f"{where}.fields must be a list of tables")
        fields = tuple(_parse_field(f"{where}.fields[{index}]", item, last=index == len(raw_fields) - 1)
                       for index, item in enumerate(raw_fields))
        seen = set()
        for item in fields:
            if item.name in seen:
                raise ValueError(f"{where}: field {item.name} is given twice")
            seen.add(item.name)
        table[did] = DidDefinition(did, name, fields)
        names[name.lower()] = did
    return table


def _parse_did(key: str) -> int:
    try:
        did = int(key, 0)
    except ValueError:
        raise ValueError(f"dids.{key}: a DID must be a number such as 0xF190") from None
    if not 0 <= did <= 0xFFFF:
        raise ValueError(f"dids.{key}: a DID is a 16-bit number")
    return did


def _parse_field(where: str, item: object, last: bool) -> DidField:
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be a table")
    unknown = set(item) - FIELD_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    name, kind = item.get("name"), item.get("type")
    if not isinstance(name, str) or not name or " " in name:
        raise ValueError(f"{where}: name must be one word")
    if kind not in INTEGER_TYPES and kind not in BYTE_TYPES:
        raise ValueError(f"{where}: type must be one of {', '.join([*INTEGER_TYPES, *BYTE_TYPES])}")
    length = item.get("length")
    if kind in INTEGER_TYPES:
        if length is not None:
            raise ValueError(f"{where}: {kind} has a fixed length; leave out length")
        length = INTEGER_TYPES[kind][0]
    elif length is None:
        if not last:
            raise ValueError(f"{where}: {kind} needs a length unless it is the last field")
    elif type(length) is not int or length <= 0:
        raise ValueError(f"{where}: length must be a positive whole number")
    scale, offset = item.get("scale", 1), item.get("offset", 0)
    for label, value in (("scale", scale), ("offset", offset)):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{where}: {label} must be a number")
    if scale == 0:
        raise ValueError(f"{where}: scale must not be 0")
    unit = item.get("unit", "")
    if not isinstance(unit, str):
        raise ValueError(f"{where}: unit must be text")
    display = item.get("display", "decimal")
    if display not in ("decimal", "hex"):
        raise ValueError(f"{where}: display must be \"decimal\" or \"hex\"")
    if display == "hex" and (kind not in INTEGER_TYPES or scale != 1 or offset):
        raise ValueError(f"{where}: display = \"hex\" is for unscaled integer fields")
    choices_in = item.get("choices", {})
    if not isinstance(choices_in, dict):
        raise ValueError(f"{where}: choices must be a table such as {{ 1 = \"default\" }}")
    if kind in BYTE_TYPES and (scale != 1 or offset or choices_in):
        raise ValueError(f"{where}: {kind} fields take no scale, offset or choices")
    choices = {}
    for key, value in choices_in.items():
        try:
            raw = int(key, 0)
        except ValueError:
            raise ValueError(f"{where}: choice key {key} must be a whole number") from None
        if not isinstance(value, str):
            raise ValueError(f"{where}: choice {key} must be text")
        choices[raw] = value
    return DidField(name, kind, length, scale, offset, unit, choices, display == "hex")


def decode(definition: DidDefinition, payload: bytes) -> tuple[list[tuple[DidField, object]], str | None]:
    """The fields and their values, or an error text when the payload has the wrong length."""
    expected = definition.length
    fixed = sum(f.length for f in definition.fields if f.length is not None)
    if (expected is not None and len(payload) != expected) or (expected is None and len(payload) < fixed):
        wanted = f"{expected}" if expected is not None else f"at least {fixed}"
        return [], f"expected {wanted} bytes, got {len(payload)}"
    values = []
    position = 0
    for item in definition.fields:
        size = item.length if item.length is not None else len(payload) - position
        chunk = payload[position:position + size]
        position += size
        if item.type == "bytes":
            values.append((item, chunk))
        elif item.type == "ascii":
            values.append((item, chunk.rstrip(b"\x00").decode("latin-1")))
        else:
            raw = int.from_bytes(chunk, "big", signed=item.signed)
            if item.hex and raw not in item.choices:
                values.append((item, f"0x{int.from_bytes(chunk, 'big'):0{2 * len(chunk)}X}"))
                continue
            values.append((item, item.choices[raw] if raw in item.choices else raw * item.scale + item.offset))
    return values, None


def format_value(item: DidField, value: object) -> str:
    if item.type == "bytes":
        return value.hex(" ").upper() or "(empty)"
    if item.type == "ascii":
        printable = "".join(c if 0x20 <= ord(c) < 0x7F else "." for c in value)
        return f'"{printable}"'
    if isinstance(value, str):  # a choice name, or a hex text
        return f"{value} {item.unit}" if item.unit and value.startswith("0x") else value
    decimals = max(_decimals(item.scale), _decimals(item.offset))
    text = f"{value:.{decimals}f}" if decimals else str(int(round(value)))
    return f"{text} {item.unit}" if item.unit else text


def format_fields(definition: DidDefinition, payload: bytes) -> str:
    """``U=1.3 A, V=3.6 A`` or ``(cannot decode: ...)``; empty for a DID without fields."""
    if not definition.fields:
        return ""
    values, error = decode(definition, payload)
    if error is not None:
        return f"(cannot decode: {error})"
    return ", ".join(f"{item.name}={format_value(item, value)}" for item, value in values)


def describe(definition: DidDefinition) -> list[str]:
    """Lines for ``ecu-can uds dids``: the DID, then one line per field."""
    length = definition.length
    size = f"[{length}]" if length is not None else "[variable]"
    lines = [f"0x{definition.did:04X} {definition.name} {size if definition.fields else ''}".rstrip()]
    lines.extend(f"  {item.describe()}" for item in definition.fields)
    return lines


def resolve(table: DidTable, text: str) -> int:
    """A DID given as a number (0xF190, 61840) or as a name from the table (case does not matter)."""
    if _is_number(text):
        did = int(text, 0)
        if not 0 <= did <= 0xFFFF:
            raise ValueError(f"{text} is not a 16-bit DID")
        return did
    for definition in table.values():
        if definition.name.lower() == text.lower():
            return definition.did
    if table:
        raise ValueError(f"no DID named {text} (names: {', '.join(d.name for d in table.values())})")
    raise ValueError(f"no DID named {text}: the profile has no [dids] table")


def _is_number(text: str) -> bool:
    try:
        int(text, 0)
    except ValueError:
        return False
    return True


def _decimals(number: float) -> int:
    exponent = Decimal(str(number)).normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _number(value: float) -> str:
    return f"{value:g}" if isinstance(value, float) and not float(value).is_integer() else f"{int(value)}"
