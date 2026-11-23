"""The public local server binds loopback only; an empty host means every interface."""

from __future__ import annotations

from pathlib import Path

import pytest
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.errors import ServerError


@pytest.mark.parametrize("host", ["", "0.0.0.0", "::", "192.168.1.10"])
def test_a_host_that_is_not_loopback_is_refused(host: str, tmp_path: Path) -> None:
    with pytest.raises(ServerError, match="may only bind loopback"):
        LocalServerConfig(listen_host=host, state_root=tmp_path)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host: str, tmp_path: Path) -> None:
    assert LocalServerConfig(listen_host=host, state_root=tmp_path).loopback_only
