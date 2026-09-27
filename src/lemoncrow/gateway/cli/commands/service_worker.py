"""HTTP service and explicit worker command groups.

These commands are independent capabilities. The retired ``servicectl``
background controller used to share their module, which made the controller
look required even after the loopback-server migration.
"""

from __future__ import annotations

import json

import click

from lemoncrow.gateway.cli.commands._shared import _emit


@click.group("service")
def service_group() -> None:
    """Production HTTP service commands."""


@service_group.command("start")
@click.option("--host", default=None, help="Bind host (overrides LEMONCROW_SERVICE_HOST).")
@click.option("--port", default=None, type=int, help="Bind port (overrides LEMONCROW_SERVICE_PORT).")
@click.option("--reload", is_flag=True, default=False, help="Enable uvicorn auto-reload.")
def service_start(host: str | None, port: int | None, reload: bool) -> None:
    """Start the optional LemonCrow HTTP service API."""
    try:
        from lemoncrow.core.service.api import main as service_main
    except ImportError as exc:
        if "cannot import name 'main'" in str(exc):
            raise click.ClickException(
                "The service API 'main' entrypoint is missing. Ensure your 'lc' installation is up to date."
            ) from exc
        raise click.ClickException(
            "Could not start the service API. Ensure all dependencies are installed: uv sync --extra api"
        ) from exc
    service_main(host=host, port=port, reload=reload)


@service_group.command("config")
def service_config() -> None:
    """Print current service configuration (no secret values)."""
    from lemoncrow.core.service.config import cfg

    click.echo(json.dumps(cfg.as_dict(), indent=2))


@click.group("worker")
def worker_group() -> None:
    """Run or inspect the explicit job queue worker."""


@worker_group.command("start")
@click.pass_context
def worker_start(ctx: click.Context) -> None:
    """Start the background worker loop in this process."""
    try:
        from lemoncrow.core.service.worker import Worker
    except ImportError as exc:
        raise click.ClickException("Worker dependencies not available.") from exc

    from lemoncrow.infra.storage.factory import create_store

    root = ctx.obj["root"]
    store = create_store(root)
    store.init()
    worker = Worker(store=store)
    click.echo("Worker started. Press Ctrl+C to stop.")
    worker.run()


@worker_group.command("run-once")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def worker_run_once(ctx: click.Context, as_json: bool) -> None:
    """Claim and process one pending job then exit."""
    try:
        from lemoncrow.core.service.worker import Worker
    except ImportError as exc:
        raise click.ClickException("Worker dependencies not available.") from exc

    from lemoncrow.infra.storage.factory import create_store

    root = ctx.obj["root"]
    store = create_store(root)
    store.init()
    processed = Worker(store=store).run_once()
    if as_json:
        _emit({"processed": processed is not None, "job_id": processed}, as_json=True)
    elif processed:
        click.echo(f"processed job: {processed}")
    else:
        click.echo("no pending jobs")


@worker_group.command("enqueue")
@click.argument("job_type")
@click.option("--payload", default="{}", show_default=True, help="Inline JSON object payload.")
@click.option("--max-attempts", default=3, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def worker_enqueue(
    ctx: click.Context,
    job_type: str,
    payload: str,
    max_attempts: int,
    as_json: bool,
) -> None:
    """Queue one background job."""
    from lemoncrow.infra.storage.factory import create_store

    try:
        payload_data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid JSON payload: {exc}") from exc
    if not isinstance(payload_data, dict):
        raise click.ClickException("payload must decode to a JSON object")

    store = create_store(ctx.obj["root"])
    store.init()
    job_id = store.jobs.enqueue_job(job_type, payload_data, max_attempts=max_attempts)
    result = {"job_id": job_id, "job_type": job_type, "status": "pending"}
    _emit(result, as_json=as_json) if as_json else click.echo(job_id)


@worker_group.command("list")
@click.option("--status", default=None, help="Filter by job status.")
@click.option("--job-type", default=None, help="Filter by job type.")
@click.option("--limit", default=20, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def worker_list(ctx: click.Context, status: str | None, job_type: str | None, limit: int, as_json: bool) -> None:
    """List queued and processed jobs."""
    from lemoncrow.infra.storage.factory import create_store

    store = create_store(ctx.obj["root"])
    store.init()
    jobs = store.jobs.list_jobs(status=status, job_type=job_type, limit=limit)
    if as_json:
        _emit(jobs, as_json=True)
    elif not jobs:
        click.echo("(no jobs)")
    else:
        for job in jobs:
            click.echo(f"{job['id']}\t{job['job_type']}\t{job['status']}\tattempts={job['attempts']}")
