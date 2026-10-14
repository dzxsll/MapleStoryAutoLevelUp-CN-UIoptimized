# -*- coding: utf-8 -*-
'''路线**离线重生成**：读录制轨迹，重新生成 `route{N}.png`（2026-10-12 加）。

## 它解决什么问题

录制器原来只落图片，录完就"死"了 —— 路线不满意，只能回游戏**重走一遍**。
现在录制器同时落一份**操作轨迹**（`minimaps/<地图>/record_trace.jsonl`，格式见
`src/utils/route_trace.py`），本工具拿轨迹就能离线重算路线：

  · **去噪**：定位抖动产生的孤立跳点，掉；
  · **自动重分段**：用户把 A→B→A 一整圈录成了一段（去程 right 和回程 left
    叠在同一张图上 → 引擎分不清，实测「启动后直奔右边一直走」），
    这里按轨迹在折返点自动切开；
  · **方向冲突复查**：用**引擎同款判据**（route_audit）数一遍，改前 / 改后都报；
  · **闭环 / 长直线告警**：最后一段走不回第一段起点会卡在终点；单笔跨度过大
    往往是"定位丢了几帧、直线糊过去"。

## 关键：落笔**不复刻**，直接调录制器真实的 `paint_step`

项目纪律（见 tools/verify_recorder_action.py 的开头）："怎么画"必须调真实入口，
自己复刻一遍就测不到真东西了。路线图的颜色是**反着存**的（录制器写 RGB→BGR、
引擎读入再 BGR→RGB，两次抵消 —— 详见 `_stamp_goal` 的长注释），
自己实现一版几乎必然把颜色搞反、整张路线被判废。
⇒ 本工具用 `RouteRecorder.__new__` 造一个只带必要字段的实例，
   调**同一个 `paint_step`**；另有一条自检把"重生成 == 原图"逐像素钉死。

## 底图从哪来

`route{N}.png` 本身就是「地图底图 + 路线像素」，所以**把路线色剥掉就是底图**
（`remove_color_code_pixels`，与录制器 `_reset_route_canvas` 是同一个函数）。
⇒ 不依赖 `map.png` 是否存在，也不受"录制中途地图扩容过"的影响。

用法：
    python -m tools.routeRebuild --map 沼泽地2               # 重生成（写盘前先备份）
    python -m tools.routeRebuild --map 沼泽地2 --report-only  # 只看报告，不写任何文件
    python -m tools.routeRebuild --map 沼泽地2 --no-split     # 关掉自动重分段
    python -m tools.routeRebuild --map 沼泽地2 --prune        # 段数变少时删掉多余的旧图
'''
from __future__ import annotations

import argparse
import os
import shutil
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml, load_image, imwrite_unicode, active_config_path  # noqa: E402
from src.utils.paths import resource_path                                             # noqa: E402
from src.utils.route_trace import (                                                   # noqa: E402
    load_last_session, codes_from_json, TRACE_NAME,
)
# ⚠️ 直接用 route_audit 的**私有**判据，而不是自己再写一遍"什么叫方向冲突"：
#    那个口径是 `audit_map` 判废时用的，抄一份两边迟早会飘
#    （项目里已经吃过"录制器说没事、挂机时不启用"这种分裂的亏）。
#    若将来它改成公开名，这里跟着改。
from src.utils.route_audit import _count_direction_conflicts                           # noqa: E402
import tools.routeRecorder as _rr                                                     # noqa: E402
from tools.routeRecorder import RouteRecorder                                          # noqa: E402

BACKUP_DIRNAME = "_backup_重生成前"

# 闭环判据：与 routeRecorder._emit_state 的 loopdist 说明是同一套口径
#   ≤10px 接得上 / ≤40px 靠失联救援 / >40px 会卡在终点
LOOP_OK, LOOP_RESCUE = 10, 40

# 单笔跨度超过这么多像素就告警（正常一帧走 0.3~2px，10fps 也不会过 10）
LONG_DRAW_WARN = 40


# ═══════════════════════════════════════════════════════════════════════════
#  时间冻结：让重放**确定性**
# ═══════════════════════════════════════════════════════════════════════════
# paint_step 的 blob 冷却是 `time.time() - t_last_draw_blob > cooldown`（真实墙上时钟）。
# 不接管它的话，同一份轨迹重生成两次可能得到**不同的图**，自检也没法钉"重生成 == 原图"。
_REPLAY_NOW = [None]


