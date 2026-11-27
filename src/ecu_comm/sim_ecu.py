"""A minimal simulated UDS server for development without an ECU.

It answers DiagnosticSessionControl (0x10), ECUReset (0x11), ReadDataByIdentifier
(0x22) and TesterPresent (0x3E). Other services get NRC 0x11 serviceNotSupported.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import can

from .isotp_link import IsoTpConfig, open_isotp

DEFAULT_DIDS = {
    0xF187: b"SIM-PART-0001",
    0xF18C: b"SN000000000042",
    0xF190: b"WSIMULATED0000001",
    0xF195: b"\x01\x02\x03",
}

SERVICE_NOT_SUPPORTED = 0x11
SUBFUNCTION_NOT_SUPPORTED = 0x12
INCORRECT_LENGTH = 0x13
REQUEST_OUT_OF_RANGE = 0x31

# P2 = 50 ms, P2* = 5000 ms (sent in 10 ms units).
SESSION_TIMING = bytes((0x00, 0x32, 0x01, 0xF4))


class SimulatedEcu:
    def __init__(self, dids: dict[int, bytes] | None = None) -> None:
        self.dids = dict(DEFAULT_DIDS if dids is None else dids)
        self.session = 0x01

    def responses(self, request: bytes) -> list[bytes]:
        """All payloads to send for ``request``. Override to add e.g. NRC 0x78 first."""
        response = self.handle(request)
        return [] if response is None else [response]

    def handle(self, request: bytes) -> bytes | None:
        """Return the response payload, or None when the response is suppressed."""
        if not request:
            return None
        sid = request[0]
        if sid == 0x10:
            return self._session(request)
        if sid == 0x11:
            return self._reset(request)
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

    def _tester_present(self, request: bytes) -> bytes | None:
        if len(request) != 2:
            return _negative(0x3E, INCORRECT_LENGTH)
        if request[1] & 0x7F:
            return _negative(0x3E, SUBFUNCTION_NOT_SUPPORTED)
        return _positive(request, b"\x7E\x00")


def _positive(request: bytes, response: bytes) -> bytes | None:
    """Apply suppressPosRspMsgIndicationBit (bit 7 of the sub-function)."""
    return None if request[1] & 0x80 else response


def _negative(sid: int, nrc: int) -> bytes:
    return bytes((0x7F, sid, nrc))


@contextmanager
def run_simulated_ecu(
    bus: can.BusABC, config: IsoTpConfig, ecu: SimulatedEcu | None = None
) -> Iterator[SimulatedEcu]:
    """Serve UDS requests on ``bus`` in a background thread.

    ``config`` is the tester's view of the link; the ECU listens on its tx_id.
    """
    ecu = ecu or SimulatedEcu()
    stop = threading.Event()

    with open_isotp(bus, config.reversed()) as stack:
        def serve() -> None:
            while not stop.is_set():
                request = stack.recv(block=True, timeout=0.05)
                if request is not None:
                    for response in ecu.responses(bytes(request)):
                        stack.send(response)

        thread = threading.Thread(target=serve, name="simulated-ecu", daemon=True)
        thread.start()
        try:
            yield ecu
        finally:
            stop.set()
            thread.join(timeout=1.0)
