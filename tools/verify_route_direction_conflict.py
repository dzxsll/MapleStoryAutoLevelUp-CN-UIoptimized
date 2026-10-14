# -*- coding: utf-8 -*-
'''「方向冲突」引擎侧校验的自检（2026-10-12 加）。

配合 `MapleStoryAutoBot.validate_route_image` 的**判据 4**。

## 防的是什么

真机现场：一张图重录后 route1 有 **7 处**、route2 有 **40 处**「同一位置附近既有
向左又有向右的路线像素」。而引擎导航是「取 search_range(10px) 内**最近的**路线像素」
—— 那个半径里两个方向都存在时它**无从判断**，表现是**角色一直朝一个方向冲出去**。

此前这条只在录制侧拦（体检 / routeRebuild），**引擎侧毫无防御**：拿带冲突的图直接
挂机，引擎照单全收，然后在运行时以"角色朝一个方向冲"暴露 —— 那个症状跟
"路线没问题"长得一模一样，是这套系统里最难查的一类。

## 用例要点

  【1】干净的线**必须仍然判过**（加了闸不能把正常图误杀）
  【2】冲突很多 → **判废、不加载**，且原因里说清"为什么"和"怎么修"
  【3】冲突只有一点点 → **仍然加载**，只打 warning（个别杂点不该让整张图报废）
  【4】阈值可配（设很大 = 退回老行为）

⚠️ 判据一律走**真实的 `validate_route_image`**，不自己复刻一遍。

用法：
    python -m tools.verify_route_direction_conflict
'''
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml                        # noqa: E402
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402

FAIL: list = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


CFG = load_yaml("config/config_default.yaml")
CODES = {tuple(int(p) for p in k.split(',')): v
         for k, v in CFG["route"]["color_code"].items()}
UD = {tuple(int(p) for p in k.split(',')): v
      for k, v in CFG["route"]["color_code_up_down"].items()}
RIGHT = next(c for c, v in CODES.items() if v.startswith("right"))
LEFT = next(c for c, v in CODES.items() if v.startswith("left"))
GOAL = next(c for c, v in CODES.items() if v.strip().endswith("goal"))


def make_bot(max_conf, ratio=None):
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.color_code = dict(CODES)
    b.color_code_up_down = dict(UD)
    b.cfg = {"route": dict(CFG["route"])}
    b.cfg["route"]["max_direction_conflicts"] = max_conf
    if ratio is not None:                 # None = 用 config 里的默认比例
        b.cfg["route"]["max_direction_conflict_ratio"] = ratio
    return b


def make_route(n_left=0, lr_gap=2):
    '''造一条"合法的"横线路线；n_left > 0 时在**旁边**加左向像素制造冲突。'''
    img = np.zeros((80, 200, 3), dtype=np.uint8)
    img[40, 20:141] = RIGHT                  # 去程：右向横线（121 像素）
    img[40, 20] = GOAL                       # 起点的 goal 只是凑数，真正靠段末
    img[40, 140] = GOAL                      # 段末 goal（判据 2 要求有 goal）
    for i in range(n_left):                  # 左向像素贴在旁边 lr_gap 像素处
        x = 20 + i
        if x < 140:
            img[40 + lr_gap, x] = LEFT
    return img


print("=" * 70)
print("方向冲突 · 引擎侧校验 自检")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】干净的线**必须仍然判过**（加闸不能误杀正常图）")
# ══════════════════════════════════════════════════════════════════════════
ok, why = make_bot(3).validate_route_image(make_route(n_left=0), "route1.png")
check("① 无冲突 → 判过", ok, True)
check("② 判过时原因为空", why, "")

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】★ 冲突很多 → 判废、不加载，且说清为什么与怎么修")
# ══════════════════════════════════════════════════════════════════════════
ok, why = make_bot(3).validate_route_image(make_route(n_left=60), "route2.png")
check("① 60 个左向像素贴着右向线 → 判废", ok, False)
check_true("② 原因点明「同时存在向左和向右」", "同时存在向左和向右" in why)
check_true("③ 原因说清后果（引擎无从判断 → 一直朝一个方向冲出去）",
           "一个方向冲出去" in why)
