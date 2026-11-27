from pathlib import Path
from uuid import uuid4

import pytest

from ecu_comm.cli import main
from ecu_comm.dbc import decode, encode, format_decoded, load_dbc
from ecu_comm.ecu_profile import load_profile
from ecu_comm.transport import BusConfig, make_frame, open_bus, send_frame

SAMPLE_DBC = Path(__file__).parents[1] / "profiles" / "test_board.dbc"


@pytest.fixture(scope="module")
def network():
    return load_dbc([SAMPLE_DBC])


def frame_line(network, arbitration_id, data, **flags):
    decoded = decode(network, make_frame(arbitration_id, bytes.fromhex(data), **flags))
    return None if decoded is None else format_decoded(decoded)


# -- decoding --------------------------------------------------------------------------


def test_sample_dbc_loads(network):
    assert [m.name for m in network.sorted_messages()] == ["BoardStatus", "PhaseCurrents", "TesterCommand", "BoardInfo"]


def test_decode_scaling_units_and_choices(network):
    assert frame_line(network, 0x100, "52 6C 09 FB 00 00 00 00") == (
        "BoardStatus State=Running, Counter=5, SupplyVoltage=24.12 V, BoardTemperature=-5 degC, FaultActive=0")


def test_decode_big_endian_signed_rounds_to_the_scale(network):
    # Raw -63 at 0.1 A would print as -6.300000000000001 without rounding.
    assert frame_line(network, 0x200, "00 0D 00 24 FF C1") == "PhaseCurrents CurrentU=1.3 A, CurrentV=3.6 A, CurrentW=-6.3 A"


def test_padding_after_the_message_is_ignored(network):
    assert frame_line(network, 0x200, "00 0D 00 24 FF C1 AA AA") == (
        "PhaseCurrents CurrentU=1.3 A, CurrentV=3.6 A, CurrentW=-6.3 A")


def test_short_frame_is_reported_not_raised(network):
    assert frame_line(network, 0x100, "01 02") == "BoardStatus (cannot decode: Wrong data size: 2 instead of 8 bytes)"


def test_multiplexed_extended_message(network):
    assert frame_line(network, 0x18FF0010, "00 78 56 34 12 00 00 00", extended=True) == (
        "BoardInfo Page=0, SerialNumber=305419896")
    assert frame_line(network, 0x18FF0010, "01 02 07 00 00 00 00 00", extended=True) == (
        "BoardInfo Page=1, SwMajor=2, SwMinor=7")
    assert "cannot decode" in frame_line(network, 0x18FF0010, "09 00 00 00 00 00 00 00", extended=True)


def test_id_lookup_respects_11_and_29_bit(network):
    assert frame_line(network, 0x100, "00 00 00 00 00 00 00 00", extended=True) is None
    assert frame_line(network, 0x7FF, "00") is None


def test_fd_message(network):
    assert frame_line(network, 0x300, "01 37 FF 00 00 00 00 00 7B 00 00 00", fd=True) == (
        "TesterCommand Mode=Speed, SpeedRequest=-100.5 rpm, TorqueLimit=12.3 Nm")


def test_encode_round_trip(network):
    message = network.find("TesterCommand")
    assert encode(message, {"Mode": "Speed", "SpeedRequest": -100.5, "TorqueLimit": 12.3}).hex(" ").upper() == (
        "01 37 FF 00 00 00 00 00 7B 00 00 00")
    with pytest.raises(ValueError, match="TesterCommand"):
        encode(message, {"Mode": "Speed"})


# -- loading errors and profiles -------------------------------------------------------


def test_duplicate_id_across_files_is_an_error(tmp_path):
    copy = tmp_path / "copy.dbc"
    copy.write_text(SAMPLE_DBC.read_text())
    with pytest.raises(ValueError, match=r"copy.dbc: 0x100 \(BoardStatus\) is already defined as BoardStatus in .*test_board.dbc"):
        load_dbc([SAMPLE_DBC, copy])


