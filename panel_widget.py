# -*- coding: utf-8 -*-
import html
import re

from qgis.PyQt.QtCore import (
    QElapsedTimer,
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QPoint,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)
from qgis.PyQt.QtGui import QColor, QKeySequence, QPalette
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
    QProgressBar,
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
    QgsExpression,
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
# 編集済みだが未保存（コミット前）のセルの文字色。保存確定後は標準の文字色
# （このロールを返さずsuperにフォールバック）に戻る。
_UNSAVED_EDIT_COLOR = QColor('#c0392b')

# 選択フィーチャーへズームする際の余白（フィーチャーの大きい方の辺に対する比率）
_ZOOM_MARGIN_RATIO = 1.0

# セル値ツールチップの表示時間（ms）。クリック/矢印キーどちらでも同じ長さにする。
_TOOLTIP_DURATION_MS = 3000

# 選択シンボロジ: 色はデフォルトのまま、塗りのみ15%に変更
_SELECTION_ALPHA = 0.25


def _spaced_multiline(text, line_height='145%', indent_after=(), break_after=()):
    """QLabel用: 行間を広げたリッチテキストにする。
    indent_afterに渡した文言の直後で改行し、続く説明文を字下げする。
    break_afterに渡した文字（例:「。」「.」）の直後でも改行・字下げする
    （行末に来た場合は改行しない）。
    """
    if isinstance(break_after, str):
        break_after = (break_after,)
    # 比較演算子(>, >=, <, <=など)を含む原文をそのままリッチテキストに渡すと
    # "<"が不正なタグ開始として解釈され、以降の内容が表示から欠落する。
    text = html.escape(text)
    indent = '&nbsp;&nbsp;&nbsp;&nbsp;'
    out_lines = []
    for line in text.split('\n'):
        for prefix in indent_after:
            if line.startswith(prefix):
                line = prefix + '<br>' + indent + line[len(prefix):].lstrip()
                break
        for ch in break_after:
            segments = line.split(ch)
            if segments[-1] == '':
                line = (ch + '<br>' + indent).join(segments[:-1]) + ch
            else:
                line = (ch + '<br>' + indent).join(segments)
        out_lines.append(line)
    return '<p style="line-height:{0};">{1}</p>'.format(
        line_height, '<br>'.join(out_lines)
    )


def _tips_pattern_link_html(pattern):
    """Tips一覧の項目部分HTML: クリックでコピーできるリンク表示。"""
    return (
        '<a href="#" style="text-decoration:none; font-family:monospace; '
        'color:#3d5afe;">{0}</a>'
    ).format(html.escape(pattern))


_TIPS_INDENT_AFTER = ('Text columns:', 'テキスト列:', 'Numeric columns:', '数値列:')


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
_DEFER_TABLE_LOAD_ON_LAYER_SELECT = False
_SUPPRESS_TABLE_LAYOUT_SIGNALS = True

LANGUAGE_CYCLE = ['en', 'ja']
LANGUAGE_LABELS = {'en': 'English', 'ja': '日本語'}

# 数値列の絞り込みトークン: ">300" ">=300" "<600" "<=600" "!=100" "300"（比較演算子省略時は完全一致）
_NUM_TOKEN_RE = re.compile(r'^(>=|<=|!=|==|>|<)?\s*(-?\d+(?:\.\d+)?)$')


def _numeric_and_match(value, tokens):
    for token in tokens:
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


def _numeric_match(cell_text, filter_text):
    # "|"区切りでOR評価。各OR項の中はカンマ区切りでAND評価（従来通り）。
    # 例: ">=100,<200|>=500" は「100以上200未満、または500以上」。
    try:
        value = float(cell_text)
    except (TypeError, ValueError):
        return False
    has_any_token = False
    for group in filter_text.split('|'):
        tokens = [t.strip() for t in group.split(',') if t.strip()]
        if not tokens:
            continue
        has_any_token = True
        if _numeric_and_match(value, tokens):
            return True
    return not has_any_token


def _text_and_match(cell_lower, tokens):
    for token in tokens:
        if token.startswith('!'):
            needle = token[1:].strip().lower()
            if needle and needle in cell_lower:
                return False
        elif token.lower() not in cell_lower:
            return False
    return True


def _text_match(cell_text, filter_text):
    # "|"区切りでOR評価。各OR項の中はカンマ区切りでAND評価（従来通り）。
    # 先頭に"!"を付けたトークンは除外（非包含）条件になる。
    # 例: "杉|桧" は「杉または桧を含む」、"森林,!計画" は
    # 「森林を含み、かつ計画を含まない」。
    cell_lower = cell_text.lower()
    has_any_token = False
    for group in filter_text.split('|'):
        tokens = [t.strip() for t in group.split(',') if t.strip()]
        if not tokens:
            continue
        has_any_token = True
        if _text_and_match(cell_lower, tokens):
            return True
    return not has_any_token


def _looks_like_numeric_comparison_filter(filter_text):
    # テキスト型フィールドでは、数字だけの入力は地名・小班名などの部分一致を優先する。
    # 比較演算子が含まれる場合だけ、数値比較の意図とみなす。
    tokens = []
    for group in filter_text.split('|'):
        tokens.extend(t.strip() for t in group.split(',') if t.strip())
    if not tokens:
        return False
    matched = [_NUM_TOKEN_RE.match(t) for t in tokens]
    return all(matched) and any(m.group(1) for m in matched)


def _cell_matches(cell_text, filter_text, use_numeric):
    if use_numeric:
        return _numeric_match(cell_text, filter_text)
    return _text_match(cell_text, filter_text)


