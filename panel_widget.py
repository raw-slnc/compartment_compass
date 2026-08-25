# -*- coding: utf-8 -*-
import re

from qgis.PyQt.QtCore import (
    QEvent,
    QItemSelectionModel,
    QPoint,
    QSettings,
    QSortFilterProxyModel,
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
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableView,
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
    QgsVectorLayerCache,
)
from qgis.gui import QgsAttributeTableModel

from .column_config_dialog import (
    COLUMN_DISPLAY,
    COLUMN_HIDDEN,
    COLUMN_INFO,
    ColumnConfigDialog,
)

_INFO_COLOR = QColor('#e67e22')
# 結合(join)由来の列。GPKGレイヤーであっても結合元がGPKGとは限らないため、
# 編集可否とは別に「このデータの由来」を示す目的の色。
_JOIN_COLOR = QColor('#2e7d32')

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


# 大規模レイヤーでも全件を即座にウィジェット化しない（QGIS標準の属性テーブルと
# 同じ QgsVectorLayerCache のキャッシュ上限）。行数はロード直後に確定するが、
# 実際の属性値は表示に必要な範囲だけ遅延取得される。
_LAYER_CACHE_SIZE = 50000

# 絞り込み反映のデバウンス時間（ms）。大規模レイヤーで1文字ごとにinvalidateFilter
# （全行評価）が走ると重いため、入力が止まってから反映する。
_FILTER_DEBOUNCE_MS = 250