class _FrozenTime:
    '''重放期间把 routeRecorder 模块里的 `time.time()` 换成"当前采样时刻"。'''

    def __init__(self, module):
        self._module = module
        self._orig = None

    def __enter__(self):
        import types
        real_time = self._module.time
        self._orig = real_time

        class _T:
            @staticmethod
            def time():
                return _REPLAY_NOW[0] if _REPLAY_NOW[0] is not None else real_time.time()

            def __getattr__(self, name):
                return getattr(real_time, name)

            # ⚠️★ 2026-10-13 修：`SimpleNamespace(time=_T())` 把 `time.time` 变成了
            #   `_T` 的**实例**，而 `routeRecorder.paint_step:1444` 写的是
            #       dt = time.time() - self.t_last_draw_blob
            #   ⇒ 先取属性（拿到实例）再 `()` 调用它 ⇒
            #       TypeError: '_T' object is not callable
            #   加一个 `__call__` 委托给 `time()` 即可（保留 `__getattr__` 转发，
            #   如 `time.sleep`）。等价改法：`SimpleNamespace(time=_T.time)`
            #   （`time` 是 staticmethod，未绑定也能直接调）。
            #   为什么直到今天才炸：那条分支只在**轨迹里有跳跃/传送像素**时才被走到
            #   （`is_draw_blob=True`），而 2026-10-13 之前录制器**一个跳跃像素都录不出来**
            #   （Alt 修饰键 bug，见 KeyBoardListener）⇒ 这条路径从未被执行过。
            #   跳跃修好后第一次真机跑 `routeRebuild` 就崩了。
            def __call__(self):
                return self.time()

        self._module.time = types.SimpleNamespace(time=_T())
        return self

    def __exit__(self, *exc):
        if self._orig is not None:
            self._module.time = self._orig
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  落笔：复用录制器真实的 paint_step
# ═══════════════════════════════════════════════════════════════════════════

def make_painter(cfg, codes_main, codes_ud, shape):
    '''造一个"只带必要字段"的录制器，专门用来调它的 `paint_step`。

    ⚠️ 字段清单必须和 `paint_step` 真正用到的对齐（照抄 tools/verify_recorder_action.py
       的 new_recorder）：color_code（**两张表要合并**，缺 up/down 灰线画不出来）、
       cfg["route_recoder"]["blob_cooldown"]、img_route、
       loc_player_global / loc_player_global_last、_blob_pixels、
       _route1_start、idx_routes、_home_recording、t_last_draw_blob。
    '''
    r = RouteRecorder.__new__(RouteRecorder)
    r.cfg = cfg
    r.color_code = {}
    r.color_code.update(dict(codes_main))
    r.color_code.update(dict(codes_ud))
    r.color_code_up_down = dict(codes_ud)
    r.img_map = np.zeros(shape, dtype=np.uint8)
    r.img_route = np.zeros(shape, dtype=np.uint8)
    r.loc_player_global = (0, 0)
    r.loc_player_global_last = None
    r._blob_pixels = {}
    r._route1_start = None
    r.idx_routes = 0
    r._home_recording = False
    # ⚠️ 初值必须和真实录制器一致（0.0）：这样重放的第一帧跳才能像原图一样"必定过冷却"
    r.t_last_draw_blob = 0.0
    return r


def paint_segment(painter, base_bgr, samples):
    '''把一段的采样重放到白板上 → 新的路线图（BGR）。

    Args:
        painter: make_painter 造的实例。**跨段复用**：`t_last_draw_blob` 要延续，
                 真实录制器换段时也不重置它（`_reset_route_canvas` 只清另外三样）。
                 不复用的话，段首那个跳会被错误地重新画出来，和原图对不上。
        samples: [(t, x, y, action, is_blob), ...]，**只含真正落过笔的帧**

    ⚠️ 每段开头要清的三样，和录制器 `_reset_route_canvas` 一一对应：
        img_route（换新白板）、loc_player_global_last（否则第一笔会从上一段终点连过来）、
        _blob_pixels（上一段的跳跃像素不该被"保护"到这一段）。
    '''
    painter.img_route = base_bgr.copy()
    painter.loc_player_global_last = None
    painter._blob_pixels = {}
    for (t, x, y, action, is_blob) in samples:
        # 让 paint_step 里那次 time.time() 读到本次采样的时刻（见 _FrozenTime）
        _REPLAY_NOW[0] = float(t)
        painter.loc_player_global = (int(x), int(y))
        painter.paint_step(action, bool(is_blob))
    return painter.img_route


