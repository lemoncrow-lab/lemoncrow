"""Migration execution against a schema fixture (Phase 9R item 15, ``migrations.execute``).

The driver replays the trusted base's migrations (or loads a trusted-base
schema fixture) into an in-memory SQLite database inside the sandbox, applies
the migrations the Change adds, rolls them back with their down migrations and
compares the restored schema with the base schema. The host classifies the
executed schema diff and the added migration text: destructive operations
(drop table/column, truncate, type narrowing, renames) and lock-heavy ones
(non-concurrent index on an existing table, NOT NULL without default, type
changes and other table rewrites).

Frameworks, as detected from the trusted base:

* plain SQL directories (golang-migrate ``*.up.sql``/``*.down.sql``, goose,
  dbmate and sql-migrate markers, Flyway ``V``/``U`` files, Prisma
  ``migration.sql``, ordered ``*.sql``): **executed** on SQLite. PostgreSQL
  spellings SQLite cannot parse (``CONCURRENTLY``, ``NOT VALID``, ``ONLY``)
  are normalized for execution; the classification reads the original text.
* Alembic, Django and Rails: a *detected* directory is **not_available**. A
  directory *configured* under ``analyses.migrations`` with its ``framework``
  and a ``[toolchain]`` image pinned by digest in the trusted base's
  ``.lemoncrow/environment.toml`` is **executed for real** in that image, on a
  SQLite file or on a declared database service, by :data:`FRAMEWORK_DRIVER`.
  Migrations are reviewed code there, so the driver is sealed (see
  ``analysis_common.SEAL_PRELUDE``): the schema is read after every process of
  theirs is gone, and only the host decides what the result means. The result
  describes the schema the migrations left; a migration can still do anything
  to its own disposable database.

Lock-heavy rules assume a PostgreSQL-style production engine unless the
spec says ``dialect: sqlite``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any, Final

from lemoncrow_client.environment import Service, Toolchain

from lemoncrow.pro.capabilities.review.analysis_common import (
    MAX_TIMEOUT,
    MIGRATIONS_ENTRY,
    AnalysisSpec,
    Evaluation,
    Finding,
    Observation,
    SideRun,
    bounded,
    build_driver,
    join_path,
    relative_path,
    seal_tampered,
    sealed_problem,
    sha256_hex,
    text_sha16,
    trusted_environment,
    valid_spec_id,
)
from lemoncrow.pro.capabilities.review.contracts import _MIGRATION_PATH, _SQL_FILE_RES
from lemoncrow.pro.environment.services import ServiceSet

DIALECTS: Final[tuple[str, ...]] = ("postgres", "mysql", "sqlite")
FRAMEWORKS: Final[tuple[str, ...]] = ("sql", "alembic", "django", "rails")
_MAX_DISCOVERED: Final[int] = 8
_MAX_BASE_FILES: Final[int] = 600
_SETTINGS = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_FRAMEWORK_SUFFIX: Final[Mapping[str, str]] = {"alembic": ".py", "django": ".py", "rails": ".rb"}
_FRAMEWORK_NEEDS: Final[Mapping[str, str]] = {
    "alembic": "python3, alembic, SQLAlchemy and the database driver",
    "django": "python3, Django, the project's requirements and the database driver",
    "rails": "python3, Ruby, activerecord and the database adapter gem",
}
_FRAMEWORK_HINT: Final[Mapping[str, str]] = {
    "alembic": (
        "Alembic migrations need Python with alembic, SQLAlchemy and a database driver in the sandbox; "
        "this build executes plain SQL migrations only. Point analyses.migrations at a directory of "
        "SQL migrations (for example alembic's --sql output) to execute them."
    ),
    "django": (
        "Django migrations need the project's Django runtime and settings in the sandbox; this build "
        "executes plain SQL migrations only (for example sqlmigrate output configured under analyses.migrations)."
    ),
    "rails": (
        "Rails migrations need Ruby, Rails and a database adapter in the sandbox; this build executes "
        "plain SQL migrations only."
    ),
}

#: The schema dump and version ordering, the only helpers a framework run needs (its command length is bounded).
_SCHEMA_LIB: Final[str] = r"""
import sqlite3

EMULATED = "'lc-emulated-not-null'"


def natural(key):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", key) if t]


def schema(con):
    out = {}
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    rows = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    for kind, name, table, sql in rows:
        if name.startswith("sqlite_"):
            continue
        if kind == "table":
            cols = [
                {"name": r[1], "type": (r[2] or "").upper(), "notnull": bool(r[3]),
                 "default": None if r[4] == EMULATED else r[4], "pk": int(r[5])}
                for r in con.execute("PRAGMA table_xinfo(%s)" % quote(name))
            ]
            out["table:" + name] = {"kind": kind, "name": name, "columns": cols}
        elif kind == "index":
            cols = [r[2] for r in con.execute("PRAGMA index_info(%s)" % quote(name))]
            unique = bool(sql and re.search(r"\bUNIQUE\b", sql, re.I))
            out["index:" + name] = {"kind": kind, "name": name, "table": table, "columns": cols, "unique": unique}
        else:
            out[kind + ":" + name] = {"kind": kind, "name": name, "table": table, "sql": " ".join((sql or "").split())}
    return out
"""

#: Driver helpers shared with ``compat_mixed_version`` (load, schema dump, dialect-normalized execute).
MIGRATION_LIB: Final[str] = _SCHEMA_LIB + r"""