class _ColumnFilterProxyModel(QSortFilterProxyModel):
    """列ごとのExcel風絞り込み（テキスト部分一致／数値比較式）を行うプロキシ。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Noneは「絞り込み無し（全件通す）」。集合を渡すとそのfidだけ通す。
        # 判定を軽くするため、実際のテキスト一致判定は事前にまとめて計算し
        # （_recompute_filter_matches）、ここでは集合の所属確認だけにする。
        # filterAcceptsRow内で列ごとにindex.data()を呼ぶと、行数分×列数分の
        # キャッシュ/プロバイダ往復が発生し大規模レイヤーで致命的に遅くなる
        # ことが実機で判明したため（14万件で約29秒）。
        self._matchingFids = None
        self._readonlyColumns = set()  # 結合(join)由来の列のモデル列番号

    def set_matching_fids(self, fids):
        self._matchingFids = fids
        self.invalidateFilter()

    def set_readonly_columns(self, cols):
        self._readonlyColumns = set(cols)

    def flags(self, index):
        base_flags = super().flags(index)
        if index.column() in self._readonlyColumns:
            base_flags &= ~Qt.ItemFlag.ItemIsEditable
        return base_flags

    def filterAcceptsRow(self, source_row, source_parent):
        if self._matchingFids is None:
            return True
        fid = self.sourceModel().rowToId(source_row)
        return fid in self._matchingFids

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        # QgsAttributeTableModel.setData()は実機確認の結果、期待通りに
        # layer.changeAttributeValue()へ書き戻さなかった（flags()は編集可を返す
        # のにsetDataがFalseを返す）ため、FeatureIdRole/FieldIndexRoleを使って
        # 直接レイヤーへ書き込む（既存の実装で確認済みの経路）。
        if role != Qt.ItemDataRole.EditRole:
            return super().setData(index, value, role)
        if index.column() in self._readonlyColumns:
            return False
        source_index = self.mapToSource(index)
        source_model = self.sourceModel()
        if source_model is None:
            return False
        layer = source_model.layer()
        fid = source_index.data(QgsAttributeTableModel.FeatureIdRole)
        field_index = source_model.data(
            source_index, QgsAttributeTableModel.FieldIndexRole
        )
        if layer is None or fid is None or field_index is None:
            return False
        ok = layer.changeAttributeValue(fid, field_index, value)
        if ok:
            self.dataChanged.emit(
                index, index, [role, Qt.ItemDataRole.DisplayRole]
            )
        return ok


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
        # retranslate_ui用: (group, title, [labels], clear_btn)
        self._settingGroups = []
        self._syncing_selection = False
        self._sel_color_layer_id = None
        self._sel_color_orig = None
        self._sel_mode_orig = None
        self._editModeActive = False
        self._populatingTable = False
        self._showAllFields = False
        self._currentLayerTypeStr = ''
        self._displayedFieldNames = []
        self._modelColumnOrder = []  # 表示順(ordered_fields)の位置→モデル上の列番号
        self._modelColToFieldIndex = {}  # モデル上の列番号→layer.fields()のインデックス
        self._layerCache = None
        self._tableModel = None
        self._filterProxy = _ColumnFilterProxyModel(self)
        self._filterDebounceTimer = QTimer(self)
        self._filterDebounceTimer.setSingleShot(True)
        self._filterDebounceTimer.timeout.connect(self._commit_pending_filters)
        self._pendingFilters = {}  # {col: (text, is_numeric)}、デバウンス中の未反映分
        self._activeFilters = {}  # {col: (text, is_numeric)}、確定済みの絞り込み条件
        self._load_saved_state()

        # セル値ツールチップ用のフローティングラベル（QToolTipはマウス位置に応じて
        # 自動で消えてしまうため使わない。表示位置とマウス位置が無関係なこの用途では
        # 自前のラベルで表示時間を完全にコントロールする）。
        self._cellTipLabel = QLabel(None, Qt.WindowType.ToolTip)
        self._cellTipLabel.setAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating
        )
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
        self.chkDisableZoom = QCheckBox(self.tr('Disable zoom on selection'))
        self.chkDisableZoom.setChecked(self._saved_disable_zoom)
        self.chkDisableZoom.toggled.connect(self._save_state)
        top.addWidget(self.chkDisableZoom)
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
        self.filterTbl.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.filterTbl.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.filterTbl.itemChanged.connect(self._on_filter_cell_changed)
        self.filterTbl.itemSelectionChanged.connect(
            self._on_filter_selection_changed
        )
        # tbl側は行数に応じて縦スクロールバーの有無が変わり、その分だけ列の描画
        # 幅が狭くなる。filterTblは縦スクロールバーを持たないため、そのままだと
        # tbl側にスクロールバーが出た瞬間に列がずれる。右側にスペーサーを置いて
        # 幅を動的に合わせる（QTableWidget自体のviewportMarginsは横/縦ヘッダーの
        # 位置決めに内部で使われており、外から触ると壊れるため使わない）。
        self._filterTblSpacer = QWidget()
        # プレーンなQWidgetはこの属性がないとスタイルシートのbackground-color/
        # borderを描画しない（QFrame系は既定で描画するが素のQWidgetは対象外）。
        self._filterTblSpacer.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        self._filterTblSpacer.setFixedWidth(0)
        filterRow = QHBoxLayout()
        filterRow.setContentsMargins(0, 0, 0, 0)
        filterRow.setSpacing(0)
        filterRow.addWidget(self.filterTbl)
        filterRow.addWidget(self._filterTblSpacer)
        rightLayout.addLayout(filterRow)

        # データテーブルはモデル/ビュー方式（QTableView）。大規模レイヤーでも
        # 表示に必要な行だけ遅延取得されるQgsVectorLayerCache+QgsAttributeTableModel
        # を使う（QGIS標準の属性テーブルと同じ仕組み）。行番号・スクロールバーは
        # 標準のまま。列名の見出しは上のfilterTbl側に表示するため、こちらの横
        # ヘッダーは非表示。
        self.tbl = QTableView()
        self.tbl.setModel(self._filterProxy)
        self.tbl.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        # ハイライトはクリックしたセル単体のみ表示する（選択対象の特定は内部的には
        # 行単位で扱う。FeatureIdRoleでどの列のインデックスからでもfidが取れる）。
        self.tbl.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems
        )
        self.tbl.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.tbl.horizontalHeader().hide()
        tbl_palette = self.tbl.palette()
        tbl_palette.setColor(
            QPalette.ColorRole.AlternateBase, QColor('#e9ecf1')
        )
        self.tbl.setPalette(tbl_palette)
        self.tbl.setAlternatingRowColors(True)
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
        self.lblLayerType = QLabel('')
        bottomRow.addWidget(self.lblLayerType)
        bottomRow.addSpacing(12)
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
        self.lblLegendJoin = QLabel('')
        self.lblLegendJoin.setStyleSheet(
            f'color: {_JOIN_COLOR.name()}; font-size: 11px;'
        )
        self.lblLegendJoin.setVisible(False)
        bottomRow.addWidget(self.lblLegendJoin)
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
        self.tbl.selectionModel().selectionChanged.connect(
            self._on_table_selection_changed
        )

        QgsProject.instance().layersAdded.connect(self._refresh_layer_combo)
        QgsProject.instance().layersRemoved.connect(self._refresh_layer_combo)

        self._refresh_layer_combo()
        self._update_language_button()
        # Info/Settingタブ構築後でないとコンテナが無いため、ここで初期状態を反映する。
        self.btnShowAllFields.setChecked(self._saved_show_all_fields)

    # -- filterTbl/tblを1つの表として矢印キーで行き来する -------------------

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            if obj is self.filterTbl and event.key() == Qt.Key.Key_Down:
                self._focus_table_first_row()
                return True
            if obj is self.filterTbl and event.key() == Qt.Key.Key_Delete:
                self._clear_selected_filter_cells()
                return True
            if obj is self.tbl and event.key() == Qt.Key.Key_Up:
                if self.tbl.currentIndex().row() == self._first_visible_row():
                    self._focus_filter_row()
                    return True
        return super().eventFilter(obj, event)

    def _clear_selected_filter_cells(self):
        items = self.filterTbl.selectedItems()
        if not items:
            current = self.filterTbl.currentItem()
            if current is not None:
                items = [current]
        for item in items:
            item.setText('')

    def _first_visible_row(self):
        # フィルタ済みの非表示行はそもそもプロキシの行空間に存在しないため、
        # 単にプロキシに1行でもあれば先頭(0)が最初の可視行になる。
        return 0 if self._filterProxy.rowCount() > 0 else -1

    def _focus_table_first_row(self):
        row = self._first_visible_row()
        if row < 0:
            return
        visual_col = max(self.filterTbl.currentColumn(), 0)
        model_col = (
            self._modelColumnOrder[visual_col]
            if visual_col < len(self._modelColumnOrder)
            else 0
        )
        self.filterTbl.clearSelection()
        # QAbstractItemView.setCurrentIndex()だけだと、選択も同時に行うかは
        # スタイル依存で不確定（見た目が「塗りつぶし」だったり無かったりする
        # 原因）。QItemSelectionModel側でSelectionFlag.Selectを明示することで、
        # _sync_table_selection_from_layerと同じ確実な塗りつぶしにする。
        self.tbl.selectionModel().setCurrentIndex(
            self._filterProxy.index(row, model_col),
            QItemSelectionModel.SelectionFlag.Select,
        )
        self.tbl.setFocus()

    def _focus_filter_row(self):
        model_col = max(self.tbl.currentIndex().column(), 0)
        visual_col = (
            self._modelColumnOrder.index(model_col)
            if model_col in self._modelColumnOrder
            else 0
        )
        # tbl側の選択解除が地図側の選択同期（層への selectByIds）を
        # 巻き込まないよう、同期処理を一時停止する。
        self._syncing_selection = True
        try:
            self.tbl.clearSelection()
        finally:
            self._syncing_selection = False
        self.filterTbl.setCurrentCell(0, visual_col)
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
        self.infoFidNameLabel.setText(self.tr('QGIS internal ID:'))
        self.chkDisableZoom.setText(self.tr('Disable zoom on selection'))
        self.chkWindowMode.setText(self.tr('Separate window'))
        self._leftTabs.setTabText(0, self.tr('Info'))
        self._leftTabs.setTabText(1, self.tr('Setting'))
        self._leftTabs.setTabText(2, self.tr('Tips'))
        if self.cmbLayer.count() > 0:
            self.cmbLayer.setItemText(0, self.tr('-- Select layer --'))
        self.lblLegendDisplay.setText(self.tr('■ Display'))
        self.lblLegendInfo.setText(self.tr('■ Info (trailing)'))
        self._update_join_legend(
            self._currentLayer, self.lblLegendJoin.isVisible()
        )
        self._update_layer_type_label(self._currentLayer)
        self.btnEditMode.setText(self.tr('Edit'))
        self.btnShowAllFields.setText(self.tr('Show all fields'))
        self.lblAllInfoMode.setText(
            self.tr('All info mode ON')
            if self._showAllFields
            else self.tr('All info mode OFF')
        )
        for group, title_key, labels, clear_btn in self._settingGroups:
            group.setTitle(self.tr(title_key))
            for i, label in enumerate(labels):
                label.setText(self.tr('Field {0}:').format(i + 1))
            if clear_btn is not None:
                clear_btn.setText(self.tr('Clear'))
        self.grpTableSetting.setTitle(self.tr('Table setting'))
        self.btnColumnConfig.setText(self.tr('Column config'))
        self.tipsGroup.setTitle(self.tr('Tips'))
        self.tipsRuleLabel.setText(self.tr(
            'Text columns: substring match, case-insensitive.\n'
            'Numeric columns: comma-separated comparisons '
            '(>, >=, <, <=, ==, !=). No operator = exact match.'
        ))
        tips_explanations = [
            self.tr('100 or more'),
            self.tr('Less than 200'),
            self.tr('100 or more, less than 200'),
            self.tr('Not equal to 0'),
            self.tr('Exactly 100'),
        ]
        for label, text in zip(self.tipsExplLabels, tips_explanations):
            label.setText(text)
        for btn in self.tipsCopyButtons:
            btn.setText(self.tr('Copy'))

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
        self._saved_disable_zoom = settings.value(
            'compartment_compass/disable_zoom', True, type=bool
        )
        self._saved_show_all_fields = settings.value(
            'compartment_compass/show_all_fields', True, type=bool
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
        settings.setValue(
            'compartment_compass/disable_zoom', self.chkDisableZoom.isChecked()
        )
        settings.setValue(
            'compartment_compass/show_all_fields',
            self.btnShowAllFields.isChecked(),
        )

    def cleanup(self):
        self._cellTipTimer.stop()
        self._cellTipLabel.hide()
        self._disconnect_selection_signal(self._currentLayer)
        self._restore_selection_color()
        try:
            QgsProject.instance().layersAdded.disconnect(
                self._refresh_layer_combo
            )
            QgsProject.instance().layersRemoved.disconnect(
                self._refresh_layer_combo
            )
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
            # Excel等を読み込んだ非空間テーブル（ジオメトリ無し）は地図との連動が
            # 成立しないため候補から除外する。
            if layer.geometryType() == Qgis.GeometryType.Null:
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
        self._update_layer_type_label(layer)

    def _update_layer_type_label(self, layer):
        self._currentLayerTypeStr = (
            layer.dataProvider().storageType() if layer is not None else ''
        )
        if self._currentLayerTypeStr:
            self.lblLayerType.setText(
                self.tr('Layer type: {0}').format(self._currentLayerTypeStr)
            )
        else:
            self.lblLayerType.setText('')

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

    def _update_join_legend(self, layer, has_join):
        if not has_join:
            self.lblLegendJoin.setVisible(False)
            return
        if self._is_gpkg_layer(layer):
            self.lblLegendJoin.setText(self.tr('■ Joined data (not editable)'))
        else:
            # GPKG以外はテーブル全体が編集不可のため、結合列だけの注記は不要。
            self.lblLegendJoin.setText(self.tr('■ Joined data'))
        self.lblLegendJoin.setVisible(True)

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
            QTableView.EditTrigger.DoubleClicked
            | QTableView.EditTrigger.EditKeyPressed
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
        self.tbl.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._set_edit_mode_checked(False)
        return True

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
        fids = set()
        for index in self.tbl.selectionModel().selectedIndexes():
            fid = index.data(QgsAttributeTableModel.FeatureIdRole)
            if fid is not None:
                fids.add(fid)
        return list(fids)

    def _on_filter_selection_changed(self):
        if self._syncing_selection:
            return
        # 検索行(filterTbl)をクリックした場合も、逆方向として下テーブル側の
        # ハイライトを解除する。clearSelection()だけだとcurrentIndexが残り、
        # 非アクティブ状態の薄い塗りとして見えてしまう＋Infoタブが古い地物の
        # ままになるため、currentIndexも明示的に解除する。
        self._syncing_selection = True
        try:
            self.tbl.clearSelection()
            self.tbl.selectionModel().clearCurrentIndex()
        finally:
            self._syncing_selection = False
        self._update_info_tab()

    def _on_table_selection_changed(self, *_args):
        if self._syncing_selection:
            return
        # 下テーブルをクリックした場合も、矢印キーでの移動と同様に検索行側の
        # ハイライトを解除する（別々のテーブルに見えて、実際は1つの表として
        # 扱いたいため）。clearSelection()だけだとcurrentIndexが残るため、
        # 逆方向と同様に明示的に解除する。
        self._syncing_selection = True
        try:
            self.filterTbl.clearSelection()
            self.filterTbl.selectionModel().clearCurrentIndex()
        finally:
            self._syncing_selection = False
        layer = self._currentLayer
        if layer is None:
            return
        self._syncing_selection = True
        try:
            layer.selectByIds(self._get_selected_fids())
        finally:
            self._syncing_selection = False
        self._refresh_layer_selection(layer)
        self._zoom_to_selection(
            layer, pan_only=self.chkDisableZoom.isChecked()
        )
        self._show_cell_tooltip(layer)
        self._update_info_tab()

    def _show_cell_tooltip(self, layer):
        """テーブルで選択中のセルの値を、対応する地物の位置にツールチップ表示する。"""
        index = self.tbl.currentIndex()
        if not index.isValid():
            return
        fid = index.data(QgsAttributeTableModel.FeatureIdRole)
        if fid is None:
            return

        feature = (
            self._layerCache.getFeature(fid) if self._layerCache else None
        )
        if (
            feature is None
            or not feature.isValid()
            or feature.geometry().isNull()
        ):
            return
        point = feature.geometry().centroid().asPoint()

        canvas = self.iface.mapCanvas()
        layer_crs = layer.crs()
        canvas_crs = canvas.mapSettings().destinationCrs()
        if (
            layer_crs.isValid()
            and canvas_crs.isValid()
            and layer_crs != canvas_crs
        ):
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
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')

        def _show():
            self._cellTipLabel.setText(text)
            self._cellTipLabel.adjustSize()
            self._cellTipLabel.move(global_pos)
            self._cellTipLabel.show()
            self._cellTipTimer.start(_TOOLTIP_DURATION_MS)

        QTimer.singleShot(0, _show)

    def _zoom_to_selection(self, layer, pan_only=False):
        """選択フィーチャーへパン/ズームする。余白は上下/左右それぞれフィーチャー
        サイズの50%とし、大きい方の余白を四辺すべてに適用する（縦横比を保つため）。
        pan_only=Trueの場合は拡大縮小を行わず、現在の縮尺のまま中心だけ合わせる。
        """
        if not layer.selectedFeatureIds():
            return
        bbox = layer.boundingBoxOfSelected()
        if bbox.isNull():
            return

        canvas = self.iface.mapCanvas()
        layer_crs = layer.crs()
        canvas_crs = canvas.mapSettings().destinationCrs()
        if (
            layer_crs.isValid()
            and canvas_crs.isValid()
            and layer_crs != canvas_crs
        ):
            transform = QgsCoordinateTransform(
                layer_crs, canvas_crs, QgsProject.instance()
            )
            try:
                bbox = transform.transformBoundingBox(bbox)
            except QgsCsException:
                return

        if pan_only:
            canvas.setCenter(bbox.center())
            canvas.refresh()
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
        if layer is None or self._tableModel is None:
            return
        selected_ids = layer.selectedFeatureIds()
        self._syncing_selection = True
        try:
            self.tbl.clearSelection()
            selection_model = self.tbl.selectionModel()
            if selection_model is None:
                return
            # SelectItems設定に合わせ、行全体ではなく列0のセルのみをハイライトする。
            select_flags = QItemSelectionModel.SelectionFlag.Select
            first_proxy_row = None
            for fid in selected_ids:
                source_row = self._tableModel.idToRow(fid)
                if source_row < 0:
                    continue
                proxy_index = self._filterProxy.mapFromSource(
                    self._tableModel.index(source_row, 0)
                )
                if not proxy_index.isValid():
                    continue  # 絞り込みで除外されている
                selection_model.select(proxy_index, select_flags)
                if first_proxy_row is None:
                    first_proxy_row = proxy_index.row()
            if first_proxy_row is not None:
                scroll_index = self._filterProxy.index(first_proxy_row, 0)
                self.tbl.scrollTo(
                    scroll_index, QAbstractItemView.ScrollHint.PositionAtCenter
                )
                # NoUpdateで選択状態を変えずにcurrentIndexだけ合わせる（Infoタブ用）。
                selection_model.setCurrentIndex(
                    scroll_index, QItemSelectionModel.SelectionFlag.NoUpdate
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
        field_names = (
            [field.name() for field in layer.fields()] if layer else []
        )
        self._column_config = {
            name: self._column_config.get(
                name, self._default_column_state(name)
            )
            for name in field_names
        }

    def _default_column_state(self, name):
        if name in self._saved_hidden_fields:
            return COLUMN_HIDDEN
        if name in self._saved_info_columns:
            return COLUMN_INFO
        return COLUMN_DISPLAY

    def _sync_data_column_width(self, logical_index, _old_size, new_size):
        if logical_index < len(self._modelColumnOrder):
            self.tbl.setColumnWidth(
                self._modelColumnOrder[logical_index], new_size
            )

    def _sync_row_header_width(self):
        # tbl側の行番号は表示件数の桁数に応じて自動で幅が変わる（絞り込みで
        # 件数が減ると狭くなる）。filterTbl側の検索マークは固定幅のままなので、
        # 絞り込みのたびに再計算しないと上下でガター幅がずれる。どちらか広い方に
        # 両方を揃えることで、以後tbl側が縮んでもズレない。
        width = max(
            self.tbl.verticalHeader().sizeHint().width(),
            self.filterTbl.verticalHeader().sizeHint().width(),
        )
        self.filterTbl.verticalHeader().setFixedWidth(width)
        self.tbl.verticalHeader().setFixedWidth(width)

    def _sync_filter_viewport_margin(self, *_args):
        self._sync_row_header_width()
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
            self.filterTbl.clear()
            self.filterTbl.setRowCount(1)
            self._filterIsNumeric = []
            self._filterProxy.set_matching_fids(None)
            self._pendingFilters = {}
            self._activeFilters = {}
            self._filterDebounceTimer.stop()
            self._layerCache = None
            self._tableModel = None
            if layer is None:
                self._filterProxy.setSourceModel(None)
                self.filterTbl.setColumnCount(0)
                self.lblCount.setText('')
                self._displayedFieldNames = []
                self._modelColumnOrder = []
                self._modelColToFieldIndex = {}
                self.lblLegendJoin.setVisible(False)
                return

            # 表示順: 表示 → 情報（後列） → 非表示（非表示列は末尾でよい、どうせ隠れる）
            _order = {COLUMN_DISPLAY: 0, COLUMN_INFO: 1, COLUMN_HIDDEN: 2}
            ordered_fields = sorted(
                layer.fields(),
                key=lambda f: _order.get(self._column_config.get(f.name()), 0),
            )
            headers = [field.name() for field in ordered_fields]
            self._displayedFieldNames = headers

            # 大規模レイヤーでも表示に必要な行だけ遅延取得される
            # QgsVectorLayerCache+QgsAttributeTableModel（QGIS標準の属性テーブルと
            # 同じ仕組み）を使う。全フィーチャーの即時ウィジェット化はしない。
            self._layerCache = QgsVectorLayerCache(layer, _LAYER_CACHE_SIZE)
            self._tableModel = QgsAttributeTableModel(self._layerCache)
            self._tableModel.loadLayer()
            self._filterProxy.setSourceModel(self._tableModel)

            self._modelColumnOrder = [
                self._tableModel.fieldCol(layer.fields().indexFromName(name))
                for name in headers
            ]
            self._modelColToFieldIndex = {
                self._tableModel.fieldCol(i): i
                for i in range(len(layer.fields()))
            }

            # 結合(join)由来の列は、GPKGレイヤーに結合されていても書き込み先は
            # 結合元（CSV/Excel等）であり、GPKG本体とは別物のため編集不可にする。
            layer_fields = layer.fields()
            joined_names = {
                name for name in headers
                if layer_fields.fieldOrigin(layer_fields.indexFromName(name))
                == Qgis.FieldOrigin.Join
            }
            self._filterProxy.set_readonly_columns(
                self._modelColumnOrder[i]
                for i, name in enumerate(headers)
                if name in joined_names
            )
            self._update_join_legend(layer, bool(joined_names))

            self.filterTbl.setColumnCount(len(headers))
            self.filterTbl.setHorizontalHeaderLabels(headers)
            for col, name in enumerate(headers):
                if name in joined_names:
                    self.filterTbl.horizontalHeaderItem(col).setForeground(
                        _JOIN_COLOR
                    )
                elif self._column_config.get(name) == COLUMN_INFO:
                    self.filterTbl.horizontalHeaderItem(col).setForeground(
                        _INFO_COLOR
                    )

            # 絞り込みセルは標準の編集可能セル（QLineEditの埋め込みではない）
            for col, field in enumerate(ordered_fields):
                item = QTableWidgetItem('')
                self.filterTbl.setItem(0, col, item)
                self._filterIsNumeric.append(field.isNumeric())

            # tbl側の見た目の列順・表示/非表示をfilterTblの表示順(ordered_fields)に
            # 合わせる。モデル自体の列順はlayer.fields()固定なので、ヘッダーの
            # 視覚的な並べ替え(moveSection)で実現する。
            header = self.tbl.horizontalHeader()
            for visual_pos, model_col in enumerate(self._modelColumnOrder):
                current_visual = header.visualIndex(model_col)
                if current_visual != visual_pos:
                    header.moveSection(current_visual, visual_pos)
            for visual_pos, name in enumerate(headers):
                model_col = self._modelColumnOrder[visual_pos]
                hidden = self._column_config.get(name) == COLUMN_HIDDEN
                self.tbl.setColumnHidden(model_col, hidden)
                self.filterTbl.setColumnHidden(visual_pos, hidden)

            # 列幅をtbl側にも反映する（filterTbl側のヘッダーを基準とする）
            for visual_pos, model_col in enumerate(self._modelColumnOrder):
                self.tbl.setColumnWidth(
                    model_col, self.filterTbl.columnWidth(visual_pos)
                )

            # 縦見出しはtbl側の行番号ガター幅に合わせて確保する。
            # ラベルは行番号の代わりに絞り込み行であることを示すマークを表示する。
            mark_item = QTableWidgetItem('\U0001F50D')  # 🔍
            mark_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.filterTbl.setVerticalHeaderItem(0, mark_item)
            self._sync_row_header_width()

            self._fit_filter_table_height()
            self._sync_filter_viewport_margin()
            self.lblCount.setText(
                self.tr('{0} features').format(self._filterProxy.rowCount())
            )
        finally:
            self._syncing_selection = previous_syncing
            self._populatingTable = False

    def _on_filter_cell_changed(self, item):
        if self._populatingTable:
            return
        visual_col = item.column()
        if visual_col >= len(self._modelColumnOrder):
            return
        self._pendingFilters[self._modelColumnOrder[visual_col]] = (
            item.text(),
            self._filterIsNumeric[visual_col],
        )
        self._filterDebounceTimer.start(_FILTER_DEBOUNCE_MS)

    def _commit_pending_filters(self):
        for model_col, (text, is_numeric) in self._pendingFilters.items():
            text = text.strip()
            if text:
                self._activeFilters[model_col] = (text, is_numeric)
            else:
                self._activeFilters.pop(model_col, None)
        self._pendingFilters = {}
        self._recompute_filter_matches()
        self.lblCount.setText(
            self.tr('{0} features').format(self._filterProxy.rowCount())
        )

    def _recompute_filter_matches(self):
        # filterAcceptsRow内でindex.data()を行数分×列数分呼ぶと、大規模レイヤーで
        # キャッシュ/プロバイダ往復が積み重なり致命的に遅くなる（14万件で約29秒）。
        # ここで一括スキャンして一致fidの集合を先に作り、プロキシ側はその集合との
        # 所属確認（O(1)）だけにする。
        layer = self._currentLayer
        if layer is None or not self._activeFilters:
            self._filterProxy.set_matching_fids(None)
            return

        checks = []
        needed_indexes = []
        for model_col, (text, is_numeric) in self._activeFilters.items():
            field_index = self._modelColToFieldIndex.get(model_col)
            if field_index is None:
                continue
            checks.append((field_index, text, is_numeric))
            needed_indexes.append(field_index)

        request = QgsFeatureRequest()
        request.setSubsetOfAttributes(needed_indexes)
        request.setFlags(QgsFeatureRequest.Flag.NoGeometry)

        matched = set()
        for feature in layer.getFeatures(request):
            ok = True
            for field_index, text, is_numeric in checks:
                value = feature.attribute(field_index)
                cell_text = '' if value is None else str(value)
                matched_this = (
                    _numeric_match(cell_text, text)
                    if is_numeric
                    else _text_match(cell_text, text)
                )
                if not matched_this:
                    ok = False
                    break
            if ok:
                matched.add(feature.id())
        self._filterProxy.set_matching_fids(matched)

    # -- Settingタブ ---------------------------------------------------

    def _build_setting_tab(self):
        layout = QVBoxLayout(self.tabSetting)

        self.infoFieldCombos = self._build_field_group(
            layout, 'Info fields', INFO_FIELD_COUNT,
            with_clear=True, with_show_all=True,
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
        # クリックでbtnShowAllFieldsのトグルと同じ動作をするショートカット。
        # ラベルではなくフラットなQPushButtonにして、見た目はテキストのまま
        # クリック可能にする。
        self.lblAllInfoMode = QPushButton('')
        self.lblAllInfoMode.setFlat(True)
        self.lblAllInfoMode.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lblAllInfoMode.setStyleSheet(
            'QPushButton { color: #5c6bc0; font-weight: bold; '
            'font-size: 11px; border: none; background: transparent; '
            'padding: 0; text-decoration: underline; }'
        )
        self.lblAllInfoMode.clicked.connect(
            lambda: self.btnShowAllFields.setChecked(
                not self.btnShowAllFields.isChecked()
            )
        )
        fidRow.addWidget(self.lblAllInfoMode)
        groupLayout.addLayout(fidRow)

        # 設定済みInfo fields（固定10行）。「全フィールドを表示」ON時は隠す。
        self.infoConfiguredContainer = QWidget()
        configuredLayout = QVBoxLayout(self.infoConfiguredContainer)
        configuredLayout.setContentsMargins(0, 0, 0, 0)
        self.infoValueLabels = []
        for _ in range(INFO_FIELD_COUNT):
            row = QHBoxLayout()
            nameLabel = QLabel('')
            nameLabel.setStyleSheet('font-weight: bold;')
            valueLabel = QLabel('')
            valueLabel.setWordWrap(True)
            row.addWidget(nameLabel)
            row.addWidget(valueLabel, 1)
            configuredLayout.addLayout(row)
            self.infoValueLabels.append((nameLabel, valueLabel))
        groupLayout.addWidget(self.infoConfiguredContainer)

        # 「全フィールドを表示」ON時のみ使う、フィーチャーの全属性を動的に並べる
        # コンテナ。レイヤーごとにフィールド数が違うため、切替のたびに作り直す。
        self.infoAllFieldsContainer = QWidget()
        self.infoAllFieldsLayout = QVBoxLayout(self.infoAllFieldsContainer)
        self.infoAllFieldsLayout.setContentsMargins(0, 0, 0, 0)
        self.infoAllFieldsContainer.setVisible(False)
        groupLayout.addWidget(self.infoAllFieldsContainer)

        groupLayout.addStretch(1)

        # 「全フィールドを表示」で列数の多いレイヤーを選ぶとInfoグループが縦に
        # 長くなるため、スクロール領域に収める。
        scrollArea = QScrollArea()
        scrollArea.setWidgetResizable(True)
        scrollArea.setFrameShape(QFrame.Shape.NoFrame)
        scrollArea.setWidget(self.infoGroup)
        layout.addWidget(scrollArea)

    def _update_info_tab(self, *_args):
        index = self.tbl.currentIndex()
        fid = (
            index.data(QgsAttributeTableModel.FeatureIdRole)
            if index.isValid()
            else None
        )

        feature = None
        if fid is not None and self._layerCache is not None:
            feature = self._layerCache.getFeature(fid)
            if feature is not None and not feature.isValid():
                feature = None

        self.infoFidValueLabel.setText(
            '' if feature is None else str(feature.id())
        )

        if self._showAllFields:
            self._populate_all_fields(feature)
            return

        field_names = (
            [f.name() for f in feature.fields()] if feature is not None else []
        )
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

    def _on_show_all_fields_toggled(self, checked):
        self._showAllFields = checked
        self.infoConfiguredContainer.setVisible(not checked)
        self.infoAllFieldsContainer.setVisible(checked)
        self.lblAllInfoMode.setText(
            self.tr('All info mode ON')
            if checked
            else self.tr('All info mode OFF')
        )
        self._update_info_tab()
        self._save_state()

    def _populate_all_fields(self, feature):
        # レイヤーごとにフィールド数が違うため、切替・地物変更のたびに作り直す。
        # 各行はQHBoxLayoutとして追加しているため、takeAt()が返すitemは
        # widget()ではなくlayout()側に入っている。ここを見落とすと中の
        # QLabelが親ウィジェットの子として残り続け、次の描画と重なって見える。
        while self.infoAllFieldsLayout.count():
            item = self.infoAllFieldsLayout.takeAt(0)
            row_layout = item.layout()
            if row_layout is not None:
                while row_layout.count():
                    sub_item = row_layout.takeAt(0)
                    sub_widget = sub_item.widget()
                    if sub_widget is not None:
                        sub_widget.deleteLater()
                row_layout.deleteLater()
            else:
                child = item.widget()
                if child is not None:
                    child.deleteLater()

        if feature is None:
            return
        for field in feature.fields():
            row = QHBoxLayout()
            nameLabel = QLabel(f'{field.name()}:')
            nameLabel.setStyleSheet('font-weight: bold;')
            value = feature[field.name()]
            valueLabel = QLabel('' if value is None else str(value))
            valueLabel.setWordWrap(True)
            row.addWidget(nameLabel)
            row.addWidget(valueLabel, 1)
            self.infoAllFieldsLayout.addLayout(row)

    def _build_tips_tab(self):
        layout = QVBoxLayout(self.tabTips)
        self._build_tips_group(layout)
        layout.addStretch(1)

    def _build_tips_group(self, parent_layout):
        self.tipsGroup = QGroupBox(self.tr('Tips'))
        tipsLayout = QVBoxLayout(self.tipsGroup)

        self.tipsRuleLabel = QLabel(self.tr(
            'Text columns: substring match, case-insensitive.\n'
            'Numeric columns: comma-separated comparisons '
            '(>, >=, <, <=, ==, !=). No operator = exact match.'
        ))
        self.tipsRuleLabel.setWordWrap(True)
        tipsLayout.addWidget(self.tipsRuleLabel)

        examples = [
            ('>=100', self.tr('100 or more')),
            ('<200', self.tr('Less than 200')),
            ('>=100,<200', self.tr('100 or more, less than 200')),
            ('!=0', self.tr('Not equal to 0')),
            ('100', self.tr('Exactly 100')),
        ]
        grid = QGridLayout()
        self.tipsExplLabels = []
        self.tipsCopyButtons = []
        for row, (pattern, explanation) in enumerate(examples):
            patternLabel = QLabel(pattern)
            patternLabel.setStyleSheet('font-family: monospace;')
            grid.addWidget(patternLabel, row, 0)
            btnCopy = QPushButton(self.tr('Copy'))
            btnCopy.clicked.connect(
                lambda _checked, text=pattern: self._copy_to_clipboard(text)
            )
            grid.addWidget(btnCopy, row, 1)
            self.tipsCopyButtons.append(btnCopy)
            explLabel = QLabel(explanation)
            grid.addWidget(explLabel, row, 2)
            self.tipsExplLabels.append(explLabel)
        grid.setColumnStretch(2, 1)
        tipsLayout.addLayout(grid)

        parent_layout.addWidget(self.tipsGroup)

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

    def _build_field_group(
        self, parent_layout, title_key, count,
        with_clear=False, with_show_all=False,
    ):
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
        if with_clear or with_show_all:
            clear_row = (count - 1) // 2 + 1
            if with_show_all:
                self.btnShowAllFields = QPushButton(self.tr('Show all fields'))
                self.btnShowAllFields.setCheckable(True)
                self.btnShowAllFields.toggled.connect(
                    self._on_show_all_fields_toggled
                )
                grid.addWidget(self.btnShowAllFields, clear_row, 1)
            if with_clear:
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
