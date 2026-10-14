'''
Utility Functions for UI
'''
# Standard Import
import logging

# PySide 6
from PySide6.QtWidgets import (
    QLabel, QHBoxLayout, QVBoxLayout, QKeySequenceEdit, QComboBox,
    QGroupBox, QCheckBox, QFormLayout, QLineEdit, QWidget
)
from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QKeySequence, QKeyEvent, QDoubleValidator, QIntValidator

# Local import
from src.utils.logger import logger
from src.utils.cfg_names import cn_section, cn_key
from src.utils.cfg_docs import field_doc_lines

def validate_numerical_input(input_str: str, error_label: QLabel,
                              val_lowest: float, val_highest: float):
    try:
        val = float(input_str)
        if not (val_lowest <= val <= val_highest):
            raise ValueError
        error_label.setVisible(False)
        return True
    except ValueError:
        error_label.setText(f"请输入 {val_lowest} 到 {val_highest} 之间的数字")
        error_label.setVisible(True)
        logger.debug(f"Invalid numerical input: {input_str}")
        return False

def create_error_label():
    error_label = QLabel()
    error_label.setStyleSheet("color: red;")
    error_label.setVisible(False)
    return error_label

def clear_debug_canvas(canvas):
    '''
    Flush the debug window viz canvas
    '''
    canvas.clear()
    canvas.setText("按开始或 'F1' 启动脚本")
    canvas.setAlignment(Qt.AlignCenter)

# ── 按键名的双向映射：Qt 的叫法 ↔ 驱动（interception）的叫法 ──────────────
# 为什么必须有这一层（2026-10-15，查用户 m12180「药水键改不了」时挖出来的）：
#   `QKeySequence` 只认 Qt 的叫法，驱动键表是另一套，而且**不是一一对应**：
#     · 'pageup' 在 Qt 里**根本解析不出来** —— QKeySequence('pageup').toString() == ''
#     · Qt 回头会吐 'PgUp' / 'Ins' / 'Del'，而驱动要的是 'pageup' / 'insert' / 'delete'
#   不做转换的后果（真机形态）：控件显示空白（或写出驱动不认识的键名）⇒ 用户一动
#   这个键，写回配置的就是空串 / 无效名 ⇒ **那一档/那个功能静默失效，且不报错**。
#   potion 的出厂档位键正是 pageup / home / end —— 第一个就中招。
KEY_TO_QT = {
    'pageup': 'PgUp', 'pagedown': 'PgDown',
    'insert': 'Ins', 'delete': 'Del',
    'esc': 'Esc', 'space': 'Space', 'enter': 'Enter', 'tab': 'Tab',
    'ctrl': 'Control', 'control': 'Control', 'alt': 'Alt', 'shift': 'Shift',
    'home': 'Home', 'end': 'End',
    'left': 'Left', 'right': 'Right', 'up': 'Up', 'down': 'Down',
}
KEY_FROM_QT = {
    'pgup': 'pageup', 'pageup': 'pageup',
    'pgdn': 'pagedown', 'pgdown': 'pagedown', 'pagedown': 'pagedown',
    'ins': 'insert', 'insert': 'insert',
    'del': 'delete', 'delete': 'delete',
    'escape': 'esc', 'esc': 'esc',
    'return': 'enter', 'enter': 'enter',
    'control': 'control', 'ctrl': 'ctrl',
    'space': 'space', 'tab': 'tab',
    'home': 'home', 'end': 'end',
    'left': 'left', 'right': 'right', 'up': 'up', 'down': 'down',
}


def key_to_qt(key):
    '''驱动键名 → 能喂给 QKeySequence 的显示名（空 ⇒ 空串）。'''
    k = str(key or '').strip()
    if not k:
        return ''
    low = k.lower()
    if low in KEY_TO_QT:
        return KEY_TO_QT[low]
    if low.startswith('f') and low[1:].isdigit():
        return low.upper()          # f1 → F1
    return k.upper() if len(k) == 1 else k


