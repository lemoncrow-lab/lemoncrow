"""LemonCrow Harbor agent adapter for running LemonCrow on Harbor benchmark datasets.

Usage::

    harbor run -d "terminal-bench/terminal-bench-core@0.1.1" \\
        --agent benchmarks.harbor.lemoncrow_agent:LemonCrowHarborAgent

Or with Bedrock credentials:

    harbor run -d "terminal-bench/terminal-bench-core@0.1.1" \\
        --agent benchmarks.harbor.lemoncrow_agent:LemonCrowBedrockHarborAgent

See docs/benchmarks/harbor-eval.md for full instructions.
"""
