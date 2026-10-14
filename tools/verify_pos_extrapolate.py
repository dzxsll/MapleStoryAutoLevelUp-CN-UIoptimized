# -*- coding: utf-8 -*-
'''「L1 位置外推」的自检（2026-10-12 加）。

## 这个功能在干什么

名牌 miss 时 `loc_nametag` 保持旧值 ⇒ 角色屏幕坐标被**冻住**，而角色还在走
（issue #6：miss 连续 650 帧 ≈ 32 秒期间基本不出招）。
本项改用**小地图全局位移**把坐标外推出去（小地图在观测到的日志里从未失败）。

## 自检盯的四件事

  【1】fail-closed：还没标定过（一次都没命中）⇒ **不许**动位置，沿用冻住的值；
      配置读不出来、`dead_reckon: false` ⇒ 同样不许动。
  【2】★ 有界性（这是"不会比冻住更差"的**唯一**保证）：
      相机是跟随角色的（地图内部屏幕位置几乎不动、地图边缘才 1:1），
      所以纯外推会无界发散。必须验证：**无论全局位移给多大**，
      外推值距「标定中心」都不超过 `dead_reckon_band`。
  【3】到顶停手：连续外推超过 `dead_reckon_max_frames` ⇒ 退回冻住。
  【4】★ 与位置保护的联动：外推成功 ⇒ `player_pos_trustworthy()` 必须放行出招
      （否则 L1 白做：位置更新了、出招却被保护挡着）。
      注意两者都要求 `nametag_miss_streak > 0`，所以用例里 streak 必须是 1 而不是 0
      —— 第一版把 streak 留成 0，`player_pos_trustworthy` 在"刚命中"那条就返回
      True 了，用例**假通过**（抓出来后改成 1）。
  【5】★ 护栏（2026-10-13 加，真机日志逼出来的）：外推"有界"≠"够准" ——
      真机实测误差峰值 **210px**，而攻击框半高只有 **35px**。两道闸：
        · 外推链**累计位移** > `dead_reckon_trust_travel` ⇒ 不信它出招
          （实测：累计 24px 时误差 ≤10px；累计 76px 时误差 ≥26px）；
        · 外推链里出现过 **y 突变**（跳跃/掉落/纵向传送，单帧 |Δy| > `dead_reckon_y_jump`）
          ⇒ **整条链作废**（相机跟不上，实测一帧就差 44px），直到下次真命中。
      ⚠️ 反例用例必须保留：**站着不动 miss 121 帧**时实测误差只有 3px ⇒
         不许因为"帧数多"就停手（那会误伤"站着挨打"这个正常场景）。

用法：
    python -m tools.verify_pos_extrapolate
'''
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml                  # noqa: E402
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot   # noqa: E402

FAIL: list = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


CFG = load_yaml("config/config_default.yaml")


def make_bot(**over):
    '''造一个只带外推所需字段的引擎实例（不抓帧、不连游戏）。'''
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.cfg = {"nametag": dict(CFG["nametag"])}
    b.cfg["nametag"]["stale_guard_frames"] = 30
    for k, v in over.items():
        b.cfg["nametag"][k] = v
    b.nametag_miss_streak = 1          # ⚠️ 必须是 >0，否则 player_pos_trustworthy 早退
    b.loc_player = (700, 400)
    b.loc_player_global = (100, 100)
    b.img_frame = None
    b.cmd_move_x = "left"
    b._pos_stale_moving_frames = 999   # 足够大：放行只可能来自外推
    b.pos_guard_tripped = False
    b._dr_anchor = None
    b._dr_center = None
    b._dr_extrap = 0
    b._dr_active = False
    b._dr_pending = None
    b._dr_e_last = None
    b._dr_e_max = 0
    b._dr_g_prev = None
    b._dr_y_jump = False
    b._dr_travel = 0
    b._dr_untrust = ""
    return b


# ══════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("L1 位置外推 · 自检")
print("=" * 70)

# ── 【1】命中时建立锚点与标定中心 ─────────────────────────────────────────
print("\n[1] 命中：建立锚点 / 标定中心")
b = make_bot()
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
check("锚点(屏幕)", b._dr_anchor[0], (710, 400))
check("锚点(全局)", b._dr_anchor[1], (100, 100))
check("标定中心=首次命中的屏幕坐标", b._dr_center, (710, 400))
check("未命中过 ⇒ 未激活", b._dr_active, False)

# 标定中心是慢变量：第二次命中 (730,400) 只走 α=0.2 ⇒ 710+4=714
b.nametag_miss_streak = 0
b.loc_player = (730, 400)
b.loc_player_global = (110, 100)
b._update_player_screen_loc()
check("标定中心慢速跟随(α=0.2)", b._dr_center, (714, 400))