# ═══════════════════════════════════════════════════════════════════════════
#  底图与折返点白点
# ═══════════════════════════════════════════════════════════════════════════

def strip_route_pixels(painter, img_bgr):
    '''剥掉路线色像素 → 底图（与录制器 `_reset_route_canvas` 是同一个函数）。'''
    return painter.remove_color_code_pixels(img_bgr.copy())


def strip_waypoint_dots(img_bgr, waypoints, radius=4):
    '''把旧的**折返点白点**抹掉（涂黑），只抹轨迹里记过的折返点附近。

    为什么不做"全图抹纯白"：小地图本来就可能含白色区域，全抹会伤底图。
    折返点的位置在轨迹里有事件记录（`ev=waypoint`）—— 这正是"把操作记下来"的用处：
    能**精确定位**要清理的东西，而不是靠猜。
    '''
    for wp in waypoints or []:
        try:
            x, y = int(wp["x"]), int(wp["y"])
        except Exception:                                    # noqa: BLE001
            continue
        cv2.circle(img_bgr, (x, y), radius, (0, 0, 0), thickness=-1)
    return img_bgr


def draw_waypoint_dot(img_bgr, pt, radius=2):
    '''在折返点画白点 —— 与 `mark_waypoint` 完全一致（白色**不在** color_code 里，
    引擎不会把它当成移动指令）。'''
    cv2.circle(img_bgr, (int(pt[0]), int(pt[1])), radius, (255, 255, 255), thickness=-1)
    return img_bgr


def stamp_goal(painter, img_bgr, pt):
    '''在段末压 goal 点 —— 调录制器**真实的 `_stamp_goal`**。

    ⚠️ 这一步不能省：引擎靠 goal 色判断"这一段走到头了、该切下一段"。
       少了它，角色会在段末原地死锁（不是走偏，是根本不知道到了）。
       必须在所有移动线**之后**画（同帧画的线会盖住它）—— 和录制器 save_route 的
       做法一致（见那里的说明）。颜色口径也由 `_stamp_goal` 里的翻转负责，
       本函数绝不自作主张拼颜色。
    '''
    painter.img_route = img_bgr
    painter.loc_player_global = (int(pt[0]), int(pt[1]))
    painter._stamp_goal()
    return img_bgr


# ═══════════════════════════════════════════════════════════════════════════
#  优化 1：去噪
# ═══════════════════════════════════════════════════════════════════════════

def denoise_spikes(samples, spike_dist=12, back_tol=4, max_pass=5):
    '''掉"跳出去又跳回来"的孤立采样点。

    判据刻意保守 —— **只删真正的尖刺，绝不删正常移动**：
      · 该点到**前后两点**的距离都 > spike_dist；
      · 而它的前后两点彼此距离 ≤ back_tol（说明中间那一下是抖出去的）。
    真机依据：角色定位靠小地图模板匹配，偶发一帧匹配偏掉、下一帧又回来；
    那一帧会在图上留一个远离路线的孤点（route_audit 的"方向冲突"抓不到它）。

    Returns:
        (新列表, 删掉的点数)
    '''
    cur = list(samples)
    removed = 0
    for _ in range(max(0, int(max_pass))):
        if len(cur) < 3:
            break
        keep, hit, i = [], 0, 0
        while i < len(cur):
            if 1 <= i <= len(cur) - 2:
                a, b, c = cur[i - 1], cur[i], cur[i + 1]
                d_ab = abs(a[1] - b[1]) + abs(a[2] - b[2])
                d_bc = abs(b[1] - c[1]) + abs(b[2] - c[2])
                d_ac = abs(a[1] - c[1]) + abs(a[2] - c[2])
                if d_ab > spike_dist and d_bc > spike_dist and d_ac <= back_tol:
                    hit += 1
                    i += 1
                    continue
            keep.append(cur[i])
            i += 1
        cur = keep
        removed += hit
        if hit == 0:
            break
    return cur, removed


# ═══════════════════════════════════════════════════════════════════════════
#  优化 2：自动重分段
# ═══════════════════════════════════════════════════════════════════════════

