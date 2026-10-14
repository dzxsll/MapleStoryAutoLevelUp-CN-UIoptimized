# -*- coding: utf-8 -*-
'''「自动化路线录制」的自检（2026-10-12 加）。

配合 `tools/routeRecorder.py` 的 `auto_split_step()` / `maybe_auto_arm()`。

## 盯的是什么

**自动分段最怕误判**：把"打怪时来回蹭两下"切成分段，路线就被切碎；
而它**又不能太钝**，否则短平台上来回走永远不触发。所以用例【1】两头都要钉。

**自动开录最怕绕过那道闸**：`arm_recording` 存在的唯一理由是
「**定位无效时拒绝开始**」—— 否则起点会被钉死在 (0,0)，而且事后从画面上看不出来。
自动化加了个触发条件，**绝不能**顺手把这道闸拆掉。用例【2】③ 专门钉它。

## 落地方式

判据调**真实入口**：分段走真实 `mark_waypoint()`（断言 `_waypoint_count` /
`_want_save_route`），开录走真实 `arm_recording()`（断言 `_armed`）——
不自己复刻一遍（项目纪律）。

用法：
    python -m tools.verify_auto_record
'''
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml                  # noqa: E402
from tools.routeRecorder import RouteRecorder           # noqa: E402

FAIL: list = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


CFG = load_yaml("config/config_default.yaml")


def make_rec(auto_split=True, auto_arm=True, confirm=3, min_travel=40,
             reverse=30, cooldown=0.0, armed=True, pos_ok=True, enabled=True):
    '''造一个只带这两个功能所需字段的录制器（不跑 GUI / 不连游戏 / 不写盘）。

    ⚠️ cooldown 默认给 **0**：自检要的是"判据本身"确定可复现，
       真实默认值是 3 秒（见 config）。
    '''
    r = RouteRecorder.__new__(RouteRecorder)
    r.cfg = dict(CFG)
    r.cfg["route_recoder"] = dict(CFG["route_recoder"])
    r.cfg["route_recoder"].update({
        "auto_split": auto_split, "auto_arm": auto_arm,
        "auto_split_confirm_frames": confirm,
        "auto_split_min_travel": min_travel,
        "auto_split_reverse_travel": reverse,
        "auto_split_cooldown": cooldown,
    })
    r.color_code = {}
    for k, v in CFG["route"]["color_code"].items():
        r.color_code[tuple(int(p) for p in k.split(","))] = v
    for k, v in CFG["route"]["color_code_up_down"].items():
        r.color_code[tuple(int(p) for p in k.split(","))] = v
    r.color_code_up_down = {}
    r.img_map = np.zeros((60, 120, 3), dtype=np.uint8)
    r.img_route = np.zeros((60, 120, 3), dtype=np.uint8)
    r.loc_player_global = (20, 30)
    r.loc_player_global_last = None
    r._blob_pixels = {}
    r._route1_start = None
    r.idx_routes = 0
    r._home_recording = False
    r._waypoint_count = 0
    r._want_save_route = False
    r.is_enable = enabled
    r._armed = armed
    r._pos_ok = pos_ok
    r.rerecord_seg = 0
    r._rerecord_backup = None
    r._emit_state = lambda: None          # 面板协议不在本自检范围
    r._auto_split_reset()
    return r


def feed(r, xs):
    '''按给定 x 序列喂位置（y 固定 30），每喂一个调一次 auto_split_step()。'''
    for x in xs:
        r.loc_player_global = (x, 30)
        r.auto_split_step()


# ══════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("自动化路线录制 · 自检")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】自动折返分段 auto_split_step()")
# ══════════════════════════════════════════════════════════════════════════
# ── 该触发：往右走 40px，再往左真的走出 30px ──────────────────────────────
r = make_rec()
feed(r, list(range(20, 61)))            # 右：20 → 60（本段已走 40px）
check("① 只往一个方向走 → 不触发（还没折返）", r._waypoint_count, 0)
feed(r, list(range(59, 27, -1)))        # 左：又走了 32px（≥ reverse_travel 30）
check("② 走够里程 + 反向**真的走出** 30px+ → **触发一次分段**", r._waypoint_count, 1)
check("③ 触发走的是真实 mark_waypoint（置了 _want_save_route）",
      r._want_save_route, True)

# ── ★ 不该触发：光反向几帧不算，得真走出去（2026-10-12 真机踩坑的那条）──────
r = make_rec(reverse=30)
feed(r, list(range(20, 61)))            # 右 40px
feed(r, [59, 58, 57, 56, 55])           # 反向 5 帧，但只走 5px
check("④ ★ 反向 5 帧但只走 5px（< reverse_travel）→ **不触发**\n"
      "      （真机上这就是'边打怪边被撞'的来回蹭，切成碎段就是它害的）",
      r._waypoint_count, 0)
