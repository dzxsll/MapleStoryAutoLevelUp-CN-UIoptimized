# -*- coding: utf-8 -*-
'''「goal 像素不再吃掉整帧」自检（2026-10-14 加，用户 m08867 拍板 E1）。

背景（用户 m08737「读取日志，查看导航问题」→ 我读 `log/MSBot_2026-10-14_15-47-36.log`）：
    15:59:34 → 16:05:07（**5.5 分钟**）角色站在本段终点**正下方 8px**
    （全局 (48,86)、段3/4），每帧指令都是 `(none none goal)`：
      · 它离本段 goal 圆点（`route4.png` 的 (56,78)/(57,79)/(58,80)）最近
        —— 注意 `route.search_range`(10) 是**方框半边长**、代码里没有
        `dist <= radius` 那一层，所以曼哈顿 16px 的 goal 像素照样当选；
      · 而 goal 色码的命令是 `none none goal` = **什么都不做**；
      · 同时 `check_reach_goal()` 因为 `|86-78| = 8 > goal_reach_y_tol(3)`
        拒绝切段 ⇒ 「命令不让动 + 段不让切」= 冻死。
      · 更糟的是**回正线进不来**：回正的门槛是"方圆内一个路线像素都找不到"，
        而这里"找到了"（goal 像素）⇒ 用户 16:05 手绘的回正线最该用时用不上。
      · 最后是**用户手动停机**结束的；停机后他手绘了一条 (57,89)→(67,78) 的回正线，
        16:21:06 `[回正·小结] 成功回主线 | 1.9s/56帧 | (76,86)→(68,78)`
        ⇒ **只要回正进得去，1.9 秒就能从 y=86 回到 y=78 的主线**。
    同类僵持（同一条日志里 `指令(none none goal)` 的连续段）：15:59–16:05 共 **25 段**、
    最长 5.5 分钟；上一轮 `..._15-07-02.log` 9 段、最长 76 秒 ⇒ 老问题，但这轮长得多。
    另有第二类症状（16:18:43–16:19:19，40 秒）：角色在另一层，最近的却是"另一层的纯横向
    像素"，指令在 `right none none` ↔ `left none none` 之间翻，人却不挪窝。

做法（本轮改动，全在 `src/engine/MapleStoryAutoLevelUp.py`）：
    `_goal_pixel_skip_params()`（唯一读 `route.goal_pixel_skip` + 取本段终点 y）
    + `_route_hit_unusable()`（唯一判"本帧这个像素能不能用"的地方）
    + `get_nearest_color_code()` 里每次扫描之后过一遍 `_filter_hit()`：
      遇到不可用的 goal 像素 ⇒ 在**同一个搜索框**里排除 goal 像素重扫一次换像素；
      换不出来 ⇒ 按"找不到路线像素"处理 ⇒ 交给**回正线 / 失联救援**。

本文件钉六件事：
  【1】`_goal_pixel_skip_params()` 真值表（关掉 / 畸形 / `goal_reach_y_tol=0` /
        没有本段终点 y ⇒ 一律**不启用**，即退回老行为）
  【2】`_route_hit_unusable()` 真值表（goal 高度差闭区间 / 另一层的纯横向像素 /
        up-down、jump、teleport 一律放行 / 判据不可用时放行）
  【3】接线白盒（三处扫描都过滤、唯一读点、日志留痕、老行为仍在）
  【4】配置 / 界面口径（出厂开、中文名、ADV_FIELD_HIDE 收着）
  【5】端到端（真的 `get_nearest_color_code()` + 假路线图）：用户现场会被判成
        "没有可用像素"（⇒ 回正接管）、而**关掉本层**就退回"收到 goal 指令"的老行为
  【6】真机日志回放（用真 `route*.png`）：站在终点正下方的那 25 段僵持**不再**出现
        `goal` 指令；而真的到位（|Δy| ≤ 容差）时**照旧**拿到 goal 像素去切段
'''
import logging
import os
import re
import sys

import numpy as np
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402

PASS = 0
FAIL = 0

# `[运行状态] 帧N 小地图(x, y) 全局(x, y) 段i/n 指令(mx my act) 怪N 名字命中…`
_RE_ST = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*?"
    r"全局\((\d+),\s*(\d+)\).*?段(\d+)/(\d+).*?指令\((\w+) (\w+) (\w+)\)")

