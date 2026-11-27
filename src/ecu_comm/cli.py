"""Command-line entry points for raw CAN and UDS communication."""

import argparse
import contextlib
import csv
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import can

from . import uds_cli
from .dbc import (
    Decoded,
    Network,
    complete_values,
    decode,
    describe_message,
    encode,
    format_decoded,
    load_dbc,
    parse_assignments,
    value_text,
)
from .ecu_profile import empty_profile, load_profile
from .transport import BusConfig, make_frame, open_bus, send_frame

LIBRARY_LOGGERS = ("UdsClient", "Connection", "isotp")

# Log formats python-can writes (all with CAN-FD) and reads; .trc is read only (its writer has no CAN-FD),
# .mf4 needs the asammdf package.
LOG_FORMATS = (".asc", ".blf", ".csv", ".log")
READ_FORMATS = (*LOG_FORMATS, ".trc", ".db", ".mf4")

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


def _positive_int(value: str) -> int:
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("expected a positive count")
    return count


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


def _dbc_options(with_profile: bool = False) -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    group = options.add_argument_group("DBC options")
    if with_profile:
        group.add_argument("--ecu", metavar="PROFILE.toml", help="take the DBC files from a profile's [dbc] section")
    group.add_argument("--dbc", action="append", metavar="FILE",
                       help="DBC file to decode with (repeat for more; replaces the profile's dbc.files)")
    return options


def _filter_options() -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    group = options.add_argument_group("DBC filters")
    group.add_argument("--message", action="append", metavar="NAME",
                       help="show only this DBC message (repeat for more)")
    group.add_argument("--signal", action="append", metavar="NAME",
                       help="show only this DBC signal, and only frames that carry it (repeat for more)")
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

    rx = commands.add_parser("listen", parents=[bus_options, _dbc_options(), _filter_options()],
                             help="display received frames")
    rx.add_argument("--duration", type=positive_seconds, default=5.0, help="total seconds (default: 5)")
    rx.add_argument("--count", type=_nonnegative_int, default=0,
                    help="stop after this many displayed frames (0: no limit)")
    rx.add_argument("--log", metavar="FILE",
                    help="also record every received frame; the extension picks the format "
                         f"({', '.join(LOG_FORMATS)})")

    log = commands.add_parser("decode", parents=[_dbc_options(with_profile=True), _filter_options()],
                              help="decode a recorded log file with DBC files")
    log.add_argument("logfile", metavar="LOGFILE", help=f"a log file ({', '.join(READ_FORMATS)})")
    log.add_argument("--relative", action="store_true", help="show times from the first frame instead of as logged")
    log.add_argument("--csv", metavar="FILE",
                     help="write one row per decoded signal value (time, ID, message, signal, value, unit) instead")

    dbc = commands.add_parser("dbc", help="inspect DBC files")
    dbc_commands = dbc.add_subparsers(dest="dbc_command", required=True)
    show = dbc_commands.add_parser("show", parents=[_dbc_options(with_profile=True)],
                                   help="list the messages and signals of DBC files")
    show.add_argument("messages", nargs="*", metavar="MESSAGE", help="message names or IDs (default: all)")
    assignments_help = ("Signal=value: a number (12.5, -3, 0x1F) or a value name (State=Running); "
                        "signals left out take their DBC start value, else 0")
    encode = dbc_commands.add_parser("encode", parents=[_dbc_options(with_profile=True)],
                                     help="build a frame from signal values and print it (no bus)")
    encode.add_argument("message", metavar="MESSAGE", help="message name or ID")
    encode.add_argument("values", nargs="*", metavar="SIGNAL=VALUE", help=assignments_help)
    dbc_send = dbc_commands.add_parser("send", parents=[bus_options, _dbc_options()],
                                       help="build a frame from signal values and send it")
    dbc_send.add_argument("message", metavar="MESSAGE", help="message name or ID")
    dbc_send.add_argument("values", nargs="*", metavar="SIGNAL=VALUE", help=assignments_help)
    dbc_send.add_argument("--brs", action="store_true", help="switch data bitrate (CAN-FD messages only)")
    dbc_send.add_argument("--count", type=_positive_int, default=1, help="frames to send (default: 1)")
    dbc_send.add_argument("--period", type=positive_seconds, metavar="SECONDS",
                          help="seconds between frames when --count > 1 (default: the DBC cycle time, else 0.1)")

    demo = commands.add_parser("demo", help="send and receive a frame on two in-process virtual buses")
    demo.add_argument("--fd", action="store_true", help="exercise an FD frame with bitrate switching")

    uds_cli.add_commands(commands, bus_options)
    return parser


