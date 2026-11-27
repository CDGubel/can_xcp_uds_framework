"""The ``ecu-can uds`` command group."""

import argparse
import shlex
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import uuid4

import can
from udsoncan.exceptions import (
    InvalidResponseException,
    NegativeResponseException,
    TimeoutException,
    UnexpectedResponseException,
)

# cli imports this module; reading attributes of the module object at call time avoids an import cycle.
from . import cli
from . import dids as did_table
from . import dtc
from .isotp_link import DEFAULT_PADDING, IsoTpConfig
from .security import load_secret, resolve_algorithm
from .sim_ecu import SimulatedEcu, run_simulated_ecu
from .sim_test_board import SIM_SECRET, TEST_BOARD_FUNCTIONAL_ID, TEST_BOARD_LINK, SimulatedTestBoard
from .transport import BusConfig, LoggedBus, open_bus
from .uds import (
    describe_response,
    open_uds_client,
    read_raw_dids,
    send_functional,
    send_raw,
    unlock,
)

SESSIONS = {"default": 0x01, "programming": 0x02, "extended": 0x03}
RESETS = {"hard": 0x01, "key-off-on": 0x02, "soft": 0x03}
ROUTINE_CONTROLS = {"start": 0x01, "stop": 0x02, "result": 0x03}
DEMO_LINK = IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8)
DEFAULT_ALGORITHM = "aes128-ecb"
DEFAULT_KEEP_ALIVE = 2.0
DEFAULT_LISTEN = 0.5
# Keep-alive TesterPresent with the positive response suppressed.
KEEP_ALIVE_REQUEST = b"\x3E\x80"


def _named_byte(names: dict[str, int]):
    def parse(value: str) -> int:
        if value.lower() in names:
            return names[value.lower()]
        number = cli.parse_int(value)
        if not 0x01 <= number <= 0x7F:
            raise argparse.ArgumentTypeError(f"expected {', '.join(names)} or a value from 0x01 to 0x7F")
        return number

    return parse


def _word(value: str) -> int:
    number = cli.parse_int(value)
    if not 0 <= number <= 0xFFFF:
        raise argparse.ArgumentTypeError("expected a 16-bit identifier")
    return number


def _byte(value: str) -> int:
    number = cli.parse_int(value)
    if not 0 <= number <= 0xFF:
        raise argparse.ArgumentTypeError("expected a byte value, e.g. 0x08")
    return number


def _group(value: str) -> int:
    number = cli.parse_int(value)
    if not 0 <= number <= 0xFFFFFF:
        raise argparse.ArgumentTypeError("expected a 3-byte DTC group, e.g. 0xFFFFFF")
    return number


def _level(value: str) -> int:
    level = cli.parse_int(value)
    if not 0x01 <= level <= 0x7D or level % 2 == 0:
        raise argparse.ArgumentTypeError("expected an odd requestSeed level from 0x01 to 0x7D")
    return level


def _padding(value: str) -> int | bool:
    """Return False for 'none' so None can still mean 'not given on the command line'."""
    if value.lower() == "none":
        return False
    padding = cli.parse_int(value)
    if not 0 <= padding <= 0xFF:
        raise argparse.ArgumentTypeError("expected a byte value or none")
    return padding


def _expect(value: str) -> bytes:
    """Hex prefix of the expected response, or 'none' (empty bytes) for no response."""
    return b"" if value.lower() == "none" else cli.parse_hex_bytes(value)


def _nonnegative_seconds(value: str) -> float:
    return 0.0 if float(value) == 0 else cli.positive_seconds(value)


def _isotp_options() -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    group = options.add_argument_group("ISO-TP and UDS options")
    group.add_argument("--tx-id", type=cli.parse_int, help="request CAN ID sent by the tester, e.g. 0x7E0")
    group.add_argument("--rx-id", type=cli.parse_int, help="response CAN ID sent by the ECU, e.g. 0x7E8")
    group.add_argument("--functional-id", type=cli.parse_int, help="functional request CAN ID, e.g. 0x7DF")
    group.add_argument("--extended", action=argparse.BooleanOptionalAction, help="use 29-bit CAN IDs")
    group.add_argument("--frame-fd", action=argparse.BooleanOptionalAction,
                       help="carry ISO-TP in FD frames of up to 64 bytes (requires --fd)")
    group.add_argument("--brs", action=argparse.BooleanOptionalAction, help="switch data bitrate in FD frames")
    group.add_argument("--padding", type=_padding, help=f"pad frames with this byte or none (default: 0x{DEFAULT_PADDING:02X})")
    group.add_argument("--stmin", type=cli.parse_int, help="separation time requested from the ECU (default: 0)")
    group.add_argument("--blocksize", type=cli.parse_int, help="flow-control block size (default: 0, no limit)")
    group.add_argument("--timeout", type=cli.positive_seconds, help="request timeout in seconds (default: 2)")
    return options


