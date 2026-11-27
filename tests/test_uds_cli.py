import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from ecu_comm.cli import main
from ecu_comm.ecu_profile import load_profile
from ecu_comm.isotp_link import IsoTpConfig
from ecu_comm.sim_ecu import run_simulated_ecu
from ecu_comm.transport import BusConfig, open_bus

EXAMPLE_PROFILE = Path(__file__).parents[1] / "profiles" / "example_ecu.toml"


@pytest.fixture
def ecu_channel():
    """A simulated ECU on a fresh virtual channel, requests on 0x7E0."""
    channel = uuid4().hex
    with open_bus(BusConfig(channel=channel)) as bus, run_simulated_ecu(bus, IsoTpConfig(0x7E0, 0x7E8)):
        yield channel


def run_uds(channel, command, *args):
    return main(["uds", command, "--channel", channel, "--tx-id", "0x7E0", "--rx-id", "0x7E8", *args])


def test_uds_demo_module():
    result = subprocess.run([sys.executable, "-m", "ecu_comm", "uds", "demo"],
                            capture_output=True, text=True, timeout=20, check=True)
    assert '0xF190 [17] 57 53 49 4D 55 4C 41 54 45 44 30 30 30 30 30 30 31 "WSIMULATED0000001"' in result.stdout
    assert result.stdout.endswith("UDS demo OK.\n")


def test_uds_commands(ecu_channel, capsys):
    assert run_uds(ecu_channel, "session", "extended") == 0
    assert run_uds(ecu_channel, "tester-present") == 0
    assert run_uds(ecu_channel, "read-did", "0xF195", "61836") == 0
    assert run_uds(ecu_channel, "reset", "soft") == 0
    assert run_uds(ecu_channel, "raw", "22 F1 87") == 0
    assert capsys.readouterr().out.splitlines() == [
        "Session 0x03 (extended) active. P2 = 50 ms, P2* = 5000 ms.",
        "Tester present OK",
        "0xF195 [3] 01 02 03",
        '0xF18C [14] 53 4E 30 30 30 30 30 30 30 30 30 30 34 32 "SN000000000042"',
        "ECU reset 0x03 accepted",
        "Response [16] 62 F1 87 53 49 4D 2D 50 41 52 54 2D 30 30 30 31 (positive response)",
    ]


def test_uds_negative_responses_exit_nonzero(ecu_channel, capsys):
    assert run_uds(ecu_channel, "read-did", "0x1234") == 1
    assert run_uds(ecu_channel, "raw", "27 01") == 1
    captured = capsys.readouterr()
    assert captured.err == "ecu-can: negative response 0x31 (RequestOutOfRange)\n"
    assert captured.out == "Response [3] 7F 27 11 (negative response 0x11 (ServiceNotSupported))\n"


def test_uds_timeout_and_missing_ids(capsys):
    channel = uuid4().hex
    assert run_uds(channel, "tester-present", "--timeout", "0.2") == 1
    assert main(["uds", "tester-present", "--channel", channel]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "ecu-can: no response from the ECU before the timeout",
        "ecu-can: UDS needs --tx-id and --rx-id, or an --ecu profile that sets them",
    ]