MARKS = (
    (r"^\s*--\s*\+goose\s+up\b", r"^\s*--\s*\+goose\s+down\b", "goose"),
    (r"^\s*--\s*migrate:up\b", r"^\s*--\s*migrate:down\b", "dbmate"),
    (r"^\s*--\s*\+migrate\s+up\b", r"^\s*--\s*\+migrate\s+down\b", "sql-migrate"),
)
PG = (
    (r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY\b", r"CREATE \1INDEX"),
    (r"\bDROP\s+INDEX\s+CONCURRENTLY\b", "DROP INDEX"),
    (r"\bNOT\s+VALID\b", ""),
    (r"\bALTER\s+TABLE\s+ONLY\b", "ALTER TABLE"),
)


def split_marked(body):
    for up, down, name in MARKS:
        u = re.search(up, body, re.I | re.M)
        if u:
            d = re.search(down, body[u.end():], re.I | re.M)
            if d:
                return body[u.end():u.end() + d.start()], body[u.end() + d.end():], name
            return body[u.end():], None, name
    return None


def load(root):
    rows = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if not name.lower().endswith(".sql"):
                continue
            full = os.path.join(base, name)
            body = text(full, 4 << 20)
            if body is None:
                continue
            updown = re.match(r"^(.+)\.(up|down)\.sql$", name, re.I)
            flyway = re.match(r"^([VU])(\d+(?:[._]\d+)*)__(.+)\.sql$", name)
            if name == "migration.sql":
                key, part, layout = rel(base, root), "up", "prisma"
            elif updown:
                key, part, layout = rel(os.path.join(base, updown.group(1)), root), updown.group(2).lower(), "up-down"
            elif flyway:
                key = rel(os.path.join(base, "V%s__%s" % (flyway.group(2), flyway.group(3))), root)
                part, layout = ("up" if flyway.group(1) == "V" else "down"), "flyway"
            else:
                key, part, layout = rel(full[:-4], root), "file", "plain"
            row = rows.setdefault(key, {"id": key, "paths": [], "up": None, "down": None, "layout": layout})
            row["paths"].append(rel(full, CWD))
            if part == "file":
                marked = split_marked(body)
                if marked:
                    row["up"], row["down"], row["layout"] = marked
                else:
                    row["up"] = body
            else:
                row[part] = body
    return [rows[key] for key in sorted(rows, key=natural)]


ADD_NOT_NULL = re.compile(
    r"\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?[A-Za-z_][\w$]*[`\"\]]?\s+[^,;]*?\bNOT\s+NULL\b[^,;]*",
    re.I,
)


def emulate_not_null(match):
    clause = match.group(0)
    if re.search(r"\bDEFAULT\b", clause, re.I):
        return clause
    R.setdefault("emulated", []).append(" ".join(clause.split())[:200])
    return clause + " DEFAULT " + EMULATED


def execute(con, sql, dialect):
    if dialect != "sqlite":
        for pattern, repl in PG:
            sql = re.sub(pattern, repl, sql, flags=re.I)
        # PostgreSQL adds a NOT NULL column without a default to an empty table; SQLite never
        # does. The fixture's tables are empty, so a sentinel default stands in (and is hidden).
        sql = ADD_NOT_NULL.sub(emulate_not_null, sql)
    con.executescript(sql)


"""

_MAIN: Final[str] = r"""def main():
    root = os.path.normpath(os.path.join(CWD, P.get("dir") or "."))
    if not os.path.isdir(root):
        finish(2, "the migration directory %s is missing on this side" % P.get("dir"))
    dialect = str(P.get("dialect") or "postgres")
    base = P.get("base") or {}
    migrations = load(root)
    if len(migrations) > 2000:
        finish(2, "more than 2000 migrations; narrow analyses.migrations")
    for row in migrations:
        known = [p for p in row["paths"] if p in base]
        if not known:
            row["state"] = "added"
        elif any(sha(os.path.join(CWD, p)) != base[p] for p in known):
            row["state"] = "edited"
        else:
            row["state"] = "base"
    con = sqlite3.connect(":memory:", isolation_level=None)
    deny = (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
    con.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY if action in deny else sqlite3.SQLITE_OK)
    con.set_progress_handler(lambda: 1 if time.monotonic() - STARTED > TIMEOUT - 3 else 0, 20000)
    fixture = str(P.get("fixture") or "")
    added = [row for row in migrations if row["state"] == "added"]
    if fixture:
        body = text(os.path.join(CWD, fixture), 8 << 20)
        if body is None:
            finish(2, "the schema fixture %s is missing" % fixture)
        try:
            con.executescript(body)
        except sqlite3.Error as exc:
            finish(2, "the trusted-base schema fixture does not load on SQLite: %s" % exc)
        R["fixture"] = fixture
    else:
        for row in migrations:
            if row["state"] == "added" or row["up"] is None:
                continue
            try:
                execute(con, row["up"], dialect)
            except sqlite3.Error as exc:
                finish(2, "the base migration %s does not apply on the SQLite schema fixture (%s); "
                          "configure a SQLite-compatible schema fixture" % (row["id"], exc))
    before = schema(con)
    R["heavy"]["schema_before.sql"] = "\n".join(con.iterdump()).replace(" DEFAULT " + EMULATED, "")
    statements = []
    con.set_trace_callback(
        lambda sql: statements.append(" ".join(sql.split())[:1000]) if len(statements) < 300 else None
    )
    applied, failed = [], None
    for row in added:
        if row["up"] is None:
            continue
        try:
            execute(con, row["up"], dialect)
            applied.append(row["id"])
        except sqlite3.Error as exc:
            failed = {"id": row["id"], "error": str(exc)[:1000]}
            break
    con.set_trace_callback(None)
    after = schema(con)
    R["heavy"]["schema_after.sql"] = "\n".join(con.iterdump()).replace(" DEFAULT " + EMULATED, "")
    rollback = {"status": "skipped"}
    if failed is None and applied:
        missing = [row["id"] for row in added if row["up"] is not None and row["down"] is None]
        if missing:
            rollback = {"status": "unverifiable", "missing_down": missing[:50]}
        else:
            rollback = {"status": "verified"}
            for row in reversed([r for r in added if r["id"] in applied]):
                try:
                    execute(con, row["down"], dialect)
                except sqlite3.Error as exc:
                    rollback = {"status": "failed", "id": row["id"], "error": str(exc)[:1000]}
                    break
            if rollback["status"] == "verified":
                restored = schema(con)
                if restored != before:
                    rollback = {"status": "mismatch", "restored": restored}
    ordered = [r for r in migrations if r["state"] != "base"] + [r for r in migrations if r["state"] == "base"]
    R["migrations"] = [
        {"id": r["id"], "paths": r["paths"][:4], "layout": r["layout"], "state": r["state"],
         "has_down": r["down"] is not None, "up": (r["up"] or "")[:16000] if r["state"] != "base" else ""}
        for r in ordered[:400]
    ]
    R["counts"] = {"total": len(migrations), "added": len(added),
                   "edited": sum(1 for r in migrations if r["state"] == "edited")}
    R.update({"dialect": dialect, "schema_before": before, "schema_after": after, "applied": applied,
              "failed": failed, "rollback": rollback, "statements": statements})
    finish(1 if failed or rollback["status"] in ("failed", "mismatch") else 0)
"""

DRIVER: Final[str] = build_driver(MIGRATION_LIB + _MAIN)

# Reviewed code (env.py, settings, migrations) runs only in ``sealed_run`` children. What the host trusts is read
# after each step's processes are gone: by this driver from the SQLite file, or by the framework's own database
# library started isolated (``-I``, outside the tree) from a server.
_ALEMBIC_STEP: Final[str] = r"""
import sys
from alembic import command
from alembic.config import Config
op, ini, url = sys.argv[1:4]
cfg = Config(ini)
cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
if op == "up":
    command.upgrade(cfg, "heads")
elif op == "down":
    command.downgrade(cfg, sys.argv[4])
else:
    command.upgrade(cfg, sys.argv[4] + ":heads", sql=True)
"""
_DJANGO_STEP: Final[str] = r"""
import os, sys
import django
from django.apps import apps
from django.core.management import call_command
django.setup()
op, directory = sys.argv[1:3]
owner = os.path.realpath(os.path.dirname(directory))
label = next((c.label for c in apps.get_app_configs() if os.path.realpath(c.path) == owner), None)
if op == "up":
    call_command("migrate", interactive=False, verbosity=0)
elif label is None:
    sys.exit("no installed Django app owns " + directory)
elif op == "down":
    call_command("migrate", label, sys.argv[3], interactive=False, verbosity=0)
else:
    call_command("sqlmigrate", label, sys.argv[3])
"""
_DJANGO_SETTINGS: Final[str] = r"""
import json, os
from importlib import import_module
_base = import_module(os.environ["LC_BASE_SETTINGS"])
globals().update({k: getattr(_base, k) for k in dir(_base) if k.isupper()})
DATABASES = {"default": json.loads(os.environ["LC_DB"])}
"""
# lc-debt: Rails runs load the image's activerecord, not the project's Gemfile or app; boot through bundler to widen it.
_RAILS_STEP: Final[str] = r"""
require "active_record"
op, dir, url, target = ARGV
ActiveRecord::Migration.verbose = false
ActiveRecord::Base.establish_connection(url)
context = ActiveRecord::MigrationContext.new(dir)
op == "up" ? context.migrate : context.migrate(Integer(target))
"""
_SQLALCHEMY_READ: Final[str] = r"""
import json, sys
import sqlalchemy as sa
with sa.create_engine(sys.argv[1]).connect() as con:
    rows = [[[None if v is None else str(v) for v in r] for r in con.execute(sa.text(q))] for q in json.loads(sys.argv[2])]
print("LCROWS:" + json.dumps(rows).encode().hex())
"""
_DJANGO_READ: Final[str] = r"""
import json, os, sys
import django
from django.conf import settings
settings.configure(DATABASES={"default": json.loads(os.environ["LC_DB"])})
django.setup()
from django.db import connection
out = []
with connection.cursor() as cur:
    for q in json.loads(sys.argv[2]):
        cur.execute(q)
        out.append([[None if v is None else str(v) for v in r] for r in cur.fetchall()])
print("LCROWS:" + json.dumps(out).encode().hex())
"""
_RAILS_READ: Final[str] = r"""
require "active_record"
require "json"
ActiveRecord::Base.establish_connection(ARGV[0])
rows = JSON.parse(ARGV[1]).map do |q|
  ActiveRecord::Base.connection.select_rows(q).map { |r| r.map { |v| v.nil? ? nil : v.to_s } }
end
puts "LCROWS:" + JSON.generate(rows).unpack1("H*")
"""
_PG_READS: Final[tuple[str, str]] = (
    "SELECT c.table_name, c.column_name, upper(c.data_type), c.is_nullable, c.column_default, "
    "coalesce(k.ordinal_position, 0) FROM information_schema.columns c LEFT JOIN "
    "(SELECT u.table_name, u.column_name, u.ordinal_position FROM information_schema.table_constraints t "
    "JOIN information_schema.key_column_usage u USING (constraint_name, table_schema, table_name) "
    "WHERE t.constraint_type = 'PRIMARY KEY' AND t.table_schema = current_schema()) k "
    "USING (table_name, column_name) WHERE c.table_schema = current_schema() "
    "ORDER BY c.table_name, c.ordinal_position",
    "SELECT i.relname, t.relname, CASE WHEN x.indisunique THEN 1 ELSE 0 END, array_to_string(ARRAY(SELECT a.attname "
    "FROM unnest(x.indkey) WITH ORDINALITY k(n, o) JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.n "
    "ORDER BY k.o), ',') FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = current_schema() AND NOT x.indisprimary",
)
_MYSQL_READS: Final[tuple[str, str]] = (
    "SELECT table_name, column_name, upper(data_type), is_nullable, column_default, "
    "IF(column_key = 'PRI', ordinal_position, 0) FROM information_schema.columns "
    "WHERE table_schema = DATABASE() ORDER BY table_name, ordinal_position",
    "SELECT index_name, table_name, IF(non_unique = 0, 1, 0), group_concat(column_name ORDER BY seq_in_index) "
    "FROM information_schema.statistics WHERE table_schema = DATABASE() AND index_name <> 'PRIMARY' "
    "GROUP BY index_name, table_name, non_unique",
)

_FRAMEWORK_MAIN: Final[str] = (
    "STEP = "
    + repr({"alembic": _ALEMBIC_STEP, "django": _DJANGO_STEP, "rails": _RAILS_STEP})
    + "\nREAD = "
    + repr({"alembic": _SQLALCHEMY_READ, "django": _DJANGO_READ, "rails": _RAILS_READ})
    + "\nDJANGO_SETTINGS = "
    + repr(_DJANGO_SETTINGS)
    + "\nPG_READS = "
    + repr(_PG_READS)
    + "\nMYSQL_READS = "
    + repr(_MYSQL_READS)
    + r"""
FW = str(P.get("framework") or "")
SERVICE = str(P.get("service") or "")
BOOK = {"alembic": ("alembic_version", "version_num"), "django": ("django_migrations", "name"),
        "rails": ("schema_migrations", "version")}[FW]
IGNORED = {"alembic_version", "django_migrations", "schema_migrations", "ar_internal_metadata"}
SQLITE = WORK + "/framework.sqlite3"
SUFFIX = ".rb" if FW == "rails" else ".py"
PY = which("python3") or sys.executable
# Where the image installed its gems: set by the trusted image, dropped by the driver's allowlist.
GEMS = {k: os.environ[k] for k in ("GEM_HOME", "GEM_PATH") if k in os.environ}


def target_url():
    if not SERVICE:
        return "sqlite3:" + SQLITE if FW == "rails" else "sqlite:///" + SQLITE
    host = os.environ.get("LC_SERVICE_%s_HOST" % re.sub(r"[-.]", "_", SERVICE).upper())
    if not host:
        finish(2, "the database service %s is not reachable from this run" % SERVICE)
    return str(P.get("database_url") or "").replace("{host}", host)


URL = target_url()
ENGINE = "sqlite" if not SERVICE else "mysql" if URL.startswith(("mysql", "mariadb", "trilogy")) else "postgresql"


def django_database():
    from urllib.parse import unquote, urlsplit
    if not SERVICE:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": SQLITE}
    u = urlsplit(URL)
    backend = "mysql" if ENGINE == "mysql" else "postgresql"
    return {"ENGINE": "django.db.backends." + backend, "NAME": u.path.lstrip("/"), "USER": unquote(u.username or ""),
            "PASSWORD": unquote(u.password or ""), "HOST": u.hostname or "", "PORT": str(u.port or "")}


def step_env(src):
    return {"PYTHONPATH": src + os.pathsep + WORK + "/lcs", "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": URL,
            "LC_DB": json.dumps(django_database()), "DJANGO_SETTINGS_MODULE": "lc_review_settings",
            "LC_BASE_SETTINGS": str(P.get("settings") or ""), "RAILS_ENV": "test", **GEMS}


def step(src, migdir, op, arg=""):
    if FW == "rails":
        argv = ["ruby", "-e", STEP[FW], op, migdir, URL, arg]
    elif FW == "alembic":
        argv = [PY, "-c", STEP[FW], op, str(P.get("config") or "alembic.ini"), URL] + ([arg] if arg else [])
    else:
        argv = [PY, "-c", STEP[FW], op, migdir] + ([arg] if arg else [])
    return sealed_run(argv, src, extra=step_env(src))


def read_server(src, queries):
    # No reviewed code is alive here; isolated, outside the tree, nothing of the project loads.
    data = json.dumps(queries)
    if FW == "rails":
        argv = ["ruby", "-e", READ[FW], URL, data]
    else:
        argv = [PY, "-I", "-c", READ[FW], URL, data]
    code, out = sealed_run(argv, WORK, extra={"LC_DB": json.dumps(django_database()), **GEMS})
    marked = [line[7:] for line in (out or "").splitlines() if line.startswith("LCROWS:")]
    if code != 0 or not marked:
        finish(3, "the schema could not be read back: %s" % (out or "")[-500:])
    return json.loads(bytes.fromhex(marked[-1]).decode("utf-8"))


def dump(src):
    # (schema, recorded versions, whether the framework's own table is there)
    if not SERVICE:
        if not os.path.isfile(SQLITE):
            return {}, [], False
        con = sqlite3.connect("file:%s?mode=ro" % SQLITE, uri=True)
        try:
            con.execute("PRAGMA trusted_schema=OFF")
            out = {k: v for k, v in schema(con).items()
                   if v.get("name") not in IGNORED and v.get("table") not in IGNORED}
            found = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (BOOK[0],)).fetchone()
            versions = [str(r[0]) for r in con.execute('SELECT "%s" FROM "%s"' % (BOOK[1], BOOK[0]))] if found else []
        finally:
            con.close()
        return out, versions, bool(found)
    columns, indexes = read_server(src, list(MYSQL_READS if ENGINE == "mysql" else PG_READS))
    out, found = {}, False
    for table, name, kind, nullable, default, pk in columns:
        if table == BOOK[0]:
            found = True
        if table in IGNORED:
            continue
        row = out.setdefault("table:" + table, {"kind": "table", "name": table, "columns": []})
        row["columns"].append({"name": name, "type": kind, "notnull": nullable == "NO", "default": default,
                               "pk": int(pk or 0)})
    for name, table, unique, cols in indexes:
        if table not in IGNORED:
            out["index:" + name] = {"kind": "index", "name": name, "table": table,
                                    "columns": (cols or "").split(","), "unique": str(unique) == "1"}
    versions = []
    if found:
        versions = [str(r[0]) for r in read_server(src, ["SELECT %s FROM %s" % (BOOK[1], BOOK[0])])[0]]
    return out, versions, found