def key_from_qt(text):
    '''QKeySequence 的显示名 → 驱动键名（空 ⇒ 空串；未知名原样小写）。'''
    t = str(text or '').strip()
    if not t:
        return ''
    low = t.lower().replace(' ', '')
    if low in KEY_FROM_QT:
        return KEY_FROM_QT[low]
    if low.startswith('f') and low[1:].isdigit():
        return low
    return low


class SingleKeyEdit(QKeySequenceEdit):
    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        modifiers = event.modifiers()

        # Allow modifier-only keys
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta):
            self.setKeySequence(QKeySequence(key))
            return

        # Otherwise, record only one key (replace previous)
        if modifiers:
            # This prevents combos like Ctrl+A
            self.setKeySequence(QKeySequence(key))
        else:
            self.setKeySequence(QKeySequence(key))

    def set_key(self, key_str):
        """把驱动/interception 的键名填进控件（'pageup' / 'control' / 'w' …）。

        ⚠️ 必须过 KEY_TO_QT：直接 `QKeySequence('pageup')` 会得到**空序列**
           （Qt 不认这个名字），控件上是空白，用户一动就把它写成空串。
        """
        self.setKeySequence(QKeySequence(key_to_qt(key_str)))

    def get_key(self):
        """取当前键的**驱动名**（'pageup' / 'insert' / 'w' …）。

        ⚠️ 必须过 KEY_FROM_QT：Qt 会用 NativeText 吐 'PgUp' / 'Ins' / 'Del'，
           这些名字喂给驱动是 UnknownKeyError（每类只warning一次 ⇒ 静默失效）。
        """
        seq = self.keySequence()
        return key_from_qt(seq.toString(QKeySequence.NativeText).strip())

class QtLogHandler(logging.Handler, QObject):
    '''
    QtLogHandler
    '''
    log_signal = Signal(str, int)  # message, level

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(msg, record.levelno)

def set_gbox_internal_visible(gbox, visible):
    '''
    「显示内部参数」勾选框用（2026-10-14 加）：
    把 `hide_keys`（= src/ui/ui.py:ADV_FIELD_HIDE）里那些底层参数整行显示/隐藏。

    这些键的功能一直在工作（引擎照读），只是默认不摆在页面上 ——
    因为注释里写明了"改错会把挂机搞坏"。它们不是"不存在"，是"默认收起来"。
    '''
    for key in getattr(gbox, "_internal_keys", ()):
        row = getattr(gbox, "_field_rows", {}).get(key)
        if row is not None:
            row.setVisible(bool(visible))