feed(r, list(range(54, 24, -1)))        # 继续反向走到累计 35px
check("⑤ 反向累计越过 30px → 这才触发", r._waypoint_count, 1)

# ── 不该触发：抖动（±1 来回）──────────────────────────────────────────────
r = make_rec()
feed(r, [50, 51, 50, 51, 50, 51, 50, 51, 50, 51, 50, 51])
check("⑥ 原地 ±1 来回蹭（抖动）→ 不触发", r._waypoint_count, 0)

# ── 不该触发：里程不够 ───────────────────────────────────────────────────
r = make_rec()
feed(r, [20, 21, 22, 23, 24])           # 只走了 4px
feed(r, [23, 22, 21, 20, 19])           # 反向 5px，总里程 9 < 40
check("⑦ 本段里程不到 min_travel → 不触发（不许把一小抖切成分段）",
      r._waypoint_count, 0)

# ── 不该触发：反向帧数不够 ───────────────────────────────────────────────
r = make_rec(confirm=3)
feed(r, list(range(20, 101)))           # 右：20 → 100
feed(r, [70, 40])                       # 反向 2 帧、已走 60px
check("⑧ 反向不足 confirm_frames 帧 → 不触发", r._waypoint_count, 0)
feed(r, [10])                           # 第 3 帧到齐
check("⑨ 凑够 3 帧 → 这一帧才触发", r._waypoint_count, 1)

# ── ★ 冷却：刚切过就不再切（防连环碎段）────────────────────────────────
r = make_rec(cooldown=999.0)
feed(r, list(range(20, 61)) + list(range(59, 27, -1)))
check("⑩ 第一次折返 → 切一次", r._waypoint_count, 1)
r._auto_split_reset()                   # 模拟换段后的状态清零
feed(r, list(range(28, 69)) + list(range(68, 36, -1)))
check("⑪ ★ 冷却期内又走了一个来回 → **不切**", r._waypoint_count, 1)

# ── 不该触发：开关关掉 / 闸门未开 ────────────────────────────────────────
r = make_rec(auto_split=False)
feed(r, list(range(20, 61)) + list(range(59, 27, -1)))
check("⑫ auto_split=false → 永不触发（老行为）", r._waypoint_count, 0)

r = make_rec(armed=False)
feed(r, list(range(20, 61)) + list(range(59, 27, -1)))
check("⑬ 还没【开始录】→ 不触发", r._waypoint_count, 0)

r = make_rec(pos_ok=False)
feed(r, list(range(20, 61)) + list(range(59, 27, -1)))
check("⑭ 定位无效 → 不触发", r._waypoint_count, 0)

r = make_rec(enabled=False)             # 面板上"暂停记录"
feed(r, list(range(20, 61)) + list(range(59, 27, -1)))
check("⑮ 暂停中 → 不触发", r._waypoint_count, 0)

# ── 换段后状态要归零（否则新段拿着上一段的方向/里程继续判）──────────────
r = make_rec()
r._as_dir, r._as_flip, r._as_travel, r._as_back = -1, 2, 123.0, 45.0
r._as_prev = (99, 30)
r._reset_route_canvas()
check("⑯ _reset_route_canvas 会清零检测状态（含反向里程）",
      (r._as_dir, r._as_flip, r._as_travel, r._as_back, r._as_prev),
      (0, 0, 0.0, 0.0, None))

# ── 竖直方向（爬绳子）也要能判 ───────────────────────────────────────────
r = make_rec()
for y in range(30, 71):                 # 往下走 40px
    r.loc_player_global = (50, y)
    r.auto_split_step()
for y in range(69, 37, -1):             # 连续往上，真走出 32px
    r.loc_player_global = (50, y)
    r.auto_split_step()
check("⑰ 竖直方向折返（爬绳/上下平台）同样能判", r._waypoint_count, 1)

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】★ 切点精度：切在**真正的折返点**，不是「敢确认」的那一刻")
# ══════════════════════════════════════════════════════════════════════════
# 现场（2026-10-12 真机）：判据要"反向走出 30px"才确认，而确认时角色已经在回程上
# 走了 30px ⇒ mark_waypoint 切的是**当前位置** ⇒ 切出来的两条线**各自仍同时含
# 左右两个方向**（体检报 route1 7 处 / route2 40 处冲突）。修法：确认时回滚白板 +
# 把位置放回折返点。
def feed_paint(r, seq):
    '''像 run_once 那样：先画一笔、再跑自动分段。seq = [(x, action), ...]'''
    for (x, action) in seq:
        r.loc_player_global = (x, 30)
        r.paint_step(action, False)
        r.auto_split_step()


