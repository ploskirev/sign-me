"""Inspect public booking controls without entering or submitting patient data."""

from booking import booking_url, sync_playwright


def main():
    config = {"district": "8", "lpu": "172", "speciality": "48"}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chromium")
        page = browser.new_page(locale="ru-RU", timezone_id="Europe/Moscow")
        page.on('pageerror', lambda error: print('Page script error:', str(error)[:300], flush=True))
        page.set_default_timeout(20_000)
        page.goto(booking_url(config), wait_until="domcontentloaded")
        doctor = page.locator('.js-doctor[data-doctor-id="1562"]')
        try:
            doctor.wait_for(state="visible")
        except Exception:
            print('Page title:',page.title(), flush=True)
            print('Doctor count:',page.locator('.js-doctor').count(), flush=True)
            print('Body:',page.locator('body').inner_text()[-2000:], flush=True)
            raise
        doctor.locator('.js-doctor-toggler').click()
        slot = doctor.locator('.js-slot:visible').first
        slot.wait_for(state="visible")
        print('Visible slots:', doctor.locator('.js-slot:visible').count(), flush=True)
        slot.click()
        doctor.locator('.service-doctor__sign-up').click()
        form = page.locator('#checkPatientForm')
        form.wait_for(state="visible")
        print('Patient form visible:', True, flush=True)
        for name in ('lastName','firstName','birthdate'):
            print('Field available:', name, form.locator(f'[name="{name}"]').is_visible(), flush=True)
        print('Consent label count:', form.locator('label.agreement').count(), flush=True)
        print('Submit button count:', form.locator('.service-form__submit').count(), flush=True)
        print('No patient data entered or submitted.', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
