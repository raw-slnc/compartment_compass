# -*- coding: utf-8 -*-
import math

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
)

COLUMN_DISPLAY = 'display'
COLUMN_INFO = 'info'
COLUMN_HIDDEN = 'hidden'

_STATES = [COLUMN_DISPLAY, COLUMN_INFO, COLUMN_HIDDEN]

_BTN_STYLE = {
    COLUMN_HIDDEN: (
        'QPushButton{background:#cccccc;color:#666666;'
        'border:1px solid #aaa;padding:2px 8px;}'
    ),
    COLUMN_DISPLAY: (
        'QPushButton{background:#4a90d9;color:white;'
        'border:1px solid #357abd;padding:2px 8px;}'
    ),
    COLUMN_INFO: (
        'QPushButton{background:#e67e22;color:white;'
        'border:1px solid #ca6f1e;padding:2px 8px;}'
    ),
}

ROWS_PER_PAGE = 20
GRID_COLS = 2
ITEMS_PER_PAGE = ROWS_PER_PAGE * GRID_COLS


class ColumnConfigDialog(QDialog):
    """列の表示状態をトグルボタンで設定するダイアログ。

    gpkg_editorのカラム設定と同じグリッド＋トグルボタンのUXを踏襲するが、
    このプラグインには編集機能が無いため 表示/情報（後列）/非表示 の3状態とする。
    """

    def __init__(self, columns, current_config=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr('Column config'))
        self.resize(420, 560)
        self.columns = list(columns)
        self._config = {
            col: (current_config or {}).get(col, COLUMN_DISPLAY)
            for col in self.columns
        }
        self._current_page = 0
        self._grid_widgets = []

        root = QVBoxLayout(self)

        self._grid_layout = QGridLayout()
        self._grid_layout.setSpacing(2)
        root.addLayout(self._grid_layout)

        nav = QHBoxLayout()
        self.btnPrev = QPushButton(self.tr('< Prev'))
        self.lblPage = QLabel('')
        self.btnNext = QPushButton(self.tr('Next >'))
        nav.addWidget(self.btnPrev)
        nav.addWidget(self.lblPage)
        nav.addWidget(self.btnNext)
        nav.addStretch(1)
        root.addLayout(nav)

        self.btnPrev.clicked.connect(self._prev_page)
        self.btnNext.clicked.connect(self._next_page)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._rebuild_grid()

    def _total_pages(self):
        return max(1, math.ceil(len(self.columns) / ITEMS_PER_PAGE))

    def _clear_grid(self):
        for widgets in self._grid_widgets:
            for w in widgets:
                self._grid_layout.removeWidget(w)
                w.setParent(None)
        self._grid_widgets.clear()
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

    def _rebuild_grid(self):
        self._clear_grid()

        total_pages = self._total_pages()
        self._current_page = max(0, min(self._current_page, total_pages - 1))
        start = self._current_page * ITEMS_PER_PAGE
        end = min(start + ITEMS_PER_PAGE, len(self.columns))
        page_items = self.columns[start:end]

        for i, col_name in enumerate(page_items):
            grid_col = i // ROWS_PER_PAGE
            grid_row = i % ROWS_PER_PAGE

            label = QLabel(f'{start + i + 1}: {col_name}')
            label.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )

            btn = QPushButton()
            btn.setMinimumWidth(70)
            self._apply_btn_state(btn, self._config[col_name])
            btn.clicked.connect(
                lambda _, cn=col_name, b=btn: self._cycle_state(cn, b)
            )

            base_col = grid_col * 2
            self._grid_layout.addWidget(label, grid_row, base_col)
            self._grid_layout.addWidget(btn, grid_row, base_col + 1)
            self._grid_widgets.append((label, btn))

        self._grid_layout.setColumnStretch(0, 1)
        self._grid_layout.setColumnStretch(1, 0)
        self._grid_layout.setColumnStretch(2, 1)
        self._grid_layout.setColumnStretch(3, 0)

        max_row = min(len(page_items), ROWS_PER_PAGE)
        spacer = QSpacerItem(
            0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding
        )
        self._grid_layout.addItem(spacer, max_row, 0)

        self.lblPage.setText(f'{self._current_page + 1}/{total_pages}')
        self.btnPrev.setEnabled(self._current_page > 0)
        self.btnNext.setEnabled(self._current_page < total_pages - 1)

    def _button_texts(self):
        return {
            COLUMN_DISPLAY: self.tr('Display'),
            COLUMN_INFO: self.tr('Info'),
            COLUMN_HIDDEN: self.tr('Hidden'),
        }

    def _apply_btn_state(self, btn, state):
        btn.setText(self._button_texts()[state])
        btn.setStyleSheet(_BTN_STYLE[state])

    def _cycle_state(self, col_name, btn):
        idx = _STATES.index(self._config[col_name])
        next_state = _STATES[(idx + 1) % len(_STATES)]
        self._config[col_name] = next_state
        self._apply_btn_state(btn, next_state)

    def _prev_page(self):
        if self._current_page > 0:
            self._current_page -= 1
            self._rebuild_grid()

    def _next_page(self):
        if self._current_page < self._total_pages() - 1:
            self._current_page += 1
            self._rebuild_grid()

    def get_config(self):
        return dict(self._config)

    def get_visible_columns(self):
        return [
            c for c, v in self._config.items()
            if v in (COLUMN_DISPLAY, COLUMN_INFO)
        ]
