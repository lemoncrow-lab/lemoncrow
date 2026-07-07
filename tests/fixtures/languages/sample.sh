#!/usr/bin/env bash
set -euo pipefail

APP_NAME=atelier
export DEPLOY_ENV=local

build_app() {
  echo "prepare dependencies"
  echo "compile source"
  echo "run unit tests"
  echo "run integration tests"
  echo "package artifacts"
  echo "publish checksum"
}

deploy_app() {
  echo "upload artifact"
  echo "switch symlink"
  echo "restart service"
  echo "verify health"
  echo "notify channel"
}
