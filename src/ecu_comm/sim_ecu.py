"""A minimal simulated UDS server for development without an ECU.

It answers DiagnosticSessionControl (0x10), ECUReset (0x11), ClearDiagnosticInformation
(0x14), ReadDTCInformation (0x19: sub-functions 0x01, 0x02 and 0x0A), ReadDataByIdentifier
(0x22) and TesterPresent (0x3E). Other services get NRC 0x11 serviceNotSupported.
With a functional ID it also answers functionally addressed single frames, on the
physical response ID, and drops the NRCs ISO 14229-1 suppresses for them.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import can

from .isotp_link import IsoTpConfig, build_stack, bus_notifier

DEFAULT_DIDS = {
    0xF187: b"SIM-PART-0001",
    0xF18C: b"SN000000000042",
    0xF190: b"WSIMULATED0000001",
    0xF195: b"\x01\x02\x03",
}

# Made-up DTCs (3-byte code: status). Clearing sets every status to "not completed" (0x50).
DEFAULT_DTCS = {
    0xC07300: 0x09,   # U0073-00: testFailed, confirmedDTC
    0x0A1B12: 0x24,   # P0A1B-12: pendingDTC, testFailedSinceLastClear
    0x9E0013: 0x50,   # B1E00-13: not completed since the last clear
}
DTC_STATUS_AVAILABILITY = 0xFF
DTC_STATUS_CLEARED = 0x50

SERVICE_NOT_SUPPORTED = 0x11
SUBFUNCTION_NOT_SUPPORTED = 0x12
INCORRECT_LENGTH = 0x13
REQUEST_OUT_OF_RANGE = 0x31
SUBFUNCTION_NOT_SUPPORTED_IN_SESSION = 0x7E
SERVICE_NOT_SUPPORTED_IN_SESSION = 0x7F

# Negative responses an ECU does not send to functionally addressed requests.
FUNCTIONAL_SUPPRESSED_NRCS = {
    SERVICE_NOT_SUPPORTED,
    SUBFUNCTION_NOT_SUPPORTED,
    REQUEST_OUT_OF_RANGE,
    SUBFUNCTION_NOT_SUPPORTED_IN_SESSION,
    SERVICE_NOT_SUPPORTED_IN_SESSION,
}

# P2 = 50 ms, P2* = 5000 ms (sent in 10 ms units).
SESSION_TIMING = bytes((0x00, 0x32, 0x01, 0xF4))


class SimulatedEcu:
    def __init__(self, dids: dict[int, bytes] | None = None, dtcs: dict[int, int] | None = None) -> None:
        self.dids = dict(DEFAULT_DIDS if dids is None else dids)
        self.dtcs = dict(DEFAULT_DTCS if dtcs is None else dtcs)
        self.session = 0x01

    def responses(self, request: bytes, functional: bool = False) -> list[bytes]:
        """All payloads to send for ``request``. Override to add e.g. NRC 0x78 first."""
        response = self.handle(request)
        if response is None:
            return []
        if functional and response[:1] == b"\x7F" and response[2] in FUNCTIONAL_SUPPRESSED_NRCS:
            return []
        return [response]

    def handle(self, request: bytes) -> bytes | None:
        """Return the response payload, or None when the response is suppressed."""
        if not request:
            return None
        sid = request[0]
        if sid == 0x10:
            return self._session(request)
        if sid == 0x11:
            return self._reset(request)
        if sid == 0x14:
            return self._clear_dtcs(request)
        if sid == 0x19:
            return self._read_dtcs(request)
        if sid == 0x22:
            return self._read_dids(request)
        if sid == 0x3E:
            return self._tester_present(request)
        return _negative(sid, SERVICE_NOT_SUPPORTED)

    def _session(self, request: bytes) -> bytes | None:
        if len(request) != 2:
            return _negative(0x10, INCORRECT_LENGTH)
        session = request[1] & 0x7F
        if session not in (0x01, 0x02, 0x03):
            return _negative(0x10, SUBFUNCTION_NOT_SUPPORTED)
        self.session = session
        return _positive(request, bytes((0x50, session)) + SESSION_TIMING)

    def _reset(self, request: bytes) -> bytes | None:
        if len(request) != 2:
            return _negative(0x11, INCORRECT_LENGTH)
        reset_type = request[1] & 0x7F
        if reset_type not in (0x01, 0x02, 0x03):
            return _negative(0x11, SUBFUNCTION_NOT_SUPPORTED)
        self.session = 0x01
        return _positive(request, bytes((0x51, reset_type)))

    def _read_dids(self, request: bytes) -> bytes:
        if len(request) < 3 or len(request) % 2 == 0:
            return _negative(0x22, INCORRECT_LENGTH)
        response = bytearray([0x62])
        for offset in range(1, len(request), 2):
            did = int.from_bytes(request[offset:offset + 2])
            if did not in self.dids:
                return _negative(0x22, REQUEST_OUT_OF_RANGE)
            response += did.to_bytes(2) + self.dids[did]
        return bytes(response)

    def _clear_dtcs(self, request: bytes) -> bytes:
        if len(request) != 4:
            return _negative(0x14, INCORRECT_LENGTH)
        group = int.from_bytes(request[1:4])
        if group == 0xFFFFFF:
            cleared = list(self.dtcs)
        elif group in self.dtcs:
            cleared = [group]
        else:
            return _negative(0x14, REQUEST_OUT_OF_RANGE)
        for code in cleared:
            self.dtcs[code] = DTC_STATUS_CLEARED
        return b"\x54"

    def _read_dtcs(self, request: bytes) -> bytes | None:
        if len(request) < 2:
            return _negative(0x19, INCORRECT_LENGTH)
        subfunction = request[1] & 0x7F
        if subfunction in (0x01, 0x02):
            if len(request) != 3:
                return _negative(0x19, INCORRECT_LENGTH)
            mask = request[2] & DTC_STATUS_AVAILABILITY
            matching = {code: status for code, status in self.dtcs.items() if status & mask}
            if subfunction == 0x01:  # format 0x01: ISO 14229-1
                body = bytes((DTC_STATUS_AVAILABILITY, 0x01)) + len(matching).to_bytes(2)
            else:
                body = bytes((DTC_STATUS_AVAILABILITY,)) + _dtc_records(matching)
        elif subfunction == 0x0A:
            if len(request) != 2:
                return _negative(0x19, INCORRECT_LENGTH)
            body = bytes((DTC_STATUS_AVAILABILITY,)) + _dtc_records(self.dtcs)
        else:
            return _negative(0x19, SUBFUNCTION_NOT_SUPPORTED)
        return _positive(request, bytes((0x59, subfunction)) + body)

    def _tester_present(self, request: bytes) -> bytes | None:
        if len(request) != 2:
            return _negative(0x3E, INCORRECT_LENGTH)
        if request[1] & 0x7F:
            return _negative(0x3E, SUBFUNCTION_NOT_SUPPORTED)
        return _positive(request, b"\x7E\x00")


def _dtc_records(dtcs: dict[int, int]) -> bytes:
    return b"".join(code.to_bytes(3) + bytes((status,)) for code, status in sorted(dtcs.items()))


def _positive(request: bytes, response: bytes) -> bytes | None:
    """Apply suppressPosRspMsgIndicationBit (bit 7 of the sub-function)."""
    return None if request[1] & 0x80 else response


def _negative(sid: int, nrc: int) -> bytes:
    return bytes((0x7F, sid, nrc))


@contextmanager
def run_simulated_ecu(
    bus: can.BusABC,
    config: IsoTpConfig,
    ecu: SimulatedEcu | None = None,
    functional_id: int | None = None,
) -> Iterator[SimulatedEcu]:
    """Serve UDS requests on ``bus`` in a background thread.

    ``config`` is the tester's view of the link; the ECU listens on its tx_id and,
    when ``functional_id`` is given, also on that ID (responses go to rx_id).
    """
    ecu = ecu or SimulatedEcu()
    stop = threading.Event()
    if functional_id is not None and functional_id in (config.tx_id, config.rx_id):
        raise ValueError("the functional ID must differ from the physical request and response IDs")

    with bus_notifier(bus) as notifier:
        physical = build_stack(bus, notifier, config.reversed())
        stacks = [(physical, False)]
        if functional_id is not None:
            functional = IsoTpConfig(**{**config.__dict__, "tx_id": config.rx_id, "rx_id": functional_id})
            stacks.append((build_stack(bus, notifier, functional), True))

        def serve() -> None:
            while not stop.is_set():
                for stack, is_functional in stacks:
                    request = stack.recv(block=not is_functional, timeout=0.02)
                    if request is not None:
                        for response in ecu.responses(bytes(request), functional=is_functional):
                            physical.send(response)

        for stack, _ in stacks:
            stack.start()
        thread = threading.Thread(target=serve, name="simulated-ecu", daemon=True)
        thread.start()
        try:
            yield ecu
        finally:
            stop.set()
            thread.join(timeout=1.0)
            for stack, _ in stacks:
                stack.stop()
