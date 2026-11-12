"""Browser flow tests against a completely intercepted local fixture."""

import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright
import booking
from booking import book_from_page, payload_matches, validate_config, save_confirmation, slot_taken, ReadRetry, WindowExpired, MSK


HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="js-doctor" data-doctor-id="839" data-doctor-name="Мирагасы Кызы">
  <button class="js-doctor-toggler" onclick="document.querySelector('.js-slot').hidden=false">Выбрать</button>
  <button class="js-slot" hidden data-ticket-id="slot-test" data-visit-start="2026-11-20T10:00:00"
    onclick="document.querySelector('.js-doctor').setAttribute('data-ticket-id',this.dataset.ticketId)">10:00</button>
  <button class="service-doctor__sign-up" onclick="document.querySelector('#checkPatientForm').hidden=false">Записаться</button>
</div>
<form id="checkPatientForm" hidden>
  <input name="lastName"><input name="firstName"><input name="middleName">
  <input name="birthdate"><input name="email"><input name="phone">
  <label class="agreement"><input type="checkbox" name="agreement">Согласие</label>
  <button class="service-form__submit">Далее</button>
</form>
<div id="confirmation" class="js-agreement-wrapper" hidden>
  <label class="agreement"><input type="checkbox">Условия записи</label>
  <button id="setAppointmentButton">Записаться</button>
