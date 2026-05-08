# Environment Authoring

Atelier Environments define the boundaries and toolsets available to agents when executing specific domains of work. Teams can create Environments to encapsulate specialized workflows such as live state changes, debugging loops, or source-of-truth corrections.

## Anatomy of an Environment

An Environment is defined in a `yaml` file:

```yaml
id: env_state_change_safety
domain: state.change
description: "Operating law for tasks that mutate durable external state."
triggers:
  - publish
  - migration
  - deploy config
forbidden:
  - resolve target from url slug alone
  - skip readback after write
required:
  - canonical_identifier_used
  - read_after_write_completed
  - observed_state_matches_intent
rubric_id: rubric_state_change_safety
related_blocks:
  - canonical-identifier-over-display-name
  - read-after-write-verification
```

## Distributing via Packs

Environments are bundled into Packs (`atelier pack create`). Install them locally to grant agents in your team or deployment access to a predefined toolkit and safety gates.

```bash
# Create the pack
atelier pack create my-env-pack --type environments

# Validate it
atelier pack validate ./my-env-pack

# Install it locally
atelier pack install ./my-env-pack
```

Packs are installed from local paths or private internal paths (`private://`). External URLs and public registries are disabled by default.
