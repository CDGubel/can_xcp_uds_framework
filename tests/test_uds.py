from contextlib import contextmanager
from uuid import uuid4

import pytest
from udsoncan.exceptions import NegativeResponseException, TimeoutException

from ecu_comm.isotp_link import IsoTpConfig
from ecu_comm.sim_ecu import SimulatedEcu, run_simulated_ecu
from ecu_comm.transport import BusConfig, open_bus
from ecu_comm.uds import describe_response, open_uds_client, read_raw_dids, send_raw

LINK = IsoTpConfig(0x7E0, 0x7E8)


@contextmanager
def ecu_session(link=LINK, fd=False, timeout=1.0):
    bus_config = BusConfig(channel=uuid4().hex, fd=fd)
    with (
        open_bus(bus_config) as ecu_bus,
        open_bus(bus_config) as tester_bus,
        run_simulated_ecu(ecu_bus, link) as ecu,
        open_uds_client(tester_bus, link, timeout=timeout) as client,
    ):
        yield client, ecu


def test_session_reset_and_tester_present():
    with ecu_session() as (client, ecu):
        data = client.change_session(3).service_data
        assert ecu.session == 3
        assert (data.p2_server_max, data.p2_star_server_max) == pytest.approx((0.05, 5.0))
        client.tester_present()
        client.ecu_reset(1)
        assert ecu.session == 1


@pytest.mark.parametrize("fd,extended", [(False, False), (True, True)])
def test_read_dids_multi_frame(fd, extended):
    link = IsoTpConfig(0x18DA10F1, 0x18DAF110, extended=True, fd=True, brs=True) if extended else LINK
    big = bytes(range(256)) * 2
    with ecu_session(link, fd=fd) as (client, ecu):
        ecu.dids[0x0100] = big
        values = read_raw_dids(client, [0xF190, 0x0100])
    assert values == {0xF190: b"WSIMULATED0000001", 0x0100: big}


def test_unknown_did_raises_negative_response():
    with ecu_session() as (client, _), pytest.raises(NegativeResponseException) as info:
        client.read_data_by_identifier(0x1234)
    assert info.value.response.code == 0x31


@pytest.mark.parametrize(
    "request_hex,response_hex,description",
    [
        ("22 F1 95", "62 F1 95 01 02 03", "positive response"),
        ("27 01", "7F 27 11", "negative response 0x11 (ServiceNotSupported)"),
        ("10", "7F 10 13", "negative response 0x13 (IncorrectMessageLengthOrInvalidFormat)"),
    ],
)
def test_send_raw(request_hex, response_hex, description):
    with ecu_session() as (client, _):
        response = send_raw(client, bytes.fromhex(request_hex))
    assert response == bytes.fromhex(response_hex)
    assert describe_response(response) == description


def test_send_raw_suppressed_response_times_out():
    with ecu_session(timeout=0.2) as (client, _), pytest.raises(TimeoutException):
        send_raw(client, bytes.fromhex("3E 80"))


class PendingEcu(SimulatedEcu):
    """Answers every request with NRC 0x78 (responsePending) before the real response."""

    def responses(self, request):
        return [bytes((0x7F, request[0], 0x78)), *super().responses(request)]


def test_send_raw_waits_through_response_pending():
    bus_config = BusConfig(channel=uuid4().hex)
    with (
        open_bus(bus_config) as ecu_bus,
        open_bus(bus_config) as tester_bus,
        run_simulated_ecu(ecu_bus, LINK, PendingEcu()),
        open_uds_client(tester_bus, LINK, timeout=1.0) as client,
    ):
        assert send_raw(client, bytes.fromhex("22 F1 95")) == bytes.fromhex("62 F1 95 01 02 03")
        assert client.read_data_by_identifier(0xF195).service_data.values[0xF195] == b"\x01\x02\x03"


@pytest.mark.parametrize(
    "request_hex,response_hex",
    [("10 83", None), ("11 02", "51 02"), ("10 04", "7F 10 12"), ("3E 00 00", "7F 3E 13"), ("22 F1", "7F 22 13")],
)
def test_simulated_ecu_handle(request_hex, response_hex):
    response = SimulatedEcu().handle(bytes.fromhex(request_hex))
    assert response == (None if response_hex is None else bytes.fromhex(response_hex))
