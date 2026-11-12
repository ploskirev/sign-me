"""Synthetic website: all browser requests are intercepted, including booking and PDF."""
from urllib.parse import urlsplit


class Website:
    def __init__(self, failures=1, pdf_error=False, on_search=None, on_create=None):
        self.failures = failures
        self.pdf_error = pdf_error
        self.on_search = on_search or (lambda: None)
        self.on_create = on_create or (lambda: None)
        self.creations = 0
        self.reads = 0
        self.pdf = b''

    def setup(self, context):
        # Generate a valid PDF locally before installing the fixture's download handler.
        preview = context.new_page()
        preview.set_content('<h1>Offline test receipt</h1>')
        self.pdf = preview.pdf()
        preview.close()
        context.route('**/*', self.route)

    def route(self, route):
        path = urlsplit(route.request.url).path
        if path == '/service-free-schedule':
            route.fulfill(content_type='text/html; charset=utf-8', body=HTML)
        elif path == '/test-schedule':
            self.reads += 1
            if self.reads <= self.failures:
                route.fulfill(status=503, json={'success': False})
            else:
                route.fulfill(json={'success':True,'result':[{'id':'839','name':'Тестовый Доктор','freeTicketCount':1,'freeParticipantCount':1}]})
        elif path == '/v2/patient/search':
            self.on_search()
            route.fulfill(json={'success':True,'result':'synthetic-patient'})
        elif path == '/v2/appointment/create':
            self.creations += 1
            self.on_create()
            route.fulfill(json={'success':True,'result':'synthetic-booking'})
        elif path == '/test-receipt':
            route.fulfill(content_type='application/pdf', headers={'Content-Disposition':'attachment; filename=receipt.pdf'},
                          body=b'INVALID' if self.pdf_error else self.pdf)
        else:
            route.abort()

HTML = '<!doctype html><html><head><meta charset="utf-8"></head><body>\n<div class="js-doctor" data-doctor-id="839" data-doctor-name="Тестовый Доктор">\n  <button class="js-doctor-toggler" onclick="document.querySelector(\'.js-slot\').hidden=false">Выбрать</button>\n  <button class="js-slot" hidden data-ticket-id="slot-test" data-visit-start="2026-11-20T10:00:00"\n    onclick="document.querySelector(\'.js-doctor\').setAttribute(\'data-ticket-id\',this.dataset.ticketId)">10:00</button>\n  <button class="service-doctor__sign-up" onclick="document.querySelector(\'#checkPatientForm\').hidden=false">Записаться</button>\n</div>\n<form id="checkPatientForm" hidden>\n  <input name="lastName"><input name="firstName"><input name="middleName">\n  <input name="birthdate"><input name="email"><input name="phone">\n  <label class="agreement"><input type="checkbox" name="agreement">Согласие</label>\n  <button class="service-form__submit">Далее</button>\n</form>\n<div id="confirmation" class="js-agreement-wrapper" hidden>\n  <label class="agreement"><input type="checkbox">Условия записи</label>\n  <button id="setAppointmentButton">Записаться</button>\n</div>\n<div class="service-tabs__item--8" hidden>Вы успешно записаны на прием к врачу.\n<div class="service-block-2__col--first"><div class="service-block-2__doctor">Тестовый Доктор</div>\n<div class="service-block-2__title">Офтальмолог</div><div class="service-block-2__mo">Тестовая поликлиника</div>\n<a href="tel:000000000">000-000-000</a></div>\n<form id="pdfForm" action="/test-receipt" method="post"><input name="address" value="Тестовая улица, 1"><input name="room" value="123"><button id="savePdfBtn">PDF</button></form></div>\n<script>\nwindow.API_HOST=location.origin;\nwindow.HealthcarePortalClient=function(){this.getDoctors=()=>{\n let good,bad;const controller=new AbortController();\n fetch(\'/test-schedule\',{signal:controller.signal}).then(async r=>{\n  if(r.ok) good(await r.json(),\'success\',{status:r.status});\n  else bad({status:r.status,getResponseHeader:k=>r.headers.get(k)});\n }).catch(()=>bad({status:0,getResponseHeader:()=>null}));\n return {done:f=>{good=f},fail:f=>{bad=f},abort:()=>controller.abort()};\n}};\n\nlet patientId;\ndocument.querySelector(\'#checkPatientForm\').onsubmit=async e=>{\n e.preventDefault();\n const response=await fetch(\'/v2/patient/search\');\n const data=await response.json();\n patientId=data.result;\n if(data.success && patientId) document.querySelector(\'#confirmation\').hidden=false;\n};\ndocument.querySelector(\'#setAppointmentButton\').onclick=async()=>{\n const form=document.querySelector(\'#checkPatientForm\');\n const data=Object.fromEntries(new FormData(form));\n const [day,month,year]=data.birthdate.split(\'.\');\n const payload={lpuId:\'172\',appointmentId:document.querySelector(\'.js-doctor\').dataset.ticketId,patientId,\n patientLastName:data.lastName,patientFirstName:data.firstName,patientMiddleName:data.middleName,\n patientBirthdate:`${year}-${month}-${day}T00:00:00`};\n const response=await fetch(\'/v2/appointment/create\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(payload)});\n const result=await response.json();\n if(result.success) document.querySelector(\'.service-tabs__item--8\').hidden=false;\n};\n</script></body></html>'