check_true("④ 原因给出两条修法（重录 / routeRebuild）",
           "重录" in why and "routeRebuild" in why)
check_true("⑤ 原因带上了冲突数（便于判断严重程度）", "冲突数" in why or "个位置" in why)

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】★ 判废门槛 = max(绝对下限, 比例×像素数) —— 不许把「几乎全对」的图判死")
# ⚠️ 这条是**真机踩坑后加的**：第一版只写绝对值 `>3`，结果用户连录 5 次全部被判废
#    （冲突 5/8/16/20…），日志里出现「⚠ 没有一条有效主线 → 挂机时角色不会移动」。
#    而 5 个冲突落在 95 像素的线上只占 5% —— 那条线 95% 是对的。
#    **拒绝数据不是办法，消化噪声才是。**
ok, why = make_bot(3).validate_route_image(make_route(n_left=1), "route1.png")
check("① 只有 1 个冲突 → 判过（仍可用）", ok, True)
# ★ 真机现场：5 个冲突 / 约 128 像素。
#   **默认（ratio=0）判废** —— 2026-10-12 实测"6 个冲突就让角色一路向右跑飞"，
#   冲突是**致命**的，不是"占比小就能忍"。所以默认必须严。
ok, why = make_bot(3).validate_route_image(make_route(n_left=5), "route1.png")
check("② ★ 默认 ratio=0：5 个冲突 > 绝对下限 3 → **判废**", ok, False)
# 比例机制本身仍在（想放宽的人可以显式放宽）—— 留着是为了可配置，不是默认值
ok, why = make_bot(3, ratio=0.2).validate_route_image(make_route(n_left=5), "route1.png")
check("②b 显式 ratio=0.2 时按比例放行（机制还在）", ok, True)
# 比例设 0 = 只按绝对值判（最严，退回老问题）
ok, why = make_bot(3, ratio=0.0).validate_route_image(make_route(n_left=4), "route1.png")
check("③ 比例设 0 → 绝对下限说了算：4 > 3 → 判废", ok, False)
# 绝对下限保护：像素数很少时比例算出来太小，仍要有下限兜着
ok, why = make_bot(3, ratio=0.01).validate_route_image(
    make_route(n_left=4)[:, :40], "tiny.png")
check("④ 比例极小但冲突 4 > 绝对下限 3 → 判废（下限兜住）", ok, False)

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】阈值可配：设很宽 = 退回老行为（不再拦）")
# ══════════════════════════════════════════════════════════════════════════
ok, why = make_bot(99999, ratio=0.0).validate_route_image(
    make_route(n_left=60), "route2.png")
check("① 绝对下限极大 → 冲突再多也放行", ok, True)
ok, why = make_bot(3, ratio=0.9).validate_route_image(make_route(n_left=60), "route2.png")
check("② 比例很大 → 放行", ok, True)
ok, why = make_bot(0, ratio=0.0).validate_route_image(make_route(n_left=1), "route1.png")
check("③ 绝对下限 0 + 比例 0 → 一个冲突都不许", ok, False)
check_true("④ config_default.yaml 里有 route.max_direction_conflict_ratio",
           "max_direction_conflict_ratio" in CFG["route"])

# ══════════════════════════════════════════════════════════════════════════
print("\n【5】原有三条判据不许被改坏（它们各有一次真机事故）")
# ══════════════════════════════════════════════════════════════════════════
_e = np.zeros((80, 200, 3), dtype=np.uint8)
ok, why = make_bot(3).validate_route_image(_e, "empty.png")
check("① 空图 → 判废", ok, False)
check_true("② 原因说得对", "一个路线像素都没有" in why)
ok, why = make_bot(3).validate_route_image(make_route()[:, :22], "tiny.png")
check("③ 像素太少 → 判废", ok, False)
_no_goal = make_route()
_no_goal[40, 140] = RIGHT                    # 去掉段末 goal
_no_goal[40, 20] = RIGHT
ok, why = make_bot(3).validate_route_image(_no_goal, "nogoal.png")
check("④ 没有 goal → 判废", ok, False)
check_true("⑤ 原因点明缺 goal", "goal" in why)

print("\n" + "=" * 70)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
