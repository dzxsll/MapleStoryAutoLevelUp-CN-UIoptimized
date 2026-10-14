# -*- coding: utf-8 -*-
'''「停下来把这只怪打死」——击杀闭环状态机的自检（2026-10-13 加）。

## 它防的是什么（用户 2026-10-13 原话：「没击杀就只管自己走了」）

改前的真实行为：攻击分支**只在出招那一帧**冻结移动，冷却期间路线立刻把角色带走。

    update_cmd_by_mob_detection()   出招帧：cmd_action="attack"、cmd_move_x="none"
    下一帧（冷却 0.9s 内）          路线接管 ⇒ 角色继续往前走
    ⇒ 怪离开索敌框 ⇒ 两下才死的怪**永远打不完**

真机证据（`log/MSBot_2026-10-13_02-03-33.log`，18 分钟）：`怪0`/`怪1` 每 5s 一条采样，
全程只有 **7 帧** `指令(... attack)` —— 一边走一边偶尔挥一刀，这就是当时的效果。

改后：`_kill_commit_tick()` 把"停下"从**一帧**扩到**整场交战**（含冷却帧），
出口三个：目标消失 / 交战超 `timeout_s`（默认 12s）/ `commit=false` 退回旧行为；
放弃后还有 `giveup_s`（默认 6s）冷却期，期内只为怪"路过顺手打"，不再冻住移动。

## 钉死的判据（每条都有对应用例）

  ① 配置缺失/写坏 ⇒ 回落 (True, 12.0, 6.0)，**不许**静默退回"一边走一边挥"
  ② 交战帧与冷却帧都必须返回 True（这是本功能的核心，也是回归最容易被改坏的地方）
  ③ 超时 ⇒ 返回 False + 记下 `_kill_giveup_until` + 清 `_kill_since`
  ④ 放弃冷却期内 ⇒ 即使有目标也返回 False（"没击杀就只管自己走"）
  ⑤ `kill.commit=false` ⇒ 一律 False（可一键退回旧行为）
  ⑥ 接线：两个攻击分支都真的调了它，且都带 `not self.is_on_ladder` 例外

用法：
    python -m tools.verify_kill_commit

⚠️ 本脚本**只读**：只读源码/配置 + 在内存里跑状态机，不写任何文件、不开游戏。
   （2026-10-13 地图被清空那次事故之后，"自检不许写盘"当成纪律。）
'''
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot       # noqa: E402
from src.utils.common import load_yaml                               # noqa: E402

FAIL: list = []
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, cond, note=""):
    ok = bool(cond)
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}{('：' + note) if note else ''}")
    if not ok:
        FAIL.append(name)


class Stub:
    '''只带被测方法所需字段的最小宿主（不跑 __init__，不建窗口）。'''


Stub._kill_cfg = MapleStoryAutoBot._kill_cfg
Stub._kill_commit_tick = MapleStoryAutoBot._kill_commit_tick


def new_bot(cfg=None):
    b = Stub()
    b.cfg = cfg if cfg is not None else {}
    b._kill_since = None
    b._kill_giveup_until = 0.0
    b.t_last_kill_log = 0.0
    return b


def section(title):
    print(f"\n【{title}】")


# ── 【1】默认值：配置里没有 kill 段 ─────────────────────────────────────────
section("1 配置缺失 ⇒ 默认 (True, 12.0, 6.0)")
b = new_bot({})
check("无 kill 段", b._kill_cfg(), (True, 12.0, 6.0))
check("cfg 整个为空 dict", new_bot()._kill_cfg(), (True, 12.0, 6.0))
check("kill 段是 None", new_bot({"kill": None})._kill_cfg(), (True, 12.0, 6.0))
check("kill 段是空 dict", new_bot({"kill": {}})._kill_cfg(), (True, 12.0, 6.0))

# ── 【2】显式配置 ──────────────────────────────────────────────────────────
section("2 显式配置生效")
check("commit=False + 3s/1.5s",
      new_bot({"kill": {"commit": False, "timeout_s": 3, "giveup_s": 1.5}})._kill_cfg(),
      (False, 3.0, 1.5))
check("只给 timeout_s（giveup 取默认 6.0）",
      new_bot({"kill": {"timeout_s": 5}})._kill_cfg(), (True, 5.0, 6.0))
