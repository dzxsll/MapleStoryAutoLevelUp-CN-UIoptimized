# -*- coding: utf-8 -*-
'''「位置过期保护」的自检（2026-10-12 加）。

## 这个功能要保护的是什么

issue #6：名牌匹配失败时引擎沿用旧坐标，而**角色还在走** —— 偏差越过攻击框半高
（`range_y/2` = 35px）后怪就落出攻击框，**打不到怪**还可能朝错方向出招
（真机实测 miss 连续 650 帧 ≈ 32 秒）。

保护的做法：**位置过期 + 引擎仍在命令角色移动** ⇒ 那个坐标必然已偏 ⇒
把出招动作摘成 "none"（**照常走路**）。名牌回来、或角色停下就恢复。

## 自检盯的三件事

  【1】判据本身：`guard=0` 要能彻底关掉；"过期但没在动"**不许**误伤（旧坐标仍有效）
  【2】计数器：过期 + 在动才累加，其余一律清零
  【3】★ 落地位置：摘动作必须发生在**所有会写 cmd_action 的步骤之后**。
       这条最容易被改坏 —— `update_cmd_by_mob_detection()` 和 `handle_stuck()`
       都会写 `cmd_action`，守护若放在它们前面，摘掉的值会被覆盖回去，
       **功能静默失效**、日志上还看不出任何异常（第一版就写错了，本用例抓出来的）。

用法：
    python -m tools.verify_pos_guard
'''
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml                  # noqa: E402
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot   # noqa: E402
from src.states.hunting import HuntingState             # noqa: E402

FAIL: list = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


def make_bot(cfg, guard=30):
    '''造一个只带判据所需字段的引擎实例（不抓帧、不连游戏）。'''
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.cfg = dict(cfg)
    b.cfg["nametag"] = dict(cfg["nametag"])
    b.cfg["nametag"]["stale_guard_frames"] = guard
    b.nametag_miss_streak = 0
    b._pos_stale_moving_frames = 0
    b.pos_guard_tripped = False
    b.cmd_move_x = "none"
    return b


CFG = load_yaml("config/config_default.yaml")

# ══════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("位置过期保护 · 自检")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】判据 player_pos_trustworthy()")
# ══════════════════════════════════════════════════════════════════════════
b = make_bot(CFG, guard=30)
check("① 刚命中（streak=0）→ 可信", b.player_pos_trustworthy(), True)

b = make_bot(CFG, guard=30)
b.nametag_miss_streak = 5
b._pos_stale_moving_frames = 29
check("② 过期 5 帧、计数 29 < 30 → 仍可信（还没到）", b.player_pos_trustworthy(), True)
b._pos_stale_moving_frames = 30
check("③ 计数到 30 → **不可信**（该摘动作了）", b.player_pos_trustworthy(), False)

b = make_bot(CFG, guard=30)
b.nametag_miss_streak = 3
b.cmd_move_x = "none"          # 角色没在动
b._update_pos_guard()          # 走**真实路径**把计数清零（不是手改字段）
check("④ 过期但角色**没在动** → 计数清零 → 仍可信\n"
      "      （这就是老实现说的『沿用上一帧位置几乎无害』成立的那一半）",
      (b._pos_stale_moving_frames, b.player_pos_trustworthy()), (0, True))

b = make_bot(CFG, guard=0)
b.nametag_miss_streak = 999
b._pos_stale_moving_frames = 999
check("⑤ guard=0 → 彻底关掉，回到老行为", b.player_pos_trustworthy(), True)

b = make_bot(CFG, guard=30)
b.nametag_miss_streak = 1
b._pos_stale_moving_frames = 30
check("⑥ 刚过期 1 帧但计数已到 → 不可信（计数才是判据）",
      b.player_pos_trustworthy(), False)

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】计数器 _update_pos_guard()")
# ══════════════════════════════════════════════════════════════════════════
b = make_bot(CFG, guard=30)
b.nametag_miss_streak = 5
b.cmd_move_x = "right"
for _ in range(3):
    b._update_pos_guard()
check("① 过期 + 在动 → 每帧 +1", b._pos_stale_moving_frames, 3)
check("② 还没到阈值 → 不算保护态", b.pos_guard_tripped, False)

b.cmd_move_x = "none"
b._update_pos_guard()
check("③ 过期 + **没在动** → 计数清零（旧坐标仍有效）",
      (b._pos_stale_moving_frames, b.pos_guard_tripped), (0, False))