def load_args_profile(args: argparse.Namespace) -> dict[str, dict]:
    return load_profile(args.ecu) if getattr(args, "ecu", None) else empty_profile()


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


def load_network(args: argparse.Namespace, profile: dict[str, dict]) -> Network | None:
    """The DBC files from --dbc, else from the profile; None when there are none."""
    files = args.dbc or profile["dbc"].get("files")
    return load_dbc(files) if files else None


@dataclass(frozen=True)
class FrameFilter:
    """Which frames and signals ``listen`` shows. None means no restriction."""

    messages: set[str] | None = None
    signals: set[str] | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace, network: Network | None) -> "FrameFilter":
        if (args.message or args.signal) and network is None:
            raise ValueError("--message and --signal need DBC files (--dbc, or dbc.files in the --ecu profile)")
        messages = set(args.message) if args.message else None
        for name in messages or ():
            network.find(name)
        signals = set(args.signal) if args.signal else None
        if signals:
            known = {signal.name for message in network.messages.values() for signal in message.signals}
            unknown = sorted(signals - known)
            if unknown:
                raise ValueError(f"no signal named {', '.join(unknown)} in the DBC files")
        return cls(messages, signals)

    def select(self, frame: can.Message, network: Network | None) -> tuple[bool, Decoded | None]:
        """Whether to show a frame, and its decoding (None for an unknown ID or without DBC files)."""
        decoded = decode(network, frame) if network is not None else None
        if decoded is None:
            return self.messages is None and self.signals is None, None
        if self.messages is not None and decoded.message.name not in self.messages:
            return False, decoded
        if self.signals is not None and decoded.error is None and not self.signals & decoded.values.keys():
            return False, decoded
        return True, decoded

    def line(self, frame: can.Message, network: Network | None) -> str | None:
        """The text to print for a frame, or None to skip it."""
        show, decoded = self.select(frame, network)
        if not show:
            return None
        if decoded is None:
            return format_frame(frame)
        return f"{format_frame(frame)}  {format_decoded(decoded, self.signals)}"


def _listen(bus: can.BusABC, duration: float, count: int, network: Network | None = None,
            frame_filter: FrameFilter = FrameFilter(), log: str | None = None) -> None:
    deadline = monotonic() + duration
    shown = 0
    writer = _open_log_writer(log) if log else None
    try:
        while not count or shown < count:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            frame = bus.recv(timeout=remaining)
            if frame is None:
                break
            if writer is not None:
                writer.on_message_received(frame)
            line = frame_filter.line(frame, network)
            if line is None:
                continue
            print(f"{frame.timestamp:.6f} {line}", flush=True)
            shown += 1
    finally:
        if writer is not None:
            writer.stop()


def _check_log_suffix(path: str) -> None:
    if Path(path).suffix.lower() not in LOG_FORMATS:
        raise ValueError(f"--log needs a file ending in {', '.join(LOG_FORMATS)}")


def _open_log_writer(path: str) -> can.Listener:
    _check_log_suffix(path)
    return can.Logger(path)


def _decode_log(args: argparse.Namespace, profile: dict[str, dict]) -> None:
    suffix = Path(args.logfile).suffix.lower()
    if suffix not in READ_FORMATS:
        raise ValueError(f"cannot read {suffix or 'a file without extension'}: use {', '.join(READ_FORMATS)}")
    if not Path(args.logfile).is_file():
        raise ValueError(f"log file not found: {args.logfile}")
    network = load_network(args, profile)
    frame_filter = FrameFilter.from_args(args, network)
    if args.csv and network is None:
        raise ValueError("--csv needs DBC files (--dbc, or dbc.files in the --ecu profile)")
    start = None
    read = shown = 0
    with contextlib.ExitStack() as stack:
        reader = stack.enter_context(can.LogReader(args.logfile))
        rows = None
        if args.csv:
            rows = csv.writer(stack.enter_context(open(args.csv, "w", newline="", encoding="utf-8")))
            rows.writerow(["time", "id", "message", "signal", "value", "unit"])
        for frame in reader:
            read += 1
            if start is None:
                start = frame.timestamp
            time = frame.timestamp - start if args.relative else frame.timestamp
            show, decoded = frame_filter.select(frame, network)
            if not show:
                continue
            shown += 1
            if rows is None:
                text = format_frame(frame) if decoded is None else (
                    f"{format_frame(frame)}  {format_decoded(decoded, frame_filter.signals)}")
                print(f"{time:.6f} {text}")
            elif decoded is not None and decoded.error is None:
                for name, value in decoded.values.items():
                    if frame_filter.signals is None or name in frame_filter.signals:
                        signal = decoded.message.get_signal_by_name(name)
                        rows.writerow([f"{time:.6f}", _frame_id(frame), decoded.message.name, name,
                                       value_text(signal, value), signal.unit or ""])
    target = f", written to {args.csv}" if args.csv else ""
    print(f"{shown} of {read} frames shown{target}", file=sys.stderr)