def test_missing_and_invalid_files(tmp_path):
    with pytest.raises(ValueError, match="DBC file not found"):
        load_dbc([tmp_path / "missing.dbc"])
    bad = tmp_path / "bad.dbc"
    bad.write_text('BO_ 1 X: 8 A\n SG_ a : 0|80@1+ (1,0) [0|1] "" B\n')
    with pytest.raises(ValueError, match="bad.dbc: .*does not fit"):
        load_dbc([bad])


def test_profile_dbc_files_are_relative_to_the_profile(tmp_path):
    profile = tmp_path / "board.toml"
    profile.write_text('[dbc]\nfiles = ["nets/a.dbc", "C:/abs/b.dbc"]\n')
    assert load_profile(profile)["dbc"]["files"] == [str(tmp_path / "nets" / "a.dbc"), str(Path("C:/abs/b.dbc"))]
    profile.write_text("[dbc]\nfiles = [1]\n")
    with pytest.raises(ValueError, match="list of file names"):
        load_profile(profile)
    profile.write_text('[dbc]\nfiles = "a.dbc"\n')
    with pytest.raises(ValueError, match="dbc.files has the wrong type"):
        load_profile(profile)


def test_offset_is_shown_and_used(tmp_path):
    dbc = tmp_path / "offset.dbc"
    dbc.write_text('BO_ 16 Temps: 2 A\n SG_ Coolant : 0|8@1+ (0.5,-40) [-40|87.5] "degC" B\n'
                   ' SG_ Oil : 8|8@1+ (1,-40) [-40|215] "degC" B\n')
    network = load_dbc([dbc])
    from ecu_comm.dbc import describe_message

    assert describe_message(network.find("Temps"))[1:] == [
        "  Coolant: bit 0, 8 bits, Intel, unsigned, x0.5 -40, [-40 .. 87.5] degC",
        "  Oil: bit 8, 8 bits, Intel, unsigned, -40, [-40 .. 215] degC",
    ]
    assert frame_line(network, 0x010, "64 64") == "Temps Coolant=10.0 degC, Oil=60 degC"


# -- ecu-can dbc show ------------------------------------------------------------------