def _security_options() -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    group = options.add_argument_group("SecurityAccess options")
    group.add_argument("--key-algorithm", metavar="NAME",
                       help=f"{DEFAULT_ALGORITHM} (default) or package.module:function")
    group.add_argument("--key-file", metavar="PATH",
                       help="file holding the secret key as hex (default: $ECU_COMM_SECURITY_KEY)")
    return options


def _add_request_commands(leaves: argparse._SubParsersAction, parents: list[argparse.ArgumentParser]) -> None:
    """Commands that send requests over an open connection: on the command line and in the shell."""
    session = leaves.add_parser("session", parents=parents, help="DiagnosticSessionControl (0x10)")
    session.add_argument("session", type=_named_byte(SESSIONS), help="default, programming, extended or a number")

    leaves.add_parser("tester-present", parents=parents, help="TesterPresent (0x3E)")

    read = leaves.add_parser("read-did", parents=parents, help="ReadDataByIdentifier (0x22), one request per DID")
    read.add_argument("dids", nargs="+", metavar="DID", help="a number (0xF190) or a name from the profile's [dids]")

    write = leaves.add_parser("write-did", parents=parents, help="WriteDataByIdentifier (0x2E)")
    write.add_argument("did", metavar="DID", help="a number (0xF184) or a name from the profile's [dids]")
    write.add_argument("data", type=cli.parse_hex_bytes, help='quoted hex value, e.g. "01 02 03"')

    reset = leaves.add_parser("reset", parents=parents, help="ECUReset (0x11)")
    reset.add_argument("reset_type", type=_named_byte(RESETS), metavar="TYPE",
                       help="hard, key-off-on, soft or a number")

    routine = leaves.add_parser("routine", parents=parents, help="RoutineControl (0x31)")
    routine.add_argument("control", type=_named_byte(ROUTINE_CONTROLS), metavar="CONTROL",
                         help="start, stop, result or a number")
    routine.add_argument("rid", type=_word, metavar="RID")
    routine.add_argument("data", nargs="?", type=cli.parse_hex_bytes, default=b"",
                         help="quoted hex routine option record")

    security = leaves.add_parser("unlock", parents=[*parents, _security_options()],
                                 help="SecurityAccess (0x27): request a seed and send the computed key")
    security.add_argument("level", type=_level, help="odd requestSeed level, e.g. 1")

    dtcs = leaves.add_parser("dtc", help="read or clear diagnostic trouble codes (0x19, 0x14)")
    dtc_commands = dtcs.add_subparsers(dest="dtc_command", required=True)
    mask_help = "status mask byte, e.g. 0x08 for confirmed DTCs (default: 0xFF, any status bit)"
    count = dtc_commands.add_parser("count", parents=parents, help="number of DTCs matching a status mask (19 01)")
    count.add_argument("--mask", type=_byte, default=0xFF, help=mask_help)
    read_dtcs = dtc_commands.add_parser("read", parents=parents, help="DTCs matching a status mask (19 02)")
    read_dtcs.add_argument("--mask", type=_byte, default=0xFF, help=mask_help)
    dtc_commands.add_parser("supported", parents=parents, help="every DTC the ECU supports, with its status (19 0A)")
    clear = dtc_commands.add_parser("clear", parents=parents, help="ClearDiagnosticInformation (14): clears DTCs "
                                                                  "and their stored data on the ECU")
    clear.add_argument("group", nargs="?", type=_group, default=dtc.ALL_GROUPS,
                       help="DTC group or single DTC, 3 bytes (default: 0xFFFFFF, all)")

    raw = leaves.add_parser("raw", parents=parents, help="send a request as given and print the response")
    raw.add_argument("payload", type=cli.parse_hex_bytes, help='quoted hex request, e.g. "22 F1 90"')
    raw.add_argument("--expect", type=_expect, metavar="HEX|none",
                     help="fail unless the response starts with these bytes (none: no response)")

    functional = leaves.add_parser("functional", parents=parents,
                                   help="send a single-frame request to the functional ID and list the responses")
    functional.add_argument("payload", type=cli.parse_hex_bytes, help='quoted hex request, e.g. "3E 80"')
    functional.add_argument("--listen", type=cli.positive_seconds, default=DEFAULT_LISTEN,
                            help=f"seconds to collect responses (default: {DEFAULT_LISTEN})")
    functional.add_argument("--expect", type=_expect, metavar="HEX|none",
                            help="fail unless exactly one response starts with these bytes (none: no response)")