def count_bgr(img, bgr):
    return int(((img[:, :, 0] == bgr[0]) & (img[:, :, 1] == bgr[1])
                & (img[:, :, 2] == bgr[2])).sum())


RP = make_rec()                          # 右走到 60 再回程
RP.img_route = np.zeros((60, 140, 3), dtype=np.uint8)
RP.t_last_draw_blob = 0.0
feed_paint(RP, [(x, "right none none") for x in range(20, 61)])   # 折返点 = (60,30)
_n_right_before = count_bgr(RP.img_route, (255, 0, 0))            # right 的 BGR
check_true("① 去程画上了 right 色的线（用例前提）", _n_right_before > 0)

# 反向第 1 帧：此刻应当拍下快照并记下折返点
RP.loc_player_global = (59, 30)
RP.paint_step("left none none", False)
RP.auto_split_step()
check("② 疑似折返的第 1 帧 → 记下的折返点是 (60,30)",
      tuple(RP._as_turn) if RP._as_turn else None, (60, 30))
check_true("③ 同一帧拍下了白板快照", RP._as_snapshot is not None)

feed_paint(RP, [(x, "left none none") for x in range(58, 27, -1)])  # 继续反向到确认
check("④ 确认并切段了一次", RP._waypoint_count, 1)
check("⑤ ★ 白点画在**真正的折返点** (60,30)，不是确认时所在的 (约29,30)",
      tuple(int(v) for v in RP.img_route[30, 60]), (255, 255, 255))
# 回滚：确认前误画的那段回程（约 32px 的 left）必须被抹掉。
# ⚠️ 残留不是 0 而是个位数：快照拍在 flip==1 那一帧**之后**，含它自己那一笔
#    （1~3 个像素）—— 这是已知且可接受的（折返点上的白点又会盖掉一部分）。
_n_left = count_bgr(RP.img_route, (0, 0, 255))
check_true(f"⑥ ★ 误画的回程像素被回滚（32 → 只剩 {_n_left} 个，≤5 即可）",
           _n_left <= 5)
# ⚠️ 去程的 right 像素会**少十几个** —— 那是白点（半径 2 ≈ 13 像素）盖在折返点上，
#    是**正确行为**（折返点本来就要标记），不是被回滚吃掉了。
_n_right_after = count_bgr(RP.img_route, (255, 0, 0))
check_true(f"⑦ 去程的 right 像素只被白点盖掉十几个（{_n_right_before} → {_n_right_after}）",
           _n_right_after >= _n_right_before - 13 and _n_right_after > 0)

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】自动开录 maybe_auto_arm()")
# ══════════════════════════════════════════════════════════════════════════
r = make_rec(armed=False, pos_ok=True)
r.maybe_auto_arm(["right"])
check("① 定位有效 + 按下方向键 → 自动开录", r._armed, True)

r = make_rec(armed=False, pos_ok=True)
r.maybe_auto_arm([])
check("② 定位有效但没按键 → 不开录（还在站着）", r._armed, False)

r = make_rec(armed=False, pos_ok=False)
r.maybe_auto_arm(["right"])
check("③ ★ 定位**无效** + 按下方向键 → **仍然拒绝开录**\n"
      "      （那道闸必须还在：否则起点会被钉死在 (0,0) 而且事后看不出来）",
      r._armed, False)

r = make_rec(armed=False, pos_ok=True, auto_arm=False)
r.maybe_auto_arm(["right"])
check("④ auto_arm=false → 老行为（必须自己点【开始录】）", r._armed, False)

r = make_rec(armed=True, pos_ok=True)
r.loc_player_global_last = (11, 22)
r.maybe_auto_arm(["right"])
check("⑤ 已经开录了 → 不重复开（也不该清掉画线基准）",
      (r._armed, r.loc_player_global_last), (True, (11, 22)))

r = make_rec(armed=False, pos_ok=True)
r.maybe_auto_arm(["up"])
check("⑥ 只按上（爬绳）也算移动 → 开录", r._armed, True)

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】配置项")
# ══════════════════════════════════════════════════════════════════════════
_rr = CFG.get("route_recoder", {})
# ⚠️ 2026-10-13 起 auto_arm / auto_split **默认 false**（用户要求：录制回到手动点路段）。
#    自动化代码与开关都保留，需要时把这两项设 true；本脚本下面的用例用显式 True 覆盖，
#    照旧验自动通路（本段只改"出厂默认值"的期望）。
for k, want in (("auto_arm", False), ("auto_split", False),
                ("auto_split_min_travel", 40), ("auto_split_confirm_frames", 3),
                ("auto_split_reverse_travel", 30), ("auto_split_cooldown", 3.0)):
    check_true(f"① config_default.yaml 里有 route_recoder.{k}", k in _rr)
    check(f"② route_recoder.{k} 默认值", _rr.get(k), want)

print("\n" + "=" * 70)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
