# -*- coding: utf-8 -*-
import re

from qgis.PyQt.QtCore import (
    QEvent,
    QItemSelectionModel,
    QPoint,
    QSettings,
    Qt,
    QTimer,
)
from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeatureRequest,
    QgsLayerTreeLayer,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)

from .column_config_dialog import (
    COLUMN_DISPLAY,
    COLUMN_HIDDEN,
    COLUMN_INFO,
    ColumnConfigDialog,
)

_INFO_COLOR = QColor('#e67e22')

# 選択フィーチャーへズームする際の余白（フィーチャーの大きい方の辺に対する比率）
_ZOOM_MARGIN_RATIO = 1.0

# セル値ツールチップの表示時間（ms）。クリック/矢印キーどちらでも同じ長さにする。
_TOOLTIP_DURATION_MS = 3000

# 選択シンボロジ: 色はデフォルトのまま、塗りのみ15%に変更
_SELECTION_ALPHA = 0.15


def _iter_layer_tree_layers(node):
    """レイヤーツリーをレイヤーパネルの表示順（上から下）で辿る。"""
    for child in node.children():
        if isinstance(child, QgsLayerTreeLayer):
            layer = child.layer()
            if layer is not None:
                yield layer
        else:
            yield from _iter_layer_tree_layers(child)


INFO_FIELD_COUNT = 10

# UIのソース言語はenglish。ja訳（i18n/compartment_compass_ja.qm）が揃うまでは
# jaを選んでもenglishのまま表示される。
LANGUAGE_CYCLE = ['en', 'ja']
LANGUAGE_LABELS = {'en': 'English', 'ja': '日本語'}

# 数値列の絞り込みトークン: ">300" ">=300" "<600" "<=600" "!=100" "300"（比較演算子省略時は完全一致）
_NUM_TOKEN_RE = re.compile(r'^(>=|<=|!=|==|>|<)?\s*(-?\d+(?:\.\d+)?)$')


def _numeric_match(cell_text, filter_text):
    try:
        value = float(cell_text)
    except (TypeError, ValueError):
        return False
    for token in filter_text.split(','):
        token = token.strip()
        if not token:
            continue
        m = _NUM_TOKEN_RE.match(token)
        if not m:
            return False
        op, num_text = m.groups()
        num = float(num_text)
        op = op or '=='
        if op == '>' and not value > num:
            return False
        if op == '>=' and not value >= num:
            return False
        if op == '<' and not value < num:
            return False
        if op == '<=' and not value <= num:
            return False
        if op in ('==', '') and not value == num:
            return False
        if op == '!=' and not value != num:
            return False
    return True


def _text_match(cell_text, filter_text):
    return filter_text.lower() in cell_text.lower()


class CompartmentCompassDockWidget(QDockWidget):
    """QGISに格納する標準Dock。ネイティブfloatingは使わない（Closable/Movableのみ）。"""


