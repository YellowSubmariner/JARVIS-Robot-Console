import json
import sys
from pathlib import Path
from urllib.parse import urlsplit


def app_directory():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


DEFAULTS = {
    'active_page': 'astra',
    'wrist_url': 'rtsp://192.168.1.10/color',
    'astra_name': 'Astra Pro HD Camera',
    # User-selected interpreter with the matching Kinova SDK. Empty until set.
    'kortex_python': '',
}


class Settings:
    def __init__(self):
        self.directory = app_directory() / 'data'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'settings.json'
        self.values = DEFAULTS.copy()
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                for key in DEFAULTS:
                    if isinstance(data.get(key), str) and data[key].strip():
                        self.values[key] = data[key]
        except (OSError, ValueError):
            pass

    def get(self, key):
        return self.values[key]

    def save(self, **changes):
        self.values.update(changes)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)


def validate_rtsp_url(value):
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
        return (parsed.scheme.lower() in ('rtsp', 'rtsps') and bool(parsed.hostname)
                and (port is None or 1 <= port <= 65535))
    except ValueError:
        return False
