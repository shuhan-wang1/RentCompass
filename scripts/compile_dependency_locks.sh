#!/usr/bin/env bash
# Regenerate reviewed dependency locks after deliberately editing constraints/inputs.
# Tested generator: Python 3.12, pip 25.3, pip-tools 7.5.2, click<8.5 (8.4.x). The
# application itself still installs pip 26.2.1 from requirements-bootstrap.lock.
# click must stay below 8.5 in the generator environment: pip-tools 7.5.2 under click 8.5.0
# writes a false "--no-index" into every lock-file header. Install the generator with
#   python3.12 -m pip install 'pip==25.3' 'pip-tools==7.5.2' 'click<8.5'
# The check below reads the click that THIS pip-compile imports (the interpreter named by its
# shebang); RENTCOMPASS_PIP_COMPILE_PYTHON overrides that interpreter for an unusual launcher.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP_COMPILE="${RENTCOMPASS_PIP_COMPILE:-pip-compile}"
cd "$REPO_DIR"

"$PIP_COMPILE" --version | grep -F '7.5.2' >/dev/null \
  || { echo "pip-compile 7.5.2 is required" >&2; exit 2; }

# The interpreter pip-compile runs under: its shebang, or the second line of pip's /bin/sh
# launcher (used for long or spaced paths):  '''exec' "/path/to/python" "$0" "$@"
compile_python=()
if [ -n "${RENTCOMPASS_PIP_COMPILE_PYTHON:-}" ]; then
  compile_python=("$RENTCOMPASS_PIP_COMPILE_PYTHON")
elif pip_compile_exe="$(command -v "$PIP_COMPILE")"; then
  shebang="$(head -n 1 "$pip_compile_exe" 2>/dev/null || true)"
  case "$shebang" in
    '#!/bin/sh'*)
      launcher="$(sed -n -e "2s/^'''exec' \"\([^\"]*\)\" .*/\1/p" \
                         -e "2s/^'''exec' \([^ \"']*\) .*/\1/p" "$pip_compile_exe")"
      if [ -n "$launcher" ]; then compile_python=("$launcher"); fi ;;
    '#!'*)
      # A shebang is a command line ("/usr/bin/env python3"): split it into words.
      read -r -a compile_python <<< "${shebang#\#!}" ;;
  esac
fi
if [ "${#compile_python[@]}" -eq 0 ]; then
  echo "cannot tell which Python runs $PIP_COMPILE; set RENTCOMPASS_PIP_COMPILE_PYTHON" >&2
  exit 2
fi
click_version="$("${compile_python[@]}" -c \
  'import importlib.metadata as m; print(m.version("click"))')" \
  || { echo "cannot read the click version of ${compile_python[*]}" >&2; exit 2; }
case "$click_version" in
  [0-7].*|8.[0-4]|8.[0-4].*) ;;
  *)
    echo "pip-compile runs with click $click_version; pip-tools 7.5.2 with click >= 8.5 writes a" \
         "false --no-index into the lock headers. Install click<8.5 into that environment" \
         "(e.g. ${compile_python[*]} -m pip install 'click<8.5') and re-run; no lock was" \
         "written." >&2
    exit 2 ;;
esac

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
