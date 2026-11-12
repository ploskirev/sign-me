"""Packaged app self-test. Never contacts the real service."""
import json
from pathlib import Path
import tempfile

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDateTimeEdit, QFileDialog
from gui_fixture import Website


def start_smoke(app, report_path):
    from talon_gui import MainWindow
    from booking import booking_url
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='talon-gui-test-')
    fixture = Website()
    window = MainWindow(Path(temporary.name), fixture.setup)
    window.legacy_directory = lambda: None
    window.show()
    stages = []
    errors = []
    appearances = []

    def capture_themes(screen):
        original = app.styleHints().colorScheme()
        before = window.values()
        try:
            for name,scheme in [('light',Qt.ColorScheme.Light),('dark',Qt.ColorScheme.Dark)]:
                # Exercise the same signal delivered by macOS without changing system settings.
                app.styleHints().colorSchemeChanged.emit(scheme)
                app.processEvents()
                assert app.theme.dark == (name == 'dark')
                assert (app.palette().color(QPalette.Window).lightness() < 128) == (name == 'dark')
                window.grab().save(str(report_path.with_name(report_path.stem+f'-{screen}-{name}.png')))
                appearances.append(screen+'-'+name)
            assert window.values() == before, 'Changing appearance changed form inputs'
        finally:
            app.styleHints().colorSchemeChanged.emit(original)
            app.processEvents()

    def finish():
        try:
            result = window.last_result
            assert result['status'] == 'confirmed', result
            assert fixture.creations == 1
            assert 'retry' in stages and 'submitting' in stages and 'exporting' in stages, stages
            details = result['details']
            assert details['doctor'] == 'Тестовый Доктор'
            assert details['speciality'] == 'Офтальмолог'
            assert details['clinic'] == 'Тестовая поликлиника'
            assert details['address'] == 'Тестовая улица, 1'
            assert details['phone'] == '000-000-000'
            assert details['date'] == '20.11.2026' and details['time'] == '10:00'
            assert window.pdf_button.isEnabled()
            saved = report_path.with_suffix('.pdf')
            original = QFileDialog.getSaveFileName
            try:
                QFileDialog.getSaveFileName = lambda *a, **kw: (str(saved), 'PDF')
                window.pdf_button.click()
            finally:
                QFileDialog.getSaveFileName = original
            assert saved.read_bytes() == fixture.pdf
            capture_themes('result')
            window.grab().save(str(report_path.with_name(report_path.stem+'-result.png')))
        except Exception as error:
            errors.append(repr(error))
        report_path.write_text(json.dumps({'ok':not errors,'errors':errors,'stages':stages,
                                           'submissions':fixture.creations,'schedule_reads':fixture.reads,
                                           'appearances':appearances},ensure_ascii=False,indent=2))
        window.close()
        app.exit(1 if errors else 0)

    def prepare():
        assert window.backdrop.ready(window), 'Native background is not installed'
        size = window.size()
        window.resize(620,650)
        app.processEvents()
        for status in ('failed','uncertain','cancelled','checked'):
            window.on_result({'status':status,'reason':'Сайт временно не отвечает. Попробуйте позже.'})
            app.processEvents()
            badge = window.result_badge
            assert not badge.wordWrap()
            assert badge.width() >= badge.sizeHint().width(), (status,'badge clipped horizontally')
            assert badge.height() >= badge.sizeHint().height(), (status,'badge clipped vertically')
        window.on_result({'status':'failed','reason':'Время ожидания закончилось. Сайт временно не отвечает. Попробуйте позже.'})
        capture_themes('failure')
        assert window.backdrop.ready(window), 'Native background did not follow resize'
        background_alpha = window.grab().toImage().pixelColor(5,5).alpha()
        assert 0 < background_alpha < 255, ('Background is opaque',background_alpha)
        window.stack.setCurrentIndex(0)
        window.resize(size)
        app.processEvents()
        values={'Фамилия':'Тестовый','Имя':'Пациент','Дата рождения':'01.01.1990','Врач':'Тестовый Доктор',
                'Страница':booking_url({'district':'8','lpu':'172','speciality':'49'})}
        for name,value in values.items():
            window.fields[name].setText(value)
        window.release_time.setCurrentSection(QDateTimeEdit.HourSection)
        QTest.mouseClick(window.release_time,Qt.LeftButton,pos=QPoint(window.release_time.width()-12,8))
        assert window.release_time.time().hour() == 22, 'Time up arrow does not work'
        QTest.mouseClick(window.release_time,Qt.LeftButton,pos=QPoint(window.release_time.width()-12,window.release_time.height()-8))
        assert window.release_time.time().hour() == 21, 'Time down arrow does not work'
        QTest.mouseClick(window.minutes,Qt.LeftButton,pos=QPoint(window.minutes.width()-12,8))
        assert window.minutes.value() == 16, 'Duration up arrow does not work'
        QTest.mouseClick(window.minutes,Qt.LeftButton,pos=QPoint(window.minutes.width()-12,window.minutes.height()-8))
        assert window.minutes.value() == 15, 'Duration down arrow does not work'
        capture_themes('form')
        window.now.setChecked(True)
        window.mode.setCurrentIndex(1)
        window.grab().save(str(report_path.with_name(report_path.stem+'-form.png')))
        window.start_button.click()
        if window.worker is None:
            errors.append(window.form_error.text())
            finish()
            return
        window.worker.stage.connect(lambda stage,detail:stages.append(stage))
        window.worker.finished.connect(lambda:QTimer.singleShot(150,finish))

    def begin():
        try:
            prepare()
        except Exception as error:
            errors.append(repr(error))
            finish()

    QTimer.singleShot(300,begin)
    exit_code = app.exec()
    temporary.cleanup()
    return exit_code
