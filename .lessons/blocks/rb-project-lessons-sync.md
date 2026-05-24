# Project lessons sync

- **id:** `rb-project-lessons-sync`
- **domain:** `coding`
- **status:** `active`
- **task_types:** implementation

## Situation
Load a tracked ReasonBlock from project lessons.

## Triggers
- workspace lessons present

## Procedure
1. Read markdown from the project lessons directory
2. Index it into SQLite

## Verification
- The block is retrievable by id after init
