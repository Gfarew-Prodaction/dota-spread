"""Optional independent collector: python -m backend.worker."""

import logging
import os
import signal

from .app import create_service


def main():
    if not os.getenv('DATABASE_URL', '').strip():
        raise SystemExit('Отдельному сборщику нужна общая PostgreSQL: задайте DATABASE_URL.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    service = create_service(default_role='steam')
    if service.collector_role == 'none':
        service.close()
        raise SystemExit('Для сборщика COLLECTOR_ROLE должна быть steam, sources или all.')
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: service.stop.set())
    try:
        service.start()
        while not service.stop.wait(1):
            if not service.collector_running():
                raise SystemExit('Сборщик остановился; процесс должен быть перезапущен хостингом.')
    finally:
        service.close()


if __name__ == '__main__':
    main()
