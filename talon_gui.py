"""Desktop UI: settings, headless progress, confirmed receipt and PDF export."""

import argparse
import configparser
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sys
import threading

from PySide6.QtCore import QDate, QThread, QTime, QTimer, Qt, Signal, QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateEdit, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QSizePolicy, QStackedWidget, QTimeEdit, QVBoxLayout, QWidget)

import gui_backend
import talon
from gui_theme import SystemTheme
from gui_backdrop import MacBackdrop



def settings_from_values(values):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config['Пациент'] = {name:values.get(name, '') for name in ('Фамилия','Имя','Отчество','Дата рождения')}
    config['Запись'] = {name:str(values.get(name,'')) for name in ('Врач','Страница','Дата выдачи','Время выдачи','Режим','Минут ожидания','Интервал секунд')}
    output = io.StringIO()
    config.write(output)
    return talon.parse_config_text(output.getvalue())


class BookingWorker(QThread):
    stage = Signal(str, object)
    journal = Signal(str)
    result = Signal(object)

    def __init__(self, settings, destination, legacy, browser_setup=None, parent=None):
        super().__init__(parent)
        self.settings, self.destination, self.legacy = settings, destination, legacy
        self.browser_setup = browser_setup
        self.cancel = threading.Event()

    def run(self):
        try:
            result = gui_backend.execute(self.settings, self.destination, self.cancel,
                lambda stage,detail:self.stage.emit(stage,detail), self.journal.emit,
                legacy_directory=self.legacy, browser_setup=self.browser_setup)
        except Exception:
            # Never expose Playwright errors containing values passed to form.fill().
            result = {'status':'failed','reason':'Не удалось запустить процесс. Проверьте доступность папки данных и свободное место на диске.'}
        self.result.emit(result)


