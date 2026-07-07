"""Atelier's self-optimization fitness (an example swarm ``FitnessSpec`` consumer).

Benchmark/test tooling, not product runtime: ``eval.py`` is the concrete
``metric_command`` a swarm run points ``atelier swarm start --fitness-cmd``
at to optimize Atelier against its own SWE benchmark. See README.md in this
directory for the pipeline and how to wire it up.
"""
