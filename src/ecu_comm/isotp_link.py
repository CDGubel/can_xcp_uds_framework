"""ISO-TP (ISO 15765-2) links on top of a python-can bus."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import can
import isotp

DEFAULT_PADDING = 0xCC


@dataclass(frozen=True)
class IsoTpConfig:
    """Normal addressing: the tester sends on ``tx_id`` and listens on ``rx_id``."""

    tx_id: int
    rx_id: int
    extended: bool = False
    fd: bool = False
    brs: bool = False
    padding: int | None = DEFAULT_PADDING
    stmin: int = 0
    blocksize: int = 0

    def validate(self) -> None:
        limit = 0x1FFFFFFF if self.extended else 0x7FF
        for name, value in (("tx_id", self.tx_id), ("rx_id", self.rx_id)):
            if not 0 <= value <= limit:
                raise ValueError(f"{name} 0x{value:X} exceeds the {29 if self.extended else 11}-bit range")
        if self.tx_id == self.rx_id:
            raise ValueError("tx_id and rx_id must differ")
        if self.brs and not self.fd:
            raise ValueError("ISO-TP bitrate switching requires FD frames")
        if self.padding is not None and not 0 <= self.padding <= 0xFF:
            raise ValueError("padding must be a byte value")
        if not 0 <= self.stmin <= 0xFF or not 0 <= self.blocksize <= 0xFF:
            raise ValueError("stmin and blocksize must be byte values")

    def reversed(self) -> "IsoTpConfig":
        """Return the matching configuration for the ECU side of the link."""
        return IsoTpConfig(**{**self.__dict__, "tx_id": self.rx_id, "rx_id": self.tx_id})

    def address(self) -> isotp.Address:
        mode = isotp.AddressingMode.Normal_29bits if self.extended else isotp.AddressingMode.Normal_11bits
        return isotp.Address(mode, txid=self.tx_id, rxid=self.rx_id)

    def params(self) -> dict:
        return {
            "stmin": self.stmin,
            "blocksize": self.blocksize,
            "tx_padding": self.padding,
            "tx_data_length": 64 if self.fd else 8,
            "can_fd": self.fd,
            "bitrate_switch": self.brs,
            "blocking_send": False,
        }


def build_stack(bus: can.BusABC, notifier: can.Notifier, config: IsoTpConfig) -> isotp.NotifierBasedCanStack:
    """Create an ISO-TP stack that is not started yet."""
    config.validate()
    if config.fd and bus.protocol == can.CanProtocol.CAN_20:
        raise ValueError("ISO-TP over FD frames requires a bus opened in CAN-FD mode")
    return isotp.NotifierBasedCanStack(bus, notifier, address=config.address(), params=config.params())


@contextmanager
def bus_notifier(bus: can.BusABC) -> Iterator[can.Notifier]:
    """Dispatch received frames to listeners until the block exits."""
    notifier = can.Notifier(bus, [], timeout=0.05)
    try:
        yield notifier
    finally:
        notifier.stop()


@contextmanager
def open_isotp(bus: can.BusABC, config: IsoTpConfig) -> Iterator[isotp.NotifierBasedCanStack]:
    """Run an ISO-TP stack on ``bus`` until the block exits. The caller still owns the bus."""
    with bus_notifier(bus) as notifier:
        stack = build_stack(bus, notifier, config)
        stack.start()
        try:
            yield stack
        finally:
            stack.stop()
