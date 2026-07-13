#!/usr/bin/env bash
# Adds the `clres` alias to ~/.zshrc (idempotent).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALIAS_LINE="alias clres='python3 ${REPO_DIR}/clres.py'"
RC="${HOME}/.zshrc"

chmod +x "${REPO_DIR}/clres.py"

if grep -qF "alias clres=" "${RC}" 2>/dev/null; then
  echo "clres alias already present in ${RC}"
else
  printf '\n# clres — Claude Code conversation browser\n%s\n' "${ALIAS_LINE}" >> "${RC}"
  echo "Added to ${RC}: ${ALIAS_LINE}"
fi

echo "Reload your shell or run: source ${RC}"
