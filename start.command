#!/bin/zsh
cd "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
if (( $# == 0 )); then
  if /usr/bin/curl --silent --fail --max-time 2 http://localhost:8799/api/state > /dev/null; then
    /usr/bin/open http://localhost:8799
    exit 0
  fi
  if [[ -f "$HOME/Library/LaunchAgents/local.hearth.collector.plist" ]]; then
    /bin/launchctl kickstart "gui/$UID/local.hearth.collector"
    /usr/bin/open http://localhost:8799
    exit 0
  fi
fi
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python server.py "$@"
fi
exec python3 server.py "$@"
