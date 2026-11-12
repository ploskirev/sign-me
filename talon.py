"""Portable macOS launcher for gorzdrav.spb.ru. No patient data is bundled."""

import argparse
import atexit
import configparser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
from urllib.parse import unquote, urlsplit

import booking
from playwright.sync_api import Error as BrowserError, sync_playwright

# Moscow has used UTC+3 without seasonal changes since 2014. No external tzdata needed.
MSK = timezone(timedelta(hours=3), "МСК")


class ConfigError(Exception):
    pass


class StopBooking(Exception):
    pass


class ScheduleRetry(booking.ReadRetry):
    pass


def normalized(text):
    return " ".join(unicodedata.normalize("NFC", text).casefold().replace("ё", "е").split())


@dataclass
class Settings:
    patient: dict
    doctor: str
    route: dict
    release: datetime
    start: datetime
    end: datetime
    interval: int
    commit: bool


def parse_config(path, now=None):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigError("Не удалось прочитать config.txt. Проверьте наличие файла и формат INI.") from error
    return parse_config_text(text, now)


def parse_config_text(text, now=None):
    now = now or datetime.now(MSK)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(text.lstrip('\ufeff'))
    except configparser.Error as error:
        raise ConfigError("Не удалось прочитать настройки. Проверьте формат INI.") from error
    allowed = {
        "Пациент": {"Фамилия", "Имя", "Отчество", "Дата рождения"},
        "Запись": {"Врач", "Страница", "Дата выдачи", "Время выдачи", "Режим", "Минут ожидания", "Интервал секунд"},
    }
    if parser.defaults() or set(parser.sections()) != set(allowed):
        raise ConfigError("В config.txt нужны ровно два раздела: [Пациент] и [Запись].")
    for section, keys in allowed.items():
        if set(parser[section]) - keys:
            raise ConfigError(f"Неизвестное поле в разделе [{section}]. Сверьте названия с инструкцией.")

    def required(section, key):
        value = parser[section].get(key, "").strip()
        if not value or "[MASKED:" in value:
            raise ConfigError(f"Заполните поле «{key}» в разделе [{section}].")
        return value

    patient = {"lastName": required("Пациент", "Фамилия"), "firstName": required("Пациент", "Имя"),
               "middleName": parser["Пациент"].get("Отчество", "").strip(),
               "birthdate": required("Пациент", "Дата рождения"), "email": "", "phone": ""}
    try:
        birthday = datetime.strptime(patient["birthdate"], "%d.%m.%Y").date()
        if birthday > now.date() or birthday.year < 1850:
            raise ValueError()
    except ValueError:
        raise ConfigError("Дата рождения должна иметь формат ДД.ММ.ГГГГ и находиться в прошлом.")
    doctor = required("Запись", "Врач")
    try:
        url = urlsplit(required("Запись", "Страница"))
    except ValueError:
        raise ConfigError("Некорректная ссылка на страницу записи.")
    if (url.scheme != "https" or url.netloc != "gorzdrav.spb.ru"
            or url.path.rstrip("/") != "/service-free-schedule" or url.query):
        raise ConfigError("Нужна HTTPS-ссылка gorzdrav.spb.ru/service-free-schedule на список врачей.")
    try:
        parts = json.loads(unquote(url.fragment))
        if not isinstance(parts, list):
            raise ValueError()
        route = {}
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError()
            for key in ("district", "lpu", "speciality"):
                if key in part:
                    if key in route or not str(part[key]).isdigit():
                        raise ValueError()
                    route[key] = str(part[key])
        if set(route) != {"district", "lpu", "speciality"}:
            raise ValueError()
    except (ValueError, TypeError):
        raise ConfigError("В ссылке должны быть выбраны район, поликлиника и специальность.")
    mode = normalized(required("Запись", "Режим"))
    if mode not in ("запись", "проверка"):
        raise ConfigError("Режим должен быть «запись» или «проверка».")
    try:
        minutes = int(parser["Запись"].get("Минут ожидания", "15"))
        interval = int(parser["Запись"].get("Интервал секунд", "3"))
        if not 1 <= minutes <= 60 or not 2 <= interval <= 30:
            raise ValueError()
    except ValueError:
        raise ConfigError("Минут ожидания: 1–60; интервал секунд: 2–30.")
    release_time = required("Запись", "Время выдачи")
    if normalized(release_time) == "сейчас":
        release = now
        start = now
    else:
        day = parser["Запись"].get("Дата выдачи", "сегодня").strip()
        try:
            if normalized(day) == "сегодня":
                date = now.date()
            elif normalized(day) == "завтра":
                date = (now + timedelta(days=1)).date()
            else:
                date = datetime.strptime(day, "%d.%m.%Y").date()
            hour = datetime.strptime(release_time, "%H:%M").time()
            release = datetime.combine(date, hour, tzinfo=MSK)
        except ValueError:
            raise ConfigError("Дата выдачи: сегодня, завтра или ДД.ММ.ГГГГ; время: ЧЧ:ММ или сейчас.")
        start = release - timedelta(seconds=15)
    end = release + timedelta(minutes=minutes)
    if end <= now:
        raise ConfigError("Окно выдачи уже прошло. Укажите другую дату или «Время выдачи = сейчас».")
    return Settings(patient, doctor, route, release, start, end, interval, mode == "запись")


