"""DTC commands (ReadDTCInformation 0x19, ClearDiagnosticInformation 0x14) and the simulators' DTCs."""

import io
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from ecu_comm import dtc
from ecu_comm.cli import main
from ecu_comm.isotp_link import IsoTpConfig
from ecu_comm.sim_ecu import SimulatedEcu, run_simulated_ecu
from ecu_comm.sim_test_board import TEST_BOARD_FUNCTIONAL_ID, TEST_BOARD_LINK, SimulatedTestBoard
from ecu_comm.transport import BusConfig, open_bus

TEST_BOARD_PROFILE = Path(__file__).parents[1] / "profiles" / "test_board.toml"


# -- parsing and formatting ---------------------------------------------------------


def test_j2012_codes():
    assert dtc.j2012_code(0xC07300) == "U0073-00"
    assert dtc.j2012_code(0x0A1B12) == "P0A1B-12"
    assert dtc.j2012_code(0x9E0013) == "B1E00-13"
    assert dtc.j2012_code(0x4123FF) == "C0123-FF"


def test_status_names():
    assert dtc.status_names(0x09) == ["testFailed", "confirmedDTC"]
    assert dtc.status_names(0x80) == ["warningIndicatorRequested"]
    assert dtc.format_record(dtc.DtcRecord(0xC07300, 0x00)) == "0xC07300 U0073-00  status 0x00 (no bits set)"


def test_parse_list_and_count():
    result = dtc.parse_list(bytes.fromhex("59 02 FF C0 73 00 09 0A 1B 12 24"), 0x02)
    assert result == dtc.DtcList(0xFF, (dtc.DtcRecord(0xC07300, 0x09), dtc.DtcRecord(0x0A1B12, 0x24)))
    assert dtc.parse_list(bytes.fromhex("59 0A 7F"), 0x0A) == dtc.DtcList(0x7F, ())
    assert dtc.parse_count(bytes.fromhex("59 01 FF 01 00 03")) == dtc.DtcCount(0xFF, 0x01, 3)


def test_short_responses_are_reported_not_rejected():
    assert dtc.parse_list(b"\x59\x02", 0x02) == dtc.DtcList(None, ())
    assert dtc.parse_count(b"\x59\x01") == dtc.DtcCount(None, None, None)
    assert dtc.format_list(dtc.DtcList(None, ()), 0x02, 0xFF, b"\x59\x02") == [
        "Response [2] 59 02: no status availability mask and no DTCs (shorter than ISO 14229-1 allows)"]


@pytest.mark.parametrize("response, sub, message", [
    ("59 02 FF C0 73", 0x02, "4 bytes each; 2 bytes follow"),
    ("59 0A FF", 0x02, "expected a response starting 59 02"),
])
def test_malformed_lists(response, sub, message):
    with pytest.raises(ValueError, match=message):
        dtc.parse_list(bytes.fromhex(response), sub)


def test_malformed_count():
    with pytest.raises(ValueError, match="expected 6 bytes for 59 01, got 4"):
        dtc.parse_count(bytes.fromhex("59 01 FF 01"))


def test_requests():
    assert dtc.read_request(0x02, 0x08) == bytes.fromhex("19 02 08")
    assert dtc.read_request(0x0A) == bytes.fromhex("19 0A")
    assert dtc.clear_request(dtc.ALL_GROUPS) == bytes.fromhex("14 FF FF FF")
    with pytest.raises(ValueError):
        dtc.clear_request(0x1000000)


# -- the simulators -----------------------------------------------------------------


def test_generic_simulator_dtcs():
    ecu = SimulatedEcu()
    assert ecu.handle(bytes.fromhex("19 01 08")) == bytes.fromhex("59 01 FF 01 00 01")
    assert ecu.handle(bytes.fromhex("19 02 08")) == bytes.fromhex("59 02 FF C0 73 00 09")
    assert ecu.handle(bytes.fromhex("19 82 08")) is None                       # suppressed positive response
    assert ecu.handle(bytes.fromhex("19 02")) == bytes.fromhex("7F 19 13")
    assert ecu.handle(bytes.fromhex("19 04 C0 73 00 01")) == bytes.fromhex("7F 19 12")
    assert ecu.handle(bytes.fromhex("14 C0 73 00")) == b"\x54"
    assert ecu.dtcs[0xC07300] == 0x50 and ecu.dtcs[0x0A1B12] == 0x24        # only that DTC cleared
    assert ecu.handle(bytes.fromhex("14 12 34 56")) == bytes.fromhex("7F 14 31")
    assert ecu.handle(bytes.fromhex("14 FF FF")) == bytes.fromhex("7F 14 13")


