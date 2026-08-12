#!/usr/bin/env bash
# Regenerate reviewed dependency locks after deliberately editing constraints/inputs.
# Tested generator: Python 3.12, pip 25.3, pip-tools 7.5.2. The application itself
# still installs pip 26.2.1 from requirements-bootstrap.lock.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP_COMPILE="${RENTCOMPASS_PIP_COMPILE:-pip-compile}"
cd "$REPO_DIR"

"$PIP_COMPILE" --version | grep -F '7.5.2' >/dev/null \
  || { echo "pip-compile 7.5.2 is required" >&2; exit 2; }

common=(--no-config --quiet --generate-hashes --reuse-hashes --allow-unsafe --strip-extras)
"$PIP_COMPILE" "${common[@]}" \
  --constraint constraints-production.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --output-file requirements-production.lock pyproject.toml
"$PIP_COMPILE" "${common[@]}" \
  --constraint constraints-production.txt \
  --output-file requirements-bootstrap.lock requirements-bootstrap.in
"$PIP_COMPILE" "${common[@]}" \
  --constraint constraints-production.txt \
  --output-file requirements-ci.lock requirements-ci.in
"$PIP_COMPILE" "${common[@]}" \
  --output-file requirements-supply.lock requirements-supply.in

echo "Dependency locks regenerated; review the diff and run the supply-chain gate."
