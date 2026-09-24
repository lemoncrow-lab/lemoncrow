from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.transports.service import ServiceTransport


def test_service_transport_has_one_canonical_path() -> None:
    assert ServiceTransport.__module__ == "lemoncrow.gateway.transports.service"
    repo = Path(__file__).resolve().parents[2]
    assert not (repo / "src/lemoncrow/gateway/adapters/remote_client.py").exists()


def test_sdk_remote_client_uses_service_transport() -> None:
    from lemoncrow.gateway.sdk.remote import RemoteClient

    client = RemoteClient(base_url="http://127.0.0.1:1", timeout=0.01)
    assert isinstance(client._client, ServiceTransport)