def _value_filter_expression(layer, field_index, filter_text, use_numeric):
    """列の値の一覧（uniqueValues）を判定し、一致した値だけを通す式を返す。

    全件を読んで1件ずつ判定すると、GPKGからの読み出しだけで26万件で約4秒
    かかる（判定自体は1秒未満）。uniqueValuesはプロバイダ側（SQLite等）で
    集計されるため速く、林班・樹種など値の種類が少ない列では判定も数百回で
    済む。値がすべて異なる列（26万種類）でも全件判定より速いことを実機で
    確認済み。判定は全件判定と同じ関数で行うため、絞り込みの書式はそのまま。

    戻り値: 式の文字列。一致する値が無い場合は''。
    式に値を書けない型（日付等）の値がある場合はNone（呼び出し側で全件判定）。
    """
    matched = []
    null_matched = False
    for value in layer.uniqueValues(field_index):
        if value is None:
            # 全件判定と同じく、NULLは空文字として判定する
            null_matched = null_matched or _cell_matches(
                '', filter_text, use_numeric
            )
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        if _cell_matches(str(value), filter_text, use_numeric):
            matched.append(value)
    column = QgsExpression.quotedColumnRef(
        layer.fields().at(field_index).name()
    )
    parts = []
    if matched:
        values = ','.join(QgsExpression.quotedValue(v) for v in matched)
        parts.append(f'{column} IN ({values})')
    if null_matched:
        parts.append(f'{column} IS NULL')
    return ' OR '.join(parts)


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
        # 貼り付けなど複数セルの一括編集中、セルごとのdataChangedを止めて
        # まとめて1回で発行するためのフラグ（大量貼り付け時の描画負荷対策）。
        self._suspendDataChangedSignal = False
        # {(fid, field_index): 編集セッション中に最初に触れた時点の値}。
        # 未保存編集（赤字）の判定に使う。「編集バッファに変更が記録されている
        # か」だけで判定すると、Undoで値が戻っても記録は残ったままだったり、
        # 元と同じ値を書き戻しても「変更あり」扱いになったりするため、実際の
        # 値をここに保持した元の値と比較する方式にしている。
        self._originalValues = {}

    def set_matching_fids(self, fids):
        self._matchingFids = fids
        # invalidateFilter()は行の出入りを連続範囲ごとに通知するため、飛び飛びに
        # 除外されていた行を戻す（絞り込み解除）と26万件で約6秒かかった。
        # invalidate()は対応表を作り直して1回の通知で済む（同条件で約0.2秒）。
        # このビューは並べ替えを使っていないため、並び順のリセットは影響しない。
        self.invalidate()

    def set_readonly_columns(self, cols):
        self._readonlyColumns = set(cols)

    def clear_original_values(self):
        # レイヤー切替時（fid/field_indexの意味が変わる）、保存確定後、破棄後の
        # いずれでも呼ぶ。呼び出し側でtbl.viewport().update()による再描画が必要。
        self._originalValues.clear()

    def set_bulk_edit_mode(self, enabled):
        # 貼り付けなど複数セルをまとめて書き込む間、setData()内のセル単位の
        # dataChanged発行を止める。呼び出し側が一括で1回だけemitすること。
        self._suspendDataChangedSignal = enabled

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

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        # 描画のたびに表示中の全セルで呼ばれるため、未保存編集が無い（通常時）
        # はセルごとの元の値の照合を省く。
        if role == Qt.ItemDataRole.ForegroundRole and self._originalValues:
            source_index = self.mapToSource(index)
            source_model = self.sourceModel()
            if source_model is not None:
                fid = source_index.data(
                    QgsAttributeTableModel.CustomRole.FeatureIdRole
                )
                field_index = source_model.data(
                    source_index,
                    QgsAttributeTableModel.CustomRole.FieldIndexRole,
                )
                key = (fid, field_index)
                if key in self._originalValues:
                    current_value = source_index.data(
                        Qt.ItemDataRole.EditRole
                    )
                    if current_value != self._originalValues[key]:
                        return _UNSAVED_EDIT_COLOR
        return super().data(index, role)

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
        fid = source_index.data(QgsAttributeTableModel.CustomRole.FeatureIdRole)
        field_index = source_model.data(
            source_index, QgsAttributeTableModel.CustomRole.FieldIndexRole
        )
        if layer is None or fid is None or field_index is None:
            return False
        if isinstance(value, str):
            # 貼り付け（クリップボードのテキスト）はどのフィールド型でも文字列
            # として届くため、書き込み前にフィールド型へ変換する。通常のセル
            # 編集（型付きエディタ経由）で渡ってくる値は既に適切な型なので、
            # ここはそのまま通す。
            # 実機確認の結果、convertCompatible()は変換後の値を直接返し、
            # 変換不能な場合は(bool,value)ではなくValueErrorを送出する。
            field = layer.fields().field(field_index)
            try:
                value = field.convertCompatible(value)
            except ValueError:
                return False
        key = (fid, field_index)
        if key not in self._originalValues:
            # このセッションで初めて触れるセルの場合のみ、書き込み前の値
            # （＝まだ未保存編集が無い、その時点の本来の値）を記録する。
            # 2回目以降の編集ではここを通らないため、複数回編集してから
            # 一部だけUndoしても正しく元の値と比較できる。
            self._originalValues[key] = source_index.data(
                Qt.ItemDataRole.EditRole
            )
        ok = layer.changeAttributeValue(fid, field_index, value)
        if ok:
            if not self._suspendDataChangedSignal:
                self.dataChanged.emit(
                    index,
                    index,
                    [
                        role,
                        Qt.ItemDataRole.DisplayRole,
                        Qt.ItemDataRole.ForegroundRole,
                    ],
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
        self._prevActiveFilters = {}  # 直前にrecomputeした時点のactiveFilters
        self._prevMatchedFids = None  # 直前の一致fid集合（条件追加時の母集合再利用用）
        # 検索行の条件だけで求めた一致fid集合（Noneは条件なし）。「選択で絞り込み」
        # の切替時に検索をやり直さず、この集合と組み合わせるために保持する。
        self._columnMatchedFids = None
        # 「選択で絞り込み」を押した時点の選択fid集合（Noneは未使用）
        self._selectionFilterFids = None
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
        self.cmbLayer.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.cmbLayer.setMinimumContentsLength(10)
        self.cmbLayer.setMinimumWidth(220)
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

        lbl_credit = QLabel('Developed by Avid Tree Work')
        lbl_credit.setStyleSheet('color: #AAAACC; font-size: 10px; background: transparent;')
        lbl_credit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        leftLayout.addWidget(lbl_credit)

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
        # キー操作はフォーカスを持つtbl/filterTbl自身に届くためこれで捕まるが、
        # ホイール操作はマウス位置にあるviewport側に届くため、Shift+ホイールの
        # 横スクロール検知にはviewportへも別途フィルタを仕込む必要がある。
        self.filterTbl.installEventFilter(self)
        self.tbl.installEventFilter(self)
        self.filterTbl.viewport().installEventFilter(self)
        self.tbl.viewport().installEventFilter(self)

        bottomRow = QHBoxLayout()
        self.lblLayerType = QLabel('')
        bottomRow.addWidget(self.lblLayerType)
        bottomRow.addSpacing(12)
        self.filterProgressBar = QProgressBar()
        self.filterProgressBar.setFixedHeight(14)
        self.filterProgressBar.setMaximumWidth(120)
        self.filterProgressBar.setTextVisible(False)
        self.filterProgressBar.hide()
        bottomRow.addWidget(self.filterProgressBar)
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
        # 押した時点の選択でテーブルを絞り込む（もう一度押すと解除）。オン/オフの
        # 見た目は持たせず、状態は件数表示の文言で示す。
        self.btnSelectionFilter = QPushButton(self.tr('Filter by selection'))
        self.btnSelectionFilter.setEnabled(False)
        self.btnSelectionFilter.clicked.connect(
            self._on_selection_filter_clicked
        )
        bottomRow.addWidget(self.btnSelectionFilter)
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
        if self._saved_splitter_sizes:
            try:
                splitter_sizes = [int(v) for v in self._saved_splitter_sizes]
            except (TypeError, ValueError):
                splitter_sizes = [380, 820]
        else:
            splitter_sizes = [380, 820]
        self._splitter.setSizes(splitter_sizes)
        self._splitter.splitterMoved.connect(self._save_state)
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
        was_blocked = self.btnShowAllFields.blockSignals(True)
        self.btnShowAllFields.setChecked(self._saved_show_all_fields)
        self.btnShowAllFields.blockSignals(was_blocked)
        self._apply_show_all_fields_state(
            self.btnShowAllFields.isChecked(), save=False
        )

    # -- filterTbl/tblを1つの表として矢印キーで行き来する -------------------

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            # 実際のホイール操作はtbl/filterTbl自身ではなく、その内部の
            # viewport（QAbstractScrollAreaが保持する別ウィジェット）に届く。
            # installEventFilterはイベント送信先のオブジェクトでしか発火しない
            # ため、viewport側にもフィルタを仕込んでおく必要がある。
            if obj in (
                self.tbl.viewport(),
                self.filterTbl.viewport(),
            ) and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self._scroll_table_horizontally(event)
                return True
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
            if obj is self.tbl and event.matches(QKeySequence.StandardKey.Copy):
                self._copy_selected_cells()
                return True
            if obj is self.tbl and event.matches(QKeySequence.StandardKey.Paste):
                self._paste_into_selected_cells()
                return True
            if obj is self.tbl and self._is_typeahead_key(event):
                # Qt標準のQAbstractItemView.keyboardSearch()は、現在の列を
                # 一致する値が見つかるまで（無ければ全行）走査する。列数の多い
                # 大規模レイヤーで、その列に存在しない文字を打つと重い全件検索に
                # なってしまう。編集モードでかつ編集可能な列であれば、代わりに
                # Qt標準のAnyKeyPressedトリガー（_enter_edit_modeで設定）に
                # そのままセル編集を任せる（素通し）。それ以外は検索自体を
                # 起こさせないためイベントを握りつぶす。
                if not (
                    self._editModeActive and self._current_cell_editable()
                ):
                    return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _is_typeahead_key(event):
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            return False
        text = event.text()
        return bool(text) and text.isprintable()

    def _current_cell_editable(self):
        index = self.tbl.currentIndex()
        return index.isValid() and bool(
            self._filterProxy.flags(index) & Qt.ItemFlag.ItemIsEditable
        )

    def _scroll_table_horizontally(self, event):
        # Shift+ホイールで横スクロール（Excelのセル操作と同じ挙動）。tbl側の
        # 横スクロールバーはfilterTbl側と双方向に同期済みなので、どちらの上で
        # ホイール操作してもtbl側を動かせば両方に反映される。回転量は通常の
        # 縦ホイール1ノッチ分のスクロール量（singleStep * wheelScrollLines）を
        # そのまま横方向に流用し、体感速度を通常スクロールと揃える。
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        hbar = self.tbl.horizontalScrollBar()
        notches = delta / 120.0
        step = hbar.singleStep() * QApplication.wheelScrollLines()
        hbar.setValue(round(hbar.value() - notches * step))

    # -- コピー&ペースト（Excel風） -----------------------------------------

    def _copy_selected_cells(self):
        # 選択範囲の外接矩形をタブ区切りテキストとしてクリップボードへ書き出す
        # （Excel等の表計算ソフトとの相互貼り付けも兼ねる）。飛び地選択は
        # 非対応（外接矩形をそのまま書き出すのみ）。
        indexes = self.tbl.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows = [i.row() for i in indexes]
        cols = [i.column() for i in indexes]
        top, bottom = min(rows), max(rows)
        left, right = min(cols), max(cols)
        lines = []
        for row in range(top, bottom + 1):
            cells = [
                str(self._filterProxy.index(row, col).data(
                    Qt.ItemDataRole.DisplayRole
                ) or '')
                for col in range(left, right + 1)
            ]
            lines.append('\t'.join(cells))
        QApplication.clipboard().setText('\n'.join(lines))

    def _paste_into_selected_cells(self):
        if not self._editModeActive:
            return
        layer = self._currentLayer
        if layer is None or not layer.isEditable():
            return
        selected_rc = {
            (index.row(), index.column())
            for index in self.tbl.selectionModel().selectedIndexes()
        }
        if not selected_rc:
            return
        clip_text = QApplication.clipboard().text()
        if not clip_text:
            return
        text_rows = clip_text.replace('\r\n', '\n').replace('\r', '\n').split(
            '\n'
        )
        while text_rows and text_rows[-1] == '':
            text_rows.pop()
        clip_grid = [row.split('\t') for row in text_rows]
        if not clip_grid:
            return

        if len(clip_grid) == 1 and len(clip_grid[0]) == 1:
            # 単一セルをコピーした場合は、選択中の全セルへ同じ値を貼り付ける
            # （塗りつぶし）。
            value_text = clip_grid[0][0]
            targets = [(r, c, value_text) for (r, c) in selected_rc]
        else:
            # 範囲をコピーした場合は、選択範囲の左上を起点にコピー範囲の
            # 形のまま貼り付ける。
            anchor_row = min(r for r, _ in selected_rc)
            anchor_col = min(c for _, c in selected_rc)
            targets = [
                (anchor_row + i, anchor_col + j, value_text)
                for i, row_vals in enumerate(clip_grid)
                for j, value_text in enumerate(row_vals)
            ]

        # 貼り付け全体をUndo1ステップにまとめる（QGIS標準のEdit>Undoで戻せる
        # ようにするため）。セル単位のdataChangedは一旦止め、最後に貼り付け
        # 範囲全体で1回だけ発行することで、大量貼り付け時の再描画負荷を抑える。
        layer.beginEditCommand(self.tr('Paste cells'))
        self._filterProxy.set_bulk_edit_mode(True)
        changed_indexes = []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for row, col, value_text in targets:
                index = self._filterProxy.index(row, col)
                if not index.isValid():
                    continue
                if not (
                    self._filterProxy.flags(index)
                    & Qt.ItemFlag.ItemIsEditable
                ):
                    continue
                if self._filterProxy.setData(
                    index, value_text, Qt.ItemDataRole.EditRole
                ):
                    changed_indexes.append(index)
        finally:
            self._filterProxy.set_bulk_edit_mode(False)
            QApplication.restoreOverrideCursor()

        if changed_indexes:
            layer.endEditCommand()
            rows = [i.row() for i in changed_indexes]
            cols = [i.column() for i in changed_indexes]
            top_left = self._filterProxy.index(min(rows), min(cols))
            bottom_right = self._filterProxy.index(max(rows), max(cols))
            self._filterProxy.dataChanged.emit(
                top_left,
                bottom_right,
                [
                    Qt.ItemDataRole.EditRole,
                    Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.ForegroundRole,
                ],
            )
        else:
            layer.destroyEditCommand()

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
        if self._tableModel is None and not self._ensure_table_loaded():
            return
        visual_col = max(self.filterTbl.currentColumn(), 0)
        # 入力中の検索セルは、フォーカスを移すと確定する。デバウンスを待たずに
        # その場で絞り込みを反映してから先頭行を選ぶ。待つと、反映前の結果の
        # 先頭行を選んで地図をズーム（描画開始）した直後に絞り込みが入り、
        # 選んだ行が消えるうえ、描画が立て続けにやり直される（実機でこの操作
        # の直後にQGISがラベル描画中に落ちた）。
        self.tbl.setFocus()
        self._flush_pending_filters()
        row = self._first_visible_row()
        if row < 0:
            self.filterTbl.setFocus()
            return
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
        self._clear_map_selection()
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
        self.btnSelectionFilter.setText(self.tr('Filter by selection'))
        self._update_count_label()
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
                clear_btn.setMinimumWidth(
                    self.btnShowAllFields.sizeHint().width()
                )
        self.grpTableSetting.setTitle(self.tr('Table setting'))
        self.btnColumnConfig.setText(self.tr('Column config'))
        self.tipsGroup.setTitle(self.tr('Tips'))
        self.tipsRuleLabel.setText(_spaced_multiline(self.tr(
            'Text columns: substring match, case-insensitive. '
            'Comma-separated = AND; "|"-separated = OR; '
            'prefix "!" to exclude.\n'
            'Numeric columns: comma-separated comparisons '
            '(>, >=, <, <=, ==, !=), "|"-separated = OR. '
            'No operator = exact match.'
        ), indent_after=_TIPS_INDENT_AFTER, break_after=('。', '.')))
        tips_explanations = [
            self.tr('100 or more'),
            self.tr('Less than 200'),
            self.tr('100 or more, less than 200'),
            self.tr('Not equal to 0'),
            self.tr('Exactly 100'),
            self.tr('Excludes "abc"'),
            self.tr('Contains "abc", excludes "xyz"'),
            self.tr('Contains "abc" or "xyz"'),
            self.tr('Less than 100, or 500 or more'),
        ]
        for label, explanation in zip(self.tipsExplLabels, tips_explanations):
            label.setText('：' + explanation)

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
        self._saved_splitter_sizes = settings.value(
            'compartment_compass/splitter_sizes', None
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
        settings.setValue(
            'compartment_compass/splitter_sizes', self._splitter.sizes()
        )

    def cleanup(self):
        self._cellTipTimer.stop()
        self._cellTipLabel.hide()
        self._release_table_model()
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

    def _release_table_model(self):
        # The proxy/view can keep C++ pointers to the source model. Detach it
        # before dropping Python references, otherwise rebuilding the table can
        # leave Qt touching a deleted QgsAttributeTableModel.
        self._filterProxy.setSourceModel(None)
        self._filterProxy.clear_original_values()
        self._tableModel = None
        self._layerCache = None

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
        # レイヤー名・タイプ表示とコンボの選択反映を、続く重い読み込み処理の前に
        # 画面へ描画させる（processEventsで一度イベントループに戻す）。これが
        # 無いとコンボ選択のハイライトすら読み込み完了まで反映されず、操作が
        # 効いていないように見える。
        self.btnEditMode.setEnabled(self._is_gpkg_layer(layer))
        self._update_layer_type_label(layer)
        QApplication.processEvents()
        self._reset_column_config(layer)
        if _DEFER_TABLE_LOAD_ON_LAYER_SELECT and layer is not None:
            self._reset_filter_state()
            self._release_table_model()
            self._apply_table_column_layout(layer, reset_filters=False)
        else:
            self._populate_table(layer)
        self._refresh_setting_combos(layer)
        if layer is not None:
            self._apply_selection_color(layer)
            self._connect_selection_signal(layer)
            self._sync_table_selection_from_layer()
            # テーブル読み込み(_populate_table)完了後にレイヤーパネル側も同期する。
            # 読み込み中の描画と競合しないよう、ここまで待ってから選択・アクティブ化する。
            self.iface.layerTreeView().setCurrentLayer(layer)
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            if node is not None and not node.itemVisibilityChecked():
                node.setItemVisibilityChecked(True)
        else:
            self._update_info_tab()

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
        # QGIS標準のEdit>Undo/RedoやEdit>Save Layer Editsが、このパネルで
        # 編集中のレイヤーに対して働くようにする（このプラグインは独自の
        # Undo/Redoボタンを持たない）。副作用として、QGISのレイヤーパネル側の
        # アクティブレイヤーもこのレイヤーに切り替わる——編集中のレイヤーが
        # どれか一目で分かる利点として許容している。
        self.iface.setActiveLayer(layer)
        # AnyKeyPressedにより、セルを選択した状態で文字を打つと（Excel同様）
        # その場でセル編集が始まり、打った文字が入力される。eventFilter側で
        # Qt標準のkeyboardSearch（現在列の全件検索）より先にこちらを使う。
        self.tbl.setEditTriggers(
            QTableView.EditTrigger.DoubleClicked
            | QTableView.EditTrigger.EditKeyPressed
            | QTableView.EditTrigger.AnyKeyPressed
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
            # コミット後は今の値が新しい「元の値」になり、破棄後は編集前の
            # 値に戻っている。いずれの場合も赤字判定の基準をリセットして
            # 再描画する。
            self._filterProxy.clear_original_values()
            self.tbl.viewport().update()
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
            fid = index.data(QgsAttributeTableModel.CustomRole.FeatureIdRole)
            if fid is not None:
                fids.add(fid)
        return list(fids)

    def _on_filter_selection_changed(self):
        if self._syncing_selection:
            return
        current_col = self.filterTbl.currentColumn()
        current_name = ''
        if current_col >= 0:
            header_item = self.filterTbl.horizontalHeaderItem(current_col)
            current_name = (
                header_item.text() if header_item is not None else ''
            )
        if self._tableModel is None and self._ensure_table_loaded():
            if current_name in self._displayedFieldNames:
                self._syncing_selection = True
                try:
                    self.filterTbl.setCurrentCell(
                        0, self._displayedFieldNames.index(current_name)
                    )
                finally:
                    self._syncing_selection = False
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
        self._clear_map_selection()
        self._update_info_tab()

    def _clear_map_selection(self):
        # 検索行(filterTbl)は特定の地物に対応しないため、フォーカスが移った
        # 時点で地図側のフィーチャー選択（ハイライト）も解除し、テーブル選択
        # と地図選択の対応関係を崩さないようにする。
        layer = self._currentLayer
        if layer is None or layer.selectedFeatureCount() == 0:
            return
        self._syncing_selection = True
        try:
            layer.removeSelection()
        finally:
            self._syncing_selection = False
        self._refresh_layer_selection(layer)

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
        except RuntimeError:
            # レイヤーのC++実体が既に削除済み（QGIS/プロジェクト終了時の
            # クリーンアップ中にテーブルの選択変更シグナルが遅れて届く場合）。
            return
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
        fid = index.data(QgsAttributeTableModel.CustomRole.FeatureIdRole)
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
        # テーブル操作由来（_syncing_selection中）の選択変更でも、ボタンの
        # 押せる/押せないは更新する。
        self._update_selection_filter_button()
        if self._syncing_selection:
            return
        # ここに来るのは地図側（テーブル操作以外）での選択変更。選択で絞り込み
        # 中なら解除する（テーブル側の行選択・検索行の操作では解除しない）。
        if self._selectionFilterFids is not None:
            self._selectionFilterFids = None
            self._apply_matching_fids(
                self._columnMatchedFids, reduce_selection=False
            )
            self._update_count_label()
            self._update_selection_filter_button()
        # 検索行にいた場合は、その列のままtbl側へ移る（↓キーでの移動と同じ）。
        self._sync_table_selection_from_layer(
            column=self._current_filter_model_column()
        )
        if self.tbl.selectionModel().hasSelection():
            self._release_filter_row()

    def _current_filter_model_column(self):
        if not self.filterTbl.currentIndex().isValid():
            return None
        visual_col = self.filterTbl.currentColumn()
        if 0 <= visual_col < len(self._modelColumnOrder):
            return self._modelColumnOrder[visual_col]
        return None

    def _release_filter_row(self):
        # 地図側で選択した場合も、テーブルの行をクリックした時と同様に検索行の
        # ハイライトを解除する。フォーカスが検索行（編集中のセルを含む）に
        # 残っていればtblへ移す。先にフォーカスを移すことで、編集中の検索
        # セルは通常どおり確定してから閉じる。
        focus = QApplication.focusWidget()
        if focus is not None and (
            focus is self.filterTbl or self.filterTbl.isAncestorOf(focus)
        ):
            self.tbl.setFocus()
        self._syncing_selection = True
        try:
            self.filterTbl.clearSelection()
            self.filterTbl.selectionModel().clearCurrentIndex()
        finally:
            self._syncing_selection = False

    def _sync_table_selection_from_layer(
        self, scroll=True, column=None, refresh_map=True
    ):
        layer = self._currentLayer
        if layer is None or self._tableModel is None:
            return
        selected_ids = layer.selectedFeatureIds()
        self._syncing_selection = True
        try:
            selection_model = self.tbl.selectionModel()
            if selection_model is None:
                return
            # clearSelection()は選択中のセルを1つずつ処理するため、10万件選択で
            # 約0.5秒かかった。reset()は通知なしで一度に捨てる（現在のセルも
            # 消えるため後で戻し、見た目はviewport().update()で描き直す）。
            previous_current = selection_model.currentIndex()
            selection_model.reset()
            # SelectItems設定に合わせ、行全体ではなく1列のセルのみをハイライトする。
            # 列0固定だと、その列がユーザー設定で非表示にされた場合に
            # ハイライトが見えなくなる（列自体は非表示でも選択状態や
            # スクロールは機能するため気付きにくい）。ここでは非表示でない
            # 先頭の表示列（_modelColumnOrderは表示→情報→非表示の順で
            # ソートされている）を使う。columnの指定があればその列にする。
            if column is not None:
                highlight_col = column
            else:
                highlight_col = (
                    self._modelColumnOrder[0] if self._modelColumnOrder else 0
                )
            # 1件ずつselection_model.select()すると、選択の結合処理が毎回走り
            # 5000件で約1.4秒かかった。QItemSelectionにまとめて1回で選択する。
            # さらに連続する行は1つの範囲にまとめる（範囲の数が選択処理の重さ
            # に効くため。10万件の選択で約0.5秒→範囲の数次第で大幅に短縮）。
            proxy_rows = []
            for fid in selected_ids:
                source_row = self._tableModel.idToRow(fid)
                if source_row < 0:
                    continue
                proxy_index = self._filterProxy.mapFromSource(
                    self._tableModel.index(source_row, highlight_col)
                )
                if not proxy_index.isValid():
                    continue  # 絞り込みで除外されている
                proxy_rows.append(proxy_index.row())
            proxy_rows.sort()
            selection = QItemSelection()
            range_start = None
            previous_row = None
            for row in proxy_rows + [None]:
                if (
                    range_start is not None
                    and (row is None or row != previous_row + 1)
                ):
                    selection.select(
                        self._filterProxy.index(range_start, highlight_col),
                        self._filterProxy.index(previous_row, highlight_col),
                    )
                    range_start = None
                if row is not None and range_start is None:
                    range_start = row
                previous_row = row
            if not selection.isEmpty():
                selection_model.select(
                    selection, QItemSelectionModel.SelectionFlag.Select
                )
            # スクロール・現在のセルは選択のうち一番上の行に合わせる。
            first_proxy_row = proxy_rows[0] if proxy_rows else None
            if first_proxy_row is None and previous_current.isValid():
                selection_model.setCurrentIndex(
                    previous_current,
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
            self.tbl.viewport().update()
            if first_proxy_row is not None:
                scroll_index = self._filterProxy.index(
                    first_proxy_row, highlight_col
                )
                if scroll:
                    self.tbl.scrollTo(
                        scroll_index,
                        QAbstractItemView.ScrollHint.PositionAtCenter,
                    )
                # NoUpdateで選択状態を変えずにcurrentIndexだけ合わせる（Infoタブ用）。
                selection_model.setCurrentIndex(
                    scroll_index, QItemSelectionModel.SelectionFlag.NoUpdate
                )
        finally:
            self._syncing_selection = False
        if refresh_map:
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
        if (
            self.tbl.verticalHeader().width() == width
            and self.filterTbl.verticalHeader().width() == width
        ):
            return
        self.filterTbl.verticalHeader().setFixedWidth(width)
        self.tbl.verticalHeader().setFixedWidth(width)
        # ここは縦スクロールバーのrangeChanged（＝ビューがupdateGeometries()で
        # 配置を計算している最中）から呼ばれることが多い。その最中に幅を変えても
        # ビューは再計算を受け付けず（再入防止）、表示部分が古い行番号幅の位置の
        # ままになって列が上下でずれた（実機で11件に絞った時に18pxずれた）。
        # 計算が終わってから配置を計算し直させる。
        QTimer.singleShot(0, self._refresh_table_geometries)

    def _refresh_table_geometries(self):
        self.tbl.updateGeometries()
        self.filterTbl.updateGeometries()

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

    def _reset_filter_state(self):
        self._filterIsNumeric = []
        self._filterProxy.set_matching_fids(None)
        self._pendingFilters = {}
        self._activeFilters = {}
        self._prevActiveFilters = {}
        self._prevMatchedFids = None
        self._columnMatchedFids = None
        # レイヤー切替・列設定の変更など、絞り込みをリセットする場面では
        # 選択で絞り込みも解除する。
        self._selectionFilterFids = None
        self._filterDebounceTimer.stop()

    def _ordered_layer_fields(self, layer):
        # 表示順: 表示 → 情報（後列） → 非表示（非表示列は末尾でよい、どうせ隠れる）
        _order = {COLUMN_DISPLAY: 0, COLUMN_INFO: 1, COLUMN_HIDDEN: 2}
        return sorted(
            layer.fields(),
            key=lambda f: _order.get(self._column_config.get(f.name()), 0),
        )

    def _apply_table_column_layout(self, layer, reset_filters=True):
        if reset_filters:
            self._reset_filter_state()

        previous_populating = self._populatingTable
        filter_updates_enabled = self.filterTbl.updatesEnabled()
        table_updates_enabled = self.tbl.updatesEnabled()
        filter_header = self.filterTbl.horizontalHeader()
        table_header = self.tbl.horizontalHeader()
        filter_header_updates_enabled = filter_header.updatesEnabled()
        table_header_updates_enabled = table_header.updatesEnabled()
        filter_signals_blocked = False
        filter_header_signals_blocked = False
        table_header_signals_blocked = False
        self._populatingTable = True
        self.filterTbl.setUpdatesEnabled(False)
        self.tbl.setUpdatesEnabled(False)
        filter_header.setUpdatesEnabled(False)
        table_header.setUpdatesEnabled(False)
        if _SUPPRESS_TABLE_LAYOUT_SIGNALS:
            filter_signals_blocked = self.filterTbl.blockSignals(True)
            filter_header_signals_blocked = filter_header.blockSignals(True)
            table_header_signals_blocked = table_header.blockSignals(True)
        try:
            self.filterTbl.clear()
            self.filterTbl.setRowCount(1)
            if layer is None:
                self.filterTbl.setColumnCount(0)
                self.lblCount.setText('')
                self.btnSelectionFilter.setEnabled(False)
                self._displayedFieldNames = []
                self._modelColumnOrder = []
                self._modelColToFieldIndex = {}
                self.lblLegendJoin.setVisible(False)
                return

            ordered_fields = self._ordered_layer_fields(layer)
            headers = [field.name() for field in ordered_fields]
            self._displayedFieldNames = headers
            if self._tableModel is None:
                self._modelColumnOrder = []
                self._modelColToFieldIndex = {}
            else:
                self._modelColumnOrder = [
                    self._tableModel.fieldCol(
                        layer.fields().indexFromName(name)
                    )
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
            if self._tableModel is None:
                self._filterProxy.set_readonly_columns([])
            else:
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

            if self._tableModel is not None:
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
            else:
                for visual_pos, name in enumerate(headers):
                    hidden = self._column_config.get(name) == COLUMN_HIDDEN
                    self.filterTbl.setColumnHidden(visual_pos, hidden)

            # 縦見出しはtbl側の行番号ガター幅に合わせて確保する。
            # ラベルは行番号の代わりに絞り込み行であることを示すマークを表示する。
            mark_item = QTableWidgetItem('\U0001F50D')  # 🔍
            mark_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.filterTbl.setVerticalHeaderItem(0, mark_item)
            self._sync_row_header_width()

            self._fit_filter_table_height()
            self._sync_filter_viewport_margin()
            self._update_count_label()
            self._update_selection_filter_button()
        finally:
            if _SUPPRESS_TABLE_LAYOUT_SIGNALS:
                table_header.blockSignals(table_header_signals_blocked)
                filter_header.blockSignals(filter_header_signals_blocked)
                self.filterTbl.blockSignals(filter_signals_blocked)
            table_header.setUpdatesEnabled(table_header_updates_enabled)
            filter_header.setUpdatesEnabled(filter_header_updates_enabled)
            self.tbl.setUpdatesEnabled(table_updates_enabled)
            self.filterTbl.setUpdatesEnabled(filter_updates_enabled)
            self._populatingTable = previous_populating

    def _filter_texts_by_name(self):
        texts = {}
        for col in range(self.filterTbl.columnCount()):
            header_item = self.filterTbl.horizontalHeaderItem(col)
            item = self.filterTbl.item(0, col)
            if header_item is not None and item is not None:
                texts[header_item.text()] = item.text()
        return texts

    def _restore_filter_texts_by_name(self, texts):
        if not texts:
            return
        previous_populating = self._populatingTable
        self._populatingTable = True
        try:
            for col, field_name in enumerate(self._displayedFieldNames):
                item = self.filterTbl.item(0, col)
                if item is not None:
                    item.setText(texts.get(field_name, ''))
        finally:
            self._populatingTable = previous_populating

    def _ensure_table_loaded(self):
        if self._tableModel is not None:
            return True
        layer = self._currentLayer
        if layer is None:
            return False
        filter_texts = self._filter_texts_by_name()
        self._populate_table(layer)
        self._restore_filter_texts_by_name(filter_texts)
        return self._tableModel is not None

    def _populate_table(self, layer):
        previous_syncing = self._syncing_selection
        self._syncing_selection = True
        self._populatingTable = True
        try:
            self._reset_filter_state()
            self._release_table_model()
            if layer is None:
                self._apply_table_column_layout(layer, reset_filters=False)
                return

            # loadLayer()中はUI更新が止まるため、先に列名・検索行・件数だけ描画して
            # 「対象レイヤーを認識済みで、これからデータ行を読む」段階を見せる。
            self._apply_table_column_layout(layer, reset_filters=False)
            QApplication.processEvents()

            # 大規模レイヤーでも表示に必要な行だけ遅延取得される
            # QgsVectorLayerCache+QgsAttributeTableModel（QGIS標準の属性テーブルと
            # 同じ仕組み）を使う。全フィーチャーの即時ウィジェット化はしない。
            # loadLayer()の進捗通知（progress(int, bool&)）はPythonから受け取れず、
            # 実行中は画面の描画も止まるため、不定形（busy）のプログレスバーを
            # 出しても動かない。読み込み前に文字で知らせておく。
            self.lblCount.setText(
                self.tr('Processing... this may take a while for large layers')
            )
            # processEvents()で配置（幅）は更新されるが、広がった部分の描画は
            # 後回しになり、そのままloadLayer()で止まると文字が途中で切れて
            # 見えた。下の行（件数・凡例・ボタン）をその場で描画させる。
            QApplication.processEvents()
            self.lblCount.parentWidget().repaint(
                self.lblCount.geometry().united(
                    self.btnLanguage.geometry()
                ).adjusted(-2, -2, 2, 2)
            )
            self._layerCache = QgsVectorLayerCache(layer, _LAYER_CACHE_SIZE)
            self._layerCache.setCacheGeometry(False)
            self._tableModel = QgsAttributeTableModel(self._layerCache)
            self._tableModel.loadLayer()
            self._filterProxy.setSourceModel(self._tableModel)

            self._apply_table_column_layout(layer, reset_filters=False)
        finally:
            self.filterProgressBar.hide()
            self.filterProgressBar.setRange(0, 1)
            self._syncing_selection = previous_syncing
            self._populatingTable = False

    def _on_filter_cell_changed(self, item):
        if self._populatingTable:
            return
        visual_col = item.column()
        header_item = self.filterTbl.horizontalHeaderItem(visual_col)
        field_name = header_item.text() if header_item is not None else ''
        if self._tableModel is None:
            if not self._ensure_table_loaded():
                return
            if field_name in self._displayedFieldNames:
                visual_col = self._displayedFieldNames.index(field_name)
            else:
                return
        if visual_col >= len(self._modelColumnOrder):
            return
        item = self.filterTbl.item(0, visual_col)
        if item is None:
            return
        self._pendingFilters[self._modelColumnOrder[visual_col]] = (
            item.text(),
            self._filterIsNumeric[visual_col],
        )
        self._filterDebounceTimer.start(_FILTER_DEBOUNCE_MS)

    def _flush_pending_filters(self):
        if self._filterDebounceTimer.isActive():
            self._filterDebounceTimer.stop()
            self._commit_pending_filters()

    def _commit_pending_filters(self):
        for model_col, (text, is_numeric) in self._pendingFilters.items():
            text = text.strip()
            if text:
                self._activeFilters[model_col] = (text, is_numeric)
            else:
                self._activeFilters.pop(model_col, None)
        self._pendingFilters = {}
        self._recompute_filter_matches()
        self._update_count_label()

    def _update_count_label(self):
        layer = self._currentLayer
        if layer is None:
            self.lblCount.setText('')
            return
        row_count = (
            self._filterProxy.rowCount()
            if self._tableModel is not None
            else layer.featureCount()
        )
        if self._selectionFilterFids is not None:
            text = self.tr('{0} features (filtered by selection)')
        else:
            text = self.tr('{0} features')
        self.lblCount.setText(text.format(row_count))

    # -- 選択で絞り込み -------------------------------------------------

    def _update_selection_filter_button(self):
        layer = self._currentLayer
        try:
            has_selection = (
                layer is not None and layer.selectedFeatureCount() > 0
            )
        except RuntimeError:
            has_selection = False  # レイヤーのC++実体が削除済み
        # 絞り込み中は、選択が無くても解除のために押せるままにする。
        self.btnSelectionFilter.setEnabled(
            self._tableModel is not None
            and (self._selectionFilterFids is not None or has_selection)
        )

    def _on_selection_filter_clicked(self):
        layer = self._currentLayer
        if layer is None or self._tableModel is None:
            return
        self._flush_pending_filters()
        if self._selectionFilterFids is not None:
            self._selectionFilterFids = None
        else:
            fids = set(layer.selectedFeatureIds())
            if not fids:
                return
            self._selectionFilterFids = fids
        self._apply_combined_filter()
        self._update_count_label()
        self._update_selection_filter_button()

    def _apply_combined_filter(self):
        # 検索行の条件（_columnMatchedFids）と選択で絞り込み（_selectionFilterFids）
        # はANDで組み合わせる。どちらもNoneなら絞り込み無し。
        fids = self._columnMatchedFids
        selection_fids = self._selectionFilterFids
        if selection_fids is not None:
            fids = (
                set(selection_fids) if fids is None else fids & selection_fids
            )
        self._apply_matching_fids(fids)

    def _apply_matching_fids(self, fids, reduce_selection=True):
        # 絞り込みの反映（set_matching_fids→invalidate()）でテーブルの選択は
        # すべて消える。そのまま選択変更が地図へ伝わると地図の選択まで消える
        # ため、伝播を止めてから、地図の選択のうち絞り込み後も見えている地物
        # だけを残して選び直す（見えなくなった地物は地図の選択からも外す。
        # invalidateFilter()を使っていた時と同じ結果）。選択が減った場合は、
        # 当時と同じく残った選択へズーム（またはパン）する。
        # reduce_selection=Falseは地図側で選び直した直後用（地図の選択は
        # ユーザーが今決めたものなので、絞り込みに合わせて減らさない）。
        layer = self._currentLayer
        previous_syncing = self._syncing_selection
        self._syncing_selection = True
        selection_reduced = False
        try:
            # 表の選択は後で地図の選択から選び直すため、invalidate()の前に
            # 通知なしで捨てておく。選択を残したままだと、invalidate()が選択中の
            # セルを1つずつ付け替えるため10万件選択で約1.2秒かかった。
            self.tbl.selectionModel().reset()
            self._filterProxy.set_matching_fids(fids)
            if layer is None:
                return
            selected_ids = layer.selectedFeatureIds()
            if reduce_selection and fids is not None and selected_ids:
                kept = [fid for fid in selected_ids if fid in fids]
                if len(kept) != len(selected_ids):
                    layer.selectByIds(kept)
                    selection_reduced = True
        finally:
            self._syncing_selection = previous_syncing
        # 地図の選択が変わらないなら地図は描き直さない（入力のたびに描画を
        # やり直させない）。
        self._sync_table_selection_from_layer(
            scroll=False, refresh_map=selection_reduced
        )
        # 行数（行番号の桁数）が変わっても縦スクロールの範囲が変わらない場合は
        # rangeChangedが来ないため、ここでも行番号の幅を合わせる。
        self._sync_row_header_width()
        if selection_reduced:
            self._zoom_to_selection(
                layer, pan_only=self.chkDisableZoom.isChecked()
            )

    def _recompute_filter_matches(self):
        # filterAcceptsRow内でindex.data()を行数分×列数分呼ぶと、大規模レイヤーで
        # キャッシュ/プロバイダ往復が積み重なり致命的に遅くなる（14万件で約29秒）。
        # ここで一括スキャンして一致fidの集合を先に作り、プロキシ側はその集合との
        # 所属確認（O(1)）だけにする。
        layer = self._currentLayer
        if layer is None or not self._activeFilters:
            self.filterProgressBar.hide()
            self._columnMatchedFids = None
            self._apply_combined_filter()
            return

        checks = []
        for model_col, (text, is_numeric) in self._activeFilters.items():
            field_index = self._modelColToFieldIndex.get(model_col)
            if field_index is None:
                continue
            use_numeric = (
                is_numeric or _looks_like_numeric_comparison_filter(text)
            )
            checks.append((field_index, text, use_numeric))
        if not checks:
            self.filterProgressBar.hide()
            self._columnMatchedFids = None
            self._apply_combined_filter()
            return

        # uniqueValues中は進捗が取れないため、不定形（busy）表示にしておく。
        self.filterProgressBar.setRange(0, 0)
        self.filterProgressBar.show()
        QApplication.processEvents()

        # 列ごとに、値の一覧から一致する値を求めて式にする（_value_filter_expression）。
        # 式にできない列だけ、従来どおり全件を読んで1件ずつ判定する。
        expressions = []
        scan_checks = []
        no_match = False
        for field_index, text, use_numeric in checks:
            expression = _value_filter_expression(
                layer, field_index, text, use_numeric
            )
            if expression is None:
                scan_checks.append((field_index, text, use_numeric))
            elif not expression:
                no_match = True
                break
            else:
                expressions.append(f'({expression})')

        matched = set()
        if not no_match:
            matched = self._collect_matching_fids(
                layer, expressions, scan_checks
            )

        self.filterProgressBar.hide()
        self._prevActiveFilters = dict(self._activeFilters)
        self._prevMatchedFids = matched
        self._columnMatchedFids = matched
        self._apply_combined_filter()

    def _collect_matching_fids(self, layer, expressions, scan_checks):
        request = QgsFeatureRequest()
        request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        if scan_checks:
            request.setSubsetOfAttributes(
                sorted({field_index for field_index, _, _ in scan_checks})
            )
        else:
            request.setNoAttributes()

        total = 0
        if expressions:
            request.setFilterExpression(' AND '.join(expressions))
        else:
            # 全件判定だけの場合、直前の条件がすべて今回にも同じ値で残っている
            # （＝条件を追加しただけ）なら、直前の一致fid集合を母集合として走査
            # 対象を絞り込める。条件の変更・削除は前回の集合が正しい母集合で
            # なくなるため、全件走査に戻す。
            can_narrow_from_previous = (
                self._prevMatchedFids is not None
                and all(
                    self._activeFilters.get(k) == v
                    for k, v in self._prevActiveFilters.items()
                )
            )
            if can_narrow_from_previous:
                request.setFilterFids(list(self._prevMatchedFids))
            total = (
                len(self._prevMatchedFids)
                if can_narrow_from_previous
                else layer.featureCount()
            )
        # 式で絞る場合は件数が事前に分からないため、busy表示のままにする。
        if total:
            self.filterProgressBar.setRange(0, total)
            self.filterProgressBar.setValue(0)
        progress_timer = QElapsedTimer()
        progress_timer.start()

        matched = set()
        processed = 0
        for feature in layer.getFeatures(request):
            ok = True
            for field_index, text, use_numeric in scan_checks:
                value = feature.attribute(field_index)
                cell_text = '' if value is None else str(value)
                if not _cell_matches(cell_text, text, use_numeric):
                    ok = False
                    break
            if ok:
                matched.add(feature.id())
            processed += 1
            # 全件ごとにprocessEventsを呼ぶとオーバーヘッドが積み重なるため、
            # 100ms間隔でのみ描画更新する（進捗の粒度と処理速度のバランス）。
            if progress_timer.elapsed() >= 100:
                if total:
                    self.filterProgressBar.setValue(processed)
                QApplication.processEvents()
                progress_timer.restart()
        return matched

    # -- Settingタブ ---------------------------------------------------

    def _build_setting_tab(self):
        layout = QVBoxLayout(self.tabSetting)

        container = QWidget()
        containerLayout = QVBoxLayout(container)
        containerLayout.setContentsMargins(0, 0, 0, 0)

        self.infoFieldCombos = self._build_field_group(
            containerLayout, 'Info fields', INFO_FIELD_COUNT,
            with_clear=True, with_show_all=True,
        )

        self.grpTableSetting = QGroupBox(self.tr('Table setting'))
        tableLayout = QVBoxLayout(self.grpTableSetting)
        self.btnColumnConfig = QPushButton(self.tr('Column config'))
        self.btnColumnConfig.clicked.connect(self._on_column_config)
        tableLayout.addWidget(self.btnColumnConfig)
        containerLayout.addWidget(self.grpTableSetting)

        containerLayout.addStretch(1)

        # Settingタブも項目数が多いと縦に長くなるため、Infoタブと同様に
        # スクロール領域に収めてタブ全体の最小高さを抑える。QScrollAreaは
        # 中身の幅を無視して縮むため、幅の最小値だけは中身のsizeHintで
        # 明示的に固定し、横方向の最小サイズは変えない。
        scrollArea = QScrollArea()
        scrollArea.setWidgetResizable(True)
        scrollArea.setFrameShape(QFrame.Shape.NoFrame)
        scrollArea.setWidget(container)
        scrollArea.setMinimumWidth(container.sizeHint().width())
        layout.addWidget(scrollArea)

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
            index.data(QgsAttributeTableModel.CustomRole.FeatureIdRole)
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

    def _apply_show_all_fields_state(self, checked, save=True):
        self._showAllFields = checked
        self.infoConfiguredContainer.setVisible(not checked)
        self.infoAllFieldsContainer.setVisible(checked)
        self.lblAllInfoMode.setText(
            self.tr('All info mode ON')
            if checked
            else self.tr('All info mode OFF')
        )
        self._update_info_tab()
        if save:
            self._save_state()

    def _on_show_all_fields_toggled(self, checked):
        self._apply_show_all_fields_state(checked)

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

        container = QWidget()
        containerLayout = QVBoxLayout(container)
        containerLayout.setContentsMargins(0, 0, 0, 0)
        self._build_tips_group(containerLayout)
        containerLayout.addStretch(1)

        # 説明文・例一覧が多いと縦に長くなるため、Info/Settingタブと同様に
        # スクロール領域に収めてタブ全体の最小高さを抑える。QScrollAreaは
        # 中身の幅を無視して縮むため、幅の最小値だけは中身のsizeHintで
        # 明示的に固定し、横方向の最小サイズは変えない。
        scrollArea = QScrollArea()
        scrollArea.setWidgetResizable(True)
        scrollArea.setFrameShape(QFrame.Shape.NoFrame)
        scrollArea.setWidget(container)
        scrollArea.setMinimumWidth(container.sizeHint().width())
        layout.addWidget(scrollArea)

    def _build_tips_group(self, parent_layout):
        self.tipsGroup = QGroupBox(self.tr('Tips'))
        tipsLayout = QVBoxLayout(self.tipsGroup)

        self.tipsRuleLabel = QLabel(_spaced_multiline(self.tr(
            'Text columns: substring match, case-insensitive. '
            'Comma-separated = AND; "|"-separated = OR; '
            'prefix "!" to exclude.\n'
            'Numeric columns: comma-separated comparisons '
            '(>, >=, <, <=, ==, !=), "|"-separated = OR. '
            'No operator = exact match.'
        ), indent_after=_TIPS_INDENT_AFTER, break_after=('。', '.')))
        self.tipsRuleLabel.setWordWrap(True)
        tipsLayout.addWidget(self.tipsRuleLabel)

        examples = [
            ('>=100', self.tr('100 or more')),
            ('<200', self.tr('Less than 200')),
            ('>=100,<200', self.tr('100 or more, less than 200')),
            ('!=0', self.tr('Not equal to 0')),
            ('100', self.tr('Exactly 100')),
            ('!abc', self.tr('Excludes "abc"')),
            ('abc,!xyz', self.tr('Contains "abc", excludes "xyz"')),
            ('abc|xyz', self.tr('Contains "abc" or "xyz"')),
            ('<100|>=500', self.tr('Less than 100, or 500 or more')),
        ]
        grid = QGridLayout()
        self.tipsExplLabels = []
        for row, (pattern, explanation) in enumerate(examples):
            patternLabel = QLabel(_tips_pattern_link_html(pattern))
            patternLabel.setToolTip(self.tr('Click the pattern to copy'))
            patternLabel.linkActivated.connect(
                lambda _href, text=pattern: self._copy_to_clipboard(text)
            )
            grid.addWidget(patternLabel, row, 0)
            explLabel = QLabel('：' + explanation)
            explLabel.setWordWrap(True)
            grid.addWidget(explLabel, row, 1)
            self.tipsExplLabels.append(explLabel)
        grid.setColumnStretch(1, 1)
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
            previous_syncing = self._syncing_selection
            self._syncing_selection = True
            try:
                self._apply_table_column_layout(layer)
            finally:
                self._syncing_selection = previous_syncing
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
            # フィールド名の長さでパネル最小幅が広がらないよう、幅の基準を
            # 短い文字数に固定する。表示しきれない分はQtの標準機能で
            # 「...」省略表示される。
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(6)
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
                if with_show_all:
                    # 固定値ではなく「Show all fields」ボタンの実際の幅に
                    # 追従させる（言語切替でテキスト幅が変わってもretranslate_ui
                    # 側で再同期される）。
                    clear_btn.setMinimumWidth(
                        self.btnShowAllFields.sizeHint().width()
                    )
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
