"""Named DIDs (profile [dids] table) and recording UDS traffic with --log."""

import io
import sys
from pathlib import Path
from uuid import uuid4

import can
import pytest

from ecu_comm import dids
from ecu_comm.cli import main
from ecu_comm.ecu_profile import load_profile
from ecu_comm.sim_ecu import run_simulated_ecu
from ecu_comm.transport import BusConfig, LoggedBus, open_bus

TEST_BOARD_PROFILE = Path(__file__).parents[1] / "profiles" / "test_board.toml"


def table(text: str) -> dids.DidTable:
    import tomllib

    return dids.parse_table(tomllib.loads(text)["dids"])


CURRENTS = """
[dids.0xFD10]
name = "PhaseCurrents"
fields = [
    { name = "U", type = "int16", scale = 0.1, unit = "A" },
    { name = "V", type = "int16", scale = 0.1, unit = "A" },
    { name = "W", type = "int16", scale = 0.1, unit = "A" },
]
"""


# -- the table --------------------------------------------------------------------


def test_decode_scaled_signed_fields():
    definition = table(CURRENTS)[0xFD10]
    assert definition.length == 6
    payload = bytes.fromhex("000D FFF6 003F")
    assert dids.format_fields(definition, payload) == "U=1.3 A, V=-1.0 A, W=6.3 A"


def test_choices_ascii_bytes_and_offset():
    defs = table("""
[dids.0xF186]
name = "Session"
fields = [{ name = "Session", type = "uint8", choices = { 1 = "default", 0x03 = "extended" } }]

[dids.0x0100]
name = "Mixed"
fields = [
    { name = "Temp", type = "uint8", offset = -40, unit = "degC" },
    { name = "Code", type = "ascii", length = 4 },
    { name = "Raw", type = "bytes" },
]
""")
    assert dids.format_fields(defs[0xF186], b"\x03") == "Session=extended"
    assert dids.format_fields(defs[0xF186], b"\x07") == "Session=7"  # not a named value: the number
    assert defs[0x0100].length is None
    assert (dids.format_fields(defs[0x0100], b"\x41AB\x00\x00\x01\x02")
            == 'Temp=25 degC, Code="AB", Raw=01 02')
    assert dids.format_fields(defs[0x0100], b"\x41ABCD") == 'Temp=25 degC, Code="ABCD", Raw=(empty)'


def test_wrong_length_is_reported_not_guessed():
    definition = table(CURRENTS)[0xFD10]
    assert dids.format_fields(definition, b"\x00\x01") == "(cannot decode: expected 6 bytes, got 2)"


def test_unsigned_24_and_32_bit():
    defs = table("""
[dids.0x0200]
name = "Counters"
fields = [{ name = "A", type = "uint24" }, { name = "B", type = "int32", scale = 0.001, unit = "s" }]
""")
    assert dids.format_fields(defs[0x0200], bytes.fromhex("010000 FFFFFC18")) == "A=65536, B=-1.000 s"


def test_resolve_numbers_and_names():
    defs = table(CURRENTS)
    assert dids.resolve(defs, "0xFD10") == 0xFD10
    assert dids.resolve(defs, "61840") == 0xF190
    assert dids.resolve(defs, "phasecurrents") == 0xFD10
    with pytest.raises(ValueError, match="no DID named Nope .names: PhaseCurrents"):
        dids.resolve(defs, "Nope")
    with pytest.raises(ValueError, match="profile has no \\[dids\\] table"):
        dids.resolve({}, "VIN")
    with pytest.raises(ValueError, match="not a 16-bit DID"):
        dids.resolve(defs, "0x10000")


def test_describe():
    assert dids.describe(table(CURRENTS)[0xFD10]) == [
        "0xFD10 PhaseCurrents [6]", "  U: int16, x0.1 A", "  V: int16, x0.1 A", "  W: int16, x0.1 A"]


