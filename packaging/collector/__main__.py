"""Start the bundled collector with data outside the app bundle."""

import os
import argparse
from pathlib import Path
import sys

import server


def main():
    if sys.platform == 'win32':
        support = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    else:
        support = Path.home() / 'Library' / 'Application Support'
    data = support / 'Local Signal' / 'data' / 'hearth.sqlite3'
    if sys.platform == 'darwin' and sys.argv[1:2] == ['--migrate-legacy']:
        from . import migration
        migration.main(sys.argv[2:], data)
        return
    if sys.platform == 'darwin':
        from .migration import legacy_database
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('--data', type=Path, default=data)
        chosen, _ = parser.parse_known_args(sys.argv[1:])
        maintenance = {'--backup', '--list-backups', '--inspect-backup', '--restore-backup', '--compact-history',
                       '--router', '--google-wifi', '--help', '-h', '--version', '--disable-background'}
        is_maintenance = any(argument.split('=', 1)[0] in maintenance for argument in sys.argv[1:])
        if not is_maintenance and chosen.data.resolve() == data.resolve() and not data.exists() and legacy_database().exists():
            from .macos import migration_preview_command, show_migration_dialog
            command = migration_preview_command()
            launch_args = sys.argv[1:]
            graphical_launch = not launch_args or (len(launch_args) == 1 and launch_args[0].startswith('-psn_'))
            if graphical_launch and not getattr(sys.stdin, 'isatty', lambda: False)():
                show_migration_dialog(command)
            raise SystemExit('An existing Mac collector database was found. Preview the migration in Terminal:\n'
                             f'{command}\nThen add --apply to preserve your history and hand off login startup. '
                             'No empty database was created.')
    if sys.platform == 'darwin' and sys.argv[1:2] in (['--enable-background'], ['--disable-background']):
        from .macos import manage_login

        manage_login(sys.argv[1], sys.argv[2:], data)
        return
    server.main(default_data=data, open_browser=True)


if __name__ == '__main__':
    main()
