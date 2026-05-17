#!/bin/sh
set -e

# Simple init script for the mailproxy project.
# - Copies .env.example -> .env if missing
# - Copies accounts.example.yml -> accounts.yml if missing
# - Creates maildata/ directory with safe permissions

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

echo "Initializing mailproxy in $ROOT_DIR"

if [ -f .env ]; then
  echo ".env already exists"
else
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
  else
    echo "No .env.example found — create .env manually or copy .env.example"
  fi
fi

if [ -f accounts.yml ]; then
  echo "accounts.yml already exists"
else
  if [ -f accounts.example.yml ]; then
    cp accounts.example.yml accounts.yml
    echo "Created accounts.yml from accounts.example.yml"
  else
    echo "No accounts.example.yml found — create accounts.yml manually"
  fi
fi

mkdir -p maildata
chmod 700 maildata || true
echo "Ensured maildata/ directory exists with restricted permissions"

echo
echo "Next steps:"
echo "  1) Edit accounts.yml with your real credentials and settings. Each account must have an 'outbound' section (per-account SMTP relay)."
echo "     Example account order: inbound -> outbound -> local"
echo "  2) Start the stack: 'make up' or 'docker compose up -d --build'"
echo "  3) To view logs: 'make logs'"
echo
echo "Tip: keep accounts.yml and .env out of version control (see .gitignore)"

# Basic sanity check: ensure each account has an outbound block
if [ -f accounts.yml ]; then
  num_accounts=$(grep -cE '^\s*address:' accounts.yml || true)
  num_outbound=$(grep -cE '^\s*outbound:' accounts.yml || true)
  if [ "$num_accounts" -gt 0 ] && [ "$num_outbound" -lt "$num_accounts" ]; then
    echo
    echo "ERROR: accounts.yml appears to have $num_accounts account(s) but only $num_outbound outbound entries."
    echo "Each account must include an 'outbound' block with SMTP relay credentials."
    echo "Please edit accounts.yml and add outbound for each account. Example: see accounts.example.yml"
    exit 1
  fi
fi
