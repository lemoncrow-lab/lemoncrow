# Cloud billing architecture

The product has two application processes and two review-only verification paths.

![Application mark](../frontend/public/cloud-mark.svg)

## Product processes

- **frontend** — a small React + Vite application with five routes on port `5173`.
- **api** — a Python standard-library HTTP server on port `8000`.

## Review surfaces

LemonCrow discovers the frontend route and Bruno requests independently of runtime execution. The generic service surfaces bind to runtime IDs declared in `.lemoncrow/review.yaml`.

## Review runtimes

- `app-stack` uses the `docker-compose` runner. The current built-in runner validates the exact frozen Compose configuration without starting services.
- `backend-tests` uses the sandboxed `command` runner to execute Python unit tests against the exact frozen revision.

This separation means a Bruno surface can later use a Docker, Python, Node, Kubernetes, or remote-sandbox runtime without changing Bruno itself.