def rollback_target(versions, base_names):
    if FW == "alembic":
        if len(versions) > 1:
            return None
        return versions[0] if versions else "base"
    if FW == "django":
        return base_names[-1] if base_names else "zero"
    return str(max([int(v) for v in versions] or [0]))


def main():
    seal_start()
    if FW == "rails":
        probe = ["ruby", "-e", 'require "active_record"']
    else:
        probe = [PY, "-I", "-c", "import alembic, sqlalchemy" if FW == "alembic" else "import django"]
    code, out = sealed_run(probe, WORK, extra=GEMS)
    if code != 0:
        finish(2, "the toolchain image cannot run %s: %s" % (FW, (out or "not installed")[-300:]))
    src = copy_root()
    migdir = os.path.join(src, str(P.get("dir") or "."))
    if not os.path.isdir(migdir):
        finish(2, "the migration directory %s is missing on this side" % P.get("dir"))
    os.makedirs(WORK + "/lcs", exist_ok=True)
    with open(WORK + "/lcs/lc_review_settings.py", "w") as handle:
        handle.write(DJANGO_SETTINGS)
    base = P.get("base") or {}
    rows = []
    for name in sorted(os.listdir(migdir), key=natural):
        full = os.path.join(migdir, name)
        if not name.endswith(SUFFIX) or name == "__init__.py" or not os.path.isfile(full):
            continue
        path = rel(full, src)
        state = "added" if path not in base else "edited" if sha(full) != base[path] else "base"
        rows.append({"id": name[: -len(SUFFIX)], "paths": [path], "layout": FW, "state": state, "full": full,
                     "up": "", "down": None})
    added = [r for r in rows if r["state"] == "added"]
    held = WORK + "/held"
    os.makedirs(held, exist_ok=True)
    for row in added:
        shutil.move(row["full"], os.path.join(held, os.path.basename(row["full"])))
    code, out = step(src, migdir, "up")
    if code is None:
        finish(2, out)
    if code != 0:
        finish(2, "the trusted base's %s migrations do not apply here: %s" % (FW, (out or "")[-800:]))
    before, versions, found = dump(src)
    if not found:
        finish(2, "%s ran but left no %s table in the review database: the project does not use the database "
                  "this analysis gave it, so nothing was verified" % (FW, BOOK[0]))
    R["heavy"]["schema_before.txt"] = json.dumps(before, indent=1, sort_keys=True)
    for row in added:
        shutil.move(os.path.join(held, os.path.basename(row["full"])), row["full"])
    base_names = [r["id"] for r in rows if r["state"] != "added"]
    target = rollback_target(versions, base_names)
    if added and FW == "alembic" and target is not None:
        code, out = step(src, migdir, "sql", target)
        if code == 0:
            added[0]["up"] = (out or "")[:16000]
    elif FW == "django":
        for row in added:
            code, out = step(src, migdir, "sql", row["id"])
            if code == 0:
                row["up"] = (out or "")[:16000]
    failed, applied = None, []
    code, out = step(src, migdir, "up")
    if code != 0:
        failed = {"id": added[0]["id"] if added else str(P.get("dir")), "error": (out or "")[-1000:]}
    elif added:
        applied = [r["id"] for r in added]
    after, _versions, _found = dump(src)
    R["heavy"]["schema_after.txt"] = json.dumps(after, indent=1, sort_keys=True)
    rollback = {"status": "skipped"}
    if failed is None and applied:
        if target is None:
            rollback = {"status": "unverifiable", "missing_down": ["several base heads"]}
        else:
            code, out = step(src, migdir, "down", target)
            if code != 0:
                rollback = {"status": "failed", "id": applied[-1], "error": (out or "")[-1000:]}
            else:
                restored, _versions, _found = dump(src)
                rollback = {"status": "verified"}
                if restored != before:
                    rollback = {"status": "mismatch", "restored": restored}
    R["migrations"] = [{"id": r["id"], "paths": r["paths"], "layout": r["layout"], "state": r["state"],
                        "has_down": True, "up": r["up"]} for r in rows[:400] if r["state"] != "base"]
    R["counts"] = {"total": len(rows), "added": len(added),
                   "edited": sum(1 for r in rows if r["state"] == "edited")}
    R.update({"framework": FW, "engine": ENGINE, "dialect": str(P.get("dialect") or "postgres"),
              "schema_before": before, "schema_after": after, "applied": applied, "failed": failed,
              "rollback": rollback, "statements": []})
    finish(0)
