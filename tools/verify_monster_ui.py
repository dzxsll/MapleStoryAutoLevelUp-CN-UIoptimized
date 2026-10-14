# -*- coding: utf-8 -*-
"""🐲 怪物识别界面自检（2026-10-13 加，CI 可跑，不依赖 config_data.yaml / monster/）

## 背景

用户原话（2026-10-13）：「把 monster_detect 这几个键做成主界面控件」。
这几个键此前**界面完全没有入口** —— 高级设置页签 2026-09-12 就被移除了，
只能靠 AI 手改 config_custom.yaml。

## 这个自检盯的是什么（"接线 ≠ 实现"）

新控件最容易坏的地方不是"画不出来"，而是**画出来了却没接上**，而且一声不吭：

  ① 没挂进 `setup_main_tab()`            → 界面上根本看不见；
  ② 没在 `apply_config_to_ui()` 里灌值   → 打开界面时控件是空/默认，与配置不一致；
  ③ 没在 `update_cfg_from_main_ui()` 读回 → 用户改了，保存时被丢掉；
  ④ `monster_detect` 不在 `_MANAGED_SECTIONS` 里 → 3 秒自动保存时
     `_merge_disk_owned_sections()` 会拿磁盘旧值盖掉界面新值 ⇒ 症状"改了没反应"，
     而且磁盘上不会有任何报错（典型静默失效）；
  ⑤ 有控件的键还留在 `ADV_FIELD_HIDE` 里 → 主界面"露出来"与高级设置"藏起来"两处打架。

所以本文件既做**真控件往返**（造真控件 → 灌值 → 改 → 读回），也做**源码级接线断言**。

## 为什么不直接 `MainWindow()`

`MainWindow.__init__` 第一件事是读 `config/config_data.yaml`，而它是被 gitignore 的
（仓库里只有 `config_data.blank.yaml`）⇒ CI 检出后构造直接 FileNotFoundError。
这里与 `tools/verify_mob_sight.py`（对引擎）同一套思路：**用桩实例绑真方法**
（普通类 + `getattr(MainWindow, name).__get__(stub)`），不跑 `__init__`，
也不去构造 `MainWindow` 本身（它是 QWidget 子类，`object.__new__` 会被 Shiboken 拒绝）。

用法：python -m tools.verify_monster_ui
"""
import copy
import inspect
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
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


# 有主界面控件的 6 个键（create_monster_gbox 里的那两个开关 + 四个输入框）
# ⚠️ 2026-10-14 加 `mob_engage_margin`（宽判定余量，用户 m08016 拍板 B）：
#    这个键**有真控件** ⇒ 按 ui.py 里那条不变量，它不许进 ADV_FIELD_HIDE。
UI_KEYS = ('stop_to_attack', 'approach_mob', 'detect_range_scale',
           'detect_range_scale_y', 'diff_thres', 'mob_engage_margin')


