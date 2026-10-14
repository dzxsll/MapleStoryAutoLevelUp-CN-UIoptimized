# -*- coding: utf-8 -*-
'''自检：**修饰键**（alt/ctrl/shift）能不能被认成跳跃键。

## 为什么专门钉这一条（2026-10-13 真机事故）
用户 `config_custom.yaml` 里 `key.jump: alt`。录制时日志明明打着
`[录制] 跳跃键 = 'alt'`（那条只读 config），但录出来的 route1/route2 经色码普查是
**0 个跳跃像素** —— 只有 left/right 线段 + 一个 goal 圆点。
⇒ 引擎永远不下发 jump ⇒ 用户报「无法跳跃」，日志里配着 `[路线够不着]`。

根因：`KeyBoardListener.on_press` 认键只有两条路
  ① 普通字符键 `key.char.lower()`  ② 其余键 `self.movement_keys.get(key)`
而修饰键**没有 `.char`**（AttributeError），只能走 ②；
② 的表里原来只有 up/down/left/right/space ⇒ `k = None`
⇒ `if k and k == self._jump_key_name()` 恒假 ⇒ `_jump_pending` 永不置位。

## 这个自检覆盖的是**真实调用点**，不是复述逻辑
- 真的构造 `KeyBoardListener`、真的调 `on_press`、真的读 `consume_jump_press()`；
- 末端再真的调一次 `RouteRecorder.action_from_keys`，
  确认「latch 住了」确实翻译成 `right none jump`（而不是只验证中间那半段）。
- 变体：把 alt 从表里拿掉 ⇒ 必须变红（见下面的「变异测试」自证）。

⚠️ 会**打桩** `keyboard.Listener`，不装全局键盘钩子（避免污染用户真实键盘）。
'''
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pynput import keyboard

import src.input.KeyBoardListener as K
from src.utils.common import load_yaml

FAIL = []


def check(cond, label, extra=""):
    if cond:
        print(f"  [OK] {label}")
    else:
        print(f"  [!!] {label}{('  —— ' + extra) if extra else ''}")
        FAIL.append(label)


def make_cfg(jump_key):
    """造一个最小 cfg（只要 key.jump + game_window.title）。"""
    return {"key": {"jump": jump_key}, "game_window": {"title": "x"}}


def make_listener(cfg):
    # ── 打桩：不启动真实全局键盘监听（本自检要反复构造，装钩子会扰民）──────
    real_listener = K.keyboard.Listener

    class _Stub:
        def __init__(self, **kw):
            self.kw = kw

        def start(self):
            pass

    K.keyboard.Listener = _Stub
    try:
        lb = K.KeyBoardListener(cfg, is_autobot=True)
    finally:
        K.keyboard.Listener = real_listener
    return lb


print("=" * 74)
print("1) movement_keys 表里到底有没有修饰键（根因的那张表）")
print("=" * 74)
lb = make_listener(make_cfg("alt"))
mk = lb.movement_keys
for nm in ("alt", "alt_l", "alt_r"):
    k = getattr(keyboard.Key, nm, None)
    if k is None:
        print(f"  [--] 本平台没有 Key.{nm}，跳过")
        continue
    check(mk.get(k) == "alt", f"Key.{nm} → 'alt'", f"实际 {mk.get(k)!r}")

print()
print("=" * 74)
print("2) 真实 on_press → consume_jump_press（端到端，含反例）")
print("=" * 74)
CASES = [
    # (按键对象,            配置的跳跃键, 期望能否 latch, 说明)
    (keyboard.Key.alt, "alt", True, "左 Alt + key.jump=alt（本次事故的那一种）"),
    (keyboard.Key.alt_l, "alt", True, "Key.alt_l 显式"),
    (keyboard.Key.alt_r, "alt", True, "Key.alt_r（右 Alt，别名之外的独立 Key）"),
    (keyboard.Key.alt, "space", False, "反例：跳跃键是 space，按 Alt 不该 latch"),
    (keyboard.Key.space, "space", True, "回归：空格这条老路还通"),
    (keyboard.Key.space, "alt", False, "反例：跳跃键是 alt，按空格不该 latch"),
    (keyboard.KeyCode.from_char("c"), "c", True, "回归：字符键 c（2026-09-17 修好的那条路）"),
    (keyboard.KeyCode.from_char("c"), "alt", False, "反例：跳跃键不是 c"),
]
for key_obj, jump_key, expect, why in CASES:
    lb = make_listener(make_cfg(jump_key))
    lb.on_press(key_obj)
    got = bool(lb.consume_jump_press())
    check(got is expect, f"{why} ⇒ latch={got}（期望 {expect}）")
    # 取走即清：再问一次必须是 False，否则会一帧跳个没完
    check(bool(lb.consume_jump_press()) is False,
          f"  ↳ {why}：consume 后必须清零")

print()
print("=" * 74)
print("2b) 录制器**真实每帧调用点**：read_keys() → action_from_keys()")
print("=" * 74)
# 这一节钉的是「忘了把 latch 传进 action_from_keys」这一类错：
# 录制器主循环实际写的是  key_press, jump_latched = self.read_keys()
# 再喂给 action_from_keys(...)；只单独测 action_from_keys(jump_latched=True)
# 是**测不到**这半段的（见 read_keys 的 docstring，2026-09-17 的教训）。
import types  # noqa: E402