# ── 【2】外推方向 + 有界性 ────────────────────────────────────────────────
print("\n[2] 外推：方向正确，且被夹在「标定中心 ± band」内")
b = make_bot(dead_reckon_band=60)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()          # 标定完成
b.nametag_miss_streak = 1              # 转 miss
b.loc_player_global = (120, 130)       # 全局 +20 / +30（1:1）
b._update_player_screen_loc()
check("外推值 = 锚点 + 全局位移(1:1)", b.loc_player, (730, 430))
check_true("外推激活", b._dr_active)
check("外推帧计数", b._dr_extrap, 1)

# ★ 有界性：全局跑到 +10000px，外推值仍不许跑出标定中心 ± band
b.loc_player_global = (10100, 10100)
b._update_player_screen_loc()
check("★ clamp x（中心 710 ± 60）", b.loc_player[0], 710 + 60)
check("★ clamp y（中心 400 ± 60）", b.loc_player[1], 400 + 60)

# ★ 反方向同样有界
b.loc_player_global = (-9900, -9900)
b._update_player_screen_loc()
check("★ clamp 反方向 x", b.loc_player[0], 710 - 60)
check("★ clamp 反方向 y", b.loc_player[1], 400 - 60)

# ── 【3】到顶停手 ─────────────────────────────────────────────────────────
print("\n[3] 连续外推超上限 ⇒ 停手（退回冻住）")
b = make_bot(dead_reckon_max_frames=3, dead_reckon_band=60)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
b.nametag_miss_streak = 1
for i in range(3):
    b.loc_player_global = (100 + 10 * (i + 1), 100)
    b._update_player_screen_loc()
check("外推了 3 帧", b._dr_extrap, 3)
_frozen = b.loc_player                       # 第 3 帧的外推值
b.loc_player_global = (100 + 500, 100)       # 第 4 帧：全局又走 500px
b._update_player_screen_loc()
check("第 4 帧不再动位置（停手 = 冻住）", b.loc_player, _frozen)
check("停手后标记为非外推", b._dr_active, False)

# ── 【4】恢复命中：结算误差（销账判据） ──────────────────────────────────
print("\n[4] 恢复命中：结算上一轮外推的实测误差")
b = make_bot(dead_reckon_band=60)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
b.nametag_miss_streak = 1
b.loc_player_global = (130, 100)
b._update_player_screen_loc()                        # 外推 ⇒ (740,400) → clamp 后 (770-?) 
_est = b.loc_player
b.nametag_miss_streak = 0
b.loc_player = (750, 400)                            # 真值：与外推值差 |750-770|=20
b.loc_player_global = (140, 100)
b._update_player_screen_loc()
check("误差被结算（max 范数）", b._dr_e_last, abs(_est[0] - 750))
check_true("误差已记入峰值", b._dr_e_max >= 0)
check("结算后清空 pending", b._dr_pending, None)
check("结算后计数归零", b._dr_extrap, 0)

# ── 【5】fail-closed ─────────────────────────────────────────────────────
print("\n[5] fail-closed：未标定 / 关开关 / 配置坏值 ⇒ 一律不动位置")
b = make_bot()                       # 无锚点
b.loc_player = (700, 400)
b.loc_player_global = (500, 500)
b._update_player_screen_loc()
check("未标定 ⇒ 位置不动", b.loc_player, (700, 400))

b = make_bot(dead_reckon=False)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
b.nametag_miss_streak = 1
b.loc_player_global = (900, 900)
b._update_player_screen_loc()
check("dead_reckon=false ⇒ 位置不动", b.loc_player, (710, 400))
check("dead_reckon=false ⇒ 从不标定", b._dr_center, None)

b = make_bot(dead_reckon_band="坏值")
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
b.nametag_miss_streak = 1
b.loc_player_global = (900, 900)
b._update_player_screen_loc()
check("band 非数字 ⇒ 位置不动（不抛异常）", b.loc_player, (710, 400))

b = make_bot()
b.loc_player_global = None           # 小地图还没算出来
b._update_player_screen_loc()
check("全局坐标缺失 ⇒ 直接返回", b.loc_player, (700, 400))

# ── 【6】与位置保护的联动 ────────────────────────────────────────────────
print("\n[6] ★ 与位置保护联动：外推成功必须放行出招")
b = make_bot(dead_reckon_band=60)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
b.nametag_miss_streak = 1
b.loc_player_global = (110, 100)
b._update_player_screen_loc()
check_true("外推激活时 player_pos_trustworthy() 放行", b.player_pos_trustworthy())