def test_profile_supplies_settings_and_cli_overrides(ecu_channel, tmp_path, capsys):
    profile = tmp_path / "ecu.toml"
    profile.write_text(
        f'[bus]\ninterface = "virtual"\nchannel = "{ecu_channel}"\n'
        "[isotp]\ntx_id = 0x7E0\nrx_id = 0x7E8\npadding = false\n"
        "[uds]\ntimeout = 1.0\n"
    )
    assert main(["uds", "read-did", "--ecu", str(profile), "0xF195"]) == 0
    # The command line wins over the profile: this request goes to an ID nobody answers.
    assert main(["uds", "read-did", "--ecu", str(profile), "--tx-id", "0x7E1", "--timeout", "0.2", "0xF195"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "0xF195 [3] 01 02 03\n"
    assert "no response" in captured.err


def test_example_profile_loads():
    profile = load_profile(EXAMPLE_PROFILE)
    assert profile["bus"]["interface"] == "pcan"
    assert (profile["isotp"]["tx_id"], profile["isotp"]["rx_id"], profile["isotp"]["padding"]) == (0x7E0, 0x7E8, 0xCC)


@pytest.mark.parametrize(
    "text,match",
    [
        ("[isotp]\ntx_id = 0x7E0\ntxid = 1\n", "unknown key isotp.txid"),
        ("[isotp]\ntx_id = true\n", "isotp.tx_id has the wrong type"),
        ("[isotp]\npadding = true\n", "byte value or false"),
        ("[can]\nbitrate = 1\n", "unknown section"),
        ("[bus\n", "Expected"),
    ],
)
def test_invalid_profile(tmp_path, text, match):
    path = tmp_path / "bad.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=match):
        load_profile(path)


def test_invalid_profile_reported_by_cli(tmp_path, capsys):
    path = tmp_path / "bad.toml"
    path.write_text("[bus]\nbitrate = \"fast\"\n")
    assert main(["listen", "--ecu", str(path)]) == 1
    assert "bus.bitrate has the wrong type" in capsys.readouterr().err


# -- test_board: shell, functional addressing, security ---------------------------------

TEST_BOARD_PROFILE = Path(__file__).parents[1] / "profiles" / "test_board.toml"
READONLY_SCRIPT = Path(__file__).parents[1] / "scripts" / "test_board_readonly.uds"


@pytest.fixture
def board_channel(monkeypatch):
    """The simulated test_board on a fresh virtual channel; no secret in the environment."""
    from ecu_comm.sim_test_board import TEST_BOARD_FUNCTIONAL_ID, TEST_BOARD_LINK, SimulatedTestBoard

    monkeypatch.delenv("ECU_COMM_SECURITY_KEY", raising=False)
    channel = uuid4().hex
    with open_bus(BusConfig(channel=channel)) as bus, \
            run_simulated_ecu(bus, TEST_BOARD_LINK, SimulatedTestBoard(), TEST_BOARD_FUNCTIONAL_ID) as ecu:
        yield channel, ecu


def run_board(channel, command, *args):
    return main(["uds", command, "--ecu", str(TEST_BOARD_PROFILE), "--interface", "virtual",
                 "--channel", channel, "--timeout", "0.5", *args])


def test_test_board_profile_loads():
    profile = load_profile(TEST_BOARD_PROFILE)
    assert profile["bus"]["channel"] == "PCAN_USBBUS2"
    assert profile["isotp"] == {"tx_id": 0x60A, "rx_id": 0x68A, "functional_id": 0x601, "padding": 0xAA,
                                "stmin": 0, "blocksize": 0}
    assert profile["security"] == {"algorithm": "aes128-ecb"}


def test_readonly_bench_script_passes_on_the_simulator(board_channel, capsys):
    channel, ecu = board_channel
    assert run_board(channel, "shell", str(READONLY_SCRIPT)) == 0
    out = capsys.readouterr().out
    assert "> sleep 6" in out and out.count("No response") == 2
    assert ecu.session == 0x01


def test_shell_unlock_routine_and_write(board_channel, tmp_path, capsys):
    from ecu_comm.sim_test_board import SIM_SECRET

    channel, ecu = board_channel
    key_file = tmp_path / "sim.key"
    key_file.write_text(SIM_SECRET.hex(" "))
    script = tmp_path / "calibrate.uds"
    script.write_text(
        "session extended\n"
        "unlock 1\n"
        "unlock 1   # a second unlock sees the zero seed\n"
        'routine start 0xFD00 "00 01 02 06"\n'
        "routine result 0xFD00\n"
        'write-did 0xF184 "41 42 43 44 45 46 47 48 49 4A 4B 4C"\n'
        'raw "31 01 FD 00 00 01 06 06" --expect "7F 31 31"\n'
    )
    assert run_board(channel, "shell", "--key-file", str(key_file), str(script)) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if not line.startswith(">")]
    assert lines == [
        "Session 0x03 (extended) active. P2 = 100 ms, P2* = 2000 ms.",
        "Security level 0x01 unlocked",
        "Security level 0x01 already unlocked (zero seed)",
        "Routine 0xFD00 start: [3] 03 36 02",
        "Routine 0xFD00 result: [3] 03 36 02",
        "0xF184 written [12] 41 42 43 44 45 46 47 48 49 4A 4B 4C",
        "Response [3] 7F 31 31 (negative response 0x31 (RequestOutOfRange))",
    ]
    assert ecu.dids[0xF184] == b"ABCDEFGHIJKL"


def test_shell_stops_at_the_first_failure(board_channel, tmp_path, capsys):
    channel, _ = board_channel
    script = tmp_path / "fails.uds"
    script.write_text('tester-present\nroutine start 0xFD00 "00 01 02 06"\ntester-present\n')
    assert run_board(channel, "shell", "--keep-alive", "0", str(script)) == 1
    captured = capsys.readouterr()
    assert captured.out.count("Tester present OK") == 1
    assert captured.err.splitlines() == ["ecu-can: negative response 0x7F (ServiceNotSupportedInActiveSession)",
                                         "ecu-can: stopped at line 2"]


@pytest.mark.parametrize("line,message", [("read-did", "the following arguments are required"),
                                         ("bogus", "invalid choice"), ('raw "22 F1', "No closing quotation")])
def test_shell_reports_bad_lines(board_channel, tmp_path, capsys, line, message):
    channel, _ = board_channel
    script = tmp_path / "bad.uds"
    script.write_text(f"\n# comment\n{line}\n")
    assert run_board(channel, "shell", str(script)) == 1
    err = capsys.readouterr().err
    assert "line 3" in err and message in err


def test_unlock_needs_a_secret(board_channel, capsys):
    channel, _ = board_channel
    assert run_board(channel, "session", "extended") == 0
    assert run_board(channel, "unlock", "1") == 1
    assert "ECU_COMM_SECURITY_KEY" in capsys.readouterr().err


def test_unlock_with_plugin_algorithm_and_environment_secret(board_channel, monkeypatch, tmp_path, capsys):
    from ecu_comm.sim_test_board import SIM_SECRET

    channel, ecu = board_channel
    monkeypatch.setenv("ECU_COMM_SECURITY_KEY", SIM_SECRET.hex())
    script = tmp_path / "unlock.uds"
    script.write_text("session extended\nunlock 1 --key-algorithm ecu_comm.security:aes128_ecb\n")
    assert run_board(channel, "shell", "--keep-alive", "0", str(script)) == 0
    assert "Security level 0x01 unlocked" in capsys.readouterr().out
    assert ecu.unlocked == 0x01


def test_functional_command(board_channel, capsys):
    channel, ecu = board_channel
    assert run_board(channel, "functional", "10 03") == 0
    assert run_board(channel, "functional", "3E 80", "--expect", "none") == 0
    assert run_board(channel, "functional", "3E 00", "--expect", "none") == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "Response [6] 50 03 00 64 00 C8 (positive response)",
        "No response",
        "Response [2] 7E 00 (positive response)",
    ]
    assert captured.err == "ecu-can: expected no response, got 7E 00\n"
    assert ecu.session == 0x03


def test_functional_needs_an_id(ecu_channel, capsys):
    assert run_uds(ecu_channel, "functional", "3E 80") == 1
    assert "--functional-id" in capsys.readouterr().err


def test_profile_key_file_is_relative_to_the_profile(tmp_path):
    profile = tmp_path / "board.toml"
    profile.write_text('[security]\nkey_file = "keys/board.key"\n')
    assert load_profile(profile)["security"]["key_file"] == str(tmp_path / "keys" / "board.key")