def test_dbc_show_one_message(capsys):
    assert main(["dbc", "show", "--dbc", str(SAMPLE_DBC), "BoardStatus"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "0x100 STD CAN [8] BoardStatus (cycle 100 ms; from TestBoard)",
        "  State: bit 0, 4 bits, Intel, unsigned, [0 .. 3], 0=Init 1=Ready 2=Running 3=Fault",
        "  Counter: bit 4, 4 bits, Intel, unsigned, [0 .. 15]",
        "  SupplyVoltage: bit 8, 16 bits, Intel, unsigned, x0.01, [0 .. 655.35] V",
        "  BoardTemperature: bit 24, 8 bits, Intel, signed, [-128 .. 127] degC",
        "  FaultActive: bit 32, 1 bit, Intel, unsigned, [0 .. 1]",
    ]


def test_dbc_show_by_id_from_a_profile(tmp_path, capsys):
    profile = tmp_path / "board.toml"
    profile.write_text(f'[dbc]\nfiles = ["{SAMPLE_DBC.as_posix()}"]\n')
    assert main(["dbc", "show", "--ecu", str(profile), "0x18FF0010", "0x300"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("0x18FF0010 EXT CAN [8] BoardInfo (cycle 1000 ms; from TestBoard)\n")
    assert "  Page: bit 0, 8 bits, Intel, unsigned, [0 .. 255], multiplexer\n" in out
    assert "  SwMajor: bit 8, 8 bits, Intel, unsigned, [0 .. 255], when Page = 1\n" in out
    assert "\n\n0x300 STD FD [12] TesterCommand (from Tester)\n" in out
    assert "  CurrentU" not in out


def test_dbc_show_all_and_errors(capsys):
    assert main(["dbc", "show", "--dbc", str(SAMPLE_DBC)]) == 0
    assert capsys.readouterr().out.count("\n\n") == 3
    assert main(["dbc", "show", "--dbc", str(SAMPLE_DBC), "NoSuchMessage"]) == 1
    assert main(["dbc", "show", "--dbc", str(SAMPLE_DBC), "0x7FF"]) == 1
    assert main(["dbc", "show"]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "ecu-can: no message named NoSuchMessage in the DBC files",
        "ecu-can: no message with ID 0x7FF in the DBC files",
        "ecu-can: no DBC files: give --dbc FILE, or an --ecu profile with dbc.files",
    ]


# -- ecu-can listen --dbc --------------------------------------------------------------

TRAFFIC = [
    make_frame(0x100, bytes.fromhex("52 6C 09 FB 00 00 00 00")),
    make_frame(0x7FF, bytes.fromhex("01 02")),
    make_frame(0x200, bytes.fromhex("00 0D 00 24 FF C1")),
    make_frame(0x18FF0010, bytes.fromhex("01 02 07 00 00 00 00 00"), extended=True),
]


def listen(monkeypatch, capsys, *options):
    import ecu_comm.cli as cli

    config = BusConfig(channel=uuid4().hex)
    with open_bus(config) as sender, open_bus(config) as receiver:
        for frame in TRAFFIC:
            send_frame(sender, frame)
        monkeypatch.setattr(cli, "open_bus", lambda config: receiver)
        status = main(["listen", "--channel", config.channel, "--duration", "0.3", *options])
    captured = capsys.readouterr()
    # Drop the timestamps.
    return status, [line.split(" ", 1)[1] for line in captured.out.splitlines()], captured.err


def test_listen_decodes_known_frames(monkeypatch, capsys):
    status, lines, _ = listen(monkeypatch, capsys, "--dbc", str(SAMPLE_DBC))
    assert status == 0
    assert lines == [
        "0x100 STD CAN [8] 52 6C 09 FB 00 00 00 00  BoardStatus State=Running, Counter=5, SupplyVoltage=24.12 V, "
        "BoardTemperature=-5 degC, FaultActive=0",
        "0x7FF STD CAN [2] 01 02",
        "0x200 STD CAN [6] 00 0D 00 24 FF C1  PhaseCurrents CurrentU=1.3 A, CurrentV=3.6 A, CurrentW=-6.3 A",
        "0x18FF0010 EXT CAN [8] 01 02 07 00 00 00 00 00  BoardInfo Page=1, SwMajor=2, SwMinor=7",
    ]


def test_listen_message_and_signal_filters(monkeypatch, capsys):
    _, lines, _ = listen(monkeypatch, capsys, "--dbc", str(SAMPLE_DBC), "--message", "PhaseCurrents",
                         "--message", "BoardInfo")
    assert [line.split("  ", 1)[1] for line in lines] == [
        "PhaseCurrents CurrentU=1.3 A, CurrentV=3.6 A, CurrentW=-6.3 A",
        "BoardInfo Page=1, SwMajor=2, SwMinor=7",
    ]
    _, lines, _ = listen(monkeypatch, capsys, "--dbc", str(SAMPLE_DBC), "--signal", "SupplyVoltage",
                         "--signal", "CurrentW")
    assert [line.split("  ", 1)[1] for line in lines] == ["BoardStatus SupplyVoltage=24.12 V",
                                                         "PhaseCurrents CurrentW=-6.3 A"]


def test_listen_count_counts_shown_frames(monkeypatch, capsys):
    _, lines, _ = listen(monkeypatch, capsys, "--dbc", str(SAMPLE_DBC), "--message", "BoardInfo", "--count", "1")
    assert len(lines) == 1 and "BoardInfo" in lines[0]


def test_listen_without_dbc_is_unchanged(monkeypatch, capsys):
    _, lines, _ = listen(monkeypatch, capsys)
    assert lines[0] == "0x100 STD CAN [8] 52 6C 09 FB 00 00 00 00" and len(lines) == 4


@pytest.mark.parametrize("options,message", [
    (["--message", "BoardStatus"], "--message and --signal need DBC files"),
    (["--dbc", str(SAMPLE_DBC), "--message", "Nope"], "no message named Nope"),
    (["--dbc", str(SAMPLE_DBC), "--signal", "Nope", "--signal", "Zip"], "no signal named Nope, Zip"),
])
def test_listen_filter_errors(capsys, options, message):
    assert main(["listen", "--channel", uuid4().hex, "--duration", "0.1", *options]) == 1
    assert message in capsys.readouterr().err
