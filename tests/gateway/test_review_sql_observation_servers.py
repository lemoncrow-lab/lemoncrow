"""SQL observed from a declared database service's own statement log."""

from __future__ import annotations

from typing import Any

import pytest
from lemoncrow_client.environment import Check, DefinitionError, check_digest, parse_environment

from lemoncrow.pro.capabilities.review.runtime_observation import LIMITATIONS, build_observation
from lemoncrow.pro.environment import oci
from lemoncrow.pro.environment.services import StartedService

IMAGE = "registry.example/db@sha256:" + "b" * 64
DEFINITION = f'version = 1\n[services.db]\nimage = "{IMAGE}"\nready = "true"\n'
PG_LOG = (
    "lc-sql LOG:  statement: CREATE TABLE t (id int)\n"
    "lc-sql LOG:  execute <unnamed>: SELECT *\n\tFROM t\n"
    "lc-sql LOG:  checkpoint starting: time\n"
    "lc-sql LOG:  statement: BEGIN\n"
    "lc-sql LOG:  statement: DELETE FROM t\n"
)
MYSQL_LOG = (
    "/usr/sbin/mariadbd, Version: 11.4. started with:\n"
    "241118 21:00:00\t    5 Connect\troot@host on app\n"
    "\t\t    5 Query\tSET AUTOCOMMIT = 1\n"
    "\t\t    5 Query\tINSERT INTO t\nVALUES (1)\n"
    "\t\t    5 Quit\t\n"
)


def test_postgres_statements_come_from_its_log_without_server_messages() -> None:
    found = oci.parse_sql_log("postgres", PG_LOG)
    assert "CREATE TABLE t (id int)" in found and "SELECT *\nFROM t" in found and "DELETE FROM t" in found
    assert not any("checkpoint" in item for item in found)


def test_mysql_statements_come_from_its_general_log() -> None:
    found = oci.parse_sql_log("mysql", MYSQL_LOG)
    assert "INSERT INTO t\nVALUES (1)" in found
    assert not any("root@host" in item for item in found)


def test_a_service_declares_its_settings_and_engine() -> None:
    text = DEFINITION + 'sql = "postgres"\n[services.db.env]\nPOSTGRES_DB = "app"\n'
    service = parse_environment(text, source="e").services[0]
    assert service.sql == "postgres" and service.env == (("POSTGRES_DB", "app"),)


@pytest.mark.parametrize(
    "extra", ['sql = "oracle"\n', '[services.db.env]\n"bad-name" = "x"\n', "[services.db.env]\nX = 1\n"]
)
def test_bad_service_settings_are_refused(extra: str) -> None:
    with pytest.raises(DefinitionError):
        parse_environment(DEFINITION + extra, source="e")


def test_settings_change_a_checks_digest_only_when_present() -> None:
    check = Check(id="c", paths=(".",), run="true", needs=("db",))
    plain = parse_environment(DEFINITION, source="e")
    logged = parse_environment(DEFINITION + 'sql = "postgres"\n', source="e")
    assert check_digest(plain, check) != check_digest(logged, check)
    assert check_digest(plain, check) == check_digest(parse_environment(DEFINITION, source="e"), check)


def test_server_statements_become_effects_with_their_engine_as_source() -> None:
    observation = build_observation(
        side="new",
        raw=None,
        server_sql=(("postgres", "CREATE TABLE audit (id int)"), ("postgres", "DELETE FROM t WHERE id = 1")),
    )
    effects = {row.category: row for row in observation.effects}
    assert effects["schema_mutation"].sources == ("postgres",)
    assert effects["db_query"].identity == "DELETE FROM t WHERE id = ?"
    assert any("statement log" in item for item in LIMITATIONS)


def test_a_credential_in_a_statement_is_redacted() -> None:
    secret = "AKIA" + "QWERTYUIOPASDFGH"
    wire = str(build_observation(side="new", raw=None, server_sql=(("postgres", f"SET k = '{secret}'"),)).to_wire())
    assert secret not in wire


@pytest.mark.parametrize(("reachable", "outcome"), [(False, "blocked"), (True, "")])
def test_a_connect_is_blocked_only_where_nothing_is_reachable(reachable: bool, outcome: str) -> None:
    raw = {"rows": [{"t": "connect", "addr": "172.19.0.2", "port": 5432, "result": "attempted"}]}
    observation = build_observation(side="new", raw=raw, network_reachable=reachable)
    assert [row.outcome for row in observation.effects if row.category == "network"] == [outcome]


class _Engine:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.runs: list[oci.ContainerSpec] = []

    def remove(self, _name: str) -> None:
        return None

    def run(self, spec: oci.ContainerSpec) -> None:
        self.runs.append(spec)

    def exec(self, *_args: Any, **_kwargs: Any) -> Any:
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return type("Done", (), {"output": result, "exit_code": 0})()


def _backend(engine: _Engine, *, capture: bool = True) -> oci.OciServiceBackend:
    return oci.OciServiceBackend(engine, network="n", prefix="rt", capture_sql=capture)  # type: ignore[arg-type]


def test_the_log_is_switched_on_only_for_observed_runs_of_an_engine() -> None:
    service = parse_environment(DEFINITION + 'sql = "postgres"\n', source="e").services[0]
    plain = parse_environment(DEFINITION, source="e").services[0]
    observed = _Engine()
    _backend(observed).start(service)
    _backend(observed).start(plain)
    _backend(observed, capture=False).start(service)
    assert "log_statement=all" in observed.runs[0].command
    assert observed.runs[1].command == () and observed.runs[2].command == ()


def test_a_log_that_cannot_be_read_is_never_an_empty_log() -> None:
    started = StartedService("db", handle="c")
    failing = _backend(_Engine(oci.OciUnavailable("gone"), oci.OciUnavailable("gone")))
    assert failing.sql_mark(started) == -1
    assert failing.sql_since(started, 0) is None
    assert _backend(_Engine()).sql_since(started, -1) is None
    assert _backend(_Engine(" 12\n")).sql_mark(started) == 12
