"""Start the bundled collector with data outside the app bundle."""

import os
from pathlib import Path
import sys

import server


def main():
    if sys.platform == 'win32':
        support = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    else:
        support = Path.home() / 'Library' / 'Application Support'
    data = support / 'Local Signal' / 'data' / 'hearth.sqlite3'
    if sys.platform == 'darwin' and sys.argv[1:2] in (['--enable-background'], ['--disable-background']):
        from .macos import manage_login

        manage_login(sys.argv[1], sys.argv[2:], data)
        return
    server.main(default_data=data, open_browser=True)


if __name__ == '__main__':
    main()