_browser_temp = None


def unpack_browser():
    global _browser_temp
    if not getattr(sys, "frozen", False):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(__file__).parent / ".browsers")
        return
    if _browser_temp is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _browser_temp.name
        return
    root = Path(sys._MEIPASS)
    temporary = tempfile.TemporaryDirectory(prefix="talon-chromium-")
    target = Path(temporary.name)
    # This archive is produced locally by build_portable.py and travels inside the executable.
    try:
        with tarfile.open(root / "chromium.tar", "r") as archive:
            archive.extractall(target, filter="data")
    except Exception:
        temporary.cleanup()
        raise
    _browser_temp = temporary
    atexit.register(temporary.cleanup)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(target)


class Journal:
    def __init__(self, path):
        self.output = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8")

    def __call__(self, message):
        line = f"{datetime.now(MSK):%d.%m.%Y %H:%M:%S} МСК — {message}"
        print(line, flush=True)
        self.output.write(line + "\n")
        self.output.flush()

    def close(self):
        self.output.close()


def identity_key(settings):
    parts = [settings.route["lpu"], settings.route["speciality"], normalized(settings.doctor)]
    parts += [normalized(settings.patient.get(key, "")) for key in ("lastName", "firstName", "middleName", "birthdate")]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


def check_previous(marker, now):
    if not marker.exists():
        return set()
    try:
        state = json.loads(marker.read_text())
    except (OSError, ValueError):
        raise StopBooking("Файл предыдущей попытки повреждён. Сначала проверьте существующую запись на сайте.")
    if state.get("status") == "rejected_slot":
        return set(state.get("excluded", []))
    if state.get("status") == "not_sent":
        marker.rename(marker.with_name(marker.stem + f"-not-sent-{time.time_ns()}.json"))
        return set()
    if state.get("status") == "confirmed":
        try:
            visit = datetime.fromisoformat(state["visit"]).replace(tzinfo=MSK)
        except (KeyError, ValueError):
            visit = None
        if visit is not None and visit < now:
            marker.rename(marker.with_name(marker.stem + f"-past-{time.time_ns()}.json"))
            return set()
        raise StopBooking("Уже есть подтверждённая будущая запись для этого пациента и врача. Повторная отправка заблокирована.")
    raise StopBooking("Предыдущая заявка уже отправлялась, результат неясен. Сначала проверьте запись на сайте; повторять её автоматически нельзя.")


