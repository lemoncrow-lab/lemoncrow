# Keeper

- **id:** `keeper`
- **domain:** `coding`
- **status:** `active`

## Situation
Changing retrieval logic for ReasonBlocks.

## Triggers
- retriever
- reasonblock

## Dead ends
- Returning duplicate procedures to the agent

## Procedure
1. Score candidate ReasonBlocks before filtering
2. Remove near-duplicate dead-end and procedure text
3. Keep the higher-ranked candidate

## Scope
- file_patterns: src/**