"""
)

#: The framework runs reviewed code: sealed, and only accepted by :func:`inspect_framework_report`.
FRAMEWORK_DRIVER: Final[str] = build_driver(_FRAMEWORK_MAIN, sealed=True, shared=_SCHEMA_LIB)


# --------------------------------------------------------------------------- #
# trusted-base specs
# --------------------------------------------------------------------------- #


def _slug(path: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", path.strip("/")).strip("-.") or "root"
    return text[-64:].lstrip("-.") or "root"


def _common_root(paths: Sequence[str]) -> str:
    parts = [PurePosixPath(path).parts for path in paths if path not in {"", "."}]
    if not parts or len(parts) != len(paths):
        return "."
    common: list[str] = []
    for column in zip(*parts, strict=False):
        if len(set(column)) != 1:
            break
        common.append(column[0])
    return "/".join(common) or "."


def _sql_spec(
    spec_id: str,
    directory: str,
    *,
    origin: str,
    source: str,
    dialect: str = "postgres",
    fixture: str = "",
    timeout: int = 120,
    title: str = "",
) -> AnalysisSpec:
    root = _common_root([directory, str(PurePosixPath(fixture).parent)]) if fixture else directory

    def inside(path: str) -> str:
        return path if root == "." else str(PurePosixPath(path).relative_to(root)) or "."

    return AnalysisSpec(
        entry_id=MIGRATIONS_ENTRY,
        id=spec_id,
        tool="sql",
        root=root,
        title=title or f"SQL migrations · {directory}",
        origin=origin,  # type: ignore[arg-type]
        source=source,
        options={"dir": inside(directory), "dialect": dialect, "fixture": inside(fixture) if fixture else ""},
        timeout=timeout,
        trusted_inputs=(fixture,) if fixture else (),
    )


def _unavailable(spec_id: str, framework: str, directory: str, *, origin: str, source: str) -> AnalysisSpec:
    return AnalysisSpec(
        entry_id=MIGRATIONS_ENTRY,
        id=spec_id,
        tool=framework,
        root=directory,
        title=f"{framework.capitalize()} migrations · {directory}",
        origin=origin,  # type: ignore[arg-type]
        source=source,
        options={"dir": "."},
        executable=False,
        reason=f"{framework.capitalize()} migrations cannot execute in this build's sandbox",
        setup_hint=_setup_hint(framework),
    )


def _setup_hint(framework: str) -> str:
    return (
        f"{_FRAMEWORK_HINT[framework]} To run them for real, configure analyses.migrations with framework: "
        f"{framework} and give the trusted base's .lemoncrow/environment.toml a [toolchain] image pinned by digest "
        f"holding {_FRAMEWORK_NEEDS[framework]}; the executor needs a rootless container engine."
    )


def _framework_spec(
    spec_id: str, framework: str, directory: str, row: Mapping[str, Any], *, where: str, timeout: int, errors: list[str]
) -> AnalysisSpec | None:
    """A configured framework run, not executable until :func:`attach_environment` finds its image."""

    root = relative_path(row.get("root") or ".")
    service = str(row.get("service") or "")
    url = str(row.get("database_url") or "")
    settings = str(row.get("settings") or "")
    config = relative_path(row.get("config") or "alembic.ini", allow_root=False)
    dialect = str(row.get("dialect") or ("postgres" if service else "sqlite"))
    inside = root is not None and (root == "." or directory == root or directory.startswith(root.rstrip("/") + "/"))
    if root is None or not inside:
        errors.append(f"{where}.root must be a repository-relative directory holding dir")
    elif dialect not in DIALECTS or (service and dialect == "sqlite"):
        errors.append(f"{where}.dialect must name the service's engine")
    elif bool(service) != bool(url) or (url and ("{host}" not in url or any(c.isspace() for c in url))):
        errors.append(f"{where}: service and database_url go together, and database_url must contain {{host}}")
    elif framework == "django" and not _SETTINGS.fullmatch(settings):
        errors.append(f"{where}.settings must name the project's Django settings module")
    elif config is None:
        errors.append(f"{where}.config must be a repository-relative file")
    else:
        spec = _unavailable(spec_id, framework, root, origin="configured", source=where)
        inner = (
            "." if directory == root else str(PurePosixPath(directory).relative_to(root)) if root != "." else directory
        )
        return replace(
            spec,
            title=str(row.get("title") or "")[:200] or f"{framework.capitalize()} migrations · {directory}",
            timeout=timeout,
            options={
                "dir": inner,
                "dialect": dialect,
                "service": service,
                "database_url": url,
                "settings": settings,
                "config": config,
            },
        )
    return None


def attach_environment(spec: AnalysisSpec, read_base: Callable[[str], str | None]) -> AnalysisSpec:
    """A configured framework spec becomes executable once the trusted base pins what it runs in."""

    if spec.tool == "sql" or spec.origin != "configured":
        return spec
    definition, problem = trusted_environment(read_base)
    toolchain = definition.toolchain if definition is not None else None
    if toolchain is None or not toolchain.digest:
        why = problem or "the trusted base's environment.toml has no [toolchain] image pinned by digest"
        return replace(spec, reason=f"{spec.reason}: {why}")
    options: dict[str, Any] = {**spec.options, "toolchain": toolchain.image}
    name = str(spec.options.get("service") or "")
    if name:
        service = definition.service(name)
        if service is None or not service.digest:
            return replace(spec, reason=f"{spec.reason}: service {name} is not declared with a pinned image")
        options["service_definition"] = {
            "image": service.image,
            "ready": service.ready,
            "env": [list(item) for item in service.env],
            "sql": service.sql,
        }
    return replace(spec, executable=True, reason="", options=options)


def runtime_values(spec: AnalysisSpec) -> dict[str, Any]:
    """The image and services one framework run needs; the host runtime never stands in for them."""

    values: dict[str, Any] = {"toolchain": Toolchain(str(spec.options["toolchain"])), "require_toolchain": True}
    definition = spec.options.get("service_definition")
    if isinstance(definition, Mapping):
        values["services"] = ServiceSet(
            (
                Service(
                    name=str(spec.options["service"]),
                    image=str(definition["image"]),
                    ready=str(definition["ready"]),
                    env=tuple((str(key), str(value)) for key, value in definition.get("env") or ()),
                    sql=str(definition.get("sql") or ""),
                ),
            )
        )
    return values


def parse_specs(rows: Sequence[Any], errors: list[str]) -> list[AnalysisSpec]:
    """``analyses.migrations`` rows; invalid rows are reported, never half-used."""

    specs: list[AnalysisSpec] = []
    for index, row in enumerate(rows):
        where = f"analyses.migrations[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = sorted(
            set(row)
            - {
                "id",
                "title",
                "dir",
                "framework",
                "dialect",
                "fixture",
                "timeout",
                "root",
                "service",
                "database_url",
                "settings",
                "config",
            }
        )
        if unknown:
            errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            continue
        spec_id = row.get("id")
        directory = relative_path(row.get("dir"), allow_root=False)
        framework = str(row.get("framework") or "sql")
        dialect = str(row.get("dialect") or ("sqlite" if framework != "sql" and not row.get("service") else "postgres"))
        fixture_raw = row.get("fixture")
        fixture = relative_path(fixture_raw, allow_root=False) if fixture_raw else ""
        try:
            timeout = max(10, min(int(row.get("timeout") or 120), MAX_TIMEOUT))
        except (TypeError, ValueError):
            errors.append(f"{where}.timeout must be a number of seconds")
            continue
        if not valid_spec_id(spec_id):
            errors.append(f"{where}.id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        elif directory is None:
            errors.append(f"{where}.dir must be a repository-relative directory")
        elif framework not in FRAMEWORKS:
            errors.append(f"{where}.framework must be one of {', '.join(FRAMEWORKS)}")
        elif dialect not in DIALECTS:
            errors.append(f"{where}.dialect must be one of {', '.join(DIALECTS)}")
        elif fixture is None:
            errors.append(f"{where}.fixture must be a repository-relative file")
        elif framework != "sql":
            configured = _framework_spec(
                str(spec_id), framework, directory, row, where=where, timeout=timeout, errors=errors
            )
            if configured is not None:
                specs.append(configured)
        else:
            specs.append(
                _sql_spec(
                    str(spec_id),
                    directory,
                    origin="configured",
                    source=where,
                    dialect=dialect,
                    fixture=fixture,
                    timeout=timeout,
                    title=str(row.get("title") or "")[:200],
                )
            )
    return specs


def discover_specs(paths: Sequence[str], read_base: Callable[[str], str | None]) -> list[AnalysisSpec]:
    """Migration frameworks the trusted base actually contains."""

    del read_base  # file names are enough; contents are read per run
    sql_dirs: dict[str, str] = {}
    other: dict[str, tuple[str, str]] = {}
    for path in paths:
        lower = path.lower()
        parent = str(PurePosixPath(path).parent)
        name = PurePosixPath(path).name
        if lower.endswith(".sql") and _MIGRATION_PATH.search(path):
            if name == "migration.sql" and "/prisma/migrations/" in f"/{lower}":
                parent = str(PurePosixPath(parent).parent)
            sql_dirs.setdefault(parent, path)
        elif lower.endswith(".py") and re.search(r"(?:^|/)alembic/versions/[^/]+\.py$", lower):
            other.setdefault(parent, ("alembic", path))
        elif lower.endswith(".py") and re.search(r"(?:^|/)migrations/0\d{3}_[^/]+\.py$", lower):
            other.setdefault(parent, ("django", path))
        elif lower.endswith(".rb") and re.search(r"(?:^|/)db/migrate/\d+_[^/]+\.rb$", lower):
            other.setdefault(parent, ("rails", path))
    # A nested folder of an already-discovered migration directory is part of it.
    sql_dirs = {
        directory: source
        for directory, source in sql_dirs.items()
        if not any(directory.startswith(outer.rstrip("/") + "/") for outer in sql_dirs if outer != directory)
    }
    specs: list[AnalysisSpec] = []
    seen: set[str] = set()
    for directory in sorted(sql_dirs)[:_MAX_DISCOVERED]:
        spec_id = _slug(directory)
        while spec_id in seen:
            spec_id = f"{spec_id[:60]}-{len(seen)}"
        seen.add(spec_id)
        specs.append(_sql_spec(spec_id, directory, origin="discovered", source=sql_dirs[directory]))
    for directory in sorted(other)[: max(0, _MAX_DISCOVERED - len(specs))]:
        if directory in sql_dirs:
            continue
        framework, source = other[directory]
        spec_id = _slug(directory)
        while spec_id in seen:
            spec_id = f"{spec_id[:60]}-{len(seen)}"
        seen.add(spec_id)
        specs.append(_unavailable(spec_id, framework, directory, origin="discovered", source=source))
    return specs


def attach_base_files(spec: AnalysisSpec, paths: Sequence[str]) -> AnalysisSpec:
    """Record which migration files the trusted base already has (names only; read per run)."""

    if not spec.executable:
        return spec
    suffix = ".sql" if spec.tool == "sql" else _FRAMEWORK_SUFFIX[spec.tool]
    directory = join_path(spec.root, str(spec.options.get("dir") or "."))
    prefix = "" if directory in {"", "."} else directory.rstrip("/") + "/"
    files = sorted(
        path
        for path in paths
        if path.startswith(prefix)
        and path.lower().endswith(suffix)
        and (spec.tool == "sql" or ("/" not in path[len(prefix) :] and not path.endswith("/__init__.py")))
    )
    if len(files) > _MAX_BASE_FILES:
        return replace(
            spec,
            executable=False,
            reason=f"more than {_MAX_BASE_FILES} base migration files",
            setup_hint="Narrow analyses.migrations to one migration directory, or squash old migrations into a schema fixture.",
        )
    return replace(spec, options={**spec.options, "base_files": files})


def payload(spec: AnalysisSpec, *, read_base: Callable[[str], str | None]) -> tuple[dict[str, Any], str]:
    """Driver input: the directory, the dialect and the trusted base's migration file keys."""

    base: dict[str, str] = {}
    for path in spec.options.get("base_files") or ():
        text = read_base(str(path))
        if text is not None:
            base[path if spec.root == "." else str(PurePosixPath(path).relative_to(spec.root))] = text_sha16(text)
    if spec.tool != "sql":
        definition = spec.options.get("service_definition")
        return {
            "framework": spec.tool,
            "dir": spec.options.get("dir") or ".",
            "dialect": spec.options.get("dialect") or "sqlite",
            "service": spec.options.get("service") or "",
            "database_url": spec.options.get("database_url") or "",
            "settings": spec.options.get("settings") or "",
            "config": spec.options.get("config") or "alembic.ini",
            # The run's identity: another image or service is another run.
            "image": spec.options.get("toolchain") or "",
            "service_image": definition.get("image") if isinstance(definition, Mapping) else "",
            "base": base,
        }, ""
    return {
        "dir": spec.options.get("dir") or ".",
        "dialect": spec.options.get("dialect") or "postgres",
        "fixture": spec.options.get("fixture") or "",
        "base": base,
    }, ""


