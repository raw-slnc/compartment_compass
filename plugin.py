# -*- coding: utf-8 -*-
import os
import sys
import traceback
from datetime import datetime

from qgis.PyQt.QtCore import QCoreApplication, QSettings, Qt, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDialog, QDockWidget, QVBoxLayout

from .panel_widget import (
    CompartmentCompassDockWidget,
    CompartmentCompassWindow,
)


class CompartmentCompass:
    """Compartment Compass プラグインのメインクラス。"""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = self.tr('Compartment Compass')
        self.dock = None
        self.dialog = None
        self.window = None
        self._switching_container = False
        self._translator = None
        self._active_locale = 'en'
        self._error_log_path = os.path.join(self.plugin_dir, 'error.log')
        self._previous_excepthook = None

    @staticmethod
    def tr(message):
        return QCoreApplication.translate('CompartmentCompass', message)

    def get_active_locale(self):
        return self._active_locale

    def _log_uncaught_exception(self, exc_type, exc_value, exc_tb):
        # PyQtはスロット内の未処理例外をsys.excepthook経由でコンソールに出す
        # だけなので、QGIS終了時に一瞬表示されて読めない問題への対策として
        # ファイルにも残す。原因がプラグイン外でも記録され得るが、他に捕まえる
        # 手段が無いため許容する。
        try:
            with open(self._error_log_path, 'a', encoding='utf-8') as f:
                f.write(
                    '\n--- {0} ---\n'.format(
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    )
                )
                traceback.print_exception(
                    exc_type, exc_value, exc_tb, file=f
                )
        except OSError:
            pass
        if self._previous_excepthook is not None:
            self._previous_excepthook(exc_type, exc_value, exc_tb)

    def _remove_translator(self):
        if self._translator:
            QCoreApplication.removeTranslator(self._translator)
            self._translator = None

    def set_language(self, locale):
        if locale is None:
            locale = QSettings().value('compartment_compass/language', 'en')
        else:
            QSettings().setValue('compartment_compass/language', locale)

        self._remove_translator()
        loaded_locale = 'en'
        if locale == 'ja':
            qm_path = os.path.join(
                self.plugin_dir, 'i18n', 'compartment_compass_ja.qm'
            )
            if os.path.exists(qm_path):
                translator = QTranslator()
                if translator.load(qm_path):
                    QCoreApplication.installTranslator(translator)
                    self._translator = translator
                    loaded_locale = 'ja'
        if locale in ('en', 'ja'):
            # 選択言語をボタン表示に反映する（ja訳が未整備でも選択状態自体は保持する）
            loaded_locale = locale
        self._active_locale = loaded_locale

        if self.actions:
            self.actions[0].setText(self.tr('Compartment Compass'))
        if self.dock:
            self.dock.setWindowTitle(self.tr('Compartment Compass'))
        if self.dialog:
            self.dialog.setWindowTitle(self.tr('Compartment Compass'))
        if self.window and hasattr(self.window, 'retranslate_ui'):
            self.window.retranslate_ui()

    def initGui(self):
        if self._previous_excepthook is None:
            self._previous_excepthook = sys.excepthook
            sys.excepthook = self._log_uncaught_exception
        self.set_language(None)
        icon_path = os.path.join(self.plugin_dir, 'icon.svg')
        action = QAction(
            QIcon(icon_path),
            self.tr('Compartment Compass'),
            self.iface.mainWindow(),
        )
        action.triggered.connect(self.run)
        self.iface.addVectorToolBarIcon(action)
        self.iface.addPluginToVectorMenu(self.menu, action)
        self.actions.append(action)

    def unload(self):
        self._remove_translator()

        for action in self.actions:
            self.iface.removePluginVectorMenu(self.menu, action)
            self.iface.removeVectorToolBarIcon(action)
        self.actions = []

        if self.window:
            self.window.cleanup()
        if self.dialog:
            self._save_dialog_geometry()
            self.dialog.hide()
            self.dialog.deleteLater()
            self.dialog = None
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        self.window = None

        if self._previous_excepthook is not None:
            sys.excepthook = self._previous_excepthook
            self._previous_excepthook = None

    def _ensure_window(self):
        if self.window is None:
            self.window = CompartmentCompassWindow(
                self.iface,
                set_language_callback=self.set_language,
                get_language_callback=self.get_active_locale,
            )
            self.window.set_window_mode_callback(self._set_window_mode)

    def _create_dock(self):
        if self.dock is not None:
            return
        self._ensure_window()
        self.dock = CompartmentCompassDockWidget(
            self.tr('Compartment Compass'), self.iface.mainWindow()
        )
        self.dock.setObjectName('CompartmentCompassDock')
        self.dock.setWidget(self.window)
        self.window.attach_dock_widget(self.dock)
        self.dock.setAllowedAreas(
            Qt.DockWidgetArea.TopDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
        )
        self.iface.addDockWidget(
            Qt.DockWidgetArea.BottomDockWidgetArea, self.dock
        )

    def _create_dialog(self):
        if self.dialog is not None:
            return
        self._ensure_window()
        self.dialog = QDialog(None, Qt.WindowType.Window)
        self.dialog.setObjectName('CompartmentCompassWindow')
        self.dialog.setWindowTitle(self.tr('Compartment Compass'))
        layout = QVBoxLayout(self.dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.window)
        self.window.attach_window_dialog(self.dialog)
        self.window.show()
        geometry = QSettings().value(
            'compartment_compass/window_geometry', None
        )
        if geometry:
            self.dialog.restoreGeometry(geometry)
        else:
            self.dialog.resize(1200, 600)
        self.dialog.finished.connect(self._on_dialog_finished)

    def _save_dialog_geometry(self):
        if self.dialog is not None:
            QSettings().setValue(
                'compartment_compass/window_geometry',
                self.dialog.saveGeometry(),
            )

    def _set_window_mode(self, enabled):
        self._ensure_window()
        self._switching_container = True
        try:
            if enabled:
                if self.dock is not None:
                    self.dock.setWidget(None)
                    self.iface.removeDockWidget(self.dock)
                    self.dock.deleteLater()
                    self.dock = None
                self._create_dialog()
                self.dialog.show()
                self.dialog.raise_()
                self.dialog.activateWindow()
            else:
                if self.dialog is not None:
                    self._save_dialog_geometry()
                    layout = self.dialog.layout()
                    if layout is not None:
                        layout.removeWidget(self.window)
                    self.window.setParent(None)
                    self.dialog.hide()
                    self.dialog.deleteLater()
                    self.dialog = None
                self._create_dock()
                self.window.show()
                self.dock.show()
                self.dock.raise_()
        finally:
            self._switching_container = False

    def _on_dialog_finished(self, *_args):
        if self._switching_container or self.window is None:
            return
        self._save_dialog_geometry()

    def run(self):
        """プラグインを実行する。ドックまたは別ウィンドウを表示する。"""
        if self.dialog is not None:
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()
            return

        if self.dock is None:
            self._create_dock()

        if self.dock.isVisible():
            self.dock.raise_()
        else:
            self.dock.show()
            self.dock.raise_()