def main():
    print('=' * 72)
    print(' 自检：🐲 怪物识别（monster_detect）的主界面控件')
    print('=' * 72)

    from PySide6.QtWidgets import QApplication, QCheckBox, QGroupBox, QLineEdit
    import src.ui.ui as U
    from src.utils.cfg_names import cn_key
    from src.utils.common import get_cfg_diff

    _app = QApplication.instance() or QApplication([])   # noqa: F841  (offscreen)

    # 桩实例：不跑 MainWindow.__init__（它要 config_data.yaml，CI 里没有），
    # 只把三个真方法绑到桩上 —— 与 tools/verify_mob_sight.py 对引擎的做法同一套思路。
    # ⚠️ 不能用 `object.__new__(MainWindow)`：MainWindow 是 QWidget 子类，
    #    Shiboken 会抛 `TypeError: object.__new__(MainWindow) is not safe`。
    class _Stub:
        pass

    obj = _Stub()
    for _name in ('create_monster_gbox', '_apply_monster_ui', '_read_monster_ui'):
        setattr(obj, _name, getattr(U.MainWindow, _name).__get__(obj, _Stub))
    obj.cfg = {}

    print('\n[1] 真控件：create_monster_gbox() 造出来的东西')
    gbox = U.MainWindow.create_monster_gbox(obj)
    check(isinstance(gbox, QGroupBox), '返回的是 QGroupBox')
    check('怪物识别' in gbox.title(), f'组框标题带「怪物识别」（{gbox.title()!r}）')
    _cbs = gbox.findChildren(QCheckBox)
    _les = gbox.findChildren(QLineEdit)
    check(len(_cbs) == 2, f'两个复选框（实际 {len(_cbs)}）')
    check(len(_les) == 4, f'四个输入框（实际 {len(_les)}）')
    check(hasattr(obj, 'monster_stop_cb') and hasattr(obj, 'monster_approach_cb'),
          '两个开关字段挂上了（monster_stop_cb / monster_approach_cb）')
    for _n in ('monster_scale_x', 'monster_scale_y', 'monster_diff_thres',
               'monster_engage_margin'):
        check(hasattr(obj, _n), f'输入框字段挂上了（{_n}）')

    print('\n[2] 真往返：灌值 → 用户改 → 读回（同段别的键一个都不许丢）')
    obj.cfg = {'monster_detect': {
        'stop_to_attack': False, 'approach_mob': False,
        'detect_range_scale': 3.5, 'detect_range_scale_y': 1.25, 'diff_thres': 0.66,
        # ↓ 算法内部键：界面不管它们，读回时**必须原样还在**
        'mode': 'contour_only', 'every_n_frames': 2, 'verify_color': True}}
    U.MainWindow._apply_monster_ui(obj)
    check(obj.monster_stop_cb.isChecked() is False, '灌值：停开关 = 关')
    check(obj.monster_approach_cb.isChecked() is False, '灌值：靠近开关 = 关')
    check(obj.monster_scale_x.text() == '3.5', f'灌值：看得远 X（{obj.monster_scale_x.text()!r}）')
    check(obj.monster_scale_y.text() == '1.25', f'灌值：看得远 Y（{obj.monster_scale_y.text()!r}）')
    check(obj.monster_diff_thres.text() == '0.66', f'灌值：识别阈值（{obj.monster_diff_thres.text()!r}）')

    obj.monster_stop_cb.setChecked(True)
    obj.monster_approach_cb.setChecked(True)
    obj.monster_scale_x.setText('2.5')
    obj.monster_scale_y.setText('1.0')
    obj.monster_diff_thres.setText('0.75')
    md = U.MainWindow._read_monster_ui(obj)
    check(md['stop_to_attack'] is True, '读回：停开关')
    check(md['approach_mob'] is True, '读回：靠近开关')
    check(md['detect_range_scale'] == 2.5, f"读回：看得远 X（{md['detect_range_scale']!r}）")
    check(md['detect_range_scale_y'] == 1.0, f"读回：看得远 Y（{md['detect_range_scale_y']!r}）")
    check(md['diff_thres'] == 0.75, f"读回：识别阈值（{md['diff_thres']!r}）")
    check(md.get('mode') == 'contour_only' and md.get('every_n_frames') == 2
          and md.get('verify_color') is True,
          '★ 同段的算法内部键一个都没被冲掉（防「整段替换」这个静默失效点）')

    print('\n[3] 兜底：没有段 / 输入框清空 / 填了非数字')
    obj.cfg = {}
    try:
        U.MainWindow._apply_monster_ui(obj)
        _ok = True
        _err = ''
    except Exception as e:          # noqa: BLE001
        _ok = False
        _err = repr(e)
    check(_ok, f'cfg 里没有 monster_detect 段 → 不抛异常（{_err}）')
    check(obj.monster_stop_cb.isChecked() is True
          and obj.monster_approach_cb.isChecked() is True
          and obj.monster_scale_x.text() == '2.0'
          and obj.monster_scale_y.text() == '1.1'
          and obj.monster_diff_thres.text() == '0.8',
          '缺键时用出厂默认（停=开 / 靠近=开 / 2.0 / 1.1 / 0.8）')
    md2 = U.MainWindow._read_monster_ui(obj)
    check(md2.get('detect_range_scale') == 2.0, '空 cfg 上 _read_monster_ui 会建段并写默认值')
    obj.monster_scale_x.setText('')
    obj.monster_diff_thres.setText('abc')
    md3 = U.MainWindow._read_monster_ui(obj)
    check(md3['detect_range_scale'] == 2.0,
          f"输入框被清空 → 保留原值，不写成 None（{md3['detect_range_scale']!r}）")
    check(md3['diff_thres'] == 0.8,
          f"填了非数字 → 保留原值（{md3['diff_thres']!r}）")

    print('\n[4] 保存链：界面值真的会进 config_custom.yaml')
    check('monster_detect' in U.MainWindow._MANAGED_SECTIONS,
          '_MANAGED_SECTIONS 里有 monster_detect'
          '（没有它 → 3 秒自动保存会用磁盘旧值盖掉界面值，且不报错）')
    _src_merge = inspect.getsource(U.MainWindow._merge_disk_owned_sections)
    check('self._MANAGED_SECTIONS' in _src_merge,
          '合并逻辑只认那一处清单（managed = self._MANAGED_SECTIONS）')
    check("'bot', 'key'" not in _src_merge,
          '★ 合并逻辑里没有第二份手抄清单（防「同一判据抄两处、改一处漏一处」）')
    base = {'monster_detect': {'stop_to_attack': True, 'approach_mob': True,
                               'detect_range_scale': 2.0, 'detect_range_scale_y': 1.1,
                               'diff_thres': 0.8, 'mode': 'contour_only'}}
    cur = copy.deepcopy(base)
    cur['monster_detect']['stop_to_attack'] = False
    cur['monster_detect']['diff_thres'] = 0.65
    diff = get_cfg_diff(base, cur)
    check(diff.get('monster_detect', {}).get('stop_to_attack') is False,
          '改了开关 → 进 diff（会被写盘）')
    check(diff.get('monster_detect', {}).get('diff_thres') == 0.65,
          '改了阈值 → 进 diff（会被写盘）')
    check('mode' not in diff.get('monster_detect', {}),
          '没改的内部键不进 diff（不会把一堆默认值写脏用户配置）')

    print('\n[5] 三处接线（缺一处 = 这个控件是装饰品）')
    _s_setup = inspect.getsource(U.MainWindow.setup_main_tab)
    check('create_monster_gbox()' in _s_setup, 'setup_main_tab() 里造了这个组框')
    check('self.monster_gbox' in _s_setup, 'setup_main_tab() 里把它加进了布局')
    _s_apply = inspect.getsource(U.MainWindow.apply_config_to_ui)
    check('_apply_monster_ui()' in _s_apply, 'apply_config_to_ui() 里灌了值')
    _s_read = inspect.getsource(U.MainWindow.update_cfg_from_main_ui)
    check('_read_monster_ui()' in _s_read, 'update_cfg_from_main_ui() 里读回了值')

    print('\n[6] 口径一致：有控件的键不进隐藏清单，且都有中文名')
    _hide = U.ADV_FIELD_HIDE.get('monster_detect', ())
    for _k in UI_KEYS:
        check(_k not in _hide, f'{_k} 不在 ADV_FIELD_HIDE 里（有控件就不该藏）')
        check(cn_key('monster_detect', _k) != _k,
              f"{_k} 有中文名（{cn_key('monster_detect', _k)}）")

    print('\n' + '=' * 72)
    print(f'结果：通过 {PASS} / 失败 {FAIL}')
    print('=' * 72)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
