"""Diagnostic trouble codes: ReadDTCInformation (0x19) and ClearDiagnosticInformation (0x14).

Responses are parsed by hand rather than by udsoncan so that an ECU which answers
with less than ISO 14229-1 requires (for example a bare ``59 02``) is reported
plainly instead of failing.
"""

from dataclasses import dataclass

REPORT_NUMBER_BY_STATUS_MASK = 0x01
REPORT_BY_STATUS_MASK = 0x02
REPORT_SUPPORTED = 0x0A
ALL_GROUPS = 0xFFFFFF

# ISO 14229-1 DTC status bits, bit 0 first.
STATUS_BITS = (
    "testFailed",
    "testFailedThisOperationCycle",
    "pendingDTC",
    "confirmedDTC",
    "testNotCompletedSinceLastClear",
    "testFailedSinceLastClear",
    "testNotCompletedThisOperationCycle",
    "warningIndicatorRequested",
)
FORMATS = {
    0x00: "SAE J2012-DA DTC format 00",
    0x01: "ISO 14229-1",
    0x02: "SAE J1939-73",
    0x03: "ISO 11992-4",
    0x04: "SAE J2012-DA DTC format 04",
}
SHORT_NOTE = "shorter than ISO 14229-1 allows"


@dataclass(frozen=True)
class DtcRecord:
    code: int    # 3 bytes: 2-byte DTC and failure type
    status: int


@dataclass(frozen=True)
class DtcList:
    """The DTCs of a 19 02 or 19 0A response. ``availability`` is None when the ECU left it out."""

    availability: int | None
    records: tuple[DtcRecord, ...]


@dataclass(frozen=True)
class DtcCount:
    availability: int | None
    format: int | None
    count: int | None


def read_request(subfunction: int, mask: int | None = None) -> bytes:
    if mask is None:
        return bytes((0x19, subfunction))
    if not 0 <= mask <= 0xFF:
        raise ValueError("a DTC status mask is a byte value")
    return bytes((0x19, subfunction, mask))


def clear_request(group: int) -> bytes:
    if not 0 <= group <= 0xFFFFFF:
        raise ValueError("a DTC group is a 3-byte value, e.g. 0xFFFFFF for all")
    return b"\x14" + group.to_bytes(3, "big")


def _check_header(response: bytes, subfunction: int) -> None:
    if response[:2] != bytes((0x59, subfunction)):
        raise ValueError(f"expected a response starting 59 {subfunction:02X}, got {response.hex(' ').upper()}")


def parse_list(response: bytes, subfunction: int) -> DtcList:
    """Parse ``59 02`` / ``59 0A``: availability mask, then 4 bytes per DTC (code, status)."""
    _check_header(response, subfunction)
    if len(response) == 2:
        return DtcList(None, ())
    body = response[3:]
    if len(body) % 4:
        raise ValueError(f"DTC records are 4 bytes each; {len(body)} bytes follow the availability mask")
    records = tuple(DtcRecord(int.from_bytes(body[i:i + 3], "big"), body[i + 3]) for i in range(0, len(body), 4))
    return DtcList(response[2], records)


def parse_count(response: bytes) -> DtcCount:
    """Parse ``59 01``: availability mask, format identifier, 2-byte count."""
    _check_header(response, REPORT_NUMBER_BY_STATUS_MASK)
    if len(response) == 2:
        return DtcCount(None, None, None)
    if len(response) != 6:
        raise ValueError(f"expected 6 bytes for 59 01, got {len(response)}")
    return DtcCount(response[2], response[3], int.from_bytes(response[4:6], "big"))


def j2012_code(code: int) -> str:
    """The SAE J2012 text of a 3-byte DTC: P/C/B/U, four characters, then the failure type."""
    dtc, failure = code >> 8, code & 0xFF
    letter = "PCBU"[dtc >> 14]
    return f"{letter}{(dtc >> 12) & 0x3:X}{(dtc >> 8) & 0xF:X}{(dtc >> 4) & 0xF:X}{dtc & 0xF:X}-{failure:02X}"


def status_names(status: int) -> list[str]:
    return [name for bit, name in enumerate(STATUS_BITS) if status & (1 << bit)]


def format_record(record: DtcRecord) -> str:
    names = " ".join(status_names(record.status)) or "(no bits set)"
    return f"0x{record.code:06X} {j2012_code(record.code)}  status 0x{record.status:02X} {names}"


def format_list(result: DtcList, subfunction: int, mask: int | None, raw: bytes) -> list[str]:
    if result.availability is None:
        return [f"Response [{len(raw)}] {raw.hex(' ').upper()}: no status availability mask and no DTCs "
                f"({SHORT_NOTE})"]
    lines = [f"Status availability mask 0x{result.availability:02X}"]
    what = f"with status & 0x{mask:02X}" if subfunction == REPORT_BY_STATUS_MASK else "supported"
    if not result.records:
        lines.append(f"No DTCs {what}")
    else:
        lines.append(f"{len(result.records)} DTC{'s' if len(result.records) != 1 else ''} {what}:")
        lines.extend(f"  {format_record(record)}" for record in result.records)
    return lines


def format_count(result: DtcCount, mask: int, raw: bytes) -> str:
    if result.availability is None:
        return (f"Response [{len(raw)}] {raw.hex(' ').upper()}: no status availability mask and no count "
                f"({SHORT_NOTE})")
    kind = FORMATS.get(result.format, "unknown format")
    return (f"{result.count} DTC{'s' if result.count != 1 else ''} with status & 0x{mask:02X} "
            f"(availability mask 0x{result.availability:02X}, format 0x{result.format:02X} {kind})")
