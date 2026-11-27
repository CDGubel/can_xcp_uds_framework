"""Open a python-can bus and build validated raw data frames."""

import copy
import threading
import time
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


def open_bus(config: BusConfig, *, receive_own_messages: bool = False) -> can.BusABC:
    """Return a bus that the caller owns. Use it as a context manager.

    With ``receive_own_messages`` every sent frame also comes back from ``recv`` with
    ``is_rx=False`` (PEAK: an echo stamped by the adapter, on the same clock as received frames).
    """
    if config.interface not in DEFAULT_CHANNELS:
        raise ValueError(f"unsupported interface: {config.interface}")
    if config.bitrate <= 0:
        raise ValueError("bitrate must be positive")
    if config.data_bitrate is not None and (not config.fd or config.data_bitrate <= 0):
        raise ValueError("a data bitrate must be positive and requires CAN-FD mode")
    echo = {"receive_own_messages": True} if receive_own_messages else {}  # default calls stay unchanged
    channel = config.channel if config.channel is not None else DEFAULT_CHANNELS[config.interface]
    if not channel.strip():
        raise ValueError("channel must be nonempty")

    if config.interface == "virtual":
        protocol = can.CanProtocol.CAN_FD if config.fd else can.CanProtocol.CAN_20
        return can.Bus(interface="virtual", channel=channel, protocol=protocol, ignore_config=True,
                       **echo)

    from can.interfaces.pcan.basic import PCAN_BITRATES, PCAN_CHANNEL_NAMES

    channel = channel.upper()
    if channel not in PCAN_CHANNEL_NAMES:
        raise ValueError(f"unknown PCAN channel: {channel}")
    if not config.fd:
        if config.bitrate not in PCAN_BITRATES:
            raise ValueError(f"unsupported classical PCAN bitrate: {config.bitrate}")
        return can.Bus(interface="pcan", channel=channel, bitrate=config.bitrate, ignore_config=True,
                       **echo)

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
    return can.Bus(interface="pcan", channel=channel, timing=timing, fd=True, ignore_config=True,
                   **echo)


class LoggedBus(can.BusABC):
    """A bus that also hands every frame it sends or receives to a listener (a ``can.Logger``).

    It is a ``can.BusABC`` in its own right (ISO-TP stacks insist on one) that sends and
    receives through the wrapped bus. Shutting it down leaves the wrapped bus and the
    listener open: their owner closes them.

    With ``echoes`` (the wrapped bus was opened with ``receive_own_messages``), sent frames
    are recorded when their echo comes back, with the adapter's timestamp, and the echo is
    not passed on. Otherwise they are recorded at sending, stamped with the PC clock, which
    on PEAK adapters can be tens of milliseconds off the received frames' clock.
    """

    def __init__(self, bus: can.BusABC, listener: can.Listener, *, echoes: bool = False) -> None:
        super().__init__(channel=bus.channel_info)
        self._bus = bus
        self._listener = listener
        self._lock = threading.Lock()  # frames come from the notifier thread and the senders
        self._can_protocol = bus.protocol
        self.channel_info = bus.channel_info
        # Name sent frames like received ones: the virtual channel, or PCAN_USBBUSn.
        self._channel_name = getattr(bus, "channel_id", None) or bus.channel_info
        self._echoes = echoes

    def _record(self, frame: can.Message) -> None:
        with self._lock:
            self._listener.on_message_received(frame)

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self._bus.send(msg, timeout=timeout)
        if self._echoes:
            return  # recorded when the echo arrives
        sent = copy.copy(msg)
        sent.timestamp = time.time()
        sent.is_rx = False
        if sent.channel is None:
            sent.channel = self._channel_name
        self._record(sent)

    def _recv_internal(self, timeout: float | None) -> tuple[can.Message | None, bool]:
        frame = self._bus.recv(timeout)
        if frame is not None:
            self._record(frame)
            if self._echoes and not frame.is_rx:
                return None, False  # our own frame: recorded, not received
        return frame, False


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
