"""Read-only monitoring of the public doctor availability list."""

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo


MSK = ZoneInfo("Europe/Moscow")
ENDPOINT = "https://gorzdrav.spb.ru/_api/api/v2/schedule/lpu/172/speciality/49/doctors"
DOCTOR_ID = "839"
DOCTOR_NAME_PART = "мирагасы кызы"


def availability():
    request = urllib.request.Request(ENDPOINT, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    if data.get("success") is not True or not isinstance(data.get("result"), list):
        raise ValueError("Сайт не вернул корректный список врачей")
    matches = [doctor for doctor in data["result"] if str(doctor.get("id")) == DOCTOR_ID]
    if len(matches) != 1 or DOCTOR_NAME_PART not in matches[0].get("name", "").casefold():
        raise ValueError("Врач не найден или его данные изменились; требуется проверка")
    count = matches[0].get("freeTicketCount")
    if type(count) is not int or count < 0:
        raise ValueError("Сайт изменил формат количества талонов")
    return count


def main():
    parser = argparse.ArgumentParser(description="Проверка талонов без создания записи")
    parser.add_argument("--watch", action="store_true", help="проверять до появления талонов или истечения лимита")
    parser.add_argument("--at", metavar="YYYY-MM-DDTHH:MM:SS", help="время начала по Москве; только с --watch")
    parser.add_argument("--interval", type=int, default=5, help="пауза между запросами, минимум 5 секунд")
    parser.add_argument("--minutes", type=int, default=10, help="длительность мониторинга, от 1 до 30 минут")
    args = parser.parse_args()
    if args.interval < 5 or args.interval > 300 or not 1 <= args.minutes <= 30:
        parser.error("Интервал должен быть 5–300 секунд, длительность — 1–30 минут")
    start = None
    if args.at:
        if not args.watch:
            parser.error("--at требует --watch")
        try:
            start = datetime.strptime(args.at, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=MSK)
        except ValueError:
            parser.error("Формат времени: YYYY-MM-DDTHH:MM:SS (Москва)")
        if start <= datetime.now(MSK):
            parser.error("Время начала уже прошло; укажите будущую дату и время")
        print(f"Ожидание {start:%Y-%m-%d %H:%M:%S} МСК. Для остановки: Ctrl+C.", flush=True)
        while (remaining := (start - datetime.now(MSK)).total_seconds()) > 0:
            time.sleep(min(remaining, 1))
        if (datetime.now(MSK) - start).total_seconds() > args.minutes * 60:
            print("Окно мониторинга пропущено, возможно компьютер спал. Проверки не запущены.")
            return 1
    remaining_window = args.minutes * 60
    if start is not None:
        remaining_window -= max(0, (datetime.now(MSK) - start).total_seconds())
    deadline = time.monotonic() + remaining_window
    errors = 0
    while True:
        if time.monotonic() >= deadline or (
            start is not None and (datetime.now(MSK) - start).total_seconds() >= args.minutes * 60
        ):
            print("Время мониторинга истекло. Запись не создавалась.")
            return 0
        try:
            count = availability()
        except urllib.error.HTTPError as error:
            print(f"HTTP {error.code}. Мониторинг остановлен; проверьте сайт вручную.", flush=True)
            return 1
        except ValueError as error:
            print(f"{error}. Мониторинг остановлен.", flush=True)
            return 1
        except (urllib.error.URLError, TimeoutError, OSError):
            errors += 1
            print(f"Не удалось получить расписание (ошибка {errors}/3).", flush=True)
            if not args.watch or errors >= 3:
                return 1
        else:
            errors = 0
            print(f"{datetime.now(MSK):%H:%M:%S} МСК — свободных талонов: {count}", flush=True)
            if count:
                print("\a\a\aПоявились талоны! Перейдите в подготовленный браузер. Запись НЕ создана.", flush=True)
                return 0
            if not args.watch:
                return 0
        delay = args.interval * (2 ** errors)
        if time.monotonic() + delay >= deadline:
            print("Время мониторинга истекло. Запись не создавалась.")
            return 0
        time.sleep(delay)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nМониторинг остановлен.")