@pytest.mark.parametrize("text, message", [
    ('[dids.F190]\nname = "VIN"', "a DID must be a number"),
    ('[dids.0x1F190]\nname = "VIN"', "a DID is a 16-bit number"),
    ('[dids.0xF190]\nname = "V I N"', "name must be one word"),
    ('[dids.0xF190]\nname = "0x12"', "looks like a number"),
    ('[dids.0xF190]\nname = "VIN"\ncolour = "red"', "unknown key"),
    ('[dids.0xF190]\nname = "VIN"\n[dids.0xF191]\nname = "vin"', "already used by 0xF190"),
    ('[dids.0xF190]\nname = "VIN"\n[dids.61840]\nname = "Other"', "defined twice"),
    ('[dids.0xF190]\nname = "A"\nfields = [{ name = "X", type = "float" }]', "type must be one of"),
    ('[dids.0xF190]\nname = "A"\nfields = [{ name = "X", type = "uint8", length = 2 }]', "fixed length"),
    ('[dids.0xF190]\nname = "A"\nfields = [{ name = "X", type = "ascii" }, { name = "Y", type = "uint8" }]',
     "needs a length unless it is the last field"),
    ('[dids.0xF190]\nname = "A"\nfields = [{ name = "X", type = "ascii", length = 0 }]', "positive whole number"),
    ('[dids.0xF190]\nname = "A"\nfields = [{ name = "X", type = "uint8", scale = 0 }]', "scale must not be 0"),
    ('[dids.0xF190]\nname = "A"\nfields = [{ name = "X", type = "bytes", scale = 2 }]', "take no scale"),
    ('[dids.0xF190]\nname = "A"\nfields = [{ name = "X", type = "uint8", choices = { a = "b" } }]',
     "whole number"),
    ('[dids.0xF190]\nname = "A"\nfields = [{ name = "X", type = "uint8" }, { name = "X", type = "uint8" }]',
     "field X is given twice"),
])
def test_invalid_tables(text, message):
    with pytest.raises(ValueError, match=message):
        table(text)


