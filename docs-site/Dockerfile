FROM node:20-slim

WORKDIR /app/

# Copy docs-site project (self-contained with package.json)
COPY docs-site/ /app/docs-site/
# custom.css imports the design tokens from the Review app's vendored copy
COPY frontend/src/design/lc-tokens.css /app/frontend/src/design/lc-tokens.css

# Install dependencies
RUN cd /app/docs-site && npm ci

# Copy public-facing docs (from repo root, synced at build time by sync-docs.mjs)
COPY docs/ /app/docs/

WORKDIR /app/docs-site/

# Build the static site once (prebuild syncs the public allowlist); the container
# serves that output, never the dev server.
RUN npm run build

EXPOSE 3200

CMD ["npm", "run", "serve", "--", "--host", "0.0.0.0", "--port", "3200", "--no-open"]
