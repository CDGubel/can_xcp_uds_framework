"""Behaviour of the simulated test_board that the hardware checks cannot cover safely."""

import pytest

from ecu_comm.security import aes128_ecb
from ecu_comm.sim_test_board import SIM_SECRET, SimulatedTestBoard


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ecu(clock):
    return SimulatedTestBoard(clock=clock)


def ask(ecu, request_hex, functional=False):
    responses = ecu.responses(bytes.fromhex(request_hex), functional)
    return responses[0] if responses else None


def key_for(seed_response: bytes, secret=SIM_SECRET) -> str:
    return aes128_ecb(seed_response[2:], seed_response[1], secret).hex()


def unlock(ecu, level=1):
    seed = ask(ecu, f"27 {level:02X}")
    assert seed[:2] == bytes((0x67, level)) and len(seed) == 18
    assert ask(ecu, f"27 {level + 1:02X}" + key_for(seed)) == bytes((0x67, level + 1))


def test_security_needs_a_non_default_session(ecu):
    assert ask(ecu, "27 01") == bytes.fromhex("7F 27 7F")


def test_unlock_then_zero_seed_and_relock_on_session_change(ecu):
    ask(ecu, "10 03")
    unlock(ecu)
    assert ask(ecu, "27 01") == bytes.fromhex("67 01") + bytes(16)
    ask(ecu, "10 03")
    assert ecu.unlocked is None


def test_wrong_keys_then_attempt_limit_then_delay(ecu, clock):
    """Like the board: 0x35, 0x35, 0x36, then 0x37 until the delay has passed."""
    ask(ecu, "10 03")
    for nrc in (0x35, 0x35, 0x36):
        assert ask(ecu, "27 01")[:2] == bytes.fromhex("67 01")
        assert ask(ecu, "27 02" + "00" * 16) == bytes((0x7F, 0x27, nrc))
    assert ask(ecu, "27 01") == bytes.fromhex("7F 27 37")
    clock.now += 4.5
    assert ask(ecu, "27 01") == bytes.fromhex("7F 27 37")
    clock.now += 6.0
    ask(ecu, "10 03")  # S3 has expired meanwhile, as in the bench check
    unlock(ecu)


@pytest.mark.parametrize(
    "request_hex,nrc",
    [("27 02" + "00" * 16, 0x24), ("27 05", 0x12), ("27 01 00", 0x13), ("27 02 00", 0x13)],
)
def test_security_request_errors(ecu, request_hex, nrc):
    ask(ecu, "10 03")
    assert ask(ecu, request_hex) == bytes((0x7F, 0x27, nrc))


def test_s3_timeout_falls_back_to_default_and_relocks(ecu, clock):
    ask(ecu, "10 03")
    unlock(ecu)
    clock.now += 4.0
    ask(ecu, "3E 80")
    clock.now += 4.0
    assert ask(ecu, "22 F1 86") == bytes.fromhex("62 F1 86 03")
    clock.now += 5.5
    assert ask(ecu, "22 F1 86") == bytes.fromhex("62 F1 86 01")
    assert ecu.unlocked is None


def test_sensor_transaction_preconditions(ecu):
    transaction = "31 01 FD 00 00 01 02 06"
    assert ask(ecu, transaction) == bytes.fromhex("7F 31 7F")
    ask(ecu, "10 03")
    assert ask(ecu, transaction) == bytes.fromhex("7F 31 33")
    unlock(ecu, 3)
    assert ask(ecu, transaction) == bytes.fromhex("7F 31 33"), "PROGRAMMING does not open the routine"
    unlock(ecu, 1)
    ecu.pwm_on = True
    assert ask(ecu, transaction) == bytes.fromhex("7F 31 22")
    ecu.pwm_on = False
    assert ask(ecu, "31 03 FD 00") == bytes.fromhex("7F 31 24")
    assert ask(ecu, transaction) == bytes.fromhex("71 01 FD 00 03 36 02")
    assert ask(ecu, "31 03 FD 00") == bytes.fromhex("71 03 FD 00 03 36 02")
    assert ecu.sensor_requests == [(0, b"\x06", 2)]


@pytest.mark.parametrize(
    "record,nrc",
    [("00 01 02", 0x13), ("00 02 02 06", 0x13), ("00 06 00" + " 06" * 6, 0x31), ("00 01 06 06", 0x31),
     ("08 01 00 06", 0x31)],
)
def test_sensor_transaction_record_checks(ecu, record, nrc):
    ask(ecu, "10 03")
    unlock(ecu)
    assert ask(ecu, "31 01 FD 00 " + record) == bytes((0x7F, 0x31, nrc))
    assert ecu.sensor_requests == []


def test_other_routines_get_a_bare_positive_response(ecu):
    assert ask(ecu, "31 01 FD 7F") == bytes.fromhex("71 01 FD 7F")


def test_functional_requests_drop_suppressible_nrcs(ecu):
    assert ask(ecu, "22 FE ED", functional=True) is None
    assert ask(ecu, "22 FE ED") == bytes.fromhex("7F 22 31")
    assert ask(ecu, "10", functional=True) == bytes.fromhex("7F 10 13")
