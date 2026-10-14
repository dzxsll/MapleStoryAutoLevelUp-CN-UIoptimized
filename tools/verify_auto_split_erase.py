# -*- coding: utf-8 -*-
'''「折返回滚」的自检（2026-10-13 加）。

## 盯的是什么

自动分段确认折返时会把白板**回滚**到折返点，抹掉"确认之前"误画上去的那段回程。
回滚要回到的是**反向发生前**那一刻，而原来的实现是在"检测到反向的第 1 帧"
（`_as_flip == 1`）**当场**给活画布拍快照 —— 可那一帧的**回程第一笔已经画上去了**：
`paint_step` 跑在 `auto_split_step` **之前**（见 routeRecorder 的帧循环）。

- 走路的线：第一笔 1px，残留看不出来（这就是它一直没被发现的原因）；
- **跳跃**：第一笔是 `cv2.circle(radius=2)` 的实心圆，正好 **13 个像素**
  （普查里所有跳跃色码的连通块都是 13px，来源就是这里）。

真机实测（用户 2026-10-13 00:53 那条 route1）：他在折返点立刻按了跳，
去程 `left none jump (74,54)x12` 与残留的 `right none jump (70,54)x13` 只差 4px
⇒ 体检报 **18 处方向冲突**，`tools/route_conflict_spots.py` 聚成一团 (75,54)。

## 落地方式

判据调**真实入口**：`paint_step()` / `auto_split_step()` / `_reset_route_canvas()`
都是真的，画布也是真的；"有多少方向冲突"调项目自己的判据
`src/utils/route_audit._count_direction_conflicts`（**不自己复刻一遍**）。

判的是**存盘那一刻**的画布（= 会写成 route1.png 的那张），不是收工后的内存状态 ——
因为后者会被后续帧继续画。

用例【2】是**变异测试**：把每帧"记住上一帧"的时机换回修复前的顺序
（先记**当前**画布、再判折返）⇒ 必须**复现**出那 13px 残留。抓不到就说明这条自检
只是"跟着实现走"。

用法：
    python -m tools.verify_auto_split_erase
'''
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml                        # noqa: E402
from src.utils.route_audit import _count_direction_conflicts  # noqa: E402
from tools.routeRecorder import RouteRecorder                 # noqa: E402

FAIL: list = []
CFG = load_yaml("config/config_default.yaml")

Y = 30               # 自检里走的那一行
X0, XTURN = 80, 38   # 起点 / 折返点
REV_STEP = 6         # 回程每帧走多远（见 drive 里为什么不能是 1px）


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


def make_rec(**over):
    '''造一个能真跑 paint_step / auto_split_step 的录制器（不跑 GUI / 不写盘）。

    ⚠️ 为什么不 import `tools/verify_auto_record.make_rec` 复用那个夹具：
       那个模块的用例是**模块级**跑的（没有 `if __name__ == "__main__"`），
       import 它会把整套用例连带跑一遍、并污染输出。
       ⇒ 夹具各自一份；**判据**仍统一走项目自己的函数（见文件头）。
    '''
    r = RouteRecorder.__new__(RouteRecorder)
    r.cfg = dict(CFG)
    r.cfg["route_recoder"] = dict(CFG["route_recoder"])
    r.cfg["route_recoder"].update({
        "auto_split": True, "auto_arm": True,
        "auto_split_confirm_frames": 3,
        "auto_split_min_travel": 40,
        "auto_split_reverse_travel": 30,
        "auto_split_cooldown": 0.0,
        # ⚠️ 必须是**负数**：paint_step 的判据是 `dt > blob_cooldown`，而自检里帧与帧
        #    只隔几微秒、`time.time()` 还没跳刻度 ⇒ dt 可能是 **0.0**，设 0.0 就成了
        #    `0.0 > 0.0` = False，**圆点一个都画不出来**。
        #    （踩过：这样 ④ 会"假通过"—— 圆点根本没画，残留当然是 0。变异测试也因此
        #      复现不出残留，光看着像"修好了"。）
        "blob_cooldown": -1.0,
    })
    r.cfg["route_recoder"].update(over)
    r.color_code = {}
    for k, v in CFG["route"]["color_code"].items():
        r.color_code[tuple(int(p) for p in k.split(","))] = v
    for k, v in CFG["route"]["color_code_up_down"].items():
        r.color_code[tuple(int(p) for p in k.split(","))] = v
    r.color_code_up_down = {}
    r.img_map = np.zeros((60, 120, 3), dtype=np.uint8)
    r.img_route = r.img_map.copy()
    r.loc_player_global = (X0, Y)
    r.loc_player_global_last = None
    r._blob_pixels = {}
    r.t_last_draw_blob = 0.0
    r._route1_start = None
    r.idx_routes = 0
    r._home_recording = False
    r._waypoint_count = 0
    r._want_save_route = False
    r.is_enable = True
    r._armed = True
    r._pos_ok = True
    r.rerecord_seg = 0
    r._rerecord_backup = None
    r._emit_state = lambda: None
    r._auto_split_reset()
    return r


