"""The ``ecu-can uds`` command group."""

import argparse
import sys
import threading
from uuid import uuid4

from udsoncan.exceptions import (
    InvalidResponseException,
    NegativeResponseException,
    TimeoutException,
    UnexpectedResponseException,
)

# cli imports this module; reading attributes of the module object at call time avoids an import cycle.
from . import cli
from .isotp_link import DEFAULT_PADDING, IsoTpConfig
from .sim_ecu import run_simulated_ecu
from .transport import BusConfig, open_bus
from .uds import describe_response, open_uds_client, read_raw_dids, send_raw

SESSIONS = {"default": 0x01, "programming": 0x02, "extended": 0x03}
RESETS = {"hard": 0x01, "key-off-on": 0x02, "soft": 0x03}
DEMO_LINK = IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8)


def _named_byte(names: dict[str, int]):
    def parse(value: str) -> int:
        if value.lower() in names:
            return names[value.lower()]
        number = cli.parse_int(value)
        if not 0x01 <= number <= 0x7F:
            raise argparse.ArgumentTypeError(f"expected {', '.join(names)} or a value from 0x01 to 0x7F")
        return number

    return parse


def _did(value: str) -> int:
    did = cli.parse_int(value)
    if not 0 <= did <= 0xFFFF:
        raise argparse.ArgumentTypeError("expected a 16-bit data identifier")
    return did


def _padding(value: str) -> int | bool:
    """Return False for 'none' so None can still mean 'not given on the command line'."""
    if value.lower() == "none":
        return False
    padding = cli.parse_int(value)
    if not 0 <= padding <= 0xFF:
        raise argparse.ArgumentTypeError("expected a byte value or none")
    return padding


def _isotp_options() -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    group = options.add_argument_group("ISO-TP and UDS options")
    group.add_argument("--tx-id", type=cli.parse_int, help="request CAN ID sent by the tester, e.g. 0x7E0")
    group.add_argument("--rx-id", type=cli.parse_int, help="response CAN ID sent by the ECU, e.g. 0x7E8")
    group.add_argument("--extended", action=argparse.BooleanOptionalAction, help="use 29-bit CAN IDs")
    group.add_argument("--frame-fd", action=argparse.BooleanOptionalAction,
                       help="carry ISO-TP in FD frames of up to 64 bytes (requires --fd)")
    group.add_argument("--brs", action=argparse.BooleanOptionalAction, help="switch data bitrate in FD frames")
    group.add_argument("--padding", type=_padding, help=f"pad frames with this byte or none (default: 0x{DEFAULT_PADDING:02X})")
    group.add_argument("--stmin", type=cli.parse_int, help="separation time requested from the ECU (default: 0)")
    group.add_argument("--blocksize", type=cli.parse_int, help="flow-control block size (default: 0, no limit)")
    group.add_argument("--timeout", type=cli.positive_seconds, help="request timeout in seconds (default: 2)")
    return options


def add_commands(commands: argparse._SubParsersAction, bus_options: argparse.ArgumentParser) -> None:
    uds = commands.add_parser("uds", help="UDS diagnostics over ISO-TP")
    leaves = uds.add_subparsers(dest="uds_command", required=True)
    parents = [bus_options, _isotp_options()]

    session = leaves.add_parser("session", parents=parents, help="DiagnosticSessionControl (0x10)")
    session.add_argument("session", type=_named_byte(SESSIONS), help="default, programming, extended or a number")

    leaves.add_parser("tester-present", parents=parents, help="TesterPresent (0x3E)")

    read = leaves.add_parser("read-did", parents=parents, help="ReadDataByIdentifier (0x22), one request per DID")
    read.add_argument("dids", type=_did, nargs="+", metavar="DID")

    reset = leaves.add_parser("reset", parents=parents, help="ECUReset (0x11)")
    reset.add_argument("reset_type", type=_named_byte(RESETS), metavar="TYPE",
                       help="hard, key-off-on, soft or a number")

    raw = leaves.add_parser("raw", parents=parents, help="send a request as given and print the response")
    raw.add_argument("payload", type=cli.parse_hex_bytes, help='quoted hex request, e.g. "22 F1 90"')

    simulate = leaves.add_parser("simulate", parents=parents,
                                 help="act as a simulated ECU on the bus (for a second adapter or channel)")
    simulate.add_argument("--duration", type=cli.positive_seconds, help="seconds to run (default: until Ctrl+C)")

    leaves.add_parser("demo", help="run UDS requests against a simulated ECU on an in-process virtual bus")