def inspect_framework_report(report: Mapping[str, Any], exit_code: int | None) -> tuple[dict[str, Any] | None, str]:
    """Stage B of a framework run: the report counts only if it is the sealed driver's own."""

    problem = sealed_problem(report, exit_code)
    if problem:
        return None, problem
    out = dict(report)
    tampered = seal_tampered(report)
    if tampered:
        out["tampered"] = tampered
    if out.get("exit") == 0:
        rollback = out.get("rollback") if isinstance(out.get("rollback"), Mapping) else {}
        assert isinstance(rollback, Mapping)
        if out.get("failed") or rollback.get("status") in {"failed", "mismatch"}:
            out["exit"] = 1
    return out, ""


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_IDENT = r"""[`"\[]?([A-Za-z_][\w.]*)[`"\]]?"""
_RENAME = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _IDENT
    + r"\s+RENAME\s+(?:COLUMN\s+)?(?:"
    + _IDENT
    + r"\s+)?TO\s+",
    re.I,
)
_TYPE_CHANGE = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _IDENT
    + r"\s+(?:ALTER|MODIFY)\s+(?:COLUMN\s+)?"
    + _IDENT
    + r"\s+(?:SET\s+DATA\s+)?TYPE\s+([^,;]+)",
    re.I,
)
_ADD_COLUMN = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _IDENT
    + r"\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    + _IDENT
    + r"\s+([^;]*)",
    re.I,
)
_SET_NOT_NULL = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    + _IDENT
    + r"\s+ALTER\s+(?:COLUMN\s+)?"
    + _IDENT
    + r"\s+SET\s+NOT\s+NULL",
    re.I,
)
_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?:"
    + _IDENT
    + r"\s+)?ON\s+(?:ONLY\s+)?"
    + _IDENT,
    re.I,
)
_FOREIGN_KEY = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _IDENT + r"\s+ADD\s+(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY",
    re.I,
)
_REWRITE = re.compile(r"\bVACUUM\s+FULL\b|\bCLUSTER\s+" + _IDENT, re.I)
_UNBOUNDED_DELETE = re.compile(r"^\s*DELETE\s+FROM\s+" + _IDENT + r"\s*$", re.I)
_INT_RANK: Final[Mapping[str, int]] = {
    "TINYINT": 1,
    "SMALLINT": 2,
    "INT2": 2,
    "MEDIUMINT": 3,
    "INT": 4,
    "INTEGER": 4,
    "INT4": 4,
    "BIGINT": 8,
    "INT8": 8,
}
_TEXT_TYPES: Final[frozenset[str]] = frozenset({"TEXT", "CLOB", "LONGTEXT", "MEDIUMTEXT", "STRING"})
_VARCHAR = re.compile(r"^(?:VAR)?CHAR(?:ACTER)?(?:\s+VARYING)?|^N?VARCHAR")


