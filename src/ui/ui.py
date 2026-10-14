'''
UI main
'''
# Standard import
import sys
import os
import re
import json
import copy
import ctypes
import logging
import subprocess
import time

# PySide 6
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel, QVBoxLayout, QWidget,
    QCheckBox, QListWidget, QFileDialog, QHBoxLayout, QLineEdit,
    QPlainTextEdit, QTabWidget, QGroupBox, QFormLayout,
    QSizePolicy, QComboBox, QListWidgetItem, QScrollArea,
    QInputDialog, QMessageBox, QDialog, QDialogButtonBox, QDoubleSpinBox
)
from PySide6.QtGui import QTextCharFormat, QColor, QTextCursor, QPixmap, QImage, QIcon
from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QProcess

# Local import
from src.utils.logger import logger
from src.utils.ui import (
    validate_numerical_input, clear_debug_canvas,
    create_error_label, SingleKeyEdit, QtLogHandler, create_advance_setting_gbox,
    set_gbox_internal_visible,
)
from src.utils.cfg_docs import load_field_docs
from src.utils.common import (
    load_yaml, override_cfg, save_yaml, get_cfg_diff, load_yaml_with_comments
)
from src.utils.paths import resource_path, APP_ROOT, IS_FROZEN

# window size for each tab
TAB_WINDOW_SIZE = {
    '主界面': (700, 800),
    '调试': (620, 960),
    '高级设置': (820, 800),
    '游戏画面': (1502, 844),   # 2026-10-14 起不再是独立页签（并入「调试」），保留兼容
    '路线地图': (800, 800),
    '工具箱': (800, 860),
}

# 「调试」页里游戏画面 / 路线地图两块画布的初始尺寸（2026-10-14 用户指定 526×216）
DEBUG_CANVAS_SIZE = (526, 216)

# 高级设置页的分组（2026-10-14 用户指定）：桶名 -> 这一桶里放哪几段配置
ADV_TAB_GROUPS = [
    ("🎯 识别与出手", ['monster_detect', 'potion']),
    ("🧭 路线·梯子与「走丢」", ['route']),
    ("🏷️ 名字定位", ['nametag']),
]

# ===== 高级设置的瘦身清单 =====
# 这两张表只影响「高级设置页显示什么」，**不动任何功能** ——
# 隐藏的配置项引擎照常读取，只是不把它们摆在用户面前。
# 想把哪一项找回来，把它从表里删掉即可。

# 整段隐藏：要么主界面已经能改（重复），要么是底层/调试参数（用户改了只会搞坏）
ADV_SETTINGS_HIDE = [
    'key',                  # 按键映射        -> 主界面「按键绑定」
    'bot',                  # 脚本模式 / 地图  -> 主界面
    'buff_skill',           # 增益技能        -> 主界面「宠物技能」
    'directional_attack',   # 攻击范围 / 冷却  -> 主界面「攻击设置」
    'aoe_skill',            # 同上（攻击模式切到 AOE 时）
    'minimap',              # 小地图取色 / 缩放 -> 纯算法与显示参数
    # ⚠️ 'route' 已于 2026-09-14 **移出**本表：需要把「平台边缘安全余量」
    #    (edge_guard_inset) 暴露给用户按地形调 —— 窄平台和宽平台该值差别很大
    #    （窄平台要 2~3，宽平台填 0），写死在配置里不合适。
    #    段内其余键仍在 ADV_FIELD_HIDE 里收着，界面上只露这一个。
    'character',            # 角色尺寸        -> 排除玩家自身区域（三种模板模式都用）
    'route_recoder',        # 路线录制器参数   -> 独立工具的配置
    'ui_coords',            # 界面按钮坐标    -> 测准后写死，用户改错会点飞
    'game_window',          # 窗口标题 / 尺寸  -> 同上
    'system',               # 线程帧率 / 语言  -> 底层性能参数
    'watchdog',             # 看门狗参数      -> 卡死判定阈值，默认值即可
    # 🧪 'bars' 2026-09-27 加：血条/蓝条识别参数（roi / span / HSV）
    #    这些是**开发期在真机上量出来的**，不是调参项 —— 用户改只会把读数搞错，
    #    而且改错了的症状是"血满着疯狂喝药"或"永远读不到"，很难联想到是这里。
    #    所以整段收起来，用户不需要也不该接触（用户零配置，见交接文档）。
    'bars',
    # 🧪 'potion' 显示在界面上，但用 ADV_FIELD_HIDE 收掉"不该手改"的键 ——
    #    用户真正要调的只有「药水键」和「什么时候喝」，其余留给界面上的
    #    「🧪 自动喝药」面板（那里显示成人话）。
]

# 段内隐藏：只留「用户真的会调」的项，算法内部阈值一律收起来
ADV_FIELD_HIDE = {
    # 🧪 potion（2026-09-27）：面板上已经用大白话展示了全部档位与冷却，
    #    这里只放行 enabled / hp / mp / confirm_frames / *_cooldown_sec
    #    中"用户会改"的那几个，见 create_potion_gbox 的说明。
    #    ⚠️ 档位列表（hp/mp）是嵌套结构，高级设置页渲染不了，必须收起来。
    'potion':         ['hp', 'mp'],
    'nametag':        ['mode', 'global_diff_thres', 'split_width',
                       # 抗闪（2026-10-14）：算法内部阈值，用户改只会把定位搞乱
                       'relax_ratio', 'relax_max_jump'],
    # ⚠️ 不变量（2026-10-13 加）：**凡是有主界面控件的键，一律不进这个清单** ——
    #    否则"主界面上露出来"与"高级设置里藏起来"两处口径打架，症状是
    #    "在一个地方改了、另一个地方看不见"。`diff_thres` 与 `detect_range_scale_y`
    #    就是因为这条从下面挪走的（它们现在是 create_monster_gbox 里的
    #    「识别阈值」与「看得远倍数 Y」）。这条不变量由 tools/verify_monster_ui.py 盯着。
    'monster_detect': ['search_box_margin', 'contour_blur',
                       'verify_color', 'verify_color_tol', 'verify_min_ratio',
                       'hp_bar_color', 'max_mob_area_trigger',
                       # 2026-10-13「停下来打怪」：开关本身留在界面上给用户按，
                       # 这两个是节奏/安全阀阈值，属于算法内部参数，收起来。
                       'stop_attack_clear_frames', 'stop_attack_timeout_s',
                       # 2026-10-14「目标锁定」：秒数是算法内部参数，收起来；
                       # 用户要的"持续攻击到怪消失"由 stop_to_attack + 本键默认值兑现。
                       'target_hold_s',
                       # 2026-10-14「低频识别优化」：空窗秒数与两个追怪防抖参数
                       # 都是算法内部参数，收起来（默认值就是标定过的口径）；
                       # `engaged_scan` 是**开关**，留在界面上给用户按。
                       'stop_attack_clear_s', 'approach_confirm_s',
                       'approach_dir_min_s',
                       # 2026-10-14「怪检测明细行」：纯观测日志开关，用户不用管
                       # （要关就在配置文件里改 false），收起来免得面板膨胀。
                       'detect_log', 'mob_engage_mode',
                       # 2026-10-14「宽判定余量」：这是用户拍板要的旋钮，**不**收起
                       # （留在界面上可调）；下面是内部参数才收。
                       # 2026-10-13「看得远 + 走过去打」：开关（approach_mob）与
                       # 倍数（detect_range_scale）都**留**给用户按/调 ——
                       # 这两个正是"怪认不出来"时用户要动的那两个旋钮；
                       # 高度容差与 stale 阈值是算法内部参数，收起来。
                       'approach_dy_tol', 'stale_pos_full_frame_after',
                       # 2026-10-14「丢名字存画面」：诊断开关，默认开，
                       # 属于排查工具而不是日常旋钮 ⇒ 收起来（要关就改 yaml）。
                       'miss_dump'],
    # 'route' 段 2026-09-14 起在界面上显示，但**只露 edge_guard_inset**
    # （用户要按地形调的那个）。其余全是算法阈值 / 回正线内部参数，
    # 改错只会把挂机搞坏 —— 一律收起来。
    'route': ['search_range', 'pingpong_single_route', 'pingpong_turn_range',
              'goal_reach_range', 'endpoint_margin', 'edge_guard',
              'edge_guard_row_tol', 'edge_guard_react_frames',
              'home_recenter', 'home_recenter_frames',
              'home_jump_brake', 'home_jump_brake_frames',
              'rescue_range', 'home_dist_tol', 'home_min_cover_span',
              'home_cover_far_tol', 'home_goal_mainline_tol',
              'home_min_return_span', 'home_min_leave', 'home_min_frames',
              'home_timeout', 'home_rearm_margin', 'home_rearm_timeout',
              'home_reenter_lock', 'home_jump_band_max_drop',
              'home_jump_band_inset', 'color_code', 'color_code_up_down',
              # 2026-10-14（用户拍板 A）：梯上让路的"真在爬"窗口。
              #   与 ladder_leave_x 同口径 —— 算法判据，改错会把爬梯搞坏
              #   （填 0 就退回"旗标为真一直让路"的老行为），不放界面。
              'ladder_still_s',
              # 2026-10-14（用户拍板 D）：绳梯脚下"蹭一下"自救。
              #   同上：算法判据，改错会把爬梯搞坏（rope_nudge_s 填 0 就退回
              #   "一直站着按 up"的老行为），不放界面。
              'rope_nudge_s', 'rope_nudge_ms', 'rope_nudge_cycle_s',
              'rope_nudge_max_s',
              # 2026-10-14（用户 m10830）：爬绳"上不去就算了"的窗口。
              #   同上：算法判据，改错会把爬梯搞坏（填 0 就退回
              #   "路线给 up/down 就一直让路"的老行为），不放界面。
              'rope_stuck_s',
              # 2026-10-15（用户 m11669「上梯子要走完，不要检测怪了」）：
              #   在梯子上、路线没给上下时替它补一个竖向指令。
              #   同上：算法判据（`ladder_finish: false` 或 `ladder_finish_s: 0`
              #   就逐字退回"路线不给竖向就不按上下"的老行为），不放界面。
              'ladder_finish', 'ladder_finish_s',
              # 2026-10-14（用户拍板 E1）：goal 像素不再吃掉整帧。
              #   同上：算法判据（填 0 退回"最近像素是谁就用谁"的老行为），不放界面；
              #   判据不可用时本层自动不生效，见 _goal_pixel_skip_params()。
              'goal_pixel_skip'],
    # 🎥 demo_record（2026-10-14 加）：开/关、接管键盘、压缩、目录、单文件分钟数
    #    都有主界面控件（create_demo_gbox）⇒ 一律**不**进本表（上面那条不变量）；
    #    只剩三个算法内部键：记不记按键明细 / 隔几帧抽一帧 / 单文件最多几帧。
    'demo_record': ['record_keys', 'every_n_frames', 'max_frames'],
}

class ConfigCombo(QComboBox):
    '''
    可编辑下拉框（配置方案用）。editable QComboBox 会吞回车且不发 editingFinished
    （2026-09-12 两次用户实测），所以子类化重写 keyPressEvent 发信号 —— PySide
    对「Python 子类重写 keyPressEvent」的虚函数分发比事件过滤器可靠。
    '''
    return_pressed = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.return_pressed.emit()
            e.accept()
            return
        super().keyPressEvent(e)


class ConfigComboLineEdit(QLineEdit):
    '''配置方案输入框：回车发 return_pressed（见 ConfigCombo 说明）。'''
    return_pressed = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.return_pressed.emit()
            e.accept()
            return
        super().keyPressEvent(e)