def _frame_id(frame: can.Message) -> str:
    return f"0x{frame.arbitration_id:08X}" if frame.is_extended_id else f"0x{frame.arbitration_id:03X}"


def _dbc_show(args: argparse.Namespace, profile: dict[str, dict]) -> None:
    network = load_network(args, profile)
    if network is None:
        raise ValueError("no DBC files: give --dbc FILE, or an --ecu profile with dbc.files")
    if args.messages:
        messages = [_find_message(network, text) for text in args.messages]
    else:
        messages = network.sorted_messages()
    for index, message in enumerate(messages):
        if index:
            print()
        print("\n".join(describe_message(message)))


def _dbc_frame(args: argparse.Namespace, profile: dict[str, dict]):
    """The frame built from MESSAGE and SIGNAL=VALUE arguments, its decoded text and the DBC message."""
    network = load_network(args, profile)
    if network is None:
        raise ValueError("no DBC files: give --dbc FILE, or an --ecu profile with dbc.files")
    message = _find_message(network, args.message)
    values = complete_values(message, parse_assignments(message, args.values))
    brs = getattr(args, "brs", False)
    if brs and not message.is_fd:
        raise ValueError(f"--brs needs a CAN-FD message; {message.name} is classic CAN")
    frame = make_frame(message.frame_id, encode(message, values), extended=message.is_extended_frame,
                       fd=message.is_fd, brs=brs)
    return frame, format_decoded(decode(network, frame)), message


def _dbc_send(args: argparse.Namespace, profile: dict[str, dict]) -> None:
    frame, text, message = _dbc_frame(args, profile)
    period = args.period or (message.cycle_time / 1000 if message.cycle_time else 0.1)
    with open_bus(bus_config(args, profile)) as bus:
        next_time = monotonic()
        for index in range(args.count):
            if index:
                next_time += period
                delay = next_time - monotonic()
                if delay > 0:
                    sleep(delay)
            send_frame(bus, frame)
    repeat = f" x{args.count}, every {period * 1000:g} ms" if args.count > 1 else ""
    print(f"Sent{repeat} {format_frame(frame)}  {text}")


def _find_message(network: Network, text: str):
    try:
        frame_id = parse_int(text)
    except argparse.ArgumentTypeError:
        return network.find(text)
    matches = [message for (mid, _), message in network.messages.items() if mid == frame_id]
    if not matches:
        raise ValueError(f"no message with ID {text} in the DBC files")
    if len(matches) > 1:
        raise ValueError(f"ID {text} is both an 11-bit and a 29-bit message: give the message name")
    return matches[0]


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
        if args.command == "decode":
            _decode_log(args, profile)
            return 0
        if args.command == "dbc":
            if args.dbc_command == "show":
                _dbc_show(args, profile)
            elif args.dbc_command == "encode":
                frame, text, _ = _dbc_frame(args, profile)
                print(f"{format_frame(frame)}  {text}")
            else:
                _dbc_send(args, profile)
            return 0

        config = bus_config(args, profile)
        if args.command == "send":
            if args.frame_fd and not config.fd:
                raise ValueError("--frame-fd requires --fd")
            frame = make_frame(args.can_id, args.data, extended=args.extended, fd=args.frame_fd, brs=args.brs)
            with open_bus(config) as bus:
                send_frame(bus, frame)
            print(f"Sent {format_frame(frame)}")
        else:
            network = load_network(args, profile)
            frame_filter = FrameFilter.from_args(args, network)
            if args.log:
                _check_log_suffix(args.log)  # before opening the bus
            with open_bus(config) as bus:
                _listen(bus, args.duration, args.count, network, frame_filter, args.log)
    except KeyboardInterrupt:
        return 130
    except (can.CanError, OSError, ValueError, RuntimeError) as exc:
        print(f"ecu-can: {exc}", file=sys.stderr)
        return 1
    return 0