def _type_parts(value: str) -> tuple[str, tuple[int, ...]]:
    match = re.match(r"^\s*([A-Z][A-Z0-9 ]*?)\s*(?:\(([\d\s,]+)\))?\s*$", value.upper())
    if match is None:
        return value.upper().strip(), ()
    args = tuple(int(item) for item in (match.group(2) or "").replace(" ", "").split(",") if item)
    return match.group(1).strip(), args


def is_narrowing(old: str, new: str) -> bool:
    """Whether changing a column from *old* to *new* can lose values (conservative)."""

    (old_name, old_args), (new_name, new_args) = _type_parts(old), _type_parts(new)
    if (old_name, old_args) == (new_name, new_args):
        return False
    if old_name in _INT_RANK and new_name in _INT_RANK:
        return _INT_RANK[new_name] < _INT_RANK[old_name]
    old_text = old_name in _TEXT_TYPES or bool(_VARCHAR.match(old_name))
    new_text = new_name in _TEXT_TYPES or bool(_VARCHAR.match(new_name))
    if old_text and new_text:
        if new_name in _TEXT_TYPES:
            return False
        if old_name in _TEXT_TYPES:
            return True
        return bool(old_args and new_args and new_args[0] < old_args[0]) or bool(not old_args and new_args)
    if old_name in {"NUMERIC", "DECIMAL"} and new_name in {"NUMERIC", "DECIMAL"}:
        return bool(old_args and new_args and (new_args[0] < old_args[0] or new_args[1:] < old_args[1:]))
    if old_name in {"DOUBLE", "DOUBLE PRECISION", "FLOAT8"} and new_name in {"REAL", "FLOAT4", "FLOAT"}:
        return True
    # Any other cross-family change (text to integer, anything to boolean ...) can fail or truncate.
    return old_name != new_name


def _statements(text: str) -> list[str]:
    return [part.strip() for part in _COMMENTS.sub(" ", text).split(";") if part.strip()]


def _tables(schema: Mapping[str, Any]) -> dict[str, dict[str, Mapping[str, Any]]]:
    out: dict[str, dict[str, Mapping[str, Any]]] = {}
    for key, value in schema.items():
        if not key.startswith("table:") or not isinstance(value, Mapping):
            continue
        columns = value.get("columns") or ()
        out[str(value.get("name") or key[6:])] = {
            str(col.get("name")): col for col in columns if isinstance(col, Mapping) and col.get("name")
        }
    return out


def schema_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, list[str]]:
    """Readable executed-schema delta (tables, columns, indexes, views, triggers)."""

    old_tables, new_tables = _tables(before), _tables(after)
    diff: dict[str, list[str]] = {
        "tables_added": sorted(set(new_tables) - set(old_tables)),
        "tables_removed": sorted(set(old_tables) - set(new_tables)),
        "columns_added": [],
        "columns_removed": [],
        "columns_changed": [],
        "indexes_added": [],
        "indexes_removed": [],
        "other_changed": [],
    }
    for table in sorted(set(old_tables) & set(new_tables)):
        old_cols, new_cols = old_tables[table], new_tables[table]
        diff["columns_added"] += [
            f"{table}.{name} {new_cols[name].get('type') or ''}".strip()
            for name in sorted(set(new_cols) - set(old_cols))
        ]
        diff["columns_removed"] += [f"{table}.{name}" for name in sorted(set(old_cols) - set(new_cols))]
        for name in sorted(set(old_cols) & set(new_cols)):
            old, new = old_cols[name], new_cols[name]
            changes = [
                f"{label} {old.get(key)!s} -> {new.get(key)!s}"
                for key, label in (("type", "type"), ("notnull", "not null"), ("default", "default"))
                if old.get(key) != new.get(key)
            ]
            if changes:
                diff["columns_changed"].append(f"{table}.{name}: " + ", ".join(changes))
    for key in sorted(set(before) | set(after)):
        if key.startswith("table:"):
            continue
        if key.startswith("index:"):
            if key not in before:
                diff["indexes_added"].append(key[6:])
            elif key not in after:
                diff["indexes_removed"].append(key[6:])
            elif before[key] != after[key]:
                diff["other_changed"].append(key)
        elif before.get(key) != after.get(key):
            diff["other_changed"].append(key)
    return diff


def _flag(
    found: dict[tuple[str, str], Finding],
    spec: AnalysisSpec,
    rule: str,
    target: str,
    title: str,
    summary: str,
    *,
    category: str,
    severity: str,
    path: str = "",
    evidence: str = "",
) -> None:
    key = (rule, target.lower())
    if key in found:
        return
    found[key] = Finding(
        rule=rule,
        category=category,
        subject=f"{spec.id}::{rule}::{target}",
        title=f"{title} · {target}"[:200],
        summary=bounded(summary),
        severity=severity,  # type: ignore[arg-type]
        critical=category == "destructive",
        paths=(path,) if path else (),
        facts=(
            ("Operation", rule.replace("_", " ")),
            ("Class", "destructive" if category == "destructive" else "lock-heavy"),
            ("Object", target),
            *((("Evidence", evidence),) if evidence else ()),
        ),
        head=Observation(status=category, summary=bounded(summary, 200), digest=sha256_hex([rule, target])),
    )


# SQLite errors that mean "SQLite cannot express this", not "this migration is wrong".
_ENGINE_LIMITS: Final[tuple[str, ...]] = (
    "syntax error",
    "cannot add a",
    "not supported",
    "unsupported",
    "cannot drop",
)


def _engine_limited(error: object, dialect: str) -> bool:
    """A failure SQLite emulation cannot attribute to the migration (PostgreSQL/MySQL dialects only)."""

    text = str(error or "").lower()
    return dialect != "sqlite" and any(marker in text for marker in _ENGINE_LIMITS)


_DOWN_FILE = re.compile(r"(?:^|[._/-])down\.sql$|(?:^|/)U\d[^/]*__[^/]*\.sql$", re.I)


def _row_path(spec: AnalysisSpec, row: Mapping[str, Any]) -> str:
    """The repository path of a migration's up file (never its down file)."""

    paths = [str(item) for item in row.get("paths") or () if str(item)]
    chosen = next((item for item in paths if not _DOWN_FILE.search(item)), paths[0] if paths else "")
    return join_path(spec.root, chosen) if chosen else ""