class MainWindow(QMainWindow):
    def __init__(self, data_directory=None, browser_setup=None):
        super().__init__()
        self.backdrop = MacBackdrop()
        if self.backdrop.library:
            self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle('Запись к врачу')
        self.resize(720, 790)
        self.setMinimumSize(620, 650)
        self.data_directory = Path(data_directory or QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
        self.browser_setup = browser_setup
        self.worker = None
        self.closing = False
        self.last_result = {}
        self.settings = None
        root = QWidget()
        root.setObjectName('windowSurface')
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)
        heading = QLabel('Запись к врачу')
        heading.setObjectName('heading')
        layout.addWidget(heading)
        subtitle = QLabel('Автоматическая запись на приём · gorzdrav.spb.ru')
        subtitle.setObjectName('subtitle')
        layout.addWidget(subtitle)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.build_form()
        self.build_progress()
        self.build_result()

    @staticmethod
    def text_label(text, name=None):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        if name:
            label.setObjectName(name)
        return label

    @staticmethod
    def configure_form(form):
        # macOS defaults otherwise keep fields at their size hint in the centre.
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

    def build_form(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0,0,0,0)
        scroll = QScrollArea()
        scroll.viewport().setObjectName('formViewport')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        contents = QWidget()
        contents.setObjectName('formContents')
        form_layout = QVBoxLayout(contents)
        form_layout.setContentsMargins(0,0,8,4)
        form_layout.setSpacing(8)
        self.fields = {}
        patient = QGroupBox('Пациент')
        patient_form = QGridLayout(patient)
        patient_form.setHorizontalSpacing(16)
        patient_form.setVerticalSpacing(6)
        for index,(name,placeholder) in enumerate((('Фамилия','Фамилия пациента'),('Имя','Имя пациента'),('Отчество','Необязательно'),('Дата рождения','ДД.ММ.ГГГГ'))):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.setAccessibleName(name)
            self.fields[name] = edit
            row,column = divmod(index,2)
            patient_form.addWidget(self.text_label(name, "fieldLabel"),row*2,column)
            patient_form.addWidget(edit,row*2+1,column)
        patient_form.setColumnStretch(0,1)
        patient_form.setColumnStretch(1,1)
        form_layout.addWidget(patient)
        doctor = QGroupBox('Врач')
        doctor_form = QFormLayout(doctor)
        self.configure_form(doctor_form)
        for name,placeholder in (('Врач','Полное ФИО, как на сайте'),('Страница','https://gorzdrav.spb.ru/service-free-schedule#…')):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.setAccessibleName(name)
            self.fields[name] = edit
            doctor_form.addRow('ФИО врача' if name=='Врач' else 'Ссылка',edit)
        form_layout.addWidget(doctor)
        schedule = QGroupBox('Поиск талонов')
        schedule_form = QFormLayout(schedule)
        self.configure_form(schedule_form)
        today = datetime.now(talon.MSK).date()
        self.release_date = QDateEdit(QDate(today.year,today.month,today.day))
        self.release_date.setDisplayFormat('dd.MM.yyyy')
        self.release_date.setCalendarPopup(True)
        self.release_time = QTimeEdit(QTime(21,0))
        self.release_time.setDisplayFormat('HH:mm')
        self.release_time.setMinimumWidth(120)
        row = QHBoxLayout()
        row.addWidget(self.release_date,1)
        row.addWidget(self.release_time)
        row.addWidget(QLabel('МСК'))
        schedule_form.addRow('Выдача талонов',row)
        self.now = QCheckBox('Начать поиск сейчас')
        self.now.toggled.connect(self.toggle_now)
        schedule_form.addRow('',self.now)
        self.minutes = QSpinBox()
        self.minutes.setRange(1,60)
        self.minutes.setValue(15)
        self.minutes.setSuffix(' мин')
        self.minutes.setMinimumWidth(115)
        self.interval = QSpinBox()
        self.interval.setRange(2,30)
        self.interval.setValue(3)
        self.interval.setSuffix(' сек')
        self.interval.setMinimumWidth(100)
        row = QHBoxLayout()
        row.addWidget(self.minutes)
        row.addSpacing(12)
        row.addWidget(QLabel('Проверять каждые'))
        row.addWidget(self.interval)
        schedule_form.addRow('Искать в течение',row)
        self.mode = QComboBox()
        self.mode.addItem('Проверка без создания записи','проверка')
        self.mode.addItem('Автоматическая запись','запись')
        self.mode.currentIndexChanged.connect(self.update_mode)
        schedule_form.addRow('Режим',self.mode)
        form_layout.addWidget(schedule)
        self.mode_note = self.text_label('', 'subtitle')
        form_layout.addStretch()
        scroll.setWidget(contents)
        contents.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        layout.addWidget(scroll,1)
        layout.addWidget(self.mode_note)
        self.form_error = self.text_label('', 'error')
        self.form_error.hide()
        layout.addWidget(self.form_error)
        buttons = QHBoxLayout()
        self.import_button = QPushButton('Импорт настроек…')
        self.import_button.setToolTip('Заполнить форму из существующего config.txt')
        self.import_button.setObjectName('quiet')
        self.import_button.clicked.connect(self.import_config)
        buttons.addWidget(self.import_button)
        buttons.addStretch()
        self.start_button = QPushButton('Начать проверку')
        self.start_button.setObjectName('primary')
        self.start_button.clicked.connect(self.start_booking)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        self.stack.addWidget(container)
        self.update_mode()

    def build_progress(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0,14,0,0)
        card = QFrame()
        card.setObjectName('statusCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24,28,24,28)
        card_layout.setSpacing(18)
        self.stage_title = self.text_label('Подготовка','stage')
        self.stage_text = self.text_label('Проверяем параметры записи.','message')
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0,0)
        self.progress_bar.setTextVisible(False)
        card_layout.addWidget(self.stage_title)
        card_layout.addWidget(self.stage_text)
        card_layout.addWidget(self.progress_bar)
        self.target_label = self.text_label('', 'subtitle')
        card_layout.addWidget(self.target_label)
        layout.addWidget(card)
        self.details_toggle = QPushButton('Показать подробности')
        self.details_toggle.setObjectName('quiet')
        self.details_toggle.setCheckable(True)
        self.details_toggle.toggled.connect(self.toggle_journal)
        layout.addWidget(self.details_toggle,0,Qt.AlignLeft)
        self.journal = QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(500)
        self.journal.hide()
        layout.addWidget(self.journal,1)
        layout.addStretch()
        self.stop_button = QPushButton('Остановить')
        self.stop_button.clicked.connect(self.stop_booking)
        layout.addWidget(self.stop_button,0,Qt.AlignRight)
        self.stack.addWidget(container)

    def build_result(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0,14,0,0)
        self.result_badge = self.text_label('', 'badge')
        self.result_badge.setWordWrap(False)
        self.result_badge.setSizePolicy(QSizePolicy.Minimum,QSizePolicy.Fixed)
        layout.addWidget(self.result_badge,0,Qt.AlignLeft)
        self.result_title = self.text_label('', 'stage')
        self.result_message = self.text_label('', 'message')
        layout.addWidget(self.result_title)
        layout.addWidget(self.result_message)
        self.receipt_box = QGroupBox('Информация о приёме')
        rows = QFormLayout(self.receipt_box)
        self.configure_form(rows)
        rows.setVerticalSpacing(15)
        self.receipt_fields = {}
        for key,label in (('date','Дата'),('time','Время'),('doctor','Врач'),('speciality','Специальность'),('clinic','Медучреждение'),('address','Адрес'),('phone','Телефон'),('room','Кабинет')):
            value = self.text_label('')
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if key in ('date','time'):
                value.setObjectName('appointmentTime')
            self.receipt_fields[key] = value
            rows.addRow(label,value)
        layout.addWidget(self.receipt_box)
        self.pdf_button = QPushButton('Скачать PDF талона')
        self.pdf_button.setObjectName('primary')
        self.pdf_button.clicked.connect(self.save_pdf)
        layout.addWidget(self.pdf_button,0,Qt.AlignLeft)
        self.save_message = self.text_label('', 'subtitle')
        layout.addWidget(self.save_message)
        layout.addStretch()
        buttons = QHBoxLayout()
        self.folder_button = QPushButton('Открыть результаты')
        self.folder_button.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_result.get('folder',str(self.data_directory)))))
        buttons.addWidget(self.folder_button)
        buttons.addStretch()
        self.back_button = QPushButton('К форме')
        self.back_button.clicked.connect(lambda:self.stack.setCurrentIndex(0))
        buttons.addWidget(self.back_button)
        layout.addLayout(buttons)
        self.stack.addWidget(container)

    def toggle_now(self, checked):
        self.release_date.setEnabled(not checked)
        self.release_time.setEnabled(not checked)

    def update_mode(self):
        real = self.mode.currentData() == 'запись'
        self.start_button.setText('Начать запись' if real else 'Начать проверку')
        self.mode_note.setText('Будет автоматически создана настоящая запись на любой доступный талон к выбранному врачу.' if real else
                              'Пройдём до финального подтверждения. Данные пациента проверит сайт, но запись создана не будет.')

    def toggle_journal(self, visible):
        self.journal.setVisible(visible)
        self.details_toggle.setText('Скрыть подробности' if visible else 'Показать подробности')

    def values(self):
        values = {name:edit.text().strip() for name,edit in self.fields.items()}
        values.update({'Дата выдачи':self.release_date.date().toString('dd.MM.yyyy'),
                       'Время выдачи':'сейчас' if self.now.isChecked() else self.release_time.time().toString('HH:mm'),
                       'Режим':self.mode.currentData(),'Минут ожидания':self.minutes.value(),'Интервал секунд':self.interval.value()})
        return values

    def import_config(self):
        path,_ = QFileDialog.getOpenFileName(self,'Импорт настроек','','Текстовые настройки (*.txt)')
        if not path:
            return
        try:
            self.load_config(Path(path))
            self.form_error.hide()
        except Exception:
            self.form_error.setText('Не удалось импортировать файл. Нужен config.txt с разделами [Пациент] и [Запись].')
            self.form_error.show()

    def load_config(self, path):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(path.read_text(encoding='utf-8-sig'))
        for name,edit in self.fields.items():
            edit.setText(parser['Пациент' if name in ('Фамилия','Имя','Отчество','Дата рождения') else 'Запись'].get(name,''))
        schedule = parser['Запись']
        date = schedule.get('Дата выдачи','сегодня').strip()
        if date.casefold() in ('сегодня','завтра'):
            today=datetime.now(talon.MSK).date()
            qdate=QDate(today.year,today.month,today.day).addDays(int(date.casefold()=='завтра'))
        else:
            qdate=QDate.fromString(date,'dd.MM.yyyy')
        if qdate.isValid():
            self.release_date.setDate(qdate)
        hour=schedule.get('Время выдачи','21:00').strip()
        self.now.setChecked(hour.casefold()=='сейчас')
        qtime=QTime.fromString(hour,'HH:mm')
        if qtime.isValid():
            self.release_time.setTime(qtime)
        mode=schedule.get('Режим','проверка').strip().strip('«»"')
        self.mode.setCurrentIndex(1 if mode=='запись' else 0)
        self.minutes.setValue(int(schedule.get('Минут ожидания','15')))
        self.interval.setValue(int(schedule.get('Интервал секунд','3')))

    def legacy_directory(self):
        if getattr(sys,'frozen',False):
            for path in Path(sys.executable).parents:
                if path.suffix=='.app':
                    adjacent=path.parent/'Результаты'
                    previous=path.parent.parent/'release'/'Результаты'
                    return adjacent if adjacent.exists() else previous
        return Path(__file__).parent/'release'/'Результаты'

    def start_booking(self):
        if self.worker is not None and self.worker.isRunning():
            return
        try:
            self.settings=settings_from_values(self.values())
        except talon.ConfigError as error:
            self.form_error.setText(str(error))
            self.form_error.show()
            return
        self.form_error.hide()
        self.journal.clear()
        self.details_toggle.setChecked(False)
        self.last_result={}
        self.stop_button.setEnabled(True)
        self.back_button.setEnabled(False)
        self.target_label.setText(self.settings.doctor)
        self.stack.setCurrentIndex(1)
        self.on_stage('waiting',{'start':self.settings.start.isoformat(),'release':self.settings.release.isoformat()})
        self.worker=BookingWorker(self.settings,self.data_directory,self.legacy_directory(),self.browser_setup,self)
        self.worker.stage.connect(self.on_stage)
        self.worker.journal.connect(self.journal.appendPlainText)
        self.worker.result.connect(self.on_result)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def stop_booking(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel.set()
            self.stop_button.setEnabled(False)
            self.stage_title.setText('Останавливаем процесс')
            self.stage_text.setText('Если заявка уже отправлена, дождёмся ответа сайта, чтобы не потерять результат.')

    def on_stage(self, stage, detail):
        if stage=='waiting':
            release=datetime.fromisoformat(detail['release'])
            title='Ждём выдачи талонов'
            message=f'Выдача: {release:%d.%m.%Y в %H:%M} МСК. Проверки начнутся за 15 секунд до этого времени.'
            if self.settings and self.settings.start <= datetime.now(talon.MSK):
                title,message='Подготовка к поиску','Сейчас проверим страницу и нужного врача.'
        elif stage=='retry':
            next_time=datetime.fromisoformat(detail['next'])
            title,message='Сайт временно не отвечает',f'Повторим безопасный запрос не раньше {next_time:%H:%M:%S} МСК. Поиск продолжается.'
        else:
            title,message={
                'preparing':('Подготовка','Запускаем скрытый браузер и открываем страницу врача.'),
                'monitoring':('Ищем свободные талоны','Пока свободных мест нет. Продолжаем проверять расписание.'),
                'found':('Талоны появились','Выбираем доступное время и пробуем записаться.'),
                'selecting':('Выбираем талон','Открываем форму для выбранного времени приёма.'),
                'patient':('Проверяем данные пациента','Заполняем форму и ждём подтверждения от сайта.'),
                'confirming':('Подтверждаем выбранный талон','Готовимся завершить оформление записи.'),
                'submitting':('Отправляем заявку','Дожидаемся ответа сайта. Повторную заявку не отправляем.'),
                'confirmed':('Запись подтверждена','Получаем сведения о приёме и талон.'),
                'exporting':('Сохраняем талон','Запись уже создана. Получаем PDF и сведения о приёме.'),
            }.get(stage,('Выполняем запись','Пожалуйста, подождите.'))
        self.stage_title.setText(title)
        self.stage_text.setText(message)

    def on_result(self, result):
        self.last_result=result
        status=result.get('status')
        titles={'confirmed':'Вы записаны на приём','checked':'Проверка завершена','cancelled':'Процесс остановлен',
                'uncertain':'Результат нужно проверить','failed':'Запись не выполнена'}
        self.result_title.setText(titles.get(status,'Процесс завершён'))
        self.result_badge.setText({'confirmed':'ПОДТВЕРЖДЕНО','checked':'ПРОВЕРКА','cancelled':'ОСТАНОВЛЕНО',
            'uncertain':'ТРЕБУЕТ ПРОВЕРКИ','failed':'НЕ УДАЛОСЬ ЗАПИСАТЬСЯ'}.get(status,'РЕЗУЛЬТАТ'))
        self.result_badge.setProperty('tone','success' if status=='confirmed' else 'error' if status in ('failed','uncertain') else 'neutral')
        self.result_badge.style().unpolish(self.result_badge)
        self.result_badge.style().polish(self.result_badge)
        self.result_message.setText(result.get('warning','') if status=='confirmed' else result.get('reason',''))
        self.result_message.setVisible(bool(self.result_message.text()))
        self.receipt_box.setVisible(status=='confirmed')
        for key,label in self.receipt_fields.items():
            label.setText(result.get('details',{}).get(key) or 'Не указано на сайте')
        self.pdf_button.setVisible(status=='confirmed')
        self.pdf_button.setEnabled(bool(result.get('pdf')))
        self.pdf_button.setText('Скачать PDF талона' if result.get('pdf_kind')=='official' else 'Сохранить PDF-копию')
        self.save_message.clear()
        self.stack.setCurrentIndex(2)

    def worker_finished(self):
        self.back_button.setEnabled(True)
        if self.closing:
            self.close()

    def save_pdf(self):
        source=Path(self.last_result.get('pdf',''))
        if not source.is_file():
            return
        target,_=QFileDialog.getSaveFileName(self,'Сохранить талон','Талон.pdf','PDF (*.pdf)')
        if not target:
            return
        if not target.lower().endswith('.pdf'):
            target+='.pdf'
        try:
            gui_backend.secure_bytes(Path(target),source.read_bytes())
            self.save_message.setText('PDF сохранён в выбранную папку.')
        except OSError:
            QMessageBox.warning(self,'Не удалось сохранить PDF','Выберите другую папку и повторите сохранение.')

    def closeEvent(self,event):
        if self.worker and self.worker.isRunning():
            self.closing=True
            self.stop_booking()
            event.ignore()
        else:
            event.accept()

    def showEvent(self,event):
        super().showEvent(event)
        if self.backdrop.install(self):
            theme = getattr(QApplication.instance(),'theme',None)
            if theme:
                self.backdrop.set_dark(self,theme.dark)
            self.setProperty('glass',True)
            self.centralWidget().setProperty('glass',True)
            self.style().unpolish(self)
            self.style().polish(self)
            self.centralWidget().style().unpolish(self.centralWidget())
            self.centralWidget().style().polish(self.centralWidget())

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.property('glass'):
            QTimer.singleShot(0,lambda:self.backdrop.install(self))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--self-test-report',type=Path)
    args=parser.parse_args()
    app=QApplication(sys.argv[:1])
    app.setOrganizationName('Talon')
    app.setApplicationName('Запись к врачу')
    app.theme = SystemTheme(app)
    if args.self_test_report:
        from gui_smoke import start_smoke
        return start_smoke(app,args.self_test_report)
    window=MainWindow(args.data_dir)
    window.show()
    return app.exec()


if __name__=='__main__':
    raise SystemExit(main())