from tools.routeRecorder import RouteRecorder  # noqa: E402


def make_stub(cfg, **kw):
    '''造一个 `action_from_keys` 跑得动的桩：**挂真实方法**，别在桩里复述逻辑。

    ⚠️ 2026-10-14：`action_from_keys` 里新增了 `self.teleport_key()`（瞬移键也改成
       从 config 读，见 routeRecorder.teleport_key 的 docstring），于是只挂了
       `jump_key` 的旧桩当场 `AttributeError: 'SimpleNamespace' object has no
       attribute 'teleport_key'` ⇒ 本文件在清单里变红。
       **凡是 action_from_keys 以后还要读的自配置项，都在这里挂上真实实现** ——
       桩只需要 `cfg`，方法本身是纯函数。
    '''
    st = types.SimpleNamespace(cfg=cfg, **kw)
    st.jump_key = lambda: RouteRecorder.jump_key(st)
    st.teleport_key = lambda: RouteRecorder.teleport_key(st)
    return st


_lb = make_listener(make_cfg("alt"))
_lb.on_press(keyboard.Key.right)          # 仿真：按住右
_lb.on_press(keyboard.Key.alt)            # 仿真：按一下跳（修饰键）
_stub = make_stub(make_cfg("alt"), kb=_lb)
_kp, _latched = RouteRecorder.read_keys(_stub)
_cmd, _blob = RouteRecorder.action_from_keys(_stub, _kp, _latched)
check(_latched is True and _cmd == "right none jump" and _blob,
      f"read_keys() → action_from_keys() 走完整链路 = {_cmd!r} (latch={_latched})",
      f"期望 'right none jump'，实际 {_cmd!r}")

# 反例：跳跃键配成 space 时，按 alt 不该录成跳（否则就是"乱跳"）
_lb2 = make_listener(make_cfg("space"))
_lb2.on_press(keyboard.Key.right)
_lb2.on_press(keyboard.Key.alt)
_stub2 = make_stub(make_cfg("space"), kb=_lb2)
_kp2, _latched2 = RouteRecorder.read_keys(_stub2)
_cmd2, _blob2 = RouteRecorder.action_from_keys(_stub2, _kp2, _latched2)
check(_cmd2 == "right none none" and _blob2 is False,
      f"反例：key.jump=space 时按 alt ⇒ {_cmd2!r}（不是跳）",
      f"实际 {_cmd2!r} blob={_blob2}")

print()
print("=" * 74)
print("3) 末端真实调用点：latch 住了 ⇒ RouteRecorder 真的吐出跳跃色码")
print("=" * 74)

for jump_key, key_press, expect_cmd in [
    ("alt", ["right"], "right none jump"),
    ("alt", ["left"], "left none jump"),
    ("alt", ["down"], "none down jump"),
    ("alt", [], "none none jump"),
]:
    stub = make_stub(make_cfg(jump_key))
    cmd, is_blob = RouteRecorder.action_from_keys(stub, key_press, True)
    check(cmd == expect_cmd and is_blob,
          f"action_from_keys({key_press}, jump_latched=True) = {cmd!r} blob={is_blob}",
          f"期望 {expect_cmd!r}")

print()
print("=" * 74)
print("4) 变异测试自证：把 alt 从表里删掉，第 2 节必须变红")
print("=" * 74)
lb = make_listener(make_cfg("alt"))
_before = bool(lb.consume_jump_press())
lb.on_press(keyboard.Key.alt)
_a = bool(lb.consume_jump_press())
for k in list(lb.movement_keys):
    if lb.movement_keys[k] == "alt":
        del lb.movement_keys[k]
lb.on_press(keyboard.Key.alt)
_b = bool(lb.consume_jump_press())
check(_a is True and _b is False,
      f"有映射 ⇒ latch={_a}；删掉映射 ⇒ latch={_b}（必须一真一假）",
      "若两者相同，说明本自检测的不是这张表（假通过）")

print()
print("=" * 74)
print("5) 拿**用户真实配置**再验一遍（销账判据：不是合成 cfg 才算数）")
print("=" * 74)
_real = load_yaml("config/config_custom.yaml") or {}
_jk = str((_real.get("key") or {}).get("jump") or "space").strip().lower()
print(f"  config/config_custom.yaml 的 key.jump = {_jk!r}")
lb = make_listener(_real)
# 找到这个键名对应的 Key 对象（先查表，再退回字符键）
_obj = None
for _k, _v in lb.movement_keys.items():
    if _v == _jk:
        _obj = _k
        break
if _obj is None and len(_jk) == 1:
    _obj = keyboard.KeyCode.from_char(_jk)
check(_obj is not None,
      f"录制器认得 {_jk!r} 这个键（表里查到 Key 对象）",
      f"表里没有 {_jk!r} ⇒ 按它永远录不到跳")
if _obj is not None:
    lb.on_press(_obj)
    check(bool(lb.consume_jump_press()) is True,
          f"按 {_jk!r} ⇒ consume_jump_press() = True")

print()
if FAIL:
    print(f"✗ {len(FAIL)} 项未通过：")
    for f in FAIL:
        print(f"    - {f}")
    sys.exit(1)
print("✓ 全部通过（修饰键跳跃键：表 / latch / 色码三层都通）")
