# -*- coding: utf-8 -*-
"""F12 = 关闭挂机界面：必须先留痕 + 必须确认（2026-10-14 加，CI 可跑）。

## 为什么有它（真机事故）

用户 2026-10-14 03:40:48 的日志里，挂机界面**自己退了**。日志尾部是 closeEvent 的
6 行（`Save yaml` → `[UI] Save UI state` → `Terminated`），既没有 `[Attack Timeout]`
（引擎唯一的自动停机路径，`src/engine/MapleStoryAutoLevelUp.py:5709-5720`）也没有
`[主循环] run_once 抛异常` ⇒ **不是引擎自杀，是窗口被关了**。

代码里唯一「非人手」的关窗路径就是 F12：

    src/ui/AutoBotController.py:84   register_func_key_handler('f12', lambda: ui.request_close.emit())
    src/ui/ui.py（改前 :794）         self.request_close.connect(self.close)

而 F12 走的是 pynput **全局**键盘监听（`src/input/KeyBoardListener.py:81-83`），
`on_press` 认 F 键时**不检查前台窗口**（`:109-128`；`run_for_autobot` `:222-229` 只空转）
⇒ 在任何窗口误按一下 F12，整个界面立刻关掉、挂机停止，**而且一行日志都不打**。
（对比 F1~F4：它们有 `_hotkey_fN_guarded()` + `_hotkeys_suspended()` 闸门。）

## 这个自检盯的判据

1. `request_close` 接到 `_hotkey_f12_guarded`，**不再**直连 `self.close`；
2. 按 F12 **先打日志**（留痕），再弹确认框；
3. 确认框**默认按钮 = 取消**（误按回车不会关程序）；
4. 选「取消」→ 一行都不动、挂机继续（取消分支也留痕）；
5. 选「确定」→ **恰好**关一次窗口（之后走 closeEvent：存设置 + 停挂机 + 退界面）；
6. 键盘监听器那一头仍然注册着 F12 → `request_close`（两端缺一不可）；
7. F1~F4 的闸门接线不许被这次改动误伤。

## 为什么不直接构造 MainWindow

与 `tools/verify_monster_ui.py` 同一套：`MainWindow.__init__` 要读
`config/config_data.yaml`（被 gitignore，CI 里没有）⇒ 用**桩实例绑真方法**
（普通类 + `getattr(MainWindow, name).__get__(stub)`），不跑 `__init__`。

用法：python -m tools.verify_hotkey_f12_close
"""
import inspect
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:                                    # noqa: BLE001
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


def code_only(func):
    """取函数源码，并**剥掉 docstring**。

    ⚠️ 必须剥：`inspect.getsource` 会把 docstring（有时还有注释）一起带出来，
    而下面有一条判据是"**代码里没有** `_hotkeys_suspended`" —— handler 的 docstring
    里正解释着"与 F1~F4 的区别是故意的：F12 不查 `_hotkeys_suspended()`"，
    不剥就会自己把自己判挂（2026-10-13 在 `verify_monster_templates` 里
    已经被同一种"注释命中自己的断言"坑过一次）。
    """
    src = inspect.getsource(func)
    first = src.find('"""')
    if first != -1:
        second = src.find('"""', first + 3)
        if second != -1:
            src = src[:first] + src[second + 3:]
    return src


