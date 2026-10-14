# -*- coding: utf-8 -*-
'''「闭环自动补段」的自检（2026-10-12 加）。

配合 `MapleStoryAutoBot._auto_close_loop` / `check_route_loop_closure`。

## 防的是什么（真机现场）

用户日志：

    [巡逻闭环] 这条巡逻线**闭不上环**：最后一段终点 (120,88) 到第一段最近像素 44px，
               超过失联救援范围 40px。
               后果：走完最后一段会循环回第一段但接不上 → 指令真空 → 干站不动 →
               看门狗 10s 后抽随机动作乱走。

**44px** —— 只差 4px 就过线，却让**整张图不可用**，只能重录。而引擎明明知道
两端的坐标，补一条几十像素的直线就能接上。这是整个寻路里**收益/成本最差**的一处。

## 用例要点

  【1】★ 复现 44px 现场 → **自动补段、闭环真的接上了**（不是只打条日志）
  【2】补出来的段**方向/终点都对**（终点必须盖在第一段的像素上）
  【3】不该补的三种情况必须**退回报错**，不许乱猜：
        纵向差太大（要画 up/down = 爬梯子/下跳）/ 缺口太远（两段不在同一层）/ 开关关掉
  【4】★ 本来就能跑的情况（≤ rescue_range）**不许动它** —— 改动面越小越好

用法：
    python -m tools.verify_loop_autoclose
'''
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml                          # noqa: E402
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
RIGHT = next(c for c, v in CODES.items() if v.strip().startswith("right"))
LEFT = next(c for c, v in CODES.items() if v.strip().startswith("left"))
GOAL = next(c for c, v in CODES.items() if v.strip().split()[-1] == "goal")
H, W = 80, 260
RESCUE = int(CFG["route"]["rescue_range"])


def make_first(x0=20, x1=100, y=40):
    '''第一段：一条横线（这就是"起点那一带"）。'''
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[y, x0:x1 + 1] = RIGHT
    img[y, x0] = GOAL
    return img


def make_last(goal_x, goal_y=40):
    '''最后一段：终点（goal 质心）落在 (goal_x, goal_y)。'''
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[goal_y, min(goal_x, 200):goal_x + 1] = RIGHT
    img[goal_y, goal_x] = GOAL
    return img


def make_bot(gx, gy=40, **over):
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.color_code = dict(CODES)
    b.color_code_up_down = {}
    b.img_routes = [make_first(), make_last(gx, gy)]
    b.cfg = {"route": dict(CFG["route"]), "watchdog": dict(CFG["watchdog"])}
    b.cfg["route"].update(over)
    return b


print("=" * 70)
print("闭环自动补段 · 自检")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】★ 复现 44px 现场 → 自动补段，且闭环**真的**接上了")
# ══════════════════════════════════════════════════════════════════════════
# 第一段到 x=100，最后一段 goal 在 x=144 ⇒ 缺口 44px（> rescue 40）
b = make_bot(144)
_gap = b.check_route_loop_closure()
check("① 量出来的缺口就是 44px（复现现场）", _gap, 44)
check("② ★ 自动补了一条连接段（2 段 → 3 段）", len(b.img_routes), 3)
_again = b.check_route_loop_closure()
check_true(f"③ ★ 补完之后再量，闭环接上了（{_again} ≤ search_range 10）",
           _again is not None and _again <= CFG["route"]["search_range"])
            # ⚠️ 不要用 list.index(数组)：numpy 数组的 == 返回数组，index() 会抛
            #    "truth value of an array is ambiguous"（自检第一次跑就踩了）。
            #    改成验证**顺序语义**：第一段必须还在 0 号位（追加而非插入）。
check("④ 第一段仍在 0 号位（是「追加」不是「插入」）",
      tuple(int(v) for v in b.img_routes[0][40, 20]), GOAL)

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】补出来的段：方向对、终点盖在第一段的像素上")
# ══════════════════════════════════════════════════════════════════════════
c = b.img_routes[-1]
check("① 终点 (100,40) 是 goal（踩到就切回第一段）",
      tuple(int(v) for v in c[40, 100]), GOAL)
check("② 起点 (144,40) 不是 goal（那是上一段的终点，别重复标记）",
      tuple(int(v) for v in c[40, 144]) != GOAL, True)
check_true("③ 中间是**纯 left** 色（从 144 往 100 走）",
           all(tuple(int(v) for v in c[40, x]) == LEFT for x in range(101, 144)))
check("④ 段外没有多余像素（不许画出穿过墙的线）",
      int((np.any(c != 0, axis=2)).sum()), 45)   # x=100..144 含两端 = 45

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】不该补的三种情况 → **退回报错**，不许乱猜")
# ══════════════════════════════════════════════════════════════════════════
b = make_bot(144, gy=70)                 # 纵向差 30px（图高 80，别越界）
g = b.check_route_loop_closure()
check("① 纵向差太大（要画 up/down = 爬梯子/下跳）→ 不补", len(b.img_routes), 2)
check("② 仍然把缺口报出来（调用方照旧报错）", g, 44 + 30)

b = make_bot(240)                        # 横向 140px > auto_close_max 60
g = b.check_route_loop_closure()
check("③ 缺口太远（两段不在同一层）→ 不补", len(b.img_routes), 2)

b = make_bot(144, auto_close_loop=False)
g = b.check_route_loop_closure()
check("④ 开关关掉 → 不补（退回老行为）", len(b.img_routes), 2)

b = make_bot(144, auto_close_y_tol=0)     # 容差 0：同行的 0 差仍然放行
g = b.check_route_loop_closure()
check("⑤ 容差 0 但纵向差本来就是 0 → 仍然补", len(b.img_routes), 3)

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】★ 本来就能跑的（≤ rescue_range）**不许动**")
# ══════════════════════════════════════════════════════════════════════════
b = make_bot(120)                        # 缺口 20px ≤ 40 → 靠失联救援能跑
g = b.check_route_loop_closure()
check("① 缺口 20px（≤ rescue 40）→ **不加段**，行为与老版一致", len(b.img_routes), 2)
check("② 距离照常报出来", g, 20)

b = make_bot(105)                        # 缺口 5px ≤ search_range → 完全正常
g = b.check_route_loop_closure()
check("③ 缺口 5px（≤ search_range）→ 正常闭环，不加段", len(b.img_routes), 2)
check("④ 距离照常报出来", g, 5)

print("\n" + "=" * 70)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
