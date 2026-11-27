"""Shared fixtures. ``board`` is the simulated test_board unless --board-profile names real hardware.

    pytest tests/test_board_checks.py --board-profile profiles/test_board.toml

On hardware only the checks without the ``sim_only`` marker run: reads, session
changes and suppressed functional requests. Nothing writes, unlocks or runs routines.
"""

import argparse
from dataclasses import dataclass
from uuid import uuid4

import can
import pytest

from ecu_comm import cli, uds_cli
from ecu_comm.ecu_profile import load_profile
from ecu_comm.isotp_link import IsoTpConfig
from ecu_comm.sim_ecu import run_simulated_ecu
from ecu_comm.sim_test_board import TEST_BOARD_FUNCTIONAL_ID, TEST_BOARD_LINK, SimulatedTestBoard
from ecu_comm.transport import BusConfig, open_bus
from ecu_comm.uds import open_uds_client, read_raw_dids, send_functional, send_raw


def pytest_addoption(parser):
    parser.addoption("--board-profile", metavar="PROFILE.toml",
                     help="run the test_board checks against the board this profile describes")


def pytest_configure(config):
    config.addinivalue_line("markers", "sim_only: only runs against the simulated test_board")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--board-profile"):
        skip = pytest.mark.skip(reason="simulation only: not sent to hardware")
        for item in items:
            if "sim_only" in item.keywords:
                item.add_marker(skip)


@dataclass
class Board:
    client: object
    bus: can.BusABC
    link: IsoTpConfig
    functional_id: int
    sim: SimulatedTestBoard | None

    def request(self, hex_request: str) -> bytes:
        return send_raw(self.client, bytes.fromhex(hex_request))

    def functional(self, hex_request: str, listen: float = 0.3) -> list[bytes]:
        return send_functional(self.client, self.bus, self.link, self.functional_id,
                               bytes.fromhex(hex_request), listen)

    def read(self, did: int) -> bytes:
        return read_raw_dids(self.client, [did])[did]


@pytest.fixture
def board(request):
    path = request.config.getoption("--board-profile")
    if path:
        profile = load_profile(path)
        link = uds_cli.isotp_config(argparse.Namespace(frame_fd=None), profile)
        functional_id = profile["isotp"].get("functional_id", TEST_BOARD_FUNCTIONAL_ID)
        with (
            open_bus(cli.bus_config(argparse.Namespace(), profile)) as bus,
            open_uds_client(bus, link, timeout=profile["uds"].get("timeout", 2.0)) as client,
        ):
            yield Board(client, bus, link, functional_id, None)
        return
    bus_config = BusConfig(channel=uuid4().hex)
    with (
        open_bus(bus_config) as ecu_bus,
        open_bus(bus_config) as tester_bus,
        run_simulated_ecu(ecu_bus, TEST_BOARD_LINK, SimulatedTestBoard(), TEST_BOARD_FUNCTIONAL_ID) as sim,
        open_uds_client(tester_bus, TEST_BOARD_LINK, timeout=1.0) as client,
    ):
        yield Board(client, tester_bus, TEST_BOARD_LINK, TEST_BOARD_FUNCTIONAL_ID, sim)
