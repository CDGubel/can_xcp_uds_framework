"""Open a python-can bus and build validated raw data frames."""

from dataclasses import dataclass
from typing import Literal

import can

FD_LENGTHS = frozenset((*range(9), 12, 16, 20, 24, 32, 48, 64))
DEFAULT_CHANNELS = {"virtual": "ecu-can", "pcan": "PCAN_USBBUS1"}


@dataclass(frozen=True)
class BusConfig:
    interface: Literal["virtual", "pcan"] = "virtual"
    channel: str | None = None
    bitrate: int = 500_000
    fd: bool = False
    data_bitrate: int | None = None
    nominal_sample_point: float = 80.0
    data_sample_point: float = 75.0


def open_bus(config: BusConfig) -> can.BusABC:
    """Return a bus that the caller owns. Use it as a context manager."""
    if config.interface not in DEFAULT_CHANNELS:
        raise ValueError(f"unsupported interface: {config.interface}")
    if config.bitrate <= 0:
        raise ValueError("bitrate must be positive")
    if config.data_bitrate is not None and (not config.fd or config.data_bitrate <= 0):
        raise ValueError("a data bitrate must be positive and requires CAN-FD mode")
    channel = config.channel if config.channel is not None else DEFAULT_CHANNELS[config.interface]
    if not channel.strip():
        raise ValueError("channel must be nonempty")

    if config.interface == "virtual":
        protocol = can.CanProtocol.CAN_FD if config.fd else can.CanProtocol.CAN_20
        return can.Bus(interface="virtual", channel=channel, protocol=protocol, ignore_config=True)

    from can.interfaces.pcan.basic import PCAN_BITRATES, PCAN_CHANNEL_NAMES

    channel = channel.upper()
    if channel not in PCAN_CHANNEL_NAMES:
        raise ValueError(f"unknown PCAN channel: {channel}")
    if not config.fd:
        if config.bitrate not in PCAN_BITRATES:
            raise ValueError(f"unsupported classical PCAN bitrate: {config.bitrate}")
        return can.Bus(interface="pcan", channel=channel, bitrate=config.bitrate, ignore_config=True)

    # PCAN-Basic expects explicit FD timing. Its classical bitrate option is
    # ignored in FD mode.
    data_bitrate = config.data_bitrate if config.data_bitrate is not None else 2_000_000
    timing = can.BitTimingFd.from_sample_point(
        f_clock=80_000_000,
        nom_bitrate=config.bitrate,
        nom_sample_point=config.nominal_sample_point,
        data_bitrate=data_bitrate,
        data_sample_point=config.data_sample_point,
    )
    if (timing.nom_bitrate, timing.data_bitrate) != (config.bitrate, data_bitrate):
        raise ValueError("PCAN FD timing cannot produce the requested bitrates exactly")
    return can.Bus(interface="pcan", channel=channel, timing=timing, fd=True, ignore_config=True)


def make_frame(
    arbitration_id: int,
    data: bytes,
    *,
    extended: bool = False,
    fd: bool = False,
    brs: bool = False,
) -> can.Message:
    """Build an 11-bit or 29-bit frame, preserving the supplied payload length."""
    if fd and len(data) not in FD_LENGTHS:
        raise ValueError("CAN-FD payload length must be 0–8, 12, 16, 20, 24, 32, 48, or 64 bytes")
    return can.Message(
        arbitration_id=arbitration_id,
        is_extended_id=extended,
        is_fd=fd,
        bitrate_switch=brs,
        data=data,
        check=True,
    )


def send_frame(bus: can.BusABC, frame: can.Message, timeout: float = 1.0) -> None:
    """Send a frame and reject FD frames on a classical-only bus."""
    if frame.is_fd and bus.protocol == can.CanProtocol.CAN_20:
        raise ValueError("an FD frame requires a bus opened in CAN-FD mode")
    bus.send(frame, timeout=timeout)