check("giveup_s=0 是合法值（放弃后不停顿）",
      new_bot({"kill": {"giveup_s": 0}})._kill_cfg(), (True, 12.0, 0.0))

# ── 【3】坏值回落（fail-closed：读不出来按"更保守"的默认走，不静默退回旧行为）──
section("3 坏值回落")
check("timeout_s=0 ⇒ 12.0", new_bot({"kill": {"timeout_s": 0}})._kill_cfg(),
      (True, 12.0, 6.0))
check("timeout_s=-5 ⇒ 12.0", new_bot({"kill": {"timeout_s": -5}})._kill_cfg(),
      (True, 12.0, 6.0))
check("giveup_s=-1 ⇒ 6.0", new_bot({"kill": {"giveup_s": -1}})._kill_cfg(),
      (True, 12.0, 6.0))
check("timeout_s='abc'（转 float 失败）⇒ 整体默认",
      new_bot({"kill": {"timeout_s": "abc"}})._kill_cfg(), (True, 12.0, 6.0))
check("kill 段不是 dict ⇒ 整体默认", new_bot({"kill": ["a"]})._kill_cfg(),
      (True, 12.0, 6.0))
# ⚠️ 这条按**真实行为**钉：NaN 由 `_v != _v` 抛错 ⇒ 被整段 except 接住 ⇒
#    timeout 与 giveup **一起**回落默认值（不是只回落 timeout）。
check("timeout_s=NaN ⇒ 整体默认", new_bot({"kill": {"timeout_s": float("nan")}})._kill_cfg(),
      (True, 12.0, 6.0))
_b = new_bot({"kill": {"timeout_s": "x"}})
check("坏值只警告一次（第一次）", _b._kill_cfg(), (True, 12.0, 6.0))
check_true("坏值置了 _kill_cfg_warned", getattr(_b, "_kill_cfg_warned", False) is True)
check("坏值第二次仍然返回默认", _b._kill_cfg(), (True, 12.0, 6.0))

# ── 【4】commit=False：一键退回旧行为 ───────────────────────────────────────
section("4 kill.commit=false ⇒ 永不停下")
b = new_bot({"kill": {"commit": False}})
b._kill_since = time.time()          # 假装正在交战
check("有目标也返回 False", b._kill_commit_tick(True), False)
check("并把交战状态清掉", b._kill_since, None)
check("没目标也 False", b._kill_commit_tick(False), False)

# ── 【5】目标消失 ───────────────────────────────────────────────────────────
section("5 目标消失 ⇒ 恢复巡逻")
b = new_bot()
b._kill_since = time.time() - 1.0
check("返回 False", b._kill_commit_tick(False), False)
check("清空 _kill_since", b._kill_since, None)
check("没有交战时也 False", new_bot()._kill_commit_tick(False), False)

# ── 【6】交战第一帧 ─────────────────────────────────────────────────────────
section("6 有目标 ⇒ 停下击杀")
b = new_bot()
t0 = time.time()
check("首帧返回 True", b._kill_commit_tick(True), True)
check_true("记下 _kill_since", b._kill_since is not None and abs(b._kill_since - t0) < 1.0,
           f"_kill_since={b._kill_since}")

# ── 【7】★核心回归★ 冷却帧必须继续停 ───────────────────────────────────────
section("7 ★核心★ 冷却期间（未超时）每帧都要 True")
b = new_bot({"kill": {"commit": True, "timeout_s": 12.0, "giveup_s": 6.0}})
b._kill_since = time.time()
# 旧实现只在出招那一帧停（其余帧交给路线）⇒ 这里必须**连续**为 True 才说明修住了。
_holds = [b._kill_commit_tick(True) for _ in range(30)]
check("连续 30 帧全为 True", all(_holds), True)
check("真值为 30 帧", len([x for x in _holds if x]), 30)
check_true("_kill_since 未被重置", b._kill_since is not None)
# 边界：刚好不到 timeout 仍然要停（留 0.5s 余量，避免 CI 抖动误判）
b._kill_since = time.time() - 11.5
check("11.5s < timeout(12s) 仍停", b._kill_commit_tick(True), True)

