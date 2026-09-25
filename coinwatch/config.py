import os
from pathlib import Path


def data_directory(override=None):
    if override or os.environ.get('COINWATCH_DATA_DIR'):
        return Path(override or os.environ['COINWATCH_DATA_DIR']).expanduser().resolve()
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return base / 'Coinwatch'
