# -*- coding: utf-8 -*-
"""🧭 页签布局与参数说明自检（2026-10-14 加，CI 可跑，不依赖 config_data.yaml / monster/）

## 背景

用户原话（2026-10-14）：
  「把游戏画面，路线地图，示范录制，日志，放在名为调试的选项页里，把识别与出手，
    路线、梯子与「走丢」，名字定位等参数的设置放在高级设置选项页里，并给出说明和数值参考。」

改动落点：`src/ui/ui.py`（页签重排 + 调试页 + 恢复高级设置页）、
`src/utils/ui.py`（每个参数一行：控件 + 灰字说明 + 参考值）、
`src/utils/cfg_docs.py`（说明的**新来源**：按行读原文）。

## 这个自检盯什么（每一条都能静默失效）

  ① **页签归属**：demo / 日志必须只在「调试」页；主界面若还挂着它们，用户
     点「开始」又得先往下滚（这正是重排的原因）。反过来，主界面该留的
     「怪物识别」不能顺手删掉。
  ② **调试页四区**：两块画布固定 526×216、日志窗够高 —— 只写"叠四个"但
     尺寸没钉死的话，Qt 会按内容拉伸，用户看到的和说好的不一样。
  ③ **高级设置页**：三桶（识别与出手 / 路线·梯子与走丢 / 名字定位）都在、
     「显示内部参数」默认不勾（内部行藏起来）、勾上后出现，
     以及 `_field_refs` 的取值契约（界面改了要真写回 cfg）。
  ④ **说明解析不错位**（本次最隐蔽的一条）：`config/config_default.yaml` 的说明
     写在**键上方**，而旧路径 `load_yaml_with_comments()` 取的是 ruamel 的
     `ca.items[key][2]`（键**下方**那段）⇒ 每个键显示的是**下一个键**的说明。
     页面照渲染、不报错、只是说的是别人。这里用三条正例 + 一条反例钉住。

## 为什么不直接 `MainWindow()`

`MainWindow.__init__` 先读 `config/config_data.yaml`（被 gitignore，CI 检出后不存在）。
与 `tools/verify_demo_ui.py` 同一套思路：普通类 + `getattr(MainWindow, 名字).__get__(stub)`。

用法：python -m tools.verify_ui_tabs
"""
import copy
import inspect
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:                                              # noqa: BLE001
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [OK]   {msg}')
    else:
        FAIL += 1
        print(f'  [FAIL] {msg}')


# 调试页必须有的四个区域（标题关键字）
DEBUG_AREAS = ('游戏画面', '路线地图', '示范录制', '日志')


