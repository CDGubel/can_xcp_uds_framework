"""test_board UDS checks: against the simulator by default, or a real board with --board-profile.

Unmarked checks only read, change sessions or send suppressed functional requests,
so they are safe on hardware. ``sim_only`` checks write, unlock or disable
communication and never reach hardware.
"""

import pytest

SESSION_TIMING = bytes.fromhex("0064 00C8")
DID_ACTIVE_SESSION, DID_ACTIVE_IMAGE = 0xF186, 0xF1A0


@pytest.fixture(autouse=True)
def back_to_default(board):
    yield
    board.request("10 01")


def nrc(response: bytes) -> int | None:
    return response[2] if response[:1] == b"\x7F" and len(response) == 3 else None


# -- smoke --------------------------------------------------------------------------


def test_tester_present(board):
    assert board.request("3E 00") == bytes.fromhex("7E 00")


def test_application_image_is_active(board):
    assert board.read(DID_ACTIVE_IMAGE) == b"\x02"


def test_default_session_after_previous_test(board):
    assert board.read(DID_ACTIVE_SESSION) == b"\x01"


@pytest.mark.parametrize("did", [0xF189, 0xF195, 0xF110, 0xF111])
def test_version_dids_are_three_bytes(board, did):
    assert len(board.read(did)) == 3


def test_sw_version_mirrors_supplier_sw_version(board):
    assert board.read(0xF189) == board.read(0xF195)


# -- data identifiers ---------------------------------------------------------------


def test_unknown_did_is_out_of_range(board):
    assert nrc(board.request("22 FE ED")) == 0x31


@pytest.mark.parametrize("request_hex", ["22", "22 F1", "22 F1 86 F1"])
def test_read_bad_length_is_rejected(board, request_hex):
    assert nrc(board.request(request_hex)) == 0x13


def test_read_several_dids_in_one_request(board):
    response = board.request("22 F1 86 F1 83")
    assert response[:4] == bytes.fromhex("62 F1 86 01")
    assert response[4:6] == bytes.fromhex("F1 83") and len(response[6:]) == 12


@pytest.mark.parametrize("did,length", [(0xF183, 12), (0xF184, 12), (0xF18C, 12), (0xF190, 17)])
def test_identification_did_lengths(board, did, length):
    assert len(board.read(did)) == length


@pytest.mark.sim_only
@pytest.mark.parametrize("length", [11, 13])
def test_fingerprint_write_wrong_length_is_rejected(board, length):
    assert nrc(board.request("2E F1 84" + "00" * length)) == 0x13


@pytest.mark.sim_only
def test_read_only_did_write_is_rejected(board):
    assert board.request("2E F1 86 03")[:2] == bytes.fromhex("7F 2E")


# -- sessions -----------------------------------------------------------------------


@pytest.mark.parametrize("session", [0x01, 0x03])
def test_session_control_reports_timing_and_active_session(board, session):
    response = board.request(f"10 {session:02X}")
    assert response == bytes((0x50, session)) + SESSION_TIMING
    assert board.read(DID_ACTIVE_SESSION) == bytes((session,))


@pytest.mark.sim_only
def test_programming_session_is_accepted(board):
    assert board.request("10 02")[:2] == bytes.fromhex("50 02")
    assert board.read(DID_ACTIVE_SESSION) == b"\x02"


@pytest.mark.parametrize("session", [0x00, 0x05, 0x7F])
def test_unknown_session_is_rejected(board, session):
    assert nrc(board.request(f"10 {session:02X}")) == 0x12


@pytest.mark.parametrize("request_hex", ["10", "10 03 00"])
def test_session_bad_length_is_rejected(board, request_hex):
    assert nrc(board.request(request_hex)) == 0x13


# -- functional addressing ------------------------------------------------------------


def test_functional_session_control_gets_a_response(board):
    assert board.functional("10 03") == [bytes((0x50, 0x03)) + SESSION_TIMING]
    assert board.read(DID_ACTIVE_SESSION) == b"\x03"


def test_functional_session_control_suppressed(board):
    assert board.functional("10 83") == []
    assert board.read(DID_ACTIVE_SESSION) == b"\x03"


def test_functional_tester_present_suppressed(board):
    assert board.functional("3E 80") == []


@pytest.mark.sim_only
def test_pre_programming_sequence(board):
    """10 83, 85 82, 28 81 01 sent functionally: all suppressed, the board stays in the extended session."""
    for request_hex in ("10 83", "85 82", "28 81 01"):
        assert board.functional(request_hex) == []
    assert board.read(DID_ACTIVE_SESSION) == b"\x03"
