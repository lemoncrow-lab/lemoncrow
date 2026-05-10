#!/bin/bash
set -e

PORT=${1:-3200}
DOCS_SRC=${2:-/docs}

echo "Syncing docs from $DOCS_SRC into Docusaurus content..."

# Clear existing docs to remove default tutorials from create-docusaurus
rm -rf /app/docs-site/docs/*

# Sync all markdown files, preserving directory structure  
if [ -d "$DOCS_SRC" ]; then
  rsync -av \
    --include='*/' \
    --include='*.md' \
    --exclude='*' \
    "$DOCS_SRC/" /app/docs-site/docs/
else
  echo "Warning: $DOCS_SRC not found, skipping sync"
fi

# Ensure _category_.json exists for subdirectories that don't have one
find /app/docs-site/docs -type d | while read dir; do
  if [ ! -f "$dir/_category_.json" ]; then
    name=$(basename "$dir")
    cat > "$dir/_category_.json" <<EOF
{
  "label": "$name",
  "position": 1
}
EOF
  fi
done

echo "Docs synced. Starting Docusaurus on port $PORT..."

cd /app/docs-site
npm run start -- --host 0.0.0.0 --port $PORT