b.cmd_move_x = "left"
b.nametag_miss_streak = 0          # 假设这一帧命中了
b._update_pos_guard()
check("④ 命中这一帧 → 计数清零", b._pos_stale_moving_frames, 0)

# 走到触发 → 标记翻转；再回落到未触发 → 标记复位
b = make_bot(CFG, guard=3)
b.nametag_miss_streak = 10
b.cmd_move_x = "right"
for _ in range(3):
    b._update_pos_guard()
check("⑤ 到阈值 → 进入保护态", b.pos_guard_tripped, True)
b.nametag_miss_streak = 0
b._update_pos_guard()
check("⑥ 重新命中 → 退出保护态", b.pos_guard_tripped, False)

# 只告警一次：再跑 10 帧仍然是保护态，标记不该反复翻转
b = make_bot(CFG, guard=3)
b.nametag_miss_streak = 10
b.cmd_move_x = "right"
_flips = 0
_prev = False
for _ in range(12):
    b._update_pos_guard()
    if b.pos_guard_tripped != _prev:
        _flips += 1
        _prev = b.pos_guard_tripped
check("⑦ 保护态只翻转一次（不刷屏）", _flips, 1)

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】★ 落地位置：摘动作必须发生在**所有会写 cmd_action 的步骤之后**")
# ══════════════════════════════════════════════════════════════════════════


class _KB:
    def __init__(self):
        self.sent = []

    def set_command(self, s):
        self.sent.append(s)


class FakeBot:
    '''只实现 HuntingState.on_frame 会碰到的那几个方法。'''

    def __init__(self, trustworthy, stuck_action="jump", stuck=True):
        self.cfg = CFG
        self.kb = _KB()
        self.cmd_move_x = "right"
        self.cmd_move_y = "none"
        self.cmd_action = "attack"        # 怪物检测"想"出招
        self.cmd_move_x_last = None
        self._trust = trustworthy
        self._stuck_action = stuck_action
        self._stuck = stuck
        self.calls = []

    def update_cmd_by_route(self):
        self.calls.append("route")

    def check_reach_goal(self):
        self.calls.append("goal")

    def update_cmd_by_mob_detection(self):
        self.calls.append("mob")
        self.cmd_action = "attack"        # 每帧都会重新写一次

    def is_player_stuck(self):
        return self._stuck               # 默认故意卡死，逼 handle_stuck 也写 cmd_action

    def handle_stuck(self):
        self.calls.append("stuck")
        self.cmd_action = self._stuck_action   # ← 第二个会覆盖 cmd_action 的地方

    def player_pos_trustworthy(self):
        return self._trust


def run_frame(trustworthy, stuck_action="jump", stuck=True):
    st = HuntingState.__new__(HuntingState)
    st.bot = FakeBot(trustworthy, stuck_action, stuck)
    HuntingState.on_frame(st)
    return st.bot


fb = run_frame(trustworthy=False)
check("① 保护态：最终发给控制器的动作是 none", fb.kb.sent[-1], "right none none")
check("② 保护态：**走路指令原样保留**（不许停车）",
      (fb.cmd_move_x, fb.cmd_move_y), ("right", "none"))
check_true("③ 确实经过了 handle_stuck（证明用例真的在覆盖那条路径）",
           "stuck" in fb.calls)

fb = run_frame(trustworthy=False, stuck_action="jump")
check("④ ★ 卡死处理把 action 写成 jump 之后，仍被摘成 none（守护在最后）",
      fb.kb.sent[-1], "right none none")

fb = run_frame(trustworthy=True, stuck_action="jump")
check("⑤ 非保护态：action 原样发出去（jump 不被误伤）",
      fb.kb.sent[-1], "right none jump")

fb = run_frame(trustworthy=True, stuck_action="none", stuck=False)
check("⑥ 非保护态、也没卡死 → 怪物检测的攻击照常发出去",
      fb.kb.sent[-1], "right none attack")

check("⑦ 每帧只发一次命令", len(fb.kb.sent), 1)

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】配置项")
# ══════════════════════════════════════════════════════════════════════════
check_true("① config_default.yaml 里有 nametag.stale_guard_frames",
           "stale_guard_frames" in CFG.get("nametag", {}))
check("② 默认值 30（>0 = 默认开启）", int(CFG["nametag"]["stale_guard_frames"]), 30)

print("\n" + "=" * 70)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