def test_profile_reports_the_file(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[dids.0xF190]\nname = "V I N"\n')
    with pytest.raises(ValueError, match=r"bad\.toml: dids\.0xF190: name must be one word"):
        load_profile(path)


def test_test_board_profile_names_its_dids():
    table_ = load_profile(TEST_BOARD_PROFILE)["dids"]
    assert table_[0xFD10].name == "PhaseCurrents"
    assert {0xF186, 0xF190, 0xF1A0, 0xFD10, 0xFD11} <= set(table_)


# -- the commands -----------------------------------------------------------------


@pytest.fixture
def board_channel(monkeypatch):
    from ecu_comm.sim_test_board import TEST_BOARD_FUNCTIONAL_ID, TEST_BOARD_LINK, SimulatedTestBoard

    monkeypatch.delenv("ECU_COMM_SECURITY_KEY", raising=False)
    channel = f"{uuid4().hex}-board"  # ends in letters: BLF takes trailing digits as the channel number
    with open_bus(BusConfig(channel=channel)) as bus, \
            run_simulated_ecu(bus, TEST_BOARD_LINK, SimulatedTestBoard(), TEST_BOARD_FUNCTIONAL_ID):
        yield channel


def run_board(channel, command, *args):
    return main(["uds", command, "--ecu", str(TEST_BOARD_PROFILE), "--interface", "virtual",
                 "--channel", channel, "--timeout", "0.5", *args])


def test_read_did_by_name_shows_fields(board_channel, capsys):
    assert run_board(board_channel, "read-did", "PhaseCurrents", "vin", "0xF186", "0xFF00") == 0
    assert capsys.readouterr().out.splitlines() == [
        "0xFD10 PhaseCurrents [6] 00 00 00 00 00 00  U=0.0 A, V=0.0 A, W=0.0 A",
        '0xF190 VIN [17] 54 45 53 54 42 4F 41 52 44 30 30 30 30 30 30 30 31  VIN="TESTBOARD00000001"',
        "0xF186 ActiveSession [1] 01  Session=default",
        "0xFF00 [4] 02 00 00 00",  # not in the table: raw bytes as before
    ]


def test_read_did_keeps_earlier_values_when_a_later_one_is_refused(board_channel, capsys):
    assert run_board(board_channel, "read-did", "VIN", "0xFEED") == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("0xF190 VIN [17]")
    assert "negative response 0x31 (RequestOutOfRange)" in captured.err


def test_unknown_name_sends_nothing(board_channel, capsys):
    assert run_board(board_channel, "read-did", "VIN", "Nope") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no DID named Nope" in captured.err


def test_write_did_by_name(board_channel, tmp_path, capsys):
    script = tmp_path / "write.uds"
    script.write_text('session extended\nunlock 1\nwrite-did AppFingerprint "41 42 43 44 45 46 47 48 49 4A 4B 4C"\n'
                      "read-did AppFingerprint\n")
    key = tmp_path / "sim.key"
    from ecu_comm.sim_test_board import SIM_SECRET

    key.write_text(SIM_SECRET.hex())
    assert run_board(board_channel, "shell", str(script), "--key-file", str(key)) == 0
    out = capsys.readouterr().out
    assert "0xF184 written [12]" in out
    assert '0xF184 AppFingerprint [12] 41 42 43 44 45 46 47 48 49 4A 4B 4C  Fingerprint="ABCDEFGHIJKL"' in out


def test_dids_command_lists_without_a_bus(capsys):
    assert main(["uds", "dids", "--ecu", str(TEST_BOARD_PROFILE), "PhaseCurrents", "0xF186"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "0xFD10 PhaseCurrents [6]", "  U: int16, x0.1 A", "  V: int16, x0.1 A", "  W: int16, x0.1 A",
        "0xF186 ActiveSession [1]", "  Session: uint8, 1=default 2=programming 3=extended",
    ]


def test_dids_command_errors(tmp_path, capsys):
    plain = tmp_path / "plain.toml"
    plain.write_text("[isotp]\ntx_id = 0x7E0\nrx_id = 0x7E8\n")
    assert main(["uds", "dids", "--ecu", str(plain)]) == 1
    assert "the profile has no [dids] table" in capsys.readouterr().err
    assert main(["uds", "dids", "--ecu", str(TEST_BOARD_PROFILE), "0xFF00"]) == 1
    assert "0xFF00 is not in the profile's [dids] table" in capsys.readouterr().err


# -- recording --------------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".asc", ".blf", ".log"])
def test_log_records_requests_and_responses(board_channel, tmp_path, capsys, suffix):
    log = tmp_path / f"uds{suffix}"
    assert run_board(board_channel, "read-did", "VIN", "--log", str(log)) == 0
    frames = list(can.LogReader(log))
    sent = [f for f in frames if f.arbitration_id == 0x60A]
    received = [f for f in frames if f.arbitration_id == 0x68A]
    assert bytes(sent[0].data) == bytes.fromhex("03 22 F1 90 AA AA AA AA")  # the request
    assert bytes(received[0].data[:5]) == bytes.fromhex("10 14 62 F1 90")   # first frame of the answer
    assert len(sent) == 2 and len(received) == 3                             # request, flow control; FF + 2 CF
    times = [f.timestamp for f in frames]
    assert times == sorted(times)


def test_shell_log_includes_functional_requests(board_channel, tmp_path, monkeypatch, capsys):
    log = tmp_path / "shell.asc"
    monkeypatch.setattr(sys, "stdin", io.StringIO("functional '3E 00'\n"))
    assert run_board(board_channel, "shell", "--keep-alive", "0", "--log", str(log)) == 0
    frames = [(f.arbitration_id, bytes(f.data[:3])) for f in can.LogReader(log)]
    assert frames == [(0x601, b"\x02\x3e\x00"), (0x68A, b"\x02\x7e\x00")]


def test_log_needs_a_known_format(board_channel, tmp_path, capsys):
    assert run_board(board_channel, "read-did", "VIN", "--log", str(tmp_path / "uds.txt")) == 1
    assert "--log needs a file ending in" in capsys.readouterr().err
    assert not (tmp_path / "uds.txt").exists()


def test_logged_bus_passes_frames_through():
    channel = f"{uuid4().hex}-pass"
    received = []

    class Collect(can.Listener):
        def on_message_received(self, msg):
            received.append(msg)

    with open_bus(BusConfig(channel=channel)) as peer, open_bus(BusConfig(channel=channel)) as bus:
        with LoggedBus(bus, Collect()) as logged:
            logged.send(can.Message(arbitration_id=0x123, data=b"\x01", is_extended_id=False))
            assert peer.recv(1.0).arbitration_id == 0x123
            peer.send(can.Message(arbitration_id=0x456, data=b"\x02", is_extended_id=False))
            assert logged.recv(1.0).arbitration_id == 0x456
            assert logged.protocol == bus.protocol
        bus.send(can.Message(arbitration_id=0x789, is_extended_id=False))  # the wrapped bus stays open
    assert [(m.arbitration_id, m.is_rx) for m in received] == [(0x123, False), (0x456, True)]
    assert received[0].channel == channel


def test_logged_bus_with_echoes_records_each_sent_frame_once_and_hides_the_echo():
    channel = f"{uuid4().hex}-echo"
    received = []

    class Collect(can.Listener):
        def on_message_received(self, msg):
            received.append(msg)

    with open_bus(BusConfig(channel=channel)) as peer, \
            open_bus(BusConfig(channel=channel), receive_own_messages=True) as bus:
        with LoggedBus(bus, Collect(), echoes=True) as logged:
            logged.send(can.Message(arbitration_id=0x123, data=b"\x01", is_extended_id=False))
            peer.send(can.Message(arbitration_id=0x456, data=b"\x02", is_extended_id=False))
            assert logged.recv(1.0).arbitration_id == 0x456  # the echo of 0x123 is not returned
            assert logged.recv(0.1) is None
    assert [(m.arbitration_id, m.is_rx) for m in received] == [(0x123, False), (0x456, True)]
    assert received[0].timestamp <= received[1].timestamp


def test_hex_display():
    defs = table("""
[dids.0xF1A1]
name = "Boot"
fields = [
    { name = "Magic", type = "uint32", display = "hex" },
    { name = "Decision", type = "uint32", display = "hex", choices = { 1 = "jumped" } },
]
""")
    assert dids.format_fields(defs[0xF1A1], bytes.fromhex("00ABCDEF 00000001")) == "Magic=0x00ABCDEF, Decision=jumped"
    assert dids.format_fields(defs[0xF1A1], bytes.fromhex("FFFFFFFF 00000007")) == "Magic=0xFFFFFFFF, Decision=0x00000007"
    assert dids.describe(defs[0xF1A1])[1] == "  Magic: uint32, hex"


@pytest.mark.parametrize("field, message", [
    ('{ name = "X", type = "uint8", display = "octal" }', 'display must be "decimal" or "hex"'),
    ('{ name = "X", type = "uint8", display = "hex", scale = 0.5 }', "for unscaled integer fields"),
    ('{ name = "X", type = "bytes", display = "hex" }', "for unscaled integer fields"),
])
def test_hex_display_errors(field, message):
    with pytest.raises(ValueError, match=message):
        table(f'[dids.0x0100]\nname = "A"\nfields = [{field}]')


def test_test_board_boot_info(board_channel, capsys):
    assert run_board(board_channel, "read-did", "BootInfo") == 0
    out = capsys.readouterr().out
    assert out.startswith("0xF1A1 BootInfo [48] 00 00")
    assert out.rstrip().endswith("RequestValid=no, ImageValid=no, Decision=none")
