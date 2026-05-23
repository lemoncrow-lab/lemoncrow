# Project knowledge sync

- **id:** `rb-project-knowledge-sync`
- **domain:** `coding`
- **status:** `active`
- **task_types:** implementation

## Situation
Load a tracked ReasonBlock from project knowledge.

## Triggers
- workspace knowledge present

## Procedure
1. Read markdown from the project knowledge directory
2. Index it into SQLite

## Verification
- The block is retrievable by id after init
