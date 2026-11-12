"""Headless booking worker and receipt export; no GUI objects live in this module."""

from datetime import datetime, timedelta
import fcntl
import html
import json
import os
from pathlib import Path

import booking
import talon
from playwright.sync_api import Error as BrowserError, sync_playwright


def secure_bytes(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as output:
        output.write(data)
    path.chmod(0o600)


def receipt_details(page, settings, marker):
    state = json.loads(marker.read_text())
    details = {"doctor": settings.doctor, "visit": state.get("visit", ""),
               "speciality": "", "clinic": "", "address": "", "phone": "", "room": ""}
    final = page.locator(".service-tabs__item--8")
    final.wait_for(state="visible", timeout=20_000)
    card = final.locator(".service-block-2__col--first").first
    def text(selector):
        element = card.locator(selector)
        return element.first.inner_text().strip() if element.count() else ""
    details.update(doctor=text(".service-block-2__doctor") or settings.doctor,
                   speciality=text(".service-block-2__title"), clinic=text(".service-block-2__mo"),
                   phone=text('a[href^="tel:"]'))
    address = page.locator('#pdfForm input[name="address"]')
    if address.count():
        details["address"] = address.input_value().strip()
    if not details["address"]:
        item = card.locator('.service-block-2-list__item').filter(has=page.locator('img[src*="marker"]'))
        if item.count():
            details["address"] = item.first.inner_text().strip()
    room = page.locator('#pdfForm input[name="room"]')
    if room.count():
        details["room"] = room.input_value().strip()
    try:
        visit = datetime.fromisoformat(details["visit"])
        details["date"] = visit.strftime("%d.%m.%Y")
        details["time"] = visit.strftime("%H:%M")
    except ValueError:
        details["date"] = details["time"] = ""
    return details


def download_official_pdf(page, target):
    with page.expect_download(timeout=30_000) as pending:
        page.locator("#savePdfBtn").click()
    download = pending.value
    if download.failure():
        raise RuntimeError("Сайт не передал PDF талона.")
    data = Path(download.path()).read_bytes()
    if not data.startswith(b"%PDF-"):
        raise RuntimeError("Сайт вернул файл другого формата вместо PDF.")
    secure_bytes(target, data)


def confirmation_copy(context, target, details, patient):
    """A clearly labelled printable copy when the website's PDF endpoint is unavailable."""
    rows = [("Пациент", " ".join(patient.get(k, "") for k in ("lastName", "firstName", "middleName")).strip()),
            ("Дата рождения", patient["birthdate"]), ("Дата", details.get("date", "")),
            ("Время", details.get("time", "")), ("Врач", details.get("doctor", "")),
            ("Специальность", details.get("speciality", "")), ("Медучреждение", details.get("clinic", "")),
            ("Адрес", details.get("address", "")), ("Кабинет", details.get("room", "")),
            ("Телефон", details.get("phone", ""))]
    body = "".join(f"<tr><th>{html.escape(label)}</th><td>{html.escape(value or 'Не указано на сайте')}</td></tr>" for label, value in rows)
    document = """<!doctype html><html lang="ru"><meta charset="utf-8"><style>
        body{font:15px Arial,sans-serif;color:#16302e;padding:30px}h1{font-size:25px}
        table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #ddd}
        th{width:32%;font-weight:normal;color:#536765}p{color:#536765;font-size:12px}
        </style><h1>Подтверждение записи к врачу</h1><table>""" + body + """</table>
        <p>Копия подтверждения, сформированная приложением по данным успешной записи на gorzdrav.spb.ru.
        Сайт не предоставил исходный PDF талона.</p></html>"""
    preview = context.new_page()
    try:
        preview.set_content(document)
        secure_bytes(target, preview.pdf(format="A4", print_background=True))
    finally:
        preview.close()


def safe_reason(error, marker):
    if marker.exists():
        try:
            state = json.loads(marker.read_text())
            if state.get("status") in ("submission_started", "not_confirmed"):
                return "uncertain", "Сайт не подтвердил результат отправленной заявки. Проверьте запись на сайте перед новой попыткой. Автоматический повтор отключён."
        except (OSError, ValueError):
            return "uncertain", "Не удалось проверить результат предыдущей заявки. Проверьте запись на сайте."
    if isinstance(error, booking.BookingCancelled):
        return "cancelled", "Процесс остановлен. Новая заявка не отправлена."
    if isinstance(error, (talon.StopBooking, talon.ConfigError, RuntimeError)) and not isinstance(error, BrowserError):
        return "failed", str(error)
    return "failed", "Не удалось завершить работу с сайтом. Проверьте интернет и повторите попытку; подробности сохранены в журнале."


def execute(settings, destination, cancel_event, progress, log_event, legacy_directory=None, browser_setup=None, clock_prepare=True):
    """Runs entirely on a worker thread. browser_setup is used only by offline tests."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = talon.identity_key(settings)
    marker = destination / f"заявка-{key}.json"
    run_dir = destination / datetime.now(talon.MSK).strftime("запуск-%Y%m%d-%H%M%S-%f")
    run_dir.mkdir(mode=0o700)
    journal = os.fdopen(os.open(run_dir / "журнал.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8")
    last_retry = ""
    def log(message):
        line = f"{datetime.now(talon.MSK):%H:%M:%S} — {message}"
        journal.write(line + "\n")
        journal.flush()
        log_event(line)
    def emit(stage, detail):
        nonlocal last_retry
        if stage == "retry":
            last_retry = detail.get("reason", "")
        elif stage == "monitoring":
            last_retry = ""
        progress(stage, detail)
    old_log, old_locator = booking.log, booking.doctor_locator
    booking.log, booking.doctor_locator = log, talon.guarded_doctor
    confirmed_result = None
    try:
        with os.fdopen(os.open(destination / f"запуск-{key}.lock", os.O_CREAT | os.O_WRONLY, 0o600), "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise talon.StopBooking("Такая запись уже выполняется в другом окне приложения.")
            if settings.commit:
                # Import the matching old CLI state to preserve duplicate protection when upgrading.
                legacy = Path(legacy_directory) / marker.name if legacy_directory else None
                if legacy and legacy.exists() and not marker.exists() and legacy.resolve() != marker.resolve():
                    booking.private_json(marker, json.loads(legacy.read_text()), exclusive=True)
                talon.check_previous(marker, datetime.now(talon.MSK))
            emit("waiting", {"start": settings.start.isoformat(), "release": settings.release.isoformat()})
            log(f"Начало проверок: {settings.start:%d.%m.%Y %H:%M:%S} МСК; конец: {settings.end:%H:%M:%S}.")
            with talon.awake():
                if clock_prepare:
                    prepare = settings.start - timedelta(minutes=3)
                    while datetime.now(talon.MSK) < prepare:
                        if cancel_event.wait(min(1, max(0, (prepare-datetime.now(talon.MSK)).total_seconds()))):
                            raise booking.BookingCancelled()
                booking.ensure_active(cancel_event=cancel_event)
                emit("preparing", {})
                talon.unpack_browser()
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True, channel="chromium")
                    try:
                        context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow", accept_downloads=True,
                                                      viewport={"width":1280,"height":900})
                        if browser_setup:
                            browser_setup(context)
                        if not settings.commit:
                            context.route("**/v2/appointment/create", lambda route: route.abort())
                        page = context.new_page()
                        page.set_default_timeout(20_000)
                        outcome = talon.monitor(page, settings, marker, log, cancel_event, emit)
                        if outcome == "no-slots":
                            reason = "Время ожидания закончилось. Запись не подтверждена."
                            if last_retry:
                                reason += " Сайт отвечал с ошибками: " + last_retry
                            return {"status":"failed", "reason":reason, "folder":str(run_dir)}
                        if outcome == "dry-run":
                            return {"status":"checked", "reason":"Проверка дошла до финальной сводки. Настоящая запись не создавалась.", "folder":str(run_dir)}
                        # Confirmation has already been persisted. Export errors must not turn success into failure.
                        confirmed_result = {"status":"confirmed", "details":{"doctor":settings.doctor}, "folder":str(run_dir),
                                            "warning":"Запись подтверждена. Не все сведения удалось сохранить; проверьте талон на сайте."}
                        state = json.loads(marker.read_text())
                        details = {"doctor":settings.doctor,"visit":state.get("visit", "")}
                        try:
                            visit = datetime.fromisoformat(details["visit"])
                            details.update(date=visit.strftime("%d.%m.%Y"),time=visit.strftime("%H:%M"))
                        except ValueError:
                            pass
                        warnings = []
                        emit("exporting", {})
                        try:
                            details = receipt_details(page, settings, marker)
                        except Exception:
                            warnings.append("Часть сведений о приёме не удалось получить с сайта.")
                        try:
                            page.wait_for_timeout(1_200)
                            secure_bytes(run_dir / "подтверждение.png", page.screenshot(full_page=True, animations="disabled"))
                        except Exception:
                            log("Скриншот подтверждения не сохранён.")
                        pdf = run_dir / "талон.pdf"
                        pdf_kind = "official"
                        try:
                            download_official_pdf(page, pdf)
                        except Exception:
                            pdf_kind = "copy"
                            try:
                                confirmation_copy(context, pdf, details, settings.patient)
                                warnings.append("Сайт не предоставил исходный PDF. Доступна PDF-копия подтверждения записи.")
                            except Exception:
                                warnings.append("PDF не удалось сохранить. Запись подтверждена; талон можно получить на сайте.")
                        result = {"status":"confirmed", "details":details, "pdf":str(pdf) if pdf.exists() else "",
                                  "pdf_kind":pdf_kind, "warning":" ".join(warnings), "folder":str(run_dir)}
                        confirmed_result = result
                        try:
                            booking.private_json(run_dir / "результат.json", result, exclusive=True)
                        except OSError:
                            result['warning'] = (result['warning'] + ' Не удалось сохранить файл результата.').strip()
                        return result
                    finally:
                        try:
                            browser.close()
                        except BrowserError:
                            pass
    except Exception as error:
        if confirmed_result is not None:
            return confirmed_result
        status, reason = safe_reason(error, marker)
        log(f"Завершение: {reason}")
        return {"status":status,"reason":reason,"folder":str(run_dir)}
    finally:
        booking.log, booking.doctor_locator = old_log, old_locator
        journal.close()