def classify(
    spec: AnalysisSpec,
    report: Mapping[str, Any],
) -> list[Finding]:
    """Destructive and lock-heavy operations: executed schema diff plus the added migration text."""

    dialect = str(report.get("dialect") or spec.options.get("dialect") or "postgres")
    before = report.get("schema_before") if isinstance(report.get("schema_before"), Mapping) else {}
    after = report.get("schema_after") if isinstance(report.get("schema_after"), Mapping) else {}
    assert isinstance(before, Mapping) and isinstance(after, Mapping)
    existing = set(_tables(before))
    found: dict[tuple[str, str], Finding] = {}
    for row in report.get("migrations") or ():
        if not isinstance(row, Mapping) or row.get("state") not in {"added", "edited"}:
            continue
        path = _row_path(spec, row)
        for statement in _statements(str(row.get("up") or "")):
            snippet = bounded(statement, 160)
            for op, pattern in _SQL_FILE_RES:
                if op == "create":
                    continue
                for hit in pattern.finditer(statement):
                    table = hit.group(1)
                    if op == "drop_column":
                        target = f"{table}.{hit.group(2)}"
                        _flag(
                            found,
                            spec,
                            "drop_column",
                            target,
                            "Drops a column",
                            f"Drops column {target}; existing data in it is lost and readers of it break.",
                            category="destructive",
                            severity="high",
                            path=path,
                            evidence=snippet,
                        )
                    elif op == "drop_table":
                        _flag(
                            found,
                            spec,
                            "drop_table",
                            table,
                            "Drops a table",
                            f"Drops table {table} and all of its rows.",
                            category="destructive",
                            severity="high",
                            path=path,
                            evidence=snippet,
                        )
                    elif op == "truncate":
                        _flag(
                            found,
                            spec,
                            "truncate",
                            table,
                            "Truncates a table",
                            f"Deletes every row of {table}.",
                            category="destructive",
                            severity="high",
                            path=path,
                            evidence=snippet,
                        )
            if (match := _UNBOUNDED_DELETE.match(statement)) is not None:
                _flag(
                    found,
                    spec,
                    "delete_all_rows",
                    match.group(1),
                    "Deletes every row",
                    f"DELETE without WHERE removes every row of {match.group(1)}.",
                    category="destructive",
                    severity="high",
                    path=path,
                    evidence=snippet,
                )
            if (match := _RENAME.search(statement)) is not None:
                target = f"{match.group(1)}.{match.group(2)}" if match.group(2) else match.group(1)
                _flag(
                    found,
                    spec,
                    "rename",
                    target,
                    "Renames a schema object",
                    f"Renames {target}; code still reading the old name breaks during a rolling deploy.",
                    category="destructive",
                    severity="medium",
                    path=path,
                    evidence=snippet,
                )
            if (match := _TYPE_CHANGE.search(statement)) is not None:
                target = f"{match.group(1)}.{match.group(2)}"
                _flag(
                    found,
                    spec,
                    "type_change",
                    target,
                    "Changes a column type",
                    f"Changes the type of {target} to {match.group(3).strip()}; the table is rewritten under an exclusive lock.",
                    category="lock_heavy",
                    severity="medium",
                    path=path,
                    evidence=snippet,
                )
            if (match := _ADD_COLUMN.search(statement)) is not None:
                clause = match.group(3)
                if re.search(r"\bNOT\s+NULL\b", clause, re.I) and not re.search(r"\bDEFAULT\b", clause, re.I):
                    target = f"{match.group(1)}.{match.group(2)}"
                    _flag(
                        found,
                        spec,
                        "not_null_without_default",
                        target,
                        "Adds a NOT NULL column without a default",
                        f"Adds {target} as NOT NULL with no default: it fails on a non-empty table or forces a backfill under lock.",
                        category="lock_heavy",
                        severity="high",
                        path=path,
                        evidence=snippet,
                    )
            if (match := _SET_NOT_NULL.search(statement)) is not None:
                target = f"{match.group(1)}.{match.group(2)}"
                _flag(
                    found,
                    spec,
                    "set_not_null",
                    target,
                    "Sets NOT NULL on an existing column",
                    f"SET NOT NULL on {target} scans the whole table under an exclusive lock.",
                    category="lock_heavy",
                    severity="medium",
                    path=path,
                    evidence=snippet,
                )
            if dialect != "sqlite" and (match := _INDEX.search(statement)) is not None and not match.group(1):
                table = match.group(3)
                if table in existing:
                    _flag(
                        found,
                        spec,
                        "non_concurrent_index",
                        table,
                        "Builds an index without CONCURRENTLY",
                        f"CREATE INDEX on existing table {table} without CONCURRENTLY blocks writes for the whole build (long on large tables).",
                        category="lock_heavy",
                        severity="medium",
                        path=path,
                        evidence=snippet,
                    )
            if (
                dialect == "postgres"
                and (match := _FOREIGN_KEY.search(statement)) is not None
                and not re.search(r"\bNOT\s+VALID\b", statement, re.I)
            ):
                _flag(
                    found,
                    spec,
                    "foreign_key_validation",
                    match.group(1),
                    "Adds a validated foreign key",
                    f"Adding a foreign key to {match.group(1)} without NOT VALID validates every row under lock.",
                    category="lock_heavy",
                    severity="low",
                    path=path,
                    evidence=snippet,
                )
            if (match := _REWRITE.search(statement)) is not None:
                _flag(
                    found,
                    spec,
                    "table_rewrite",
                    match.group(1) or "database",
                    "Rewrites tables",
                    "VACUUM FULL / CLUSTER rewrites tables under an exclusive lock.",
                    category="lock_heavy",
                    severity="medium",
                    path=path,
                    evidence=snippet,
                )
    # The executed schema diff: what actually happened on the fixture.
    old_tables, new_tables = _tables(before), _tables(after)
    for table in sorted(set(old_tables) - set(new_tables)):
        _flag(
            found,
            spec,
            "drop_table",
            table,
            "Drops a table",
            f"Table {table} no longer exists after the migrations ran.",
            category="destructive",
            severity="high",
        )
    for table in sorted(set(old_tables) & set(new_tables)):
        for name in sorted(set(old_tables[table]) - set(new_tables[table])):
            _flag(
                found,
                spec,
                "drop_column",
                f"{table}.{name}",
                "Drops a column",
                f"Column {table}.{name} no longer exists after the migrations ran.",
                category="destructive",
                severity="high",
            )
        for name in sorted(set(old_tables[table]) & set(new_tables[table])):
            old, new = old_tables[table][name], new_tables[table][name]
            target = f"{table}.{name}"
            if old.get("type") != new.get("type"):
                if is_narrowing(str(old.get("type") or ""), str(new.get("type") or "")):
                    _flag(
                        found,
                        spec,
                        "type_narrowing",
                        target,
                        "Narrows a column type",
                        f"{target} changes from {old.get('type')} to {new.get('type')}; existing values can be truncated or rejected.",
                        category="destructive",
                        severity="high",
                    )
                else:
                    _flag(
                        found,
                        spec,
                        "type_change",
                        target,
                        "Changes a column type",
                        f"{target} changes from {old.get('type')} to {new.get('type')}; the table is rewritten under lock.",
                        category="lock_heavy",
                        severity="medium",
                    )
            if not old.get("notnull") and new.get("notnull"):
                if new.get("default") is None:
                    _flag(
                        found,
                        spec,
                        "not_null_without_default",
                        target,
                        "Makes a column NOT NULL without a default",
                        f"{target} becomes NOT NULL without a default: existing NULLs fail it, and the check scans the table under lock.",
                        category="lock_heavy",
                        severity="high",
                    )
                else:
                    _flag(
                        found,
                        spec,
                        "set_not_null",
                        target,
                        "Sets NOT NULL on an existing column",
                        f"{target} becomes NOT NULL; the check scans the whole table under lock.",
                        category="lock_heavy",
                        severity="medium",
                    )
    return list(found.values())


