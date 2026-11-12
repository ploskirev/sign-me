"""Offline integration tests for the desktop worker and form."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import booking
import gui_backend
from gui_fixture import Website
import talon


class DesktopWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.cancel = threading.Event()
        now = datetime.now(talon.MSK)
        self.settings = talon.Settings({'lastName':'Тестовый','firstName':'Пациент','middleName':'','birthdate':'01.01.1990'},
            'Тестовый Доктор',{'district':'8','lpu':'172','speciality':'49'},now,now,now+timedelta(minutes=2),2,True)
        self.stages = []

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, website):
        return gui_backend.execute(self.settings,self.directory,self.cancel,
            lambda stage,data:self.stages.append(stage),lambda line:None,browser_setup=website.setup,clock_prepare=False)

    def test_server_retry_then_official_receipt(self):
        website = Website()
        result = self.execute(website)
        self.assertEqual(result['status'],'confirmed',result)
        self.assertIn('retry',self.stages)
        self.assertEqual(website.creations,1)
        self.assertEqual(result['pdf_kind'],'official')
        self.assertEqual(Path(result['pdf']).read_bytes(),website.pdf)
        self.assertEqual(result['details']['address'],'Тестовая улица, 1')
        self.assertEqual(result['details']['time'],'10:00')

    def test_cancellation_before_browser_starts(self):
        self.cancel.set()
        website = Website()
        result = self.execute(website)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(website.creations,0)
        self.assertNotIn('preparing',self.stages)

    def test_cancellation_after_patient_check_prevents_submission(self):
        website = Website(failures=0,on_search=self.cancel.set)
        result = self.execute(website)
        self.assertEqual(result['status'],'cancelled',result)
        self.assertEqual(website.creations,0)

    def test_cancellation_after_submission_keeps_confirmation(self):
        website = Website(failures=0,on_create=self.cancel.set)
        result = self.execute(website)
        self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(website.creations,1)

    def test_invalid_download_uses_labelled_copy(self):
        result = self.execute(Website(failures=0,pdf_error=True))
        self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(result['pdf_kind'],'copy')
        self.assertTrue(Path(result['pdf']).read_bytes().startswith(b'%PDF-'))
        self.assertIn('PDF-копия',result['warning'])

    def test_failed_exports_never_undo_confirmation(self):
        real_write = booking.private_json
        def write(path,*args,**kwargs):
            if path.name == 'результат.json':
                raise OSError('disk full')
            return real_write(path,*args,**kwargs)
        with patch.object(gui_backend,'download_official_pdf',side_effect=OSError), \
             patch.object(gui_backend,'confirmation_copy',side_effect=OSError), \
             patch.object(booking,'private_json',side_effect=write):
            result = self.execute(Website(failures=0))
        self.assertEqual(result['status'],'confirmed',result)
        self.assertEqual(result['pdf'],'')
        self.assertIn('PDF не удалось',result['warning'])

    def test_prior_uncertain_submission_is_not_retried(self):
        marker = self.directory / f'заявка-{talon.identity_key(self.settings)}.json'
        marker.write_text(json.dumps({'status':'submission_started'}))
        website = Website()
        result = self.execute(website)
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(website.creations,0)
        self.assertEqual(website.reads,0)


class DesktopFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_form_validation_and_error_display(self):
        from talon_gui import MainWindow
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(directory)
            window.start_booking()
            self.assertIsNone(window.worker)
            self.assertIn('Фамилия',window.form_error.text())
            window.on_result({'status':'uncertain','reason':'Проверьте результат на сайте'})
            self.assertEqual(window.result_title.text(),'Результат нужно проверить')
            self.assertTrue(window.pdf_button.isHidden())
            window.close()

    def test_import_and_parse_without_id(self):
        from talon_gui import MainWindow,settings_from_values
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.txt'
            path.write_text('[Пациент]\nФамилия=Тестовый\nИмя=Пациент\nДата рождения=01.01.1990\n'
                '[Запись]\nВрач=Тестовый Доктор\nСтраница='+booking.booking_url({'district':'8','lpu':'172','speciality':'49'})+
                '\nВремя выдачи=сейчас\nРежим=запись\nМинут ожидания=10\nИнтервал секунд=4\n')
            window = MainWindow(directory)
            window.load_config(path)
            settings = settings_from_values(window.values())
            self.assertEqual(settings.doctor,'Тестовый Доктор')
            self.assertTrue(settings.commit)
            self.assertEqual(settings.interval,4)
            window.close()


if __name__ == '__main__':
    unittest.main()
