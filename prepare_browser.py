"""Open a fresh local browser for a manual walkthrough of the booking form."""

import os
from pathlib import Path

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).parent / ".browsers"))

from playwright.sync_api import Error, sync_playwright


URL = (
    "https://gorzdrav.spb.ru/service-free-schedule"
    "#%5B%7B%22district%22:%228%22%7D,"
    "%7B%22lpu%22:%22172%22%7D,"
    "%7B%22speciality%22:%2249%22%7D%5D"
)


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        page = context.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        except Error:
            print("Не удалось загрузить страницу. Проверьте сообщение в браузере и соединение.")
        print("Подготовительный режим: сценарий не нажимает кнопки и не отправляет запись.")
        print("Пройдите вход вручную, выберите пациента и нужного врача.")
        print("Не подтверждайте запись во время пробного прохода.")
        print("Пароли, содержимое страницы и снимки экрана сценарий не сохраняет.")
        try:
            input("После проверки нажмите Enter здесь, чтобы закрыть браузер: ")
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