def _log_options() -> argparse.ArgumentParser:
    options = argparse.ArgumentParser(add_help=False)
    group = options.add_argument_group("recording")
    group.add_argument("--log", metavar="FILE",
                       help="record every frame sent and received; the extension picks the format "
                            f"({', '.join(cli.LOG_FORMATS)})")
    return options


def add_commands(commands: argparse._SubParsersAction, bus_options: argparse.ArgumentParser) -> None:
    uds = commands.add_parser("uds", help="UDS diagnostics over ISO-TP")
    leaves = uds.add_subparsers(dest="uds_command", required=True)
    parents = [bus_options, _isotp_options(), _log_options()]
    _add_request_commands(leaves, parents)

    table = leaves.add_parser("dids", help="list the DIDs named in a profile's [dids] table (no bus)")
    table.add_argument("--ecu", metavar="PROFILE.toml", required=True, help="the profile with the [dids] table")
    table.add_argument("names", nargs="*", metavar="DID", help="DID numbers or names (default: all)")

    shell = leaves.add_parser("shell", parents=[*parents, _security_options()],
                              help="run requests from a script or typed lines over one connection, "
                                   "with TesterPresent in the background")
    shell.add_argument("script", nargs="?", help="file with one command per line (default: read standard input)")
    shell.add_argument("--keep-alive", type=_nonnegative_seconds, metavar="SECONDS",
                       help=f"TesterPresent period, 0 to disable (default: {DEFAULT_KEEP_ALIVE:g})")

    simulate = leaves.add_parser("simulate", parents=[*parents, _security_options()],
                                 help="act as a simulated ECU on the bus (for a second adapter or channel)")
    simulate.add_argument("--board", choices=("generic", "test_board"), default="generic",
                          help="generic: minimal ECU; test_board: bench-board behaviour, 0x60A/0x68A/0x601 "
                               "and padding 0xAA unless given")
    simulate.add_argument("--duration", type=cli.positive_seconds, help="seconds to run (default: until Ctrl+C)")

    leaves.add_parser("demo", help="run UDS requests against a simulated ECU on an in-process virtual bus")


def isotp_config(args: argparse.Namespace, profile: dict[str, dict],
                 defaults: IsoTpConfig | None = None) -> IsoTpConfig:
    section = profile["isotp"]
    tx_id = cli.pick(args, section, "tx_id", defaults and defaults.tx_id)
    rx_id = cli.pick(args, section, "rx_id", defaults and defaults.rx_id)
    if tx_id is None or rx_id is None:
        raise ValueError("UDS needs --tx-id and --rx-id, or an --ecu profile that sets them")
    padding = cli.pick(args, section, "padding", defaults.padding if defaults else DEFAULT_PADDING)
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


