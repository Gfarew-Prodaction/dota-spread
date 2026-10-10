"""Load a private local configuration without executing shell code."""

import os
import fcntl
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .worker import main as run_worker

ROOT = Path(__file__).resolve().parents[1]
CONFIG_KEYS = {
    'DATABASE_URL', 'COLLECTOR_ROLE', 'COLLECTOR_ID', 'STEAM_PRIORITY_SIZE',
    'STEAM_REQUEST_INTERVAL', 'LIS_REFRESH_INTERVAL', 'UPDATE_INTERVAL',
    'QUOTE_TTL', 'CATALOG_TTL', 'PENDING_TTL', 'STEAM_BATCH_SIZE',
}


def load_config(path, environ=None):
    environ = os.environ if environ is None else environ
    values = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or key not in CONFIG_KEYS or key in values:
            raise ValueError(f'.env.collector: проверь строку {number}. Нужен формат ИМЯ=значение.')
        if value[:1] in ('"', "'"):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f'.env.collector: проверь кавычки в строке {number}.')
            value = value[1:-1]
        values[key] = value
    merged = {**values, **environ}
    try:
        url = urlsplit(merged.get('DATABASE_URL', ''))
        valid = url.scheme in ('postgresql', 'postgres') and url.hostname and url.path not in ('', '/')
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Вставь строку PostgreSQL из Neon → Connect в DATABASE_URL файла .env.collector. '
                         'Нужна только строка подключения, без команды psql.')
    if merged.get('COLLECTOR_ROLE', 'all') not in ('all', 'steam', 'sources'):
        raise ValueError('Домашнему сборщику нужна COLLECTOR_ROLE=all, steam или sources.')
    for key in CONFIG_KEYS - {'DATABASE_URL', 'COLLECTOR_ROLE', 'COLLECTOR_ID'}:
        if key in merged:
            try:
                number = float(merged[key]) if key == 'STEAM_REQUEST_INTERVAL' else int(merged[key])
                if not 0 < number < float('inf'):
                    raise ValueError
            except ValueError:
                raise ValueError(f'.env.collector: {key} должна быть положительным числом.') from None
    for key, value in values.items():
        environ.setdefault(key, value)
    environ.setdefault('COLLECTOR_ROLE', 'all')
    environ.setdefault('STEAM_PRIORITY_SIZE', '200')


def main():
    if sys.argv[1:] == ['--stop']:
        return stop_collector()
    if sys.argv[1:]:
        print('Запуск: python -m backend.local_worker. Остановка: python -m backend.local_worker --stop.')
        return 1
    path = ROOT / '.env.collector'
    try:
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                file.write((ROOT / '.env.collector.example').read_text(encoding='utf-8'))
        path.chmod(0o600)
        load_config(path)
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else 'Не удалось прочитать .env.collector.'
        print(message)
        return 1
    # The home collector intentionally runs without paid bulk providers.
    os.environ.pop('STEAMAPIS_KEY', None)
    lock_path = ROOT / '.cache' / 'home-collector.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'r+', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Домашний сборщик уже работает. Для остановки открой stop-collector.command.')
            return 0
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        try:
            return run_foreground()
        finally:
            lock.seek(0)
            lock.truncate()


def run_foreground():
    try:
        run_worker()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('Сборщик не запустился. Проверь DATABASE_URL и доступ к Neon; пароль в чат не отправляй.')
        return 1
    return 0


def stop_collector():
    path = ROOT / '.cache' / 'home-collector.lock'
    if not path.exists():
        print('Домашний сборщик уже остановлен.')
        return 0
    try:
        with path.open('r+', encoding='utf-8') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pid = int(lock.read().strip())
                result = subprocess.run(['ps', '-p', str(pid), '-o', 'args='], capture_output=True, text=True, check=False)
                if not result.stdout.strip().endswith('-m backend.local_worker'):
                    print('Процесс сборщика не подтверждён; команда остановки отменена.')
                    return 1
                os.kill(pid, signal.SIGTERM)
                print('Команда остановки отправлена домашнему сборщику.')
                return 0
        print('Домашний сборщик уже остановлен.')
        return 0
    except (OSError, ValueError):
        print('Не удалось остановить сборщик; повтори команду через несколько секунд.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
