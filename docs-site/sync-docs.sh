#!/bin/bash
set -e

PORT=${1:-3200}

# Build the static site (prebuild syncs the public docs) and serve the output.
# For live editing use `npm run start` directly.
echo "Building the docs site..."
npm run build

echo "Serving the built site on port $PORT..."
npm run serve -- --host 0.0.0.0 --port "$PORT" --no-open