@dataclass
class Connection:
    """What a request needs: the client, the bus for functional frames, and shell-wide defaults."""

    client: object
    bus: can.BusABC
    link: IsoTpConfig
    functional_id: int | None
    key_algorithm: str | None = None
    key_file: str | None = None
    dids: did_table.DidTable = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def run(args: argparse.Namespace, profile: dict[str, dict]) -> int:
    try:
        if args.uds_command == "demo":
            _demo()
            return 0
        if args.uds_command == "dids":
            _list_dids(profile["dids"], args.names)
            return 0
        if args.log:
            cli.check_log_suffix(args.log)  # before opening the bus
        bus = cli.bus_config(args, profile)
        defaults = TEST_BOARD_LINK if getattr(args, "board", None) == "test_board" else None
        link = isotp_config(args, profile, defaults)
        functional_id = cli.pick(args, profile["isotp"], "functional_id",
                                 TEST_BOARD_FUNCTIONAL_ID if defaults else None)
        if args.uds_command == "simulate":
            _simulate(bus, link, functional_id, args, profile)
            return 0
        timeout = cli.pick(args, profile["uds"], "timeout", 2.0)
        with _open_logged_bus(bus, args.log) as can_bus, open_uds_client(can_bus, link, timeout=timeout) as client:
            connection = Connection(
                client, can_bus, link, functional_id,
                getattr(args, "key_algorithm", None) or profile["security"].get("algorithm"),
                getattr(args, "key_file", None) or profile["security"].get("key_file"),
                profile["dids"],
            )
            if args.uds_command == "shell":
                keep_alive = cli.pick(args, profile["uds"], "keep_alive", DEFAULT_KEEP_ALIVE)
                return _shell(connection, args, keep_alive)
            return execute(connection, args)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ecu-can: {exc}", file=sys.stderr)
        return 1


def execute(connection: Connection, args: argparse.Namespace) -> int:
    """Run one request command; report errors on stderr and return the exit status."""
    try:
        with connection.lock:
            return _request(connection, args)
    except NegativeResponseException as exc:
        print(f"ecu-can: {describe_response(exc.response.get_payload())}", file=sys.stderr)
    except TimeoutException:
        print("ecu-can: no response from the ECU before the timeout", file=sys.stderr)
    except (InvalidResponseException, UnexpectedResponseException, ValueError, RuntimeError, OSError,
            can.CanError) as exc:
        print(f"ecu-can: {exc}", file=sys.stderr)
    return 1


def _request(connection: Connection, args: argparse.Namespace) -> int:
    client = connection.client
    command = args.uds_command
    if command == "session":
        data = client.change_session(args.session).service_data
        name = next((n for n, v in SESSIONS.items() if v == args.session), "custom")
        timing = ""
        if data.p2_server_max is not None:
            timing = f" P2 = {data.p2_server_max * 1000:g} ms, P2* = {data.p2_star_server_max * 1000:g} ms."
        print(f"Session 0x{args.session:02X} ({name}) active.{timing}")
    elif command == "tester-present":
        client.tester_present()
        print("Tester present OK")
    elif command == "read-did":
        numbers = [did_table.resolve(connection.dids, text) for text in args.dids]
        for did in numbers:  # print each as it arrives, so a later refusal keeps the earlier values
            value = read_raw_dids(client, [did])[did]
            print(format_did(did, value, connection.dids))
    elif command == "write-did":
        did = did_table.resolve(connection.dids, args.did)
        client.write_data_by_identifier(did, args.data)
        print(f"0x{did:04X} written [{len(args.data)}] {_hex(args.data)}")
    elif command == "reset":
        client.ecu_reset(args.reset_type)
        print(f"ECU reset 0x{args.reset_type:02X} accepted")
    elif command == "routine":
        data = client.routine_control(args.rid, args.control, args.data or None).service_data
        record = bytes(data.routine_status_record or b"")
        name = next((n for n, v in ROUTINE_CONTROLS.items() if v == args.control), f"0x{args.control:02X}")
        print(f"Routine 0x{args.rid:04X} {name}: [{len(record)}] {_hex(record)}".rstrip())
    elif command == "unlock":
        algorithm = resolve_algorithm(args.key_algorithm or connection.key_algorithm or DEFAULT_ALGORITHM)
        secret = load_secret(args.key_file or connection.key_file)
        if unlock(client, args.level, lambda seed, level: algorithm(seed, level, secret)):
            print(f"Security level 0x{args.level:02X} unlocked")
        else:
            print(f"Security level 0x{args.level:02X} already unlocked (zero seed)")
    elif command == "dtc":
        return _dtc(connection, args)
    elif command == "functional":
        if connection.functional_id is None:
            raise ValueError("functional requests need --functional-id, or an --ecu profile that sets it")
        responses = send_functional(client, connection.bus, connection.link, connection.functional_id,
                                    args.payload, args.listen)
        for response in responses:
            print(_format_response(response))
        if not responses:
            print("No response")
        if args.expect is not None:
            matched = (not responses) if args.expect == b"" else (
                len(responses) == 1 and responses[0].startswith(args.expect))
            return _check_expectation(matched, args.expect, responses)
    else:
        try:
            response = send_raw(client, args.payload)
        except TimeoutException:
            if args.expect == b"":
                print("No response")
                return 0
            raise
        print(_format_response(response))
        if args.expect is not None:
            return _check_expectation(response.startswith(args.expect) and args.expect != b"",
                                      args.expect, [response])
        return 1 if response[:1] == b"\x7F" else 0
    return 0


