#!/bin/zsh
set -e
cd "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python background.py install
fi
exec python3 background.py install