class RecorderPanel(QWidget):
    """录制路线时的**置顶分步面板**（2026-09-12 用户选定的形态）。

    用户原话："录制全程都用鼠标点，不要再搞什么命令行了，而是做成分步骤的内置弹窗"。
    → 录制时这个小窗浮在游戏上面，按顺序告诉你现在该干嘛，鼠标直接点，
      不用切回主界面、也不用记 F1~F4。

    ⚠️ 关键技术点：**不能抢焦点**。
      游戏窗口一旦失活，角色会当场停住（键盘输入要窗口是前台才生效）。
      所以带上 Qt.WindowDoesNotAcceptFocus —— Windows 上等价于 WS_EX_NOACTIVATE：
      点按钮时鼠标事件照常收到，但窗口不会被激活，游戏继续是前台窗口。

    面板只负责"显示步骤 + 把按钮变成命令"，真正的动作全在录制器进程里，
    通过 on_command 回调交给主界面转发（见 MainWindow._send_to_recorder）。
    """

    # 分步文案不再用常量表 —— 改由 update_state 按 phase（见 _phase）直接给，
    # 因为同一个 phase 里的文案还要带上段数/途经点数等实时数字。

    def __init__(self, on_command, on_close, parent=None, append_home=False,
                 mode="normal"):
        super().__init__(parent)
        self._on_command = on_command
        self._on_close = on_close
        # 「只补回正线」模式（2026-09-12 用户要求）：给已经录好的图补 route_home.png，
        # 旧的主线路线（route{N}.png）一条都不动 → 只走回正线那段流程。
        self.append_mode = append_home
        # 录制模式跟随配置的练级模式：normal=巡逻 / stand=定点。
        # 2026-09-12 用户需求："录制也分两种，一种巡逻一种定点"——两套流程完全不同。
        self.mode = mode
        # 纯面板本地的选择（不用回传给录制器，只影响走到哪个 phase）
        self.mainline_done = False     # 巡逻：点过【主线录完了】
        self.want_home = False         # 巡逻：在"要不要录回正线"那步选了"要"
        self.want_another_home = False # 回正线存好后又点了【我再补一条】
        self._last_state = None        # 缓存上次状态，供纯本地按钮重绘
        self._map_id = None            # 缓存图名，模式变化时用来刷标题
        # 存段确认（2026-09-16 加）：见 update_state 里 mainline 分支的说明。
        # _saved_flash = (提示文字, 记下的时刻)，6 秒后自动不再显示；
        # _step_green 记住标题当前是不是绿的，避免每次刷新都重设样式表。
        self._saved_flash = None
        self._step_green = False
        self._last_routes = None

        self.setWindowTitle("录制挂机路线")
        self.setWindowFlags(Qt.Tool
                            | Qt.WindowStaysOnTopHint
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        self.lbl_step = QLabel("第 1 步 · 走完去程")
        self.lbl_step.setStyleSheet("font-size: 15px; font-weight: bold;")
        lay.addWidget(self.lbl_step)

        self.lbl_hint = QLabel()
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setMinimumWidth(300)
        self.lbl_hint.setMaximumWidth(340)
        lay.addWidget(self.lbl_hint)

        self.lbl_progress = QLabel("已保存路段：0 段")
        # ⚠️ 必须自动换行 + 限宽（2026-09-16 用户实测，附两张截图）：
        #    这行文字会随着录制**越来越长** —— 从
        #      「已存 0 段 · 当前 (104,108) · 本段 55 像素」
        #    涨到
        #      「已存 1 段 · 折返点 1 个 · 离起点 54px · 当前 (104,108) ·
        #        按住了【D】 ⚠️ 角色没动？ · 本段 0 像素」
        #    而它原来既没 setWordWrap 也没限宽 → 标签 sizeHint 一路变大 →
        #    **面板窗口每存一段就宽一截，而且 Qt 不会自己缩回去**。
        #    离线实测（tools 侧脚本，不 show 窗口）：短文字面板 328px → 长文字 **562px**；
        #    加上下面两条后，两种文字都恒定 **328px**。
        #    ⚠️ 同面板的 lbl_hint 一直有这两条，所以它从来没撑宽过窗口 —— 别把它删了。
        self.lbl_progress.setWordWrap(True)
        self.lbl_progress.setMinimumWidth(300)
        self.lbl_progress.setMaximumWidth(340)
        lay.addWidget(self.lbl_progress)

        # ── 按钮区 ──
        # 全部按钮都建好，但**按步骤只显示当前这一步该点的那 1~3 个**（见 _show_only）。
        # 2026-09-12 用户原话："一堆功能挤在一起不知道谁是谁"。
        # → 面板上永远只留当前真正要用的按钮，其余隐藏（QVBoxLayout 里隐藏 = 不占位）。
        # 【开始录】闸门（2026-09-15 加，用户要求"手动录制"）——
        # 点它之前录制器**一笔都不画**。理由：起点 = 第一次按下方向键那一刻的坐标，
        # 而那一刻定位可能还没成功（坐标仍是 (0,0)）→ 起点被钉成垃圾、事后看不出来。
        # 用户原话："打开的一瞬间就录制了起点，但还来不及聚焦到游戏窗口，
        # 所以这个起点我都不确定有没有录到，尤其是显示当前位置在一开始永远是 (0,0)"。
        self.btn_arm = QPushButton("开始录（我站好了）")
        self.btn_arm.setToolTip(
            "先站到你想当起点的位置，再点这个 —— 从这一刻起才开始画线。\n"
            "点之前一笔都不画，所以不用担心手滑按了方向键把起点录错。\n\n"
            "⚠️ 还没定位到角色（坐标显示 (0,0)）时按钮不出现 —— 先点一下游戏窗口\n"
            "让它在前台、确认小地图可见。")
        self.btn_arm.clicked.connect(lambda: self._on_command('arm'))
        lay.addWidget(self.btn_arm)

        self.btn_waypoint = QPushButton("在这折返，存下这段")
        self.btn_waypoint.setToolTip(
            "走到要掉头的位置（比如平台另一头）→ 点这个。\n"
            "会立刻存成 route{N}.png，然后从你现在站的位置接着录新的一段，\n"
            "用来把「去程」和「回程」分成两段。（键盘 F8）\n\n"
            "⚠️ 去程和回程必须分成两段：叠在同一段的话，同一个位置既有向右\n"
            "又有向左的路线像素，引擎分不清该走哪边 —— 会一路冲出平台。")
        self.btn_waypoint.clicked.connect(lambda: self._on_command('waypoint'))
        lay.addWidget(self.btn_waypoint)

        self.btn_finish = QPushButton("这是终点，保存这段")
        self.btn_finish.setToolTip(
            "这一段走到头了 → 点这个，立刻存成 route{N}.png，然后自动开新的一段。\n"
            "（键盘 F3）\n\n"
            "和【在这折返】效果一样，只是名字对应不同场景：\n"
            "· 中途掉头 →【在这折返】\n"
            "· 整条主线的最后一段 →【这是终点】")
        self.btn_finish.clicked.connect(lambda: self._on_command('save_route'))
        lay.addWidget(self.btn_finish)

        # 2026-09-12：定点模式删除，「标定安全点」按钮随之移除
        # （录制器侧的 save_standpoint / F5 一并去掉，见 tools/routeRecorder.py）

        # 巡逻：主线录够了 → 只影响面板走到哪一步，不用通知录制器
        self.btn_mainline_done = QPushButton("主线录完了，下一步")
        self.btn_mainline_done.clicked.connect(self._on_mainline_done)
        lay.addWidget(self.btn_mainline_done)

        self.btn_want_home = QPushButton("录回正线")
        self.btn_want_home.clicked.connect(self._on_want_home)
        lay.addWidget(self.btn_want_home)

        self.btn_skip_home = QPushButton("不用了，结束录制")
        self.btn_skip_home.clicked.connect(lambda: self._on_command('quit'))
        lay.addWidget(self.btn_skip_home)

        self.btn_home_start = QPushButton("我在落地点")
        self.btn_home_start.setToolTip(
            "⚠️ 先自己走下去（或掉下去），站到落点上再点这个。\n"
            "不是站在平台边缘 —— 起点画在边缘的话，真掉下去接不上。（键盘 F6 第一次）")
        self.btn_home_start.clicked.connect(lambda: self._on_command('home_start'))
        lay.addWidget(self.btn_home_start)

        self.btn_home_end = QPushButton("已回到主线")
        self.btn_home_end.setToolTip(
            "一路走回主线之后点这个 —— 点了就自动保存回正线。\n"
            "（键盘 F6 第二次）")
        self.btn_home_end.clicked.connect(lambda: self._on_command('home_end'))
        lay.addWidget(self.btn_home_end)

        self.btn_home_cancel = QPushButton("放弃这条")
        self.btn_home_cancel.setToolTip(
            "不想录这条回正线了？点这个退出，回到上一步。（键盘 F7）")
        self.btn_home_cancel.clicked.connect(lambda: self._on_command('home_cancel'))
        lay.addWidget(self.btn_home_cancel)

        self.btn_home_again = QPushButton("我再补一条")
        self.btn_home_again.setToolTip(
            "这张图还有别的地方会掉下去 → 点这个再录一条（会自动叠加上去）。")
        self.btn_home_again.clicked.connect(self._on_home_again)
        lay.addWidget(self.btn_home_again)

        self.btn_quit = QPushButton("结束录制")
        self.btn_quit.setToolTip("地图会自动保存，然后自动登记到地图列表里。")
        self.btn_quit.clicked.connect(lambda: self._on_command('quit'))
        lay.addWidget(self.btn_quit)

        # 次要按钮一行（暂停 / 截图 —— 任何步骤都能用，但不抢主按钮的位置）
        row = QHBoxLayout()
        self.btn_pause = QPushButton("暂停记录")
        self.btn_pause.clicked.connect(lambda: self._on_command('toggle_pause'))
        self.btn_shot = QPushButton("截图")
        self.btn_shot.setToolTip("截一张当前游戏画面存到 screenshots/（做怪物模板用）")
        self.btn_shot.clicked.connect(lambda: self._on_command('screenshot'))
        for b in (self.btn_pause, self.btn_shot):
            row.addWidget(b)
        lay.addLayout(row)

        self.lbl_note = QLabel("（键盘 F1~F8 / q 也能用，但用鼠标点就够了）")
        self.lbl_note.setWordWrap(True)
        lay.addWidget(self.lbl_note)

        self._all_buttons = (
            self.btn_arm, self.btn_waypoint, self.btn_finish,
            self.btn_mainline_done, self.btn_want_home, self.btn_skip_home,
            self.btn_home_start, self.btn_home_end, self.btn_home_cancel,
            self.btn_home_again, self.btn_quit, self.btn_pause, self.btn_shot,
        )

        self._placed = False
        self.update_state(0, False, False)

    def showEvent(self, event):
        """首次显示时贴到屏幕右上角 —— 一般不挡角色和地图；用户想挪直接拖走。"""
        super().showEvent(event)
        if self._placed:
            return
        self._placed = True
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        geo = scr.availableGeometry()
        self.adjustSize()
        self.move(max(geo.left(), geo.right() - self.width() - 24),
                  geo.top() + 24)

    def _show_only(self, primary, secondary=()):
        """只显示当前这一步该点的按钮，其余隐藏 —— 面板上永远只有 1~3 个按钮。

        2026-09-12 用户原话："一堆功能挤在一起不知道谁是谁"。
        primary   = 这一步推荐点的（加粗描边）
        secondary = 这一步也能点但不推荐（普通样式）
        """
        for b in self._all_buttons:
            b.setVisible(b in primary or b in secondary)
            b.setStyleSheet("")
        for b in primary:
            b.setStyleSheet("font-weight: 500; border: 2px solid #2d7ff9;")

    def _show_recorder_error(self, err):
        """录制器发来的严重错误（如第一帧找不到小地图）→ 弹窗让用户立刻知道。"""
        if getattr(self, "_last_recorder_err", None) == err:
            return
        self._last_recorder_err = err
        QMessageBox.warning(self, "录制器启动失败", err)

    def _set_step_text(self, text, green=False):
        """设置面板顶部的步骤标题（`green=True` = 存段确认那种绿色）。

        为什么不直接 setText + setStyleSheet：面板每秒被刷新约 5 次，
        每次都重设样式表会让 Qt 重算整棵控件树（纯浪费）。
        这里只在"颜色真的需要变"时才动样式表。
        """
        # ⚠️ 这里必须是 lbl_step.setText —— 本函数就是它的包装。
        #    2026-09-16 批量替换时把这一行也换成了 self._set_step_text(text)，
        #    直接无限递归（RecursionError），靠离线用例当场抓到。
        self.lbl_step.setText(text)
        if green != self._step_green:
            self._step_green = green
            self.lbl_step.setStyleSheet(
                "font-size: 15px; font-weight: bold;"
                + (" color: #1a7f37;" if green else ""))

    def _phase(self, routes, home, sp):
        """当前该走哪一步（2026-09-12：定点流程已删，只剩巡逻这一套）：

        巡逻 normal：mainline（中转点/终点二选一）→ ask_home → home → home_done
        补录 append：直接进 home（旧主线路线不动）
        """
        if self.append_mode:
            return "home_done" if home == 2 else "home"
        if self.want_home:
            return "home_done" if home == 2 else "home"
        if self.mainline_done:
            return "ask_home"
        return "mainline"

    def _rerender(self):
        """用上次的状态重画（纯本地按钮点了之后用）。"""
        if self._last_state:
            self.update_state(**self._last_state)

    def _on_mainline_done(self):
        self.mainline_done = True
        self._rerender()

    def _on_want_home(self):
        self.want_home = True
        self._rerender()

    def _on_home_again(self):
        # 回正线已存完又想再补一条 → 回到"还没起头"那一步（旧的会叠加上去）
        self.want_another_home = True
        self._rerender()

    def update_state(self, routes=0, paused=False, map_saved=False, home=0,
                     append=False, sp=False, wp=0, mode=None, finished=False,
                     loopdist=-1, pos=None, keys="", drawn=0, err=None,
                     armed=False, posok=False, frameok=False):
        """按录制器回报的状态刷新步骤提示与按钮（录制器侧见 _emit_state）。

        routes 已存段数 / home 0没起头·1已起头·2已存 / append 只补回正线 /
        sp 保留字段（定点已删，恒为 0） / wp 途经点数 / mode 恒为 normal /
        loopdist 当前位置到第一段起点的距离（-1 = 还不该关心，见下面巡逻提示） /
        pos 角色全局坐标 / keys 当前捕获到的按键 / drawn 本段已画像素数（实时反馈） /
        err 录制器严重错误（如「找不到小地图」）；None 或 'none' = 正常
        armed 用户是否点过【开始录】（2026-09-15 加）—— 没点之前只显示"准备开始"这一步，
              录制器那边也一笔都不画（见 routeRecorder.arm_recording）
        posok 录制器本帧是否定位到角色（坐标不是 (0,0)）—— 决定能不能点【开始录】
        frameok 录制器本帧**有没有抓到游戏画面**（2026-09-15 加）—— 必须和 posok 分开报，
              否则"一帧都没抓到"和"抓到了但没定位到角色"在面板上长得一模一样，
              会把用户引去查错的东西（实测踩过）
        """
        if mode:
            self.mode = mode
            # 模式可能是录制器启动后才回报的 → 标题跟着换（巡逻 / 定点）
            if getattr(self, "_map_id", None):
                self.set_map_name(self._map_id)
        if append:
            self.append_mode = True
        self._last_state = dict(routes=routes, paused=paused, map_saved=map_saved,
                                home=home, append=append, sp=sp, wp=wp, mode=mode,
                                loopdist=loopdist, pos=pos, keys=keys, drawn=drawn,
                                err=err, armed=armed, posok=posok, frameok=frameok)
        if err and err != "none":
            self._show_recorder_error(err)

        # 存段确认（2026-09-16 加）：录制器存完一段就把 routes +1，这里据此点亮
        # 「✅ 第 N 段已保存」。为什么不能只靠主界面状态栏那句 ——
        # 用户实测原话："点完保存终点后一点反馈都没有，我都不确定自己是否保存上了"：
        # 面板是小窗浮在游戏上，而状态栏在主界面里、被挡在后面，等于没有反馈。
        # 判定用"routes 变大"而不是"点了按钮"，所以无论用鼠标还是键盘 F3 存段都有效。
        if self._last_routes is not None and routes > self._last_routes:
            self._saved_flash = (f"✅ 第 {routes} 段已保存", time.time())
        self._last_routes = routes

        if finished:
            self._set_step_text("录制已结束")
            self.lbl_hint.setText("正在登记地图… 这个窗口马上会自己关掉。")
            for b in self._all_buttons:
                b.setVisible(True)
                b.setEnabled(False)
            return

        self.btn_pause.setText("继续记录" if paused else "暂停记录")

        # 起头之后就把"再补一条"的临时标志清掉（否则存完又会退回上一步）
        if home == 1:
            self.want_another_home = False

        phase = self._phase(routes, home, sp)
        # 存完一条又点了【我再补一条】→ 当作还没起头，重新走一遍
        if phase == "home_done" and self.want_another_home:
            phase, home = "home", 0
        secondary = [self.btn_pause, self.btn_shot]

        # 【开始录】闸门（2026-09-15 加）：用户没点之前一律停在"准备开始"这一步。
        # 录制器那边同样一笔都不画 —— 见 routeRecorder.arm_recording。
        if not armed:
            phase = "ready"

        if phase == "ready":
            self._set_step_text("第 1 步 · 准备开始")
            if not frameok:
                # 2026-09-15 加：**抓帧失败**和"没定位到角色"必须分开说。
                # 实测踩过：只报一个信号时，用户按"没定位到角色"去查颜色配置，
                # 而真因是录制器一帧都没抓到（日志里只有一行"还没抓到游戏画面，等待中"）。
                self.lbl_hint.setText(
                    "⏳ 还没抓到游戏画面 —— 录制器现在拿不到游戏窗口的画面。\n\n"
                    "按顺序查：\n"
                    "1. 游戏窗口是不是被别的窗口挡住 / 最小化了？\n"
                    "2. 上一次录制的进程可能没退干净 —— 把屏幕上那些调试小窗\n"
                    "   （Game Window Debug / Route Map Debug / 小地图）全关掉，\n"
                    "   结束录制后重开一次；\n"
                    "3. 游戏刚开、画面还没出来（等几秒）。")
                self.lbl_progress.setText("等待游戏画面…")
            elif not posok:
                self.lbl_hint.setText(
                    "⏳ 画面有了，但没找到你的角色（坐标显示 (0,0)）。\n\n"
                    "先做这两步：\n"
                    "1. 点一下游戏窗口，让它成为前台\n"
                    "2. 确认游戏里小地图是完整可见的\n\n"
                    "⚠️ 这时候按方向键会把起点记成 (0,0) —— 所以现在先别走。\n"
                    "（还不行就把这一屏截图发给 AI —— 玩家点的颜色参数目前不在界面上。）")
                self.lbl_progress.setText("等待定位…")
            else:
                self.lbl_hint.setText(
                    "✅ 已找到你的角色。\n\n"
                    "现在站到你想当起点的位置（一般是平台的一端），\n"
                    "然后点下面的【开始录】—— 从那一刻起才开始画线。\n\n"
                    "点了之后：走到要掉头的位置 →【在这折返，存下这段】。")
                self.lbl_progress.setText("可以开始录了")
            # 按钮常驻同一批（避免布局跳动）：没准备好时**不显示**【开始录】
            # —— "按钮出现 = 可以开始了"本身就是最清楚的信号。
            # 这里不放【暂停记录】：还没开始录，暂停没有意义（2026-09-15）。
            # 放【截图】：定位失败时正需要它留证据。
            self._show_only([self.btn_arm] if (frameok and posok) else [],
                            [self.btn_shot, self.btn_quit])

        elif phase == "mainline":
            # 存段确认（2026-09-16 加，用户实测："点完保存终点后一点反馈都没有，
            # 我都不确定自己是否保存上了"）：
            # 录制器存完段只会把 routes +1，**面板界面几乎没变化** ——
            # 步骤标题反而从「第 1 段」跳到「第 2 段」，看着像"还要再录一段"，
            # 而真正的确认消息发在**主界面状态栏**上，被面板和游戏挡在后面看不见。
            # ⇒ 在面板自己的标题上给一条 6 秒内不会错过的绿色确认。
            _flash = ""
            if self._saved_flash:
                _msg, _t = self._saved_flash
                if time.time() - _t < 6.0:
                    _flash = f"{_msg} → 接着录第 {routes + 1} 段"
                else:
                    self._saved_flash = None
            self._set_step_text(_flash or f"巡逻路线 · 第 {routes + 1} 段",
                                green=bool(_flash))
            hint = ("走，走到一个位置就点一下：\n"
                    "· 走到要掉头的位置（平台另一头）→【在这折返，存下这段】\n"
                    "· 这段是最后一段 →【这是终点，保存这段】\n\n"
                    "想让角色来回走：去程存一段、回程再存一段（共两段）。")
            progress = f"已存 {routes} 段" + (f" · 折返点 {wp} 个" if wp else "")
            # ── 闭环实时提示（2026-09-12 加）────────────────────────────────
            # 走完最后一段会循环回第一段，终点必须落在**第一段起点**附近才接得上：
            #   ≤10px 稳 / ≤40px 靠失联救援 / >40px 会卡在终点。
            # 这个数以前只能事后看日志，录的时候是盲的 —— 现在边走边看。
            loopdist = self._last_state.get("loopdist", -1)
            if loopdist >= 0:
                if loopdist <= 10:
                    verdict = "现在收尾就能闭合 ✓"
                elif loopdist <= 40:
                    verdict = "只能靠失联救援接上，建议再走近点"
                else:
                    verdict = "这样收尾会闭不上环"
                hint += (f"\n\n如果这是最后一段：走回起点 10px 内再保存。\n"
                         f"现在离起点 {loopdist}px —— {verdict}")
                progress += f" · 离起点 {loopdist}px"
            # ── 录制实时反馈（2026-09-12）──────────────────────────────────
            # 「走了一圈却一个像素都没画上」三种原因症状一模一样，这里直接给结论：
            #   pos 不变 + keys 有值 → 角色没动（游戏窗口没焦点，按键没进游戏）
            #   keys=none           → 按键没捕获（多半是没用管理员权限启动）
            #   pos 变 + drawn 不涨 → 画线环节坏了
            pos = self._last_state.get("pos")
            keys = self._last_state.get("keys", "") or ""
            drawn = self._last_state.get("drawn", 0)
            if pos:
                progress += f" · 当前 ({pos})"
                # ── 定位失败护栏（2026-09-14 加）────────────────────────────
                # 坐标卡在 (0,0) 说明录制器既没定位到小地图、也没找到玩家黄点。
                # 这时候录出来的路径会全部堆在一个点上（全是 goal、没有 left/right），
                # 是**废路线** —— 2026-09-13 用户就录出过一次，白折腾一轮。
                # 与其让你录完才发现，不如当场拦住。
                try:
                    _bad = (tuple(pos)[0] == 0 and tuple(pos)[1] == 0)
                except Exception:
                    _bad = False
                if _bad:
                    progress += " 🔴 定位失败"
                    hint = ("🔴 定位失败，现在录出来是废路线，别录！\n\n"
                            "坐标卡在 (0,0)，说明录制器既没定位到小地图、"
                            "也没找到玩家黄点。\n\n"
                            "先做这两步：\n"
                            "1. 确认游戏窗口是前台、小地图完整可见\n"
                            "2. 检查设置里 `minimap.player_color`（BGR）是不是你小地图上"
                            "那个点的颜色 —— 用窗口截图工具取一下真实 BGR 值\n\n"
                            "坐标不再是 (0,0) 之后再开始录。")
                if keys and keys != "none":
                    kn = {"left": "左", "right": "右", "up": "上", "down": "下",
                          "space": "跳"}.get(keys.split(',')[0], keys)
                    progress += f" · 按住了【{kn}】"
                    if drawn <= 13:      # 13 = 一个 goal 圆点的像素数
                        # ⚠️ 2026-09-16 改成独立一行（用户实测**没看见**过它）：
                        #    原来这里只是行尾加一个" ⚠ 角色没动？"，淹没在
                        #    「已存 1 段 · 折返点 1 个 · 离起点 54px · 当前 (104,108) ·
                        #      按住了【D】 ⚠ 角色没动？ · 本段 0 像素」这一长串里。
                        #    后果很重：这一段实际是**0 像素的废线**，录完挂机就表现为
                        #    "移动范围远小于录制范围" → 卡死，而用户完全不知道线是空的。
                        #    根因多半是游戏窗口不是前台（按键没送进游戏，角色没走）。
                        progress += ("\n\n⚠️ 按了键但角色没动、本段 0 像素 —— "
                                     "这一段是废线，存了也没用！\n"
                                     "　　多半是游戏窗口不是前台（按键没送进游戏）。\n"
                                     "　　请点一下游戏窗口，确认人物真的在走，再继续录。")
                progress += f" · 本段 {drawn} 像素"
            self.lbl_hint.setText(hint)
            self.lbl_progress.setText(progress)
            # 一段都还没存时不显示【主线录完了】（这时说"录完了"是误导）
            extra = [self.btn_mainline_done] if routes else []
            self._show_only(
                [self.btn_waypoint, self.btn_finish],
                extra + secondary + [self.btn_quit])

        elif phase == "ask_home":
            self._set_step_text(f"主线录完了（{routes} 段）")
            self.lbl_hint.setText(
                "要顺手录一条回正线吗？\n\n"
                "回正线 = 掉出平台之后走回主线的路。\n"
                "不录也能跑，但掉出去可能回不来。")
            self.lbl_progress.setText(f"已存 {routes} 段")
            self._show_only([self.btn_want_home], [self.btn_skip_home] + secondary)

        elif phase == "home":
            if home == 1:
                self._set_step_text("回正线 · 第 2 步")
                self.lbl_hint.setText(
                    "现在一路走回主线。\n\n"
                    "到了点【已回到主线】—— 点了就自动保存。")
                self.lbl_progress.setText("回正线：录制中")
                self._show_only([self.btn_home_end],
                                [self.btn_home_cancel] + secondary + [self.btn_quit])
            else:
                self._set_step_text("回正线 · 第 1 步")
                self.lbl_hint.setText(
                    "先自己走下去（或掉下去），站到落点上。\n\n"
                    "⚠️ 不是平台边缘 —— 起点画在边缘的话，真掉下去接不上。\n\n"
                    "站好点【我在落地点】。")
                self.lbl_progress.setText("回正线：还没起头")
                self._show_only([self.btn_home_start], secondary + [self.btn_quit])

        else:  # home_done
            self._set_step_text("回正线已存")
            self.lbl_hint.setText(
                "已存进 route_home.png。\n\n"
                "还有别的地方会掉下去 →【我再补一条】\n"
                "没有了 →【结束录制】（地图会自动保存）。")
            self.lbl_progress.setText("回正线：已存")
            self._show_only([self.btn_home_again], [self.btn_quit] + secondary)

        if paused:
            self._set_step_text("已暂停 · " + self.lbl_step.text())
            self.lbl_hint.setText("当前是暂停状态，路线不会记。\n" + self.lbl_hint.text())

    def set_map_name(self, map_id):
        self._map_id = map_id
        if self.append_mode:
            prefix = "补录回正线"
        else:
            prefix = "录制 · 巡逻"
        self.setWindowTitle(f"{prefix} — {map_id}")

    def force_close(self):
        """程序主动收面板（录制已结束 / 启动失败）—— 不再问用户。"""
        self._on_close = None
        self.close()

    def closeEvent(self, event):
        """点 X 关面板 = 想结束录制：先问一句（由主界面弹窗），用户说继续就不关。"""
        if self._on_close is not None and not self._on_close():
            event.ignore()
            return
        event.accept()


class MainWindow(QMainWindow):
    '''
    MainWindow
    '''
    request_close = Signal()
    # 游戏内热键信号：pynput 线程只能发信号，Qt 控件操作必须回主线程执行
    hotkey_f1 = Signal()
    hotkey_f2 = Signal()
    hotkey_f3 = Signal()
    hotkey_f4 = Signal()

    # 录制器多久没输出就判定它已死（秒）—— 见 _recorder_alive 的说明。
    # 取 20s 的理由：录制器正常时至少每几秒会有一行日志（抓帧/存段/体检），
    # 20s 没有任何输出基本可以断定它卡死或被强杀了；同时不至于把
    # "正在等用户操作"的短暂安静误判成死亡。
    _RECORDER_STALE_SEC = 20.0

    def __init__(self, controller=None):
        super().__init__()

        # UI window Icon
        self.setWindowIcon(QIcon(resource_path("media/icon.png")))

        self.controller = controller # autoBotController
        #
        self.selected_map = ""

        # Load default yaml as base config
        _, self.comments, self.comments_section = load_yaml_with_comments("config/config_default.yaml")
        # 参数说明/参考值的**权威来源**（2026-10-14 加）：按行读原文，
        # 说明 = 紧贴该键上方的那段 # 注释。
        # ⚠️ 不用 self.comments —— 那是 ruamel 的 ca.items[key][2]（键**下方**那段），
        #    在本仓库的注释写法下会整体错位一格（每个键显示的是下一个键的说明）。
        #    详见 src/utils/cfg_docs.py 的模块注释与 tools/verify_ui_tabs.py 的三条断言。
        self.field_docs, self.field_docs_section = load_field_docs("config/config_default.yaml")
        self.cfg_base = load_yaml("config/config_default.yaml")
        self.cfg = copy.deepcopy(self.cfg_base)
        self.path_cfg_custom = "config/config_custom.yaml" # Custom config path

        # Load database
        self.data = load_yaml("config/config_data.yaml")

        # Window Settings
        self.setWindowTitle("冒险岛怀旧服 - 自动练级")
        self.setMinimumSize(1, 1)
        self.resize(TAB_WINDOW_SIZE['主界面'][0],
                    TAB_WINDOW_SIZE['主界面'][1])

        # Setup tabs —— 页签布局（2026-10-14 用户重排）：
        #   主界面   = 开跑要用的旋钮（控制/攻击/怪物识别/按键/拾取/宠物/地图/喝药）
        #   调试     = 所有「看」的东西集中一页：游戏画面 / 路线地图 / 示范录制 / 日志
        #   高级设置 = 参数全表（识别与出手 / 路线·梯子与走丢 / 名字定位）+ 说明与参考值
        #   工具箱   = 验证 / 登记 / 诊断
        #
        # 【为什么把示范录制与日志从主界面搬走】主界面是"开跑"页：以前日志窗吊在
        #   最下面，把 700×800 的窗口撑满，想点「开始」得先往下滚；而它们在语义上
        #   都属于"看数据 / 排查"。搬到调试页后主界面只剩要填要点的东西。
        self.tabs = QTabWidget()
        self.advance_settings_gboxes = {}
        self.tab_main = self.setup_main_tab()
        self.tab_debug = self.setup_debug_tab()
        self.tab_advance_setting = self.setup_advance_setting_tab()
        self.tab_toolbox = self.setup_toolbox_tab()

        # Add tabs to tab widget
        self.tabs.addTab(self.tab_main, "主界面")
        self.tabs.addTab(self.tab_debug, "调试")
        self.tabs.addTab(self.tab_advance_setting, "高级设置")
        self.tabs.addTab(self.tab_toolbox, "工具箱")

        # Change tabs signals
        self.tabs.currentChanged.connect(self.on_tab_changed)

        # Put tab widget to layout
        layout = QVBoxLayout()
        layout.addWidget(self.tabs)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Load previous stored UI state
        self.load_ui_state()

        # ⚠️⚠️ 2026-09-26 关键修复：启动时必须把配置**填进界面控件**，否则自动保存
        #       会把空控件当成用户设置、写出「按键全空」的配置。
        #
        #   【曾经的 bug】__init__ 里从不调 apply_config_to_ui，控件创建出来一直是空的；
        #   而下面的 _autosave_timer 每 3 秒 tick 一次 → _flush_settings →
        #   update_cfg_from_main_ui → 无条件执行
        #       self.cfg["key"]["jump"] = self.jump_key.get_key()   # 空控件 → ''
        #   于是 self.cfg 被写空，get_cfg_diff(默认值, 被写空的 cfg) 算出「几个空值」
        #   这个差异，落盘成 config_custom.yaml：
        #       key: {aoe_skill: '', jump: '', return_home: '', teleport: ''}
        #
        #   【危害】静默。用户什么都没做，只是「打开界面等 3 秒」，配置就被写坏，
        #   症状是「按键不工作」，且没有任何报错。2026-09-26 实测复现：
        #   全新环境（无任何自定义方案）打开界面 10 秒，config_custom.yaml 自动生成
        #   且内容为上述空值；源码运行与打包运行**都会中招**。
        #
        #   【为什么以前没发现】本地存在自定义方案文件，某些路径（load_config 等）
        #   曾把控件填上值，掩盖了这个问题；任何"干净环境首次启动"（＝每个新用户）
        #   必然踩到。
        #
        #   【为什么放在这里】必须在 load_ui_state() 之后 —— 它可能恢复"上次用的方案"
        #   并据此改 self.cfg，先填控件就会填成过期的值；又必须在启动定时器之前，
        #   否则定时器仍可能抢在填值前跑一次。
        self.apply_config_to_ui()

        # 配置方案：填充下拉列表 + 自动保存定时器（3 秒去抖，设置有变化才写盘）
        self._refresh_config_combo()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave_tick)
        self._autosave_timer.start(3000)

        # 地图列表自动扫描：录完路线/自动登记后无需重启界面，5 秒内新图自动出现
        self._map_scan_timer = QTimer(self)
        self._map_scan_timer.timeout.connect(self.refresh_map_list)
        self._map_scan_timer.start(5000)

        # 自动喝药面板刷新（2026-09-27 加）：环境提示 + 实时血蓝读数。
        # 为什么用定时器而不是只在开始时刷一次：
        #   ① 环境（窗口大小 / 缩放）随时可能被用户改掉；
        #   ② 挂机中的血蓝百分比要能实时看到 —— 不然"到底有没有在工作"
        #      只能靠翻日志，而血蓝监控失效时的表现恰恰是"什么都不发生"。
        self._potion_timer = QTimer(self)
        self._potion_timer.timeout.connect(self._refresh_potion_live)
        self._potion_timer.start(2000)

        # Signal
        # ★ 2026-10-14：原来这里直接 `connect(self.close)`。F12 走的是 pynput
        #   **全局**键盘监听（src/input/KeyBoardListener.py:81-83；on_press 认 F 键时
        #   **不看前台窗口**）⇒ 在任何窗口误按一下 F12，整个界面立刻关掉、挂机停止，
        #   而且**一行日志都不打**（日志尾部只剩 closeEvent 那 6 行）。
        #   用户真机现场：2026-10-14 03:40:48 就是这么退的（日志里既没有
        #   [Attack Timeout] 也没有 run_once 异常 ⇒ 不是引擎自杀）。
        #   现改成：先记日志 → 弹确认框（默认「取消」）→ 才关。见 _hotkey_f12_guarded。
        self.request_close.connect(self._hotkey_f12_guarded)
        # 游戏内 F1~F4 热键 → 主界面功能（信号回主线程，pynput 线程只 emit）
        # 【冲突互斥】录制器占用 F1 暂停录制 / F2 截图 / F3 存路线 / F4 存地图，
        # 与主界面同名 —— 录制器运行期间主界面这四个热键**全部挂起**，退出后自动恢复。
        # 2026-09-12 用户报告两边 F1~F4 重叠：上一版只挂了 F2/F3/F4，**F1 漏了**。
        self._recorder_proc = None
        # 录制时的置顶分步面板（纯鼠标操作，见 RecorderPanel）
        self._recorder_panel = None
        self._last_recorder_routes = 0
        self._last_recorder_home = 0    # 回正线状态：0 没录 / 1 正在录 / 2 已存
        self.hotkey_f1.connect(self._hotkey_f1_guarded)
        self.hotkey_f2.connect(self._hotkey_f2_guarded)
        self.hotkey_f3.connect(self._hotkey_f3_guarded)
        self.hotkey_f4.connect(self._hotkey_f4_guarded)
        # ── 录制器"存活兜底"两个时间戳（见 _recorder_alive，2026-09-27 加）──
        # _recorder_started_t / _recorder_last_output_t：前者在启动录制器时置位，
        # 后者每次收到录制器输出时刷新；两者都超过 _RECORDER_STALE_SEC 秒没有任何
        # 动静，就认为录制器已死（防它被强杀/卡死后永久吞掉主界面 F1~F4 热键）。
        self._recorder_started_t = None
        self._recorder_last_output_t = None

    def refresh_map_list(self):
        """重新扫描 minimaps/ 与登记表，刷新地图列表（2026-09-12 用户需求）。
        录完路线/自动登记后**无需重启界面**：后台定时器每 5 秒调一次，
        列表有变化才重建，并保持当前选中的地图。登记表从磁盘重读
        （录制器是独立进程写盘的，主界面内存里的旧登记表看不到新图）。"""
        import yaml as _yaml
        minimap_dir = resource_path("minimaps")
        if not os.path.isdir(minimap_dir):
            os.makedirs(minimap_dir, exist_ok=True)
        try:
            with open("config/config_data.yaml", "r", encoding="utf-8") as f:
                self.data = _yaml.safe_load(f) or {}   # 静默重读登记表（录路线进程可能刚写过）
        except Exception:
            pass   # 读失败（如正在写盘）就用上一次的登记表，下个周期再试

        new_items = []   # (显示文本, 地图名)
        for name in os.listdir(minimap_dir):
            if name.startswith(".") or name.startswith("_"):
                continue

            # 显示条件（2026-09-12 放宽支持中文地图ID）：
            #   · eng_to_cn 登记了 → 显示 "id (中文名)"
            #   · 没登记 eng_to_cn 但在 map_mobs_mapping 里（中文 ID 常见形态）
            #     → 直接显示 ID 本身，不再强迫用户登记两遍
            # ⚠️ 2026-10-13（真机事故：**界面根本打不开**）：登记表里出现**裸头部**
            #    （`map_mobs_mapping:` 后面一行都没有）时，yaml 解析出来是 None，
            #    `name in None` 直接抛 `TypeError: argument of type 'NoneType'
            #    is not iterable`。而本函数在启动链上（__init__ → setup_main_tab →
            #    create_map_selection_gbox → refresh_map_list）⇒ 窗口还没显示就死。
            #    触发条件：删掉最后一张已登记地图（写侧见 utils/registry.py 的
            #    _drop_line_in_block，已同步修好）。这里 `or {}` 兜底，双保险：
            #    即使登记表被手改坏，界面也照常打开（那张图只是不显示）。
            _eng_to_cn = self.data.get("eng_to_cn") or {}
            _map_mobs = self.data.get("map_mobs_mapping") or {}
            if name in _eng_to_cn:
                display_text = f"{name} ({_eng_to_cn[name]})"
            elif name in _map_mobs:
                display_text = name
            else:
                continue  # 两处都没登记 = 不是给挂机用的地图
            if os.path.isdir(os.path.join(minimap_dir, name)):
                new_items.append((display_text, name))

        # 与当前列表比对，有变化才重建（避免闪烁、保持选中）
        current = [(self.list_widget_maps.item(i).text(),
                    self.list_widget_maps.item(i).data(Qt.UserRole))
                   for i in range(self.list_widget_maps.count())]
        if new_items == current:
            return False
        self.list_widget_maps.clear()
        for display_text, name in new_items:
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, name)
            self.list_widget_maps.addItem(item)
        if self.selected_map:
            for i in range(self.list_widget_maps.count()):
                if self.list_widget_maps.item(i).data(Qt.UserRole) == self.selected_map:
                    self.list_widget_maps.setCurrentRow(i)
                    break
        return True

    def setup_main_tab(self):
        '''
        Init Main Tab with scrollable area
        '''
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)

        # 🎥 示范录制「交接中」横幅（2026-10-14 加）——
        #    交接模式（demo_record.record_only=true）会把键盘静音：机器人一个键都不发。
        #    原来只在日志里写一句，而日志区已经挪到「调试」页 ⇒ 主界面根本看不到，
        #    症状又回到"点了开始，角色一动不动"（静默失败）。
        #    所以主界面必须有一条**常驻**提示，且由 _refresh_demo_banner() 按真实状态自愈。
        self.demo_banner = QLabel()
        self.demo_banner.setWordWrap(True)
        self.demo_banner.setStyleSheet(
            "background-color:#3a2f00; color:#ffd76a; border:1px solid #8a7000;"
            "padding:6px; font-size:12px;")
        self.demo_banner.setVisible(False)
        scroll_layout.addWidget(self.demo_banner)

        # Control group box
        self.control_gbox = self.create_control_gbox()
        scroll_layout.addWidget(self.control_gbox)

        # Attack setting group box
        self.attack_gbox = self.create_attack_gbox()
        scroll_layout.addWidget(self.attack_gbox)

        # 🐲 怪物识别（2026-10-13 加）—— 紧跟攻击设置，同为「打怪」类旋钮
        self.monster_gbox = self.create_monster_gbox()
        scroll_layout.addWidget(self.monster_gbox)

        # Key bindings group box
        self.key_binding_gbox = self.create_key_binding_gbox()
        scroll_layout.addWidget(self.key_binding_gbox)

        # 🖱️ 自动拾取（2026-10-01 加）—— 紧跟按键绑定，同为「配键位」类
        self.pickup_gbox = self.create_pickup_gbox()
        scroll_layout.addWidget(self.pickup_gbox)

        # Pet function group box
        self.buff_skill_gbox = self.create_buff_skill_gbox()
        scroll_layout.addWidget(self.buff_skill_gbox)

        # Map selection group box
        self.map_selection_gbox = self.create_map_selection_gbox()
        scroll_layout.addWidget(self.map_selection_gbox)

        # 🧪 自动喝药（2026-09-27 加）
        self.potion_gbox = self.create_potion_gbox()
        scroll_layout.addWidget(self.potion_gbox)

        # 🎥 示范录制 与 📜 日志：2026-10-14 搬到「调试」页（见 setup_debug_tab）——
        #   它们在语义上属于"看数据 / 排查"，不该占着开跑页最下面那块地方。
        scroll_area.setWidget(scroll_widget)

        tab_main = QWidget()
        layout = QVBoxLayout(tab_main)
        layout.addWidget(scroll_area)

        return tab_main

    def setup_debug_tab(self):
        '''
        🐞 调试页（2026-10-14 用户重排）

        用户指定的排布：**从上到下叠四个区域** ——
          ① 游戏画面（实时截图 + 检测框）   初始 526×216
          ② 路线地图（当前段 / 回正线 / 光标）初始 526×216
          ③ 示范录制
          ④ 日志                             可以长一点

        【为什么不做二级页签】用户明确要求"从上到下叠"；四块都用 QScrollArea 兜底，
        窗口不够高时滚动即可。
        '''
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(10)

        # ① 游戏画面
        self.debug_canvas = QLabel()
        clear_debug_canvas(self.debug_canvas)
        self.debug_canvas.setStyleSheet("background-color: black; color: white;")
        self.debug_canvas.setFixedSize(DEBUG_CANVAS_SIZE[0], DEBUG_CANVAS_SIZE[1])
        layout.addWidget(self._wrap_debug_area("🖥️ 游戏画面（实时截图 + 检测框）",
                                               self.debug_canvas))

        # ② 路线地图
        self.route_map_canvas = QLabel()
        clear_debug_canvas(self.route_map_canvas)
        self.route_map_canvas.setStyleSheet("background-color: black; color: white;")
        self.route_map_canvas.setFixedSize(DEBUG_CANVAS_SIZE[0], DEBUG_CANVAS_SIZE[1])
        layout.addWidget(self._wrap_debug_area("🗺️ 路线地图（当前段 / 回正线 / 光标）",
                                               self.route_map_canvas))

        # ③ 示范录制（你玩，它录）
        self.demo_gbox = self.create_demo_gbox()
        layout.addWidget(self.demo_gbox)

        # ④ 日志
        self.log_gbox = self.create_log_gbox()
        layout.addWidget(self.log_gbox)

        layout.addStretch(1)
        scroll_area.setWidget(inner)

        tab_debug = QWidget()
        outer = QVBoxLayout(tab_debug)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll_area)

        return tab_debug

    def _wrap_debug_area(self, title, canvas):
        '''把一块画布包成带标题的组框（调试页里用）'''
        gbox = QGroupBox(title)
        box_layout = QVBoxLayout(gbox)
        box_layout.setContentsMargins(6, 6, 6, 6)
        box_layout.addWidget(canvas)
        return gbox

    def setup_advance_setting_tab(self):
        '''
        ⚙️ 高级设置页（2026-10-14 恢复并改版）

        用户要求：把「识别与出手 / 路线·梯子与「走丢」/ 名字定位」这些参数的设置
        放到高级设置页里，**并给出说明和数值参考**。

        结构 = 三个桶（ADV_TAB_GROUPS）+ 每个桶里放对应配置段 + 每个参数一行
        「标签/控件 + 灰字说明 + 参考值」。

        页顶的「显示内部参数」勾选框：默认只显示日常旋钮；勾上才把
        ADV_FIELD_HIDE 里那些"改错会把挂机搞坏"的底层参数（rope_nudge_*、
        home_* 回正阈值、色码表…）摆出来 —— 它们一直生效，只是默认收起来。
        '''
        tab_advance_setting = QWidget()
        outer = QVBoxLayout(tab_advance_setting)

        self.internal_params_cb = QCheckBox(
            "显示内部参数（⚠️ 算法阈值，改错会让挂机行为异常；不确定就别动）")
        self.internal_params_cb.setToolTip(
            "默认只显示你需要调的旋钮。\n"
            "勾上之后，各组里会多出底层参数（绳梯自救、回正线阈值、色码表、名字定位\n"
            "的内部阈值…）。它们一直在生效 —— 只是改之前最好先问一下 AI。")
        self.internal_params_cb.setChecked(False)
        self.internal_params_cb.stateChanged.connect(self._on_internal_params_toggled)
        outer.addWidget(self.internal_params_cb)

        self.advance_settings_gboxes = {}  # section -> QGroupBox（set_gbox_enabled 等仍遍历它）

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        container = QWidget()
        container_layout = QVBoxLayout(container)

        for group_title, sections in ADV_TAB_GROUPS:
            bucket = QGroupBox(group_title)
            bucket_layout = QVBoxLayout(bucket)
            for section in sections:
                if section not in self.cfg:
                    logger.error(f"[界面] 高级设置：配置里没有 {section} 段 —— 这一桶先放着")
                    continue
                gbox = create_advance_setting_gbox(
                    section, self.cfg, self.comments, self.comments_section,
                    hide_keys=ADV_FIELD_HIDE.get(section, ()),
                    field_docs=self.field_docs,
                    show_internal=self.internal_params_cb.isChecked())
                self.advance_settings_gboxes[section] = gbox
                bucket_layout.addWidget(gbox)
            container_layout.addWidget(bucket)

        container_layout.addStretch(1)
        scroll_area.setWidget(container)

        outer.addWidget(scroll_area)
        return tab_advance_setting

    def _on_internal_params_toggled(self, state):
        '''「显示内部参数」勾选框：把底层参数的行显示/隐藏（只影响显示，不动配置）'''
        show = Qt.CheckState(state) == Qt.Checked
        for gbox in self.advance_settings_gboxes.values():
            set_gbox_internal_visible(gbox, show)
        logger.info(f"[界面] 高级设置：内部参数{'显示' if show else '隐藏'}")

    def setup_game_window_viz_tab(self):
        tab_game_window_viz = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)  # Remove margins
        tab_game_window_viz.setLayout(layout)

        # Create a large QLabel as a canvas
        self.debug_canvas = QLabel()
        self.debug_canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        clear_debug_canvas(self.debug_canvas)
        self.debug_canvas.setStyleSheet("background-color: black; color: white;")
        layout.addWidget(self.debug_canvas)

        return tab_game_window_viz

    def setup_route_map_viz_tab(self):
        tab_route_map_viz_tab = QWidget()
        layout = QVBoxLayout()
        tab_route_map_viz_tab.setLayout(layout)

        # Create a large QLabel as a canvas
        self.route_map_canvas = QLabel()
        clear_debug_canvas(self.route_map_canvas)
        self.route_map_canvas.setStyleSheet("background-color: black; color: white;")
        layout.addWidget(self.route_map_canvas)

        return tab_route_map_viz_tab

    # ------------------------------------------------------------------
    # 工具箱页签（2026-09-12）：只放「验证 / 登记 / 诊断」三类辅助功能。
    # 资源制作三连（标定名字 / 截怪物模板 / 录路线）在主界面 F2/F3/F4。
    # 各工具以独立控制台窗口运行（有自己的交互/输出），复用 tools/ 现有实现。
    # ------------------------------------------------------------------
    # ⚠️ 2026-09-26：`_REPO_ROOT` 已废弃，**不要再用**。
    #    旧定义是「从本文件往上退三层」＝只对源码模式成立；打包后 __file__ 位于
    #    exe 旁边的 _internal/ 里，退三层会算成 _internal/ 本身 ⇒ 资源全部找不到
    #    （症状：点 F3 报 FileNotFoundError: ...\_internal\monster）。
    #    一律改用 src.utils.paths.APP_ROOT（打包＝exe 所在目录，源码＝仓库根）。
    #    下面的 property 会在被访问时立刻抛错，防止有人再写出静默算错的代码。
    @property
    def _REPO_ROOT(self):
        raise RuntimeError(
            "_REPO_ROOT 已废弃（打包后算错）。请改用 src.utils.paths.APP_ROOT。")

    def _is_admin(self):
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _console_python(self):
        """控制台版 python.exe 的确定路径。
        【关键】GUI 若以 pythonw.exe（无控制台版）运行（用快捷方式/直接双击 pyw
        启动时常见），sys.executable 就是 pythonw —— 用它开 CREATE_NEW_CONSOLE
        窗口不会有任何窗口显示、input() 立即 EOF，工具秒退 = 用户看到「点了没反应」
        （2026-09-12 用户实测）。同目录找 python.exe 换掉，找不到再退回环境变量
        指向的 Python（PYTHON_HOME / PATH 里第一个可用的）。"""
        py = sys.executable or ''
        if os.path.basename(py).lower() in ('pythonw.exe', 'pythonw'):
            candidate = os.path.join(os.path.dirname(py), 'python.exe')
            if os.path.exists(candidate):
                return candidate
            for env_name in ('PYTHON_HOME', 'CONDA_PREFIX'):
                home = os.environ.get(env_name)
                if home:
                    candidate = os.path.join(home, 'python.exe')
                    if os.path.exists(candidate):
                        return candidate
        return py

    def _tool_argv(self, module, *extra):
        """构造「启动 tools/ 下某个子工具」的命令行参数。

        源码运行 → `['-u', '-m', 'tools.xxx', ...]`（交给 python）
        打包运行 → `['--tool', 'tools.xxx', ...]`（交给**本 exe 自己**，
                    见 src/main.py 的 _run_tool —— exe 不认 `-m`，必须走这条）

        界面上另起进程跑的工具都在用这个：F2 标定名字 / F3 截怪 / F4 录路线 /
        工具箱的「诊断包」「模板查重」。
        """
        rest = [str(x) for x in extra]
        if IS_FROZEN:
            return ['--tool', module] + rest
        return ['-u', '-m', module] + rest

    def _spawn_console(self, module_args, pause=True):
        """在新控制台窗口里运行 tools/ 下的模块。
        pause 参数已废弃（保留兼容）：窗口跑完停住的行为由各工具自己结束时
        input 等待实现 —— 不经 cmd.exe（cmd /c 的引号剥离会毁掉带引号的
        python 路径与参数，2026-09-12 用户实测：报「不是内部或外部命令」）。"""
        py = self._console_python()
        if isinstance(module_args, str):   # 兼容传字符串的调用点（验证类按钮）
            module_args = [module_args]
        # ⚠️ 打包后走 --tool 自调用（exe 不认 -m，见 _tool_argv）
        argv = self._tool_argv(module_args[0], *module_args[1:])
        # ⚠️ cwd 必须是**应用根目录**而不是源码仓库根：打包后 __file__ 在
        #    exe 旁边的 _internal 里，_REPO_ROOT 会算错，资源就找不到了。
        try:
            subprocess.Popen([py] + argv,
                             cwd=APP_ROOT,
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception as e:
            # 启动失败不能再静默（用户只会看到「没反应」）
            QMessageBox.warning(self, "工具启动失败", f"无法启动 {module_args}:\n{e}")

    def _require_admin_for_hotkey_tools(self):
        """录路线要接收游戏内按键 —— 非管理员会被 Windows 静默屏蔽（§8.5）。"""
        if not self._is_admin():
            # ⚠️ 提示语必须按**打包方式**给不同说法（2026-09-27 修）：
            #    免安装包里**没有** 启动界面.bat（打包产物只有 exe 和几个
            #    资源目录），照着旧提示去找 bat 会找不到，用户就卡在这。
            how = ("右键「冒险岛自动练级.exe」→ 以管理员身份运行"
                   if IS_FROZEN else
                   "右键「启动界面.bat」→ 以管理员身份运行")
            QMessageBox.warning(
                self, "需要管理员权限",
                "游戏客户端以管理员权限运行，普通权限程序收不到游戏里按的按键。\n\n"
                f"请先关闭本程序，然后{how}，再重试。")
            return False
        return True

    def _launch_diagnose(self):
        self._spawn_console('tools.diagnose', pause=True)

    def _launch_diagnose_nopic(self):
        '''不含游戏画面的诊断包（见工具箱里那个按钮的说明）。

        `--frames 0` 让 tools/diagnose.py 跳过现场抓帧、也不收 debug/ 里的旧帧，
        只收「日志 + 配置 + 名字/怪物模板」。
        '''
        self._spawn_console(['tools.diagnose', '--frames', '0'], pause=True)

    def _launch_mob_template_qa(self):
        self._spawn_console('tools.mob_template_qa', pause=True)

    def setup_toolbox_tab(self):
        tab_toolbox = QWidget()
        layout = QVBoxLayout()
        tab_toolbox.setLayout(layout)

        tip = QLabel(
            "资源制作三连（标名字 / 截怪 / 录路线）在主界面 F2 / F3 / F4。\n"
            "先是完整流程教程，再往下是体检与诊断 —— 平时挂机哪都不用点。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        # ---- 教程（2026-09-17 重写）----------------------------------------
        # ⚠️ 旧教程写的是「去程按 F3、回程再按 F3，两段才能循环」——**错的**：
        #    录制器里 F3 = 「存这段（存成 route{N}.png）」，**F8 才是折返分段**。
        #    照旧教程做，用户会在去程终点就存盘，录出一条只有一段的线（不会往返）。
        #    现在改成以**面板按钮**为主（录制面板 2026-09-15 重写过，按钮名就是步骤），
        #    快捷键只在括号里注明，不再当主要指引。
        # ------------------------------------------------------------------
        gbox_guide = QGroupBox("换一张新图挂机 —— 照着做 5 步")
        glay = QVBoxLayout(gbox_guide)
        # ⚠️ 2026-09-26 重排步骤顺序（旧版把 F3 截怪放在 F4 录路线之前 —— **错的**）。
        #    原因：F3 在代码里有硬前置 `if not self.selected_map: return`（见
        #    launch_template_capture），必须先选中地图才能截怪；而地图是 F4 录路线
        #    时才创建并登记到 config_data.yaml 的。出厂 config_data.yaml 是空表
        #    （eng_to_cn: {} / map_mobs_mapping: {}），所以新用户照旧顺序做，会在
        #    第 ② 步撞上「请先选地图」且列表全空 —— 卡死且不知道去哪弄地图。
        #    ⇒ 正确顺序：标名字 → 录路线(产生地图) → 选地图 → 截怪 → 开始。
        guide = QLabel(
            "<b>① F2 标定名字</b>　<font color=gray>只有换角色才要做</font><br>"
            "　先在弹出的输入框里填你角色的名字（和游戏里一模一样），点确定；<br>"
            "　然后在游戏画面上<b>框住角色的名字行</b>，再<b>点一下角色中心</b>。"
            "不标的话角色定位会失效。<br><br>"

            "<b>② F4 录挂机路线</b>　<font color=gray>要管理员运行本程序，"
            "否则收不到游戏里的按键</font><br>"
            "　<b>这一步会创建地图</b> —— 新图必须先录路线，地图列表里才会有它。<br>"
            "　弹出来的录制面板，<b>全程用鼠标点按钮</b>就够了：<br>"
            "　· 站到想当起点的位置 → 点【开始录（我站好了）】<br>"
            "　· 一路走到平台另一头 → 点【在这折返，存下这段】<br>"
            "　· 再走回起点附近 → 点【这是终点，保存这段】<br>"
            "　· 点【主线录完了，下一步】<br>"
            "　⚠️ <b>去程和回程必须分成两段</b>：挤在同一段的话，同一个位置既有向左"
            "又有向右的路线像素，角色会一路冲出平台。<br><br>"

            "<b>③ 选地图</b><br>"
            "　录完路线关闭界面、重新打开，地图就会出现在下方列表里 —— 点选它。"
            "（路线会自动登记，不用手动加。）<br><br>"

            "<b>④ F3 截怪物模板</b>　<font color=gray>换图才做，"
            "且必须在选好地图之后</font><br>"
            "　框住一只怪自动抠图。<b>一只怪通常 2 个模板就够</b> —— 挑姿态差别大的"
            "两种截（比如站立 / 攻击），<b>相似的就别重复截了</b>。<br>"
            "　· 游戏是横版，怪只有左右两个朝向，而引擎会<b>自动把每张模板左右镜像</b>，"
            "所以一张顶两张用，不用自己截反面。<br>"
            "　· 感觉<b>识别不稳</b>（漏怪 / 打空）就多截几个姿态；<br>"
            "　· 感觉<b>帧率变低</b>（模板越多每帧匹配越慢）就去下面「模板查重」清一遍。<br>"
            "　<b>以前截过的怪直接复用</b> —— 主界面选中地图 →「🐾 打哪几种怪」勾上就行，"
            "不用重截。<br><br>"

            "<b>⑤ ▶ 开始 (F1)</b>　<font color=gray>建议先画回正线再挂机</font><br>"
            "　路线和怪物都会<b>自动登记</b>。<br><br>"

            "<b>🖊 手绘回正线</b>　<font color=gray>「掉下去能自己走回来」的关键，"
            "强烈建议做</font><br>"
            "　回正线 = 掉出平台后怎么走回主线。选好地图后随时可做。<br>"
            "　推荐：主界面选中地图 → 点【🖊 手绘回正线】—— <b>不用开游戏、"
            "不用真的掉一次</b>，在小地图上先点落点、再点回到主线的路就行。<br>"
            "　也可以在录制时现场录：【录回正线】→ 自己走到坑里 →【我在落地点】"
            "→ 走回主线 →【已回到主线】。<br>"
            "　一张图可以有好几条 —— 有几个会掉的坑就画几条。<br><br>"

            "<b>出问题怎么办</b><br>"
            "· <b>按 F2/F3/F4 没反应</b>：多半是没以管理员身份运行 —— 关掉程序，"
            "右键「启动界面.bat」→ 以管理员身份运行。<br>"
            "· <b>掉下去回不来</b>：多半是没画回正线，或落点画错了 —— "
            "用【🖊 手绘回正线】补一条。<br>"
            "· <b>挂机变卡 / 帧率低</b>：下面「模板查重」看一眼，删掉重复的模板是"
            "零损失的。<br>"
            "· <b>角色不动 / 乱走 / 按键没反应</b>：点下面「保存诊断包」，"
            "把弹出的整个目录发给 AI。")
        guide.setWordWrap(True)
        guide.setTextFormat(Qt.RichText)
        glay.addWidget(guide)
        layout.addWidget(gbox_guide)

        # ---- 核对（2026-09-15 **撤掉**，别再装回来）----------------------
        # 这里原来有三个按钮：核对1 验证名字定位 / 核对2 验证定位轨迹 / 核对3 验证怪物模板。
        # 撤掉的理由（用户 2026-09-15 裁定）：
        #   ① 三者输出的都是**技术报告**（score 最小/中位/最大、分离度倍数、离阈值还剩多少），
        #      终端用户看了也不知道该怎么办；
        #   ② 三者都要求先有 debug/capture_raw_*.png 抓帧，而本页**没有抓帧按钮**
        #      → 用户点下去只会看到「没有可用的抓帧」，然后无从下手；
        #   ③ verify_nametag 自己还写着「未命中的帧……**请自行核对**」—— 最后一步判断它给不了。
        # ⇒ 改为：出问题一律走「保存诊断包」。包里已带**游戏画面 + 名字/怪物模板**
        #   （tools/diagnose.py，2026-09-15 同日升级），收件人用同一批 tools/ 离线复查。
        #   ⚠️ tools/verify_nametag.py / verify_nametag_track.py / verify_monster.py **保留**
        #      （开发者侧工具，只是不再给终端用户当按钮）。
        # ------------------------------------------------------------------

        # ---- 模板体检（2026-09-15）----
        # 引擎对**每张** PNG 都会生成「原图 + 左右镜像」两个模板，所以
        # 每次怪检测的匹配次数 = 张数 × 2 —— 多一张模板就是每次检测多跑两次
        # 全 ROI 的 matchTemplate。而"多出来的"往往只是同一个姿势重截了一遍。
        gbox_tpl = QGroupBox("模板体检（挂机卡了/识别不稳时可查）")
        tpl_layout = QVBoxLayout(gbox_tpl)
        # 2026-09-26 补说明：原先只说「多得会变慢」，没讲清「一张顶两张」这个
        # 关键前提，用户容易重复截同一个姿态（纯浪费，还拖慢每帧检测）。
        tpl_tip = QLabel(
            "<font color=gray>"
            "引擎加载每张模板时会<b>自动左右镜像</b>，所以每张实际参与 <b>2 次</b>匹配"
            "（怪朝左朝右都能认）。一只怪一般 <b>2 个姿态差异较大的模板</b>就够了；"
            "感觉识别不稳就多截几个，感觉卡顿就去下面清一遍重复的。"
            "</font>")
        tpl_tip.setWordWrap(True)
        tpl_layout.addWidget(tpl_tip)
        btn_tpl = QPushButton(
            "模板查重 —— 列出相似度过高的模板（只报告不删，删哪张你定）")
        btn_tpl.clicked.connect(self._launch_mob_template_qa)
        tpl_layout.addWidget(btn_tpl)
        layout.addWidget(gbox_tpl)

        # ---- 诊断 ----
        gbox_diag = QGroupBox("诊断（挂机出问题时用）")
        diag_layout = QVBoxLayout(gbox_diag)
        btn_diag = QPushButton(
            "保存诊断包 —— 一键收集：报错日志 / 配置 / 游戏画面 3 张 / 名字与怪物模板，\n"
            "整个目录发给 AI 排查")
        btn_diag.clicked.connect(self._launch_diagnose)
        diag_layout.addWidget(btn_diag)
        # ★ 2026-09-28 加：**不含画面**的诊断包。
        #   为什么需要：诊断包默认会把**游戏画面**收进去，而画面里有角色名 ——
        #   有些用户不愿意把自己的角色名/画面发出来（issue #6 那位明确说了
        #   「无法提供涉及角色名的截图」）。原来只能靠
        #   `python -m tools.diagnose --frames 0` 这种命令行做法，但**普通用户
        #   不会敲命令**（也不该为了反馈问题去学命令行）⇒ 给一个按钮。
        #   ⚠️ 代价写在按钮上：没有画面，收件人只能靠模板推断"名牌有没有被挡住"，
        #      定位会慢一些 —— 让用户自己权衡，不替他决定。
        btn_diag_nopic = QPushButton(
            "保存诊断包（不含游戏画面）—— 只收日志 / 配置 / 模板\n"
            "适合不方便把游戏画面发出来的情况（定位会慢一些）")
        btn_diag_nopic.clicked.connect(self._launch_diagnose_nopic)
        diag_layout.addWidget(btn_diag_nopic)
        layout.addWidget(gbox_diag)

        layout.addStretch(1)
        return tab_toolbox

    def save_ui_state(self):
        path = os.path.join(os.path.expanduser("~"), ".maplebot_ui_state.json")
        state = {
            "last_config_path": self.path_cfg_custom
        }
        with open(path, 'w') as f:
            json.dump(state, f)
        logger.info(f"[UI] Save UI state to {path}")

    def load_ui_state(self):
        path = os.path.join(os.path.expanduser("~"), ".maplebot_ui_state.json")
        if os.path.exists(path):
            with open(path, 'r') as f:
                state = json.load(f)
            last = state.get("last_config_path")
            # ★ 2026-09-17：打包运行时**只认自己目录下**的配置。
            #   ui_state 存的是绝对路径（开发机上就是源码目录里的方案文件），
            #   照用会让 exe 去读写源码里的配置 —— 打包产物本该自给自足。
            #   统一交给 common.active_config_path 裁决（含「本目录同名方案」回退）。
            if IS_FROZEN:
                from src.utils.common import active_config_path
                last = active_config_path()
            # ⚠️ 上次用的方案可能已被改名/删除（2026-09-12 用户实测：把「某个方案.yaml」
            #    改名成「新方案名.yaml」后，这里仍指向旧路径）。不处理的话会静默丢设置：
            #      load_config 内部吞掉 FileNotFoundError → path_cfg_custom 退回 __init__
            #      的默认值 config/config_custom.yaml（一个不存在的文件）→ 之后每 3 秒
            #      的自动保存会**新建**这个孤儿文件并把设置写进去，用户真正的方案
            #      等于被丢弃（与 §8.9 属同一类问题）。
            #    所以路径不存在时明确回退到磁盘上真实存在的方案，并在日志里说清楚。
            if last and not os.path.exists(resource_path(last)):
                files = self._list_config_files()
                if files:
                    # 统一成相对路径 —— path_cfg_custom 在别处都是相对的（含 __init__ 默认值
                    # 与状态文件里存的值），一处绝对一处相对会让显示/比较出现"看着不一样"的假象
                    last = os.path.relpath(files[0], APP_ROOT)
                    logger.warning(f"[UI] 上次的配置方案已不存在：{state.get('last_config_path')}；"
                                   f"自动改用 {os.path.basename(last)}")
                else:
                    logger.warning(f"[UI] 上次的配置方案已不存在：{last}；"
                                   f"config/ 下也没有别的方案，用出厂默认值")
                    last = None
            # last 为空时**不能**调 load_config —— 它把 path=None 当成"让用户挑文件"，
            # 会弹出模态文件对话框，界面启动时就卡住了
            if last:
                self.load_config(create_error_label(), last)
        logger.info(f"[UI] Load UI state from {path}")


    def create_attack_gbox(self):
        '''
        Create attack group box using two-column QFormLayouts
        '''
        gbox = QGroupBox("⚔️ 攻击设置")

        # Left column
        form_left = QFormLayout()
        self.attack_mode = QComboBox()
        self.attack_mode.addItems(["基础攻击", "AOE技能"])
        self.attack_mode.setItemData(0, "directional")
        self.attack_mode.setItemData(1, "aoe_skill")
        self.attack_mode.setFixedWidth(100)

        # Connect dropdown change to handler
        self.attack_mode.currentIndexChanged.connect(
            self.update_atk_config_trigger_by_drop_list
        )

        # Set default value
        form_left.addRow("攻击模式：", self.attack_mode)

        self.attack_range_x = QLineEdit()
        self.attack_range_x.setFixedWidth(60)
        form_left.addRow("水平范围 X:", self.attack_range_x)

        # Right column
        form_right = QFormLayout()
        self.attack_cooldown = QLineEdit()
        self.attack_cooldown.setFixedWidth(60)
        form_right.addRow("攻击冷却（秒）：", self.attack_cooldown)

        self.attack_range_y = QLineEdit()
        self.attack_range_y.setFixedWidth(60)
        form_right.addRow("垂直范围 Y:", self.attack_range_y)

        # Combine left and right forms
        columns = QHBoxLayout()
        columns.addLayout(form_left)
        columns.addSpacing(20)
        columns.addLayout(form_right)

        # 挂机方式（2026-10-01 按用户要求移除该控件：定点驻守早已删除，只剩
        # 「巡逻绕圈」一项，控件没得选只剩占地方。bot.mode 固定写 "normal"。）

        # Field validation
        error_label = create_error_label()
        self.attack_range_x.editingFinished.connect(
            lambda: validate_numerical_input(self.attack_range_x.text(), error_label, 0, 9999))
        self.attack_range_y.editingFinished.connect(
            lambda: validate_numerical_input(self.attack_range_y.text(), error_label, 0, 9999))
        self.attack_cooldown.editingFinished.connect(
            lambda: validate_numerical_input(self.attack_cooldown.text(), error_label, 0, 9999))

        # Final layout
        layout = QVBoxLayout()
        layout.addWidget(error_label)
        layout.addLayout(columns)
        gbox.setLayout(layout)
        return gbox

    # ── 🐲 怪物识别（2026-10-13 加，用户要求把这几个键做成主界面控件）─────────
    # 为什么单开一组：高级设置页签 2026-09-12 移除后，`monster_detect` 这组键在界面上
    #   **完全没有入口**，只能让 AI 手改 config_custom.yaml。而"看得远不远 / 要不要停下
    #   打怪 / 识别阈值松紧"是用户按地图和手感要反复拧的旋钮 —— 属于"用户会改"的那类。
    # ⚠️ 只把这 5 个键放上来；mode / verify_* / every_n_frames 等仍是算法内部参数
    #    （为什么收起它们见 ADV_FIELD_HIDE 那一处）。
    # ⚠️ 三处接线缺一不可（少一处 = 这个控件是装饰品）：
    #    ① setup_main_tab() 里挂上；② apply_config_to_ui() 里灌值；③ update_cfg_from_main_ui()
    #    里读回。tools/verify_monster_ui.py 就是钉这三处的（含真控件往返）。
    def create_monster_gbox(self):
        '''
        怪物识别：monster_detect 里"用户会改"的 5 个键（2026-10-13 加）
        '''
        gbox = QGroupBox("🐲 怪物识别")

        form_left = QFormLayout()
        self.monster_stop_cb = QCheckBox("巡逻中停下来打完再走")
        self.monster_stop_cb.setToolTip(
            "勾上：索敌框里看到怪就站定，打完再走\n"
            "（连续 2 个检测帧框里没怪才算打完；两条安全阀会强制放行，日志里有 WARNING）\n"
            "取消：老行为 —— 边走边打")
        form_left.addRow(self.monster_stop_cb)

        self.monster_approach_cb = QCheckBox("够不着就走过去打")
        self.monster_approach_cb.setToolTip(
            "勾上：怪在攻击范围外、但在同一层（高度差 <= 60px）时朝它走一段\n"
            "（跨层不硬走：上面的要爬梯子、下面的要跳，交给路线）")
        form_left.addRow(self.monster_approach_cb)

        form_right = QFormLayout()
        self.monster_scale_x = QLineEdit()
        self.monster_scale_x.setFixedWidth(60)
        self.monster_scale_x.setToolTip(
            "感知框 = 攻击框 + 搜索框外扩，再乘这个倍数。1 = 只看攻击范围那么远")
        form_right.addRow("看得远倍数 X:", self.monster_scale_x)

        self.monster_scale_y = QLineEdit()
        self.monster_scale_y.setFixedWidth(60)
        self.monster_scale_y.setToolTip(
            "竖直方向单独收窄（省时间）：竖直多看没用 —— 够不着的不会横着走过去")
        form_right.addRow("看得远倍数 Y:", self.monster_scale_y)

        self.monster_diff_thres = QLineEdit()
        self.monster_diff_thres.setFixedWidth(60)
        self.monster_diff_thres.setToolTip(
            "模板匹配的差异阈值（0~1）：越小越严格。越小越容易漏，越大越容易误报")
        form_right.addRow("识别阈值:", self.monster_diff_thres)

        # 2026-10-14 用户 m08016 拍板「B 够得着判据加余量」：这是他要反复拧的旋钮，
        # 所以放上来（不是一个写死在代码里的常数）。读数依据见 config_default.yaml。
        self.monster_engage_margin = QLineEdit()
        self.monster_engage_margin.setFixedWidth(60)
        self.monster_engage_margin.setToolTip(
            "宽判定余量（px）：判「够得着」时把攻击框向外各扩这么多\n"
            "人的眼睛是预估的（大概在范围内就会打），0 = 严格按技能框\n"
            "示范录制读数：389 帧「看见了怪却判够不着」里 69% 只差 ≤45px ⇒ 默认 40")
        form_right.addRow("宽判定余量(px):", self.monster_engage_margin)

        columns = QHBoxLayout()
        columns.addLayout(form_left)
        columns.addSpacing(20)
        columns.addLayout(form_right)

        error_label = create_error_label()
        self.monster_scale_x.editingFinished.connect(
            lambda: validate_numerical_input(self.monster_scale_x.text(), error_label, 1, 20))
        self.monster_scale_y.editingFinished.connect(
            lambda: validate_numerical_input(self.monster_scale_y.text(), error_label, 1, 20))
        self.monster_diff_thres.editingFinished.connect(
            lambda: validate_numerical_input(self.monster_diff_thres.text(), error_label, 0, 1))
        self.monster_engage_margin.editingFinished.connect(
            lambda: validate_numerical_input(
                self.monster_engage_margin.text(), error_label, 0, 200))

        layout = QVBoxLayout()
        layout.addWidget(error_label)
        layout.addLayout(columns)
        gbox.setLayout(layout)
        return gbox

    def _apply_monster_ui(self):
        '''
        把 cfg["monster_detect"] 的值灌进上面那 5 个控件（缺键用出厂默认值）
        '''
        md = self.cfg.get("monster_detect") or {}
        try:
            self.monster_stop_cb.setChecked(bool(md.get("stop_to_attack", True)))
            self.monster_approach_cb.setChecked(bool(md.get("approach_mob", True)))
            self.monster_scale_x.setText(str(md.get("detect_range_scale", 2.0)))
            self.monster_scale_y.setText(str(md.get("detect_range_scale_y", 1.1)))
            self.monster_diff_thres.setText(str(md.get("diff_thres", 0.8)))
            self.monster_engage_margin.setText(str(md.get("mob_engage_margin", 40)))
        except AttributeError:
            # 控件还没建（构造顺序变了 / 单测里的裸实例）—— 当场不炸，等用户真点保存
            pass

    def _read_monster_ui(self):
        '''
        读那 5 个控件 → 就地写回 cfg["monster_detect"]（**不动**同一段里别的键）

        ⚠️ 就地写的原因：`monster_detect` 段里还有 mode / verify_* / every_n_frames 等
           算法内部键，本界面不管它们；整段替换会把它们冲掉。
        '''
        def _num(text, old, cast):
            try:
                return cast(str(text).strip())
            except (TypeError, ValueError):
                # 输入框被清空/填了非数字：保留原值，别把配置写成 None
                return old

        md = self.cfg.setdefault("monster_detect", {})
        md["stop_to_attack"] = bool(self.monster_stop_cb.isChecked())
        md["approach_mob"] = bool(self.monster_approach_cb.isChecked())
        md["detect_range_scale"] = _num(self.monster_scale_x.text(),
                                        md.get("detect_range_scale", 2.0), float)
        md["detect_range_scale_y"] = _num(self.monster_scale_y.text(),
                                          md.get("detect_range_scale_y", 1.1), float)
        md["diff_thres"] = _num(self.monster_diff_thres.text(),
                                md.get("diff_thres", 0.8), float)
        # 宽判定余量：允许写 "40" / "40.0"，超范围夹住（与 validate_numerical_input 的 0~200 一致）
        try:
            _mg = int(float(str(self.monster_engage_margin.text()).strip()))
            md["mob_engage_margin"] = max(0, min(200, _mg))
        except (TypeError, ValueError):
            md["mob_engage_margin"] = int(md.get("mob_engage_margin", 40) or 0)
        return md

    # ── 🎥 示范录制（2026-10-14 加，用户要求"我自己操作，录下来当样本"）────────
    # 为什么单开一组：同「怪物识别」的理由 —— 高级设置页签 2026-09-12 移除后，
    #   `demo_record` 这组键在界面上**没有任何入口**，用户想录一次只能让 AI 改 yaml。
    # ⚠️ 三处接线缺一不可（少一处 = 这个控件是装饰品，见 tools/verify_demo_ui.py）：
    #    ① setup_main_tab() 里挂上；② apply_config_to_ui() 里灌值；
    #    ③ update_cfg_from_main_ui() 里读回 —— 并且 `demo_record` 必须进
    #    `_MANAGED_SECTIONS`（否则 3 秒自动保存会拿磁盘旧值盖掉界面值，静默失效）。
    # ⚠️ 勾上「开示范录制」+「接管键盘」之后，机器人**一个键都不发**，全靠你自己玩 ——
    #    这是刻意的：录制期间要的是"人的操作"，不是机器人的操作（引擎照常感知、照常
    #    算影子决策，但按键被 kb.disable() 彻底静音）。
    def create_demo_gbox(self):
        '''
        示范录制：demo_record 里"用户会改"的 5 个键（2026-10-14 加）
        '''
        gbox = QGroupBox("🎥 示范录制（你玩，它录）")

        form_left = QFormLayout()
        self.demo_enabled_cb = QCheckBox("开示范录制")
        self.demo_enabled_cb.setToolTip(
            "勾上：点「开始」之后，把每一帧的怪物检测与你按的键录到文件里\n"
            "（录完把目录交给 AI，用 tools/analyze_demo.py 出对照报告）\n"
            "不勾：完全不录，也没有任何额外开销")
        form_left.addRow(self.demo_enabled_cb)

        self.demo_record_only_cb = QCheckBox("录制时接管键盘（机器人不出手）")
        self.demo_record_only_cb.setToolTip(
            "勾上（推荐）：录制期间**键盘静音**，机器人一个键都不发，完全由你操作 ——\n"
            "这样录到的才是「人怎么打」，不是「机器人怎么打」\n"
            "取消：机器人照常挂机，你的按键也会被一起记下来")
        form_left.addRow(self.demo_record_only_cb)

        form_right = QFormLayout()
        self.demo_out_dir = QLineEdit()
        self.demo_out_dir.setFixedWidth(160)
        self.demo_out_dir.setToolTip(
            "存到哪个目录（相对程序目录；默认 debug/demo）\n"
            "录出来的是 demo_*.jsonl.gz + 同名的 _meta.json")
        form_right.addRow("存到目录:", self.demo_out_dir)

        self.demo_max_minutes = QLineEdit()
        self.demo_max_minutes.setFixedWidth(60)
        self.demo_max_minutes.setToolTip(
            "单个文件最长录多少分钟，到点自动换下一个文件（0 = 不限制）")
        form_right.addRow("单个文件 分钟:", self.demo_max_minutes)

        self.demo_gzip_cb = QCheckBox("压缩存盘（.gz）")
        self.demo_gzip_cb.setToolTip(
            "约省 8 倍空间（1 分钟约 1.3MB → 160KB 左右），代价是 CPU 略高")
        form_right.addRow(self.demo_gzip_cb)

        columns = QHBoxLayout()
        columns.addLayout(form_left)
        columns.addSpacing(20)
        columns.addLayout(form_right)

        error_label = create_error_label()
        self.demo_max_minutes.editingFinished.connect(
            lambda: validate_numerical_input(self.demo_max_minutes.text(),
                                             error_label, 0, 600))

        layout = QVBoxLayout()
        layout.addWidget(error_label)
        layout.addLayout(columns)
        gbox.setLayout(layout)
        return gbox

    def _apply_demo_ui(self):
        '''
        把 cfg["demo_record"] 的值灌进上面那 5 个控件（缺键用出厂默认值）
        '''
        dd = self.cfg.get("demo_record") or {}
        try:
            self.demo_enabled_cb.setChecked(bool(dd.get("enabled", False)))
            self.demo_record_only_cb.setChecked(bool(dd.get("record_only", True)))
            self.demo_gzip_cb.setChecked(bool(dd.get("gzip", True)))
            self.demo_out_dir.setText(str(dd.get("out_dir", "debug/demo")))
            self.demo_max_minutes.setText(str(dd.get("max_minutes", 30)))
        except AttributeError:
            # 控件还没建（构造顺序变了 / 单测里的裸实例）—— 当场不炸
            pass

    def _read_demo_ui(self):
        '''
        读那 5 个控件 → 就地写回 cfg["demo_record"]（**不动**同一段里别的键）

        ⚠️ 就地写的原因：`demo_record` 段里还有 record_keys / every_n_frames /
           max_frames 等算法内部键，本界面不管它们；整段替换会把它们冲掉。
        '''
        def _num(text, old, cast):
            try:
                return cast(str(text).strip())
            except (TypeError, ValueError):
                # 输入框被清空/填了非数字：保留原值，别把配置写成 None
                return old

        def _mins(text, old):
            '''分钟数：整数就写成整数（别把 30 写成 30.0，配置要好看也好读）'''
            try:
                v = float(str(text).strip())
            except (TypeError, ValueError):
                return old
            return int(v) if float(v).is_integer() else v

        dd = self.cfg.setdefault("demo_record", {})
        dd["enabled"] = bool(self.demo_enabled_cb.isChecked())
        dd["record_only"] = bool(self.demo_record_only_cb.isChecked())
        dd["gzip"] = bool(self.demo_gzip_cb.isChecked())
        dd["out_dir"] = str(self.demo_out_dir.text()).strip() or "debug/demo"
        dd["max_minutes"] = _mins(self.demo_max_minutes.text(),
                                  dd.get("max_minutes", 30))
        return dd

    def create_pickup_gbox(self):
        '''
        🖱️ 自动拾取（2026-10-01 加）

        场景：国服靠名字标签定位角色，宠物老是挡住名字 → 定位不到人；
        但把宠物收起来就没人帮捡东西了。这个功能用「按一次拾取键 + 等冷却」
        代替宠物拾取。

        ⚠️ 冷却的实际间隔 = 界面值 × 0.7~1.3 随机（见 KeyBoardController.PICKUP_JITTER），
           界面上显示的是**基准值**。抖动不是装饰，是刻意设计：
           精确周期在统计上不像人按的，内核反作弊做行为特征分析时是可疑信号。
        '''
        gbox = QGroupBox("🖱️ 自动拾取（宠物挡名字时用）")
        form = QFormLayout()

        self.pickup_cooldown = QDoubleSpinBox()
        self.pickup_cooldown.setRange(0.5, 10.0)   # 下限 0.5：更低既没用也更可疑
        self.pickup_cooldown.setSingleStep(0.1)
        self.pickup_cooldown.setDecimals(1)
        self.pickup_cooldown.setSuffix(" 秒")
        self.pickup_cooldown.setFixedWidth(110)
        self.pickup_cooldown.setToolTip(
            "两次拾取之间的**基准**间隔。\n"
            "实际间隔会在 ±30% 内随机波动（如填 2.0 → 实际 1.4~2.6 秒）。\n"
            "⚠️ 调小会让拾取挤占攻击节奏、打怪变慢。")
        form.addRow("拾取冷却：", self.pickup_cooldown)

        self.pickup_hint = QLabel()
        self.pickup_hint.setWordWrap(True)
        self.pickup_hint.setStyleSheet("color: #666;")
        form.addRow("", self.pickup_hint)

        # 键或冷却一变就刷新提示，让用户实时看到「实际间隔」区间
        # ⚠️ SingleKeyEdit 继承 QKeySequenceEdit，**没有 textChanged**
        #   （那是 QLineEdit 的信号），要用 keySequenceChanged。
        self.pickup_key.keySequenceChanged.connect(self.update_pickup_hint)
        self.pickup_cooldown.valueChanged.connect(self.update_pickup_hint)
        self.update_pickup_hint()

        gbox.setLayout(form)
        return gbox

    def update_pickup_hint(self):
        '''刷新自动拾取的说明文字（随键位/冷却实时变）。'''
        if not self.pickup_key.get_key():
            self.pickup_hint.setText(
                "当前**未启用**（拾取键留空）。把「拾取」填上键位即可开启。\n"
                "用法：收掉宠物，脚本会按「拾取键」自动捡东西。")
            return
        cd = self.pickup_cooldown.value()
        # ⚠️ 必须提示「调小会挤掉攻击」：
        #   拾取键和攻击键都由 KeyBoardController 同一个线程发（见 run()），
        #   冷却调得太小 = 单位时间里塞进去的拾取键变多，会和攻击抢出招时机
        #   —— 表现就是「打怪变慢 / 漏刀」。这就是第一版预览里那句提示，
        #   2026-10-01 落地正式界面时漏掉了，用户指出后补回。
        warn = ""
        if cd < 1.0:
            warn = "\n⚠️ 冷却偏小：拾取会和攻击抢出招时机，打怪会变慢。建议 ≥ 1.5 秒。"
        self.pickup_hint.setText(
            f"把宠物收起来，脚本每 {cd * 0.7:.1f}~{cd * 1.3:.1f} 秒按一次拾取键"
            f"（基准 {cd:.1f} 秒，±30% 随机波动）。\n"
            "间隔刻意不固定 —— 精确周期不像人按的，随机更稳妥。\n"
            "冷却调小会让拾取挤占攻击节奏，打怪变慢；卡顿就调大。" + warn)

    def create_key_binding_gbox(self):
        gbox = QGroupBox("🎮 按键绑定")
        hbox = QHBoxLayout()

        # Left Column
        form_left = QFormLayout()
        self.basic_attack_key = SingleKeyEdit()
        self.basic_attack_key.setFixedWidth(100)
        form_left.addRow("基础攻击：", self.basic_attack_key)

        self.teleport_key = SingleKeyEdit()
        self.teleport_key.setFixedWidth(100)
        form_left.addRow("瞬移：", self.teleport_key)

        # Right Column
        form_right = QFormLayout()
        self.aoe_skill_key = SingleKeyEdit()
        self.aoe_skill_key.setFixedWidth(100)
        form_right.addRow("群体技能：", self.aoe_skill_key)

        self.jump_key = SingleKeyEdit()
        self.jump_key.setFixedWidth(100)
        form_right.addRow("跳跃：", self.jump_key)

        self.return_home_key = SingleKeyEdit()
        self.return_home_key.setFixedWidth(100)
        form_right.addRow("回城：", self.return_home_key)

        self.pickup_key = SingleKeyEdit()
        self.pickup_key.setFixedWidth(100)
        self.pickup_key.setToolTip(
            "宠物挡名字时用：把宠物收起来，用这个键自动捡东西。\n"
            "留空 = 关闭自动拾取。")
        form_right.addRow("拾取：", self.pickup_key)

        # Combine left and right column form
        hbox.addLayout(form_left)
        hbox.addSpacing(20)  # space between columns
        hbox.addLayout(form_right)
        gbox.setLayout(hbox)
        return gbox

    def create_buff_skill_section(self):
        '''
        Create buff skill section with toggle checkbox and dynamic rows
        '''
        container = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignLeft)
        container.setLayout(layout)

        # Auto Buff checkbox
        self.checkbox_enable_buff = QCheckBox("自动Buff")
        self.checkbox_enable_buff.stateChanged.connect(self.toggle_auto_buff)
        layout.addWidget(self.checkbox_enable_buff, alignment=Qt.AlignLeft)

        # Buff section (initially hidden)
        self.buff_section_container = QWidget()
        self.buff_section_container.setVisible(False)
        buff_section_layout = QVBoxLayout()
        buff_section_layout.setContentsMargins(0, 0, 0, 0)
        buff_section_layout.setSpacing(2)
        buff_section_layout.setAlignment(Qt.AlignLeft)
        self.buff_section_container.setLayout(buff_section_layout)

        # Buff rows layout
        self.buff_layout = QVBoxLayout()
        self.buff_layout.setSpacing(2)
        self.buff_layout.setAlignment(Qt.AlignLeft)
        self.buff_inputs = []

        # Initial buff row
        self.add_buff_row()

        # Add dynamic row layout + button
        buff_section_layout.addLayout(self.buff_layout)

        self.button_add_buff = QPushButton("+ 添加Buff按键")
        self.button_add_buff.setFixedWidth(100)
        self.button_add_buff.clicked.connect(self.add_buff_row)
        buff_section_layout.addWidget(self.button_add_buff, alignment=Qt.AlignLeft)

        layout.addWidget(self.buff_section_container)

        return container

    def create_buff_skill_gbox(self):
        '''
        增益技能设置

        上游这里原是「宠物技能」组，除增益技能外还有「自动加HP/MP」——
        那套血条识别把上游 UI 的尺寸（752x1282、条宽高比 7.5）写死在代码里，
        国服是 Unity 重制 UI，实测不成立且失败时静默无提示，故整体移除。
        '''
        gbox = QGroupBox("💫 增益技能")
        layout_form = QFormLayout()
        layout_form.addRow(self.create_buff_skill_section())
        gbox.setLayout(layout_form)
        return gbox

    def create_map_selection_gbox(self):
        '''
        Creates a group box containing the scroll list for map selection
        '''
        gbox = QGroupBox("🗺️ 地图")

        # Load map list from directory
        self.list_widget_maps = QListWidget()
        self.list_widget_maps.itemClicked.connect(self.on_map_selected)
        self.refresh_map_list()   # 扫描并填充（之后由 5 秒定时器自动保持最新）

        layout = QVBoxLayout()
        self.label_map_info = QLabel("请选择地图:")
        layout.addWidget(self.label_map_info)
        layout.addWidget(self.list_widget_maps)

        # 地图级操作：登记怪物 / 删除地图（2026-09-12）
        row = QHBoxLayout()
        self.btn_map_mobs = QPushButton("🐾 打哪几种怪…")
        self.btn_map_mobs.setToolTip(
            "勾选这张图会出现的怪。怪物模板是全局通用的 —— 以前截过的怪"
            "直接勾上就能用，不用重新截。\n"
            "（引擎只加载这张图登记过的怪，所以新图要勾一次。）")
        self.btn_map_mobs.clicked.connect(self.register_existing_mobs)
        self.btn_map_mobs.setEnabled(False)
        row.addWidget(self.btn_map_mobs)

        # 📦 内置怪物素材库（2026-09-29）：随包附带一批官方素材转好的现成模板，
        #    用户选完地图点一下就能把该图会出现的怪直接装好并登记，
        #    **完全不用自己截**。这是「选图后没怪可用 → 不打怪」的正解。
        self.btn_mob_lib = QPushButton("📦 从素材库选怪…")
        self.btn_mob_lib.setToolTip(
            "从随包附带的怪物素材库（国服官方素材）里挑怪。\n"
            "选中会自动装好模板并登记到当前地图 —— 不用开游戏、不用自己截。\n"
            "库里没有的怪，仍然可以用【截取怪物模板】自己截。")
        self.btn_mob_lib.clicked.connect(self.register_from_library)
        self.btn_mob_lib.setEnabled(False)
        row.addWidget(self.btn_mob_lib)

        # 🗑️ 删除地图（2026-09-12 用户需求）：录坏的图一键清掉重录，
        #    不用去资源管理器里翻 minimaps/ 目录。
        self.btn_delete_map = QPushButton("🗑️ 删除这张地图…")
        self.btn_delete_map.setToolTip(
            "把这张图的路线 / 回正线整个删掉，并从登记表里移除。\n"
            "删除前会自动整目录备份到 minimaps/<图名>.bak —— 删错了改回原名就能救回来。\n"
            "（挂机或录制进行中不能删。）")
        self.btn_delete_map.clicked.connect(self.delete_selected_map)
        self.btn_delete_map.setEnabled(False)   # 没选中地图时禁用
        self.list_widget_maps.itemSelectionChanged.connect(self._update_delete_btn)
        row.addWidget(self.btn_delete_map)
        layout.addLayout(row)

        # 🖊 手绘回正线（v0.9，PRD 6.1）—— 主推入口：
        #    不用开游戏、不用真的掉下去一次，纯鼠标在放大的小地图上点几下就画好。
        #    老的「补录回正线（要真掉一次）」保留在录制流程里，但不再是首选。
        row2 = QHBoxLayout()
        self.btn_draw_home = QPushButton("🖊 手绘回正线（推荐）")
        self.btn_draw_home.setToolTip(
            "画一条「掉下去之后怎么走回来」的路线。\n\n"
            "· 不用开游戏、不用真的掉下去一次 —— 纯鼠标点几下就行\n"
            "· 打开的是这张图的小地图（放大 4 倍），主线用彩色画着，一眼看出坑在哪\n"
            "· 第 1 下点落点（角色掉下去站的位置），再点回到主线的路，最后点保存\n\n"
            "（老办法「补录回正线」仍在录制流程里，只是要先真掉一次。）")
        self.btn_draw_home.clicked.connect(self._open_home_route_drawer)
        self.btn_draw_home.setEnabled(False)    # 没选中地图时禁用
        row2.addWidget(self.btn_draw_home)
        layout.addLayout(row2)

        gbox.setLayout(layout)
        return gbox

    def create_potion_gbox(self):
        '''🧪 自动喝药面板（2026-09-27 加，自己写的，未照搬上游 PR 的面板）。

        【它解决什么】
          挂机时血/蓝掉太快、手动补不过来 —— 这个面板让工具自己盯着血条蓝条，
          低于设定的线就帮你按一下药水键。

        【设计取舍（为什么长这样）】
          · 用户**不需要填任何数字**：血条蓝条的位置、颜色、满值宽度
            都是开发期在真机上量好写进 config 的，这里是只读展示。
            用户只要改「药水键」和「什么时候喝」两件事。
          · 环境不对时**直接说明白**（而不是开了没效果）：血条位置是按
            1366x768 窗口化量死的，环境一变就失准，所以按钮下方常驻一行
            环境提示，能实时刷新。
          · 一键测：不想挂机、只想确认"工具到底看不看得见我的血条"时，
            点「试一下」跑一次读数，立刻出结果 —— 省掉"开了半天不知道有没有用"。
        '''
        gbox = QGroupBox("🧪 自动喝药（血蓝监控）")
        layout = QVBoxLayout()

        # ── 顶部说明（一句话讲清这个功能干嘛的）─────────────────────
        tip = QLabel(
            "工具会一直盯着你的血条和蓝条，掉到下面设定的位置就自动帮你按药水键。\n"
            "血条位置是**按 1366x768 窗口化量好的**，所以不用你自己标定 —— "
            "但环境要符合要求（见下方提示）。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        # ── 总开关 ────────────────────────────────────────────────
        row = QHBoxLayout()
        self.cb_potion_enabled = QCheckBox("启用自动喝药")
        self.cb_potion_enabled.setToolTip(
            "想自己控药（比如打 BOSS）时把它取消勾选 —— 挂机照常跑，只是不自动喝药。")
        # 初始值从配置读（缺省开）
        _potion_on = True
        try:
            _potion_on = bool(self.cfg.get("potion", {}).get("enabled", True))
        except Exception:
            _potion_on = True
        self.cb_potion_enabled.setChecked(_potion_on)
        self.cb_potion_enabled.toggled.connect(self.on_potion_toggled)
        row.addWidget(self.cb_potion_enabled)

        # 🔍 一键试读：不开挂机也能验证"到底看不看得见血条"
        self.btn_potion_test = QPushButton("🔍 试一下（读一次血蓝）")
        self.btn_potion_test.setToolTip(
            "立刻读一次当前画面上的血条/蓝条，把结果告诉你。\n"
            "用来确认环境对不对、工具看不看得见你的血条 —— 不用先开始挂机。")
        self.btn_potion_test.clicked.connect(self.test_bars_reading)
        row.addWidget(self.btn_potion_test)
        row.addStretch()
        layout.addLayout(row)

        # ── 档位说明（只读文字，不是输入框 —— 见上面"用户零配置"）─────
        self.label_potion_tiers = QLabel("")
        self.label_potion_tiers.setWordWrap(True)
        layout.addWidget(self.label_potion_tiers)

        # ── 环境提示行（实时刷新）──────────────────────────────────
        self.label_potion_env = QLabel("")
        self.label_potion_env.setWordWrap(True)
        layout.addWidget(self.label_potion_env)

        # ── 实时读数行（挂钩机时的血蓝百分比）────────────────────────
        self.label_potion_live = QLabel("读数：还没开始挂机")
        self.label_potion_live.setWordWrap(True)
        layout.addWidget(self.label_potion_live)

        # 首次填充文字
        self._refresh_potion_labels()

        gbox.setLayout(layout)
        return gbox

    def _potion_tiers_text(self):
        '''把配置里的档位翻译成一句人话（只读展示）。'''
        try:
            potion = self.cfg.get("potion", {}) or {}
        except Exception:
            return "读不到喝药配置。"

        def _fmt(tiers, label):
            if not tiers:
                return f"{label}：未配置"
            parts = []
            for t in tiers:
                if not isinstance(t, dict):
                    continue
                key = t.get("key")
                thr = t.get("threshold")
                name = t.get("name", "?")
                if key:
                    parts.append(f"低于 {thr}% 按 {key}（{name}）")
                else:
                    parts.append(f"低于 {thr}% 不喝（{name}）")
            return f"{label}：" + "；".join(parts) if parts else f"{label}：未配置"

        hp = _fmt(potion.get("hp"), "血")
        mp = _fmt(potion.get("mp"), "蓝")
        cd_h = potion.get("hp_cooldown_sec", 1.5)
        cd_m = potion.get("mp_cooldown_sec", 1.5)
        cf = potion.get("confirm_frames", 2)
        return (f"{hp}\n{mp}\n"
                f"（连续 {cf} 帧都低于才算数；两次之间至少隔 {cd_h} / {cd_m} 秒）")

    def _refresh_potion_labels(self):
        '''刷新档位文字 + 环境提示（面板初始化与定时器都会调）。'''
        if not getattr(self, "label_potion_tiers", None):
            return
        try:
            self.label_potion_tiers.setText(self._potion_tiers_text())
        except Exception as e:                                   # noqa: BLE001
            self.label_potion_tiers.setText(f"读喝药配置出错：{e}")

        # 环境提示：这是**只读诊断**，不弹窗、不拦人 —— 真拦在引擎启动时做
        env = "环境：检测中…"
        try:
            from src.utils.env_precheck import check_bars_environment
            ok, title, msg = check_bars_environment(self.cfg)
            if ok:
                env = f"✅ 环境没问题 —— {title}"
            else:
                # 只取第一行与"怎么改"的首条，别把整段塞进面板
                first = msg.splitlines()[0] if msg else title
                env = f"⚠️ {title} —— {first}（详细做法请看「日志」区，点开始时会写进去）"
        except Exception as e:                                   # noqa: BLE001
            env = f"环境：检查失败（{e}）"
        self.label_potion_env.setText(env)

    def on_potion_toggled(self, checked):
        '''总开关：立刻作用到正在跑的引擎（不必停挂机）。'''
        try:
            self.controller.auto_bot.set_potion_enabled(checked)
        except Exception as e:                                   # noqa: BLE001
            # 引擎还没起来（没点过开始）时这里会失败 —— 属正常，不当错误
            logger.debug(f"[自动喝药] 开关暂时没送到引擎（多半还没开始挂机）：{e}")

    def test_bars_reading(self):
        '''🔍 试一下：抓当前画面读一次血蓝，把结果用人话告诉用户。

        【为什么要有这个按钮】
          "开了半天不知道到底有没有用"是最常见的困惑 —— 血蓝监控尤其如此：
          它出问题时的表现是**什么都不发生**（读数不可信 → 不喝药），
          而那是"正确行为"还是"没工作"从外面根本分不出来。
          这个按钮把内部状态直接摊开：读到了多少、环境对不对、为什么没喝。
        '''
        try:
            import numpy as np
            from src.input.GameWindowCapturor import GameWindowCapturor
            from src.utils.bars import BarsDetector
            from src.utils.common import (crop_frame_to_client,
                                          resolve_title_bar_height)
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "试不了", f"加载识别模块失败：{e}")
            return

        # ── 1. 先查环境（不对就别费劲抓帧了）─────────────────────
        try:
            from src.utils.env_precheck import check_bars_environment
            ok, title, msg = check_bars_environment(self.cfg)
        except Exception as e:                                   # noqa: BLE001
            ok, title, msg = False, "环境检查失败", str(e)
        if not ok:
            QMessageBox.warning(self, title, msg)
            return

        # ── 2. 抓一帧 ────────────────────────────────────────────
        cap = None
        try:
            cap = GameWindowCapturor(self.cfg)
            # 抓帧是另一个线程在填缓冲区，给它一点时间攒出第一帧
            for _ in range(30):
                QApplication.processEvents()
                time.sleep(0.1)
                raw = cap.get_frame()
                if raw is not None:
                    break
            if raw is None:
                QMessageBox.warning(
                    self, "读不到游戏画面",
                    "没能从游戏窗口抓到画面。\n\n"
                    "请确认：游戏开着、**没有被最小化**、并且已经在角色画面里"
                    "（不是登录界面）。\n\n"
                    "（被别的窗口挡住没关系，最小化不行。）")
                return
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "试读失败", f"抓帧出错：{e}")
            return
        finally:
            # 抓帧器起了后台线程，用完必须停掉，否则会一直占着资源
            try:
                if cap is not None:
                    cap.stop()
            except Exception:                                    # noqa: BLE001
                pass

        # ── 3. 裁剪 + 识别 ───────────────────────────────────────
        try:
            th, tw = self.cfg["game_window"]["size"]
            # ★ issue #14：与引擎共用同一个自适应取值，避免"录制正常但跑起来偏"
            frame, _msg = crop_frame_to_client(
                raw, (int(th), int(tw)),
                resolve_title_bar_height(self.cfg, tag="试读"), tag="试读")
            if frame is None:
                QMessageBox.warning(
                    self, "画面大小对不上",
                    f"抓到画面了，但裁不成要求的尺寸。\n\n{_msg}\n\n"
                    "请把游戏设成 1366x768、窗口化运行。")
                return

            detector = BarsDetector.from_dict(self.cfg["bars"])
            hp_res, mp_res = detector.detect(frame)
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "识别出错", f"读血条时出错：{e}")
            return

        def _line(label, res):
            if res.valid and res.percent is not None:
                return f"{label}：{res.percent:.1f}%"
            return f"{label}：读不到（{res.reason}）"

        # ── 4. 把结果和人话一起给出来 ─────────────────────────────
        body = (f"{_line('血量', hp_res)}\n"
                f"{_line('魔法', mp_res)}\n\n"
                f"（读到数值 = 工具看得见你的血条，功能正常。\n"
                f"　读不到通常是：血条颜色和预设差太多、或游戏窗口不在前台。\n"
                f"　读不到时工具**不会**乱喝药 —— 那是特意这么设计的。）")
        QMessageBox.information(self, "试读结果", body)

    def _refresh_potion_live(self):
        '''定时刷新喝药面板：环境提示 + 挂机中的实时血蓝读数（2 秒一次）。

        ⚠️ 全程静默兜底：这个定时器在界面线程里跑，它抛异常会把整个界面带崩。
           面板刷不出来只是"少看一个数字"，绝不能因此让界面死掉。
        '''
        try:
            self._refresh_potion_labels()
        except Exception:                                        # noqa: BLE001
            pass

        try:
            bot = getattr(self.controller, "auto_bot", None)
            # label 还没建出来（定时器可能早于面板构建先响一次）→ 直接跳过
            if bot is None or not getattr(self, "label_potion_live", None):
                return
            if not getattr(bot, "bars_ok", False):
                return
            hp = getattr(bot, "hp_percent", None)
            mp = getattr(bot, "mp_percent", None)
            if hp is None and mp is None:
                self.label_potion_live.setText(
                    "读数：还没有数据（点了「开始」之后才会读；"
                    "若一直是这样，点上面的「试一下」看看）")
                return
            hp_s = f"{hp:.1f}%" if hp is not None else "读不到"
            mp_s = f"{mp:.1f}%" if mp is not None else "读不到"
            drunk = getattr(bot, "n_potion_drunk", 0)
            state = "开" if getattr(bot, "potion_enabled", True) else "关"
            self.label_potion_live.setText(
                f"当前读数：血量 {hp_s} ｜ 魔法 {mp_s}　"
                f"（本次已喝 {drunk} 次，喝药开关：{state}）")
        except Exception:                                        # noqa: BLE001
            pass

    def _update_delete_btn(self):
        '''按「地图列表当前有没有选中项」启用/禁用这几个按钮。

        ★ 2026-09-29 修（用户实测：「选了地图，这三个按钮还是灰的，点了也没用」）

        ── 为什么会灰（真机复现，见 tools/verify_map_buttons.py）─────────────
        原来本方法只读 `self.selected_map`，而那个值**只在 on_map_selected 里赋值**，
        两者分别挂在两个信号上，Qt 的触发顺序是：

            ① itemSelectionChanged  →  本方法先跑（此刻 selected_map 还是旧值 ""）
            ② itemClicked           →  on_map_selected 才赋值

        ⇒ 第一次点击时本方法读到的仍是空串 → has=False → 三个按钮保持禁用；
          而**不会有第二次机会**：再点同一行选区没变，itemSelectionChanged
          不再发出 ⇒ 按钮**永远**灰着（用户点了没反应就是这么来的）。

        ── 修法 ──────────────────────────────────────────────────────────────
        不依赖"赋值是否已发生"，直接**问列表当前选中的是哪一项** —— 这样
        点选 / 键盘上下键切换 / 程序化 setCurrentRow（如 refresh_map_list 恢复
        选中）三种来源都能正确点亮。

        ⚠️ 不要退回"只读 self.selected_map"的写法：那会重新引入信号顺序 bug。
        ⚠️ 判定用 **`item.isSelected()`（QListWidgetItem 的方法）**，
           不要写成 `self.list_widget_maps.isItemSelected(item)` —— QListWidget
           **没有** isItemSelected 这个方法（实测 AttributeError）。
        ⚠️ 仍要判一次选中态：`clearSelection()` 之后 `currentItem()` 仍非 None
           （实测：clearSelection 后 item.isSelected()=False 但 currentItem 不空），
           不判会把已清空选择当成有效选择、按钮误亮。
        '''
        item = self.list_widget_maps.currentItem()
        if item is not None and item.isSelected():
            # 与 on_map_selected 同一套解析口径：显示文本可能是 "id (中文名)"
            self.selected_map = item.text().split(" (")[0]
        else:
            # ★ 必须**清掉**旧值：selection 被清空（clearSelection / 列表重建后
            #   没有选中项）时，若沿用上一次的 selected_map，按钮会**误亮** ——
            #   看起来选中了一张图，实际列表里啥都没选（自检 [6] 钉的正是这条）。
            self.selected_map = ""

        has = bool(self.selected_map)
        self.btn_delete_map.setEnabled(has)
        self.btn_map_mobs.setEnabled(has)
        if getattr(self, 'btn_mob_lib', None) is not None:
            self.btn_mob_lib.setEnabled(has)
        if getattr(self, 'btn_draw_home', None) is not None:
            self.btn_draw_home.setEnabled(has)

    def create_log_gbox(self):
        gbox = QGroupBox("📜 日志")
        layout = QVBoxLayout()

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        # 2026-10-14：日志窗搬到「调试」页，用户要求"稍微长一点" —— 从原来的
        #   maxHeight 150 改成 minHeight 280 / maxHeight 520：既给足可见行数，
        #   又不会把整页挤到必须滚动才能看到下面的东西。
        self.log_output.setMinimumHeight(280)
        self.log_output.setMaximumHeight(520)
        # ⚠️ 必须显式钉死配色（2026-09-13 用户实测：日志**完全看不见**）。
        #    append_log 里文字颜色是硬编码的（INFO=白 / WARNING=橙 / ERROR=红），
        #    而这里不设背景的话，QPlainTextEdit 会跟着系统主题走 ——
        #    白色文字落到白色背景上，等于整片 INFO 日志隐身，只剩红/橙能看见。
        #    所以背景必须设成深色，硬编码的那套文字色才成立（见 append_log）。
        self.log_output.setStyleSheet(
            "QPlainTextEdit{"
            "  background-color:#1a1a1a;"
            "  color:#d4d4d4;"
            "  font-family:Consolas,'Courier New','Microsoft YaHei',monospace;"
            "  font-size:12px;"
            "}")

        layout.addWidget(self.log_output)
        gbox.setLayout(layout)

        # Create Qt logger handler
        self.qt_log_handler = QtLogHandler()
        self.qt_log_handler.log_signal.connect(self.append_log)
        self.qt_log_handler.setFormatter(
            logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%H:%M:%S'))

        # Add it to your logger
        logger.addHandler(self.qt_log_handler)

        return gbox

    def create_control_gbox(self):
        '''
        Creates a group box with hotkey instructions and a dropdown to select mode
        '''
        gbox = QGroupBox("🕹️ 脚本控制")
        layout = QVBoxLayout()
        layout.setSpacing(6)

        # Load Config Section
        self.load_config_error_label = create_error_label()
        layout.addWidget(self.load_config_error_label)  # Add above the button

        # 配置方案下拉框（2026-09-12 用户设计）：一个控件同时完成「新建 / 切换 / 保存」
        #   · 从列表选一项 = 加载那套配置（加载前先把当前修改落盘）
        #   · 输入新名字回车/移开焦点 = 新建：设置重置为出厂默认并立即写盘
        #   · 主界面设置项的修改由后台定时器（_autosave_tick）自动落盘，无需保存按钮
        load_config_layout = QHBoxLayout()
        load_config_layout.setSpacing(8)
        load_config_layout.setAlignment(Qt.AlignLeft)

        layout.addWidget(QLabel("配置方案:"))
        self.config_combo = ConfigCombo()
        self.config_combo.setLineEdit(ConfigComboLineEdit())
        self.config_combo.setFixedWidth(200)
        self.config_combo.setToolTip(
            "下拉选一项 = 切换到那套配置；\n"
            "直接在框里改名字回车 = 重命名当前配置；\n"
            "点旁边的 🆕 = 新建一套空方案（设置重置为出厂默认）。")
        self.config_combo.activated.connect(self._on_config_activated)
        # 回车提交：子类 keyPressEvent 发 return_pressed 信号。
        # 【为什么不用 editingFinished / eventFilter】editable QComboBox 在真机上
        # 既不发 editingFinished（吞回车），eventFilter 安装了也不触发（两次用户
        # 实测失败）—— 子类重写 keyPressEvent 是 PySide 最可靠的回车捕获层。
        self.config_combo.return_pressed.connect(self._on_config_edit_committed)
        self.config_combo.lineEdit().return_pressed.connect(self._on_config_edit_committed)
        load_config_layout.addWidget(self.config_combo)

        # 🆕 新建方案：点击后输入框清空等名字，回车即创建（用户交互模型）
        self.button_new_config = QPushButton("🆕")
        self.button_new_config.setFixedWidth(34)
        self.button_new_config.setToolTip("新建一套配置方案：设置重置为出厂默认，输入名字后回车创建。")
        self.button_new_config.clicked.connect(self._start_new_config)
        load_config_layout.addWidget(self.button_new_config)

        # 🗑 删除当前方案（2026-09-12 用户需求：防止方案越积越多）
        self.button_delete_config = QPushButton("🗑")
        self.button_delete_config.setFixedWidth(34)
        self.button_delete_config.setToolTip(
            "删除当前选中的配置方案（不可恢复，至少保留一套）。\n"
            "名字标定等资源文件不受影响。")
        self.button_delete_config.clicked.connect(self._delete_current_config)
        load_config_layout.addWidget(self.button_delete_config)
        self.label_config_path = QLabel("")

        # --- Control Buttons ---
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        button_layout.setAlignment(Qt.AlignLeft)

        # Start / Pause Button
        self.button_start_pause = QPushButton("▶ 开始 (F1)")
        self.button_start_pause.setCheckable(True)
        self.button_start_pause.clicked.connect(self.toggle_start_ui)

        # 资源制作三连（2026-09-12 用户定义的工作流）：
        # F1 挂机、F2 标名字、F3 截怪物、F4 录路线 —— 换号/换图的高频操作全部在主界面
        # ⚠️ 按钮必须连「带守卫」的入口（_hotkey_f*_guarded），**不能直连 launch_***：
        #    否则录制器运行期间热键被挂起了、点按钮却还能启动第二个工具（2026-09-12 发现）。
        self.button_nametag = QPushButton("F2 标定名字")
        self.button_nametag.setToolTip(
            "标定「角色名标签」模板 + 位置偏移 —— 换角色后必做一次。\n"
            "先在输入框填角色名，再在游戏画面里框住角色的名字行、点一下角色中心。")
        self.button_nametag.clicked.connect(self._hotkey_f2_guarded)

        self.button_template = QPushButton("F3 截怪物模板")
        self.button_template.setToolTip(
            "截取怪物模板 —— 换挂机地图时用。\n"
            "打开独立窗口：框住一只怪自动抠图存档，换个朝向再截 1~2 帧更稳。")
        self.button_template.clicked.connect(self._hotkey_f3_guarded)

        self.button_route = QPushButton("F4 录挂机路线")
        self.button_route.setToolTip(
            "录制地图路线 —— 换挂机地图时用。\n"
            "⚠️ 必须以管理员身份运行本程序，否则收不到游戏里按的按键。\n\n"
            "弹出的录制面板全程用鼠标点：\n"
            "  【开始录（我站好了）】→ 走到平台另一头点【在这折返，存下这段】\n"
            "  → 走回起点点【这是终点，保存这段】→【主线录完了，下一步】。\n\n"
            "⚠️ 去程和回程必须分成两段：挤在一段的话同一处既有向左又有向右的\n"
            "路线像素，角色会一路冲出平台。\n\n"
            "（键盘：F8 折返分段 / F3 存这段 / F6 回正线两次 / F7 放弃 / q 结束）\n"
            "录完会自动登记，界面地图列表里直接能选到（无需手改配置）。")
        self.button_route.clicked.connect(self._hotkey_f4_guarded)

        button_layout.addWidget(self.button_start_pause)
        button_layout.addWidget(self.button_nametag)
        button_layout.addWidget(self.button_template)
        button_layout.addWidget(self.button_route)

        layout.addLayout(button_layout)
        layout.addLayout(load_config_layout)

        gbox.setLayout(layout)
        return gbox
        logger.info(f"[UI] Map selected: {map_name}")

    def load_config(self, error_label, path=None):
        '''
        Load config_custom.yaml from file and apply settings to UI
        '''
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择配置文件", "", "YAML 文件 (*.yaml);;所有文件 (*)"
            )
            if not path:
                return  # User canceled

        # Validation
        if not path.endswith(".yaml"):
            error_label.setText("仅支持 .yaml 格式文件")
            error_label.setVisible(True)
            return

        # Validate the file name
        if "config_default.yaml" in path or "config_data.yaml" in path:
            error_label.setText(f"{path} cannot be loaded as customized yaml")
            error_label.setVisible(True)
            return

        error_label.setVisible(False)
        self.label_config_path.setText(path)
        self.label_config_path.setStyleSheet("color: green")

        # Load customized yaml
        try:
            self.cfg = override_cfg(self.cfg, load_yaml(path))
        except FileNotFoundError:
            logger.warning(f"[UI] Unable to find config file: {path}")
        else:
            self.path_cfg_custom = path

        # Re-apply config to UI
        self.apply_config_to_ui()

    def _merge_disk_owned_sections(self):
        '''
        保存前把磁盘 config_custom.yaml 里「主界面不管理的段」合并回内存 cfg。

        背景（2026-09-12 检查换号流程时发现）：F2 标定工具直接写磁盘 config_custom
        的 nametag 段；若主界面开着时标定，关窗/手动保存会用界面内存的旧值算差异，
        把新标定结果覆盖回旧值（换号必踩）。段级合并后：主界面管理的段
        （bot/key/攻击/buff）以界面为准，其余段（nametag 等）以磁盘为准。
        '''
        # ⚠️ 只留**一处**清单：类属性 `_MANAGED_SECTIONS`（见下面）。原来这里把同一份
        #    字面量写了两遍 —— 正是"同一判据抄两处、改一处漏一处"的典型形态
        #    （全局 AGENTS.md 第 6 条点名的故障形态）。2026-10-13 给 monster_detect
        #    加界面控件时，差点就只改了这一处、把另一处漏掉。
        managed = self._MANAGED_SECTIONS
        try:
            # 静默读（yaml.safe_load 直读）—— 本函数被 3 秒定时器反复调用，
            # 走 load_yaml 会每 3 秒打两条 INFO 刷屏日志（2026-09-12 用户实测）
            import yaml as _yaml
            with open(self.path_cfg_custom, "r", encoding="utf-8") as f:
                disk = _yaml.safe_load(f)
        except FileNotFoundError:
            return
        if not disk:
            return
        for section, values in disk.items():
            if section not in managed:
                # ⚠️ 必须**深合并**，绝不能整段替换（2026-09-12 实测踩坑，代价一整天）。
                #    用户配置是「局部配置」——只写自己改过的键。整段替换会把
                #    config_default 提供的默认键一起丢掉。本次实例：用户方案的
                #    nametag 段只有 name/offset 两个键，整段替换后 mode / diff_thres /
                #    global_diff_thres 全丢 → 引擎第一帧 KeyError('mode') →
                #    主循环线程静默死亡 → 症状「点开始、角色不动、日志全静默」。
                #    深合并的语义正好是想要的：磁盘值优先，磁盘没写的键保留默认。
                base = self.cfg.get(section)
                if isinstance(base, dict) and isinstance(values, dict):
                    override_cfg(base, values)
                else:
                    self.cfg[section] = values

    # ---- 配置方案下拉框（2026-09-12 用户设计：新建/切换/自动保存三合一）----
    # 主界面管理的段（保存时以界面为准，防止工具写盘的结果被覆盖的合并逻辑见
    # _merge_disk_owned_sections；其余段以磁盘为准）。
    # ★ 2026-10-13 加 `monster_detect`：这一段的 5 个键（stop_to_attack / approach_mob /
    #   detect_range_scale / detect_range_scale_y / diff_thres）现在有主界面控件
    #   （create_monster_gbox）。**不加进来的后果**：3 秒自动保存时
    #   _merge_disk_owned_sections 会拿磁盘上的旧值盖掉界面刚改的值
    #   ⇒ 症状是"控件点了一点反应都没有"，而且不报错（静默失效）。
    #   ⚠️ 同一段里另外那些算法内部键**不会**因此丢：`_read_monster_ui()` 是就地写这 5 个键，
    #      其余键原样留在 cfg 里（它们的值来自启动时的深合并）。
    _MANAGED_SECTIONS = ('bot', 'key', 'directional_attack', 'aoe_skill', 'buff_skill',
                         'monster_detect',
                         # ★ 2026-10-14 加 `demo_record`：有主界面控件（create_demo_gbox）。
                         #   不加进来的后果同 monster_detect —— 3 秒自动保存时
                         #   _merge_disk_owned_sections 拿磁盘旧值盖掉界面新值 ⇒
                         #   症状"勾了没反应"，且不报错（静默失效）。
                         'demo_record')

    def _config_display_name(self, path):
        return os.path.splitext(os.path.basename(path))[0]

    def _list_config_files(self):
        """config/ 下所有用户配置（排除出厂默认 / 登记表 / 引擎临时文件 /
        下划线开头的临时备份文件）。"""
        import glob
        out = []
        for f in sorted(glob.glob(os.path.join(APP_ROOT, 'config', '*.yaml'))):
            base = os.path.basename(f)
            if base in ('config_default.yaml', 'config_data.yaml', '.config_tmp.yaml'):
                continue
            if base.startswith('_'):
                continue
            out.append(f)
        return out

    def _refresh_config_combo(self):
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        for f in self._list_config_files():
            self.config_combo.addItem(self._config_display_name(f))
        self.config_combo.blockSignals(False)
        self.config_combo.setCurrentText(self._config_display_name(self.path_cfg_custom))
        self.config_combo.lineEdit().setPlaceholderText('')
        self.label_config_path.setText(self.path_cfg_custom)

    def _flush_settings(self):
        """把界面当前设置落盘到当前配置文件（与磁盘内容相同则不写，且静默读盘）。"""
        self.update_cfg_from_main_ui()
        self._merge_disk_owned_sections()
        if "config_default.yaml" in self.path_cfg_custom:
            return
        cfg_diff = get_cfg_diff(self.cfg_base, self.cfg)
        if os.path.exists(self.path_cfg_custom):
            try:
                # 静默读（同 merge：定时器每 3 秒调用，不走会打日志的 load_yaml）
                import yaml as _yaml
                with open(self.path_cfg_custom, "r", encoding="utf-8") as f:
                    existing = _yaml.safe_load(f)
                if existing == cfg_diff:
                    return  # 无变化，不写
            except Exception:
                pass
        save_yaml(cfg_diff, self.path_cfg_custom)
        logger.info(f"[界面] 设置已自动保存到 {self.path_cfg_custom}")

    def _autosave_tick(self):
        try:
            self._flush_settings()
        except Exception as e:
            logger.error(f"[界面] 自动保存失败: {e}")
        # 顺带让示范录制横幅跟着真实状态走（看门狗自动停、启动失败等路径也自愈）
        self._refresh_demo_banner()

    def _on_config_activated(self, index):
        """用户从下拉列表选中一项 = 切换到那套配置（切换前先把当前修改落盘）。
        每个分支都留日志：真机出问题时日志能精确定位断在哪一步（2026-09-12）。

        【根因记录】activated 信号传的是**选中项编号（int）**而不是文字 ——
        此前把它当文字拼路径，每次切换都去找 config/0.yaml（不存在）然后复原，
        这就是「无法切换方案」从始至终的根因（日志一击定位：config/0.yaml）。"""
        index = int(index)   # 防御：无论信号给 int 还是别的，统一按编号取文字
        name = self.config_combo.itemText(index)
        if not name:
            return
        logger.info(f"[配置切换] 下拉选择了 {name!r}（当前 {self.path_cfg_custom}）")
        self._pending_new_config = False   # 从列表选择 = 取消待新建状态
        target = f'config/{name}.yaml'
        if os.path.abspath(target) == os.path.abspath(self.path_cfg_custom):
            logger.info("[配置切换] 选中的就是当前方案，忽略")
            return
        if not os.path.exists(target):
            logger.warning(f"[配置切换] 文件不存在: {target}，复原列表")
            self._refresh_config_combo()  # 防御：列表项不存在时复原显示
            return
        self._flush_settings()  # 当前修改先落盘，切走也不丢
        if not self._ask_confirm(
                "切换配置",
                f"切换到配置「{name}」？\n当前界面的设置会先保存，然后加载该配置。",
                "切换", "取消"):
            logger.info("[配置切换] 用户在确认框点了取消")
            self._refresh_config_combo()
            return
        self.load_config(self.load_config_error_label, target)
        self._refresh_config_combo()
        self.save_ui_state()   # 立即记住当前方案（verify 类工具靠它跟随）
        self.statusBar().showMessage(f"已切换到配置「{name}」", 5000)
        self.config_combo.clearFocus()
        logger.info(f"[配置切换] 完成，当前 = {self.path_cfg_custom}")
    def _ask_confirm(self, title, text, yes_text="确定", no_text="取消"):
        """中文按钮的确认框 —— Yes/No 英文按钮用户容易点错/随手关掉（2026-09-12）。"""
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        yes = box.addButton(yes_text, QMessageBox.YesRole)
        box.addButton(no_text, QMessageBox.NoRole)
        box.exec()
        return box.clickedButton() is yes

    def _ask_choice3(self, title, text, b1, b2, b3):
        """三选一弹窗（2026-09-12 为「给已录好的图补回正线」加）。

        返回 1/2/3 表示点了哪个按钮，0 = 关掉窗口（等同于取消）。
        b1 是推荐项（排第一个，界面上通常也最常用）；b3 一般传"取消"。
        """
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        buttons = [box.addButton(b1, QMessageBox.AcceptRole),
                   box.addButton(b2, QMessageBox.AcceptRole),
                   box.addButton(b3, QMessageBox.RejectRole)]
        box.exec()
        clicked = box.clickedButton()
        for i, b in enumerate(buttons):
            if clicked is b:
                return i + 1
        return 0

    def _delete_current_config(self):
        """🗑 删除当前选中的配置方案（不可恢复；至少保留一套；资源文件不动）。"""
        name = self._config_display_name(self.path_cfg_custom)
        current_path = self.path_cfg_custom
        if not self._ask_confirm(
                "删除配置方案",
                f"确定删除配置「{name}」？\n文件 {current_path} 将被移除，不可恢复。\n"
                "（已标定的名字、路线、怪物模板等资源文件不受影响）",
                "删除", "取消"):
            return
        remaining = [f for f in self._list_config_files()
                     if os.path.abspath(f) != os.path.abspath(current_path)]
        if not remaining:
            QMessageBox.information(self, "不能删除",
                                    "这是最后一套配置，至少要保留一套。")
            return
        try:
            os.remove(current_path)
        except Exception as e:
            QMessageBox.warning(self, "删除失败", f"{e}")
            return
        logger.info(f"[界面] 配置方案已删除: {current_path}")
        # 自动切到剩余的第一套，界面随之刷新
        self.path_cfg_custom = remaining[0]
        self.load_config(self.load_config_error_label, remaining[0])
        self._refresh_config_combo()
        self.statusBar().showMessage(
            f"已删除「{name}」，当前配置：{self._config_display_name(remaining[0])}", 5000)
        self.config_combo.clearFocus()

    def _start_new_config(self):
        """🆕 点击后：输入框清空、进入「等待输入新方案名」状态，回车即创建。"""
        if not self._ask_confirm(
                "新建配置方案",
                "新建一套空方案？主界面设置（按键 / 攻击范围 / 怪物识别 / 增益 / 地图选择）将重置为出厂默认；\n"
                "已标定的名字、路线、怪物模板等资源文件不受影响。\n\n"
                "点「新建」后在输入框里输入新方案的名字，回车即创建。",
                "新建", "取消"):
            return
        self._pending_new_config = True
        self.config_combo.blockSignals(True)
        self.config_combo.setCurrentText('')
        self.config_combo.blockSignals(False)
        self.config_combo.lineEdit().setPlaceholderText("输入新方案名字后回车…")
        self.config_combo.setFocus()

    def _validate_config_name(self, name):
        if not re.fullmatch(r'[\w\u4e00-\u9fff-]+', name):
            QMessageBox.warning(self, "名字不合法", "只能用中文、英文、数字、下划线、横线。")
            self._refresh_config_combo()
            return False
        return True

    def _create_new_config(self, name):
        """创建名为 name 的空方案：设置重置为出厂默认，立即写盘。"""
        self._flush_settings()  # 旧配置的修改先落盘
        target = f'config/{name}.yaml'
        default = load_yaml("config/config_default.yaml")
        for section in self._MANAGED_SECTIONS:
            self.cfg[section] = copy.deepcopy(default.get(section, {}))
        self.cfg['bot']['map'] = ''
        self.path_cfg_custom = target
        self.apply_config_to_ui()
        self.list_widget_maps.clearSelection()
        self.label_map_info.setText("请选择地图:")
        self.selected_map = None
        self._refresh_config_combo()
        self._flush_settings()   # 立即生成配置文件
        self.save_ui_state()     # 立即记住当前方案
        logger.info(f"[界面] 已新建配置方案「{name}」：设置重置为出厂默认，地图选择已清空")
        self.statusBar().showMessage(f"已新建配置方案「{name}」", 5000)
        self.config_combo.clearFocus()

    def _rename_current_config(self, name):
        """把当前配置文件重命名为 name（标定等引用经 path_cfg_custom 跟随）。
        目标名字已存在时 → 询问是否直接切换到那份（用户输入已有方案名的真实意图
        通常就是切换，而不是报错卡住 —— 2026-09-12 用户实测「锁在当前方案」）。"""
        old_path = self.path_cfg_custom
        target = f'config/{name}.yaml'
        self._flush_settings()   # 先把当前内容写进旧文件，再整体改名
        if os.path.exists(target):
            if not self._ask_confirm(
                    "该名字已有一套配置",
                    f"config/{name}.yaml 已存在。\n\n"
                    f"要放弃重命名、直接切换到这套配置吗？\n"
                    f"（当前方案「{self._config_display_name(old_path)}」会先保存，不会被删除）",
                    "切换过去", "取消"):
                self._refresh_config_combo()
                return
            self._on_config_activated(name)
        else:
            self._refresh_config_combo()
            return
        try:
            os.rename(old_path, target)
        except Exception as e:
            QMessageBox.warning(self, "重命名失败", f"{e}")
            self._refresh_config_combo()
            return
        self.path_cfg_custom = target
        self._refresh_config_combo()
        logger.info(f"[界面] 配置已重命名: {old_path} → {target}")
        self.statusBar().showMessage(f"配置已重命名为「{name}」", 5000)
        self.config_combo.clearFocus()   # 恢复到「点别处」后的显示状态（光标撤出输入框）

    def _on_config_edit_committed(self):
        """下拉框输入框回车/焦点移出：按状态分发 —— 新建待输入 → 创建；否则 → 重命名。"""
        name = self.config_combo.currentText().strip()
        logger.info(f"[配置输入] 回车/移焦点提交: {name!r}（当前 {self.path_cfg_custom}，"
                    f"待新建标志 {getattr(self, '_pending_new_config', False)}）")
        self.config_combo.lineEdit().setPlaceholderText('')
        pending = getattr(self, '_pending_new_config', False)
        self._pending_new_config = False
        if not name:
            self._refresh_config_combo()
            return
        if not self._validate_config_name(name):
            return
        current = self._config_display_name(self.path_cfg_custom)
        target = f'config/{name}.yaml'
        exists = os.path.exists(target)
        if name == current:
            return  # 名字没变，忽略
        if pending and not exists:
            # 🆕 待新建 + 全新名字 → 创建空方案
            self._create_new_config(name)
        elif exists:
            # 名字已有一套配置（无论输入者意图是新建还是重命名）→ 询问是否直接切换
            self._on_config_activated(name)
        elif pending:
            self._create_new_config(name)
        else:
            self._rename_current_config(name)

    def update_atk_config_trigger_by_drop_list(self):
        '''
        Update attack config fields based on selected attack mode from drop-down.
        '''
        index = self.attack_mode.currentIndex()  # Get selected index
        if index == 0:
            atk_cfg = self.cfg["directional_attack"]
        elif index == 1:
            atk_cfg = self.cfg["aoe_skill"]
        else:
            atk_cfg = None

        if atk_cfg:
            self.attack_range_x.setText(str(atk_cfg["range_x"]))
            self.attack_cooldown.setText(str(atk_cfg["cooldown"]))
            self.attack_range_y.setText(str(atk_cfg["range_y"]))

    def apply_config_to_ui(self):
        # === 挂机方式 ===
        # 界面已无该控件（2026-10-01 移除），配置里恒为 normal。
        # 仍显式写回一次：旧配置若残留 "stand"，这里顺手纠正，免得引擎按废案跑。

    # === Attack Section ===
        atk_cfg = None
        if self.cfg["bot"]["attack"] == "directional":
            self.attack_mode.setCurrentIndex(0)
            atk_cfg = self.cfg["directional_attack"]
        elif self.cfg["bot"]["attack"] == "aoe_skill":
            self.attack_mode.setCurrentIndex(1)
            atk_cfg = self.cfg["aoe_skill"]
        else:
            # 配置里的攻击模式非法（手改配置 / 旧配置残留 / 拼写错误）。
            # 原实现会让 atk_cfg 停在 None，下一行 atk_cfg["range_x"] 直接抛
            # TypeError，界面初始化就崩、且报错完全看不出是配置问题。
            # 改为回退到默认模式并明确记日志 —— 用户至少还能打开界面去改。
            logger.error(f"[界面] 未知的攻击模式 {self.cfg['bot']['attack']!r}，"
                         f"已回退为「基础攻击」。")
            self.cfg["bot"]["attack"] = "directional"
            self.attack_mode.setCurrentIndex(0)
            atk_cfg = self.cfg["directional_attack"]
        self.attack_range_x.setText(str(atk_cfg["range_x"]))
        self.attack_cooldown.setText(str(atk_cfg["cooldown"]))
        self.attack_range_y.setText(str(atk_cfg["range_y"]))

        # === 🐲 怪物识别（2026-10-13 加）===
        self._apply_monster_ui()

        # === 🎥 示范录制（2026-10-14 加）===
        self._apply_demo_ui()

        # === Key Bindings ===
        key_cfg = self.cfg["key"]
        self.basic_attack_key.set_key(key_cfg["directional_attack"])
        self.teleport_key.set_key(key_cfg["teleport"])
        self.aoe_skill_key.set_key(key_cfg["aoe_skill"])
        self.jump_key.set_key(key_cfg["jump"])
        self.return_home_key.set_key(key_cfg["return_home"])
        # 🖱️ 自动拾取（2026-10-01 加）：用 .get 兜底，旧配置里没有这两个键
        self.pickup_key.set_key(key_cfg.get("pickup", "") or "")
        try:
            self.pickup_cooldown.setValue(
                float(key_cfg.get("pickup_cooldown", 2.0) or 2.0))
        except (TypeError, ValueError):
            self.pickup_cooldown.setValue(2.0)
        self.update_pickup_hint()

        # === Buff SKills ===
        # Set MP settings default value
        buff_cfg = self.cfg["buff_skill"]
        self.checkbox_enable_buff.setChecked(len(buff_cfg["keys"]))
        # Clear old UI rows
        for i in reversed(range(self.buff_layout.count())):
            widget = self.buff_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        self.buff_inputs.clear()
        # Add new rows based on config
        for key, cd in zip(buff_cfg["keys"], buff_cfg["cooldown"]):
            self.add_buff_row(key=key, cooldown=str(cd))  # <- new version below

        # Map Selection
        for i in range(self.list_widget_maps.count()):
            item = self.list_widget_maps.item(i)
            if item.data(Qt.UserRole) == self.cfg["bot"]["map"]:
                self.list_widget_maps.setCurrentItem(item)
                self.on_map_selected(item)  # Reuse existing logic
                break

        # Advance settings
        self.update_advance_setting_ui_from_cfg()

    def set_gbox_enabled(self, enabled: bool):
        gray_style = "color: lightgray;" if not enabled else ""
        gboxs = [
            self.attack_gbox,
            self.key_binding_gbox,
            self.buff_skill_gbox,
            self.map_selection_gbox,
            # 🎥 示范录制（2026-10-14 加）：挂机中改录制参数没有任何作用
            #   （录制器在点「开始」那一刻就建好了）⇒ 一起置灰，别让人以为改了会生效
            self.demo_gbox,
        ]
        gboxs += list(self.advance_settings_gboxes.values())
        # Apply disable + style
        for gbox in gboxs:
            gbox.setEnabled(enabled)
            if hasattr(gbox, "setStyleSheet"):
                gbox.setStyleSheet(gray_style)

    def on_tab_changed(self, index):
        tab_name = self.tabs.tabText(index)
        if tab_name in ("调试", "游戏画面", "路线地图"):
            # 游戏画面 / 路线地图两块画布都在「调试」页里（2026-10-14 起），
            # 所以进这一页就要开可视化。后两个名字只为兼容老布局留着。
            self.controller.enable_bot_viz()

        elif tab_name == "高级设置":
            # 参数页不需要实时画面；进来时把 cfg 的当前值回填到控件上
            # （主界面的改动、配置文件的手改，都能在这里看到）
            self.controller.disable_bot_viz()
            self.update_advance_setting_ui_from_cfg()

        elif tab_name == "主界面":
            self.controller.disable_bot_viz()
            self.apply_config_to_ui()
            self.refresh_map_list()   # 回主界面时立即看一眼有没有新地图

        elif tab_name == "工具箱":
            self.controller.disable_bot_viz()

        else:
            logger.error(f"[界面] 未预期的标签页: {tab_name}")
            self.controller.disable_bot_viz()

        if tab_name in TAB_WINDOW_SIZE:
            self.resize(TAB_WINDOW_SIZE[tab_name][0],
                        TAB_WINDOW_SIZE[tab_name][1])

        logger.info(f"[界面] 用户切换到标签页: {tab_name}")

    def _open_home_route_drawer(self):
        '''
        打开【🖊 手绘回正线】面板（v0.9）：手绘 route_home.png，不用真的掉下去。

        为什么走同进程对话框（设计 1.2 取舍 #4）：手绘**不需要抓帧、不需要游戏
        窗口**，开子进程反而要多一套 QProcess 双向管道；面板里那排单选、已有线
        列表、红字提示用 cv2 窗口也画不出来。

        前置检查三条一条都不能省（非程序员点了没反应最难查）：
          ① 选中地图；② 这张图存过 map.png（手绘要拿它当底图）；③ 挂机没在跑
            （面板保存会改 route_home.png，正被引擎读着的时候改会出诡异故障）。
        '''
        map_name = self.selected_map
        if not map_name:
            QMessageBox.information(self, "先选地图",
                                    "请先在上面列表里点选要画回正线的地图。")
            return
        map_dir = os.path.join("minimaps", map_name)
        if not os.path.isdir(map_dir):
            QMessageBox.warning(self, "这张图不存在", f"目录不存在：{map_dir}")
            return
        if not os.path.exists(os.path.join(map_dir, "map.png")):
            QMessageBox.warning(
                self, "这张图还没存地图",
                f"{map_dir} 里没有 map.png。\n\n"
                f"手绘回正线要拿这张图的小地图当底图才画得了 —— "
                f"先在界面上【录制挂机路线】把这张图录一遍（录完会自动存下 map.png），"
                f"再回来画回正线。")
            return
        if self.button_start_pause.isChecked():
            QMessageBox.warning(
                self, "先暂停挂机",
                "挂机正在跑，改回正线之前请先点【⏸ 暂停】。\n"
                "（正在被引擎读着的图被改掉，会出现很难查的诡异行为。）")
            return

        try:
            from tools.homeRouteDrawer import open_drawer
            saved, msg = open_drawer(map_name, cfg=self.cfg, parent=self)
        except Exception as e:
            logger.error(f"[回正线] 手绘面板打不开：{e}")
            QMessageBox.critical(self, "手绘面板打不开",
                                 f"{e}\n\n请看「📜 日志」区最后一行。")
            return
        if saved:
            self.append_log(f"[回正线] {map_name}：{msg}", logging.INFO)
            self.statusBar().showMessage(
                "【回正线】已存进 route_home.png，下次开跑就生效", 10000)
        elif msg:
            self.append_log(f"[回正线] {map_name}：{msg}", logging.WARNING)

    def delete_selected_map(self):
        '''
        删除选中的地图（2026-09-12 用户需求）：整目录删掉 + 从登记表移除。

        ⚠️ 破坏性操作，四道保险一条都不能少：
          ① 挂机 / 录制运行中禁止删（正被引擎读着的图删了会出诡异故障）；
          ② 二次确认，弹窗里列出**实际要删的路径和文件**，不玩虚的；
          ③ 先整目录备份到 minimaps/<图>.bak 再删（历史上 route3.png 就是没备份丢的）；
          ④ 删的正是当前配置在用的图 → 清空 bot.map 并明确提示重选，
             否则配置指向一个不存在的目录，下次启动报一堆错。
        '''
        import shutil
        map_name = self.selected_map
        if not map_name:
            return

        # ① 运行中禁止
        if self.button_start_pause.isChecked():
            QMessageBox.warning(self, "无法删除",
                                "挂机正在运行，请先点【⏸ 暂停】再删除地图。")
            return
        if self._recorder_proc is not None:
            QMessageBox.warning(self, "无法删除",
                                "录制器正在运行，请先结束录制再删除地图。")
            return

        map_dir = os.path.join("minimaps", map_name)
        if not os.path.isdir(map_dir):
            QMessageBox.information(self, "无需删除", f"目录不存在：{map_dir}")
            return

        # ② 二次确认：把要删的东西摊开给用户看
        try:
            files = sorted(os.listdir(map_dir))
        except Exception:
            files = []
        # v0.9「手绘回正线」的可选备注 route_home.json（只给人看的名字/说明，
        # 引擎运行时**不读**）—— 整目录删除本来就会带上它，这里单独点名是为了
        # 让用户知道它也在删除清单里，别以为"备注另存在别处、删了还在"。
        HOME_SIDE_FILES = ("route_home.json",)
        shown = "、".join(files[:8]) + ("…" if len(files) > 8 else "")
        backup_dir = map_dir + ".bak"
        reply = QMessageBox.question(
            self, "确认删除地图",
            f"确定删除地图「{map_name}」吗？\n\n"
            f"将删除整个目录：\n  {os.path.abspath(map_dir)}\n"
            f"  内含 {len(files)} 个文件：{shown}\n\n"
            f"同时会从 config/config_data.yaml 的登记表里移除它。\n"
            + (f"（其中 {('、'.join(HOME_SIDE_FILES))} 是「手绘回正线」给你看的备注，"
               f"也会一起删掉。）\n" if any(f in files for f in HOME_SIDE_FILES) else "")
            + "\n"
            f"删除前会自动备份到：\n  {os.path.abspath(backup_dir)}\n"
            f"（删错了把 .bak 改回原名就能救回来）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            logger.info(f"[删除地图] 用户取消了删除「{map_name}」")
            return

        # ③ 先备份，备份失败就不删（宁可不删也不能删了没备份）
        try:
            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir)
            shutil.copytree(map_dir, backup_dir)
        except Exception as e:
            QMessageBox.warning(self, "备份失败",
                                f"删除已取消 —— 备份到\n{backup_dir}\n失败：{e}")
            logger.error(f"[删除地图] 备份失败，已取消删除「{map_name}」: {e}")
            return
        # ③ 附：回正线备注文件单独清一次（整目录删除会带上它，这里显式删是为了
        #    留一行日志 —— 它是手绘器给人看的便利贴，漏删时下次重录会在列表里
        #    冒出坐标对不上的孤儿条目，很迷惑人）
        for side in HOME_SIDE_FILES:
            side_path = os.path.join(map_dir, side)
            if os.path.exists(side_path):
                try:
                    os.remove(side_path)
                    logger.info(f"[删除地图] 已清掉回正线备注 {side}")
                except OSError as e:
                    logger.warning(f"[删除地图] 清不掉 {side}（目录仍会整体删除）: {e}")
        try:
            shutil.rmtree(map_dir)
        except Exception as e:
            QMessageBox.warning(self, "删除失败", f"{map_dir} 删除失败：{e}")
            logger.error(f"[删除地图] 删除目录失败「{map_name}」: {e}")
            return

        # 反登记（目录已经删了，这步失败也只警告，不算删除失败）
        try:
            from src.utils.registry import unregister_map
            unregister_map(map_name)
        except Exception as e:
            logger.warning(f"[删除地图] 从登记表移除失败（目录已删除）: {e}")

        logger.info(f"[删除地图] 已删除「{map_name}」，备份在 {os.path.abspath(backup_dir)}")

        # ④ 删的正是当前在用的图 → 清空配置里的选择
        self.selected_map = ""
        self.label_map_info.setText("请选择地图:")
        if self.cfg.get("bot", {}).get("map") == map_name:
            self.cfg["bot"]["map"] = ""
            logger.warning(
                f"[删除地图] 配置里选的正是「{map_name}」，已清空 bot.map —— "
                f"请重新选一张图再开始挂机。")
            QMessageBox.information(
                self, "已删除",
                f"地图「{map_name}」已删除。\n\n"
                f"它原本就是你当前选中的挂机地图，已把配置里的选择清空 —— "
                f"请重新选一张图再开始。")

        self.refresh_map_list()

    def on_map_selected(self, item):
        # Parse English name from item text
        map_name = item.text().split(" (")[0]
        self.selected_map = map_name

        map_path = os.path.join("minimaps", map_name)
        self.label_map_info.setText(f"已选择地图: {map_path}")

    def toggle_auto_buff(self, state):
        '''
        Callback function for auto buff
        '''
        enabled = Qt.CheckState(state) == Qt.Checked
        self.buff_section_container.setVisible(enabled)
        logger.debug("[切换自动Buff] 增益区域: "
                     f"{'已启用' if enabled else '已禁用'}")

    def _preflight_check(self):
        '''
        开始挂机前的配置体检（2026-09-12）：把「开始之后才发现不打怪」这类静默坑
        在**点开始的那一刻**就摊开给用户看，而不是等他发现"怎么不打怪"再回头查日志。

        背景：本次用户报「定点打怪不工作」，查下来是 config_data.yaml 里这张图的
        怪物列表是空的 —— 引擎一只怪都认不出来，于是站桩/走路、一次都不打怪，
        而日志里只有一行 INFO 的 Loaded monsters: []。已经浪费了一整轮排查。

        Returns:
            True = 放行；False = 拦下（已弹窗说明 / 已转去截模板）
        '''
        map_name = self.selected_map or self.cfg.get("bot", {}).get("map", "")
        if not map_name:
            return True    # 没选地图：后面原有逻辑会处理，这里不重复拦
        # 配置里有图但列表没选中（比如刚启动、还没点过列表）→ 补上，
        # 否则点「去截取怪物模板」时它拿不到目标地图。
        if not self.selected_map:
            self.selected_map = map_name
        # 重读登记表：截怪物模板是**独立进程**写盘的，内存里的副本看不到
        try:
            import yaml as _yaml
            with open("config/config_data.yaml", "r", encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
        except Exception:
            return True    # 读不动就不拦，别把人卡在门外
        if (data.get("map_mobs_mapping", {}) or {}).get(map_name):
            return True    # 已登记怪 → 放行

        box = QMessageBox(self)
        box.setWindowTitle("这张图还没登记怪物")
        box.setIcon(QMessageBox.Warning)
        box.setText(
            f"地图「{map_name}」还没有登记任何怪物。\n\n"
            f"没有怪模板，引擎在画面里一只怪都认不出来 —— 开始之后角色会一直"
            f"走路 / 站桩，一次都不打怪，而且不会报错。\n\n"
            f"（这就是「打怪不工作」最常见的原因。）")
        box.setInformativeText("怪物模板是全局通用的 —— 以前截过的怪勾一下就能用，不用重截。")

        # 已有模板库非空 → 给一条"不用重截"的捷径（用户 2026-09-12：
        # 「之前不是说录好一次的怪所有图都通用么」，指的就是这条路没做）
        monster_root = os.path.join(APP_ROOT, "monster")
        btn_pick = None
        if os.path.isdir(monster_root) and any(
                os.path.isdir(os.path.join(monster_root, d))
                for d in os.listdir(monster_root)):
            btn_pick = box.addButton("从已有模板里选…", QMessageBox.ActionRole)
        btn_cap = box.addButton("去截新怪", QMessageBox.ActionRole)
        btn_go = box.addButton("仍要开始", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(btn_pick or btn_cap)
        box.exec()
        if box.clickedButton() is btn_pick:
            self.register_existing_mobs()
            return False
        if box.clickedButton() is btn_cap:
            self.launch_template_capture()
            return False
        return box.clickedButton() is btn_go

    def _refresh_demo_banner(self):
        """🎥 示范录制「交接中」横幅 —— 主界面唯一的常驻提示（2026-10-14 加）。

        为什么要有它：交接模式（`demo_record.record_only=true`）会把键盘静音，
        机器人一个键都不发（见 `src/utils/demo_recorder.py` 文件头）。原来只在日志里
        写一句，而日志区已经挪到「调试」页 ⇒ 主界面看不到，症状又变成
        「点了开始，角色一动不动」（静默失败 —— 和"抓不到游戏画面"那类症状同源）。

        判据取自**当前真实状态**（勾选着的开始按钮 + 配置里的交接模式），
        所以在任何路径之后调一次都能自愈：正常暂停、启动失败、看门狗自动停。
        """
        try:
            _d = self.cfg.get("demo_record") or {}
            _handover = bool(_d.get("enabled")) and bool(_d.get("record_only", True))
            _running = bool(self.button_start_pause.isChecked())
            _want = _handover and _running
            if _want and not self.demo_banner.isVisible():
                _out = str(_d.get("out_dir") or "debug/demo")
                self.demo_banner.setText(
                    "🎥 示范录制中 —— **键盘已静音，现在轮到你操作**（机器人不会动）。\n"
                    f"· 你正常玩就行：程序在后台录「画面 + 你按的键」，存到 {_out}/\n"
                    "· 录完按 F1（暂停）结束这一次，文件立刻落盘"
                    "（.jsonl.gz + 同名 _meta.json）。\n"
                    "· 想让机器人边打边录：把「录制时接管键盘」取消勾选后再开始。")
                self.demo_banner.setVisible(True)
                logger.info("[示范录制] 主界面已显示「轮到你操作」横幅")
            elif not _want and self.demo_banner.isVisible():
                self.demo_banner.setVisible(False)
                logger.info("[示范录制] 主界面横幅已收起")
        except Exception as e:                                # noqa: BLE001
            logger.error(f"[界面] 示范录制横幅刷新失败: {e}")

    def toggle_start_ui(self):
        if self.button_start_pause.isChecked(): # When start autobot
            self.update_cfg_from_main_ui()

            # 开始前的体检：没登记怪就先问一句，避免"开始后才发现不打怪"
            if not self._preflight_check():
                self.button_start_pause.setChecked(False)
                self.button_start_pause.setText("▶ 开始 (F1)")
                self.button_start_pause.setStyleSheet("")
                return

            # Save UI config to tmp file
            cfg_path = "config/.config_tmp.yaml"
            save_yaml(self.cfg, cfg_path)

            # 🎥 示范录制（2026-10-14 加）：开了录制就在日志里先说一句 ——
            #    `record_only: true` 时**键盘是静音的**，机器人一个键都不发。
            #    不说的话，用户点完「开始」看到角色一动不动，只会以为程序坏了
            #    （这正是 2026-10-14 那次「红框里有绿框却不打」的同类症状：静默）。
            _demo = self.cfg.get("demo_record") or {}
            if _demo.get("enabled"):
                if _demo.get("record_only", True):
                    self.append_log(
                        "[示范录制] 已开启且键盘静音 —— 这次**由你自己操作**，"
                        "机器人不会动；录完把 debug/demo/ 里的文件交给开发者。",
                        logging.WARNING)
                else:
                    self.append_log(
                        "[示范录制] 已开启但**键盘没有静音** —— 机器人会照常挂机，"
                        "录到的样本里混着机器人的操作（只有想对比时才这么用）。",
                        logging.WARNING)

            # Start AutoBot
            ret = self.controller.start_bot(cfg_path)

            if ret == 0: # Start success
                # 🎥 录制中就在按钮上写出来 —— 这是用户唯一一直在看的地方
                self.button_start_pause.setText(
                    "⏸ 暂停 (F1) · 录制中" if (self.cfg.get("demo_record") or {}).get("enabled")
                    else "⏸ 暂停 (F1)")
                self.button_start_pause.setStyleSheet("background-color: lightgreen;")
                self.set_gbox_enabled(False)
            elif ret == -2:
                # 按键驱动不可用（2026-09-27 加）——
                # 不弹这个窗的话，用户看到的就是「点开始、角色一动不动」，
                # 只能去日志里猜那句英文报错是什么意思。
                self.button_start_pause.setChecked(False)
                from src.input.InterceptionController import how_to_install_driver
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle("还差一步：按键驱动没装")
                box.setText(
                    "程序已经装好了，但**按键驱动**还需要单独装一次 ——"
                    "这是两个不同的东西。\n\n"
                    "没有它，工具按不动你的角色（表现就是「点了开始，角色一动不动」）。")
                box.setInformativeText(how_to_install_driver())
                box.exec_()
            elif ret == -3:
                # 没有可用路线（2026-09-27 加）——
                # 不拦的话症状是「点开始后角色一动不动、日志也不刷」，
                # 而前台守卫还在抢窗口，用户以为是程序卡死，只能去任务管理器强杀。
                self.button_start_pause.setChecked(False)
                self.button_start_pause.setText("▶ 开始 (F1)")
                self.button_start_pause.setStyleSheet("")
                _m = self.selected_map or "（未选择地图）"
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle("这张图还没有可用路线")
                box.setText(
                    f"地图「{_m}」**没有一条可用的路线**，所以没能开始挂机。\n\n"
                    "如果就这么开始，角色会一动不动（看不出原因），"
                    "所以这里先拦下来了。")
                box.setInformativeText(
                    "常见原因：\n"
                    "　· 路线没有**终点标记**（录制时没走到最后一步就保存了）\n"
                    "　· 这张图根本没录过路线\n"
                    "　· 去程和回程叠在了同一段里\n\n"
                    "**怎么修**：按 F4 重录这张图 ——\n"
                    "　起点开始走 → 中途要折返就按一次分段 → "
                    "**走到终点按 F3 保存**（这一步才会盖上终点标记）。\n\n"
                    "日志区里有 `[路线校验]` 打出的具体原因，照着看更准。")
                box.exec_()
            else:
                # Start failed
                self.button_start_pause.setChecked(False)
                # 没有这一句的话，用户只会看到按钮自己弹回来、不知道为什么（2026-09-12）
                self.statusBar().showMessage(
                    "启动失败 —— 请看「📜 日志」区最后一行写的具体原因", 10000)

        else: # When pause autobot
            self.button_start_pause.setText("▶ 开始 (F1)")
            self.button_start_pause.setStyleSheet("")
            self.controller.pause_bot()
            self.set_gbox_enabled(True)
            clear_debug_canvas(self.debug_canvas) # Set debug viz to null
            clear_debug_canvas(self.route_map_canvas) # Set debug viz to null

        # 开始/暂停两条路都走这里：横幅按"当前真实状态"自愈（含启动失败那一支）
        self._refresh_demo_banner()

    def launch_nametag_calibration(self):
        # 名字在主界面的图形对话框里输入（中文输入法友好），工具拿 --name 直进框选，
        # 黑色控制台窗口只显示进度、不需要任何输入（cmd 问答已废除，2026-09-12）
        name, ok = QInputDialog.getText(
            self, "标定名字标签",
            "输入你角色的名字（要和游戏里显示的一模一样）：")
        if not ok or not name.strip():
            return
        name = name.strip()
        # 标定结果写进「当前配置」——新建配置起了名字的话就写那一份，保证同一套流程
        self._spawn_console(['tools.calibrate_nametag', '--name', name,
                             '--config', self.path_cfg_custom], pause=True)

    def launch_template_capture(self):
        # 必须先选地图：怪物模板保存时自动登记到该图的打怪列表（--map 静默传入，
        # 框选过程中零打断 —— 控制台问询曾导致窗口卡死，2026-09-12 用户实测）
        if not getattr(self, 'selected_map', None):
            # 2026-09-26 改：旧提示只说「请先选地图」。对新用户来说地图列表是**空的**
            # （出厂 config_data.yaml 是空表，地图要靠 F4 录路线才产生），
            # 他只会在列表里找不到东西、然后卡住 —— 所以这里要分两种人说：
            # 列表非空 = 忘了点选；列表为空 = 先去录一条路线。
            maps = [self.list_widget_maps.item(i).text()
                    for i in range(self.list_widget_maps.count())]
            if maps:
                QMessageBox.information(
                    self, "先选地图",
                    "请先在下方地图列表里点选要挂机的地图，再截怪物模板。\n\n"
                    f"当前可选：{('、'.join(maps[:8]))}{' …' if len(maps) > 8 else ''}")
            else:
                QMessageBox.information(
                    self, "还没有任何地图 —— 请先录一条路线",
                    "截怪物模板时必须知道「这只怪属于哪张图」，所以要先有地图。\n\n"
                    "而你还没有任何地图。地图是在**录挂机路线**时自动创建的：\n\n"
                    "　1. 确认本程序是「以管理员身份运行」（否则收不到游戏里的按键）\n"
                    "　2. 回到主界面按 F4（或点「录路线」），在游戏里走一遍挂机路线并保存\n"
                    "　3. 关闭界面重新打开 —— 刚录的图就会出现在下方地图列表里\n"
                    "　4. 点选它，再回来按 F3 截怪\n\n"
                    "（详细步骤见上方「换一张新图挂机 —— 照着做 5 步」。）")
            return
        # 可编辑下拉：monster/ 里已截过的怪直接选（历史怪物库），也可输入新名字
        mobs = sorted((d for d in os.listdir(os.path.join(APP_ROOT, "monster"))
                       if os.path.isdir(os.path.join(APP_ROOT, "monster", d))),
                      key=str.lower)
        name, ok = QInputDialog.getItem(
            self, "截取怪物模板",
            f"截哪一种怪？（将登记到「{self.selected_map}」的打怪列表）",
            mobs, 0, True)
        if not ok or not name.strip():
            return
        self._spawn_console(['tools.template_capture', '--name', name.strip(),
                             '--map', self.selected_map], pause=True)

    def register_from_library(self):
        '''📦 从内置素材库装怪并登记到当前地图（2026-09-29）。

        用户选完地图 → 点这个 → 想让这张图打哪几种怪就勾哪几种 →
        模板自动装进 monster/，同时登记进 map_mobs_mapping。

        为什么要它：原流程要求用户「开游戏 → 找到怪 → 框住它」才能有模板，
        而国服怀旧服怪种类多，用户不可能每种都截。素材库随包发一批现成的，
        选图后勾一下就能开打 —— 这是「选图后不打怪」最常见的根因的解。
        '''
        map_name = getattr(self, 'selected_map', None)
        if not map_name:
            QMessageBox.information(self, "先选地图",
                                    "请先在地图列表里点选要挂机的地图。")
            return

        try:
            from src.utils.mob_library import load_index, installed_mobs
        except Exception as e:
            QMessageBox.warning(self, "素材库不可用", f"{e}")
            return

        index = load_index()
        if not index:
            QMessageBox.information(
                self, "素材库是空的",
                "没找到 monster_lib/ 里的素材。\n\n"
                "你仍然可以用【截取怪物模板】自己截怪（截一次，所有图通用）。")
            return

        have = installed_mobs()

        # 当前这张图已登记的（从磁盘重读，登记表可能被别的进程改过）
        try:
            import yaml as _yaml
            from src.utils.registry import REG_PATH
            with open(REG_PATH, "r", encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            registered = set((data.get("map_mobs_mapping", {}) or {}).get(map_name) or [])
        except Exception:
            registered = set()

        dlg = QDialog(self)
        dlg.setWindowTitle(f"「{map_name}」从素材库选怪")
        dlg.setMinimumWidth(500)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"勾选这张图会出现的怪（素材库 {len(index)} 种，按等级排序）：\n"
            "· 已装好的会自动登记到这张图\n"
            "· 没装过的会自动装好再登记\n"
            "· 库里没有的怪，仍可用【截取怪物模板】自己截"))

        # 搜索框：82 种怪滚动找太累，直接过滤
        edit_filter = QLineEdit()
        edit_filter.setPlaceholderText("输入怪名过滤（如：猪、蘑菇、Lv30）")
        v.addWidget(edit_filter)

        lw = QListWidget()
        lw.setSelectionMode(QListWidget.MultiSelection)

        def _lv_key(nm):
            lv = (index.get(nm) or {}).get('level', '')
            return (int(lv) if str(lv).isdigit() else 999, nm)

        for name in sorted(index, key=_lv_key):
            ent = index.get(name) or {}
            lv = ent.get('level', '')
            tip = '  [已装]' if name in have else ''
            item = QListWidgetItem(f"Lv{lv:<4} {name}{tip}")
            item.setData(Qt.UserRole, name)
            lw.addItem(item)
            item.setSelected(name in registered)
        v.addWidget(lw)

        def _apply_filter(text):
            t = (text or '').strip().lower()
            for i in range(lw.count()):
                it = lw.item(i)
                it.setHidden(bool(t) and t not in it.text().lower())

        edit_filter.textChanged.connect(_apply_filter)

        # 常用快捷：一键选中/取消「已装」的怪
        row_btn = QHBoxLayout()
        btn_sel_inst = QPushButton("只选已装的")
        btn_clr = QPushButton("全部取消")

        def _select_installed():
            for i in range(lw.count()):
                it = lw.item(i)
                it.setSelected(it.data(Qt.UserRole) in have)

        def _clear_all():
            for i in range(lw.count()):
                lw.item(i).setSelected(False)

        btn_sel_inst.clicked.connect(_select_installed)
        btn_clr.clicked.connect(_clear_all)
        row_btn.addWidget(btn_sel_inst)
        row_btn.addWidget(btn_clr)
        row_btn.addStretch()
        v.addLayout(row_btn)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return

        picked = {lw.item(i).data(Qt.UserRole) for i in range(lw.count())
                  if lw.item(i).isSelected()}
        if not picked:
            QMessageBox.information(
                self, "没勾选",
                "至少勾一种怪 —— 一张都不勾的话引擎认不出怪，角色不会打。")
            return

        # 先装模板（装不上的直接跳过，不让整条登记失败）
        from src.utils.mob_library import install
        installed_now, failed = [], []
        for m in sorted(picked):
            try:
                r = install(m)
            except Exception:
                r = 'missing'
            if r == 'installed':
                installed_now.append(m)
            elif r == 'missing':
                failed.append(m)

        usable = sorted(picked - set(failed))
        if not usable:
            QMessageBox.warning(self, "都没装上",
                                "选中的怪一个都没能装上，请检查 monster_lib/ 目录。")
            return

        try:
            from src.utils.registry import register_map, register_mob, unregister_mob
            register_map(map_name)
            added = [m for m in usable if m not in registered
                     and register_mob(map_name, m)]
            removed = [m for m in registered - set(usable)
                       if unregister_mob(map_name, m)]
        except Exception as e:
            QMessageBox.warning(self, "登记失败", f"{e}")
            return

        logger.info(f"[素材库登记] 「{map_name}」= {usable}"
                    f"（新装 {installed_now}，新增登记 {added}，移除 {removed}）")
        msg = f"「{map_name}」现在会打：{'、'.join(usable)}\n"
        if installed_now:
            msg += f"\n新装模板 {len(installed_now)} 种：{'、'.join(installed_now)}"
        if failed:
            msg += f"\n\n⚠️ 素材库里没有（已跳过）：{'、'.join(failed)}"
        msg += "\n\n直接点开始即可。"
        QMessageBox.information(self, "已登记", msg)

    def register_existing_mobs(self):
        '''
        从**已有**怪物库里勾选，直接登记到当前图 —— 不用重新截（2026-09-12）。

        为什么必须有：怪物模板文件 monster/<怪名>/ 是全局通用的，但引擎只加载
        config_data.yaml 里「当前图登记的」怪（map_mobs_mapping[地图]）。
        以前想给新图加一种已经截过的怪，只能走「截取怪物模板」再框一次 ——
        模板明明就在硬盘上却要重截，等于不通用（用户 2026-09-12 直接指出这点）。
        现在勾一下就完成登记；取消勾选也会真的移除。
        '''
        map_name = getattr(self, 'selected_map', None)
        if not map_name:
            QMessageBox.information(self, "先选地图",
                                    "请先在地图列表里点选要挂机的地图。")
            return

        monster_root = os.path.join(APP_ROOT, "monster")
        available = sorted((d for d in os.listdir(monster_root)
                            if os.path.isdir(os.path.join(monster_root, d))),
                           key=str.lower) if os.path.isdir(monster_root) else []
        if not available:
            QMessageBox.information(
                self, "还没有任何怪物模板",
                "monster/ 目录是空的 —— 得先截一次怪：\n"
                "点【截取怪物模板】框住一只怪存下来，之后所有图都能勾选复用。")
            return

        # 当前已登记的先勾上（登记表可能被别的进程改过，这里从磁盘重读）
        try:
            import yaml as _yaml
            from src.utils.registry import REG_PATH
            with open(REG_PATH, "r", encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
        except Exception:
            data = {}
        registered = set((data.get("map_mobs_mapping", {}) or {}).get(map_name) or [])

        dlg = QDialog(self)
        dlg.setWindowTitle(f"「{map_name}」打哪几种怪？")
        dlg.setMinimumWidth(430)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "勾选这张图会出现的怪：\n"
            "模板是全局通用的 —— 勾上就能用，不用重新截。"))
        lw = QListWidget()
        lw.setSelectionMode(QListWidget.MultiSelection)
        for name in available:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            lw.addItem(item)
            item.setSelected(name in registered)
        v.addWidget(lw)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return

        picked = {lw.item(i).data(Qt.UserRole) for i in range(lw.count())
                  if lw.item(i).isSelected()}
        if not picked:
            QMessageBox.information(
                self, "没勾选",
                "至少勾一种怪 —— 一张都不勾的话引擎认不出怪，角色不会打。")
            return

        try:
            from src.utils.registry import register_map, register_mob, unregister_mob
            register_map(map_name)
            added = [m for m in picked if m not in registered and register_mob(map_name, m)]
            removed = [m for m in registered - picked if unregister_mob(map_name, m)]
        except Exception as e:
            QMessageBox.warning(self, "登记失败", f"{e}")
            return

        logger.info(f"[怪物登记] 「{map_name}」打怪列表 = {sorted(picked)}"
                    f"（新增 {added}，移除 {removed}）")
        QMessageBox.information(
            self, "已登记",
            f"「{map_name}」现在会打：{'、'.join(sorted(picked))}\n\n"
            f"直接点开始即可，不用重新截模板。")

    def _hotkeys_suspended(self):
        """录制器运行期间，主界面的 F1~F4 全局热键**全部挂起**。

        录制器占用：F1 暂停录制 / F2 截图 / F3 存路线 / F4 存地图 —— 与主界面同名，
        所以它活着的时候主界面这四个键全部让位。
        （2026-09-12 用户报告两边 F1~F4 重叠：上一版只挂了 F2/F3/F4，**F1 漏了**。）

        2026-09-12 二次改造：录制改成「置顶分步面板 + 鼠标点」后，这里不只是防冲突 ——
        录制期间用户根本不需要主界面的热键，改成一个指向面板的提示更不容易误操作。

        判定依据是 self._recorder_alive() —— 界面 F4 启动的那个录制器 QProcess 是否在跑。
        ⚠️ 独立 bat 启动的录制器不在此列（用户 2026-09-12：那些 bat 是历史残留，
        功能全做好后会删掉；录制器今后只从界面 F4 启动，届时这里是唯一入口）。
        """
        if not self._recorder_alive():
            return False
        self.statusBar().showMessage(
            "录制进行中 —— 请用那个置顶的「录制挂机路线」面板点按钮"
            "（主界面 F1~F4 已暂挂，避免和录制器抢键）", 5000)
        return True

    def _hotkey_f1_guarded(self):
        if self._hotkeys_suspended():
            return
        self.button_start_pause.click()

    def _hotkey_f2_guarded(self):
        if self._hotkeys_suspended():
            return
        self.launch_nametag_calibration()

    def _hotkey_f3_guarded(self):
        if self._hotkeys_suspended():
            return
        self.launch_template_capture()

    def _hotkey_f4_guarded(self):
        if self._hotkeys_suspended():
            return
        self.launch_route_recorder()

    def _hotkey_f12_guarded(self):
        """F12（全局热键）= 请求关闭挂机界面 —— **先留痕、再确认、才关**。

        ⚠️ 为什么必须问一句（2026-10-14 用户真机，日志 03:40:48）：
        F12 是 pynput **全局**热键（`src/input/KeyBoardListener.py:81-83`，
        `on_press` `:109-128` 处理 F 键时**不检查前台窗口**；`run_for_autobot`
        `:222-229` 只是空转），所以在**任何窗口**误按一下，整个界面立刻关掉、
        挂机停止，而且原来连一行日志都不打 —— 事后完全看不出是谁关的
        （日志尾部就只剩 closeEvent 的 6 行：Save yaml → Save UI state → Terminated）。

        ⚠️ 与 F1~F4 的区别是**故意的**：F1~F4 在录制器运行期间要
        `_hotkeys_suspended()` 给录制器让键；录制器不用 F12，所以这里**不挂起** ——
        录制中按 F12 照样弹确认框。

        默认按钮取「取消」：误按回车不会把挂机关掉。
        """
        logger.info("[热键] 按下 F12 —— 请求关闭挂机界面（会先确认）")
        reply = QMessageBox.question(
            self, "关闭挂机界面",
            "确定要关闭挂机界面吗？\n\n"
            "· 确定 = 保存设置并**停止挂机**、退出界面\n"
            "· 取消 = 什么都不做，挂机继续\n\n"
            "（F12 是全局热键：在任何窗口按到都会走到这里）",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
        if reply != QMessageBox.Ok:
            logger.info("[热键] F12 的关闭请求已取消，挂机继续")
            return
        self.close()

    def launch_route_recorder(self):
        if not self._require_admin_for_hotkey_tools():
            return
        map_id, ok = QInputDialog.getText(
            self, "录制挂机路线 (F4)",
            "给这张图起个名字（中文名就可以，如：蘑菇村）：\n\n"
            "点确定后会弹出一个置顶小面板，每一步只显示该点的按钮，跟着点就行：\n\n"
            "· 先录主线：走到一个位置就选一次 ——\n"
            "  【在这折返，存下这段】（走到要掉头的位置）/【这是终点，保存这段】\n"
            "  ⚠️ 想让角色来回走，就录去 + 回两段，走完最后一段会自动循环回第一段\n"
            "  （收尾时尽量走回起点 10px 内，闭不上环会在日志里报警）\n\n"
            "· 再录回正线（掉出平台后走回来的路）：\n"
            "  【我在落地点】→ 走回主线 →【已回到主线】（点了自动保存）\n"
            "  ⚠️ 起点是掉下去之后的落点，不是平台边缘。\n\n"
            "全程鼠标点，地图在结束录制时自动保存并登记到地图列表。")
        if not ok:
            return
        map_id = map_id.strip()
        # 2026-09-12 放宽支持中文：中文路径 IO 已由 unicode 封装解决；
        # 只挡真正会出问题的字符（Windows 文件名非法字符 / 空格 / 点开头）
        if (not map_id
                or re.search(r'[\\/:*?"<>|\s]', map_id)
                or map_id.startswith('.')):
            QMessageBox.warning(self, "名字不合法",
                                "地图名不能为空，且不能包含空格或 \\/:*?\"<>| 这些字符。")
            return

        # 目录已存在要先问用户 —— 原来这一步是在录制器的控制台里问的，
        # 反馈搬进界面后录制器不再有控制台，改由这里弹窗（2026-09-12）
        replace = False
        append_home = False
        if os.path.isdir(resource_path(os.path.join("minimaps", map_id))):
            # 三选一（2026-09-12 用户要求）：给已录好的图补回正线，不必重录主线
            choice = self._ask_choice3(
                "这张图已经录过",
                f"「{map_id}」下面已经有路线了。要怎么办？\n\n"
                "· 补录回正线（推荐）：只录「掉下去之后走回来的路」，"
                "旧的主线路线一条都不动。\n"
                "· 清掉重录：旧路线全部删掉，从头再录一遍。",
                "补录回正线", "清掉重录", "取消")
            if choice == 1:
                append_home = True
            elif choice == 2:
                replace = True
            else:
                return

        self._start_recorder(map_id, replace, append_home)

    def _fix_window_size_to_actual(self):
        """按**当前实际窗口尺寸**改写配置里的 game_window.size / title_bar_height。

        【为什么要有这个按钮】（2026-09-27 加，issue #2 收尾）
          窗口尺寸与配置对不上时录制器一帧都拿不到（见 _activate_game_or_abort
          里的尺寸预检）。原来的提示只给了一条命令行：
              python -m tools.measure_window --write-config
          **这对打包版用户是无效的** —— 免安装包里没有 Python，也没有
          tools/ 源码，他敲不了这条命令。等于把问题原样丢回给用户。

        ⇒ 在界面里提供一键修好的入口，两种打包方式都能用：
             · 源码运行：直接调 tools.measure_window 的逻辑；
             · 打包版：走 exe 的 --tool 自调用（见 src/main.py 的 _run_tool），
               不依赖外部 Python。

        Returns:
            True = 已成功改写；False = 没改成（已弹窗说明原因）
        """
        import subprocess

        py = self._console_python()
        try:
            args = self._tool_argv('tools.measure_window', '--write-config')
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "改不了", f"没能准备测量工具：{e}")
            return False

        try:
            # 捕获输出以便把结果转述给用户（这一步只是量窗口、写配置，很快）
            r = subprocess.run([py, *args], capture_output=True, timeout=60)
            out = (r.stdout or b"").decode("utf-8", "replace")
            err = (r.stderr or b"").decode("utf-8", "replace")
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(
                self, "改配置失败",
                f"运行测量工具时出错：{e}\n\n"
                f"也可以手动改 config/config_default.yaml 里的 game_window.size：\n"
                f"　　格式是 [高, 宽]（注意是**高在前**）。")
            return False

        if r.returncode != 0:
            QMessageBox.warning(
                self, "改配置失败",
                "测量工具没能完成。\n\n"
                f"它说：\n{err.strip() or out.strip() or '（没有任何输出）'}\n\n"
                "常见原因：游戏没开，或「游戏窗口标题」设置不对。")
            return False

        # 重新读配置，让界面用上新的尺寸
        try:
            from src.utils.common import load_yaml, override_cfg
            self.cfg = override_cfg(load_yaml("config/config_default.yaml"),
                                    load_yaml(self.path_cfg_custom))
        except Exception:                                        # noqa: BLE001
            pass   # 读不回来也不影响已经写进磁盘的配置

        # 从工具输出里挑出关键几行转述（别把整篇日志糊给用户）
        keep = [ln for ln in out.splitlines()
                if any(k in ln for k in ("客户区", "窗口标题", "已写入", "size",
                                         "标题栏", "✓", "✗"))]
        detail = "\n".join(keep[:8]) or out.strip()[:400]
        QMessageBox.information(
            self, "已按当前窗口大小改好配置",
            "搞定，配置已经按你现在的游戏窗口大小改好了。\n\n"
            "现在再按一次 F4 就能正常录制（如果游戏窗口之后又改了大小，"
            "重新点一次这个按钮即可）。\n\n"
            f"工具输出摘要：\n{detail}")
        return True

    def _activate_game_or_abort(self):
        """启动录制器前，先把前台交给游戏窗口，并**等到确认真的切过去**。

        ⚠️ 为什么必须在启动录制器**之前**做（2026-09-12 用户指出，原话：
           「打开录制器必须要输入地图名字，输完了才会自动聚焦到游戏窗口，
            这中间的延迟肯定不止 1 帧了，你必然失败」）：

           点 F4 → 弹出 QInputDialog 输入地图名（此时**主界面**是前台）
           → 输完名字、录制器才启动 → 录制器第一帧就要用
             `get_minimap_loc_size` 建世界坐标系。
           这段"输名字 + 切窗口"的延迟远不止一帧，第一帧抓到的会是主界面
           或桌面 → 返回 None → run_once 每帧 return -1 → **stdin 命令
           永远不被消费** → 面板按钮"按了没反应"。

           所以不能只在录制器里"第一帧失败就重试/退出"，而要**从源头保证
           第一帧抓到的就是游戏画面** —— 先切前台、轮询确认，确认到了再启动。

        Returns:
            True  = 游戏窗口已在前台，可以启动录制器
            False = 切不过去（已弹窗提示用户）
        """
        title = self.cfg["game_window"].get("title", "")

        # ── 最小化检查（2026-09-15 加，实测踩出来的）────────────────────────
        # Windows 的抓屏 API（Graphics Capture）**抓不到最小化的窗口** —— 一帧都不会来。
        # 而录制器那边只会笼统地说"还没抓到游戏画面"，用户根本想不到是窗口被最小化了：
        # 实测用户卡了 1 分 43 秒，日志里只有一行"还没抓到游戏画面，等待中…"，
        # 于是他去查颜色配置、查残留进程，全查错了方向。
        # ⇒ 在启动录制器**之前**就把这种情况拦下来，明确告诉他怎么解决。
        try:
            import win32gui
            import win32con
            from src.utils.common import find_game_window_hwnd
            _hwnd = find_game_window_hwnd(title)
            if _hwnd and win32gui.IsIconic(_hwnd):
                # 尽力还原一次（跨进程 ShowWindow 不一定成功，实测 Unity 游戏就没成功）
                win32gui.ShowWindow(_hwnd, win32con.SW_RESTORE)
                QApplication.processEvents()
                time.sleep(0.6)
                if win32gui.IsIconic(_hwnd):
                    QMessageBox.warning(
                        self, "游戏窗口是最小化的",
                        "抓屏抓不到最小化的窗口 —— 这样录制器一帧画面都拿不到。\n\n"
                        "请点任务栏上的游戏窗口把它还原（别最小化），然后再点 F4。\n\n"
                        "录制期间也请保持游戏窗口是打开的（被别的窗口挡住没关系，"
                        "最小化不行）。")
                    return False
        except Exception:
            pass   # 查不了就照旧流程走 —— 不因为这段新检查把路堵死

        def _is_foreground():
            try:
                import pygetwindow as gw
                w = gw.getActiveWindow()
                return bool(title) and w is not None and title in w.title
            except Exception:
                return False   # 拿不到前台信息：下面的 activate 仍会兜底

        # ── 窗口尺寸预检（2026-09-27 加，issue #2 的真因）─────────────────────
        # 为什么必须在启动录制器**之前**查：
        #   config 的 game_window.size 是**客户区尺寸**，录制器抓帧后会按它裁剪
        #   （crop_frame_to_client）。窗口尺寸对不上 → 每帧裁剪都返回 None →
        #   录制器刷 10 秒"还没抓到游戏画面"后 sys.exit(2)。
        #   而它给出的提示原来是"等待游戏窗口聚焦"——把用户引去查前台、查双屏，
        #   真因（窗口比配置小）完全看不出来（issue #2 实测就是这么卡住的：
        #   配置 1366x768、实际客户区 1282x707）。
        # ⇒ 在这里就拦下来，直接告诉他差多少、怎么调。
        try:
            import win32gui
            from src.utils.common import find_game_window_hwnd
            _hwnd = find_game_window_hwnd(title)
            _size = self.cfg["game_window"].get("size")     # [高, 宽]
            if _hwnd and _size:
                _cl = win32gui.GetClientRect(_hwnd)
                _cw, _ch = int(_cl[2] - _cl[0]), int(_cl[3] - _cl[1])
                _th, _tw = int(_size[0]), int(_size[1])
                if (_cw, _ch) != (_tw, _th):
                    # ⚠️ 2026-09-27 收尾修：原来的提示只给了一条命令行
                    #    `python -m tools.measure_window --write-config`，
                    #    而**打包版用户没有 Python、也没有 tools/ 源码**，
                    #    敲不了这条命令 —— 等于把问题原样丢回给用户。
                    #    现在改成给「一键修好」的按钮（两种打包都能用）。
                    box = QMessageBox(self)
                    box.setIcon(QMessageBox.Warning)
                    box.setWindowTitle("游戏窗口尺寸不对")
                    box.setText(
                        f"游戏窗口的画面区域（不含最上面那条标题栏）现在的大小是\n"
                        f"　　{_cw} x {_ch}（宽 x 高）\n"
                        f"而配置里写的是 {_tw} x {_th}。\n\n"
                        f"录制器要按配置的尺寸去裁画面，对不上就一帧都拿不到 ——\n"
                        f"这就是它报「抓不到游戏画面」的真正原因。\n\n"
                        f"注意：这跟「前台」「双屏」都没关系，不用去查那些。")
                    btn_fix = box.addButton("一键按当前窗口大小改配置（推荐）",
                                            QMessageBox.AcceptRole)
                    box.addButton("我自己把窗口调成上面那个尺寸", QMessageBox.RejectRole)
                    box.exec_()
                    if box.clickedButton() is btn_fix:
                        # 就地修好，然后**继续**启动录制器（不必让用户再按一次 F4）
                        if self._fix_window_size_to_actual():
                            return self._activate_game_or_abort()
                    return False
        except Exception:
            pass   # 查不了就照旧流程走 —— 不因为这段新检查把路堵死

        if not _is_foreground():
            try:
                from src.utils.common import activate_game_window
                # ⚠️ 2026-09-27 修（issue #2）：**必须看返回值**。
                #    原实现只 except 异常，而 activate_game_window 在"切不过去"时
                #    **不抛异常、只返回 False**（它对常规/强制两条路都试过、
                #    各自核对过 GetForegroundWindow 才给结论）。
                #    于是切换失败时这里照样往下走、照样启动录制器 ——
                #    录制器第一帧抓到的是主界面，用户看到的就是"明明没切过去却开录了"。
                _ok = activate_game_window(title)
                if not _ok:
                    QMessageBox.warning(
                        self, "没能把游戏窗口切到前台",
                        f"试了两种办法，前台仍然不是「{title}」。\n\n"
                        f"请手动点一下游戏窗口，让它显示在最前面，然后再按 F4。\n\n"
                        f"（这一步是必要的：录制器第一帧必须抓到游戏画面，"
                        f"才能建立世界坐标系。）")
                    return False
            except Exception as e:
                QMessageBox.warning(
                    self, "找不到游戏窗口",
                    f"没法把前台切到游戏窗口（标题「{title}」）：{e}\n\n"
                    f"请确认游戏已经启动，或检查设置里的「游戏窗口标题」。")
                return False

        # Windows 切窗口不是瞬时的 —— 轮询确认（最多 5 秒）
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if _is_foreground():
                return True
            QApplication.processEvents()   # 别把界面卡死
            time.sleep(0.1)

        QMessageBox.warning(
            self, "游戏窗口还没切到前台",
            f"等了 5 秒，前台窗口仍然不是「{title}」。\n\n"
            f"录制器第一帧必须抓到游戏画面才能建立世界坐标系，抓不到就没法录。\n\n"
            f"请手动点一下游戏窗口，然后再点 F4。")
        return False

    def _start_recorder(self, map_id, replace=False, append_home=False):
        """启动路线录制器 —— 用 QProcess 而非独立控制台窗口。

        为什么改（用户 2026-09-12 要求「把录制反馈搬进界面」）：
          原来走 `_spawn_console`（CREATE_NEW_CONSOLE），反馈全在另一个黑窗里，
          用户得在两个窗口之间来回切。

        2026-09-12 二次改造（用户："不要再搞什么命令行了，做成分步骤的内置弹窗"）：
          反馈不再靠日志区，改成一个**置顶分步面板**（RecorderPanel）——
          面板上的按钮 = 命令，通过 QProcess 的 stdin 发给录制器；
          录制器把状态写回 stdout（`@@STATE ...`），面板据此换步骤提示。
          整条链路都不需要用户敲键盘或看命令行。

        副作用（都是想要的）：
          · 不再有控制台窗口；录制器自己的 cv2 预览窗口照常显示；
          · 主界面退出时 QProcess 一并结束录制器，不留孤儿进程。
        """
        # ⚠️ 必须**先**把前台交给游戏窗口，再启动录制器（2026-09-12 用户指出）
        if not self._activate_game_or_abort():
            return

        # 面板必须先建好：录制器一启动就会发初始状态，面板不在的话那行状态就白发了
        self._show_recorder_panel(map_id, append_home)

        py = self._console_python()
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_recorder_output)
        proc.errorOccurred.connect(self._on_recorder_error)
        proc.finished.connect(self._on_recorder_finished)
        # ⚠️ 打包后走 --tool 自调用（见 _tool_argv）；源码运行仍是 -m
        args = self._tool_argv('tools.routeRecorder', '--new_map', map_id)
        if replace:
            args.append('--replace')
        if append_home:
            args.append('--append-home')
        self._recorder_proc = proc
        self._recorder_map_id = map_id      # 结束时体检要用（见 _on_recorder_finished）
        # 存活兜底的两个时间戳（见 _recorder_alive）：启动那一刻先同时置位，
        # 之后每次收到输出只刷 _recorder_last_output_t。
        self._recorder_started_t = time.time()
        self._recorder_last_output_t = time.time()
        self._last_recorder_routes = 0
        self._last_recorder_home = 0    # 回正线状态：0 没录 / 1 正在录 / 2 已存
        self.append_log("[录制] 启动录制器：%s%s"
                        % (map_id,
                           "（覆盖旧路线）" if replace
                           else "（只补回正线，旧路线不动）" if append_home
                           else ""), logging.INFO)
        proc.start(py, args)
        if not proc.waitForStarted(5000):
            self.append_log("[录制] 录制器启动失败，请检查 Python 路径", logging.ERROR)
            self._recorder_proc = None
            self._close_recorder_panel()

    # ---------- 录制置顶面板 ↔ 录制器进程 的通道 ----------

    def _show_recorder_panel(self, map_id, append_home=False):
        """显示置顶分步面板（每次录制新建一个，避免上一轮的状态残留）。"""
        self._close_recorder_panel()
        self._recorder_panel = RecorderPanel(
            self._send_to_recorder, self._confirm_close_recorder,
            parent=self, append_home=append_home)
        self._recorder_panel.set_map_name(map_id)
        self._recorder_panel.update_state(0, False, False)
        self._recorder_panel.show()
        self._recorder_panel.raise_()

    def _close_recorder_panel(self):
        """收起面板（录制结束/启动失败）。幂等 —— finished 和 error 可能都来。"""
        panel = self._recorder_panel
        if panel is None:
            return
        self._recorder_panel = None
        panel.force_close()    # 主动收：不走"要结束录制吗"的确认弹窗
        panel.deleteLater()

    def _send_to_recorder(self, cmd):
        """把面板按钮变成命令发给录制器（写它的 stdin，一行一条 `@@CMD xxx`）。"""
        proc = self._recorder_proc
        if proc is None or proc.state() == QProcess.ProcessState.NotRunning:
            self.statusBar().showMessage("录制器已经结束了", 4000)
            return
        proc.write(("@@CMD %s\n" % cmd).encode('utf-8'))

    def _confirm_close_recorder(self):
        """面板被点 X：问一句是不是要结束录制。返回 True = 允许关掉面板。"""
        if not self._recorder_alive():
            return True
        panel = self._recorder_panel
        # 追加模式不存地图（用旧的 map.png），提示措辞要换 —— 否则会说错
        if panel is not None and panel.append_mode:
            return self._ask_confirm(
                "结束补录？",
                "关掉这个面板会结束录制（已经保存的回正线不会丢）。\n\n"
                "回正线起了头但还没点【保存回正线】的话，这次录的会作废。",
                yes_text="结束补录", no_text="继续录")
        return self._ask_confirm(
            "结束录制？",
            "关掉这个面板会结束录制（已经保存的路线不会丢）。\n\n"
            "还没点过【保存地图】的话，地图不会保存、这张图也不会自动登记。",
            yes_text="结束录制", no_text="继续录")

    def _apply_recorder_state(self, line):
        """解析录制器发来的状态行 → 刷新面板步骤提示。

        格式：`@@STATE routes=1 paused=0 mapsaved=0 home=0`（见 routeRecorder._emit_state）
        """
        kv = {}
        for token in line.split()[1:]:
            if '=' in token:
                k, v = token.split('=', 1)
                kv[k] = v
        if kv.get('finished') == '1':
            if self._recorder_panel is not None:
                self._recorder_panel.update_state(0, False, False, 0, finished=True)
            self.statusBar().showMessage("【录制结束】正在登记地图…", 8000)
            return
        try:
            routes = int(kv.get('routes', 0))
            paused = kv.get('paused', '0') == '1'
            map_saved = kv.get('mapsaved', '0') == '1'
            home = int(kv.get('home', '0'))
            append = kv.get('append', '0') == '1'
            sp = kv.get('sp', '0') == '1'
            wp = int(kv.get('wp', '0'))
            loopdist = int(kv.get('loopdist', '-1'))
        except ValueError:
            return
        pos = kv.get('pos')
        keys = kv.get('keys', '') or ''
        drawn = int(kv.get('drawn', 0) or 0)
        mode = kv.get('mode') or None
        # 【开始录】闸门状态（2026-09-15 加，见 routeRecorder.arm_recording）
        armed = kv.get('armed', '0') == '1'
        posok = kv.get('posok', '0') == '1'
        frameok = kv.get('frameok', '0') == '1'
        if self._recorder_panel is not None:
            self._recorder_panel.update_state(routes, paused, map_saved, home,
                                              append, sp, wp, mode,
                                              loopdist=loopdist, pos=pos,
                                              keys=keys, drawn=drawn,
                                              armed=armed, posok=posok,
                                              frameok=frameok)
        if routes > self._last_recorder_routes:
            self.statusBar().showMessage(
                f"【第 {routes} 段已保存】接着走，走完点面板上的【保存这一段】", 8000)
        self._last_recorder_routes = routes
        if home > self._last_recorder_home:
            self.statusBar().showMessage(
                {1: "【回正线】已起头 —— 现在一路走回主线，然后点【已回到主线】",
                 2: "【回正线已保存】掉出平台后会沿它走回主线（也可以直接结束录制）"}.get(home, ""),
                8000)
        self._last_recorder_home = home

    def _recorder_alive(self):
        """录制器是否正在跑 —— 热键挂起与按钮守卫的唯一判据。

        ⚠️★ 2026-09-27 修「F1 暂停键失效」：
          原实现只看 `proc.state() != QProcess.ProcessState.NotRunning`。
          这个判据在**进程异常死亡时不可靠** —— 录制器是被
          任务管理器强杀 / 崩溃 / 卡死时，Qt 的进程状态可能一直停在
          Running（正常退出走 `finished` 信号才会清零 `_recorder_proc`）。
          而 `_hotkeys_suspended()` 一旦认为"录制器还活着"，就会把
          **主界面 F1~F4 全部吞掉**（`_hotkey_f1_guarded` 直接 return）。

          症状（用户实测）：录制器结束后想按 F1 暂停挂机，**按了没反应**，
          而挂机仍在跑、还在抢前台窗口，用户只能去任务管理器强杀进程。

          ⇒ 加**存活兜底**：进程状态说"在跑"还不够，还要求它**最近有过输出**；
            超过 `_RECORDER_STALE_SEC` 秒没有任何 stdout/stderr，
            就判定它已经死了，热键立刻恢复可用。
            这样"卡死但没退出"的录制器最多挡住热键 20 秒，不会永久吞键。

        Returns:
            bool: True = 录制器仍在正常运行（此时主界面 F1~F4 让位）
        """
        proc = self._recorder_proc
        if proc is None:
            return False
        if proc.state() == QProcess.ProcessState.NotRunning:
            return False
        # 进程状态说在跑 —— 再用"最近有没有输出"确认一次（防卡死/被强杀）
        last = getattr(self, "_recorder_last_output_t", None)
        if last is None:
            # 还没收到过任何输出：以启动时刻为基准（避免刚启动就被误判为死）
            last = getattr(self, "_recorder_started_t", None)
        if last is not None and (time.time() - last) > self._RECORDER_STALE_SEC:
            logger.warning(
                f"[录制] 录制器超过 {self._RECORDER_STALE_SEC:.0f} 秒没有任何输出，"
                f"判定它已经停止响应 —— 恢复主界面 F1~F4 热键。\n"
                f"        如果它确实还在跑，请到任务管理器结束 python 进程。")
            return False
        return True

    def _on_recorder_output(self):
        """录制器的 stdout/stderr → 状态行解析（喂面板）+ 其余进日志区。"""
        proc = self._recorder_proc
        if proc is None:
            return
        text = bytes(proc.readAllStandardOutput().data()).decode('utf-8', 'replace')
        # 收到输出 = 它活着（见 _recorder_alive 的存活兜底）
        self._recorder_last_output_t = time.time()
        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                continue
            if line.startswith('@@STATE'):
                self._apply_recorder_state(line)   # 协议行：不进日志，免得刷屏
                continue
            self.append_log(line, logging.INFO)

    def _on_recorder_error(self, err):
        """录制器进程级错误（启动失败 / 崩溃）—— 没有控制台了，必须在这里说清楚。"""
        if err == QProcess.ProcessError.FailedToStart:
            self.append_log("[录制] 录制器启动失败：检查 Python 路径", logging.ERROR)
            self.statusBar().showMessage("【录制】启动失败，请看日志区", 8000)
        elif err == QProcess.ProcessError.Crashed:
            self.append_log("[录制] 录制器异常退出（崩溃）", logging.ERROR)
            self.statusBar().showMessage("【录制】录制器异常退出", 8000)
        self._recorder_proc = None
        self._close_recorder_panel()

    def _on_recorder_finished(self, code, status):
        """录制器结束：收面板 + 提示 + 立刻刷新地图列表（它退出前会自动登记）。"""
        self.append_log(f"[录制] 录制器已退出（退出码 {code}）", logging.INFO)
        self._recorder_proc = None
        # ⚠️ 顺序：体检要用"这一轮到底录没录回正线"，所以**先取走再清零**
        #    （2026-09-17 修：原来先清零，体检就分不清"这图本来就没回正线"
        #      和"刚录了一条但判废"）
        home_state = self._last_recorder_home
        self._last_recorder_routes = 0
        self._last_recorder_home = 0    # 回正线状态：0 没录 / 1 正在录 / 2 已存
        self._close_recorder_panel()
        if code == 0:
            self.statusBar().showMessage("【录制结束】地图列表已刷新", 8000)
        else:
            # 非零退出码 = 录制器自己发现严重错误后退出（如第一帧找不到小地图）。
            self.statusBar().showMessage(
                f"【录制器异常退出】退出码 {code}，请看下方日志", 8000)
        self.refresh_map_list()
        # ── 录完立刻体检（2026-09-12）──────────────────────────────────────
        # 以前只能等挂机时才发现"角色不动"，回头再查是哪一步录废的 —— 白跑一轮。
        # 这里在**录制结束的那一刻**就把结论摊开：哪条线是废的、为什么废。
        if code == 0 and getattr(self, "_recorder_map_id", None):
            self._audit_after_record(self._recorder_map_id, home_state)

    def _audit_after_record(self, map_name, home_state=0):
        """录制结束后体检刚录的这张图，有问题直接弹窗（不然用户要等到挂机才发现）。

        ⚠️ 2026-09-17：回正线状态也要摊开。原来 `ok` 只看主线（`n_main > 0`），
        所以"刚录了一条回正线、但它判废"时界面**全绿**，用户以为存好了。
        `home_state` = 这一轮录制器报的回正线状态（0 没录 / 1 正在录 / 2 已存）。
        """
        try:
            from src.utils.route_audit import audit_map
            rep_ = audit_map(map_name, self.cfg)
        except Exception as e:
            self.append_log(f"[录制体检] 体检没跑起来：{e}", logging.WARNING)
            return
        body = "\n".join(rep_["lines"]) or "  （没有 route*.png）"
        self.append_log(f"[录制体检] {map_name}\n{body}",
                        logging.INFO if rep_["ok"] else logging.ERROR)
        # 回正线一句话结论（主线正常时也要说，否则"掉下去站桩"要等真掉一次才知道）
        if rep_.get("home_ok"):
            home_txt = "，回正线可用"
        elif home_state == 2:
            home_txt = "，但刚录的回正线判废、挂机时不会启用（详见日志）"
        else:
            home_txt = "，⚠️ 这张图没有可用回正线（掉出去只能站桩打怪）"
        if rep_["ok"]:
            self.statusBar().showMessage(
                f"【录制体检】{map_name}：{rep_['n_main']} 段主线正常"
                + (f"，闭环 {rep_['loop_dist']}px" if rep_["loop_dist"] is not None else "")
                + home_txt,
                12000)
        else:
            box = QMessageBox(self)
            box.setWindowTitle("刚录的路线有问题")
            box.setIcon(QMessageBox.Warning)
            box.setText(f"地图「{map_name}」录完了，但体检没过关 —— "
                        f"现在挂机的话角色不会动。\n\n{body}")
            box.setInformativeText(
                "最常见的原因：录制时角色没动（游戏窗口没焦点，按键没进游戏）。\n"
                "重录时看面板上那行「当前 (x,y) · 按住了【右】 · 本段 N 像素」——\n"
                "按住了键但像素一直是 13（= 只有终点标记）就是角色没动。")
            box.addButton("知道了，重录", QMessageBox.AcceptRole)
            box.exec()
            return
        # ── 主线没事，但**刚录的回正线判废** → 必须弹，不然用户白掉一次才知道 ──
        if home_state == 2 and not rep_.get("home_ok"):
            home_lines = [ln for ln in rep_["lines"] if "回正线" in ln]
            box = QMessageBox(self)
            box.setWindowTitle("刚录的回正线用不了")
            box.setIcon(QMessageBox.Warning)
            box.setText("地图「%s」的主线没问题，但你刚存的回正线判废了 —— "
                        "挂机时不会启用，掉出平台只会站桩打怪。\n\n%s"
                        % (map_name, "\n".join(home_lines)))
            box.setInformativeText(
                "最常见的原因：① 起点没画在落点上；② 没沿坑底横着盖一段"
                "（只画竖线接不住）；③ 终点没画在主线的彩色像素上。\n"
                "不想开游戏的话，用【🖊 手绘回正线】画（画完当场校验覆盖率）。")
            box.addButton("知道了", QMessageBox.AcceptRole)
            box.exec()

    def update_cfg_from_main_ui(self):
        '''
        Collect setting from UI framework
        '''
        # 挂机方式：界面已无该控件（2026-10-01 移除），固定 normal
        self.cfg["bot"]["mode"] = "normal"

        def _num(text, old, cast):
            """
            解析界面里的数值；解析不了就保留原值。

            为什么需要它：本方法在关闭窗口时也会被调用（closeEvent）。
            若输入框为空（例如「打开界面后没切过标签页就直接关」，此时
            输入框尚未被 apply_config_to_ui 填值），int('') 会抛 ValueError，
            导致 closeEvent 中断、**用户改的设置完全存不下来**。
            """
            t = (text or "").strip()
            if t:
                try:
                    return cast(t)
                except (TypeError, ValueError):
                    pass
            logger.warning(f"[界面] 数值解析失败，保留原值 {old!r}（输入={text!r}）")
            return old

        # Attack setting gbox
        # 【2026-09-26 兜底】`_key()`：控件读出来是空、而配置里原本有值时，保留原值。
        #   与上面 _num() 同一个思路（数值解析失败保留原值），专门防"控件还没被
        #   apply_config_to_ui 填过值就被自动保存读走"这一类问题。
        #   注意：用户**故意**清空某个按键（想禁用瞬移等）时，cfg 里原本也是空，
        #   此时原值为空、保留结果仍为空 ⇒ 不影响"故意清空"的正常用法。
        def _key(widget, old):
            v = widget.get_key()
            if not v:
                if old:
                    logger.warning(f"[界面] 按键控件为空，保留原值 {old!r}（疑似控件未初始化）")
                return old
            return v

        if self.attack_mode.currentText() == "基础攻击":
            self.cfg["bot"]["attack"] = "directional"
            self.cfg["key"]["directional_attack"] = _key(
                self.basic_attack_key, self.cfg["key"].get("directional_attack"))
            da = self.cfg["directional_attack"]
            da["range_x"] = _num(self.attack_range_x.text(), da["range_x"], int)
            da["range_y"] = _num(self.attack_range_y.text(), da["range_y"], int)
            da["cooldown"] = _num(self.attack_cooldown.text(), da["cooldown"], float)
        elif self.attack_mode.currentText() == "AOE技能":
            self.cfg["bot"]["attack"] = "aoe_skill"
            self.cfg["key"]["aoe_skill"] = _key(
                self.basic_attack_key, self.cfg["key"].get("aoe_skill"))
            ao = self.cfg["aoe_skill"]
            ao["range_x"] = _num(self.attack_range_x.text(), ao["range_x"], int)
            ao["range_y"] = _num(self.attack_range_y.text(), ao["range_y"], int)
            ao["cooldown"] = _num(self.attack_cooldown.text(), ao["cooldown"], float)
        else:
            logger.error(f"[update_cfg_from_main_ui] Unsupported attack mode: {self.cfg['bot']['attack']}")
        # 🐲 怪物识别（2026-10-13 加）：就地写这 5 个键，**不动**同段其它键
        #   （mode / verify_* / every_n_frames 仍是算法内部参数；见 _read_monster_ui 注释）
        self._read_monster_ui()
        # 🎥 示范录制（2026-10-14 加）：同样就地写这 5 个键，**不动**同段其它键
        #   （record_keys / every_n_frames / max_frames 仍是算法内部参数）
        self._read_demo_ui()
        # Key binding gbox
        self.cfg["key"]["teleport"] = _key(
            self.teleport_key, self.cfg["key"].get("teleport"))
        self.cfg["key"]["aoe_skill"] = _key(
            self.aoe_skill_key, self.cfg["key"].get("aoe_skill"))
        self.cfg["key"]["jump"] = _key(
            self.jump_key, self.cfg["key"].get("jump"))
        self.cfg["key"]["return_home"] = _key(
            self.return_home_key, self.cfg["key"].get("return_home"))
        # 🖱️ 自动拾取（2026-10-01 加）
        #   注意 _key 的兜底语义：拿不到键时保留原值。拾取键**允许为空**（=关闭），
        #   所以这里不能用 _key 的「保留原值」兜底 —— 用户清空输入框就是要关掉它。
        self.cfg["key"]["pickup"] = self.pickup_key.get_key().strip()
        self.cfg["key"]["pickup_cooldown"] = round(self.pickup_cooldown.value(), 2)
        # Buff skills
        if not self.checkbox_enable_buff.isChecked():
            self.cfg["buff_skill"]["keys"] = []
            self.cfg["buff_skill"]["cooldown"] = []
        else:
            keys = []
            cooldowns = []
            for key_input, cd_input in self.buff_inputs:
                key = key_input.get_key().strip()
                try:
                    cd = int(cd_input.text())
                except ValueError:
                    cd = 0  # or skip this entry / log warning
                if key:
                    keys.append(key)
                    cooldowns.append(cd)
            self.cfg["buff_skill"]["keys"] = keys
            self.cfg["buff_skill"]["cooldown"] = cooldowns

        # Map selection
        self.cfg["bot"]["map"] = self.selected_map

    def update_debug_canvas(self, img):
        if img is None:
            return

        height, width, _ = img.shape
        logger.debug(f"[update_debug_canvas] Image size: {width}x{height}, Canvas size: {self.debug_canvas.width()}x{self.debug_canvas.height()}")
        
        # ⚠️ 必须显式传 bytesPerLine：不传时 Qt 会把每行字节数向上对齐到 4 的倍数。
        #    客户区宽 1366 → 每行 1366*3 = 4098 字节（不是 4 的倍数），
        #    Qt 会按 4100 读 → 每行多 2 字节、偏移逐行累积 → 画面出现**横线 + 整体斜掉**，
        #    同时颜色通道错乱看起来发灰（2026-09-12 用户反馈）。
        #    实测：不指定时 Qt bytesPerLine=4100，显式指定后=4098。
        #    前提：img 必须是 C-contiguous（引擎侧 img_frame_debug / img_route_debug 都是）。
        qimg = QImage(img.data, width, height, width * 3, QImage.Format_BGR888)
        pixmap = QPixmap.fromImage(qimg)

        # Scale the image to fit canvas while maintaining aspect ratio
        scaled_pixmap = pixmap.scaled(
                            self.debug_canvas.width(),
                            self.debug_canvas.height(),
                            Qt.KeepAspectRatio,  # 2026-09-12 改：原本 IgnoreAspectRatio 会把 1366x768 硬拉到 1502x844 导致画面变斜（比例 1.78→1.78 几乎一致，改 KeepAspectRatio 无黑边且不形变）
                            Qt.SmoothTransformation)

        self.debug_canvas.setPixmap(scaled_pixmap)

    def update_route_map_canvas(self, img):
        if img is None:
            return

        height, width, _ = img.shape
        # ⚠️ 必须显式传 bytesPerLine：不传时 Qt 会把每行字节数向上对齐到 4 的倍数。
        #    客户区宽 1366 → 每行 1366*3 = 4098 字节（不是 4 的倍数），
        #    Qt 会按 4100 读 → 每行多 2 字节、偏移逐行累积 → 画面出现**横线 + 整体斜掉**，
        #    同时颜色通道错乱看起来发灰（2026-09-12 用户反馈）。
        #    实测：不指定时 Qt bytesPerLine=4100，显式指定后=4098。
        #    前提：img 必须是 C-contiguous（引擎侧 img_frame_debug / img_route_debug 都是）。
        qimg = QImage(img.data, width, height, width * 3, QImage.Format_BGR888)
        pixmap = QPixmap.fromImage(qimg)

        scaled_pixmap = pixmap.scaled(
                            self.route_map_canvas.width(),
                            self.route_map_canvas.height(),
                            Qt.KeepAspectRatio,  # 2026-09-12 改：原本 IgnoreAspectRatio 会把 1366x768 硬拉到 1502x844 导致画面变斜（比例 1.78→1.78 几乎一致，改 KeepAspectRatio 无黑边且不形变）
                            Qt.SmoothTransformation)

        self.route_map_canvas.setPixmap(scaled_pixmap)

    def update_advance_setting_ui_from_cfg(self):
        '''
        Updates UI fields in an existing gbox to reflect the latest cfg[title].
        '''
        for title in self.cfg:
            if title in ADV_SETTINGS_HIDE: # skip hide settings
                continue
            gbox = self.advance_settings_gboxes.get(title)
            if gbox is None:
                # 2026-10-14 起高级设置只挂 ADV_TAB_GROUPS 里那三段
                # （monster_detect / potion / route / nametag）⇒ 其余段没有控件组，跳过。
                continue
            refs = getattr(gbox, "_field_refs", {})
            for key, value in self.cfg[title].items():
                widget = refs.get(key)
                if widget is None:
                    continue  # unknown field, skip

                # Checkbox
                if isinstance(value, bool) and isinstance(widget, QCheckBox):
                    widget.setChecked(value)

                # List of QLineEdits
                elif isinstance(value, (list, tuple)) and isinstance(widget, list):
                    for line, v in zip(widget, value):
                        line.setText(str(v))

                # Single numeric value
                elif isinstance(value, (int, float)) and isinstance(widget, QLineEdit):
                    widget.setText(str(value))

                # Droplist
                elif isinstance(value, str) and isinstance(widget, QComboBox):
                    index = widget.findText(value)
                    if index != -1:
                        widget.setCurrentIndex(index)

                # String
                elif isinstance(value, str) and isinstance(widget, QLineEdit):
                    widget.setText(value)

    def add_buff_row(self, key="", cooldown=""):
        row_widget = QWidget()
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)  # Tight spacing

        # Buff Key
        label_1 = QLabel("按 ")
        label_1.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

        key_edit = SingleKeyEdit()
        key_edit.setFixedWidth(100)
        if key:
            key_edit.set_key(key)
        key_edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

        row_layout.addWidget(label_1)
        row_layout.addWidget(key_edit)

        # Cooldown
        label_cd = QLabel(" 键，每隔 ")
        label_cd.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

        cooldown_edit = QLineEdit()
        cooldown_edit.setPlaceholderText("0")
        cooldown_edit.setText(cooldown)
        cooldown_edit.setFixedWidth(60)
        cooldown_edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

        label_second = QLabel(" 秒。")
        label_second.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

        row_layout.addWidget(label_cd)
        row_layout.addWidget(cooldown_edit)
        row_layout.addWidget(label_second)

        # Error label (optional)
        error_label = QLabel()
        error_label.setStyleSheet("color: red;")
        error_label.setVisible(False)

        # Validation
        cooldown_edit.editingFinished.connect(
            lambda: validate_numerical_input(cooldown_edit.text(), error_label, 0, 9999))

        # Delete button
        button_delete = QPushButton("-")
        button_delete.setFixedWidth(20)
        button_delete.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        button_delete.clicked.connect(lambda: self.remove_buff_row(row_widget, error_label))
        row_layout.addWidget(button_delete)

        # Align to left and fix widget height
        row_layout.setAlignment(Qt.AlignLeft)
        row_widget.setLayout(row_layout)
        row_widget.setFixedHeight(28)  # Optional: reduce vertical height

        self.buff_layout.addWidget(error_label)
        self.buff_layout.addWidget(row_widget)
        self.buff_inputs.append((key_edit, cooldown_edit))

    def remove_buff_row(self, row_widget: QWidget, error_label: QLabel):
        # Remove from layout_main
        self.buff_layout.removeWidget(row_widget)
        self.buff_layout.removeWidget(error_label)
        row_widget.setParent(None)
        error_label.setParent(None)

        # Remove from tracking list
        self.buff_inputs = [
            entry for entry in self.buff_inputs if entry[0] != row_widget
        ]

    def append_log(self, message: str, level: int):
        # ⚠️ 这三个色是配 **深色背景**（#1a1a1a，见 create_log_gbox）调的，别改回 white。
        #    2026-09-13 用户实测：这里写 QColor("white") 而日志区没设背景时，
        #    白字落在白底上，INFO 日志整片看不见，只剩 ERROR/WARNING 看得见。
        color = QColor("#d4d4d4")          # INFO：浅灰，在深底上清晰
        if level >= logging.ERROR:
            color = QColor("#ff5555")      # ERROR：红（纯红在深底上发暗，提亮一档）
        elif level >= logging.WARNING:
            color = QColor("#ffb86c")      # WARNING：橙

        fmt = QTextCharFormat()
        fmt.setForeground(color)

        cursor = self.log_output.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(message + "\n", fmt)
        self.log_output.setTextCursor(cursor)
        self.log_output.ensureCursorVisible()

    def closeEvent(self, event):
        '''
        Call when user close the UI window
        '''
        # 🎥 录制中关窗口必须先问一句（2026-10-14 加）。
        #    为什么：录制器按 max_minutes 分段换文件，正在写的那一段只有
        #    停挂机（`MapleStoryAutoLevelUp.py:3970 self.demo.stop()`）才会收尾，
        #    直接关界面 = 当前样本丢掉、`_meta.json` 也缺一条，事后完全看不出。
        #    与 F12 那条路的分工：`_on_hotkey_close` 问的是"要不要关界面"，
        #    用户对"正在录制"完全不知情 ⇒ 这里单独就录制问一次（只在真的在录时才问）。
        try:
            _d = self.cfg.get("demo_record") or {}
            _recording = bool(_d.get("enabled")) and bool(
                self.button_start_pause.isChecked())
        except Exception:                                     # noqa: BLE001
            _recording = False
        if _recording:
            if not self._ask_confirm(
                    "正在录制示范样本",
                    "示范录制还开着 —— 现在直接关界面，"
                    "**正在录的这一段会丢**（压缩文件没写完、_meta.json 也不会有它）。\n\n"
                    "点「停止并关闭」= 先停挂机、把当前样本落盘，再退出界面。",
                    "停止并关闭", "取消"):
                logger.info("[示范录制] 关窗被用户取消 —— 接着录，界面不关")
                event.ignore()
                return
            logger.info("[示范录制] 关窗前先停挂机，把当前样本落盘")
            self.button_start_pause.setChecked(False)
            self.controller.pause_bot()
            self._refresh_demo_banner()

        # Collect current UI setting and update to self.cfg
        self.update_cfg_from_main_ui()
        # 标定等工具可能在此期间写过磁盘配置 —— 合并后再算差异，避免覆盖（§8.7 同源问题）
        self._merge_disk_owned_sections()
        # Save current UI config to config_XXXX.yaml
        if "config_default.yaml" not in self.path_cfg_custom:
            cfg_diff = get_cfg_diff(self.cfg_base, self.cfg)
            save_yaml(cfg_diff, self.path_cfg_custom)

        # Save your UI state (e.g., last loaded config path)
        self.save_ui_state()

        # Terminate all bot threads
        self.controller.terminate_bot()

        event.accept()  # Continue with the close

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