b2 = make_bot(dead_reckon=False)     # 关掉外推 ⇒ 旧行为：过期+在动 ⇒ 停手
b2.nametag_miss_streak = 1
b2.loc_player_global = (110, 100)
b2._update_player_screen_loc()
check("关掉外推时仍按老判据停手", b2.player_pos_trustworthy(), False)

# ── 【7】护栏：外推不可信时停手（但**不停**外推） ─────────────────────────
print("\n[7] ★ 护栏：累计位移 / y 突变 ⇒ 不出招（位置照旧外推）")


def _chain(bot, globals_):
    '''在已标定的 bot 上走一串 miss 帧；globals_ = 每帧的小地图全局坐标。'''
    bot.nametag_miss_streak = 1
    for g in globals_:
        bot.loc_player_global = g
        bot._update_player_screen_loc()


# (a) 累计位移超阈值 ⇒ 停止出招，但位置仍在被外推更新
b = make_bot(dead_reckon_band=60, dead_reckon_trust_travel=40)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
_chain(b, [(110, 100), (120, 100), (140, 100), (160, 100)])
check("累计位移 60px > 阈值 40px", b._dr_travel, 60)
check("★ 不信任（停手）", b.player_pos_trustworthy(), False)
check("不信任的原因非空（会打进 [运行状态]）", bool(b._dr_untrust), True)
check("★ 但位置**仍在**被外推更新（停手 ≠ 停外推）", b._dr_active, True)
check("外推值推进到中心+60（仍在 clamp 内）", b.loc_player, (770, 400))

# (b) ★ 反例：站着不动 miss 很多帧 ⇒ **必须仍然信任**（不许按帧数关）
b = make_bot(dead_reckon_band=60, dead_reckon_trust_travel=40)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
_chain(b, [(100, 100)] * 121)
check("站着不动：连续外推 121 帧", b._dr_extrap, 121)
check("站着不动：累计位移 0px", b._dr_travel, 0)
check("★ 站着不动 ⇒ 仍然信任（真机实测这种情形误差只有 3px）",
      b.player_pos_trustworthy(), True)
check("未记下任何不信任原因", b._dr_untrust, "")

# (c) 外推链里出现 y 突变（跳跃/掉落）⇒ 整条链作废，且**后续帧也不恢复**
#     位移闸门设成 999 关掉，确保这里判的只能是 y 突变
b = make_bot(dead_reckon_band=60, dead_reckon_trust_travel=999, dead_reckon_y_jump=10)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
_chain(b, [(102, 140)])                       # 单帧 |Δy| = 40 > 10 ⇒ 跳跃/掉落
check_true("y 突变被记下", b._dr_y_jump)
check("★ y 突变 ⇒ 停手", b.player_pos_trustworthy(), False)
_chain(b, [(102, 141), (102, 142)])           # 之后走得很平（每帧 y 只差 1px）
check("★ 突变过后仍在同一轮外推链里 ⇒ 不恢复信任", b.player_pos_trustworthy(), False)
check("位移闸门已关（证明这里不是位移在起作用）", b._dr_travel, 42)

# (d) 真命中 ⇒ 锚点变真值 ⇒ 护栏清零、恢复出招
b.nametag_miss_streak = 0
b.loc_player = (712, 402)
b.loc_player_global = (102, 142)
b._update_player_screen_loc()
check("命中后 y 突变标记清零", b._dr_y_jump, False)
check("命中后累计位移清零", b._dr_travel, 0)
check("命中后不信任原因清空", b._dr_untrust, "")
_chain(b, [(104, 144)])                       # 之后重新 miss：只走了 2px
check("★ 重新 miss 且位移很小 ⇒ 恢复出招", b.player_pos_trustworthy(), True)

# (e) trust_travel = 0 ⇒ 关掉这道闸（退回 2026-10-12 的行为：外推成功即信任）
b = make_bot(dead_reckon_band=60, dead_reckon_trust_travel=0)
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
_chain(b, [(160, 100)])
check("trust_travel=0 ⇒ 位移再大也不记原因", b._dr_untrust, "")
check("trust_travel=0 ⇒ 仍然信任（可回退）", b.player_pos_trustworthy(), True)

# (f) 配置坏值 ⇒ fail-closed（不许抛异常把主循环带崩）
b = make_bot(dead_reckon_band=60, dead_reckon_trust_travel="坏值")
b.nametag_miss_streak = 0
b.loc_player = (710, 400)
b.loc_player_global = (100, 100)
b._update_player_screen_loc()
_chain(b, [(160, 100)])
check("trust_travel 非数字 ⇒ 位置不动（fail-closed）", b.loc_player, (710, 400))

# ── 汇总 ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
if FAIL:
    print(f"结果：{len(FAIL)} 项失败")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print("结果：全部通过")