def find_turn_split(samples, min_leg=15, loop_tol=20, min_pts=3):
    '''一段 A→B→A 的整圈该从哪切开？返回**去程最后一个采样**的下标；不该切返回 None。

    为什么这是本工具最值钱的优化：去程画 right、回程画 left，两者叠在同一张
    route{N}.png 上时，引擎"找最近路线像素"会在同一位置挑到方向相反的指令，
    实测表现是**启动后直奔右边一直走**。正确做法本来是在折返点手工点
    【在这折返，存下这段】—— 而这一步最容易点早/点晚。

    判据（纯用轨迹坐标，不需要看图）：
      · 终点回得到起点附近（曼哈顿 ≤ loop_tol）—— 这才说明是一整圈；
      · 沿**主运动轴**朝去程方向走出去 ≥ min_leg（太短的一小截不算来回）；
      · 切点两侧各自 ≥ min_pts 个采样点。

    ⚠️★ 取极值必须看**主运动轴**，不能看"离起点的曼哈顿距离"。
       真机上回程不会精确踩在去程上（哪怕只差 3px），那个回程起点算出来的
       "离起点距离"会比去程终点**更大** ⇒ 切点会落到回程的第一帧上，
       于是去程那半段里混进一个 left 像素 —— 冲突没消掉，白切。
       沿主运动轴取**首次**到达极值的下标，拿到的才是去程终点。
       （自检 tools/verify_route_rebuild.py 用例【4】就是钉这件事的。）
    '''
    if len(samples) < max(min_pts * 2, 6):
        return None
    x0, y0 = samples[0][1], samples[0][2]
    xn, yn = samples[-1][1], samples[-1][2]
    if abs(xn - x0) + abs(yn - y0) > loop_tol:
        return None

    xs = [s[1] for s in samples]
    ys = [s[2] for s in samples]
    if (max(xs) - min(xs)) >= (max(ys) - min(ys)):
        vals = xs
    else:
        vals = ys
    fwd = 1 if (vals[1] - vals[0]) >= 0 else -1     # 去程朝哪边走

    best_i, best_v = 0, vals[0]
    for i, v in enumerate(vals):
        if (fwd > 0 and v > best_v) or (fwd < 0 and v < best_v):
            best_i, best_v = i, v                    # 严格大于 ⇒ 取**首次**到达极值
    if abs(best_v - vals[0]) < min_leg:
        return None
    if best_i < min_pts - 1 or (len(samples) - 1 - best_i) < min_pts - 1:
        return None
    return best_i


def longest_draw(samples):
    '''单笔最长跨度（px）。跨度大 = 中间丢过帧，那一段是直线糊过去的。'''
    return max((abs(samples[i][1] - samples[i - 1][1])
                + abs(samples[i][2] - samples[i - 1][2])
                for i in range(1, len(samples))), default=0)


# ═══════════════════════════════════════════════════════════════════════════
#  轨迹 → 段
# ═══════════════════════════════════════════════════════════════════════════

def collect_segments(records):
    '''把轨迹帧按 `seg` 字段切段 → {段号: [(t, x, y, action, is_blob), ...]}。

    只收**真正落过笔**的帧（`on=1` 且 `a != ""`）：录制器只在
    `is_enable and _armed and action != ""` 时才调 paint_step。
    多收一帧就会多画一笔，就还原不出原图了。
    '''
    segs = {}
    for r in records:
        if r.get("k") != "f":
            continue
        if not int(r.get("on", 0)):
            continue
        a = r.get("a") or ""
        if a == "":
            continue
        segs.setdefault(int(r.get("seg", 0)), []).append(
            (float(r.get("t", 0.0)), int(r.get("x", 0)), int(r.get("y", 0)),
             a, int(r.get("b", 0))))
    return segs


def waypoint_events(records):
    return [r for r in records if r.get("k") == "e" and r.get("ev") == "waypoint"]


def goal_positions(records):
    '''每个源段末的 goal 位置 → {段号: (x, y)}。

    为什么不能用"该段最后一个采样点"当 goal：用户按下 F3 那一帧**可能没有按键**
    （action == ""），而那种帧录制器**不落笔、也就不进轨迹**。于是最后采样点会
    停在几帧之前，goal 就压偏了。
    ⇒ 用事件里记下的坐标 —— 这正是"把操作也记下来"的用处：事件带的是
      `save_route` 当时真实的 `loc_player_global`，一点不差。
    '''
    out = {}
    for r in records:
        if r.get("k") != "e":
            continue
        if r.get("ev") not in ("save", "waypoint"):
            continue
        try:
            out[int(r.get("seg", 0))] = (int(r["x"]), int(r["y"]))
        except Exception:                                    # noqa: BLE001
            continue
    return out