_DEFAULT_CC = {
    (255, 0, 0): "left none none",
    (0, 0, 255): "right none none",
    (0, 255, 255): "right none jump",
    (255, 0, 0): "left none none",
    (255, 127, 0): "left none jump",
    (127, 255, 0): "none down jump",
    (255, 0, 255): "none none jump",
    (0, 255, 127): "stop stop stop",
    (255, 255, 0): "none none goal",
    (255, 0, 127): "none up teleport",
    (127, 0, 255): "none down teleport",
    (0, 127, 0): "left none teleport",
    (139, 69, 19): "right none teleport",
}
_DEFAULT_UD = {
    (127, 127, 127): "none up none",
    (255, 255, 127): "none down none",
}


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望={want!r} 实际={got!r}")


def check_true(name, cond, detail=""):
    check(name + (f"  ({detail})" if detail else ""), bool(cond), True)


def stub(cfg=None, **kw):
    '''只绑本次两个新方法的假引擎（不跑 __init__）。'''
    class S:
        pass

    s = S()
    for _n in ("_goal_pixel_skip_params", "_route_hit_unusable"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg if cfg is not None else {}
    s.loc_player_global = (48, 86)
    s._seg_goals = [(56, 78)]
    s.idx_routes = 0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _cfg(**kw):
    return {"route": dict(kw)}


def engine(cfg=None, route_px=None, img_route=None, player=(48, 86),
           seg_goals=None, idx=0, w=140, h=140, **kw):
    '''能跑真 `get_nearest_color_code()` 的假引擎（不跑 __init__）。'''
    class S:
        pass

    s = S()
    for _n in ("get_nearest_color_code", "_goal_pixel_skip_params",
               "_route_hit_unusable"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg if cfg is not None else _cfg(
        search_range=10, rescue_range=40, goal_pixel_skip=1, goal_reach_y_tol=3)
    s.color_code = dict(_DEFAULT_CC)
    s.color_code_up_down = dict(_DEFAULT_UD)
    if img_route is None:
        img_route = np.zeros((h, w, 3), dtype=np.uint8)
        for (_x, _y), _col in (route_px or {}).items():
            img_route[_y, _x] = _col
    s.img_route = img_route
    s.loc_player_global = player
    s._seg_goals = [(56, 78)] if seg_goals is None else seg_goals
    s.idx_routes = idx
    s.is_show_debug_window = False
    s.img_route_debug = None
    s.loc_last_route_pixel = None
    s.t_route_rescue_log = 0.0
    s.t_goal_skip_log = 0.0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _cmd_of(hit):
    return "" if hit is None else str(hit.get("command", ""))


def main():
    global PASS, FAIL

    print("【1】_goal_pixel_skip_params() 真值表（判不了 ⇒ 不启用 = 退回老行为）")
    check("缺键 ⇒ (启用, 容差 3, 终点 y=78)",
          stub({})._goal_pixel_skip_params(), (True, 3, 78))
    check("goal_pixel_skip=0 ⇒ 关掉本层",
          stub(_cfg(goal_pixel_skip=0))._goal_pixel_skip_params()[0], False)
    check("goal_pixel_skip='0' ⇒ 关掉（字符串也算）",
          stub(_cfg(goal_pixel_skip="0"))._goal_pixel_skip_params()[0], False)
    check("goal_pixel_skip='1' ⇒ 启用",
          stub(_cfg(goal_pixel_skip="1"))._goal_pixel_skip_params()[0], True)
    check("goal_pixel_skip=None ⇒ 按出厂 1（启用）",
          stub(_cfg(goal_pixel_skip=None))._goal_pixel_skip_params()[0], True)
    check("goal_pixel_skip='abc' ⇒ 按出厂 1（畸形不猜成「关」）",
          stub(_cfg(goal_pixel_skip="abc"))._goal_pixel_skip_params()[0], True)
    check("goal_reach_y_tol=0 ⇒ 不启用（高度闸关了，goal 本来就该切段）",
          stub(_cfg(goal_reach_y_tol=0))._goal_pixel_skip_params()[0], False)
    check("goal_reach_y_tol='abc' ⇒ 退回 3 且启用",
          stub(_cfg(goal_reach_y_tol="abc"))._goal_pixel_skip_params()[:2], (True, 3))
    check("goal_reach_y_tol=5 ⇒ 用 5（与 check_reach_goal 同一个键）",
          stub(_cfg(goal_reach_y_tol=5))._goal_pixel_skip_params()[1], 5)
    check("拿不到本段终点 y（_seg_goals=None）⇒ 不启用",
          stub(_cfg(), _seg_goals=None)._goal_pixel_skip_params()[0], False)
    check("_seg_goals 为空 ⇒ 不启用",
          stub(_cfg(), _seg_goals=[])._goal_pixel_skip_params()[0], False)
    check("idx_routes 越界 ⇒ 不启用",
          stub(_cfg(), _seg_goals=[(56, 78)], idx_routes=9)
          ._goal_pixel_skip_params()[0], False)
    check("终点 y 不是数字 ⇒ 不启用",
          stub(_cfg(), _seg_goals=[(56, "x")])._goal_pixel_skip_params()[0], False)

    print("\n【2】_route_hit_unusable() 真值表（只判两类，其余一律放行）")
    _s = stub(_cfg())
    _hit_goal = {"pixel": (56, 78), "color": (255, 255, 0),
                 "command": "none none goal", "distance": 16}
    _u, _why = _s._route_hit_unusable(_hit_goal, 78, 3)
    check_true("goal 像素 + 高度差 8 > 3 ⇒ 不可用", _u)
    check_true("理由说清了是 goal 像素与高度差", "goal 像素" in _why and "8px" in _why, _why)
    check("goal 像素 + 高度差 3（= 容差，闭区间）⇒ 可用",
          _s._route_hit_unusable(_hit_goal, 83, 3)[0], False)
    check("goal 像素 + 高度差 2 ⇒ 可用（真到位了，交给 check_reach_goal 切段）",
          _s._route_hit_unusable(_hit_goal, 84, 3)[0], False)
    check("goal 像素但 goal_y 未知 ⇒ 放行（判不了就别乱丢）",
          _s._route_hit_unusable(_hit_goal, None, 3)[0], False)
    _hit_left = {"pixel": (61, 78), "color": (255, 0, 0),
                 "command": "left none none", "distance": 21}
    _u2, _why2 = _s._route_hit_unusable(_hit_left, 78, 3)
    check_true("纯横向像素在另一层（Δy=8）⇒ 不可用", _u2)
    check_true("理由点明「另一层」", "另一层" in _why2, _why2)
    check("纯横向像素同层（Δy=2）⇒ 可用",
          _s._route_hit_unusable({"pixel": (61, 88), "command": "left none none"},
                                 78, 3)[0], False)
    check("纯横向像素 Δy=3（= 容差）⇒ 可用",
          _s._route_hit_unusable({"pixel": (61, 83), "command": "left none none"},
                                 78, 3)[0], False)
    for _c in ("none up none", "none down none", "right none jump",
               "none up teleport", "stop stop stop"):
        check(f"{_c} 不管高度差多大都放行（不是纯横向）",
              _s._route_hit_unusable({"pixel": (61, 78), "command": _c}, 78, 3)[0],
              False)
    check("hit=None ⇒ 放行（没有像素谈不上丢）",
          _s._route_hit_unusable(None, 78, 3)[0], False)
    check("loc_player_global 为空 ⇒ 放行",
          stub(_cfg(), loc_player_global=None)
          ._route_hit_unusable(_hit_goal, 78, 3)[0], False)
    check("命令拆不出 3 段 ⇒ 放行（不认识的格式不动它）",
          _s._route_hit_unusable({"pixel": (61, 78), "command": "left none"}, 78, 3)[0],
          False)

    print("\n【3】接线白盒（源码）")
    with open(os.path.join(_ROOT, "src", "engine", "MapleStoryAutoLevelUp.py"),
              encoding="utf-8") as _fh:
        _src = _fh.read()
    check("三处扫描都走过滤版本（主扫 + 两级救援）",
          _src.count("_scan_filtered("), 4)   # 1 处定义 + 3 处调用
    check("过滤里用「排除 goal 像素」重扫一次",
          _src.count("_scan(radius, center, skip_goal=True)"), 1)
    check("_scan 里真的按 skip_goal 跳过 goal 色码",
          _src.count('and "goal" in str(self.color_code[pixel])'), 1)
    check("route.goal_pixel_skip 全文件只有一个读点",
          _src.count('"goal_pixel_skip", 1'), 1)
    check("_goal_pixel_skip_params 只定义一次",
          _src.count("def _goal_pixel_skip_params(self):"), 1)
    check("_route_hit_unusable 只定义一次",
          _src.count("def _route_hit_unusable(self, hit, goal_y, y_tol):"), 1)
    check_true("过滤时留痕 [路线·goal过滤]",
               "[路线·goal过滤]" in _src)
    check_true("留痕里点名怎么关掉（route.goal_pixel_skip 填 0）",
               "`route.goal_pixel_skip` 填 0" in _src)
    check_true("留痕里说清换了谁 / 交给回正线",
               "交给回正线 / 救援接管" in _src)
    check_true("过滤只发生在 get_nearest_color_code 内（_scan 是它的内层函数）",
               _src.index("def get_nearest_color_code(self):")
               < _src.index("def _filter_hit(n, radius, center):"))
    check_true("goal_reach_y_tol 没被改动（还是与 check_reach_goal 同一个键）",
               '_goal_y_tol = int(self.cfg.get("route", {}).get("goal_reach_y_tol", 3) or 0)'
               in _src)
    # ── 2026-10-14 加：侧向分支修复（②）+ 回正入口可达性闸（③）──────────────
    # 用户 m09666 拍板；取证见 `_filter_hit` 里那段注释 + 引擎入口块那段。
    check("侧向换像素只走 `usable_only=True` 这一处",
          _src.count("_scan(radius, center, usable_only=True)"), 1)
    check("usable_only 复用 _route_hit_unusable（不另写一套判据）",
          _src.count("_ok_cc = not self._route_hit_unusable("), 1)
    check_true("侧向换不到 ⇒ 退回老行为（不再交给回正 / 救援）",
               "本帧仍按它走" in _src and "退回老行为" in _src)
    check_true("③ 回正入口要「最近像素够得着」才准进",
               "int(_d_far) <= _reach" in _src)
    check_true("③ 半径口径与救援半径同源（rescue_range / search_range 取大者）",
               '_reach = self.cfg["route"].get("rescue_range", 40)' in _src
               and 'if _reach <= int(self.cfg["route"]["search_range"]):' in _src)
    check_true("③ 拒绝进入时留痕（够不着 ⇒ 不进去）",
               "够不着）⇒ **不进去**" in _src)

    print("\n【4】配置 / 界面口径")
    with open(os.path.join(_ROOT, "config", "config_default.yaml"),
              encoding="utf-8") as _fh:
        _cfg_all = yaml.safe_load(_fh)
    _r = _cfg_all["route"]
    check("出厂 route.goal_pixel_skip = 1（开）", _r.get("goal_pixel_skip"), 1)
    check("出厂 route.goal_reach_y_tol 仍是 3（本层不动它）",
          _r.get("goal_reach_y_tol"), 3)
    from src.utils.cfg_names import cn_key                        # noqa: E402
    _cn = cn_key("route", "goal_pixel_skip")
    check_true("goal_pixel_skip 有中文名（日志点名它，用户去 yaml 能对上）",
               _cn != "goal_pixel_skip" and "goal" in _cn, _cn)
    import src.ui.ui as _ui                                       # noqa: E402
    check_true("goal_pixel_skip 在 ADV_FIELD_HIDE['route'] 里收着（算法判据）",
               "goal_pixel_skip" in _ui.ADV_FIELD_HIDE.get("route", []))

    print("\n【5】端到端：真的 get_nearest_color_code() + 假路线图")
    _logh = _Collect()
    logging.getLogger("MSBot").addHandler(_logh)
    try:
        # A：用户现场 —— 最近的像素是本段 goal，角色在正下方 8px
        _px = {(56, 78): (255, 255, 0),      # goal
               (61, 78): (255, 0, 0)}        # 另一层的 left none none
        _e = engine(route_px=_px, player=(48, 86), seg_goals=[(56, 78)])
        _n, _ud = _e.get_nearest_color_code()
        check("A 站在本段终点正下方 ⇒ 本帧没有可用像素（交给回正线/救援）",
              (_n, _ud), (None, None))
        check_true("A 有 [路线·goal过滤] 留痕",
                   any("[路线·goal过滤]" in m for m in _logh.msgs),
                   str(len(_logh.msgs)))
        _e2 = engine(cfg=_cfg(search_range=10, rescue_range=40,
                              goal_pixel_skip=0, goal_reach_y_tol=3),
                     route_px=_px, player=(48, 86), seg_goals=[(56, 78)])
        _n2, _ud2 = _e2.get_nearest_color_code()
        check("A' 关掉本层（goal_pixel_skip=0）⇒ 退回老行为：收到 goal 指令",
              _cmd_of(_n2), "none none goal")

        # B：真的到位（Δy = 0）⇒ goal 像素照旧可用（否则会把切段搞坏）
        _e3 = engine(route_px=_px, player=(56, 78), seg_goals=[(56, 78)])
        check("B 真站在终点上 ⇒ 照旧拿到 goal 像素（切段不受影响）",
              _cmd_of(_e3.get_nearest_color_code()[0]), "none none goal")

        # C：排除 goal 后能换到"同层、真能走"的像素
        _px2 = {(50, 86): (255, 255, 0),     # goal 色码，但它在角色脚下
                (52, 86): (0, 0, 255)}       # 同层的 right none none
        _e4 = engine(route_px=_px2, player=(48, 86), seg_goals=[(40, 78)])
        _n4, _ = _e4.get_nearest_color_code()
        check("C 排除 goal 后换到同层可动像素", _cmd_of(_n4), "right none none")

        # D：附近只有"另一层的纯横向像素" ⇒ **2026-10-14 改**：退回用它（老行为）。
        #    改前这里是"整帧交给回正线 / 救援"，真机代价极大（46/46 次都走那条路，
        #    见引擎 `_filter_hit` 里的取证注释）；治 16:18 那 40 秒左右抖的初衷，
        #    由 D2（换到同层可用像素）与"退回老行为"共同保证。
        _px3 = {(61, 78): (255, 0, 0)}
        _logh.msgs.clear()
        _e5 = engine(route_px=_px3, player=(48, 86), seg_goals=[(56, 78)])
        _n5, _ud5 = _e5.get_nearest_color_code()
        check("D 只有另一层的横向像素 ⇒ 仍按它走（退回老行为）",
              _cmd_of(_n5), "left none none")
        check_true("D 留痕说清「换不到 ⇒ 本帧仍按它走」，而不再说「交给回正/救援」",
                   any("[路线·goal过滤]" in m and "仍按它走" in m
                       for m in _logh.msgs)
                   and not any("交给回正线" in m for m in _logh.msgs),
                   str(_logh.msgs))
        # D2：另一层横向像素最近、但框里另有一个"真能用"的像素 ⇒ 换用它
        #     ⚠️ 换像素只在 `color_code` 里找（上下码不在 color_code 里，换不到它们），
        #        所以这里用"同层横向"（蓝 `right none none`，Δy=3 正好在容差内）
        _px3b = {(48, 90): (255, 0, 0),      # d=4，Δy=4 ⇒ 不可用（另一层）
                 (44, 83): (0, 0, 255)}      # d=7 > 4，同层 ⇒ 可用
        _logh.msgs.clear()
        _e5b = engine(route_px=_px3b, player=(48, 86), seg_goals=[(56, 78)])
        _n5b, _ = _e5b.get_nearest_color_code()
        check("D2 框里有可用像素 ⇒ 换用它（不再朝另一层走）",
              _cmd_of(_n5b), "right none none")
        check_true("D2 留痕写明「改用」了哪个像素",
                   any("改用" in m and "[路线·goal过滤]" in m for m in _logh.msgs),
                   str(_logh.msgs))
        _e6 = engine(cfg=_cfg(search_range=10, rescue_range=40,
                              goal_pixel_skip=0, goal_reach_y_tol=3),
                     route_px=_px3, player=(48, 86), seg_goals=[(56, 78)])
        check("D' 关掉本层 ⇒ 老行为会朝那一层走",
              _cmd_of(_e6.get_nearest_color_code()[0]), "left none none")

        # E：同层横向像素照旧放行（别误伤正常跟随）
        _px4 = {(52, 88): (0, 0, 255)}
        _e7 = engine(route_px=_px4, player=(48, 86), seg_goals=[(48, 78)])
        check("E 同层横向像素照旧放行（正常跟随不受影响）",
              _cmd_of(_e7.get_nearest_color_code()[0]), "right none none")
    finally:
        logging.getLogger("MSBot").removeHandler(_logh)

    print("\n【6】真机日志回放：真 route*.png + 新/旧两套判据对质")
    _logs = [p for p in sorted(__import__("glob").glob(
                 os.path.join(_ROOT, "log", "MSBot_*.log")))
             if "运行状态" in open(p, encoding="utf-8", errors="ignore").read(
                 max(1, os.path.getsize(p)))]
    if not _logs:
        print("  [SKIP] log/ 下没有带 [运行状态] 的日志 —— 本段只在有日志时才算证据")
    else:
        _path = max(_logs, key=lambda p: os.path.getsize(p))
        _map_dir = None
        _samples = []
        for _ln in open(_path, encoding="utf-8", errors="ignore"):
            if _map_dir is None:
                _m0 = re.search(r"minimaps[\\/]([^\\/]+)[\\/]route1\.png", _ln)
                if _m0:
                    _map_dir = _m0.group(1)
            _m = _RE_ST.match(_ln)
            if _m:
                _samples.append((int(_m.group(2)), int(_m.group(3)),
                                 int(_m.group(4)), _m.group(6), _m.group(7),
                                 _m.group(8)))
        if _map_dir is None or len(_samples) < 10:
            print(f"  [SKIP] 地图目录={_map_dir!r} / 可解析状态行={len(_samples)}"
                  f"（{os.path.basename(_path)}）")
        else:
            import cv2                                            # noqa: E402
            with open(os.path.join(_ROOT, "config", "config_default.yaml"),
                      encoding="utf-8") as _fh:
                _cc_cfg = yaml.safe_load(_fh)["route"]
            _cc = {tuple(map(int, k.split(','))): v
                   for k, v in _cc_cfg["color_code"].items()}
            _ud = {tuple(map(int, k.split(','))): v
                   for k, v in _cc_cfg["color_code_up_down"].items()}
            _goal_cols = [c for c, v in _cc.items() if "goal" in str(v)]
            _segs = {}
            _imgs = {}
            for _p in sorted(__import__("glob").glob(
                    os.path.join(_ROOT, "minimaps", _map_dir, "route*.png"))):
                _b = os.path.basename(_p)
                if _b in ("route_home.png", "route_rest.png"):
                    continue
                _mi = re.match(r"route(\d+)\.png$", _b)
                if not _mi:
                    continue
                _arr = cv2.imdecode(np.fromfile(_p, dtype=np.uint8),
                                    cv2.IMREAD_COLOR)
                if _arr is None:
                    continue
                _imgs[int(_mi.group(1))] = _arr
                _pts = []
                for _gc in _goal_cols:
                    _ys, _xs = np.where(np.all(_arr == np.array(_gc, dtype=np.uint8),
                                               axis=2))
                    _pts.extend(zip(_xs.tolist(), _ys.tolist()))
                _segs[int(_mi.group(1))] = _pts
            _n_log_goal = 0        # 日志里当时真的收到了 `none none goal`
            _n_old_goal = 0        # 老判据下拿到 goal 像素
            _n_new_goal = 0        # 新判据下拿到 goal 像素
            _n_new_none = 0        # 新判据下"没有可用像素"（⇒ 回正/救援）
            _n_wrong = 0           # ★ 核心：站正下方 8px 却仍拿到 goal 指令
            _n_reach_ok = 0        # 真到位（Δy ≤ 容差）时仍拿到 goal（不许误伤）
            _n_reach = 0
            _wrong_cases = []      # 残差样本（新版仍发 goal 的那几条）
            _tol = int(_cc_cfg.get("goal_reach_y_tol", 3) or 3)
            for (_x, _y, _si, _mx, _my, _act) in _samples:
                _img = _imgs.get(_si)
                _gpts = _segs.get(_si) or []
                if _img is None or not _gpts or _y is None:
                    continue
                _gx, _gy = min(_gpts, key=lambda p: abs(p[0] - _x) + abs(p[1] - _y))
                _base = dict(search_range=10, rescue_range=40,
                             goal_reach_y_tol=_tol)
                # ⚠️ `_seg_goals` 必须**按段号对齐**（引擎读的是
                #    `self._seg_goals[self.idx_routes]`）—— 只放一个元素的话，
                #    段 2/3/4 会越界 ⇒ 本层被判成"判不了 ⇒ 不启用"，
                #    回放就变成"新旧完全一样"的假绿。
                _sg = [(0, 0)] * max(len(_imgs) or 1, 4)
                _sg[_si - 1] = (_gx, _gy)
                _eo = engine(cfg={"route": dict(_base, goal_pixel_skip=0)},
                             img_route=_img, player=(_x, _y), seg_goals=list(_sg),
                             idx=_si - 1, h=_img.shape[0], w=_img.shape[1])
                _en = engine(cfg={"route": dict(_base, goal_pixel_skip=1)},
                             img_route=_img, player=(_x, _y), seg_goals=list(_sg),
                             idx=_si - 1, h=_img.shape[0], w=_img.shape[1])
                _ho, _ = _eo.get_nearest_color_code()
                _co = _cmd_of(_ho)
                _co_n, _co_ud = _en.get_nearest_color_code()
                _cn = _cmd_of(_co_n)
                _reach = abs(_y - _gy) <= _tol
                if _reach and "goal" in _co:
                    _n_reach += 1
                if _act == "goal":
                    _n_log_goal += 1
                if "goal" in _co:
                    _n_old_goal += 1
                if "goal" in _cn:
                    _n_new_goal += 1
                if _co_n is None and _co_ud is None:
                    _n_new_none += 1
                if ("goal" in _co) and not _reach and "goal" in _cn:
                    _n_wrong += 1
                    if len(_wrong_cases) < 10:
                        _wrong_cases.append(
                            f"人({_x},{_y}) 段{_si} goal代理({_gx},{_gy}) "
                            f"老={_co!r} 新={_cn!r}")
                if _reach and "goal" in _co and "goal" in _cn:
                    _n_reach_ok += 1
            print(f"       日志 {os.path.basename(_path)}：地图 {_map_dir}、"
                  f"状态采样 {len(_samples)} 条、其中日志指令就是 goal 的 "
                  f"{_n_log_goal} 条")
            print(f"       老判据拿到 goal 像素 {_n_old_goal} 条 / "
                  f"新判据 {_n_new_goal} 条（其中「真到位」的 {_n_reach_ok}/{_n_reach}）"
                  f"；新判据判成「没有可用像素」 {_n_new_none} 条")
            for _wc in _wrong_cases:
                print(f"       · 残差 {_wc}")
            # 锚点用「老判据会拿到 goal 像素」而不是「日志里当时发过 goal 指令」：
            # 本层上线后，正是这些帧**不再**发 goal 指令（这就是修好的表现），
            # 再拿"日志里有 goal 指令"当准入条件等于要求自己失效。
            check_true("样本够大、且回放里确实存在「老判据会拿到 goal 像素」的帧（不是空跑）",
                       _n_old_goal > 0,
                       f"老判据 {_n_old_goal} 条 / 日志当时发过 goal 指令 {_n_log_goal} 条")
            check_true("★ 站在本段终点正下方时，新判据**不再**发 goal 指令",
                       _n_wrong == 0, f"{_n_wrong} 条")
            check_true("★ 那种帧改判成「没有可用像素」⇒ 交给回正线/救援接管",
                       _n_new_none > 0, f"{_n_new_none} 条")
            if _n_reach == 0:
                # 三态：这里要如实区分「没观测」与「没发生」—— 这一段日志里
                # 没有"人真站在本段终点上"的样本，不等于该判据没问题，
                # 它由【5】的 B/E（合成场景）覆盖。
                print("  [SKIP] 这段日志里没有「人真站在本段终点上」的样本"
                      "（属于「没观测」；该判据由【5】B/E 覆盖）")
            else:
                check_true("真的到位（|Δy| ≤ 容差）时照旧拿到 goal 像素（不许误伤切段）",
                           _n_reach_ok == _n_reach, f"{_n_reach_ok}/{_n_reach}")

    print(f"\n===== verify_goal_pixel_skip: 通过 {PASS} / 失败 {FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