def main():
    print('=' * 72)
    print(' 自检：F12 = 关闭挂机界面（先留痕 → 再确认 → 才关）')
    print('=' * 72)

    from PySide6.QtWidgets import QApplication, QMessageBox
    import src.ui.ui as U

    _app = QApplication.instance() or QApplication([])   # noqa: F841  (offscreen)

    print('\n[1] 源码接线：request_close 不再直连 self.close')
    _src_init = inspect.getsource(U.MainWindow.__init__)
    check('self.request_close.connect(self._hotkey_f12_guarded)' in _src_init,
          'request_close → _hotkey_f12_guarded（必须经过确认框）')
    check('self.request_close.connect(self.close)' not in _src_init,
          '★ 没有绕过确认框直连 self.close（改前就是这么被 F12 关掉的）')
    check('self.hotkey_f1.connect(self._hotkey_f1_guarded)' in _src_init
          and 'self.hotkey_f4.connect(self._hotkey_f4_guarded)' in _src_init,
          'F1~F4 的 guarded 接线没被误伤')

    print('\n[2] 另一头也得在：键盘监听器仍然注册 F12 → request_close')
    _ac_path = os.path.join(REPO_ROOT, 'src', 'ui', 'AutoBotController.py')
    with open(_ac_path, encoding='utf-8') as f:
        _ac = f.read()
    check("register_func_key_handler('f12'" in _ac and 'ui.request_close.emit()' in _ac,
          "AutoBotController 里 f12 → ui.request_close.emit() 仍在（两端缺一不可）")

    print('\n[3] 行为：桩实例 + 假确认框（跑真方法，不跑 MainWindow.__init__）')

    class _Stub:
        pass

    obj = _Stub()
    obj._hotkey_f12_guarded = getattr(U.MainWindow, '_hotkey_f12_guarded').__get__(obj, _Stub)

    _closed = []
    obj.close = lambda: _closed.append(1)

    _logs = []

    class _FakeLogger:
        def info(self, msg, *a, **k):
            _logs.append(str(msg))

        def warning(self, msg, *a, **k):
            _logs.append('WARN:' + str(msg))

        def error(self, msg, *a, **k):
            _logs.append('ERR:' + str(msg))

        def debug(self, msg, *a, **k):
            _logs.append(str(msg))

    class _FakeMB:
        Ok = QMessageBox.Ok
        Cancel = QMessageBox.Cancel
        reply = QMessageBox.Cancel
        calls = []

        @staticmethod
        def question(parent=None, title='', text='', buttons=None, default=None):
            _FakeMB.calls.append((parent, title, text, buttons, default))
            return _FakeMB.reply

    _real_logger = U.logger
    _real_mb = U.QMessageBox
    U.logger = _FakeLogger()
    U.QMessageBox = _FakeMB
    try:
        # ① 选「取消」
        _FakeMB.reply = QMessageBox.Cancel
        obj._hotkey_f12_guarded()
        check(_closed == [], '选「取消」→ 不关窗口（挂机继续）')
        check(any('F12' in m for m in _logs), f'按 F12 有留痕日志（{_logs}）')
        check(any('取消' in m for m in _logs), '取消分支也留痕（看得出来是谁按的）')
        _p, _title, _tx, _btn, _default = _FakeMB.calls[-1]
        check(_p is obj, '确认框挂在主窗口上（parent = self）')
        check(_default == QMessageBox.Cancel,
              '★ 默认按钮 = 取消（误按回车不会把挂机关掉）')
        check('挂机' in _tx and 'F12' in _tx,
              f'确认框说明了后果与来源（{_tx.splitlines()[0]!r}…）')

        # ② 选「确定」
        _FakeMB.reply = QMessageBox.Ok
        _logs.clear()
        obj._hotkey_f12_guarded()
        check(_closed == [1], '选「确定」→ 恰好关一次窗口（之后走 closeEvent）')
        check(len(_FakeMB.calls) == 2, f'两次按 F12 = 两次确认框（实际 {len(_FakeMB.calls)}）')
    finally:
        U.logger = _real_logger
        U.QMessageBox = _real_mb

    print('\n[4] 与 F1~F4 的区别是**故意的**：F12 录制期间不挂起')
    _src_f12 = inspect.getsource(U.MainWindow._hotkey_f12_guarded)
    _code_f12 = code_only(U.MainWindow._hotkey_f12_guarded)
    check('_hotkeys_suspended' not in _code_f12,
          '★ 代码里（不含 docstring）没有 _hotkeys_suspended ——'
          ' F12 不挂起，录制中按了照样问')
    check('logger' in _code_f12 and 'close()' in _code_f12,
          '留痕与关窗都写在 handler 里（不是只靠调用方）')
    check('_hotkeys_suspended' in _src_f12,
          'docstring 里解释了"为什么与 F1~F4 不同"（判据剥的是 docstring，不是删说明）')

    print('\n' + '=' * 72)
    print(f'结果：通过 {PASS} / 失败 {FAIL}')
    print('=' * 72)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
