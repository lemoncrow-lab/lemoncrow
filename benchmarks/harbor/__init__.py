"""Atelier Harbor agent adapter for running Atelier on Harbor benchmark datasets.

Usage::

    harbor run -d "terminal-bench/terminal-bench-core@0.1.1" \\
        --agent benchmarks.harbor.atelier_agent:AtelierHarborAgent

Or with Bedrock credentials:

    harbor run -d "terminal-bench/terminal-bench-core@0.1.1" \\
        --agent benchmarks.harbor.atelier_agent:AtelierBedrockHarborAgent

See docs/benchmarks/harbor-eval.md for full instructions.
"""
