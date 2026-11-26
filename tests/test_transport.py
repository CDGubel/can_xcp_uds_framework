from unittest.mock import patch
from uuid import uuid4

import can
import pytest

from ecu_comm.transport import BusConfig, make_frame, open_bus, send_frame


@pytest.mark.parametrize(
    "frame_fd,extended,brs,length",
    [(False, False, False, 8), (True, True, True, 64), (True, False, False, 12)],
)
def test_virtual_round_trip(frame_fd, extended, brs, length):
    config = BusConfig(channel=uuid4().hex, fd=True)
    arbitration_id = 0x18DAF110 if extended else 0x123
    frame = make_frame(arbitration_id, bytes(range(length)), fd=frame_fd, extended=extended, brs=brs)

    with open_bus(config) as receiver, open_bus(config) as sender:
        send_frame(sender, frame)
        received = receiver.recv(timeout=0.2)

    assert received is not None
    assert received.equals(frame, timestamp_delta=None, check_channel=False, check_direction=False)


def test_virtual_timeout_and_classical_mode_rejects_fd():
    with open_bus(BusConfig(channel=uuid4().hex)) as bus:
        assert bus.recv(timeout=0.01) is None
        with pytest.raises(ValueError, match="bus opened in CAN-FD mode"):
            send_frame(bus, make_frame(0x123, bytes(12), fd=True))


@pytest.mark.parametrize(
    "can_id,length,flags",
    [
        (0x800, 0, {}),
        (0x20000000, 0, {"extended": True}),
        (0x123, 9, {}),
        (0x123, 9, {"fd": True}),
        (0x123, 0, {"brs": True}),
    ],
)
def test_invalid_frame_is_rejected(can_id, length, flags):
    with pytest.raises(ValueError):
        make_frame(can_id, bytes(length), **flags)


def test_pcan_fd_uses_requested_timing():
    with patch("ecu_comm.transport.can.Bus") as bus:
        open_bus(BusConfig(interface="pcan", fd=True, bitrate=250_000, data_bitrate=1_000_000))

    options = bus.call_args.kwargs
    assert options["interface"] == "pcan"
    assert options["channel"] == "PCAN_USBBUS1"
    assert options["fd"] and options["ignore_config"]
    timing = options["timing"]
    assert isinstance(timing, can.BitTimingFd)
    assert (timing.nom_bitrate, timing.data_bitrate) == (250_000, 1_000_000)
    assert (timing.nom_sample_point, timing.data_sample_point) == pytest.approx((80, 75))


def test_pcan_classical_forwards_bitrate_and_channel():
    with patch("ecu_comm.transport.can.Bus") as bus:
        open_bus(BusConfig(interface="pcan", channel="pcan_usbbus2", bitrate=250_000))
    bus.assert_called_once_with(
        interface="pcan", channel="PCAN_USBBUS2", bitrate=250_000, ignore_config=True
    )


@pytest.mark.parametrize(
    "config,match",
    [
        (BusConfig(data_bitrate=2_000_000), "requires CAN-FD"),
        (BusConfig(interface="pcan", bitrate=123_456), "unsupported classical"),
        (BusConfig(interface="pcan", channel="missing"), "unknown PCAN channel"),
    ],
)
def test_invalid_bus_config_fails_before_open(config, match):
    with patch("ecu_comm.transport.can.Bus") as bus:
        with pytest.raises(ValueError, match=match):
            open_bus(config)
    bus.assert_not_called()