class CompartmentCompassWindow(QWidget):
    """左右分割の中身ウィジェット。ドック/別ウィンドウどちらのコンテナにも付け替え可能。"""

    def __init__(
        self,
        iface,
        set_language_callback=None,
        get_language_callback=None,
        parent=None,
    ):
        super().__init__(parent)
        self.iface = iface
        self._window_mode_callback = None
        self._set_language_callback = set_language_callback
        self._get_language_callback = get_language_callback
        self._column_config = {}
        self._currentLayer = None
        self._filterIsNumeric = []  # 列ごとのis_numeric（filterTbl列に対応）
        self._settingGroups = []  # retranslate_ui用: (group, title, [labels], clear_btn)
        self._syncing_selection = False
        self._sel_color_layer_id = None
        self._sel_color_orig = None
        self._sel_mode_orig = None
        self._editModeActive = False
        self._populatingTable = False
        self._displayedFieldNames = []
        self._load_saved_state()

        # セル値ツールチップ用のフローティングラベル（QToolTipはマウス位置に応じて
        # 自動で消えてしまうため使わない。表示位置とマウス位置が無関係なこの用途では
        # 自前のラベルで表示時間を完全にコントロールする）。
        self._cellTipLabel = QLabel(None, Qt.WindowType.ToolTip)
        self._cellTipLabel.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._cellTipLabel.setStyleSheet(
            'QLabel { background-color: #ffffdc; color: #000000; '
            'border: 1px solid #767676; padding: 2px 4px; }'
        )
        self._cellTipTimer = QTimer(self)
        self._cellTipTimer.setSingleShot(True)
        self._cellTipTimer.timeout.connect(self._cellTipLabel.hide)

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        top = QHBoxLayout()
        top.addStretch(1)
        self.chkWindowMode = QCheckBox(self.tr('Separate window'))
        top.addWidget(self.chkWindowMode)
        root.addLayout(top)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)

        leftContainer = QWidget()
        leftLayout = QVBoxLayout(leftContainer)
        leftLayout.setContentsMargins(0, 0, 0, 0)

        layerRow = QHBoxLayout()
        self.lblLayer = QLabel(self.tr('Layer:'))
        layerRow.addWidget(self.lblLayer)
        self.cmbLayer = QComboBox()
        self.cmbLayer.setMinimumWidth(200)
        layerRow.addWidget(self.cmbLayer, 1)
        leftLayout.addLayout(layerRow)

        self._leftTabs = QTabWidget()
        self.tabInfo = QWidget()
        self.tabSetting = QWidget()
        self.tabTips = QWidget()
        # タブページ（QWidget）は既定では自身の背景を塗らず、QTabWidgetのペイン
        # 描画（Baseパレット＝白系）がそのまま透けて見え、周囲のUI（Windowパレット）
        # と色味が浮く。ページ自身にWindowパレットで塗るよう明示する。
        # 注意: スタイルシートで実装すると、そのウィジェット配下（コンボボックスの
        # ポップアップ含む）の描画方式が変わり、選択項目のハイライト文字が見えなく
        # なる副作用があったため、QPaletteを直接設定する方式にしている。
        for tab_page in (self.tabInfo, self.tabSetting, self.tabTips):
            tab_page.setAutoFillBackground(True)
            page_palette = tab_page.palette()
            page_palette.setColor(
                tab_page.backgroundRole(),
                page_palette.color(QPalette.ColorRole.Window),
            )
            tab_page.setPalette(page_palette)
        self._build_setting_tab()
        self._build_info_tab()
        self._build_tips_tab()
        self._leftTabs.addTab(self.tabInfo, self.tr('Info'))
        self._leftTabs.addTab(self.tabSetting, self.tr('Setting'))
        self._leftTabs.addTab(self.tabTips, self.tr('Tips'))
        leftLayout.addWidget(self._leftTabs)

        self._splitter.addWidget(leftContainer)

        rightContainer = QWidget()
        rightLayout = QVBoxLayout(rightContainer)
        rightLayout.setContentsMargins(0, 0, 0, 0)

        # 絞り込み入力専用の小さなテーブル（1行のみ）。列名（見出し）はこちら側に
        # 表示する。絞り込みセルは標準のテーブルセル（QLineEditの埋め込みではなく
        # 通常の編集可能セル）で行う。
        self.filterTbl = QTableWidget()
        self.filterTbl.setRowCount(1)
        self.filterTbl.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.filterTbl.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.filterTbl.itemChanged.connect(self._apply_filters)
        # tbl側は行数に応じて縦スクロールバーの有無が変わり、その分だけ列の描画
        # 幅が狭くなる。filterTblは縦スクロールバーを持たないため、そのままだと
        # tbl側にスクロールバーが出た瞬間に列がずれる。右側にスペーサーを置いて
        # 幅を動的に合わせる（QTableWidget自体のviewportMarginsは横/縦ヘッダーの
        # 位置決めに内部で使われており、外から触ると壊れるため使わない）。
        self._filterTblSpacer = QWidget()
        # プレーンなQWidgetはこの属性がないとスタイルシートのbackground-color/
        # borderを描画しない（QFrame系は既定で描画するが素のQWidgetは対象外）。
        self._filterTblSpacer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._filterTblSpacer.setFixedWidth(0)
        filterRow = QHBoxLayout()
        filterRow.setContentsMargins(0, 0, 0, 0)
        filterRow.setSpacing(0)
        filterRow.addWidget(self.filterTbl)
        filterRow.addWidget(self._filterTblSpacer)
        rightLayout.addLayout(filterRow)

        # データテーブルは標準のまま設置する（行番号・スクロールバーはそのまま）。
        # 列名の見出しは上のfilterTbl側に表示するため、こちらの横ヘッダーは非表示。
        self.tbl = QTableWidget()
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # ハイライトはクリックしたセル単体のみ表示する（選択対象の特定は内部的には
        # 行単位で扱う。列0のitemに保持したfidをクリックした行番号から引くため、
        # どの列をクリックしてもその行の地物が選択される）。
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl.horizontalHeader().hide()
        rightLayout.addWidget(self.tbl, 1)

        # 列幅はfilterTbl側のヘッダー操作を基準にtbl側へ反映。横スクロールは双方向で
        # 同期する（tbl側のドラッグ/ホイールだけでなく、filterTbl側でのキーボード
        # フォーカス移動による自動スクロールにもtbl側が追従するように）。
        self.filterTbl.horizontalHeader().sectionResized.connect(
            self._sync_data_column_width
        )
        self.tbl.horizontalScrollBar().valueChanged.connect(
            self.filterTbl.horizontalScrollBar().setValue
        )
        self.filterTbl.horizontalScrollBar().valueChanged.connect(
            self.tbl.horizontalScrollBar().setValue
        )

        # tblは行数によって縦スクロールバーの有無が変わり、その分だけ列を描画できる
        # 幅が狭くなる。filterTblは縦スクロールバーを持たないため、そのままだと
        # tbl側にスクロールバーが出た瞬間に列がずれる。tbl側と同じ幅を右端に
        # 余白として確保することでビューポート幅を揃える。
        self.tbl.verticalScrollBar().rangeChanged.connect(
            self._sync_filter_viewport_margin
        )

        # 2つのテーブルを1つの表であるかのように矢印キーで行き来できるようにする
        # （↓でfilterTbl→tbl先頭行、↑でtbl先頭行→filterTblへフォーカス移動）。
        self.filterTbl.installEventFilter(self)
        self.tbl.installEventFilter(self)

        bottomRow = QHBoxLayout()
        self.lblCount = QLabel('')
        bottomRow.addWidget(self.lblCount)
        bottomRow.addSpacing(12)
        self.lblLegendDisplay = QLabel(self.tr('■ Display'))
        self.lblLegendDisplay.setStyleSheet('color: #000000; font-size: 11px;')
        bottomRow.addWidget(self.lblLegendDisplay)
        self.lblLegendInfo = QLabel(self.tr('■ Info (trailing)'))
        self.lblLegendInfo.setStyleSheet(
            f'color: {_INFO_COLOR.name()}; font-size: 11px;'
        )
        bottomRow.addWidget(self.lblLegendInfo)
        bottomRow.addStretch(1)
        self.btnEditMode = QPushButton(self.tr('Edit'))
        self.btnEditMode.setCheckable(True)
        self.btnEditMode.setEnabled(False)
        self.btnEditMode.toggled.connect(self._on_edit_mode_toggled)
        bottomRow.addWidget(self.btnEditMode)
        self.btnLanguage = QPushButton('')
        self.btnLanguage.clicked.connect(self._cycle_language)
        bottomRow.addWidget(self.btnLanguage)
        rightLayout.addLayout(bottomRow)

        self._splitter.addWidget(rightContainer)
        self._splitter.setSizes([380, 820])
        root.addWidget(self._splitter, 1)

        self.cmbLayer.currentIndexChanged.connect(self._on_layer_selected)
        self.chkWindowMode.toggled.connect(self._on_window_mode_toggled)
        self.tbl.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.tbl.itemChanged.connect(self._on_table_item_changed)

        QgsProject.instance().layersAdded.connect(self._refresh_layer_combo)
        QgsProject.instance().layersRemoved.connect(self._refresh_layer_combo)

        self._refresh_layer_combo()
        self._update_language_button()

    # -- filterTbl/tblを1つの表として矢印キーで行き来する -------------------

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            if obj is self.filterTbl and event.key() == Qt.Key.Key_Down:
                self._focus_table_first_row()
                return True
            if obj is self.tbl and event.key() == Qt.Key.Key_Up:
                if self.tbl.currentRow() == self._first_visible_row():
                    self._focus_filter_row()
                    return True
        return super().eventFilter(obj, event)

    def _first_visible_row(self):
        for row in range(self.tbl.rowCount()):
            if not self.tbl.isRowHidden(row):
                return row
        return -1

    def _focus_table_first_row(self):
        row = self._first_visible_row()
        if row < 0:
            return
        col = max(self.filterTbl.currentColumn(), 0)
        self.filterTbl.clearSelection()
        self.tbl.setCurrentCell(row, col)
        self.tbl.setFocus()

    def _focus_filter_row(self):
        col = max(self.tbl.currentColumn(), 0)
        # tbl側の選択解除が地図側の選択同期（層への selectByIds）を
        # 巻き込まないよう、同期処理を一時停止する。
        self._syncing_selection = True
        try:
            self.tbl.clearSelection()
        finally:
            self._syncing_selection = False
        self.filterTbl.setCurrentCell(0, col)
        self.filterTbl.setFocus()

    # -- コンテナ付け替え -------------------------------------------------

    def attach_dock_widget(self, dock_widget):
        self._set_window_mode_checked(False)
        self.show()

    def attach_window_dialog(self, dialog):
        self._set_window_mode_checked(True)
        self.show()

    def set_window_mode_callback(self, callback):
        self._window_mode_callback = callback

    def _set_window_mode_checked(self, checked):
        self.chkWindowMode.blockSignals(True)
        self.chkWindowMode.setChecked(checked)
        self.chkWindowMode.blockSignals(False)

    def _on_window_mode_toggled(self, checked):
        if self._window_mode_callback is None:
            self._set_window_mode_checked(False)
            return
        self._window_mode_callback(checked)

    # -- 言語切り替え -----------------------------------------------------

    def _current_language_code(self):
        if self._get_language_callback:
            return self._get_language_callback()
        return 'en'

    def _update_language_button(self):
        code = self._current_language_code()
        label = LANGUAGE_LABELS.get(code, code)
        self.btnLanguage.setText(f'Language: {label}')

    def _cycle_language(self):
        current = self._current_language_code()
        if current not in LANGUAGE_CYCLE:
            current = 'en'
        idx = LANGUAGE_CYCLE.index(current)
        next_locale = LANGUAGE_CYCLE[(idx + 1) % len(LANGUAGE_CYCLE)]
        if self._set_language_callback:
            self._set_language_callback(next_locale)
        self.retranslate_ui()

    def retranslate_ui(self):
        self._update_language_button()
        self.lblLayer.setText(self.tr('Layer:'))
        self.chkWindowMode.setText(self.tr('Separate window'))
        self._leftTabs.setTabText(0, self.tr('Info'))
        self._leftTabs.setTabText(1, self.tr('Setting'))
        self._leftTabs.setTabText(2, self.tr('Tips'))
        if self.cmbLayer.count() > 0:
            self.cmbLayer.setItemText(0, self.tr('-- Select layer --'))
        self.lblLegendDisplay.setText(self.tr('■ Display'))
        self.lblLegendInfo.setText(self.tr('■ Info (trailing)'))
        self.btnEditMode.setText(self.tr('Edit'))
        for group, title_key, labels, clear_btn in self._settingGroups:
            group.setTitle(self.tr(title_key))
            for i, label in enumerate(labels):
                label.setText(self.tr('Field {0}:').format(i + 1))
            if clear_btn is not None:
                clear_btn.setText(self.tr('Clear'))
        self.grpTableSetting.setTitle(self.tr('Table setting'))
        self.btnColumnConfig.setText(self.tr('Column config'))

    # -- 状態の保存/復元（レイヤー単位ではなく直近の状態のみ） --------------

    def _load_saved_state(self):
        settings = QSettings()
        self._saved_info_fields = list(
            settings.value('compartment_compass/info_fields', []) or []
        )
        self._saved_hidden_fields = set(
            settings.value('compartment_compass/hidden_fields', []) or []
        )
        self._saved_info_columns = set(
            settings.value('compartment_compass/info_columns', []) or []
        )

    def _save_state(self, *_args):
        settings = QSettings()
        settings.setValue(
            'compartment_compass/info_fields',
            [c.currentText() for c in self.infoFieldCombos],
        )
        hidden = [
            name for name, state in self._column_config.items()
            if state == COLUMN_HIDDEN
        ]
        settings.setValue('compartment_compass/hidden_fields', hidden)
        info_columns = [
            name for name, state in self._column_config.items()
            if state == COLUMN_INFO
        ]
        settings.setValue('compartment_compass/info_columns', info_columns)

    def cleanup(self):
        self._cellTipTimer.stop()
        self._cellTipLabel.hide()
        self._disconnect_selection_signal(self._currentLayer)
        self._restore_selection_color()
        try:
            QgsProject.instance().layersAdded.disconnect(self._refresh_layer_combo)
            QgsProject.instance().layersRemoved.disconnect(self._refresh_layer_combo)
        except TypeError:
            pass

    # -- レイヤー一覧・テーブル --------------------------------------------

    def _refresh_layer_combo(self, *_args):
        current_id = self.cmbLayer.currentData()
        self.cmbLayer.blockSignals(True)
        self.cmbLayer.clear()
        self.cmbLayer.addItem(self.tr('-- Select layer --'), None)
        root = QgsProject.instance().layerTreeRoot()
        for layer in _iter_layer_tree_layers(root):
            if not isinstance(layer, QgsVectorLayer):
                continue
            self.cmbLayer.addItem(layer.name(), layer.id())
        if current_id is not None:
            idx = self.cmbLayer.findData(current_id)
            if idx >= 0:
                self.cmbLayer.setCurrentIndex(idx)
        self.cmbLayer.blockSignals(False)
        if current_id != self.cmbLayer.currentData():
            self._on_layer_selected(self.cmbLayer.currentIndex())

    def _on_layer_selected(self, _index):
        layer_id = self.cmbLayer.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if self._editModeActive and not self._try_exit_edit_mode():
            self._revert_layer_combo_selection()
            return
        self._disconnect_selection_signal(self._currentLayer)
        self._restore_selection_color()
        self._currentLayer = layer
        self._reset_column_config(layer)
        self._populate_table(layer)
        self._refresh_setting_combos(layer)
        if layer is not None:
            self._apply_selection_color(layer)
            self._connect_selection_signal(layer)
            self._sync_table_selection_from_layer()
        else:
            self._update_info_tab()
        self.btnEditMode.setEnabled(self._is_gpkg_layer(layer))

    def _revert_layer_combo_selection(self):
        current_id = self._currentLayer.id() if self._currentLayer else None
        idx = self.cmbLayer.findData(current_id)
        self.cmbLayer.blockSignals(True)
        self.cmbLayer.setCurrentIndex(max(idx, 0))
        self.cmbLayer.blockSignals(False)

    # -- 属性編集（GPKGレイヤーのみ） ---------------------------------------

    @staticmethod
    def _is_gpkg_layer(layer):
        if layer is None:
            return False
        return layer.dataProvider().storageType() == 'GPKG'

    def _set_edit_mode_checked(self, checked):
        self.btnEditMode.blockSignals(True)
        self.btnEditMode.setChecked(checked)
        self.btnEditMode.blockSignals(False)

    def _on_edit_mode_toggled(self, checked):
        if checked:
            self._enter_edit_mode()
        elif not self._try_exit_edit_mode():
            self._set_edit_mode_checked(True)

    def _enter_edit_mode(self):
        layer = self._currentLayer
        if not self._is_gpkg_layer(layer):
            self._set_edit_mode_checked(False)
            return
        layer.startEditing()
        self._editModeActive = True
        self.tbl.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
        )

    def _try_exit_edit_mode(self):
        if not self._editModeActive:
            return True
        layer = self._currentLayer
        if layer is not None and layer.isEditable():
            if layer.isModified():
                result = QMessageBox.question(
                    self,
                    self.tr('Save changes?'),
                    self.tr(
                        'There are unsaved edits. Save changes before '
                        'leaving edit mode?'
                    ),
                    QMessageBox.StandardButton.Save
                    | QMessageBox.StandardButton.Discard
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Save,
                )
                if result == QMessageBox.StandardButton.Cancel:
                    return False
                if result == QMessageBox.StandardButton.Save:
                    layer.commitChanges()
                else:
                    layer.rollBack()
            else:
                layer.commitChanges()
        self._editModeActive = False
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._set_edit_mode_checked(False)
        return True

    def _on_table_item_changed(self, item):
        if self._populatingTable or not self._editModeActive:
            return
        layer = self._currentLayer
        if layer is None:
            return
        col = item.column()
        if col >= len(self._displayedFieldNames):
            return
        fid_item = self.tbl.item(item.row(), 0)
        if fid_item is None:
            return
        fid = fid_item.data(Qt.ItemDataRole.UserRole)
        field_index = layer.fields().indexFromName(self._displayedFieldNames[col])
        if field_index < 0:
            return
        layer.changeAttributeValue(fid, field_index, item.text())

    # -- 選択シンボロジ（塗り15%・赤に一時変更し、レイヤー切替/終了時に復元） -----

    def _apply_selection_color(self, layer):
        sp = layer.selectionProperties()
        self._sel_color_layer_id = layer.id()
        self._sel_color_orig = QColor(sp.selectionColor())
        self._sel_mode_orig = sp.selectionRenderingMode()

        base = sp.selectionColor()
        if not base.isValid():
            base = QgsProject.instance().selectionColor()
        faded = QColor(base)
        faded.setAlphaF(_SELECTION_ALPHA)
        sp.setSelectionColor(faded)
        sp.setSelectionRenderingMode(Qgis.SelectionRenderingMode.CustomColor)
        layer.triggerRepaint()
        self.iface.mapCanvas().refresh()

    def _restore_selection_color(self):
        if not self._sel_color_layer_id:
            return
        layer = QgsProject.instance().mapLayer(self._sel_color_layer_id)
        if layer is not None:
            sp = layer.selectionProperties()
            sp.setSelectionColor(self._sel_color_orig or QColor())
            sp.setSelectionRenderingMode(self._sel_mode_orig)
            layer.triggerRepaint()
            self.iface.mapCanvas().refresh()
        self._sel_color_layer_id = None
        self._sel_color_orig = None
        self._sel_mode_orig = None

    # -- テーブル ⇔ 地図の選択同期 ------------------------------------------

    def _get_selected_fids(self):
        rows = {idx.row() for idx in self.tbl.selectionModel().selectedIndexes()}
        fids = []
        for row in rows:
            item = self.tbl.item(row, 0)
            if item is not None:
                fid = item.data(Qt.ItemDataRole.UserRole)
                if fid is not None:
                    fids.append(fid)
        return fids

    def _on_table_selection_changed(self):
        if self._syncing_selection:
            return
        layer = self._currentLayer
        if layer is None:
            return
        self._syncing_selection = True
        try:
            layer.selectByIds(self._get_selected_fids())
        finally:
            self._syncing_selection = False
        self._refresh_layer_selection(layer)
        self._zoom_to_selection(layer)
        self._show_cell_tooltip(layer)
        self._update_info_tab()

    def _show_cell_tooltip(self, layer):
        """テーブルで選択中のセルの値を、対応する地物の位置にツールチップ表示する。"""
        item = self.tbl.currentItem()
        if item is None:
            return
        fid_item = self.tbl.item(item.row(), 0)
        fid = fid_item.data(Qt.ItemDataRole.UserRole) if fid_item is not None else None
        if fid is None:
            return

        feature = next(
            layer.getFeatures(QgsFeatureRequest().setFilterFid(fid)), None
        )
        if feature is None or feature.geometry().isNull():
            return
        point = feature.geometry().centroid().asPoint()

        canvas = self.iface.mapCanvas()
        layer_crs = layer.crs()
        canvas_crs = canvas.mapSettings().destinationCrs()
        if layer_crs.isValid() and canvas_crs.isValid() and layer_crs != canvas_crs:
            transform = QgsCoordinateTransform(
                layer_crs, canvas_crs, QgsProject.instance()
            )
            try:
                point = transform.transform(point)
            except QgsCsException:
                return

        device_point = canvas.mapSettings().mapToPixel().transform(point)
        global_pos = canvas.mapToGlobal(
            QPoint(round(device_point.x()), round(device_point.y()))
        )
        # QToolTipはマウスカーソルの実位置とここで指定した位置/rectとの関係を見て
        # 自動的に消す仕組みのため、クリック起点（カーソルはテーブル側にあり、
        # 表示位置の地図キャンバスとは無関係）だとほぼ即座に消えてしまう。
        # マウス位置に左右されない自前のフローティングラベルに置き換える。
        text = item.text()

        def _show():
            self._cellTipLabel.setText(text)
            self._cellTipLabel.adjustSize()
            self._cellTipLabel.move(global_pos)
            self._cellTipLabel.show()
            self._cellTipTimer.start(_TOOLTIP_DURATION_MS)

        QTimer.singleShot(0, _show)

    def _zoom_to_selection(self, layer):
        """選択フィーチャーへパン/ズームする。余白は上下/左右それぞれフィーチャー
        サイズの50%とし、大きい方の余白を四辺すべてに適用する（縦横比を保つため）。
        """
        if not layer.selectedFeatureIds():
            return
        bbox = layer.boundingBoxOfSelected()
        if bbox.isNull():
            return

        canvas = self.iface.mapCanvas()
        layer_crs = layer.crs()
        canvas_crs = canvas.mapSettings().destinationCrs()
        if layer_crs.isValid() and canvas_crs.isValid() and layer_crs != canvas_crs:
            transform = QgsCoordinateTransform(
                layer_crs, canvas_crs, QgsProject.instance()
            )
            try:
                bbox = transform.transformBoundingBox(bbox)
            except QgsCsException:
                return

        margin = max(bbox.width(), bbox.height()) * _ZOOM_MARGIN_RATIO
        if margin <= 0:
            canvas.setCenter(bbox.center())
        else:
            canvas.setExtent(
                QgsRectangle(
                    bbox.xMinimum() - margin,
                    bbox.yMinimum() - margin,
                    bbox.xMaximum() + margin,
                    bbox.yMaximum() + margin,
                )
            )
        canvas.refresh()

    def _connect_selection_signal(self, layer):
        if layer is None:
            return
        try:
            layer.selectionChanged.connect(self._on_layer_selection_changed)
        except RuntimeError:
            pass  # レイヤーが接続直前に削除された場合のみ発生

    def _disconnect_selection_signal(self, layer):
        if layer is None:
            return
        try:
            layer.selectionChanged.disconnect(self._on_layer_selection_changed)
        except (TypeError, RuntimeError):
            pass  # 未接続、または削除済みレイヤーの場合

    def _on_layer_selection_changed(self, *_args):
        if self._syncing_selection:
            return
        self._sync_table_selection_from_layer()

    def _sync_table_selection_from_layer(self):
        layer = self._currentLayer
        if layer is None:
            return
        selected_ids = set(layer.selectedFeatureIds())
        self._syncing_selection = True
        try:
            self.tbl.clearSelection()
            selection_model = self.tbl.selectionModel()
            if selection_model is None:
                return
            model = self.tbl.model()
            # SelectItems設定に合わせ、行全体ではなく列0のセルのみをハイライトする。
            select_flags = QItemSelectionModel.SelectionFlag.Select
            first_selected_row = None
            for row in range(self.tbl.rowCount()):
                item = self.tbl.item(row, 0)
                if (
                    item is not None
                    and item.data(Qt.ItemDataRole.UserRole) in selected_ids
                ):
                    selection_model.select(model.index(row, 0), select_flags)
                    if first_selected_row is None and not self.tbl.isRowHidden(row):
                        first_selected_row = row
            if first_selected_row is not None:
                first_visible_col = next(
                    (
                        col for col in range(self.tbl.columnCount())
                        if not self.tbl.isColumnHidden(col)
                    ),
                    0,
                )
                scroll_item = self.tbl.item(first_selected_row, first_visible_col)
                if scroll_item is not None:
                    self.tbl.scrollToItem(
                        scroll_item,
                        QAbstractItemView.ScrollHint.PositionAtCenter,
                    )
                # NoUpdateで選択状態を変えずにcurrentItemだけ合わせる（Infoタブ用）。
                selection_model.setCurrentIndex(
                    model.index(first_selected_row, first_visible_col),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            self._syncing_selection = False
        self._refresh_layer_selection(layer)
        self._update_info_tab()

    def _refresh_layer_selection(self, layer):
        if layer is None:
            return
        layer.triggerRepaint()
        self.iface.mapCanvas().refresh()

    def _reset_column_config(self, layer):
        field_names = [field.name() for field in layer.fields()] if layer else []
        self._column_config = {
            name: self._column_config.get(name, self._default_column_state(name))
            for name in field_names
        }

    def _default_column_state(self, name):
        if name in self._saved_hidden_fields:
            return COLUMN_HIDDEN
        if name in self._saved_info_columns:
            return COLUMN_INFO
        return COLUMN_DISPLAY

    def _sync_data_column_width(self, logical_index, _old_size, new_size):
        self.tbl.setColumnWidth(logical_index, new_size)

    def _sync_filter_viewport_margin(self, *_args):
        scrollbar = self.tbl.verticalScrollBar()
        # isVisible()はレイアウト状況によっては信用できないため、スクロール範囲
        # （最大値>最小値＝実際にスクロールが必要）で判定する。
        needed = scrollbar.maximum() > scrollbar.minimum()
        width = scrollbar.sizeHint().width() if needed else 0
        self._filterTblSpacer.setFixedWidth(width)
        # 空白に見えないよう、スペーサーが実際に必要な時だけ枠線を付ける。
        self._filterTblSpacer.setStyleSheet(
            'border-top: 1px solid #8c8c8c; border-right: 1px solid #8c8c8c; '
            'border-bottom: 1px solid #8c8c8c; border-left: none;'
            if needed
            else ''
        )

    def _fit_filter_table_height(self):
        # 1行しか使わないので、見出し＋行の高さ＋枠線ぶんだけで固定する
        # （既定のサイズポリシーのままだと余白が大きく空くため）。
        header_h = self.filterTbl.horizontalHeader().height()
        row_h = self.filterTbl.rowHeight(0) if self.filterTbl.rowCount() else 0
        frame = 2 * self.filterTbl.frameWidth()
        self.filterTbl.setFixedHeight(header_h + row_h + frame)

    def _populate_table(self, layer):
        previous_syncing = self._syncing_selection
        self._syncing_selection = True
        self._populatingTable = True
        try:
            self.tbl.clear()
            self.filterTbl.clear()
            self.filterTbl.setRowCount(1)
            self._filterIsNumeric = []
            if layer is None:
                self.tbl.setRowCount(0)
                self.tbl.setColumnCount(0)
                self.filterTbl.setColumnCount(0)
                self.lblCount.setText('')
                self._displayedFieldNames = []
                return

            # 表示順: 表示 → 情報（後列） → 非表示（非表示列は末尾でよい、どうせ隠れる）
            _order = {COLUMN_DISPLAY: 0, COLUMN_INFO: 1, COLUMN_HIDDEN: 2}
            ordered_fields = sorted(
                layer.fields(),
                key=lambda f: _order.get(self._column_config.get(f.name()), 0),
            )
            headers = [field.name() for field in ordered_fields]
            self._displayedFieldNames = headers

            self.filterTbl.setColumnCount(len(headers))
            self.filterTbl.setHorizontalHeaderLabels(headers)
            self.tbl.setColumnCount(len(headers))
            for col, name in enumerate(headers):
                if self._column_config.get(name) == COLUMN_INFO:
                    self.filterTbl.horizontalHeaderItem(col).setForeground(
                        _INFO_COLOR
                    )

            features = list(layer.getFeatures())
            self.tbl.setRowCount(len(features))

            field_indexes = [layer.fields().indexFromName(name) for name in headers]
            for row, feature in enumerate(features):
                attributes = feature.attributes()
                for col, field_index in enumerate(field_indexes):
                    value = attributes[field_index]
                    item = QTableWidgetItem('' if value is None else str(value))
                    if col == 0:
                        item.setData(Qt.ItemDataRole.UserRole, feature.id())
                    self.tbl.setItem(row, col, item)

            for col, name in enumerate(headers):
                hidden = self._column_config.get(name) == COLUMN_HIDDEN
                self.tbl.setColumnHidden(col, hidden)
                self.filterTbl.setColumnHidden(col, hidden)

            # 絞り込みセルは標準の編集可能セル（QLineEditの埋め込みではない）
            for col, field in enumerate(ordered_fields):
                item = QTableWidgetItem('')
                self.filterTbl.setItem(0, col, item)
                self._filterIsNumeric.append(field.isNumeric())

            # 列幅をtbl側にも反映する（filterTbl側のヘッダーを基準とする）
            for col in range(len(headers)):
                self.tbl.setColumnWidth(col, self.filterTbl.columnWidth(col))

            # 縦見出しはtbl側の行番号ガター幅に合わせて確保する。
            # ラベルは行番号の代わりに絞り込み行であることを示すマークを表示する。
            mark_item = QTableWidgetItem('\U0001F50D')  # 🔍
            mark_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.filterTbl.setVerticalHeaderItem(0, mark_item)
            self.filterTbl.verticalHeader().setFixedWidth(
                self.tbl.verticalHeader().sizeHint().width()
            )

            self._fit_filter_table_height()
            self._sync_filter_viewport_margin()
            self._apply_filters()
        finally:
            self._syncing_selection = previous_syncing
            self._populatingTable = False

    def _apply_filters(self, *_args):
        active = [
            (col, item.text().strip(), self._filterIsNumeric[col])
            for col in range(self.filterTbl.columnCount())
            for item in [self.filterTbl.item(0, col)]
            if item is not None and item.text().strip()
        ]

        visible_count = 0
        for row in range(self.tbl.rowCount()):
            show = True
            for col, text, is_numeric in active:
                item = self.tbl.item(row, col)
                cell_text = item.text() if item else ''
                matched = (
                    _numeric_match(cell_text, text)
                    if is_numeric
                    else _text_match(cell_text, text)
                )
                if not matched:
                    show = False
                    break
            self.tbl.setRowHidden(row, not show)
            if show:
                visible_count += 1

        self.lblCount.setText(self.tr('{0} features').format(visible_count))

    # -- Settingタブ ---------------------------------------------------

    def _build_setting_tab(self):
        layout = QVBoxLayout(self.tabSetting)

        self.infoFieldCombos = self._build_field_group(
            layout, 'Info fields', INFO_FIELD_COUNT, with_clear=True
        )

        self.grpTableSetting = QGroupBox(self.tr('Table setting'))
        tableLayout = QVBoxLayout(self.grpTableSetting)
        self.btnColumnConfig = QPushButton(self.tr('Column config'))
        self.btnColumnConfig.clicked.connect(self._on_column_config)
        tableLayout.addWidget(self.btnColumnConfig)
        layout.addWidget(self.grpTableSetting)

        layout.addStretch(1)

        for combo in self.infoFieldCombos:
            combo.currentIndexChanged.connect(self._update_info_tab)

    def _build_info_tab(self):
        layout = QVBoxLayout(self.tabInfo)

        # 修正前の内容（ID行＋Info fields 6行）をそのまま、タイトル無しの
        # グループひとつにまとめて囲む。
        self.infoGroup = QGroupBox('')
        groupLayout = QVBoxLayout(self.infoGroup)

        # 設定不要・常時固定で先頭に表示する（データの分類・整備状況に関わらず
        # QGISが必ず割り振るfeature.id()を「個体識別番号」として使う。あくまで
        # QGIS内部の一時的な番号であり永続的な業務キーではないことが伝わるよう
        # 名称で明示する）。
        fidRow = QHBoxLayout()
        self.infoFidNameLabel = QLabel(self.tr('QGIS internal ID:'))
        self.infoFidNameLabel.setStyleSheet('font-weight: bold;')
        self.infoFidValueLabel = QLabel('')
        # px指定はDPI/OSのスケーリングで意図しない大きさになりうるため、
        # ポイントサイズの相対指定（+2）でフォントを直接調整する。フォント差だけ
        # だと目立たないため、バッジ風の背景・角丸も付けて視認性を上げる。
        fidValueFont = self.infoFidValueLabel.font()
        fidValueFont.setBold(True)
        fidValueFont.setPointSize(fidValueFont.pointSize() + 2)
        self.infoFidValueLabel.setFont(fidValueFont)
        self.infoFidValueLabel.setStyleSheet(
            'background-color: #e3f2fd; color: #1565c0;'
            'border-radius: 4px; padding: 1px 8px;'
        )
        fidRow.addWidget(self.infoFidNameLabel)
        fidRow.addWidget(self.infoFidValueLabel)
        fidRow.addStretch(1)
        groupLayout.addLayout(fidRow)

        self.infoValueLabels = []
        for _ in range(INFO_FIELD_COUNT):
            row = QHBoxLayout()
            nameLabel = QLabel('')
            nameLabel.setStyleSheet('font-weight: bold;')
            valueLabel = QLabel('')
            valueLabel.setWordWrap(True)
            row.addWidget(nameLabel)
            row.addWidget(valueLabel, 1)
            groupLayout.addLayout(row)
            self.infoValueLabels.append((nameLabel, valueLabel))

        layout.addWidget(self.infoGroup)
        layout.addStretch(1)

    def _update_info_tab(self, *_args):
        layer = self._currentLayer
        item = self.tbl.currentItem()
        fid = None
        if item is not None:
            fid_item = self.tbl.item(item.row(), 0)
            fid = fid_item.data(Qt.ItemDataRole.UserRole) if fid_item is not None else None

        feature = None
        if layer is not None and fid is not None:
            feature = next(
                layer.getFeatures(QgsFeatureRequest().setFilterFid(fid)), None
            )

        self.infoFidValueLabel.setText('' if feature is None else str(feature.id()))

        field_names = [f.name() for f in feature.fields()] if feature is not None else []
        for combo, (nameLabel, valueLabel) in zip(
            self.infoFieldCombos, self.infoValueLabels
        ):
            field_name = combo.currentText()
            if not field_name:
                nameLabel.setText('')
                valueLabel.setText('')
                continue
            nameLabel.setText(f'{field_name}:')
            if feature is not None and field_name in field_names:
                value = feature[field_name]
                valueLabel.setText('' if value is None else str(value))
            else:
                valueLabel.setText('')

    def _build_tips_tab(self):
        layout = QVBoxLayout(self.tabTips)
        self._build_tips_group(layout)
        layout.addStretch(1)

    def _build_tips_group(self, parent_layout):
        group = QGroupBox(self.tr('Tips'))
        tipsLayout = QVBoxLayout(group)

        ruleLabel = QLabel(self.tr(
            'Text columns: substring match, case-insensitive.\n'
            'Numeric columns: comma-separated comparisons '
            '(>, >=, <, <=, ==, !=). No operator = exact match.'
        ))
        ruleLabel.setWordWrap(True)
        tipsLayout.addWidget(ruleLabel)

        examples = [
            ('>=100', self.tr('100 or more')),
            ('<200', self.tr('Less than 200')),
            ('>=100,<200', self.tr('100 or more, less than 200')),
            ('!=0', self.tr('Not equal to 0')),
            ('100', self.tr('Exactly 100')),
        ]
        grid = QGridLayout()
        for row, (pattern, explanation) in enumerate(examples):
            patternLabel = QLabel(pattern)
            patternLabel.setStyleSheet('font-family: monospace;')
            grid.addWidget(patternLabel, row, 0)
            btnCopy = QPushButton(self.tr('Copy'))
            btnCopy.clicked.connect(
                lambda _checked, text=pattern: self._copy_to_clipboard(text)
            )
            grid.addWidget(btnCopy, row, 1)
            explLabel = QLabel(explanation)
            grid.addWidget(explLabel, row, 2)
        grid.setColumnStretch(2, 1)
        tipsLayout.addLayout(grid)

        parent_layout.addWidget(group)

    @staticmethod
    def _copy_to_clipboard(text):
        QApplication.clipboard().setText(text)

    def _on_column_config(self):
        layer = self._currentLayer
        if layer is None:
            return
        field_names = [field.name() for field in layer.fields()]
        dialog = ColumnConfigDialog(field_names, self._column_config, self)
        if dialog.exec() == ColumnConfigDialog.DialogCode.Accepted:
            self._column_config = dialog.get_config()
            self._populate_table(layer)
            self._sync_table_selection_from_layer()
            self._refresh_setting_combos(layer)
            self._save_state()

    def _build_field_group(self, parent_layout, title_key, count, with_clear=False):
        group = QGroupBox(self.tr(title_key))
        grid = QGridLayout(group)
        combos = []
        labels = []
        for i in range(count):
            row, pair = divmod(i, 2)
            label = QLabel(self.tr('Field {0}:').format(i + 1))
            grid.addWidget(label, row, pair * 2)
            labels.append(label)
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._save_state)
            grid.addWidget(combo, row, pair * 2 + 1)
            combos.append(combo)
        clear_btn = None
        if with_clear:
            clear_row = (count - 1) // 2 + 1
            clear_btn = QPushButton(self.tr('Clear'))
            clear_btn.clicked.connect(lambda: self._clear_combos(combos))
            grid.addWidget(clear_btn, clear_row, 3)
        parent_layout.addWidget(group)
        self._settingGroups.append((group, title_key, labels, clear_btn))
        return combos

    @staticmethod
    def _clear_combos(combos):
        for combo in combos:
            combo.setCurrentIndex(0)

    def _refresh_setting_combos(self, layer):
        all_names = [field.name() for field in layer.fields()] if layer else []
        self._refresh_combo_group(
            self.infoFieldCombos, all_names, self._saved_info_fields
        )

    @staticmethod
    def _refresh_combo_group(combos, field_names, saved_values=None):
        saved_values = saved_values or []
        for i, combo in enumerate(combos):
            current = combo.currentText()
            if not current and i < len(saved_values):
                current = saved_values[i]
            combo.blockSignals(True)
            combo.clear()
            combo.addItem('')
            combo.addItems(field_names)
            idx = combo.findText(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