# ── 【8】超时放弃 ───────────────────────────────────────────────────────────
section("8 超过 timeout_s ⇒ 放弃 + 冷却")
b = new_bot({"kill": {"commit": True, "timeout_s": 12.0, "giveup_s": 6.0}})
b._kill_since = time.time() - 12.5
t_before = time.time()
check("返回 False", b._kill_commit_tick(True), False)
check("清空 _kill_since", b._kill_since, None)
check_true("_kill_giveup_until ≈ now+6s",
           abs(b._kill_giveup_until - (t_before + 6.0)) < 1.0,
           f"_kill_giveup_until - now = {b._kill_giveup_until - t_before:.2f}s")

# ── 【9】放弃冷却期内：有怪也不停 ───────────────────────────────────────────
section("9 放弃冷却期内 ⇒ 只为怪路过顺手打")
b = new_bot({"kill": {"commit": True, "timeout_s": 12.0, "giveup_s": 6.0}})
b._kill_giveup_until = time.time() + 5.0
check("有目标仍返回 False", b._kill_commit_tick(True), False)
check("不记录新的交战开始", b._kill_since, None)
check("连续 10 帧都不停", all(not b._kill_commit_tick(True) for _ in range(10)), True)

# ── 【10】冷却过后重新交战 ──────────────────────────────────────────────────
section("10 冷却过后又有怪 ⇒ 重新停下打")
b = new_bot({"kill": {"commit": True, "timeout_s": 12.0, "giveup_s": 6.0}})
b._kill_giveup_until = time.time() - 0.1        # 窗口已过
check("重新交战 True", b._kill_commit_tick(True), True)
check_true("重新记下 _kill_since", b._kill_since is not None)

# ── 【11】接线检查（接线 ≠ 实现：写得再对，没接进调用链就是永不触发）────────
section("11 接线检查（源码文本）")
_src = os.path.join(ROOT, "src", "engine", "MapleStoryAutoLevelUp.py")
with open(_src, "r", encoding="utf-8") as _f:
    _text = _f.read()
check_true("AOE 分支调了 _kill_commit_tick(len(self.monsters) > 0)",
           "_hold = self._kill_commit_tick(len(self.monsters) > 0)" in _text)
check_true("方向分支调了 _kill_commit_tick(True)",
           "_hold = self._kill_commit_tick(True)" in _text)
check_true("无方向出口调了 _kill_commit_tick(False)（结束交战）",
           "self._kill_commit_tick(False)" in _text)
check_true("AOE 冷却帧的停走带了梯子例外",
           "elif _hold and not self.is_on_ladder:" in _text)
check_true("方向分支冷却帧的停走带了梯子例外",
           "if _hold and not self.is_on_ladder:" in _text)
# 方向分支的关键形状：冷却帧必须 **return**（否则会掉进出招逻辑）
check_true("方向分支冷却帧会 return（不再掉进出招）",
           "if not _can_fire:\n                if _hold and not self.is_on_ladder:" in _text)

# ── 【12】配置实体检查（声明值 ≠ 实体）──────────────────────────────────────
section("12 配置实体（config_default.yaml / config_custom.yaml）")
_d = load_yaml(os.path.join(ROOT, "config", "config_default.yaml")) or {}
_dk = _d.get("kill")
check_true("config_default.yaml 有 kill 段", isinstance(_dk, dict), f"got={type(_dk).__name__}")
if isinstance(_dk, dict):
    check("默认 commit", bool(_dk.get("commit", True)), True)
    check("默认 timeout_s", float(_dk.get("timeout_s", 12.0)), 12.0)
    check("默认 giveup_s", float(_dk.get("giveup_s", 6.0)), 6.0)
_c_path = os.path.join(ROOT, "config", "config_custom.yaml")
if os.path.exists(_c_path):
    _c = load_yaml(_c_path) or {}
    _ck = _c.get("kill")
    if _ck is None:
        print("  [OK ] config_custom.yaml 未覆盖 kill 段 ⇒ 跟随默认（开启）")
    else:
        print(f"  [note] config_custom.yaml 覆盖了 kill 段：{_ck}")

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项未通过：" + ", ".join(FAIL))
    sys.exit(1)
print("✅ 全部通过（击杀闭环状态机 + 接线 + 配置）")
