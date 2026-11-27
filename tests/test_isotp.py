from uuid import uuid4

import pytest

from ecu_comm.isotp_link import IsoTpConfig, open_isotp
from ecu_comm.transport import BusConfig, open_bus


@pytest.mark.parametrize(
    "fd,extended,length",
    [(False, False, 5), (False, False, 300), (True, False, 300), (True, True, 4095)],
)
def test_isotp_round_trip(fd, extended, length):
    bus_config = BusConfig(channel=uuid4().hex, fd=fd)
    link = IsoTpConfig(0x18DA10F1 if extended else 0x7E0, 0x18DAF110 if extended else 0x7E8,
                       extended=extended, fd=fd, brs=fd)
    payload = bytes(i % 256 for i in range(length))

    with (
        open_bus(bus_config) as tester_bus,
        open_bus(bus_config) as ecu_bus,
        open_isotp(tester_bus, link) as tester,
        open_isotp(ecu_bus, link.reversed()) as ecu,
    ):
        tester.send(payload)
        assert ecu.recv(block=True, timeout=5) == payload


@pytest.mark.parametrize("padding,dlc,data", [(0xCC, 8, "02 3E 00 CC CC CC CC CC"), (None, 3, "02 3E 00")])
def test_single_frame_padding(padding, dlc, data):
    bus_config = BusConfig(channel=uuid4().hex)
    with open_bus(bus_config) as sniffer, open_bus(bus_config) as bus, \
            open_isotp(bus, IsoTpConfig(0x7E0, 0x7E8, padding=padding)) as stack:
        stack.send(b"\x3E\x00")
        frame = sniffer.recv(timeout=1)

    assert frame is not None and frame.arbitration_id == 0x7E0
    assert frame.dlc == dlc and frame.data == bytes.fromhex(data)


@pytest.mark.parametrize(
    "config,match",
    [
        (IsoTpConfig(0x800, 0x7E8), "11-bit range"),
        (IsoTpConfig(0x7E0, 0x7E0), "must differ"),
        (IsoTpConfig(0x7E0, 0x7E8, brs=True), "requires FD frames"),
        (IsoTpConfig(0x7E0, 0x7E8, padding=0x100), "byte value"),
        (IsoTpConfig(0x7E0, 0x7E8, fd=True), "CAN-FD mode"),
    ],
)
def test_invalid_isotp_config(config, match):
    with open_bus(BusConfig(channel=uuid4().hex)) as bus:
        with pytest.raises(ValueError, match=match):
            with open_isotp(bus, config):
                pass
