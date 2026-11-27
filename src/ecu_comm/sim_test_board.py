"""A simulated "test_board" ECU that mirrors a real bench board's UDS behaviour.

Link: requests on 0x60A, responses on 0x68A, functional requests on 0x601,
padding 0xAA, classic CAN. Everything here is public or made up: identification
values are placeholders and ``SIM_SECRET`` is a throw-away key for simulation only.

Behaviour copied from the board:

* sessions 0x01/0x02/0x03 with P2 100 ms and P2* 2000 ms; a session change or
  reset relocks SecurityAccess; S3 (``s3`` seconds without a request) falls back
  to the default session;
* DID 0xF186 reports the active session, 0xF1A0 the active image (0x02 = app);
* SecurityAccess with a 16-byte seed and key = AES-128-ECB(secret, seed); an
  all-zero seed means already unlocked; wrong keys give 0x35, the last allowed
  attempt 0x36, then seed requests give 0x37 until ``lockout`` seconds pass;
* RoutineControl 0xFD00 "sensor transaction": extended session (else 0x7F),
  level 0x01 unlocked (else 0x33), PWM off (else 0x22), record
  ``device tx_size rx_size tx[tx_size]`` checked like the board (0x13, 0x31). With
  no sensors fitted it answers status 3 (timeout), error 0x36, length rx_size.
  Sub-function 0x03 repeats the last reply. Other RIDs answer with a bare
  positive response, as the board does;
* ControlDTCSetting (0x85) and CommunicationControl (0x28) for the functional
  pre-programming sequence.

Assumptions (not checked against the board): WriteDataByIdentifier only accepts
0xF184 (12 bytes) and needs no unlock; other known DIDs are read-only (0x31).
"""

import os
import time
from collections.abc import Callable

from .isotp_link import IsoTpConfig
from .security import aes128_ecb
from .sim_ecu import (
    INCORRECT_LENGTH,
    REQUEST_OUT_OF_RANGE,
    SERVICE_NOT_SUPPORTED_IN_SESSION,
    SUBFUNCTION_NOT_SUPPORTED,
    SimulatedEcu,
    _negative,
    _positive,
)

TEST_BOARD_LINK = IsoTpConfig(tx_id=0x60A, rx_id=0x68A, padding=0xAA)
TEST_BOARD_FUNCTIONAL_ID = 0x601

# Throw-away AES key for the simulator only. It is not any real ECU's key.
SIM_SECRET = bytes.fromhex("00112233445566778899AABBCCDDEEFF")

SESSION_DEFAULT, SESSION_PROGRAMMING, SESSION_EXTENDED = 0x01, 0x02, 0x03
# P2 = 100 ms, P2* = 2000 ms (10 ms units).
TEST_BOARD_SESSION_TIMING = bytes.fromhex("0064 00C8")
SEED_LENGTH = 16
LEVEL_BASIC, LEVEL_PROGRAMMING = 0x01, 0x03
RID_SENSOR_TRANSACTION = 0xFD00

CONDITIONS_NOT_CORRECT = 0x22
REQUEST_SEQUENCE_ERROR = 0x24
SECURITY_ACCESS_DENIED = 0x33
INVALID_KEY = 0x35
EXCEEDED_ATTEMPTS = 0x36
DELAY_NOT_EXPIRED = 0x37

STATUS_TIMEOUT, ERROR_TIMEOUT = 0x03, 0x36

TEST_BOARD_DIDS = {
    0xF110: bytes((1, 0, 0)),             # calibration version
    0xF111: bytes((1, 2, 0)),             # bootloader version
    0xF112: bytes((0, 1, 0)),             # hardware version
    0xF183: b"BOOT-FP-0001",              # bootloader fingerprint, 12 bytes
    0xF184: b"APPL-FP-0001",              # application fingerprint, 12 bytes
    0xF189: bytes((0, 3, 1)),             # software version (mirrors 0xF195)
    0xF18C: b"TB0000000001",              # serial number, 12 bytes
    0xF190: b"TESTBOARD00000001",         # VIN, 17 bytes
    0xF195: bytes((0, 3, 1)),             # supplier software version
    0xF1A0: bytes((0x02,)),               # active image: application
    0xFD10: bytes(6),                     # phase currents U, V, W: int16 BE, 0.1 A
    0xFD11: bytes(4),                     # sensor addresses U, V, W, found flag
    0xFF00: bytes((0x02, 0x00, 0x00, 0x00)),  # UDS version
}
WRITABLE_DIDS = {0xF184: 12}