def main():
    print('=' * 72)
    print(' 自检：🧭 页签布局（主界面 / 调试 / 高级设置 / 工具箱）与参数说明')
    print('=' * 72)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QApplication, QCheckBox, QGroupBox, QLabel,
                                   QLineEdit)
    import src.ui.ui as U
    from src.utils.cfg_docs import load_field_docs
    from src.utils.common import load_yaml, load_yaml_with_comments

    _app = QApplication.instance() or QApplication([])          # noqa: F841

    cfg_path = os.path.join(REPO_ROOT, 'config', 'config_default.yaml')
    cfg = load_yaml(cfg_path)
    field_docs, section_docs = load_field_docs(cfg_path)
    _, old_comments, old_sections = load_yaml_with_comments(cfg_path)

    # ── [1] 页签结构与归属 ────────────────────────────────────────────────
    print('\n[1] 页签：四个页签、顺序、各自的归属（源码级断言）')
    check('调试' in U.TAB_WINDOW_SIZE,
          f"TAB_WINDOW_SIZE 里有「调试」（{U.TAB_WINDOW_SIZE.get('调试')}）")
    _s_init = inspect.getsource(U.MainWindow.__init__)
    _idx = [_s_init.find(f'self.tabs.addTab({_n}, "{_t}")') for _n, _t in
            (('self.tab_main', '主界面'), ('self.tab_debug', '调试'),
             ('self.tab_advance_setting', '高级设置'), ('self.tab_toolbox', '工具箱'))]
    check(-1 not in _idx and _idx == sorted(_idx),
          f'addTab 顺序 = 主界面 → 调试 → 高级设置 → 工具箱（{_idx}）')
    check('connect(self.on_tab_changed)' in _s_init, '页签切换信号接上了')

    _s_main = inspect.getsource(U.MainWindow.setup_main_tab)
    check('create_demo_gbox' not in _s_main and 'create_log_gbox' not in _s_main,
          '★ 主界面不再摆「示范录制 / 日志」（搬去调试页，主界面只剩要填要点的）')
    check('self.monster_gbox' in _s_main, '主界面的「🐲 怪物识别」还在')

    _s_dbg = inspect.getsource(U.MainWindow.setup_debug_tab)
    for _need in ('create_demo_gbox()', 'create_log_gbox()', 'self.demo_gbox',
                  'self.log_gbox'):
        check(_need in _s_dbg, f'setup_debug_tab() 里有 {_need}')

    _s_tab = inspect.getsource(U.MainWindow.on_tab_changed)
    check('("调试", "游戏画面", "路线地图")' in _s_tab,
          '进「调试」页会开实时可视化（画布在这一页）')
    check('tab_name == "高级设置"' in _s_tab
          and 'update_advance_setting_ui_from_cfg()' in _s_tab,
          '进「高级设置」页会从 cfg 回填控件')

    _flat = [s for _, secs in U.ADV_TAB_GROUPS for s in secs]
    check(_flat == ['monster_detect', 'potion', 'route', 'nametag'],
          f'三个桶装的正是这四段（{_flat}）')
    check(all(_s in cfg for _s in _flat), '这四段在 config_default.yaml 里都存在')

    # ── 桩实例 ───────────────────────────────────────────────────────────
    class _Stub:
        pass

    obj = _Stub()
    obj.cfg = copy.deepcopy(cfg)
    obj.comments = old_comments
    obj.comments_section = old_sections
    obj.field_docs = field_docs
    obj.field_docs_section = section_docs
    obj.advance_settings_gboxes = {}
    obj.append_log = lambda *a, **k: None       # create_log_gbox 会接日志信号
    for _n in ('setup_debug_tab', '_wrap_debug_area', 'create_demo_gbox',
               'create_log_gbox', 'setup_advance_setting_tab',
               '_on_internal_params_toggled'):
        setattr(obj, _n, getattr(U.MainWindow, _n).__get__(obj, _Stub))

    # ── [2] 调试页：真控件 ───────────────────────────────────────────────
    print('\n[2] 调试页：真控件 —— 四区都在，尺寸/高度按用户要求钉死')
    tab_debug = U.MainWindow.setup_debug_tab(obj)
    _w, _h = U.DEBUG_CANVAS_SIZE
    check((obj.debug_canvas.width(), obj.debug_canvas.height()) == (_w, _h),
          f'游戏画面初始 {obj.debug_canvas.width()}×{obj.debug_canvas.height()}'
          f' = DEBUG_CANVAS_SIZE {_w}×{_h}')
    check((obj.route_map_canvas.width(), obj.route_map_canvas.height()) == (_w, _h),
          f'路线地图初始 {obj.route_map_canvas.width()}×{obj.route_map_canvas.height()}')
    check(obj.log_output.minimumHeight() >= 280,
          f'日志窗最小高度 {obj.log_output.minimumHeight()} ≥ 280'
          f'（用户："日志框可以稍微长一点"）')
    _titles = [g.title() for g in tab_debug.findChildren(QGroupBox)]
    for _area in DEBUG_AREAS:
        check(any(_area in _t for _t in _titles),
              f'调试页里有「{_area}」这一区（实际：{_titles}）')

    # ── [3] 高级设置页：三桶 + 内部参数开关 + 取值契约 ────────────────────
    print('\n[3] 高级设置页：三桶 + 「显示内部参数」+ 取值契约')
    tab_adv = U.MainWindow.setup_advance_setting_tab(obj)
    check(isinstance(obj.internal_params_cb, QCheckBox), '页顶有「显示内部参数」勾选框')
    check(obj.internal_params_cb.isChecked() is False,
          '★ 默认不勾（内部参数默认收起来，界面不至于吓人）')
    _bt = [g.title() for g in tab_adv.findChildren(QGroupBox)]
    for _need in ('识别与出手', '路线', '名字定位'):
        check(any(_need in _t for _t in _bt),
              f'桶「{_need}…」在页面上（实际：{_bt}）')
    check(sorted(obj.advance_settings_gboxes) == sorted(_flat),
          f'四段的控件组都建了（{sorted(obj.advance_settings_gboxes)}）')

    for _sec in ('monster_detect', 'potion', 'route', 'nametag'):
        _gb = obj.advance_settings_gboxes.get(_sec)
        check(_gb is not None and len(_gb._field_rows) == len(obj.cfg[_sec]),
              f'{_sec}：每个键都有一行（{len(getattr(_gb, "_field_rows", {}))}'
              f'/{len(obj.cfg[_sec])}）')

    _md = obj.advance_settings_gboxes['monster_detect']
    _hide_keys = U.ADV_FIELD_HIDE.get('monster_detect', ())
    _k_internal = _hide_keys[0]
    check(_md._field_rows[_k_internal].isHidden() is True,
          f'内部行默认隐藏（{_k_internal}）')
    U.MainWindow._on_internal_params_toggled(obj, Qt.Checked.value)
    check(_md._field_rows[_k_internal].isHidden() is False,
          '勾上「显示内部参数」后它出现')
    U.MainWindow._on_internal_params_toggled(obj, Qt.Unchecked.value)
    check(_md._field_rows[_k_internal].isHidden() is True, '取消勾选又收起来')
    for _k in ('stop_to_attack', 'approach_mob'):
        check(_md._field_rows[_k].isHidden() is False,
              f'日常旋钮不受勾选框影响（{_k} 一直可见）')

    # 取值契约：界面改了必须真写回 cfg（否则是装饰品）
    _refs = _md._field_refs
    obj.cfg['monster_detect']['approach_dy_tol'] = 60
    _refs['approach_dy_tol'].setText('77')
    check(obj.cfg['monster_detect']['approach_dy_tol'] == 77,
          '数值键写回 cfg（approach_dy_tol 60 → 77）')
    _refs['stop_to_attack'].setChecked(True)
    check(obj.cfg['monster_detect']['stop_to_attack'] is True, 'bool 键写回 cfg')
    _route_refs = obj.advance_settings_gboxes['route']._field_refs
    check(len(_route_refs) >= 30,
          f'route 段的控件大多建出来了（{len(_route_refs)} 个）')
    _potion_rows = obj.advance_settings_gboxes['potion']._field_rows
    check('hp' in _potion_rows and 'mp' in _potion_rows,
          'potion 段的 hp/mp 也在（内部行，藏起来但没丢）')

    # ── [4] 说明与参考：覆盖率 ──────────────────────────────────────────
    print('\n[4] 说明与参考：可见字段的说明覆盖率')
    from src.utils.cfg_docs import default_hint
    _missing = []
    _covered = 0
    _total = 0
    for _sec, _gb in obj.advance_settings_gboxes.items():
        for _k, _row in _gb._field_rows.items():
            if _row.isHidden():
                continue
            _total += 1
            _texts = [_l.text() for _l in _row.findChildren(QLabel)]
            # 「有说明」= 有「说明：」行，或者「参考：」里除了自动拼的出厂值
            # 还有注释自己的内容（短旋钮的注释常整句落在「参考」里，见 cfg_docs.split_doc）
            _auto_ref = '参考：' + default_hint(obj.cfg[_sec].get(_k))
            _has = any(_t.startswith('说明：') and len(_t) > 3 for _t in _texts) or \
                any(_t.startswith('参考：') and _t != _auto_ref for _t in _texts)
            if _has:
                _covered += 1
            else:
                _missing.append(f'{_sec}.{_k}')
    _cover_pct = (100.0 * _covered / _total) if _total else 0.0
    check(_cover_pct >= 95.0,
          f'可见字段说明覆盖率 {_covered}/{_total} = {_cover_pct:.1f}% ≥ 95%'
          f'（缺：{_missing[:6]}）')
    _row_enf = _md._field_rows['every_n_frames']
    _enf_texts = [_l.text() for _l in _row_enf.findChildren(QLabel)]
    check(any(_t.startswith('参考：') and '默认' in _t for _t in _enf_texts),
          '同一行给出「参考：默认 …」这类数值参考')

    # ── [5] 说明解析不错位（本次最隐蔽的一条） ───────────────────────────
    print('\n[5] 说明解析不错位（旧路径拿的是键**下方**那段，会整体错一格）')
    check('每隔几帧' in (field_docs.get('monster_detect', {}).get('every_n_frames') or ''),
          'every_n_frames 的说明是它自己的（含「每隔几帧」）')
    check('goal 像素过滤' in (field_docs.get('route', {}).get('goal_pixel_skip') or ''),
          'goal_pixel_skip 的说明是它自己的（含「goal 像素过滤」）')
    check('竖向进展窗口' in (field_docs.get('route', {}).get('ladder_still_s') or ''),
          'ladder_still_s 的说明是它自己的（含「竖向进展窗口」）')
    check('绳梯脚下' in (field_docs.get('route', {}).get('rope_nudge_s') or ''),
          'rope_nudge_s 的说明是它自己的（含「绳梯脚下」）')
    check('连续几帧' in (field_docs.get('potion', {}).get('confirm_frames') or ''),
          'confirm_frames 拿到的是**它自己那行**的行尾注释（含「连续几帧」）')
    check('每隔几帧' not in (field_docs.get('monster_detect', {}).get('detect_log') or ''),
          '反例：detect_log 不该拿到 every_n_frames 的说明')
    _old_txt = (old_comments.get('monster_detect', {}) or {}).get('detect_log', '')
    check('每隔几帧' in _old_txt,
          '反证：旧路径（load_yaml_with_comments）确实把 every_n_frames 的说明'
          '错挂到了 detect_log 上 ⇒ 这就是被修掉的静默错位')
    check('每隔几帧' in ' '.join(_enf_texts),
          '★ 页面上这一行的「说明：」用的确实是新来源（不是只有 cfg_docs 自己算对了）')

    # ── [6] 中文名兜底 ──────────────────────────────────────────────────
    print('\n[6] 中文名：页面上的标签不出现裸英文键')
    from src.utils.cfg_names import cn_key
    _naked = []
    for _sec, _gb in obj.advance_settings_gboxes.items():
        for _k, _row in _gb._field_rows.items():
            _labels = [l.text() for l in _row.findChildren(QLabel)
                       if l.text() and not l.text().startswith(('说明：', '参考：'))]
            if _labels and _labels[0] == _k and cn_key(_sec, _k) == _k:
                _naked.append(f'{_sec}.{_k}')
    check(not _naked, f'每个参数都有中文标签（裸英文键：{_naked[:6]}）')

    print('\n' + '=' * 72)
    print(f'结果：通过 {PASS} / 失败 {FAIL}')
    print('=' * 72)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
