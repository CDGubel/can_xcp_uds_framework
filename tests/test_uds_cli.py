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
