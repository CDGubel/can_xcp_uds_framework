"""UDS (ISO 14229) client access through udsoncan over an ISO-TP link."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from time import monotonic

import can
from udsoncan import DidCodec, Response
from udsoncan.client import Client
from udsoncan.configs import default_client_config
from udsoncan.connections import PythonIsoTpConnection

from .isotp_link import IsoTpConfig, build_stack, bus_notifier

RESPONSE_PENDING = b"\x78"


class RawDidCodec(DidCodec):
    """Return a DID's payload as bytes. Only valid when one DID is read per request."""

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
