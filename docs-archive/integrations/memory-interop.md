# Memory Interop

Atelier interoperates with memory systems but does not replace them.

## Separation of Concerns

- Memory systems store facts, preferences, and durable state.
- Atelier stores procedures, verification rules, failure patterns, and benchmark traces.

## Adapters

- `src/atelier/integrations/memory/openmemory.py`
- `src/atelier/integrations/memory/mem0.py`
- `src/atelier/integrations/memory/generic_vector_memory.py`

## Sync Policy

Pull from memory:

- prior task context
- accepted identifiers
- user or project facts

Push to memory:

- pointers to completed traces
- accepted procedural lessons when the host explicitly opts in

Never push by default:

- raw chain-of-thought
- hidden reasoning
- unreviewed rescue procedures
