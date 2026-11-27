"""UDS (ISO 14229) client access through udsoncan over an ISO-TP link."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from time import monotonic

import can
from udsoncan import DidCodec, Response
from udsoncan.client import Client
from udsoncan.configs import default_client_config
from udsoncan.connections import PythonIsoTpConnection

from .isotp_link import IsoTpConfig, build_stack, bus_notifier
from .transport import FD_LENGTHS, make_frame, send_frame

RESPONSE_PENDING = b"\x78"


class RawDidCodec(DidCodec):
    """Return a DID's payload as bytes. Only valid when one DID is read per request."""

    def encode(self, value: bytes) -> bytes:
        return bytes(value)

    def decode(self, did_payload: bytes) -> bytes:
        return bytes(did_payload)

    def __len__(self) -> int:
        raise DidCodec.ReadAllRemainingData


@contextmanager
def open_uds_client(
    bus: can.BusABC,
    config: IsoTpConfig,
    *,
    timeout: float = 2.0,
    data_identifiers: dict | None = None,
) -> Iterator[Client]:
    """Open a udsoncan client on ``bus``. Without codecs, DIDs decode as raw bytes."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    client_config = deepcopy(default_client_config)
    client_config["data_identifiers"] = data_identifiers or {"default": RawDidCodec}
    client_config["request_timeout"] = timeout
    client_config["p2_timeout"] = min(timeout, client_config["p2_timeout"])
    with bus_notifier(bus) as notifier:
        connection = PythonIsoTpConnection(build_stack(bus, notifier, config))
        with Client(connection, config=client_config) as client:
            yield client


def read_raw_dids(client: Client, dids: list[int]) -> dict[int, bytes]:
    """Read each DID in its own request, so payloads of unknown length stay intact."""
    values = {}
    for did in dids:
        response = client.read_data_by_identifier(did)
        values[did] = response.service_data.values[did]
    return values


def send_raw(client: Client, payload: bytes, timeout: float | None = None) -> bytes:
    """Send ``payload`` unchanged and return the response bytes, positive or negative.

    NRC 0x78 (responsePending) extends the wait to P2*. Raises
    ``udsoncan.exceptions.TimeoutException`` when nothing arrives.
    """
    if not payload:
        raise ValueError("a UDS request needs at least a service ID")
    connection = client.conn
    connection.empty_rxqueue()
    connection.send(payload)
    wait = timeout if timeout is not None else client.config["request_timeout"]
    while True:
        deadline = monotonic() + wait
        response = connection.wait_frame(timeout=max(deadline - monotonic(), 0.0), exception=True)
        if response[:1] == b"\x7F" and response[1:2] == payload[:1] and response[2:3] == RESPONSE_PENDING:
            wait = client.config["p2_star_timeout"]
            continue
        return response


def describe_response(response: bytes) -> str:
    """Name a negative response code, or report a positive response."""
    if response[:1] == b"\x7F" and len(response) >= 3:
        return f"negative response 0x{response[2]:02X} ({Response.Code.get_name(response[2])})"
    return "positive response"


def single_frame(payload: bytes, can_id: int, link: IsoTpConfig) -> can.Message:
    """Build the one ISO-TP single frame of a functional request, with ``link``'s framing.

    Classic CAN carries up to 7 payload bytes; FD frames up to 62 (escape format).
    """
    if not payload:
        raise ValueError("a UDS request needs at least a service ID")
    if len(payload) <= 7:
        data = bytes((len(payload),)) + payload
    elif link.fd and len(payload) <= 62:
        data = bytes((0x00, len(payload))) + payload
    else:
        limit = 62 if link.fd else 7
        raise ValueError(f"a functional request must fit in one single frame ({limit} bytes), got {len(payload)}")
    if link.padding is not None:
        size = 8 if len(data) <= 8 else next(n for n in FD_LENGTHS if n >= len(data))
        data += bytes((link.padding,)) * (size - len(data))
    elif link.fd and len(data) > 8:
        raise ValueError("an FD single frame longer than 8 bytes needs padding")
    return make_frame(can_id, data, extended=link.extended, fd=link.fd, brs=link.brs)


def send_functional(
    client: Client, bus: can.BusABC, link: IsoTpConfig, functional_id: int, payload: bytes, listen: float
) -> list[bytes]:
    """Send ``payload`` to ``functional_id`` and collect every response on ``link.rx_id`` for ``listen`` s.

    An empty list means no ECU answered (for example a suppressed positive response).
    """
    if functional_id in (link.tx_id, link.rx_id):
        raise ValueError("the functional ID must differ from the physical request and response IDs")
    frame = single_frame(payload, functional_id, link)
    connection = client.conn
    connection.empty_rxqueue()
    send_frame(bus, frame)
    responses = []
    deadline = monotonic() + listen
    while (remaining := deadline - monotonic()) > 0:
        response = connection.wait_frame(timeout=remaining, exception=False)
        if response is not None:
            responses.append(bytes(response))
    return responses


KeyFunction = Callable[[bytes, int], bytes]


def unlock(client: Client, level: int, compute_key: KeyFunction) -> bool:
    """Request a seed for odd ``level`` and send the computed key.

    Returns False without sending a key when the seed is all zeros (already unlocked).
    """
    if not 0x01 <= level <= 0x7D or level % 2 == 0:
        raise ValueError("a security level is an odd requestSeed value from 0x01 to 0x7D")
    seed = bytes(client.request_seed(level).service_data.seed)
    if not any(seed):
        return False
    client.send_key(level + 1, compute_key(seed, level))
    return True
