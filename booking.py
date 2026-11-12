"""Timed browser booking. No confirmation is sent without --commit."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).parent / ".browsers"))

from playwright.sync_api import Error as BrowserError, sync_playwright

MSK = ZoneInfo("Europe/Moscow")
ROOT = Path(__file__).resolve().parent


class ReadRetry(Exception):
    """A temporary failure of a read operation, safe to repeat before submission."""

    def __init__(self, reason, delay=0):
        super().__init__(reason)
        self.delay = delay


class WindowExpired(Exception):
    pass


class BookingCancelled(Exception):
    pass


def ensure_active(deadline=None, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise BookingCancelled("Остановлено пользователем до отправки заявки.")
    ensure_window(deadline)


def ensure_window(deadline):
    if deadline is not None and datetime.now(MSK) >= deadline:
        raise WindowExpired("Окно записи завершилось до отправки заявки.")


def retry_after_seconds(value, now=None):
    """Honor either Retry-After seconds or an HTTP date, without retrying early."""
    try:
        return max(15, int(value))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError()
            return max(15, int((date - (now or datetime.now(MSK))).total_seconds()) + 1)
        except (ValueError, TypeError, OverflowError):
            return 15


def patient_result(response):
    status = response.status
    if status == 429:
        raise ReadRetry("Сайт ограничил частоту проверки пациента (HTTP 429).",
                        retry_after_seconds(response.headers.get("retry-after")))
    if status in (0, 408, 425) or 500 <= status <= 599:
        raise ReadRetry(f"Проверка пациента временно недоступна: HTTP {status}.")
    if status != 200:
        raise RuntimeError(f"Проверка пациента остановлена: HTTP {status}. Требуется ручная проверка.")
    try:
        found = response.json()
    except (ValueError, BrowserError):
        raise ReadRetry("Проверка пациента вернула неполный или некорректный JSON.")
    if not isinstance(found, dict) or type(found.get("success")) is not bool:
        raise ReadRetry("Изменился или повреждён формат ответа проверки пациента.")
    if found["success"] is not True or not found.get("result"):
        # A definite refusal/not-found result is not evidence of server overload.
        raise RuntimeError("Сайт не подтвердил пациента; запись остановлена")
    return found


def log(message):
    print(f"{datetime.now(MSK):%Y-%m-%d %H:%M:%S} МСК — {message}", flush=True)


def validate_config(data):
    for key in ("district", "lpu", "speciality", "doctor_id"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(data.get(key, ""))):
            raise ValueError(f"Некорректное поле {key}")
        data[key] = str(data[key])
    if not isinstance(data.get("doctor_name_contains"), str) or not data["doctor_name_contains"].strip():
        raise ValueError("Нужен фрагмент имени врача")
    patient = data.get("patient", {})
    for key in ("lastName", "firstName", "birthdate"):
        if not isinstance(patient.get(key), str) or not patient[key].strip():
            raise ValueError(f"Не заполнено поле пациента {key}")
        if "[MASKED:" in patient[key]:
            raise ValueError(f"Поле пациента {key} скрыто маской; нужен локальный ввод")
    birth = datetime.strptime(patient["birthdate"], "%d.%m.%Y").date()
    if birth > datetime.now(MSK).date():
        raise ValueError("Дата рождения находится в будущем")
    for key in ("middleName", "email", "phone"):
        if not isinstance(patient.get(key, ""), str):
            raise ValueError(f"Некорректное поле пациента {key}")
    return data


def booking_url(config):
    route = [{key: config[key]} for key in ("district", "lpu", "speciality")]
    return "https://gorzdrav.spb.ru/service-free-schedule#" + quote(json.dumps(route, separators=(",", ":")), safe=",:")


def private_json(path, data, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    with os.fdopen(os.open(path, flags, 0o600), "w") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())


def doctor_locator(page, config):
    doctor = page.locator(f'.js-doctor[data-doctor-id="{config["doctor_id"]}"]')
    doctor.wait_for(state="visible", timeout=30_000)
    if doctor.count() != 1 or config["doctor_name_contains"].casefold() not in (doctor.get_attribute("data-doctor-name") or "").casefold():
        raise RuntimeError("Врач не совпадает с настройками")
    return doctor


def poll_count(page, config):
    # Use the site's normal client, including its ordinary session/token handling.
    data = page.evaluate("""async ({lpu, speciality, doctor_id}) => {
        const client = new HealthcarePortalClient(API_HOST);
        const result = await Promise.race([
            client.getDoctors(lpu, speciality),
            new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 15000))
        ]);
        if (result.success !== true || !Array.isArray(result.result))
            return {error: 'schedule'};
        const matches = result.result.filter(d => String(d.id) === doctor_id);
        if (matches.length !== 1) return {error: 'doctor'};
        const d = matches[0];
        return {name:d.name, count:d.freeTicketCount, participant:d.freeParticipantCount};
    }""", {key: config[key] for key in ("lpu", "speciality", "doctor_id")})
    if data.get("error") or config["doctor_name_contains"].casefold() not in data.get("name", "").casefold():
        raise RuntimeError("Не удалось проверить врача по расписанию")
    counts = [data.get("count"), data.get("participant")]
    if any(type(value) is not int or value < 0 for value in counts):
        raise RuntimeError("Изменился формат доступности талонов")
    return max(counts)


def matches_response(response, path, method="POST"):
    return urlsplit(response.url).path.endswith(path) and response.request.method == method


def payload_matches(payload, config, ticket, patient_id):
    patient = config["patient"]
    expected = {
        "lpuId": config["lpu"], "appointmentId": ticket, "patientId": patient_id,
        "patientLastName": patient["lastName"], "patientFirstName": patient["firstName"],
        "patientMiddleName": patient.get("middleName", ""),
        "patientBirthdate": datetime.strptime(patient["birthdate"], "%d.%m.%Y").strftime("%Y-%m-%dT00:00:00"),
    }
    return all(str(payload.get(key, "")) == str(value) for key, value in expected.items())


def slot_taken(result):
    if result.get("success") is not False:
        return False
    message = str(result.get("message") or "").casefold()
    return bool(re.search(r"(?:талон|номерок|слот).{0,80}(?:уже занят|занят другим|уже забронирован|недоступен)", message))


def save_confirmation(page, marker):
    final = page.locator(".service-tabs__item--8")
    final.wait_for(state="visible", timeout=20_000)
    final.get_by_text("Вы успешно записаны на прием к врачу.", exact=False).wait_for(state="visible")
    # The site animates both opacity and scrolling after confirmation.
    # Waiting here does not slow down acquiring the appointment itself.
    page.wait_for_timeout(1_200)
    image_path = marker.with_suffix(".png")
    screenshot = page.screenshot(full_page=True, animations="disabled")
    with os.fdopen(os.open(image_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(screenshot)
    log(f"Скриншот подтверждения сохранён: {image_path}")
    return image_path


def book_from_page(page, config, commit, marker, excluded=None, deadline=None, cancel_event=None, progress=None):
    emit = progress or (lambda stage, detail: None)
    ensure_active(deadline, cancel_event)
    if excluded is None:
        excluded = set()
    doctor = doctor_locator(page, config)
    choose = doctor.locator(".js-doctor-toggler")
    if choose.count() != 1:
        return "unavailable"
    choose.click()
    doctor.locator(".js-slot:visible").first.wait_for(state="visible", timeout=20_000)
    def available_slot():
        for candidate in doctor.locator(".js-slot:visible").all():
            if candidate.get_attribute("data-ticket-id") not in excluded:
                return candidate
        return None
    slot = available_slot()
    if slot is None:
        # Try other available dates in the currently displayed calendar.
        dates = doctor.locator(".datepicker-slot").evaluate_all("elements => elements.map(e => e.dataset.date).filter(Boolean)")
        for date in dict.fromkeys(dates):
            if not date.isdigit():
                continue
            doctor.locator(f'.datepicker-slot[data-date="{date}"]').click()
            slot = available_slot()
            if slot is not None:
                break
    if slot is None:
        return "unavailable"
    ticket = slot.get_attribute("data-ticket-id")
    visit = slot.get_attribute("data-visit-start")
    if not ticket or not visit:
        raise RuntimeError("У талона отсутствует идентификатор или время")
    log(f"Выбран талон на {visit}")
    emit("selecting", {"visit": visit})
    slot.click()
    doctor.locator(".service-doctor__sign-up").click()
    form = page.locator("#checkPatientForm")
    form.wait_for(state="visible")
    log("Открыта форма пациента.")
    emit("patient", {})
    patient = config["patient"]
    for name in ("lastName", "firstName", "birthdate"):
        form.locator(f'[name="{name}"]').fill(patient[name])
    for name in ("middleName", "email", "phone"):
        if patient.get(name):
            form.locator(f'[name="{name}"]').fill(patient[name])
    # These are the site's ordinary processing terms, required for the authorized booking.
    form.locator("label.agreement").click()
    log(f"Форма заполнена; корректность обязательных полей: {form.evaluate('(form) => form.checkValidity()')}.")
    ensure_active(deadline, cancel_event)
    with page.expect_response(lambda r: matches_response(r, "/v2/patient/search", "GET"), timeout=30_000) as checked:
        form.locator(".service-form__submit").click()
    response = checked.value
    log(f"Получен ответ проверки пациента: HTTP {response.status}.")
    ensure_active(deadline, cancel_event)
    found = patient_result(response)
    confirm = page.locator("#setAppointmentButton")
    confirm.wait_for(state="visible")
    log("Открыт шаг подтверждения записи.")
    emit("confirming", {})
    # Ensure the originally selected ticket is still represented by the doctor card.
    if doctor.get_attribute("data-ticket-id") != ticket:
        raise RuntimeError("Выбранный талон изменился")
    if not commit:
        log("Репетиция дошла до подтверждения. Запись НЕ отправлена.")
        return "dry-run"
    attempts = []
    blocked_by_deadline = False
    blocked_by_cancel = False

    def guard_submission(route):
        nonlocal blocked_by_deadline, blocked_by_cancel
        if cancel_event is not None and cancel_event.is_set():
            blocked_by_cancel = True
            route.abort()
            return
        if deadline is not None and datetime.now(MSK) >= deadline:
            blocked_by_deadline = True
            route.abort()
            return
        request = route.request
        payload = request.post_data_json if request.method == "POST" else None
        if attempts or not isinstance(payload, dict) or not payload_matches(payload, config, ticket, found["result"]):
            route.abort()
            return
        attempts.append(True)
        route.fallback()

    page.route("**/v2/appointment/create", guard_submission)
    confirm.locator("xpath=ancestor::div[contains(@class,'js-agreement-wrapper')]").locator("label.agreement").click()
    ensure_active(deadline, cancel_event)
    # Persist before the click. An uncertain outcome must never cause an automatic resend.
    existing = json.loads(marker.read_text()) if marker.exists() else None
    if existing and existing.get("status") != "rejected_slot":
        raise FileExistsError("Предыдущая отправка требует проверки")
    state = {"status": "submission_started", "doctor_id": config["doctor_id"], "visit": visit,
             "ticket": ticket, "excluded": sorted(excluded)}
    private_json(marker, state, exclusive=existing is None)
    log("Отправляю запись один раз.")
    emit("submitting", {})
    try:
        # A request that was sent within the window may finish after the deadline.
        with page.expect_response(lambda r: matches_response(r, "/v2/appointment/create"), timeout=45_000) as sent:
            timeout = 20_000 if deadline is None else max(1, min(20_000, int((deadline - datetime.now(MSK)).total_seconds() * 1000)))
            confirm.click(timeout=timeout)
    except BrowserError:
        if not attempts and (blocked_by_cancel or (cancel_event is not None and cancel_event.is_set())):
            private_json(marker, {**state, "status": "not_sent"})
            raise BookingCancelled("Запрос не отправлен. Остановлено пользователем.")
        if not attempts and (blocked_by_deadline or (deadline is not None and datetime.now(MSK) >= deadline)):
            private_json(marker, {**state, "status": "not_sent"})
            raise WindowExpired("Окно истекло до отправки запроса; заявка не отправлена.")
        raise
    response = sent.value
    result = response.json()
    if response.status != 200 or result.get("success") is not True:
        if response.status == 200 and slot_taken(result):
            excluded.add(ticket)
            private_json(marker, {**state, "status": "rejected_slot", "excluded": sorted(excluded)})
            page.unroute("**/v2/appointment/create", guard_submission)
            log("Сайт сообщил, что талон занят. Выбираю другой.")
            return "retry"
        private_json(marker, {**state, "status": "not_confirmed"})
        raise RuntimeError("Запись не подтверждена; повторная отправка отключена")
    private_json(marker, {**state, "status": "confirmed"})
    log(f"Сервер подтвердил запись: врач {config['doctor_id']}, приём {visit}.")
    emit("confirmed", {"visit": visit})
    return "confirmed"


def wait_until(moment):
    while (seconds := (moment - datetime.now(MSK)).total_seconds()) > 0:
        time.sleep(min(seconds, 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--at", help="YYYY-MM-DDTHH:MM:SS по Москве; без параметра начать сейчас")
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--commit", action="store_true", help="разрешить одну настоящую запись")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--keep-open", action="store_true", help="оставить окно после подтверждения до закрытия пользователем")
    args = parser.parse_args()
    if args.keep_open and not args.headed:
        parser.error("--keep-open требует --headed")
    if not 1 <= args.minutes <= 30:
        parser.error("Длительность должна быть от 1 до 30 минут")
    config = validate_config(json.loads(args.config.read_text()))
    start = datetime.now(MSK)
    if args.at:
        start = datetime.strptime(args.at, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=MSK)
        if start <= datetime.now(MSK):
            parser.error("Время начала уже прошло")
    end = start + timedelta(minutes=args.minutes)
    identity = [config["lpu"], config["doctor_id"], *[config["patient"].get(k, "").strip().casefold() for k in ("lastName", "firstName", "middleName", "birthdate")]]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    marker = ROOT / "private" / f"submission-{key}.json"
    excluded = set()
    if args.commit and marker.exists():
        previous = json.loads(marker.read_text())
        if previous.get("status") != "rejected_slot":
            raise RuntimeError("Сохранён результат предыдущей отправки. Сначала проверьте существующую запись")
        excluded.update(previous.get("excluded", []))
    # Hold a process-wide lock throughout the wait, polling, and submission.
    import fcntl
    lock_path = ROOT / "private" / f"run-{key}.lock"
    lock_path.parent.mkdir(mode=0o700, exist_ok=True)
    with os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log(f"Запуск подготовлен на {start:%Y-%m-%d %H:%M:%S}; режим: {'запись' if args.commit else 'репетиция'}.")
        wait_until(start - timedelta(minutes=3))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed, channel="chromium")
            try:
                context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
                if not args.commit:
                    context.route("**/v2/appointment/create", lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(20_000)
                page.goto(booking_url(config), wait_until="domcontentloaded", timeout=60_000)
                doctor_locator(page, config)
                log("Страница и врач проверены. Ожидаю начало мониторинга.")
                wait_until(start)
                while datetime.now(MSK) < end:
                    count = poll_count(page, config)
                    log(f"Свободных талонов по списку врачей: {count}")
                    if count:
                        page.reload(wait_until="domcontentloaded", timeout=60_000)
                        status = book_from_page(page, config, args.commit, marker, excluded)
                        if status == "confirmed":
                            try:
                                save_confirmation(page, marker)
                            except Exception:
                                log("Сервер подтвердил запись, но скриншот сохранить не удалось.")
                            if args.keep_open:
                                log("Окно оставлено открытым для печати. Закройте его после сохранения талона.")
                                while browser.is_connected() and not page.is_closed():
                                    try:
                                        page.wait_for_timeout(1_000)
                                    except Exception:
                                        break
                            return 0
                        if status == "dry-run":
                            return 0
                        if status == "retry":
                            continue
                    page.wait_for_timeout(5_000)
                log("Окно мониторинга завершено; запись не отправлялась.")
                return 2
            except Exception as error:
                import traceback
                frames = traceback.extract_tb(error.__traceback__)
                local_lines = [frame.lineno for frame in frames if Path(frame.filename).name == 'booking.py']
                log(f"Диагностика: строки сценария {local_lines}.")
                try:
                    image_path = ROOT / "private" / datetime.now(MSK).strftime('error-%Y%m%d-%H%M%S.png')
                    with os.fdopen(os.open(image_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                        output.write(page.screenshot(full_page=True))
                    log(f"Экран ошибки сохранён локально: {image_path}")
                except Exception:
                    pass
                raise
            finally:
                browser.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Остановлено пользователем.")
    except Exception as error:
        # Playwright error strings can contain the values passed to fill().
        log(f"Остановка из-за ошибки ({type(error).__name__}). Автоповтор отправки отключён. Проверьте результат записи перед перезапуском.")
        if type(error) is RuntimeError:
            log(str(error))
        raise SystemExit(1)
