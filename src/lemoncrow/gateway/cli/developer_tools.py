"""Checkout-only engineering tools, dispatched by lc dev."""

from __future__ import annotations

from pathlib import Path

import click

from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.gateway.cli.commands.defaults import defaults_group
from lemoncrow.gateway.cli.commands.eval import eval_
from lemoncrow.gateway.cli.commands.perf import perf_group
from lemoncrow.gateway.cli.commands.review_benchmark import (
    benchmark,
    derive_label_command,
    import_reviews,
    init_corpus_command,
)
from lemoncrow.gateway.cli.commands.review_eval import review_eval
from lemoncrow.gateway.cli.commands.review_golden import golden_score


@click.group()
@click.option("--root", type=click.Path(path_type=Path), default=default_store_root)
@click.pass_context
def developer_tools(ctx: click.Context, root: Path) -> None:
    """Run evaluation, profiling and default-definition tools."""
    ctx.ensure_object(dict)
    ctx.obj["root"] = root


review_eval.add_command(benchmark)
review_eval.add_command(import_reviews)
review_eval.add_command(derive_label_command)
review_eval.add_command(init_corpus_command)
review_eval.add_command(golden_score)
eval_.add_command(review_eval)
# The same command, where the strategy notes name it: `lc dev eval review-benchmark`.
eval_.add_command(benchmark, "review-benchmark")
developer_tools.add_command(eval_)
developer_tools.add_command(perf_group)
developer_tools.add_command(defaults_group)

if __name__ == "__main__":
    developer_tools(prog_name="lc dev")