def drive(r, jump_on_flip=True, post_split_frame=False):
    '''真实时序：往左走到折返点（途中跳一次）→ 折返 → 在**折返那一帧**又按跳 → 回程走远。

    返回**这一段存盘那一刻**的画布（= 会写成 route1.png 的那张）。

    `post_split_frame=True`：分段确认后**不复位画布**，再多画一笔 ——
    模拟"save_route 那条路被拦下、帧末没复位"（它有提前返回，真实存在）。
    '''
    saved = {}

    def step(x, action, blob):
        # ⚠️ 顺序与真实帧循环一致：paint_step 在前、auto_split_step 在后。
        #    这个顺序就是本 bug 的根：拍快照时这一帧的笔已经落下去了。
        r.loc_player_global = (x, Y)
        r.paint_step(action, blob)
        r.auto_split_step()
        if r._want_save_route:
            saved.setdefault("img", r.img_route.copy())
            if not post_split_frame:
                r._reset_route_canvas()      # save_route 帧末的复位
                r._want_save_route = False
            return True
        return False

    # ⚠️ 回程每一步走 REV_STEP（=6px），不是 1px：
    #    切点被放回折返点后，`mark_waypoint()` 会在那儿画一枚 **radius=2 的白圆点**。
    #    回程那枚跳跃圆点若只离切点 1px，白点会**整个把它盖掉** —— 实测残留 13px → 0，
    #    变异测试就永远复现不出来（我在这上面空转了三轮）。
    #    真机上这个距离是 4px（用户 00:53 那条 route1：切点 x≈74、圆点 x≈70），
    #    白点盖不到 ⇒ 自检里也要把两者拉开。
    seq = [(x, "left none jump" if x == XTURN + 4 else "left none none", x == XTURN + 4)
           for x in range(X0, XTURN - 1, -1)]        # 80 → 38，一路向左
    # ↓ 回程第一帧就是 _as_flip == 1，而它**同时带着回程第一笔**（按跳 ⇒ 13px 圆点）
    seq += [(XTURN + REV_STEP * (j + 1), "right none jump" if j == 0 else "right none none",
             j == 0) for j in range(6)]              # 44,50,56,62,68,74

    if post_split_frame:
        _tail = XTURN + REV_STEP * 6
        seq += [(_tail + REV_STEP, "right none none", False),
                (_tail + REV_STEP * 2, "right none none", False)]

    for x, a, b in seq:
        if step(x, a, b) and not post_split_frame:
            break
    return saved["img"]


def judge(img, r):
    '''用**项目自己的**判据数方向冲突；它按角色分左右，所以要传 BGR 键。

    画布在内存里是 BGR（见 save_route 的说明），而 config 的 color_code 键是 RGB。
    '''
    cc_bgr = {tuple(reversed(rgb)): v for rgb, v in r.color_code.items()}
    return _count_direction_conflicts(img, cc_bgr, radius=8)


def count_cmd(img, cmd):
    n = 0
    for k, v in CFG["route"]["color_code"].items():
        if v != cmd:
            continue
        bgr = tuple(int(p) for p in k.split(","))[::-1]
        n += int(np.all(img == np.array(bgr, dtype=np.uint8), axis=2).sum())
    return n


def bgrs(cmd):
    return {tuple(int(p) for p in k.split(","))[::-1]
            for k, v in CFG["route"]["color_code"].items() if v == cmd}