def audit_image(painter, img_bgr, codes_main):
    '''按**引擎同款判据**体检一张（内存里的）路线图。'''
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    conflicts, n_left, n_right = _count_direction_conflicts(rgb, codes_main, radius=8)
    return {"conflicts": conflicts, "left": n_left, "right": n_right}


def build_cfg(codes_main, codes_ud, blob_cooldown):
    '''拿当前 config 当底，但**色码表与冷却用轨迹里记的当时值**。

    为什么不用现在的 config：用户完全可能在录完之后改了配置（换跳跃键、
    调色码）。拿新配置去重放旧轨迹，画出来的东西既不是原样、也不是他现在想要的。
    '''
    cfg_path = active_config_path()
    cfg = load_yaml(cfg_path or os.path.join("config", "config_default.yaml"))
    cfg.setdefault("route", {})
    if codes_main:
        cfg["route"]["color_code"] = {f"{r},{g},{b}": v for (r, g, b), v in codes_main.items()}
    if codes_ud:
        cfg["route"]["color_code_up_down"] = {f"{r},{g},{b}": v
                                              for (r, g, b), v in codes_ud.items()}
    cfg.setdefault("route_recoder", {})
    if blob_cooldown is not None:
        cfg["route_recoder"]["blob_cooldown"] = blob_cooldown
    cfg["route_recoder"].setdefault("blob_cooldown", 0.5)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════════════

def plan_segments(segs, do_denoise, spike_dist, do_split, min_leg, loop_tol):
    '''逐段：去噪 →（可选）重分段。

    Returns:
        (out_segments, log_lines, total_removed)
        out_segments: [(源段号, 采样, split_info)]，split_info=None 表示这一段没被切；
                      被切时是 {"piece": 0|1}（0 = 去程，1 = 回程）
    '''
    out, lines, total_removed = [], [], 0
    for s in sorted(segs):
        samples = segs[s]
        if not samples:
            continue
        before = len(samples)
        removed = 0
        if do_denoise:
            samples, removed = denoise_spikes(samples, spike_dist=spike_dist)
            total_removed += removed
        note = ""
        split_at = (find_turn_split(samples, min_leg=min_leg, loop_tol=loop_tol)
                    if do_split else None)
        if split_at is not None:
            # ⚠️★ 切点那一帧**只归去程**，回程从它的**下一帧**开始。
            #   为什么不能两边共用：折返点那一帧带的是**去程**的动作（right），
            #   而 paint_step 对每段的**第一帧**会画一个"零长度线段"——
            #   在 numpy/OpenCV 里那等于**画了一个像素**。让回程从它开头，
            #   回程图上就会多出一个 right 像素，紧挨着 left 像素 ⇒ 还是方向冲突，
            #   等于白切。（自检用例【4】实测抓到过。）
            #   代价：两段首尾差几个像素（回程起点是真实回程轨迹点），
            #   引擎按"最近路线像素"接引（search_range=10）足够接上。
            pieces = [(samples[:split_at + 1], {"piece": 0}),
                      (samples[split_at + 1:], {"piece": 1})]
            note = (f" → **自动切成 2 段**（折返点在第 {split_at} 帧，"
                    f"坐标 {samples[split_at][1]},{samples[split_at][2]}，"
                    f"离起点 {abs(samples[split_at][1] - samples[0][1]) + abs(samples[split_at][2] - samples[0][2])}px）")
        else:
            pieces = [(samples, None)]
        lines.append(f"  段{s + 1}: {before} 帧"
                     + (f" → 去噪删掉 {removed} 个孤立点" if removed else "")
                     + note)
        for smp, info in pieces:
            if smp:
                out.append((s, smp, info))
    return out, lines, total_removed