def _dtc(connection: Connection, args: argparse.Namespace) -> int:
    """Send a DTC request; print the result, or the negative response on stderr (exit status 1)."""
    sub = args.dtc_command
    if sub == "clear":
        request = dtc.clear_request(args.group)
    elif sub == "count":
        request = dtc.read_request(dtc.REPORT_NUMBER_BY_STATUS_MASK, args.mask)
    elif sub == "read":
        request = dtc.read_request(dtc.REPORT_BY_STATUS_MASK, args.mask)
    else:
        request = dtc.read_request(dtc.REPORT_SUPPORTED)
    response = send_raw(connection.client, request)
    if response[:1] == b"\x7F":
        print(f"ecu-can: {describe_response(response)}", file=sys.stderr)
        return 1
    if sub == "clear":
        if response != b"\x54":
            raise ValueError(f"expected 54, got {_hex(response)}")
        which = "all DTC groups" if args.group == dtc.ALL_GROUPS else f"DTC group 0x{args.group:06X}"
        print(f"Clear accepted ({which})")
    elif sub == "count":
        print(dtc.format_count(dtc.parse_count(response), args.mask, response))
    else:
        subfunction = request[1]
        mask = args.mask if sub == "read" else None
        for line in dtc.format_list(dtc.parse_list(response, subfunction), subfunction, mask, response):
            print(line)
    return 0


def _check_expectation(matched: bool, expect: bytes, responses: list[bytes]) -> int:
    if matched:
        return 0
    wanted = "no response" if expect == b"" else f"a response starting {_hex(expect)}"
    got = ", ".join(_hex(r) for r in responses) or "no response"
    print(f"ecu-can: expected {wanted}, got {got}", file=sys.stderr)
    return 1


def _hex(data: bytes) -> str:
    return data.hex(" ").upper()


def _format_response(response: bytes) -> str:
    return f"Response [{len(response)}] {_hex(response)} ({describe_response(response)})"


def format_did(did: int, value: bytes, table: did_table.DidTable | None = None) -> str:
    """``0xF190 [17] 54 45 ...``, with the DID's name and fields when the table describes it."""
    definition = (table or {}).get(did)
    name = f" {definition.name}" if definition else ""
    text = f"0x{did:04X}{name} [{len(value)}] {_hex(value)}".rstrip()
    fields = did_table.format_fields(definition, value) if definition else ""
    if fields:
        return f"{text}  {fields}"
    if value and all(0x20 <= byte < 0x7F for byte in value):
        text += f' "{value.decode("ascii")}"'
    return text


def _list_dids(table: did_table.DidTable, names: list[str]) -> None:
    if not table:
        raise ValueError("the profile has no [dids] table")
    numbers = [did_table.resolve(table, name) for name in names] if names else sorted(table)
    for did in numbers:
        if did not in table:
            raise ValueError(f"0x{did:04X} is not in the profile's [dids] table")
        for line in did_table.describe(table[did]):
            print(line)


@contextmanager
def _open_logged_bus(config: BusConfig, log: str | None):
    """Open the bus; with a log file, every frame sent or received on it is also recorded."""
    with open_bus(config, receive_own_messages=bool(log)) as can_bus:
        if not log:
            yield can_bus
            return
        writer = cli.open_log_writer(log)
        try:
            with LoggedBus(can_bus, writer, echoes=True) as logged:
                yield logged
        finally:
            writer.stop()


# -- shell ------------------------------------------------------------------------


class ShellError(Exception):
    """A shell line could not be parsed (message ready to print), or help was shown (empty)."""


