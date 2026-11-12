import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import talon
from talon import MSK, ConfigError, ScheduleRetry, StopBooking, Settings


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'config.txt'
        self.text = (Path(__file__).parent / 'portable/config.txt').read_text()
        self.text = self.text.replace('Фамилия =\n', 'Фамилия = Примеров\n').replace('Имя =\n', 'Имя = Пример\n')
        self.text = self.text.replace('Дата рождения =\n', 'Дата рождения = 02.03.1980\n').replace('Врач =\n', 'Врач = Врач Пример Примерович\n')
        self.now = datetime(2030, 1, 2, 20, 0, tzinfo=MSK)

    def tearDown(self):
        self.temp.cleanup()

    def read(self, text=None):
        self.path.write_text(self.text if text is None else text, encoding='utf-8')
        return talon.parse_config(self.path, self.now)

    def test_simple_config_derives_ids_and_schedule(self):
        result = self.read()
        self.assertEqual(result.route, {'district':'8','lpu':'172','speciality':'49'})
        self.assertEqual(result.start.strftime('%H:%M:%S'), '20:59:45')
        self.assertEqual(result.end.strftime('%H:%M:%S'), '21:15:00')
        self.assertFalse(result.commit)
        self.assertEqual(result.patient['middleName'], '')

    def test_real_mode_is_explicit(self):
        self.assertTrue(self.read(self.text.replace('Режим = проверка', 'Режим = запись')).commit)

    def test_foreign_host_is_rejected_before_patient_can_be_sent(self):
        with self.assertRaises(ConfigError):
            self.read(self.text.replace('https://gorzdrav.spb.ru/', 'https://example.com/'))

    def test_expired_window_does_not_silently_move_to_tomorrow(self):
        self.now = self.now.replace(hour=22)
        with self.assertRaises(ConfigError):
            self.read()

    def test_now_and_tomorrow(self):
        immediate = self.read(self.text.replace('Время выдачи = 21:00', 'Время выдачи = сейчас'))
        self.assertEqual(immediate.start, self.now)
        tomorrow = self.read(self.text.replace('Дата выдачи = сегодня', 'Дата выдачи = завтра'))
        self.assertEqual(tomorrow.release.date(), (self.now + timedelta(days=1)).date())

    def test_url_retains_site_compatible_hash_separators(self):
        url = talon.booking.booking_url(self.read().route)
        self.assertIn('%22district%22:%228%22', url)
        self.assertNotIn('%3A', url)

    def test_bom_and_percent_in_url_are_accepted(self):
        self.path.write_text('\ufeff' + self.text, encoding='utf-8')
        self.assertEqual(talon.parse_config(self.path, self.now).route['lpu'], '172')

    def test_unknown_field_rejected(self):
        with self.assertRaises(ConfigError):
            self.read(self.text.replace('Минут ожидания = 15', 'Минут ожидание = 15'))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.marker = Path(self.temp.name) / 'state.json'
        now = datetime.now(MSK)
        self.settings = Settings({'lastName':'Примеров','firstName':'Пример','birthdate':'02.03.1980'},
                                 'Врач Пример Примерович', {'district':'8','lpu':'172','speciality':'49'},
                                 now, now, now+timedelta(minutes=5), 3, True)
        self.good = {'status':200,'data':{'success':True,'result':[
            {'id':'123','name':self.settings.doctor,'freeTicketCount':1,'freeParticipantCount':1}]}}
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.log = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def test_original_failure_then_success_is_retried(self):
        bad = {'status':200,'data':{'success':False,'errorCode':503,'result':None}}
        with patch.object(talon, 'read_schedule', side_effect=[bad,self.good]) as read, \
             patch.object(talon, 'wait_page'), \
             patch.object(talon.booking,'book_from_page',return_value='confirmed') as book:
            self.assertEqual(talon.monitor(self.page,self.settings,self.marker,self.log),'confirmed')
        self.assertEqual(read.call_count,2)
        self.assertEqual(book.call_count,1)
        self.assertTrue(any('errorCode=503' in call.args[0] for call in self.log.call_args_list))

    def test_network_error_before_submission_is_retried(self):
        with patch.object(talon,'read_schedule',return_value=self.good), patch.object(talon,'wait_page'), \
             patch.object(talon.booking,'book_from_page',side_effect=[talon.BrowserError('network'), 'confirmed']) as book:
            self.assertEqual(talon.monitor(self.page,self.settings,self.marker,self.log),'confirmed')
        self.assertEqual(book.call_count,2)

    def test_error_after_submission_is_never_retried(self):
        def uncertain(*args, **kwargs):
            self.marker.write_text('{"status":"submission_started"}')
            raise talon.BrowserError('network')
        with patch.object(talon,'read_schedule',return_value=self.good) as read, \
             patch.object(talon.booking,'book_from_page',side_effect=uncertain) as book:
            with self.assertRaises(talon.BrowserError):
                talon.monitor(self.page,self.settings,self.marker,self.log)
        self.assertEqual(read.call_count,1)
        self.assertEqual(book.call_count,1)

    def test_missing_doctor_waits_until_deadline_without_choosing_another(self):
        wrong = {'status':200,'data':{'success':True,'result':[
            {'id':'999','name':'Другой Врач','freeTicketCount':5,'freeParticipantCount':5}]}}
        clock = Mock(wraps=datetime)
        clock.now.return_value = self.settings.start
        def advance(page, moment):
            clock.now.return_value = moment
        with patch.object(talon,'datetime',clock), patch.object(talon.booking,'datetime',clock), \
             patch.object(talon,'read_schedule',return_value=wrong) as read, patch.object(talon,'wait_page',side_effect=advance) as waits, \
             patch.object(talon.booking,'book_from_page') as book:
            self.assertEqual(talon.monitor(self.page,self.settings,self.marker,self.log),'no-slots')
        self.assertGreater(read.call_count,8)
        self.assertEqual(waits.call_args.args[1],self.settings.end)
        book.assert_not_called()

    def test_twelve_server_errors_then_success(self):
        bad = {'status':503,'data':None}
        with patch.object(talon,'read_schedule',side_effect=[bad]*12+[self.good]) as read, \
             patch.object(talon,'wait_page'), patch.object(talon.booking,'book_from_page',return_value='confirmed') as book:
            self.assertEqual(talon.monitor(self.page,self.settings,self.marker,self.log),'confirmed')
        self.assertEqual(read.call_count,13)
        self.assertEqual(book.call_count,1)

    def test_patient_read_failures_keep_backoff_despite_successful_schedule(self):
        clock = Mock(wraps=datetime)
        clock.now.return_value = self.settings.start
        delays = []
        def advance(page, moment):
            delays.append((moment-clock.now.return_value).total_seconds())
            clock.now.return_value=moment
        with patch.object(talon,'datetime',clock), patch.object(talon.booking,'datetime',clock), \
             patch.object(talon,'read_schedule',return_value=self.good), patch.object(talon,'wait_page',side_effect=advance), \
             patch.object(talon.booking,'book_from_page',side_effect=[talon.booking.ReadRetry('HTTP 503')]*12+['confirmed']) as book:
            self.assertEqual(talon.monitor(self.page,self.settings,self.marker,self.log),'confirmed')
        self.assertEqual(book.call_count,13)
        self.assertEqual(delays[:5],[3,4,8,15,15])
        self.assertLessEqual(max(delays),15)

    def test_retry_after_longer_than_window_ends_without_another_request(self):
        clock = Mock(wraps=datetime)
        clock.now.return_value=self.settings.start
        def advance(page, moment):
            clock.now.return_value=moment
        with patch.object(talon,'datetime',clock), patch.object(talon.booking,'datetime',clock), \
             patch.object(talon,'read_schedule',return_value={'status':429,'retry_after':'3600'}) as read, \
             patch.object(talon,'wait_page',side_effect=advance) as waits:
            self.assertEqual(talon.monitor(self.page,self.settings,self.marker,self.log),'no-slots')
        self.assertEqual(read.call_count,1)
        self.assertEqual(waits.call_args.args[1],self.settings.end)

    def test_window_expiring_during_schedule_never_starts_booking(self):
        clock = Mock(wraps=datetime)
        clock.now.return_value=self.settings.start
        def slow_read(*args):
            clock.now.return_value=self.settings.end
            return self.good
        with patch.object(talon,'datetime',clock), patch.object(talon.booking,'datetime',clock), \
             patch.object(talon,'read_schedule',side_effect=slow_read), patch.object(talon.booking,'book_from_page') as book:
            self.assertEqual(talon.monitor(self.page,self.settings,self.marker,self.log),'no-slots')
        book.assert_not_called()

    def test_http_date_retry_after(self):
        from email.utils import format_datetime
        now=datetime.now(MSK).replace(microsecond=0)
        value=format_datetime(now+timedelta(seconds=600))
        delay=talon.booking.retry_after_seconds(value,now)
        self.assertGreaterEqual(delay,600)

    def test_not_sent_record_does_not_block_new_window(self):
        self.marker.write_text('{"status":"not_sent"}')
        self.assertEqual(talon.check_previous(self.marker,datetime.now(MSK)),set())
        self.assertFalse(self.marker.exists())

    def test_duplicate_names_and_changed_ids_stop(self):
        duplicate = json.loads(json.dumps(self.good))
        duplicate['data']['result'].append(dict(duplicate['data']['result'][0],id='456'))
        with self.assertRaises(StopBooking):
            talon.schedule_decision(duplicate,self.settings.doctor)
        with self.assertRaises(StopBooking):
            talon.schedule_decision(self.good,self.settings.doctor,'456')

    def test_name_normalization_and_integer_id(self):
        result = json.loads(json.dumps(self.good))
        result['data']['result'][0].update(id=123,name='  ВРАЧ  ПРИМЁР  ПРИМЕРОВИЧ  ')
        self.assertEqual(talon.schedule_decision(result,self.settings.doctor)[0],'123')

    def test_throttle_honors_retry_after(self):
        with self.assertRaises(ScheduleRetry) as error:
            talon.schedule_decision({'status':429,'retry_after':'60'},self.settings.doctor)
        self.assertEqual(error.exception.delay,60)

    def test_auth_failure_does_not_retry(self):
        with self.assertRaises(StopBooking):
            talon.schedule_decision({'status':403},self.settings.doctor)

    def test_successful_future_record_blocks_new_run(self):
        self.marker.write_text(json.dumps({'status':'confirmed','visit':(datetime.now(MSK)+timedelta(days=1)).isoformat()}))
        with self.assertRaises(StopBooking):
            talon.check_previous(self.marker,datetime.now(MSK))

    def test_past_record_is_archived(self):
        self.marker.write_text(json.dumps({'status':'confirmed','visit':'2020-01-01T10:00:00'}))
        self.assertEqual(talon.check_previous(self.marker,datetime.now(MSK)),set())
        self.assertFalse(self.marker.exists())
        self.assertEqual(len(list(self.marker.parent.glob('state-past-*.json'))),1)

    def test_closed_browser_stops_without_request(self):
        self.page.is_closed.return_value=True
        with patch.object(talon,'read_schedule') as read:
            with self.assertRaises(StopBooking):
                talon.monitor(self.page,self.settings,self.marker,self.log)
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
