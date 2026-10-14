# -*- coding: utf-8 -*-
"""🎥 示范录制界面自检（2026-10-14 加，CI 可跑，不依赖 config_data.yaml / monster/）

## 背景

用户原话（2026-10-14）：「我自己操作，录下我的打怪行为，以此作为数据样本来优化逻辑」。
录制器（`src/utils/demo_recorder.py`）与分析器（`tools/analyze_demo.py`）都已就位，
但 `demo_record` 这组键**界面完全没有入口** —— 高级设置页签 2026-09-12 就被移除了，
用户想录一次只能让 AI 手改 yaml。所以加了主界面「🎥 示范录制」组框。

## 这个自检盯的是什么（"接线 ≠ 实现"）

新控件最容易坏的地方不是"画不出来"，而是**画出来了却没接上**，而且一声不吭：

  ① 没挂进 `setup_main_tab()`            → 界面上根本看不见；
  ② 没在 `apply_config_to_ui()` 里灌值   → 打开界面时控件是默认态，与配置不一致
    （症状：配置里录着，界面显示没勾，用户以为没生效）；
  ③ 没在 `update_cfg_from_main_ui()` 读回 → 用户勾了，保存时被丢掉；
  ④ `demo_record` 不在 `_MANAGED_SECTIONS` 里 → 3 秒自动保存时
    `_merge_disk_owned_sections()` 会拿磁盘旧值盖掉界面新值 ⇒ 症状"勾了没反应"，
    而且磁盘上不会有任何报错（典型静默失效）；
  ⑤ 有控件的键还留在 `ADV_FIELD_HIDE` 里 → 主界面"露出来"与高级设置"藏起来"两处打架。

所以本文件既做**真控件往返**（造真控件 → 灌值 → 改 → 读回），也做**源码级接线断言**。

## 为什么不直接 `MainWindow()`

`MainWindow.__init__` 第一件事是读 `config/config_data.yaml`，而它是被 gitignore 的
（仓库里只有 `config_data.blank.yaml`）⇒ CI 检出后构造直接 FileNotFoundError。
这里与 `tools/verify_monster_ui.py` 同一套思路：**用桩实例绑真方法**
（普通类 + `getattr(MainWindow, name).__get__(stub)`），不跑 `__init__`。

用法：python -m tools.verify_demo_ui
"""
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


# 有主界面控件的 5 个键（create_demo_gbox 里的三个开关 + 两个输入框）
UI_KEYS = ('enabled', 'record_only', 'gzip', 'out_dir', 'max_minutes')

# 同段里"界面不管"的内部键：读回时必须原样还在（防「整段替换」这个静默失效点）
INTERNAL_KEYS = {'record_keys': True, 'every_n_frames': 2, 'max_frames': 0}