# ══════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("折返回滚 · 自检（折返点按跳 ⇒ 旧段不许留下新方向的像素）")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】折返点按跳：回程那枚 13px 圆点必须被回滚掉")
# ══════════════════════════════════════════════════════════════════════════
r = make_rec()
img1 = drive(r, jump_on_flip=True)
n_conf, n_l, n_r = judge(img1, r)
check("① 折返被确认（走的是真实 mark_waypoint）", r._waypoint_count, 1)
check("② 切点被放回真正的折返点", r.loc_player_global, (XTURN, Y))
check("③ 存盘时旧段里**新方向（right）像素剩几个**", n_r, 0)
check("④ 存盘时旧段里那枚 `right none jump` 圆点剩几个",
      count_cmd(img1, "right none jump"), 0)
check("⑤ 项目判据数出来的方向冲突", n_conf, 0)
check_true("⑥ 去程的像素还在（回滚不是把整段擦掉）", n_l > 30)

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】变异测试：把「记住上一帧」换回修复前的顺序 ⇒ 必须复现残留")
# ══════════════════════════════════════════════════════════════════════════
# 修复前的语义有**两半**，两半都要还原，少还原一半就复现不出来：
#   ① 快照取**当前活画布**（此刻这一帧的笔已经落下去了）⇒ 污染；
#   ② 切点仍取**上一帧位置**（修复前是 `prev`，不是 `_as_last_pos`）。
# ⚠️ 只还原①不还原②会得到一个**假阴性**：切点变成"回程第一帧的位置"，
#    而残留那枚圆点正好画在那里 ⇒ `mark_waypoint()` 的白圆点（radius=2）
#    会把它整个盖掉，画布上看着干干净净。（我在这上面空转了三轮。）
r = make_rec()
_prev_pos = {"v": None}


def _old_order(_r=r, _st=_prev_pos):
    _cur = tuple(int(v) for v in _r.loc_player_global)
    _r._as_remember()                 # ① 快照 = 当前活画布（污染）
    _r._as_last_pos = _st["v"]        # ② 切点 = 上一帧位置（修复前的 prev）
    _st["v"] = _cur
    return _r._auto_split_detect()


r.auto_split_step = _old_order
img2 = drive(r, jump_on_flip=True)
n_conf_m, _n_l_m, n_r_m = judge(img2, r)
print(f"       （变异后：right {n_r_m}px、"
      f"right-jump 圆点 {count_cmd(img2, 'right none jump')}px、冲突 {n_conf_m} 处）")
check_true("① 变异后**确实复现**了那 13px 圆点", count_cmd(img2, "right none jump") >= 13)
check_true("② 变异后**确实复现**了方向冲突", n_conf_m > 0)
check("③ 对照：修好的那条（用例【1】）是 0 —— 本自检能区分好坏", (n_r, n_conf), (0, 0))

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】跳跃色保护表也必须一起回滚（+ 一条「画布没复位」的后续帧）")
# ══════════════════════════════════════════════════════════════════════════
r = make_rec()
img3 = drive(r, jump_on_flip=True, post_split_frame=True)
n_conf3, _n_l3, n_r3 = judge(img3, r)
check("① 后续那一笔线不能把回程圆点「保护」回来",
      count_cmd(img3, "right none jump"), 0)
check("② 存盘时新方向像素仍是 0", n_r3, 0)
check("③ 方向冲突仍是 0", n_conf3, 0)
_stale = [xy for xy, c in (r._blob_pixels or {}).items() if c in bgrs("right none jump")]
check("④ 保护表里不许留下回程那枚圆点", _stale, [])

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】不按跳的普通折返：本来就只残留 1~2px，修完必须是 0")
# ══════════════════════════════════════════════════════════════════════════
r = make_rec()
img4 = drive(r, jump_on_flip=False)
check("① 回程第一笔是线（不是圆点）时也是 0", judge(img4, r)[2], 0)
check("② 0 冲突", judge(img4, r)[0], 0)

# ══════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
if FAIL:
    print(f"【异常】{len(FAIL)} 项不符：")
    for f in FAIL:
        print(f"   ✗ {f}")
    sys.exit(2)
print("【OK】折返回滚自检全部通过")