class SimulatedTestBoard(SimulatedEcu):
    def __init__(
        self,
        secret: bytes = SIM_SECRET,
        *,
        levels: tuple[int, ...] = (LEVEL_BASIC, LEVEL_PROGRAMMING),
        max_attempts: int = 3,
        lockout: float = 10.0,
        s3: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(TEST_BOARD_DIDS)
        self.secret = secret
        self.levels = levels
        self.max_attempts = max_attempts
        self.lockout = lockout
        self.s3 = s3
        self.clock = clock
        self.pwm_on = False
        self.unlocked: int | None = None
        self.last_reply: bytes | None = None
        self.sensor_requests: list[tuple[int, bytes, int]] = []
        self._seed: tuple[int, bytes] | None = None
        self._failed_attempts = 0
        self._locked_until = 0.0
        self._last_request = clock()

    # -- dispatch ---------------------------------------------------------------

    def handle(self, request: bytes) -> bytes | None:
        if not request:
            return None
        now = self.clock()
        if self.session != SESSION_DEFAULT and now - self._last_request > self.s3:
            self._enter_session(SESSION_DEFAULT)
        self._last_request = now
        handler = {
            0x27: self._security_access,
            0x28: self._communication_control,
            0x2E: self._write_did,
            0x31: self._routine_control,
            0x85: self._control_dtc_setting,
        }.get(request[0])
        return handler(request) if handler else super().handle(request)

    def _enter_session(self, session: int) -> None:
        self.session = session
        self.unlocked = None
        self._seed = None

    def _session(self, request: bytes) -> bytes | None:
        if len(request) != 2:
            return _negative(0x10, INCORRECT_LENGTH)
        session = request[1] & 0x7F
        if session not in (SESSION_DEFAULT, SESSION_PROGRAMMING, SESSION_EXTENDED):
            return _negative(0x10, SUBFUNCTION_NOT_SUPPORTED)
        self._enter_session(session)
        return _positive(request, bytes((0x50, session)) + TEST_BOARD_SESSION_TIMING)

    def _reset(self, request: bytes) -> bytes | None:
        response = super()._reset(request)
        if response is None or response[0] == 0x51:
            self._enter_session(SESSION_DEFAULT)
        return response

    # -- data identifiers -------------------------------------------------------

    def _read_dids(self, request: bytes) -> bytes:
        self.dids[0xF186] = bytes((self.session,))
        return super()._read_dids(request)

    def _write_did(self, request: bytes) -> bytes:
        if len(request) < 4:
            return _negative(0x2E, INCORRECT_LENGTH)
        did = int.from_bytes(request[1:3])
        if did not in WRITABLE_DIDS:
            return _negative(0x2E, REQUEST_OUT_OF_RANGE)
        if len(request) - 3 != WRITABLE_DIDS[did]:
            return _negative(0x2E, INCORRECT_LENGTH)
        self.dids[did] = bytes(request[3:])
        return bytes((0x6E,)) + request[1:3]

    # -- security access --------------------------------------------------------

    def _security_access(self, request: bytes) -> bytes | None:
        if len(request) < 2:
            return _negative(0x27, INCORRECT_LENGTH)
        sub = request[1] & 0x7F
        level = sub if sub % 2 else sub - 1
        if level not in self.levels:
            return _negative(0x27, SUBFUNCTION_NOT_SUPPORTED)
        if self.session == SESSION_DEFAULT:
            return _negative(0x27, SERVICE_NOT_SUPPORTED_IN_SESSION)
        if sub % 2:
            return self._request_seed(request, level)
        return self._send_key(request, level)

    def _request_seed(self, request: bytes, level: int) -> bytes | None:
        if len(request) != 2:
            return _negative(0x27, INCORRECT_LENGTH)
        if self.clock() < self._locked_until:
            return _negative(0x27, DELAY_NOT_EXPIRED)
        if self.unlocked == level:
            seed = bytes(SEED_LENGTH)
        else:
            seed = os.urandom(SEED_LENGTH)
            self._seed = (level, seed)
        return _positive(request, bytes((0x67, level)) + seed)

    def _send_key(self, request: bytes, level: int) -> bytes | None:
        if len(request) != 2 + SEED_LENGTH:
            return _negative(0x27, INCORRECT_LENGTH)
        if self._seed is None or self._seed[0] != level:
            return _negative(0x27, REQUEST_SEQUENCE_ERROR)
        seed = self._seed[1]
        self._seed = None
        if request[2:] != aes128_ecb(seed, level, self.secret):
            self._failed_attempts += 1
            if self._failed_attempts >= self.max_attempts:
                self._failed_attempts = 0
                self._locked_until = self.clock() + self.lockout
                return _negative(0x27, EXCEEDED_ATTEMPTS)
            return _negative(0x27, INVALID_KEY)
        self._failed_attempts = 0
        self.unlocked = level
        return _positive(request, bytes((0x67, level + 1)))

    # -- routines ---------------------------------------------------------------

    def _routine_control(self, request: bytes) -> bytes | None:
        if len(request) < 4:
            return _negative(0x31, INCORRECT_LENGTH)
        sub = request[1] & 0x7F
        if sub not in (0x01, 0x02, 0x03):
            return _negative(0x31, SUBFUNCTION_NOT_SUPPORTED)
        rid = int.from_bytes(request[2:4])
        header = bytes((0x71, sub)) + request[2:4]
        if rid != RID_SENSOR_TRANSACTION:
            return _positive(request, header)
        if self.session != SESSION_EXTENDED:
            return _negative(0x31, SERVICE_NOT_SUPPORTED_IN_SESSION)
        if self.unlocked != LEVEL_BASIC:
            return _negative(0x31, SECURITY_ACCESS_DENIED)
        if sub == 0x03:
            if self.last_reply is None:
                return _negative(0x31, REQUEST_SEQUENCE_ERROR)
            return _positive(request, header + self.last_reply)
        if sub == 0x02:
            return _negative(0x31, SUBFUNCTION_NOT_SUPPORTED)
        record = request[4:]
        if len(record) < 4 or len(record) != 3 + record[1]:
            return _negative(0x31, INCORRECT_LENGTH)
        device, tx_size, rx_size = record[0], record[1], record[2]
        if not 1 <= tx_size <= 5 or rx_size > 5 or device > 7:
            return _negative(0x31, REQUEST_OUT_OF_RANGE)
        if self.pwm_on:
            return _negative(0x31, CONDITIONS_NOT_CORRECT)
        self.sensor_requests.append((device, bytes(record[3:]), rx_size))
        self.last_reply = self.sensor_reply(device, bytes(record[3:]), rx_size)
        return _positive(request, header + self.last_reply)

    def sensor_reply(self, device: int, frame: bytes, rx_size: int) -> bytes:
        """The routine's ``status error len data`` record. Default: no sensor answers."""
        return bytes((STATUS_TIMEOUT, ERROR_TIMEOUT, rx_size))

    # -- pre-programming services -----------------------------------------------

    def _control_dtc_setting(self, request: bytes) -> bytes | None:
        if len(request) != 2:
            return _negative(0x85, INCORRECT_LENGTH)
        if request[1] & 0x7F not in (0x01, 0x02):
            return _negative(0x85, SUBFUNCTION_NOT_SUPPORTED)
        if self.session == SESSION_DEFAULT:
            return _negative(0x85, SERVICE_NOT_SUPPORTED_IN_SESSION)
        return _positive(request, bytes((0xC5, request[1] & 0x7F)))

    def _communication_control(self, request: bytes) -> bytes | None:
        if len(request) != 3:
            return _negative(0x28, INCORRECT_LENGTH)
        if request[1] & 0x7F > 0x03:
            return _negative(0x28, SUBFUNCTION_NOT_SUPPORTED)
        if self.session == SESSION_DEFAULT:
            return _negative(0x28, SERVICE_NOT_SUPPORTED_IN_SESSION)
        return _positive(request, bytes((0x68, request[1] & 0x7F)))
