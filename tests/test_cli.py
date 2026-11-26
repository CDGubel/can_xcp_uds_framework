import subprocess
import sys
from uuid import uuid4

import pytest

from ecu_comm.cli import main
from ecu_comm.transport import BusConfig, make_frame, open_bus, send_frame


@pytest.mark.parametrize("options,marker", [([], "STD CAN [8]"), (["--fd"], "STD FD BRS [12]")])
def test_module_demo(options, marker):
    result = subprocess.run(
        [sys.executable, "-m", "ecu_comm", "demo", *options],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert "Virtual round trip OK." in result.stdout
    assert marker in result.stdout


def test_cli_send_and_receive_on_virtual_channel(capsys):
    channel = uuid4().hex
    with open_bus(BusConfig(channel=channel, fd=True)) as receiver:
        assert main([
            "send", "--channel", channel, "--fd", "--frame-fd", "--brs",
            "--extended", "0x18DAF110", "00 01 02 03 04 05 06 07 08 09 0A 0B",
        ]) == 0
        frame = receiver.recv(timeout=0.2)

    assert frame is not None and frame.is_fd and frame.bitrate_switch and frame.is_extended_id
    assert frame.arbitration_id == 0x18DAF110
    assert frame.data == bytes(range(12))
    assert "Sent 0x18DAF110 EXT FD BRS [12]" in capsys.readouterr().out


def test_cli_listen_prints_one_frame(capsys, monkeypatch):
    import ecu_comm.cli as cli

    config = BusConfig(channel=uuid4().hex)
    with open_bus(config) as sender, open_bus(config) as receiver:
        send_frame(sender, make_frame(0x123, bytes.fromhex("00 FE")))
        monkeypatch.setattr(cli, "open_bus", lambda config: receiver)
        assert main(["listen", "--channel", config.channel, "--count", "1"]) == 0

    assert capsys.readouterr().out.endswith("0x123 STD CAN [2] 00 FE\n")


def test_cli_rejects_fd_frame_in_classical_mode(capsys):
    assert main(["send", "--frame-fd", "0x123", "00"]) == 1
    assert "--frame-fd requires --fd" in capsys.readouterr().err