def schedule_decision(envelope, doctor_name, doctor_id=None):
    status = envelope.get("status", 0)
    data = envelope.get("data")
    if status in (401, 403):
        raise StopBooking(f"Сайт требует участия пользователя (HTTP {status}). Откройте страницу вручную.")
    if status == 429:
        delay = booking.retry_after_seconds(envelope.get("retry_after"))
        raise ScheduleRetry("Сайт ограничил частоту запросов (HTTP 429).", delay)
    if status != 200 or not isinstance(data, dict):
        raise ScheduleRetry(f"Расписание временно недоступно: HTTP {status}; ответ {type(data).__name__}.")
    if data.get("success") is not True or not isinstance(data.get("result"), list):
        code = data.get("errorCode")
        # Only a numeric code is logged; the server message may contain personal data.
        code = code if isinstance(code, (int, float)) else "не указан"
        raise ScheduleRetry(f"Ответ расписания отклонён: success={data.get('success') is True}, errorCode={code}, result={type(data.get('result')).__name__}.")
    doctors = data["result"]
    matching = [d for d in doctors if isinstance(d, dict) and normalized(str(d.get("name", ""))) == normalized(doctor_name)]
    if len(matching) > 1:
        raise StopBooking("На странице несколько врачей с одинаковым ФИО. Автоматический выбор неоднозначен.")
    if not matching:
        raise ScheduleRetry("В ответе расписания не найдено точное ФИО врача. Проверьте ФИО и ссылку, если ошибка повторяется.")
    doctor = matching[0]
    identifier = str(doctor.get("id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ScheduleRetry("Сайт вернул некорректный идентификатор врача.")
    if doctor_id is not None and identifier != doctor_id:
        raise StopBooking("Идентификатор выбранного врача изменился во время запуска. Требуется ручная проверка.")
    counts = [doctor.get("freeTicketCount"), doctor.get("freeParticipantCount")]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ScheduleRetry("Сайт изменил формат счётчика талонов; запрос будет повторён.")
    return identifier, str(doctor["name"]), max(counts)


def read_schedule(page, route):
    return page.evaluate("""async ({lpu, speciality}) => {
        if (typeof HealthcarePortalClient === 'undefined' || typeof API_HOST === 'undefined')
            return {status:0, data:null};
        return await new Promise(resolve => {
            let finished=false;
            const finish=value=>{if(!finished){finished=true;clearTimeout(timer);resolve(value)}};
            const request=new HealthcarePortalClient(API_HOST).getDoctors(lpu,speciality);
            const timer=setTimeout(()=>{finish({status:0,data:null});request.abort()},12000);
            request.done((data,_,xhr)=>finish({status:xhr.status,data}));
            request.fail(xhr=>finish({status:xhr.status,data:null,retry_after:xhr.getResponseHeader('Retry-After')}));
        });
    }""", route)


def wait_page(page, moment, cancel_event=None):
    while datetime.now(MSK) < moment:
        booking.ensure_active(cancel_event=cancel_event)
        if page.is_closed():
            raise StopBooking("Окно браузера закрыто. Запуск остановлен.")
        page.wait_for_timeout(min(1000, max(1, int((moment - datetime.now(MSK)).total_seconds() * 1000))))


def recoverable_before_submission(error, marker):
    if marker.exists():
        try:
            if json.loads(marker.read_text()).get("status") != "rejected_slot":
                return False
        except (ValueError, OSError):
            return False
    return isinstance(error, (booking.ReadRetry, BrowserError))


def monitor(page, settings, marker, log, cancel_event=None, progress=None):
    emit = progress or (lambda stage, detail: None)
    def wait(moment):
        if cancel_event is None:
            wait_page(page, moment)
        else:
            wait_page(page, moment, cancel_event)
    doctor_id = None
    failures = 0
    excluded = check_previous(marker, datetime.now(MSK)) if settings.commit else set()
    url = booking.booking_url(settings.route)
    need_load = True
    while datetime.now(MSK) < settings.end:
        booking.ensure_active(cancel_event=cancel_event)
        if page.is_closed():
            raise StopBooking("Окно браузера закрыто. Мониторинг остановлен.")
        try:
            if need_load:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function("typeof HealthcarePortalClient !== 'undefined' && typeof API_HOST !== 'undefined'", timeout=20_000)
                need_load = False
            booking.ensure_active(settings.end, cancel_event)
            envelope = read_schedule(page, settings.route)
            doctor_id, server_name, count = schedule_decision(envelope, settings.doctor, doctor_id)
            booking.ensure_active(settings.end, cancel_event)
            if datetime.now(MSK) < settings.start:
                failures = 0
                log("Страница и врач проверены. Ожидаю время начала мониторинга.")
                emit("waiting", {"start": settings.start.isoformat(), "release": settings.release.isoformat()})
                wait(settings.start)
                continue
            log(f"Свободных талонов: {count}.")
            emit("monitoring", {"count": count})
            if count:
                emit("found", {"count": count})
                config = {**settings.route, "doctor_id": doctor_id, "doctor_name_contains": server_name, "patient": settings.patient}
                page.reload(wait_until="domcontentloaded", timeout=30_000)
                options = {"deadline": settings.end}
                if cancel_event is not None:
                    options["cancel_event"] = cancel_event
                if progress is not None:
                    options["progress"] = progress
                outcome = booking.book_from_page(page, config, settings.commit, marker, excluded, **options)
                if outcome in ("confirmed", "dry-run"):
                    return outcome
                if outcome == "retry":
                    failures = 0
                    need_load = True
                    continue
            failures = 0
        except booking.WindowExpired:
            break
        except Exception as error:
            if not recoverable_before_submission(error, marker):
                raise
            failures += 1
            reason = str(error) if isinstance(error, booking.ReadRetry) else type(error).__name__
            # Only read operations are retried. Respect throttling and recover the page after repeated errors.
            delay = max(settings.interval, min(15, 2 ** min(failures, 4)), getattr(error, "delay", 0))
            wake = min(settings.end, datetime.now(MSK) + timedelta(seconds=delay))
            log(f"Сбой безопасного запроса до отправки заявки (подряд: {failures}): {reason} Следующая проверка не раньше {wake:%H:%M:%S} МСК; окно до {settings.end:%H:%M:%S}.")
            emit("retry", {"reason": reason, "next": wake.isoformat(), "count": failures})
            need_load = failures >= 2
            wait(wake)
            continue
        wait(min(settings.end, datetime.now(MSK) + timedelta(seconds=settings.interval)))
    log("Время ожидания истекло. Подтверждённой записи нет.")
    return "no-slots"


def guarded_doctor(page, config):
    locator = page.locator(f'.js-doctor[data-doctor-id="{config["doctor_id"]}"]')
    locator.wait_for(state="visible", timeout=20_000)
    if locator.count() != 1 or normalized(locator.get_attribute("data-doctor-name") or "") != normalized(config["doctor_name_contains"]):
        raise StopBooking("Карточка врача не совпадает с выбранным ФИО.")
    return locator


@contextmanager
def awake():
    process = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield
    finally:
        process.terminate()
        process.wait()


def run(settings, destination):
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = identity_key(settings)
    marker = destination / f"заявка-{key}.json"
    with os.fdopen(os.open(destination / f"запуск-{key}.lock", os.O_CREAT | os.O_WRONLY, 0o600), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopBooking("Такой запуск уже работает. Используйте открытое окно.")
        if settings.commit:
            check_previous(marker, datetime.now(MSK))
        journal = Journal(destination / datetime.now(MSK).strftime("журнал-%Y%m%d-%H%M%S-%f.txt"))
        booking.log = journal
        booking.doctor_locator = guarded_doctor
        try:
            journal(f"Режим: {'НАСТОЯЩАЯ ЗАПИСЬ' if settings.commit else 'ПРОВЕРКА без отправки заявки'}.")
            journal(f"Начало проверок {max(settings.start, datetime.now(MSK)):%d.%m.%Y %H:%M:%S}; конец {settings.end:%d.%m.%Y %H:%M:%S}.")
            journal("Не закрывайте терминал и окно автоматизации. Для остановки — Ctrl+C или закрытие браузера.")
            with awake():
                prepare = settings.start - timedelta(minutes=3)
                while datetime.now(MSK) < prepare:
                    time.sleep(min(1, (prepare - datetime.now(MSK)).total_seconds()))
                journal("Открываю встроенный браузер.")
                unpack_browser()
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=False, channel="chromium")
                    try:
                        context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow", viewport={"width":1280,"height":900})
                        if not settings.commit:
                            context.route("**/v2/appointment/create", lambda route: route.abort())
                        page = context.new_page()
                        page.set_default_timeout(20_000)
                        outcome = monitor(page, settings, marker, journal)
                        if outcome == "confirmed":
                            try:
                                booking.save_confirmation(page, marker)
                            except Exception:
                                journal("Запись подтверждена сервером, но скриншот не сохранён. Сохраните талон из открытого браузера.")
                            print("\a", end="", flush=True)
                        if outcome in ("confirmed", "dry-run"):
                            journal("Окно оставлено открытым. После сохранения талона или проверки закройте его.")
                            while browser.is_connected() and not page.is_closed():
                                try:
                                    page.wait_for_timeout(500)
                                except BrowserError:
                                    break
                        return 0 if outcome in ("confirmed", "dry-run") else 2
                    finally:
                        browser.close()
        except Exception as error:
            safe = str(error) if isinstance(error, (StopBooking, RuntimeError)) and not isinstance(error, BrowserError) else type(error).__name__
            journal(f"Остановка: {safe}")
            if marker.exists():
                journal("Перед повторным запуском проверьте состояние заявки в файле результата и на сайте.")
            raise
        finally:
            journal.close()


def self_test():
    """Check bundled browser, runtime and interception without accessing any website."""
    unpack_browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chromium")
        try:
            context = browser.new_context()
            context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<h1>Проверка встроенного браузера</h1>"))
            page = context.new_page()
            page.goto("https://offline-test.invalid/")
            assert page.locator("h1").inner_text() == "Проверка встроенного браузера"
            print("SELF-TEST OK: Python, Playwright и Chromium работают без внешних пакетов. Сеть не использовалась.", flush=True)
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description="Запись к врачу на gorzdrav.spb.ru — локальный инструмент для macOS")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--self-test", action="store_true", help="проверить встроенный браузер без обращения к сайту")
    parser.add_argument("--check-config", action="store_true", help="проверить конфиг без запуска браузера")
    parser.add_argument("--no-pause", action="store_true", help="не ожидать Enter при завершении")
    args = parser.parse_args()
    status = 0
    try:
        if args.self_test:
            self_test()
            return 0
        location = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        config_path = (args.config or location / "config.txt").resolve()
        settings = parse_config(config_path)
        if args.check_config:
            print(f"Конфигурация корректна. Начало: {settings.start:%d.%m.%Y %H:%M:%S} МСК. Режим: {'запись' if settings.commit else 'проверка'}.")
            return 0
        status = run(settings, config_path.parent / "Результаты")
    except (ConfigError, StopBooking) as error:
        print(f"\n{error}", flush=True)
        status = 1
    except KeyboardInterrupt:
        print("\nОстановлено пользователем. Если заявка уже отправлялась, проверьте результат на сайте.", flush=True)
        status = 130
    except Exception as error:
        print(f"\nОшибка: {type(error).__name__}. Подробности без личных данных — в папке Результаты.", flush=True)
        status = 1
    finally:
        if not args.no_pause and not args.self_test and not args.check_config and sys.stdin.isatty():
            try:
                input("\nНажмите Enter, чтобы закрыть окно… ")
            except (KeyboardInterrupt, EOFError):
                pass
    return status


if __name__ == "__main__":
    raise SystemExit(main())