def create_advance_setting_gbox(title, cfg, comments=None, comments_section=None,
                                hide_keys=(), field_docs=None, show_internal=False):
    '''
    建一组"参数设置"控件（2026-10-14 改版）

    每个参数一行，行内从上到下三样：
      标签 + 控件      ← 取值逻辑与旧版完全一致（仍然直接写回 cfg[title][key]）
      说明：…          ← 灰色小字，来源是 config_default.yaml 里紧贴该键上方的注释
      参考：默认 X；…  ← 出厂值 + 注释里带数字的建议
    说明/参考只显示摘要，鼠标悬停在控件或说明上给全文；注释原文一行不丢。

    `field_docs` 由 `src/utils/cfg_docs.load_field_docs()` 提供。**不走
    load_yaml_with_comments** —— 后者拿的是键下方那段注释，会整体错位一格
    （详见 src/utils/cfg_docs.py 的模块注释）。
    '''
    # 分组标题显示中文；内部一律仍用英文 key，取值/写回逻辑不受影响
    gbox = QGroupBox(cn_section(title))
    form_layout = QFormLayout()
    gbox._field_refs = {}                       # 供 update_advance_setting_ui_from_cfg 回填
    gbox._field_rows = {}                       # 整行控件（含说明/参考），「显示内部参数」用
    gbox._internal_keys = set(hide_keys or ())  # 默认收起来的底层参数

    if comments_section and title in comments_section:
        gbox.setToolTip(comments_section[title])

    def get_comment(key):
        # 优先用按行读出来的说明（紧贴键上方的那段，不会错位）
        doc = (field_docs or {}).get(title, {}).get(key)
        if doc:
            return doc
        if comments and title in comments and key in comments[title]:
            return comments[title][key]
        return ""

    def make_value_widget(key, value):
        '''按类型建控件；返回值控件的 QWidget（取值仍直接写回 cfg[title][key]）'''
        tooltip = get_comment(key)

        if isinstance(value, bool):
            checkbox = QCheckBox()
            checkbox.setChecked(value)
            checkbox.setToolTip(tooltip)
            def update_checkbox(state):
                cfg[title][key] = Qt.CheckState(state) == Qt.Checked
            checkbox.stateChanged.connect(update_checkbox)
            gbox._field_refs[key] = checkbox
            return checkbox

        if isinstance(value, list) or isinstance(value, tuple):
            box = QWidget()
            hbox = QHBoxLayout(box)
            hbox.setContentsMargins(0, 0, 0, 0)
            edits = []
            for v in value:
                line = QLineEdit(str(v))
                validator = QDoubleValidator() if isinstance(v, float) else QIntValidator()
                line.setValidator(validator)
                line.setToolTip(tooltip)
                edits.append(line)
                hbox.addWidget(line)

            def update_list():
                cfg[title][key] = [
                    float(e.text()) if isinstance(v, float) else int(e.text())
                    for e, v in zip(edits, value) if e.text() != ''
                ]

            for edit in edits:
                edit.textChanged.connect(update_list)

            gbox._field_refs[key] = edits
            return box

        if isinstance(value, (int, float)):
            line = QLineEdit(str(value))
            validator = QDoubleValidator() if isinstance(value, float) else QIntValidator()
            line.setValidator(validator)
            line.setToolTip(tooltip)
            def update_value(val):
                if val != '':
                    cfg[title][key] = float(val) if isinstance(value, float) else int(val)
            line.textChanged.connect(update_value)
            gbox._field_refs[key] = line
            return line

        if isinstance(value, str):
            if tooltip and "Options:" in tooltip:
                # Parse options from tooltip
                options_part = tooltip.split("Options:")[1]
                options = [opt.strip('" ') for opt in options_part.strip().split('"') if opt.strip()]

                # Create drop list
                combo = QComboBox()
                combo.addItems(options)
                if value in options:
                    combo.setCurrentText(value)
                combo.setToolTip(tooltip)

                combo.currentTextChanged.connect(lambda val: cfg[title].__setitem__(key, val))
                gbox._field_refs[key] = combo
                return combo

            line = QLineEdit(value)
            line.setToolTip(tooltip)
            line.textChanged.connect(lambda val: cfg[title].__setitem__(key, val))
            gbox._field_refs[key] = line
            return line

        return None

    def add_field(key, value):
        widget = make_value_widget(key, value)

        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 3, 0, 3)
        row_layout.setSpacing(2)

        top = QHBoxLayout()
        label = QLabel(cn_key(title, key))
        label.setMinimumWidth(170)
        top.addWidget(label)
        if widget is not None:
            top.addWidget(widget, 0 if isinstance(widget, QCheckBox) else 1)
        top.addStretch(1)
        row_layout.addLayout(top)

        summary, reference = field_doc_lines(field_docs, title, key, value)
        full_tip = "\n".join(p for p in (summary, reference) if p)
        if summary:
            doc_label = QLabel("说明：" + summary)
            doc_label.setWordWrap(True)
            doc_label.setStyleSheet("color:#8a8a8a; font-size:11px;")
            doc_label.setToolTip(full_tip or summary)
            row_layout.addWidget(doc_label)
        if reference:
            ref_label = QLabel("参考：" + reference)
            ref_label.setWordWrap(True)
            ref_label.setStyleSheet("color:#6b7f9c; font-size:11px;")
            ref_label.setToolTip(full_tip or reference)
            row_layout.addWidget(ref_label)

        if key in gbox._internal_keys:
            row.setVisible(bool(show_internal))
        gbox._field_rows[key] = row
        form_layout.addRow(row)

    for key, value in cfg[title].items():
        # hide_keys 里的字段默认收起来（功能照常，引擎仍然读这个配置），
        # 用户在页面上勾「显示内部参数」时才显示 —— 见 set_gbox_internal_visible。
        add_field(key, value)

    gbox.setLayout(form_layout)
    return gbox