def main():
    print('=' * 72)
    print(' 自检：🎥 示范录制（demo_record）的主界面控件')
    print('=' * 72)

    from PySide6.QtWidgets import QApplication, QCheckBox, QGroupBox, QLineEdit
    import src.ui.ui as U
    from src.utils.cfg_names import cn_key, cn_section

    _app = QApplication.instance() or QApplication([])   # noqa: F841  (offscreen)

    class _Stub:
        pass

    obj = _Stub()
    for _name in ('create_demo_gbox', '_apply_demo_ui', '_read_demo_ui'):
        setattr(obj, _name, getattr(U.MainWindow, _name).__get__(obj, _Stub))
    obj.cfg = {}

    print('\n[1] 真控件：create_demo_gbox() 造出来的东西')
    gbox = U.MainWindow.create_demo_gbox(obj)
    check(isinstance(gbox, QGroupBox), '返回的是 QGroupBox')
    check('示范录制' in gbox.title(), f'组框标题带「示范录制」（{gbox.title()!r}）')
    _cbs = gbox.findChildren(QCheckBox)
    _les = gbox.findChildren(QLineEdit)
    check(len(_cbs) == 3, f'三个复选框（实际 {len(_cbs)}）')
    check(len(_les) == 2, f'两个输入框（实际 {len(_les)}）')
    for _n in ('demo_enabled_cb', 'demo_record_only_cb', 'demo_gzip_cb',
               'demo_out_dir', 'demo_max_minutes'):
        check(hasattr(obj, _n), f'字段挂上了（{_n}）')
    check('键盘' in obj.demo_record_only_cb.toolTip()
          or '静音' in obj.demo_record_only_cb.toolTip(),
          '★「接管键盘」的提示里写清了"机器人不出手"'
          '（不写清，用户会以为只是顺手录一下）')

    print('\n[2] 真往返：灌值 → 用户改 → 读回（同段别的键一个都不许丢）')
    obj.cfg = {'demo_record': dict(
        {'enabled': True, 'record_only': False, 'gzip': False,
         'out_dir': 'debug/我的样本', 'max_minutes': 5}, **INTERNAL_KEYS)}
    U.MainWindow._apply_demo_ui(obj)
    check(obj.demo_enabled_cb.isChecked() is True, '灌值：总开关 = 开')
    check(obj.demo_record_only_cb.isChecked() is False, '灌值：接管键盘 = 关')
    check(obj.demo_gzip_cb.isChecked() is False, '灌值：压缩 = 关')
    check(obj.demo_out_dir.text() == 'debug/我的样本',
          f'灌值：目录（{obj.demo_out_dir.text()!r}）')
    check(obj.demo_max_minutes.text() == '5',
          f'灌值：单文件分钟（{obj.demo_max_minutes.text()!r}）')

    obj.demo_enabled_cb.setChecked(True)
    obj.demo_record_only_cb.setChecked(True)
    obj.demo_gzip_cb.setChecked(True)
    obj.demo_out_dir.setText('debug/demo')
    obj.demo_max_minutes.setText('30')
    dd = U.MainWindow._read_demo_ui(obj)
    check(dd['enabled'] is True, '读回：总开关')
    check(dd['record_only'] is True, '读回：接管键盘')
    check(dd['gzip'] is True, '读回：压缩')
    check(dd['out_dir'] == 'debug/demo', f"读回：目录（{dd['out_dir']!r}）")
    check(dd['max_minutes'] == 30, f"读回：单文件分钟（{dd['max_minutes']!r}）")
    check(isinstance(dd['max_minutes'], int),
          '★ 退出分钟数写成整数（别把 30 写成 30.0 —— 配置是给人看的）')
    for _k, _v in INTERNAL_KEYS.items():
        check(dd.get(_k) == _v, f'★ 内部键 {_k} 原样还在（防「整段替换」）')

    print('\n[3] 兜底：没有段 / 输入框清空 / 填了非数字')
    obj.cfg = {}
    try:
        U.MainWindow._apply_demo_ui(obj)
        _ok, _err = True, ''
    except Exception as e:          # noqa: BLE001
        _ok, _err = False, repr(e)
    check(_ok, f'cfg 里没有 demo_record 段 → 不抛异常（{_err}）')
    check(obj.demo_enabled_cb.isChecked() is False,
          '缺键时用出厂默认（总开关 = 关 —— 默认不许偷偷录）')
    check(obj.demo_record_only_cb.isChecked() is True
          and obj.demo_gzip_cb.isChecked() is True
          and obj.demo_out_dir.text() == 'debug/demo'
          and obj.demo_max_minutes.text() == '30',
          '缺键时其余默认（接管键盘=开 / 压缩=开 / debug/demo / 30）')
    obj.demo_out_dir.setText('   ')
    obj.demo_max_minutes.setText('abc')
    dd2 = U.MainWindow._read_demo_ui(obj)
    check(dd2['out_dir'] == 'debug/demo',
          f"目录被清空 → 退回默认，不写成空串（{dd2['out_dir']!r}）")
    check(dd2['max_minutes'] == 30,
          f"填了非数字 → 保留原值（{dd2['max_minutes']!r}）")
    obj.demo_max_minutes.setText('2.5')
    check(U.MainWindow._read_demo_ui(obj)['max_minutes'] == 2.5,
          '填了小数就保留小数（2.5 分钟这种用法不拦）')

    print('\n[4] 保存链：界面值真的会进配置文件')
    check('demo_record' in U.MainWindow._MANAGED_SECTIONS,
          '_MANAGED_SECTIONS 里有 demo_record'
          '（没有它 → 3 秒自动保存会用磁盘旧值盖掉界面值，且不报错）')
    _src_merge = inspect.getsource(U.MainWindow._merge_disk_owned_sections)
    check('self._MANAGED_SECTIONS' in _src_merge,
          '合并逻辑只认那一处清单（managed = self._MANAGED_SECTIONS）')

    print('\n[5] 三处接线（缺一处 = 这个控件是装饰品）')
    _s_setup = inspect.getsource(U.MainWindow.setup_main_tab)
    check('create_demo_gbox()' in _s_setup, 'setup_main_tab() 里造了这个组框')
    check('self.demo_gbox' in _s_setup, 'setup_main_tab() 里把它加进了布局')
    _s_apply = inspect.getsource(U.MainWindow.apply_config_to_ui)
    check('_apply_demo_ui()' in _s_apply, 'apply_config_to_ui() 里灌了值')
    _s_read = inspect.getsource(U.MainWindow.update_cfg_from_main_ui)
    check('_read_demo_ui()' in _s_read, 'update_cfg_from_main_ui() 里读回了值')
    _s_en = inspect.getsource(U.MainWindow.set_gbox_enabled)
    check('self.demo_gbox' in _s_en,
          'set_gbox_enabled() 里也管它（挂机中置灰 —— 录制器在点开始时已建好）')

    print('\n[5b] 用户看得见：点「开始」时把录制状态摊开（不许静默）')
    _s_toggle = inspect.getsource(U.MainWindow.toggle_start_ui)
    check('示范录制' in _s_toggle,
          '★ toggle_start_ui() 里会说明录制已开'
          '（不说 → 键盘静音后角色一动不动，用户只会以为程序坏了）')
    check('键盘静音' in _s_toggle or '键盘没有静音' in _s_toggle,
          '两种模式都说清了（静音 = 你自己打 / 没静音 = 样本被污染）')
    check('录制中' in _s_toggle,
          '开始后按钮上标出「录制中」（用户一直在看的地方）')

    print('\n[6] 口径一致：有控件的键不进隐藏清单，且都有中文名')
    _hide = U.ADV_FIELD_HIDE.get('demo_record', ())
    for _k in UI_KEYS:
        check(_k not in _hide, f'{_k} 不在 ADV_FIELD_HIDE 里（有控件就不该藏）')
        check(cn_key('demo_record', _k) != _k,
              f"{_k} 有中文名（{cn_key('demo_record', _k)}）")
    for _k in INTERNAL_KEYS:
        check(_k in _hide, f'{_k} 收在 ADV_FIELD_HIDE 里（算法内部键，界面不管）')
    check(cn_section('demo_record') != 'demo_record',
          f"段名有中文名（{cn_section('demo_record')}）")

    print('\n[7] 与引擎口径一致：界面默认值 = 出厂默认值')
    _yaml = os.path.join(REPO_ROOT, 'config', 'config_default.yaml')
    try:
        import yaml
        with open(_yaml, encoding='utf-8') as _f:
            _sec = (yaml.safe_load(_f) or {}).get('demo_record', {})
    except Exception as _e:                                    # noqa: BLE001
        _sec = {}
        check(False, f'读 config_default.yaml 失败：{_e!r}')
    obj.cfg = {}
    U.MainWindow._apply_demo_ui(obj)          # 界面在"配置里什么都没有"时的兜底
    _d = U.MainWindow._read_demo_ui(obj)
    for _k in UI_KEYS:
        check(_sec.get(_k) == _d.get(_k),
              f'{_k}：出厂默认 {_sec.get(_k)!r} = 界面兜底 {_d.get(_k)!r}')
    check(_sec.get('enabled') is False,
          '★ 出厂默认是**关**（录制不许在用户没勾的情况下自己开始）')

    print('\n' + '=' * 72)
    print(f'结果：通过 {PASS} / 失败 {FAIL}')
    print('=' * 72)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