def run(map_name, report_only=False, do_split=True, do_denoise=True, spike_dist=12,
        min_leg=15, loop_tol=20, out_dir=None, prune=False):
    map_dir = resource_path(os.path.join("minimaps", map_name))
    tpath = os.path.join(map_dir, TRACE_NAME)
    if not os.path.exists(tpath):
        print(f"[错误] 这张图没有录制轨迹：{tpath}")
        print("       轨迹从 2026-10-12 的录制器才开始写。**旧录像没有轨迹**，")
        print("       要用新版本重新 F4 录一次才会有（录完轨迹会自己出现在该目录）。")
        return 2

    header, records = load_last_session(tpath)
    if not records:
        print(f"[错误] 轨迹文件里没有可用记录：{tpath}")
        return 2

    codes_main = codes_from_json(header.get("codes"))
    codes_ud = codes_from_json(header.get("codes_ud"))
    if not codes_main:
        print("[错误] 轨迹头里没有色码表 —— 文件可能被手工改过。")
        return 2
    cfg = build_cfg(codes_main, codes_ud, header.get("blob_cooldown"))

    segs = collect_segments(records)
    if not segs:
        print("[错误] 轨迹里没有落笔帧 —— 那次录制可能一笔都没画。")
        return 2
    wps = waypoint_events(records)
    goals = goal_positions(records)

    print("=" * 74)
    print(f"路线离线重生成 · {map_name}")
    print("=" * 74)
    print(f"  轨迹文件   : {tpath}")
    print(f"  会话时刻   : {header.get('t')}")
    print(f"  录到的段数 : {len(segs)}（段号 {sorted(segs)}）")
    print(f"  折返点事件 : {len(wps)} 个")
    print(f"  本次开关   : 重分段={'开' if do_split else '关'}  "
          f"去噪={'开' if do_denoise else '关'}")
    print()

    # ── 1) 载入原图并剥出底图（每段各自一份）────────────────────────────
    origins, bases, painter = {}, {}, None
    for s in sorted(segs):
        p = resource_path(f"minimaps/{map_name}/route{s + 1}.png")
        if not os.path.exists(p):
            print(f"  [警告] 段{s + 1} 找不到原图 {p} —— 跳过这一段")
            continue
        img = load_image(p)
        if painter is None:
            painter = make_painter(cfg, codes_main, codes_ud, img.shape)
        origins[s] = img
        bases[s] = strip_route_pixels(painter, img)
    if painter is None:
        print("[错误] 一张可用的原图都没有，无法重生成。")
        return 2

    # ── 2) 去噪 + 重分段 ──────────────────────────────────────────────
    out_segments, lines, total_removed = plan_segments(
        {s: segs[s] for s in bases}, do_denoise, spike_dist, do_split, min_leg, loop_tol)
    if not out_segments:
        print("[错误] 重算后一段都不剩。")
        return 2

    # ── 3) 重放落笔 ──────────────────────────────────────────────────
    rendered = []
    with _FrozenTime(_rr):
        for (bg, smp, info) in out_segments:
            img = paint_segment(painter, bases[bg], smp)
            last_pt = (smp[-1][1], smp[-1][2])
            if info is not None:
                # 被切开的段：旧的折返点白点位置已经不准，抹掉重画
                strip_waypoint_dots(img, wps)
                if info["piece"] == 0:
                    draw_waypoint_dot(img, last_pt)
                # 去程那半段的终点 = **新的**折返点；回程那半段仍用原来记下的 goal
                goal_pt = last_pt if info["piece"] == 0 else goals.get(bg, last_pt)
            else:
                goal_pt = goals.get(bg, last_pt)
            # goal 必须最后画（要压在所有移动线之上）
            stamp_goal(painter, img, goal_pt)
            rendered.append((bg, smp, img))

    # ── 4) 报告 ─────────────────────────────────────────────────────
    print("── 处理过程 " + "─" * 62)
    for ln in lines:
        print(ln)
    if total_removed:
        print(f"  去噪合计删掉 {total_removed} 个孤立采样点")
    print()

    print("── 改前对照（原 route{N}.png，引擎口径）" + "─" * 34)
    for s in sorted(bases):
        st = audit_image(painter, origins[s], codes_main)
        print(f"  原 route{s + 1}.png : left={st['left']:5d} right={st['right']:5d} "
              f"左右冲突位置={st['conflicts']}")
    print()

    print("── 重生成结果（引擎口径）" + "─" * 48)
    for i, (bg, smp, img) in enumerate(rendered):
        st = audit_image(painter, img, codes_main)
        mx = longest_draw(smp)
        warn = ""
        if st["conflicts"]:
            warn += "  ⚠️ 仍有左右冲突"
        if mx > LONG_DRAW_WARN:
            warn += f"  ⚠️ 单笔 {mx}px，中间可能丢过帧"
        print(f"  route{i + 1}.png（来自段{bg + 1}）: left={st['left']:5d} "
              f"right={st['right']:5d} 左右冲突位置={st['conflicts']}，单笔最长={mx}px{warn}")
    print()

    first_pt = (rendered[0][1][0][1], rendered[0][1][0][2])
    last_pt = (rendered[-1][1][-1][1], rendered[-1][1][-1][2])
    loop_d = abs(last_pt[0] - first_pt[0]) + abs(last_pt[1] - first_pt[1])
    verdict = ("接得上 ✓" if loop_d <= LOOP_OK else
               "靠失联救援勉强接得上" if loop_d <= LOOP_RESCUE else
               "**接不上 —— 角色会卡在终点** ✗")
    print("── 闭环检查 " + "─" * 62)
    print(f"  最后一段终点 {last_pt} 到第一段起点 {first_pt} 的距离 = {loop_d}px → {verdict}")
    print("  （≤10px 接得上；≤40px 靠救援；>40px 会卡住）")
    print()

    if report_only:
        print("[report-only] 没有写任何文件。")
        return 0

    # ── 5) 写盘（先备份原图）────────────────────────────────────────────
    if out_dir is None:
        bak = os.path.join(map_dir, BACKUP_DIRNAME)
        os.makedirs(bak, exist_ok=True)
        for s in sorted(origins):
            src = resource_path(f"minimaps/{map_name}/route{s + 1}.png")
            dst = os.path.join(bak, f"route{s + 1}.png")
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
        print(f"  原图已备份：{bak}")

    target_dir = out_dir or map_dir
    os.makedirs(target_dir, exist_ok=True)
    for i, (bg, smp, img) in enumerate(rendered):
        out = os.path.join(target_dir, f"route{i + 1}.png")
        if not imwrite_unicode(out, img):
            print(f"[错误] 写出失败：{out}")
            return 1
        print(f"  已写 {out}")

    # 段数变少时，多出来的旧 route{N}.png **必须**清掉：
    # 引擎是按 route1、route2…顺序加载直到文件不存在的，留着旧的会把新路线接在后面走。
    stale = list(range(len(rendered) + 1, len(bases) + 1))
    if stale:
        print()
        print(f"  ⚠️ 段数从 {len(bases)} 变成 {len(rendered)}，"
              f"以下旧图已多余，**不删掉的话引擎仍会加载它们**：")
        for e in stale:
            print(f"        route{e}.png")
        if prune:
            for e in stale:
                p = os.path.join(target_dir, f"route{e}.png")
                if os.path.exists(p):
                    os.remove(p)
                    print(f"        已删 route{e}.png")
        else:
            print("      （加 --prune 可自动删除）")
    elif len(rendered) != len(bases):
        print(f"  段数从 {len(bases)} 变成 {len(rendered)}。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="读录制轨迹，离线重生成 route{N}.png",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", required=True, help="地图目录名（minimaps/<名字>/）")
    ap.add_argument("--report-only", action="store_true", help="只打印报告，不写任何文件")
    ap.add_argument("--no-split", action="store_true", help="关掉自动重分段")
    ap.add_argument("--no-denoise", action="store_true", help="关掉去噪")
    ap.add_argument("--spike-dist", type=int, default=12,
                    help="去噪阈值：离前后两点都超过这么多像素才算尖刺（默认 12）")
    ap.add_argument("--min-leg", type=int, default=15,
                    help="自动重分段：去程至少要走这么远才算一个来回（默认 15）")
    ap.add_argument("--loop-tol", type=int, default=20,
                    help="自动重分段：终点回到起点这么多像素内才算一整圈（默认 20）")
    ap.add_argument("--prune", action="store_true",
                    help="段数变少时，删掉多余的旧 route{N}.png")
    ap.add_argument("--out-dir", default=None,
                    help="写到别的目录（自检用；给定时不备份、不碰原图）")
    a = ap.parse_args(argv)
    return run(a.map, report_only=a.report_only, do_split=not a.no_split,
               do_denoise=not a.no_denoise, spike_dist=a.spike_dist,
               min_leg=a.min_leg, loop_tol=a.loop_tol, out_dir=a.out_dir,
               prune=a.prune)


if __name__ == "__main__":
    sys.exit(main())
