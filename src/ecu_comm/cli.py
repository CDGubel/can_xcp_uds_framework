"""Command-line entry points for raw CAN and UDS communication."""

import argparse
import logging
import math
import sys
from time import monotonic
from uuid import uuid4

import can

from . import uds_cli
from .ecu_profile import load_profile
from .transport import BusConfig, make_frame, open_bus, send_frame

LIBRARY_LOGGERS = ("UdsClient", "Connection", "isotp")

BUS_DEFAULTS = {
    "interface": "virtual",
    "channel": None,
    "bitrate": 500_000,
    "fd": False,
    "data_bitrate": None,
    "nominal_sample_point": 80.0,
    "data_sample_point": 75.0,
}


def parse_int(value: str) -> int:
    try:
        return int(value, 16 if value.lower().startswith("0x") else 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a decimal or 0x-prefixed integer") from exc


def parse_hex_bytes(value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('expected hex bytes, for example "01 02 AB"') from exc


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number of seconds")
    return seconds


def _sample_point(value: str) -> float:
    percent = float(value)
    if not 50 <= percent < 100:
        raise argparse.ArgumentTypeError("expected a percentage from 50 up to 100 (exclusive)")
    return percent


def _nonnegative_int(value: str) -> int:
    count = int(value)
    if count < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative count")
    return count


def _bus_options() -> argparse.ArgumentParser:
    """Bus options default to None so an --ecu profile can fill them in."""
    options = argparse.ArgumentParser(add_help=False)
    group = options.add_argument_group("bus options")
    group.add_argument("--ecu", metavar="PROFILE.toml", help="load bus, ISO-TP and UDS settings from a profile")
    group.add_argument("--interface", choices=("virtual", "pcan"), help="default: virtual")
    group.add_argument("--channel", help="default: ecu-can (virtual) or PCAN_USBBUS1 (PCAN)")
    group.add_argument("--bitrate", type=int, help="nominal bit/s (default: 500000)")
    group.add_argument("--fd", action=argparse.BooleanOptionalAction, help="open the bus in CAN-FD mode")
    group.add_argument("--data-bitrate", type=int, help="FD data bit/s (default: 2000000)")
    group.add_argument("--nominal-sample-point", type=_sample_point,
                       help="PCAN FD nominal sample point in percent (default: 80)")
    group.add_argument("--data-sample-point", type=_sample_point,
                       help="PCAN FD data sample point in percent (default: 75)")
    return options


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CAN, CAN-FD and UDS bench tools")
    parser.add_argument("-v", "--verbose", action="store_true", help="show ISO-TP and UDS library logs")
    bus_options = _bus_options()

    commands = parser.add_subparsers(dest="command", required=True)
    tx = commands.add_parser("send", parents=[bus_options], help="send one raw data frame")
    tx.add_argument("can_id", type=parse_int, metavar="ID")
    tx.add_argument("data", nargs="?", type=parse_hex_bytes, default=b"", help="quoted hex payload")
    tx.add_argument("--extended", action="store_true", help="use a 29-bit ID")
    tx.add_argument("--frame-fd", action="store_true", help="send an FD frame (requires --fd)")
    tx.add_argument("--brs", action="store_true", help="switch data bitrate (requires --frame-fd)")

    rx = commands.add_parser("listen", parents=[bus_options], help="display received frames")
    rx.add_argument("--duration", type=positive_seconds, default=5.0, help="total seconds (default: 5)")
    rx.add_argument("--count", type=_nonnegative_int, default=0, help="stop after this many frames (0: no limit)")

    demo = commands.add_parser("demo", help="send and receive a frame on two in-process virtual buses")
    demo.add_argument("--fd", action="store_true", help="exercise an FD frame with bitrate switching")

    uds_cli.add_commands(commands, bus_options)
    return parser


def load_args_profile(args: argparse.Namespace) -> dict[str, dict]:
    return load_profile(args.ecu) if getattr(args, "ecu", None) else {"bus": {}, "isotp": {}, "uds": {}}


def pick(args: argparse.Namespace, section: dict, name: str, default):
    """Command line first, then the profile, then the built-in default."""
    value = getattr(args, name, None)
    if value is not None:
        return value
    return section.get(name, default)


def bus_config(args: argparse.Namespace, profile: dict[str, dict]) -> BusConfig:
    return BusConfig(**{name: pick(args, profile["bus"], name, default) for name, default in BUS_DEFAULTS.items()})


def format_frame(frame: can.Message) -> str:
    width = 8 if frame.is_extended_id else 3
    flags = ["EXT" if frame.is_extended_id else "STD", "FD" if frame.is_fd else "CAN"]
    if frame.bitrate_switch:
        flags.append("BRS")
    if frame.error_state_indicator:
        flags.append("ESI")
    if frame.is_remote_frame:
        flags.append("RTR")
    if frame.is_error_frame:
        flags.append("ERROR")
    return (
        f"0x{frame.arbitration_id:0{width}X} {' '.join(flags)} "
        f"[{frame.dlc}] {frame.data.hex(' ').upper()}"
    ).rstrip()


def _listen(bus: can.BusABC, duration: float, count: int) -> None:
    deadline = monotonic() + duration
    received = 0
    while not count or received < count:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        frame = bus.recv(timeout=remaining)
        if frame is None:
            break
        print(f"{frame.timestamp:.6f} {format_frame(frame)}", flush=True)
        received += 1


def _demo(fd: bool) -> None:
    config = BusConfig(channel=f"ecu-can-demo-{uuid4()}", fd=fd)
    frame = make_frame(0x123, bytes(range(12 if fd else 8)), fd=fd, brs=fd)
    with open_bus(config) as receiver, open_bus(config) as sender:
        send_frame(sender, frame)
        received = receiver.recv(timeout=1.0)
        if received is None or not received.equals(frame, check_direction=False, timestamp_delta=None,
                                                   check_channel=False):
            raise RuntimeError("virtual demo did not receive the expected frame")
    print(f"Virtual round trip OK. {format_frame(received)}")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(relativeCreated)8.1f %(name)s %(message)s")
    else:
        # The CLI reports errors itself, so keep the libraries' own error logs quiet.
        for name in LIBRARY_LOGGERS:
            logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if args.command == "demo":
            _demo(args.fd)
            return 0

        profile = load_args_profile(args)
        if args.command == "uds":
            return uds_cli.run(args, profile)

        config = bus_config(args, profile)
        if args.command == "send":
            if args.frame_fd and not config.fd:
                raise ValueError("--frame-fd requires --fd")
            frame = make_frame(args.can_id, args.data, extended=args.extended, fd=args.frame_fd, brs=args.brs)
            with open_bus(config) as bus:
                send_frame(bus, frame)
            print(f"Sent {format_frame(frame)}")
        else:
            with open_bus(config) as bus:
                _listen(bus, args.duration, args.count)
    except KeyboardInterrupt:
        return 130
    except (can.CanError, OSError, ValueError, RuntimeError) as exc:
        print(f"ecu-can: {exc}", file=sys.stderr)
        return 1
    return 0