def isotp_config(args: argparse.Namespace, profile: dict[str, dict]) -> IsoTpConfig:
    section = profile["isotp"]
    tx_id, rx_id = cli.pick(args, section, "tx_id", None), cli.pick(args, section, "rx_id", None)
    if tx_id is None or rx_id is None:
        raise ValueError("UDS needs --tx-id and --rx-id, or an --ecu profile that sets them")
    padding = cli.pick(args, section, "padding", DEFAULT_PADDING)
    return IsoTpConfig(
        tx_id=tx_id,
        rx_id=rx_id,
        extended=cli.pick(args, section, "extended", False),
        fd=cli.pick(argparse.Namespace(fd=args.frame_fd), section, "fd", False),
        brs=cli.pick(args, section, "brs", False),
        padding=None if padding is False else padding,
        stmin=cli.pick(args, section, "stmin", 0),
        blocksize=cli.pick(args, section, "blocksize", 0),
    )


def run(args: argparse.Namespace, profile: dict[str, dict]) -> int:
    try:
        if args.uds_command == "demo":
            _demo()
            return 0
        bus = cli.bus_config(args, profile)
        link = isotp_config(args, profile)
        timeout = cli.pick(args, profile["uds"], "timeout", 2.0)
        if args.uds_command == "simulate":
            _simulate(bus, link, args.duration)
            return 0
        with open_bus(bus) as can_bus, open_uds_client(can_bus, link, timeout=timeout) as client:
            return _request(client, args)
    except NegativeResponseException as exc:
        print(f"ecu-can: {describe_response(exc.response.get_payload())}", file=sys.stderr)
    except TimeoutException:
        print("ecu-can: no response from the ECU before the timeout", file=sys.stderr)
    except (InvalidResponseException, UnexpectedResponseException) as exc:
        print(f"ecu-can: {exc}", file=sys.stderr)
    return 1


def _request(client, args: argparse.Namespace) -> int:
    command = args.uds_command
    if command == "session":
        response = client.change_session(args.session)
        data = response.service_data
        name = next((n for n, v in SESSIONS.items() if v == args.session), "custom")
        timing = ""
        if data.p2_server_max is not None:
            timing = f" P2 = {data.p2_server_max * 1000:g} ms, P2* = {data.p2_star_server_max * 1000:g} ms."
        print(f"Session 0x{args.session:02X} ({name}) active.{timing}")
    elif command == "tester-present":
        client.tester_present()
        print("Tester present OK")
    elif command == "read-did":
        for did, value in read_raw_dids(client, args.dids).items():
            print(format_did(did, value))
    elif command == "reset":
        client.ecu_reset(args.reset_type)
        print(f"ECU reset 0x{args.reset_type:02X} accepted")
    else:
        response = send_raw(client, args.payload)
        print(f"Response [{len(response)}] {response.hex(' ').upper()} ({describe_response(response)})")
        return 1 if response[:1] == b"\x7F" else 0
    return 0


def format_did(did: int, value: bytes) -> str:
    text = f"0x{did:04X} [{len(value)}] {value.hex(' ').upper()}"
    if value and all(0x20 <= byte < 0x7F for byte in value):
        text += f' "{value.decode("ascii")}"'
    return text


def _simulate(bus: BusConfig, link: IsoTpConfig, duration: float | None) -> None:
    with open_bus(bus) as can_bus, run_simulated_ecu(can_bus, link):
        print(f"Simulated ECU: requests on 0x{link.tx_id:X}, responses on 0x{link.rx_id:X}. "
              "Press Ctrl+C to stop.", flush=True)
        try:
            threading.Event().wait(duration)
        except KeyboardInterrupt:
            pass


def _demo() -> None:
    bus = BusConfig(channel=f"ecu-can-uds-demo-{uuid4()}")
    with (
        open_bus(bus) as ecu_bus,
        open_bus(bus) as tester_bus,
        run_simulated_ecu(ecu_bus, DEMO_LINK),
        open_uds_client(tester_bus, DEMO_LINK, timeout=1.0) as client,
    ):
        client.change_session(SESSIONS["extended"])
        print("Session 0x03 (extended) active.")
        client.tester_present()
        print("Tester present OK")
        for did, value in read_raw_dids(client, [0xF190, 0xF187]).items():
            print(format_did(did, value))
        response = send_raw(client, bytes.fromhex("27 01"))
        print(f"Raw 27 01 -> {response.hex(' ').upper()} ({describe_response(response)})")
    print("UDS demo OK.")
