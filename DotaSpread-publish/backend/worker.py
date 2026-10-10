"""Optional independent collector: python -m backend.worker."""

import logging
import os
import signal
import time

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
        print(f'Сборщик запущен: роль {service.collector_role}, приоритет {service.priority_size} предметов. '
              'Результаты сохраняются в общей PostgreSQL. Остановка — Ctrl+C.', flush=True)
        next_report = 0
        while not service.stop.wait(1):
            if not service.collector_running():
                raise SystemExit('Сборщик остановился; процесс должен быть перезапущен хостингом.')
            if time.monotonic() >= next_report:
                result = service.snapshot()
                steam = result['sources']['steam']
                wait = max(0, int((steam.get('retry_at') or 0) - time.time()))
                owner = service.controls.get('steam', {}).get('worker', 'ожидаем первую проверку')
                print(f"Проверено {result['checked_total']}/{result['catalog_total']}; "
                      f"свежих выгодных: {result['profitable_total']}; "
                      f"интервал Steam: {result['steam_request_interval_seconds']:g} с; "
                      f"пауза Steam: {wait} с; база: {result['storage']['state']}; "
                      f"последний сборщик Steam: {owner}.", flush=True)
                next_report = time.monotonic() + 60
    finally:
        service.close()


if __name__ == '__main__':
    main()