def _schema_text(schema: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in sorted(schema):
        value = schema[key]
        if not isinstance(value, Mapping):
            continue
        if value.get("kind") == "table":
            lines.append(f"table {value.get('name')}")
            for col in value.get("columns") or ():
                if isinstance(col, Mapping):
                    null = " NOT NULL" if col.get("notnull") else ""
                    default = f" DEFAULT {col.get('default')}" if col.get("default") is not None else ""
                    lines.append(f"  {col.get('name')} {col.get('type') or ''}{null}{default}".rstrip())
        elif value.get("kind") == "index":
            unique = "unique " if value.get("unique") else ""
            lines.append(
                f"{unique}index {value.get('name')} on {value.get('table')} ({', '.join(map(str, value.get('columns') or ()))})"
            )
        else:
            lines.append(f"{value.get('kind')} {value.get('name')}: {value.get('sql')}")
    return lines


def evaluate(spec: AnalysisSpec, head: SideRun | None, base: SideRun | None) -> Evaluation:
    """Verdict and findings of one migration spec (head side; the base is replayed inside it)."""

    del base  # the head run replays the trusted base itself (or loads its fixture)
    if head is None or head.state == "not_run":
        return Evaluation("NOT_RUN", [head.reason if head is not None else "the head side did not run"])
    if head.state == "unavailable":
        return Evaluation(
            "NOT_RUN",
            [head.reason or "migrations could not execute"],
            setup_hint=spec.setup_hint
            or "Add analyses.migrations with a SQLite-compatible schema fixture to the trusted-base .lemoncrow/review.yaml.",
        )
    if head.state == "error":
        return Evaluation("UNKNOWN", [head.reason or "the migration driver returned no usable result"])
    report = head.report
    before = report.get("schema_before") if isinstance(report.get("schema_before"), Mapping) else {}
    after = report.get("schema_after") if isinstance(report.get("schema_after"), Mapping) else {}
    assert isinstance(before, Mapping) and isinstance(after, Mapping)
    rollback = report.get("rollback") if isinstance(report.get("rollback"), Mapping) else {}
    assert isinstance(rollback, Mapping)
    failed = report.get("failed") if isinstance(report.get("failed"), Mapping) else None
    applied = [str(item) for item in report.get("applied") or ()]
    edited = [
        row for row in report.get("migrations") or () if isinstance(row, Mapping) and row.get("state") == "edited"
    ]
    findings = classify(spec, report)
    diff = schema_diff(before, after)
    status = "PASS"
    reasons: list[str] = []
    fixture = str(report.get("fixture") or "")
    framework = spec.tool != "sql"
    engine = str(report.get("engine") or "sqlite")
    origin = (
        f"the trusted base's {spec.tool} migrations on {engine}"
        if framework
        else f"schema fixture {fixture}" if fixture else "the trusted base's migrations replayed on SQLite"
    )
    dialect = str(report.get("dialect") or "postgres")
    tampered = str(report.get("tampered") or "")
    if tampered:
        status = "UNKNOWN"
        reasons.append(f"the migration run interfered with the analysis ({tampered}); its result is not trusted")
    if failed is not None:
        limited = not framework and _engine_limited(failed.get("error"), dialect)
        if limited:
            # SQLite cannot express this statement; that says nothing about the production engine.
            status = "UNKNOWN"
            reasons.append(
                f"migration {failed.get('id')} could not be emulated on SQLite ({failed.get('error')}); "
                "its effect on the schema is unverified"
            )
        else:
            status = "FAIL"
            reasons.append(f"migration {failed.get('id')} failed to apply: {failed.get('error')}")
        findings.append(
            Finding(
                rule="not_emulated" if limited else "apply_failed",
                category="apply",
                subject=f"{spec.id}::apply",
                title=(
                    f"Migration not executable on the SQLite emulation · {failed.get('id')}"
                    if limited
                    else f"Migration does not apply · {failed.get('id')}"
                )[:200],
                summary=bounded(f"Applying {failed.get('id')} on {origin} failed: {failed.get('error')}"),
                severity="medium" if limited else "high",
                critical=not limited,
                facts=(("Migration", str(failed.get("id"))), ("Error", bounded(failed.get("error"), 200))),
                head=Observation("failed", bounded(failed.get("error"), 200), sha256_hex(failed)),
            )
        )
    state = str(rollback.get("status") or "skipped")
    if state == "failed" and not framework and _engine_limited(rollback.get("error"), dialect):
        state = "unverifiable"
        if status == "PASS":
            status = "UNKNOWN"
        reasons.append(
            f"down migration {rollback.get('id')} could not be emulated on SQLite ({rollback.get('error')}); "
            "rollback is unverified"
        )
    elif state == "failed":
        status = "FAIL"
        reasons.append(f"down migration {rollback.get('id')} failed: {rollback.get('error')}")
    elif state == "mismatch":
        status = "FAIL"
        reasons.append("rolling back the new migrations did not restore the base schema")
    elif state == "unverifiable" and rollback.get("status") == "unverifiable":
        if status == "PASS":
            status = "UNKNOWN"
        reasons.append(
            "rollback could not be verified: no down migration for "
            + ", ".join(map(str, rollback.get("missing_down") or ()))
        )
    if state in {"failed", "mismatch", "unverifiable"}:
        restored = rollback.get("restored") if isinstance(rollback.get("restored"), Mapping) else None
        delta = schema_diff(before, restored) if restored is not None else {}
        leftover = [f"{key.replace('_', ' ')}: {', '.join(values[:6])}" for key, values in delta.items() if values]
        findings.append(
            Finding(
                rule=f"rollback_{state}",
                category="rollback",
                subject=f"{spec.id}::rollback",
                title={
                    "failed": "Rollback fails",
                    "mismatch": "Rollback does not restore the base schema",
                    "unverifiable": "Rollback not verifiable",
                }[state],
                summary=bounded(reasons[-1] + (("; left over: " + "; ".join(leftover)) if leftover else "")),
                severity="medium" if state == "unverifiable" else "high",
                critical=state != "unverifiable",
                facts=(
                    ("Rollback", state),
                    *((("Left over", bounded("; ".join(leftover), 200)),) if leftover else ()),
                ),
                base=Observation("base schema", f"{len(_tables(before))} tables", sha256_hex(before)),
                head=Observation(state, bounded(reasons[-1], 200), sha256_hex(rollback)),
            )
        )
    for row in edited:
        path = _row_path(spec, row)
        if status == "PASS":
            status = "UNKNOWN"
        reasons.append(f"the Change edits already-applied migration {row.get('id')}")
        findings.append(
            Finding(
                rule="edited_applied_migration",
                category="history",
                subject=f"{spec.id}::edited::{row.get('id')}",
                title=f"Edits an applied migration · {row.get('id')}"[:200],
                summary="Databases that already ran the base version never re-run it, so their schema diverges from this one.",
                severity="high",
                paths=(path,),
                facts=(("Migration", str(row.get("id"))),),
                head=Observation("edited", str(row.get("id")), sha256_hex([row.get("id"), row.get("up")])),
            )
        )
    changed = any(diff.values())
    if changed:
        counts = (
            ("Tables", diff["tables_added"], diff["tables_removed"]),
            ("Columns", diff["columns_added"], diff["columns_removed"]),
            ("Indexes", diff["indexes_added"], diff["indexes_removed"]),
        )
        headline = ", ".join(
            part
            for label, added, removed in counts
            for part in (
                f"+{len(added)} {label.lower()}" if added else "",
                f"-{len(removed)} {label.lower()}" if removed else "",
            )
            if part
        )
        if diff["columns_changed"]:
            headline = ", ".join(filter(None, (headline, f"{len(diff['columns_changed'])} columns changed")))
        if diff["other_changed"]:
            headline = ", ".join(filter(None, (headline, f"{len(diff['other_changed'])} other objects changed")))
        details = [
            *(f"+ table {item}" for item in diff["tables_added"]),
            *(f"- table {item}" for item in diff["tables_removed"]),
            *(f"+ column {item}" for item in diff["columns_added"]),
            *(f"- column {item}" for item in diff["columns_removed"]),
            *(f"~ column {item}" for item in diff["columns_changed"]),
            *(f"+ index {item}" for item in diff["indexes_added"]),
            *(f"- index {item}" for item in diff["indexes_removed"]),
        ]
        findings.insert(
            0,
            Finding(
                rule="schema_diff",
                category="schema",
                subject=f"{spec.id}::schema",
                title=f"Schema change · {headline}"[:200],
                summary=bounded("; ".join(details) or headline),
                severity="low",
                paths=tuple(
                    _row_path(spec, row)
                    for row in report.get("migrations") or ()
                    if isinstance(row, Mapping) and row.get("state") == "added"
                )[:10],
                facts=(
                    *((label, f"+{len(added)} / -{len(removed)}") for label, added, removed in counts),
                    ("Rollback", state),
                    ("Fixture", fixture or "base migrations replayed"),
                    (
                        "Engine",
                        (
                            f"{engine} (real {spec.tool} run)"
                            if framework
                            else f"SQLite (rules: {report.get('dialect') or 'postgres'})"
                        ),
                    ),
                ),
                base=Observation("base schema", f"{len(_tables(before))} tables", sha256_hex(before)),
                head=Observation("migrated", f"{len(_tables(after))} tables", sha256_hex(after)),
            ),
        )
    if status == "PASS":
        reasons.append(
            f"applied {len(applied)} new migration(s) on {origin}; rollback restored the base schema"
            if applied
            else f"no new migration; the migrations apply on {origin}"
        )
    heavy: dict[str, str] = {}
    old_lines, new_lines = _schema_text(before), _schema_text(after)
    if old_lines != new_lines:
        heavy["schema.diff"] = "\n".join(
            difflib.unified_diff(old_lines, new_lines, "base-schema", "migrated-schema", lineterm="", n=2)
        )
    if head.heavy.get("schema_after.sql"):
        heavy["schema_after.sql"] = head.heavy["schema_after.sql"]
    destructive = sum(1 for item in findings if item.category == "destructive")
    lock_heavy = sum(1 for item in findings if item.category == "lock_heavy")
    if report.get("emulated"):
        reasons.append(
            "NOT NULL column(s) without a default were emulated on the empty fixture tables; "
            "on a populated production table they fail or need a backfill"
        )
    if destructive or lock_heavy:
        # Executing cleanly is not safety: flagged operations go to human judgment as behavior changes.
        reasons.append(f"{destructive} destructive and {lock_heavy} lock-heavy operation(s) flagged for review")
    return Evaluation(
        status,  # type: ignore[arg-type]
        reasons,
        findings,
        summary={
            "framework": spec.tool,
            "engine": engine if framework else "sqlite-emulation",
            "dialect": report.get("dialect") or "postgres",
            "fixture": fixture,
            "counts": report.get("counts") or {},
            "applied": applied[:50],
            "rollback": state,
            "schema_diff": {key: values[:50] for key, values in diff.items()},
            "destructive": destructive,
            "lock_heavy": lock_heavy,
        },
        heavy=heavy,
    )


__all__ = [
    "DRIVER",
    "FRAMEWORK_DRIVER",
    "attach_base_files",
    "attach_environment",
    "classify",
    "discover_specs",
    "evaluate",
    "inspect_framework_report",
    "is_narrowing",
    "parse_specs",
    "payload",
    "runtime_values",
    "schema_diff",
]