def test_test_board_simulator_copies_the_board_stubs():
    board = SimulatedTestBoard()
    assert board.handle(bytes.fromhex("19 02 FF")) == bytes.fromhex("59 02")
    assert board.handle(bytes.fromhex("19 01 FF")) == bytes.fromhex("59 01")
    assert board.handle(bytes.fromhex("19 0A")) == bytes.fromhex("59 0A")
    assert board.handle(bytes.fromhex("19 04 00 00 00 FF")) == bytes.fromhex("59 04")
    assert board.handle(bytes.fromhex("19 02")) == bytes.fromhex("7F 19 13")    # length table
    assert board.handle(bytes.fromhex("19 00")) == bytes.fromhex("7F 19 13")    # 0x00 never matches
    assert board.handle(bytes.fromhex("19 16")) == bytes.fromhex("7F 19 12")
    assert board.handle(bytes.fromhex("14 FF FF FF")) == b"\x54"
    assert board.handle(bytes.fromhex("14 FF FF FF 01")) == bytes.fromhex("7F 14 13")


# -- the commands -------------------------------------------------------------------


@pytest.fixture
def generic_channel():
    channel = f"{uuid4().hex}-generic"
    with open_bus(BusConfig(channel=channel)) as bus, run_simulated_ecu(bus, IsoTpConfig(0x7E0, 0x7E8)):
        yield channel


@pytest.fixture
def board_channel(monkeypatch):
    monkeypatch.delenv("ECU_COMM_SECURITY_KEY", raising=False)
    channel = f"{uuid4().hex}-board"
    with open_bus(BusConfig(channel=channel)) as bus, \
            run_simulated_ecu(bus, TEST_BOARD_LINK, SimulatedTestBoard(), TEST_BOARD_FUNCTIONAL_ID):
        yield channel


def run_generic(channel, *args):
    return main(["uds", "dtc", *args, "--channel", channel, "--tx-id", "0x7E0", "--rx-id", "0x7E8",
                 "--timeout", "0.5"])


def run_board(channel, *args):
    return main(["uds", "dtc", *args, "--ecu", str(TEST_BOARD_PROFILE), "--interface", "virtual",
                 "--channel", channel, "--timeout", "0.5"])


def test_read_count_supported_and_clear(generic_channel, capsys):
    assert run_generic(generic_channel, "count", "--mask", "0x08") == 0
    assert capsys.readouterr().out == "1 DTC with status & 0x08 (availability mask 0xFF, format 0x01 ISO 14229-1)\n"
    assert run_generic(generic_channel, "read") == 0
    assert capsys.readouterr().out.splitlines() == [
        "Status availability mask 0xFF",
        "3 DTCs with status & 0xFF:",
        "  0x0A1B12 P0A1B-12  status 0x24 pendingDTC testFailedSinceLastClear",
        "  0x9E0013 B1E00-13  status 0x50 testNotCompletedSinceLastClear testNotCompletedThisOperationCycle",
        "  0xC07300 U0073-00  status 0x09 testFailed confirmedDTC",
    ]
    assert run_generic(generic_channel, "clear") == 0
    assert capsys.readouterr().out == "Clear accepted (all DTC groups)\n"
    assert run_generic(generic_channel, "read", "--mask", "0x0F") == 0
    assert capsys.readouterr().out.splitlines() == ["Status availability mask 0xFF", "No DTCs with status & 0x0F"]
    assert run_generic(generic_channel, "supported") == 0
    assert "3 DTCs supported:" in capsys.readouterr().out


def test_negative_response_exits_1(generic_channel, capsys):
    assert run_generic(generic_channel, "clear", "0x123456") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "negative response 0x31 (RequestOutOfRange)" in captured.err


def test_board_short_answers(board_channel, capsys):
    assert run_board(board_channel, "read", "--mask", "0x08") == 0
    assert capsys.readouterr().out == (
        "Response [2] 59 02: no status availability mask and no DTCs (shorter than ISO 14229-1 allows)\n")
    assert run_board(board_channel, "count") == 0
    assert "Response [2] 59 01: no status availability mask and no count" in capsys.readouterr().out
    assert run_board(board_channel, "clear") == 0
    assert capsys.readouterr().out == "Clear accepted (all DTC groups)\n"


def test_dtc_in_the_shell(board_channel, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("dtc supported\ndtc read --mask 0x08\n"))
    assert main(["uds", "shell", "--ecu", str(TEST_BOARD_PROFILE), "--interface", "virtual",
                 "--channel", board_channel, "--timeout", "0.5", "--keep-alive", "0"]) == 0
    out = capsys.readouterr().out
    assert "> dtc supported\nResponse [2] 59 0A: no status availability mask" in out
    assert "> dtc read --mask 0x08\nResponse [2] 59 02:" in out


@pytest.mark.parametrize("args, message", [
    (["read", "--mask", "0x100"], "expected a byte value"),
    (["clear", "0x1000000"], "expected a 3-byte DTC group"),
])
def test_bad_arguments(args, message, capsys):
    with pytest.raises(SystemExit):
        main(["uds", "dtc", *args, "--channel", "x", "--tx-id", "1", "--rx-id", "2"])
    assert message in capsys.readouterr().err