</div>
<div class="service-tabs__item--8" hidden>Вы успешно записаны на прием к врачу.</div>
<script>
let patientId;
document.querySelector('#checkPatientForm').onsubmit=async e=>{
 e.preventDefault();
 const response=await fetch('/v2/patient/search');
 const data=await response.json();
 patientId=data.result;
 if(data.success && patientId) document.querySelector('#confirmation').hidden=false;
};
document.querySelector('#setAppointmentButton').onclick=async()=>{
 const form=document.querySelector('#checkPatientForm');
 const data=Object.fromEntries(new FormData(form));
 const [day,month,year]=data.birthdate.split('.');
 const payload={lpuId:'172',appointmentId:document.querySelector('.js-doctor').dataset.ticketId,patientId,
 patientLastName:data.lastName,patientFirstName:data.firstName,patientMiddleName:data.middleName,
 patientBirthdate:`${year}-${month}-${day}T00:00:00`};
 const response=await fetch('/v2/appointment/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
 const result=await response.json();
 if(result.success) document.querySelector('.service-tabs__item--8').hidden=false;
};
</script></body></html>"""


class BookingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, channel="chromium")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.marker = Path(self.directory.name) / "submission.json"
        self.config = validate_config({
            "district": "8", "lpu": "172", "speciality": "49", "doctor_id": "839",
            "doctor_name_contains": "Мирагасы Кызы",
            "patient": {"lastName": "Тестовый", "firstName": "Пациент", "middleName": "", "birthdate": "01.01.1990"},
        })
        self.creations = []
        self.search_result = {"success": True, "result": "patient-test"}
        self.search_status = 200
        self.search_headers = {}
        self.search_raw = None
        self.search_count = 0
        self.search_failures_left = 0
        self.on_search = lambda: None
        self.on_create = lambda: None
        self.create_result = {"success": True, "result": "booking-test"}
        self.context = self.browser.new_context()
        self.context.route("**/*", self.route)
        self.page = self.context.new_page()
        self.page.goto("https://booking.test/")

    def tearDown(self):
        self.context.close()
        self.directory.cleanup()

    def route(self, route):
        url = route.request.url
        if url == "https://booking.test/":
            route.fulfill(status=200, content_type="text/html", body=HTML)
        elif url == "https://booking.test/v2/patient/search":
            self.search_count += 1
            self.on_search()
            if self.search_failures_left:
                self.search_failures_left -= 1
                route.fulfill(status=503,json={"success":False})
            elif self.search_raw is not None:
                route.fulfill(status=self.search_status,body=self.search_raw,content_type='text/html')
            else:
                route.fulfill(status=self.search_status,json=self.search_result,headers=self.search_headers)
        elif url == "https://booking.test/v2/appointment/create":
            self.creations.append(route.request.post_data_json)
            self.on_create()
            route.fulfill(status=200, json=self.create_result)
        else:
            route.abort()

    def test_rehearsal_never_creates(self):
        self.assertEqual(book_from_page(self.page, self.config, False, self.marker), "dry-run")
        self.assertEqual(self.creations, [])
        self.assertFalse(self.marker.exists())

    def test_success_sends_once_and_records_confirmation(self):
        self.assertEqual(book_from_page(self.page, self.config, True, self.marker), "confirmed")
        self.assertEqual(len(self.creations), 1)
        self.assertEqual(json.loads(self.marker.read_text())["status"], "confirmed")
        image = save_confirmation(self.page, self.marker)
        self.assertTrue(image.read_bytes().startswith(b'\x89PNG'))
        self.assertEqual(image.stat().st_mode & 0o777, 0o600)

    def test_taken_slot_retries_a_different_ticket(self):
        self.create_result = {"success": False, "message": "Талон уже занят другим пациентом"}
        excluded = set()
        self.assertEqual(book_from_page(self.page, self.config, True, self.marker, excluded), "retry")
        self.assertEqual(excluded, {"slot-test"})
        self.assertEqual(json.loads(self.marker.read_text())["status"], "rejected_slot")
        self.page.reload()
        self.page.locator('.js-slot').evaluate("e=>e.dataset.ticketId='slot-second'")
        self.create_result = {"success": True}
        self.assertEqual(book_from_page(self.page, self.config, True, self.marker, excluded), "confirmed")
        self.assertEqual([request['appointmentId'] for request in self.creations], ['slot-test', 'slot-second'])

    def test_unknown_error_is_not_a_slot_conflict(self):
        self.assertFalse(slot_taken({"success":False,"message":"Неизвестная ошибка"}))
        self.assertFalse(slot_taken({"success":True,"message":"Талон уже занят"}))

    def test_masked_patient_is_not_accepted(self):
        self.config['patient']['lastName']='[MASKED:LAST_NAME]'
        with self.assertRaises(ValueError):
            validate_config(self.config)

    def test_patient_not_found_never_creates(self):
        self.search_result = {"success": True, "result": ""}
        with self.assertRaises(RuntimeError):
            book_from_page(self.page, self.config, True, self.marker)
        self.assertEqual(self.creations, [])

    def test_patient_transient_http_errors_allow_retry_without_submission(self):
        for status in (408,425,429,500,502,503,504):
            with self.subTest(status=status):
                self.page.reload()
                self.search_status=status
                self.search_headers={'Retry-After':'60'}
                with self.assertRaises(ReadRetry) as error:
                    book_from_page(self.page,self.config,True,self.marker)
                if status==429:
                    self.assertEqual(error.exception.delay,60)
                self.assertFalse(self.marker.exists())
        self.assertEqual(self.creations,[])

    def test_patient_invalid_json_and_shape_allow_retry(self):
        self.search_raw='<html>Temporarily unavailable</html>'
        with self.assertRaises(ReadRetry):
            book_from_page(self.page,self.config,True,self.marker)
        self.page.reload()
        self.search_raw=None
        self.search_result=[]
        with self.assertRaises(ReadRetry):
            book_from_page(self.page,self.config,True,self.marker)
        self.assertEqual(self.creations,[])

    def test_patient_auth_error_stops_without_retry(self):
        self.search_status=403
        with self.assertRaises(RuntimeError):
            book_from_page(self.page,self.config,True,self.marker)
        self.assertEqual(self.creations,[])

    def test_expiry_after_patient_check_prevents_submission(self):
        clock=Mock(wraps=datetime)
        now=datetime.now(MSK)
        clock.now.return_value=now
        deadline=now+timedelta(minutes=1)
        self.on_search=lambda: setattr(clock.now,'return_value',deadline)
        with patch.object(booking,'datetime',clock), self.assertRaises(WindowExpired):
            book_from_page(self.page,self.config,True,self.marker,deadline=deadline)
        self.assertEqual(self.creations,[])
        self.assertFalse(self.marker.exists())

    def test_confirmation_after_deadline_is_still_recorded(self):
        clock=Mock(wraps=datetime)
        now=datetime.now(MSK)
        clock.now.return_value=now
        deadline=now+timedelta(minutes=1)
        self.on_create=lambda: setattr(clock.now,'return_value',deadline+timedelta(seconds=1))
        with patch.object(booking,'datetime',clock):
            self.assertEqual(book_from_page(self.page,self.config,True,self.marker,deadline=deadline),'confirmed')
        self.assertEqual(len(self.creations),1)
        self.assertEqual(json.loads(self.marker.read_text())['status'],'confirmed')

    def test_real_flow_recovers_after_patient_server_failures(self):
        import talon
        now=datetime.now(MSK)
        settings=talon.Settings(self.config['patient'],self.config['doctor_name_contains'],
            {k:self.config[k] for k in ('district','lpu','speciality')},now,now,now+timedelta(minutes=5),3,True)
        good={'status':200,'data':{'success':True,'result':[{'id':'839','name':settings.doctor,'freeTicketCount':1,'freeParticipantCount':1}]}}
        self.context.add_init_script("window.HealthcarePortalClient=function(){};window.API_HOST='https://booking.test';")
        self.search_failures_left=2
        with patch.object(talon,'read_schedule',return_value=good), patch.object(talon,'wait_page'), \
             patch.object(booking,'booking_url',return_value='https://booking.test/'):
            self.assertEqual(talon.monitor(self.page,settings,self.marker,lambda message:None),'confirmed')
        self.assertEqual(self.search_count,3)
        self.assertEqual(len(self.creations),1)

    def test_rejected_booking_is_not_retried(self):
        self.create_result = {"success": False, "errorCode": 1}
        with self.assertRaises(RuntimeError):
            book_from_page(self.page, self.config, True, self.marker)
        self.assertEqual(len(self.creations), 1)
        self.assertEqual(json.loads(self.marker.read_text())["status"], "not_confirmed")

    def test_prior_submission_blocks_second_attempt(self):
        self.marker.write_text('{"status":"submission_started"}')
        with self.assertRaises(FileExistsError):
            book_from_page(self.page, self.config, True, self.marker)
        self.assertEqual(self.creations, [])

    def test_changed_doctor_stops_before_patient_search(self):
        self.page.locator('.js-doctor').evaluate("e=>e.dataset.doctorName='Другой врач'")
        with self.assertRaises(RuntimeError):
            book_from_page(self.page, self.config, True, self.marker)
        self.assertEqual(self.creations, [])

    def test_payload_rejects_wrong_patient_and_ticket(self):
        payload = {"lpuId":"172", "appointmentId":"slot-test", "patientId":"patient-test",
                   "patientLastName":"Тестовый", "patientFirstName":"Пациент", "patientMiddleName":"",
                   "patientBirthdate":"1990-01-01T00:00:00"}
        self.assertTrue(payload_matches(payload, self.config, "slot-test", "patient-test"))
        self.assertFalse(payload_matches(payload, self.config, "other-slot", "patient-test"))
        self.assertFalse(payload_matches(payload, self.config, "slot-test", "other-patient"))


if __name__ == "__main__":
    unittest.main()