class _ShellParser(argparse.ArgumentParser):
    def error(self, message: str):
        raise ShellError(f"{self.prog}: {message}")

    def exit(self, status: int = 0, message: str | None = None):
        if message:
            raise ShellError(message.strip())
        raise ShellError("")


def shell_parser() -> argparse.ArgumentParser:
    parser = _ShellParser(prog="uds>", add_help=False)
    leaves = parser.add_subparsers(dest="uds_command", required=True, parser_class=_ShellParser)
    _add_request_commands(leaves, [])
    sleep = leaves.add_parser("sleep", help="wait (TesterPresent keeps running)")
    sleep.add_argument("seconds", type=cli.positive_seconds)
    leaves.add_parser("help", help="list the commands")
    leaves.add_parser("quit", help="leave the shell")
    return parser


def _shell_lines(script: str | None):
    """Yield (line number, line); typed lines get a prompt."""
    if script is not None:
        with open(script, encoding="utf-8") as file:
            yield from enumerate(file, 1)
    elif sys.stdin.isatty():
        number = 0
        while True:
            try:
                line = input("uds> ")
            except EOFError:
                print()
                return
            number += 1
            yield number, line
    else:
        yield from enumerate(sys.stdin, 1)


def _shell(connection: Connection, args: argparse.Namespace, keep_alive: float) -> int:
    """Run commands line by line. Scripts and piped input stop at the first failure."""
    interactive = args.script is None and sys.stdin.isatty()
    parser = shell_parser()
    stop = threading.Event()
    keeper = None
    if keep_alive:
        keeper = threading.Thread(target=_keep_alive, args=(connection, keep_alive, stop),
                                  name="uds-keep-alive", daemon=True)
        keeper.start()
    status = 0
    try:
        for number, line in _shell_lines(args.script):
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError as exc:
                tokens, error = None, f"line {number}: {exc}"
            else:
                error = None
            if tokens == []:
                continue
            if tokens is not None:
                if not interactive:
                    print(f"> {shlex.join(tokens)}", flush=True)
                try:
                    command = parser.parse_args(tokens)
                except ShellError as exc:
                    if not str(exc):
                        continue
                    error = f"line {number}: {exc}"
            if error is not None:
                print(f"ecu-can: {error}", file=sys.stderr)
                result = 1
            elif command.uds_command == "quit":
                break
            elif command.uds_command == "help":
                parser.print_help()
                result = 0
            elif command.uds_command == "sleep":
                time.sleep(command.seconds)
                result = 0
            else:
                result = execute(connection, command)
            sys.stdout.flush()
            if result:
                status = 1
                if not interactive:
                    print(f"ecu-can: stopped at line {number}", file=sys.stderr)
                    break
    except KeyboardInterrupt:
        status = 130
    finally:
        stop.set()
        if keeper is not None:
            keeper.join(timeout=1.0)
    return status


def _keep_alive(connection: Connection, period: float, stop: threading.Event) -> None:
    while not stop.wait(period):
        try:
            with connection.lock:
                connection.client.conn.send(KEEP_ALIVE_REQUEST)
        except Exception as exc:  # keep the shell usable; the next request reports a dead link
            print(f"ecu-can: keep-alive failed: {exc}", file=sys.stderr)


# -- simulation -------------------------------------------------------------------


def _simulate(bus: BusConfig, link: IsoTpConfig, functional_id: int | None,
              args: argparse.Namespace, profile: dict[str, dict]) -> None:
    if args.board == "test_board":
        secret = load_secret(args.key_file or profile["security"].get("key_file")) or SIM_SECRET
        ecu: SimulatedEcu = SimulatedTestBoard(secret)
    else:
        ecu = SimulatedEcu()
    with _open_logged_bus(bus, args.log) as can_bus, run_simulated_ecu(can_bus, link, ecu, functional_id):
        functional = f", functional requests on 0x{functional_id:X}" if functional_id is not None else ""
        print(f"Simulated {args.board} ECU: requests on 0x{link.tx_id:X}, responses on 0x{link.rx_id:X}"
              f"{functional}. Press Ctrl+C to stop.", flush=True)
        try:
            threading.Event().wait(args.duration)
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
        print(f"Raw 27 01 -> {_hex(response)} ({describe_response(response)})")
    print("UDS demo OK.")
