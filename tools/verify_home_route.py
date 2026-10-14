# -*- coding: utf-8 -*-
'''
离线自检：v0.9「回正线」**第二版：去掉落点锚，改比两条线的距离**（2026-09-13）

核心判定（唯一实现，进入 / 保持 / 退出共用）：
    d_home = 角色 → 最近「已武装」回正线像素 的曼哈顿距离
    d_main = 角色 → 最近主线像素 的曼哈顿距离（**全图**，不受 search_range 限制）
    d_home < d_main - home_dist_tol(2)  →  进 / 保持回正
    否则                                →  主线优先（相等时也是主线优先）

覆盖（PRD 第 10 节验收闸门 + 设计 T04）：
    1. PRD 2.3 六行验算（坑底三点进 / 主线上不进 / 竖段中段保持 / 交汇点退出）
    2. 主线上经过回正线正上方不误触发
    3. L 形拐角走得通（左端 / 中点 / 右端三个起点）
    4. PRD 2.4 反例：只画竖线 → 坑边缘接不住（**反向用例**）
    5. 判废五条：合法 L 形通过 / 只画竖线拦截 / 终点不贴主线拦截 / 无 goal 拦截 /
       跨度不足拦截 / 一图 2 条只禁 1 条
    6. 超时出口 + 冷静期不重进 + 冷静期到期解锁 + 去武装 30s 兜底
    7. 撞色自检新口径（回正线像素被底图吃掉 → 报坐标）
    8. 旧图迁移（127,0,127 涂黑 + 中文提示）
    9. 中文路径整轮
    10. 横段汇聚编码（左半 right / 右半 left / 拐角留给竖段）

⚠️ 纪律（设计 10.15）：Stub 上挂的是**引擎真实方法**，不另写一份假逻辑
    —— 否则"测的和跑的不是同一份代码"，全绿也保证不了线上不出事。

真实地形基线（维护者实测，废都南方工地，小地图 268×187）：
    平台（主线）: y = 122, x ∈ [92, 106]
    坑底        : y = 128，x ∈ [94, 108]（比平台低 6px）
    回正线 L 形 : 横段沿坑底 x=94..108(y=128) + 竖段 x=99(y=123..128) 跳回主线

不开游戏、不抢焦点、不需要 PySide6。

用法：
    python -m tools.verify_home_route
'''
import logging
import os
import shutil
import sys
import tempfile
import time

import cv2
import numpy as np

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
from src.utils.common import load_image, imwrite_unicode, mask_route_colors
from src.utils.logger import logger as PROJ_LOGGER
from src.utils.home_route import (
    manhattan, color_code_maps, home_params, draw_params,
    collect_pixels, split_components, component_has_jump,
    dist_to_pixels, mainline_dist, nearest_mainline_dist,
    calc_line_geom, far_segment_stats, cover_stats, floor_row_span,
    simulate_coverage, home_should_return,
    validate_home_line, pick_color, stamp_goal, render_home_image,
    paint_converging_hseg, home_pixel_clash, wipe_legacy_anchor,
    calc_jump_band, jump_band_params, hseg_pixel_color,
    DEFAULT_JUMP_BAND_PARAMS,
    screen_to_map, load_home_notes, save_home_notes,
    LEGACY_ANCHOR_RGB, DEFAULT_GOAL_RGB,
)

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


class capture_log:
    '''临时挂一个 handler 抓 logger 输出（用来断言"这条提示到底有没有打出来"）。'''

    def __init__(self, logger=None, level=logging.WARNING):
        raw = logger or PROJ_LOGGER
        self.logger = getattr(raw, "_logger", raw)
        self.level = level
        self.msgs = []

    def __enter__(self):
        self.h = logging.Handler()
        self.h.emit = lambda rec: self.msgs.append(rec.getMessage())
        self.h.setLevel(self.level)
        self.logger.addHandler(self.h)
        return self.msgs

    def __exit__(self, *exc):
        self.logger.removeHandler(self.h)
        return False


# ══════════════════════════════════════════════════════════════════════
# 配置与色码（与 config_default.yaml 保持一致）
# ══════════════════════════════════════════════════════════════════════
COLOR_CODE = {
    "255,0,0": "left none none",
    "0,0,255": "right none none",
    "255,127,0": "left none jump",
    "0,255,255": "right none jump",
    "127,255,0": "none down jump",
    "255,0,255": "none none jump",
    "255,255,0": "none none goal",
    "127,127,127": "none up none",
    "255,255,127": "none down none",
}
COLOR_CODE_UP_DOWN = {"127,127,127": "none up none", "255,255,127": "none down none"}

HOME_PARAMS = {
    "home_dist_tol": 2,
    "home_min_cover_span": 5,
    "home_cover_far_tol": 2,
    "home_goal_mainline_tol": 1,
    "home_min_return_span": 5,
    "home_min_leave": 0,
    "home_min_frames": 0,
    "home_timeout": 8,
    "home_reenter_lock": 10,
    "home_rearm_margin": 4,
    "home_rearm_timeout": 30,
}

CFG = {
    "bot": {"attack": "directional", "mode": "normal"},
    "key": {"teleport": ""},
    "route": dict(
        {"search_range": 10, "rescue_range": 40,
         "color_code": COLOR_CODE,
         "color_code_up_down": COLOR_CODE_UP_DOWN},
        **HOME_PARAMS),
    "route_recoder": {
        "home_draw_scale": 4, "home_draw_scale_min": 3, "home_draw_scale_max": 6,
        "home_draw_goal_radius": 1,
    },
    "watchdog": {"range": 10, "timeout": 10, "attack_grace": 5},
}

GOAL = DEFAULT_GOAL_RGB                # (255, 255, 0)
JUMP = (255, 0, 255)                   # 洋红 = 原地跳
RIGHT_JUMP = (0, 255, 255)             # 青 = 右跳
LEFT_JUMP = (255, 127, 0)              # 橙 = 左跳
BLUE = (0, 0, 255)                     # 蓝 = 向右走
RED = (255, 0, 0)                      # 红 = 向左走
GRAY = (127, 127, 127)                 # 灰 = 向上
LYELLOW = (255, 255, 127)              # 浅黄 = 向下

SHAPE = (187, 268)                     # 小地图尺寸（实测基线）

#: 真实地形（维护者实测，废都南方工地）
MAIN_Y = 122
MAIN_X0, MAIN_X1 = 92, 106             # 平台（主线）y=122 的 x 范围
PIT_Y = 128                            # 坑底
PIT_X0, PIT_X1 = 94, 108               # 可能的落点区间
CORNER_X = 99                          # L 形拐角（竖段所在列）


def parse_codes(raw):
    return {tuple(int(p) for p in k.split(',')): v for k, v in raw.items()}


CC = parse_codes(COLOR_CODE)
CC_UD = parse_codes(COLOR_CODE_UP_DOWN)
ROUTE_CODES = set(CC) | set(CC_UD)


def blank(shape=SHAPE):
    return np.zeros((shape[0], shape[1], 3), dtype=np.uint8)


def make_mainline(xs=range(MAIN_X0, MAIN_X1 + 1), y=MAIN_Y, shape=SHAPE):
    '''主线：y=122 的一条横线（蓝 = 向右走）。'''
    img = blank(shape)
    for x in xs:
        img[y, x] = BLUE
    return img


def make_l_home(corner_x=CORNER_X, x0=PIT_X0, x1=PIT_X1, pit_y=PIT_Y,
                main_y=MAIN_Y, shape=SHAPE):
    '''标准 **L 形**回正线（PRD 2.3 / 设计 3.1 的样例）：

        横段：沿坑底 x0..x1（y=坑底），**逐像素汇聚着色**（左半 right / 右半 left）
        竖段：x=corner_x，从坑底跳回主线（y=坑底-1 .. 主线+1，全是 jump 色）
        拐角：(corner_x, 坑底) —— 由**竖段最后着色**，是 jump 色
        终点：goal 盖在主线像素 (corner_x, 主线) 上

    Returns:
        (img_rgb, 全部像素列表)
    '''
    img = blank(shape)
    # ① 横段（逐像素汇聚着色，跳过拐角列）
    hseg = [(x, pit_y) for x in range(x0, x1 + 1)]
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    paint_converging_hseg(bgr, hseg, (corner_x, pit_y), CC, "walk")
    img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    # ② 竖段（含拐角像素 → 覆盖成 jump 色）
    for y in range(main_y + 1, pit_y + 1):
        img[y, corner_x] = JUMP
    # ③ 终点（最后盖）
    img[main_y, corner_x] = GOAL
    return img, split_components(img, ROUTE_CODES)[0]


# ══════════════════════════════════════════════════════════════════════
# Stub：只带被测方法所需字段，方法全挂引擎真实实现
# ══════════════════════════════════════════════════════════════════════
class Stub:
    '''只带被测方法所需字段的最小宿主（方法全部来自引擎真实实现）。'''


# ── 自动挂**全部**引擎真实方法（2026-09-15 改，对齐 verify_home_route_qa2.py）──
# ⚠️ 这里原来是**手写白名单**。引擎每加一个新方法，白名单就漏一个 → 一调到就
#    AttributeError → **脚本 exit=1 被截断**，而它**只在失败时才打印总结行**，
#    于是看着像"没报错"。
#    2026-09-15 实测：只跑到【12】就崩（`AttributeError: 'Stub' object has no
#    attribute '_perf'`，engine:3017），【12】之后**一条都没跑** —— 等于"回正判据
#    的后续用例根本没验过"。缺的正是引擎 09-14 之后新加的那一批
#    （`_perf` / `_apply_recenter` / `_apply_jump_brake` / `_trace_home_cmd` /
#     `_patrol_degraded_now` …）。
#    改成"自动挂全部"后，以后引擎再加方法**不用回来补名单**。
#    （verify_navigation.py 同一个坑踩了七次，注释原话"别再靠崩了才发现"。）
for _name in dir(MapleStoryAutoBot):
    if _name.startswith("__") or hasattr(Stub, _name):
        continue
    _attr = getattr(MapleStoryAutoBot, _name)
    if callable(_attr):
        setattr(Stub, _name, _attr)


def new_bot(player_global=(100, 100), cfg=None):
    b = Stub()
    b.cfg = cfg if cfg is not None else CFG
    b.loc_player_global = player_global
    b.loc_player = (500, 300)
    b.color_code = dict(CC)
    b.color_code_up_down = dict(CC_UD)
    b.img_routes = []
    b.img_route = None
    b.img_route_home = None
    b.img_route_debug = None
    b.is_show_debug_window = False
    b.is_using_home_route = False
    b.is_on_ladder = False
    b.idx_routes = 0
    b.route_reverse = False
    b._pingpong_start = None
    b._seg_goals = []
    b._seg_dir = []
    b._seg_span = []
    b._goal_armed = True
    b.cmd_move_x = "none"
    b.cmd_move_y = "none"
    b.cmd_action = "none"
    b.cmd_move_x_last = "none"
    b._turn_frames_left = 0        # 2026-09-17 转身状态机（见 update_cmd_by_mob_detection）
    b._turn_dir = None
    b.t_last_home_fail_log = 0.0
    b.loc_last_route_pixel = None
    b.t_route_rescue_log = 0.0
    # ── v0.9 第二版回正状态 ────────────────────────────────────────────
    b.home_lines = []
    b.home_line = None
    b.home_d_home = None
    b.home_d_main = None
    b._home_line_near = None
    b._main_seg_i = 0
    b._main_pts_xy = None
    b._main_pts_seg = None
    b.home_enter_pos = None
    b.home_enter_t = 0.0
    b.home_frame = 0
    b.home_last_goal_log = 0.0
    b.home_reenter_lock_t = 0.0
    b.t_last_home_lock_log = 0.0
    return b


def build_scenario(player=(99, PIT_Y), cfg=None):
    '''标准场景：主线（平台）+ L 形回正线 + 已解析好的 home_lines。'''
    mainline = make_mainline()
    home, comp = make_l_home()
    b = new_bot(player_global=player, cfg=cfg)
    b.img_routes = [mainline]
    b.img_route_home = home
    b.img_route = mainline
    b._build_mainline_cache()
    b.home_lines = b._scan_home_lines("自检", home, b.img_routes)
    return b, home


def frame(b):
    '''跑一整帧导航（清指令 → update_cmd_by_route），返回本帧指令。'''
    b.cmd_move_x = "none"
    b.cmd_move_y = "none"
    b.cmd_action = "none"
    b.update_cmd_by_route()
    return (b.cmd_move_x, b.cmd_move_y, b.cmd_action)


JUMP_HEIGHT = 6          # 一次跳跃在**小地图**上爬升的像素（坑深 6px，实测）

def apply_cmd(pos, cmd):
    '''把一帧指令变成位移（挂机引擎的按键 → 小地图坐标的真实关系）。

        left / right → x ∓ 1
        jump         → y - JUMP_HEIGHT（小台子一跳就上来了）
    '''
    x, y = pos
    mx, my, act = cmd
    if act == "jump":
        y -= JUMP_HEIGHT
    if mx == "right":
        x += 1
    elif mx == "left":
        x -= 1
    return (x, y)


def walk(b, start, max_frames=40):
    '''从 start 出发逐帧走，直到退出回正（或超帧）。返回 (轨迹, 是否曾进回正)。'''
    b.loc_player_global = start
    traj = []
    entered = False
    for _ in range(max_frames):
        cmd = frame(b)
        if b.is_using_home_route:
            entered = True
        traj.append((b.loc_player_global, b.is_using_home_route, cmd))
        b.loc_player_global = apply_cmd(b.loc_player_global, cmd)
        if entered and not b.is_using_home_route:
            break
    return traj, entered


# ══════════════════════════════════════════════════════════════════════
print("\n【1】4x 画布坐标换算：(400,300) → (100,75)（**禁止用 //**）")
check("4 倍图上 (400,300)", screen_to_map(400, 300, 4, 268, 187), (100, 75))
check("4 倍图上 (403,300) 要四舍五入到 101（// 会算成 100）",
      screen_to_map(403, 300, 4, 268, 187), (101, 75))
check("1 倍图恒等", screen_to_map(97, 130, 1, 268, 187), (97, 130))
check("越界要 clamp 到右下角", screen_to_map(99999, 99999, 4, 268, 187), (267, 186))
check("负数要 clamp 到 0", screen_to_map(-5, -5, 4, 268, 187), (0, 0))
check("6 倍图（滚轮上限）", screen_to_map(600, 450, 6, 268, 187), (100, 75))
check("手绘缩放默认值来自配置", draw_params(CFG)["home_draw_scale"], 4)

print("\n【2】动作 → 颜色（与录制器按键着色规则一致）")
check("向右走 = 蓝", pick_color((10, 10), (20, 11), "walk", CC), BLUE)
check("向左走 = 红", pick_color((20, 10), (10, 11), "walk", CC), RED)
check("向上走 = 灰", pick_color((10, 20), (11, 10), "walk", CC), GRAY)
check("向下走 = 浅黄", pick_color((10, 10), (11, 20), "walk", CC), LYELLOW)
check("原地跳 = 洋红", pick_color((10, 20), (10, 10), "jump", CC), JUMP)
check("向右跳 = 青", pick_color((10, 10), (20, 11), "jump", CC), RIGHT_JUMP)
check("向左跳 = 橙", pick_color((20, 10), (10, 11), "jump", CC), LEFT_JUMP)
check("向上（爬绳）= 灰", pick_color((10, 20), (11, 10), "up", CC), GRAY)
check("向下 = 浅黄", pick_color((10, 10), (11, 20), "down", CC), LYELLOW)
check("色码表被改过也永不返回非法色（回落到内置常量）",
      pick_color((10, 10), (20, 11), "walk", {}), BLUE)
check("色码表**只有两张**（没有落点锚那张）", len(color_code_maps(CFG)), 2)

print("\n【3】★ PRD 2.3 六行验算：home_should_return（tol=2）")
# d_home / d_main 全部按真实地形算：平台 y=122 x∈[92,106]，坑底 y=128
check("坑底中点 (99,128)：0 < 6-2 ✅ 进回正", home_should_return(0, 6, 2), True)
check("坑底左端 (94,128)：0 < 8-2 ✅ 进回正", home_should_return(0, 8, 2), True)
check("坑底右端 (108,128)：0 < 8-2 ✅ 进回正", home_should_return(0, 8, 2), True)
check("主线上巡逻 (99,122)：6 < 0-2 ❌ 不进回正", home_should_return(6, 0, 2), False)
check("竖段中段 (99,125)：0 < 3-2 ✅ 保持回正", home_should_return(0, 3, 2), True)
check("交汇点 (99,122)：0 < 0-2 ❌ 相等 → 主线优先，退出",
      home_should_return(0, 0, 2), False)
# 边界：相等一律主线优先
check("相等（5 vs 5）→ 主线优先", home_should_return(5, 5, 2), False)
check("只小 2px（3 vs 5）→ 主线优先（严格小于号 = tol 的意义）",
      home_should_return(3, 5, 2), False)
check("小 3px（2 vs 5）→ 进回正", home_should_return(2, 5, 2), True)
# 量不出来（None）→ 不进回正，交给老兜底
check("d_home 为 None → 不进", home_should_return(None, 6, 2), False)
check("d_main 为 None → 不进", home_should_return(0, None, 2), False)
# tol 配成负数 → 回落 2（若真按 -5 算，0 < 1+5 会成立 —— 这条把回落钉死）
check("★ tol 配成负数 → 回落 2（不是按负值算）", home_should_return(0, 1, -5), False)

print("\n【4】距离：dist_to_pixels / mainline_dist（全图最近，不套搜索框）")
xs = np.asarray([92, 99, 106], dtype=np.int32)
ys = np.asarray([122, 122, 122], dtype=np.int32)
check("点到像素集最近距离", dist_to_pixels(xs, ys, (99, 128)), 6)
check("空数组 → None（不是 0）", dist_to_pixels(np.asarray([], dtype=np.int32),
                                          np.asarray([], dtype=np.int32), (1, 1)), None)
check("pt 为 None → None", dist_to_pixels(xs, ys, None), None)
main_img = make_mainline()
check("坑底到主线 = 6（**全图**，不是 search_range 内）",
      mainline_dist((99, 128), [main_img], ROUTE_CODES), 6)
check("坑底右端到主线 = 8", mainline_dist((108, 128), [main_img], ROUTE_CODES), 8)
check("主线上 = 0", mainline_dist((99, 122), [main_img], ROUTE_CODES), 0)
check("没有主线 → None（不是假装 0）", mainline_dist((99, 128), [], ROUTE_CODES), None)
check("nearest_mainline_dist 带半径仍然可用",
      nearest_mainline_dist((99, 128), [main_img], ROUTE_CODES, 10), 6)

print("\n【5】连通域划分：一个 8-连通域 = 一条回正线")
home, comp = make_l_home()
comps = split_components(home, ROUTE_CODES)
check("L 形回正线是一个连通域", len(comps), 1)
check("总像素 21（横段 14 + 竖段 6 + goal 1）", len(comps[0]), 21)
check("认得出这条线含跳跃", component_has_jump(home, comps[0], CC), True)
# 两条线（隔得远）
img2 = home.copy()
for y in range(40, 46):
    img2[y, 150] = JUMP
img2[40, 150] = GOAL
check("隔得远 → 2 个组件", len(split_components(img2, ROUTE_CODES)), 2)
check("空图返回空列表", split_components(blank(), ROUTE_CODES), [])

print("\n【6】几何现算：calc_line_geom / far_segment_stats（设计 4.3 验算）")
main_img = make_mainline()
geom = calc_line_geom(comp, [main_img], ROUTE_CODES, CFG, img_rgb=home)
far = far_segment_stats(comp, [main_img], ROUTE_CODES, CFG)
print(f"       start={geom['start']} goal={geom['goal']} span={geom['span']} "
      f"has_jump={geom['has_jump']} bbox={geom['bbox']}")
print(f"       cover_xs={far['cover_xs']} cover_span={far['cover_span']} "
      f"(far_pts {len(far['far_pts'])} 个)")
check("终点 = 拐角列的主线像素", geom["goal"], (CORNER_X, MAIN_Y))
check("起点在坑底（离主线最远的像素）", geom["start"][1], PIT_Y)
check("跨度 ≥ 5", geom["span"] >= 5, True)
check("含跳跃", geom["has_jump"], True)
check("★ 起始段覆盖跨度 = 15（坑底整条横段，设计 4.3 验算值）", far["cover_span"], 15)
check("★ 覆盖 x 范围 = (94, 108)", far["cover_xs"], (94, 108))
# 只画竖线的反例（PRD 2.4）
vimg = blank()
for y in range(123, 129):
    vimg[y, 100] = JUMP
vimg[MAIN_Y, 100] = GOAL
vcomp = split_components(vimg, ROUTE_CODES)[0]
vfar = far_segment_stats(vcomp, [main_img], ROUTE_CODES, CFG)
check("★ 只画竖线 → cover_span = 1（判废④ 精确命中）", vfar["cover_span"], 1)

print("\n【6.5】★ cover_stats：起始段 / 落点行**两者取宽**（2026-09-17 真机假废）")
# 为什么加这一节（真机踩出来的）：
#   2026-09-17 用户录了一条**正确**的回正线：沿坑底 x34~84 横着铺满，再沿梯子爬回主线。
#   引擎却判它**废线**。根因：`far_segment_stats` 挑的是"离主线**最远**的那批像素"，
#   而这张图坑底离主线的距离是**平滑变化**的（30px → 18px），
#   只有最左 3 个像素算"最远" → cover_span=3 < 5 → 误判。
#   可它在"落点行"（y=126）上其实铺了 **51px**，坑底 165 个落点**全部**接得住。
# ⇒ 口径改成 cover_stats()：起始段 与 落点行 **谁宽用谁**。
#   ⚠️ 不能只留"落点行"：只画竖线的反例在落点行上只有 1px，但它**真的废**
#      （接不住坑两端）→ 两条必须一起看、取宽。
_fx = "tools/test_fixtures/废都南方工地_真机"
_fx_map = load_image(f"{_fx}/map.png")
_fx_home = cv2.cvtColor(load_image(f"{_fx}/route_home.png"), cv2.COLOR_BGR2RGB)
_fx_routes = [cv2.cvtColor(load_image(f"{_fx}/route{n}.png"), cv2.COLOR_BGR2RGB)
              for n in ("1", "2")]
_fx_cc = {f"{a},{b},{c}": v for (a, b, c), v in CC.items()}
_fx_ud = {f"{a},{b},{c}": v for (a, b, c), v in CC_UD.items()}
_fx_home = mask_route_colors(_fx_map, _fx_home, _fx_cc)
_fx_home = mask_route_colors(_fx_map, _fx_home, _fx_ud)
_fx_routes = [mask_route_colors(_fx_map, r, _fx_cc) for r in _fx_routes]
_fx_routes = [mask_route_colors(_fx_map, r, _fx_ud) for r in _fx_routes]
_fxc = split_components(_fx_home, ROUTE_CODES)
check("真机夹具只有 1 条回正线（118px）", (len(_fxc), len(_fxc[0])), (1, 118))
_ffar = far_segment_stats(_fxc[0], _fx_routes, ROUTE_CODES, CFG)
_fcov = cover_stats(_fxc[0], _fx_routes, ROUTE_CODES, CFG)
_frow = floor_row_span(_fxc[0], _fx_routes, ROUTE_CODES, CFG)
print(f"       旧口径 far_segment_stats cover_span={_ffar['cover_span']}"
      f"（<5 → 会误判废）")
print(f"       新口径 floor_row_span  row_y={_frow['row_y']} span={_frow['span']}"
      f" → cover_stats={_fcov['cover_span']}（by={_fcov['by']}）")
check("★ 真机线：落点行 y=126、跨度 51px", (_frow["row_y"], _frow["span"]), (126, 51))
check("★ cover_stats 取宽 = 51（不是 far 的 3）", _fcov["cover_span"], 51)
check("★ cover_stats 走的是「落点行」口径", _fcov["by"], "落点行")
check("★ 覆盖 x 范围 = (34, 84)", tuple(_fcov["cover_xs"]), (34, 84))
check("★ 真机线判**有效**（这就是 2026-09-17 修的那个假废）",
      validate_home_line(_fxc[0], _fx_home, _fx_routes, ROUTE_CODES, CFG), (True, ""))
# ── 对照（不许把反例放过）────────────────────────────────────────────
_vcov = cover_stats(vcomp, [main_img], ROUTE_CODES, CFG)
_vrow = floor_row_span(vcomp, [main_img], ROUTE_CODES, CFG)
check("★ 对照：只画竖线 → 落点行跨度 1，cover_stats 仍是 1（反例没被放过）",
      (_vrow["span"], _vcov["cover_span"]), (1, 1))
_lcov = cover_stats(comp, [main_img], ROUTE_CODES, CFG)
_lrow = floor_row_span(comp, [main_img], ROUTE_CODES, CFG)
check("对照：合法 L 形 → 落点行跨度 15，cover_stats = 15（与旧口径一致，没变松）",
      (_lrow["span"], _lcov["cover_span"]), (15, 15))
check("对照：L 形的 cover_stats 仍走「起始段」（不抢口径）", _lcov["by"], "起始段")

print("\n【6.6】★ 防『地面伪灰』：up 像素在地面层且不在真管柱上 → 不发 up（2026-09-17）")
# 真机反例：废都南方工地管道实际在 x=84（(84,122..126) 5px 连续），
# 但落点行 x=82、83 也有孤立灰（用户走到管柱前踩过留下的），
# 引擎在 (82,126) 看到距离 0 的 up 就发 up → 管道不在 x=82 → 角色按 8 秒不动
# → 漂出搜索半径 → [路线失联] 把锚点 (84,126) 旁边的蓝色 (81,126) 当"right"
# 指令发给远处的角色 → 一直往右走。
#
# 修法：up 像素在角色**同 y**上、且**同列上下 3px 内没有第二个 up 像素**
# → 判定"地面伪灰"（不是真管柱的起点）→ 用 color_code（多半是"right"）让角色
# 先走到管柱正下方再爬。
#
# 这里用真机夹具直接复现这个 bug。
if True:
    _sim_bot = new_bot()
    # ⚠️ 把灰（up 像素色）从 color_code 拿掉，否则 new_bot 用全色码表会判定 (82,126)
    # 的灰"算"color_code 也能取到，**模拟不出真实配置**。
    # 真实配置 config_default.yaml 只把灰放在 color_code_up_down。
    _sim_bot.color_code = {k: v for k, v in _sim_bot.color_code.items()
                          if k not in {(127, 127, 127), (255, 255, 127)}}
    from src.utils.common import load_image as _ldimg
    _sim_bot.img_map = _ldimg(f"{_fx}/map.png")
    _sim_bot.img_routes = []
    for _n in ("route1.png", "route2.png"):
        _rr = cv2.cvtColor(_ldimg(f"{_fx}/{_n}"), cv2.COLOR_BGR2RGB)
        _rr = mask_route_colors(_sim_bot.img_map, _rr, _fx_cc)
        _rr = mask_route_colors(_sim_bot.img_map, _rr, _fx_ud)
        _sim_bot.img_routes.append(_rr)
    _sim_bot.img_route_home = cv2.cvtColor(_ldimg(f"{_fx}/route_home.png"),
                                           cv2.COLOR_BGR2RGB)
    print("DEBUG: home loaded", "color_code_127?", (127, 127, 127) in _sim_bot.color_code)
    _sim_bot.home_lines = _sim_bot._scan_home_lines("夹具", _sim_bot.img_route_home,
                                                   _sim_bot.img_routes)
    print("DEBUG: scanned", len(_sim_bot.home_lines))
    print("DEBUG: home_lines typed=", type(_sim_bot.home_lines))
    _sim_bot._build_mainline_cache()
    # 模拟：从 (82,126) 出发（地面伪灰位置），3 帧内必须先**走右**，不能僵在 up
    for k in ("is_on_ladder", "cmd_move_x_last"):
        if not hasattr(_sim_bot, k):
            setattr(_sim_bot, k, False if k == "is_on_ladder" else "none")
    _sim_bot.is_on_ladder = False
    _sim_bot.loc_player_global = (82, 126)
    _sim_bot.is_using_home_route = True   # 强制回正状态跑
    _sim_bot.img_route = _sim_bot.img_route_home   # 回正时引擎把 img_route 换成 home
    _home_d_main, _ = _sim_bot._main_dist()
    _home_d_home, _ = _sim_bot._home_dist()
    _sim_bot.home_d_main = _home_d_main
    _sim_bot.home_d_home = _home_d_home
    _sim_bot.cmd_move_x = "none"; _sim_bot.cmd_move_y = "none"; _sim_bot.cmd_action = "none"
    _sim_bot.update_cmd_by_route()
    cmd_at_82 = (_sim_bot.cmd_move_x, _sim_bot.cmd_move_y, _sim_bot.cmd_action)
    check("★ 落点行 (82,126) 不再发 up（伪灰被压制）", cmd_at_82, ("right", "none", "none"))
    # 对照：(84,126) 仍然发 up（真管柱起点）
    _sim_bot.loc_player_global = (84, 126)
    _sim_bot.home_d_main = None; _sim_bot.home_d_home = None
    _sim_bot.img_route = _sim_bot.img_route_home
    _sim_bot.cmd_move_x = "none"; _sim_bot.cmd_move_y = "none"; _sim_bot.cmd_action = "none"
    _sim_bot.update_cmd_by_route()
    cmd_at_84 = (_sim_bot.cmd_move_x, _sim_bot.cmd_move_y, _sim_bot.cmd_action)
    check("对照：管柱真起点 (84,126) 正常发 up", cmd_at_84, ("none", "up", "none"))

print("\n【6.7】★ 真绳梯在当前段图上（up 像素落在角色这一行）⇒ 必须发 up，不能照抄横向（2026-10-14）")
# 真机反例（废铁2，17:22 场逐帧复刻）：角色走到 (162,78) 正踩在 x=163 那根绳梯脚下，
# route1 里 (162,78)、(163,78) 是 up 像素、(163,75) 是同一列更上面的第二个竖向像素；
# 而 route_home.png 全图的竖向像素只有 x=70..71 那 9 个（y=80..88）⇒ 旧版伪灰闸
# （只拿回正线当参照）把**每一根真绳梯**都判成"地面伪灰" ⇒ 照抄横向 right
# ⇒ 角色从绳梯脚下直接走过去、全程不按 up（真机 17:23:24 全局 (178,78) 指令 right none none）。
# 修法：判"是不是真绳梯"时同时看**当前段**路线图（同列 ±1px、上下 3px 内有竖向色码）。

def _fakeup_cmd(with_rope, giveup=False, on_ladder=False, ladder_finish=None):
    '''合成 40×40 段图：(20,19) 一个 right、(20,20) 一个 up（正好在角色这一行）；
       with_rope=True 时在 (20,19)/(20,18) 追两个 up 像素 = 真绳梯的连续竖向。
       giveup=True 时先摆出 P0 的"暂停爬绳"窗口（`_rope_giveup_until` 在未来）。
       on_ladder=True 时把 `is_on_ladder` 旗标摆真（爬梯走完那层的入口条件）；
       ladder_finish=False 时把 `route.ladder_finish` 关掉（回滚口径对照）。'''
    b = new_bot(player_global=(20, 20))
    b.color_code = {k: v for k, v in b.color_code.items()
                    if k not in {(127, 127, 127), (255, 255, 127)}}
    seg = np.zeros((40, 40, 3), np.uint8)
    seg[20, 10:31] = (0, 0, 255)         # 角色这一行是一整条横向路线（最近的横向像素 = (19,20)、(21,20)）
    #                                      ⚠️ 必须让这一行有跨度：否则引擎的"边缘保护"
    #                                      （`_apply_edge_guard`）会判"人已到路线右边界"强制改向左，
    #                                      观测不到伪灰闸自己给的方向。
    seg[20, 20] = (127, 127, 127)        # y=20, x=20 → up（距离 0，与角色同一行）
    if with_rope:
        seg[19, 20] = (127, 127, 127)    # 同一列向上连续 ⇒ 真绳梯
        seg[18, 20] = (127, 127, 127)
    b.img_map = np.zeros((40, 40, 3), np.uint8)
    b.img_routes = [seg]
    b.img_route = seg
    b.img_route_home = np.zeros((40, 40, 3), np.uint8)   # 回正线里这一列没有竖向像素
    b._build_mainline_cache()
    b.home_lines = []
    if giveup:
        # ★ P0（2026-10-14，用户 m10830）：模拟"这根绳判定上不去"的暂停窗口
        b._rope_giveup_until = time.time() + 5.0
    if on_ladder:
        # ★ 爬梯走完（2026-10-15，用户 m11669）：引擎自己说"人在梯子上"，
        #   但这一帧路线只给了横向（with_rope=False）⇒ 正是真机里"键在发、人不动"那种帧。
        b.is_on_ladder = True
        b.t_ladder_move = time.time() - 30.0     # 30s 没有竖向进展
    if ladder_finish is not None:
        b.cfg["route"]["ladder_finish"] = ladder_finish
    return frame(b)

_rope_cmd = _fakeup_cmd(True)
_fake_cmd = _fakeup_cmd(False)
check("★ 真绳梯（当前段图上同列有连续竖向像素）→ 发 up，不再被伪灰闸压成横向",
      _rope_cmd[1], "up")
check("对照：孤立灰（同列上下没有竖向延续）仍按伪灰处理 → 走横向",
      (_fake_cmd[0], _fake_cmd[1]), ("right", "none"))
print(f"       真绳梯 {_rope_cmd} ｜ 孤立灰 {_fake_cmd}")

# ★ P0（2026-10-14，用户 m10830）：爬绳超时 ⇒ 暂停期间**不再死按 up**。
#   真机取证（log/MSBot_2026-10-14_17-40-51.log 段11，152 分钟）：>10s 的打怪空档
#   105 次里 87 次（1540s = 会话 17%）角色几乎没动（净位移 ≤8px）、而发键 90~96 次/5s
#   ⇒ 一直按着 up 顶着绳；同时怪逻辑被让路块整段静音（最长一次 211s）。
_giveup_cmd = _fakeup_cmd(True, giveup=True)
check("★ P0：暂停爬绳期间同一张图不再发 up（改发 none，控制权交回怪逻辑）",
      _giveup_cmd[1], "none")
check("★ P0：暂停期间只压竖向 —— 横向/动作照旧（不许把整条指令清掉）",
      (_giveup_cmd[0], _giveup_cmd[2]), (_rope_cmd[0], _rope_cmd[2]))
print(f"       P0 暂停期 {_giveup_cmd} ｜ 正常 {_rope_cmd}")

# ★ 爬梯走完（2026-10-15，用户 m11669「上梯子要走完，不要检测怪了」）：
#   真机取证（log/MSBot_2026-10-15_00-36-31.log，26 分钟）：全局坐标连续 ≥5s 不动的
#   8 段**全部是"竖向指令 0/N 帧"**（30s @(210,60)、15s @(78,74)、15s @(180,64)…）
#   —— 梯子上按左右键是不动的，而路线这帧只给了横向 ⇒ 键在发、人不动，
#   同时 `_ladder_yield()` 判"没在爬"⇒ 怪逻辑被放进来站桩打怪。
_ladder_cmd = _fakeup_cmd(False, on_ladder=True)
check("★ 爬梯走完：在梯上、路线这帧只给横向 ⇒ 替它补 up（而不是照抄横向）",
      (_ladder_cmd[0], _ladder_cmd[1]), ("right", "up"))
check("★ 爬梯走完：只补竖向 —— 动作照旧（不许顺手改动作/横向）",
      _ladder_cmd[2], _fake_cmd[2])
_off_cmd = _fakeup_cmd(False, on_ladder=True, ladder_finish=False)
check("★ 爬梯走完：route.ladder_finish=false ⇒ 逐字退回老行为（这就是回滚口径）",
      (_off_cmd[0], _off_cmd[1]), ("right", "none"))
_no_ladder_cmd = _fakeup_cmd(False, on_ladder=False)
check("对照：不在梯子上 ⇒ 不补（本层只信 is_on_ladder）",
      (_no_ladder_cmd[0], _no_ladder_cmd[1]), ("right", "none"))
print(f"       梯上路线只给横向 {_ladder_cmd} ｜ 关掉本层 {_off_cmd} ｜ 不在梯上 {_no_ladder_cmd}")

print("\n【7】覆盖模拟 simulate_coverage：L 形 15/15，只画竖线接不住")
landing = [(x, PIT_Y) for x in range(PIT_X0, PIT_X1 + 1)]
cov_l = simulate_coverage(comp, [(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)],
                          landing, 2)
check("落点区间共 15 个点", cov_l["total"], 15)
check("★ L 形：15/15 全接住", cov_l["covered"], 15)
check("L 形没有漏网的落点", cov_l["missed"], [])
cov_v = simulate_coverage(vcomp, [(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)],
                          landing, 2)
check("只画竖线：接不住（< 100%）", cov_v["covered"] < cov_v["total"], True)
check("只画竖线：坑底右端 (108,128) 确实接不住", (108, 128) in cov_v["missed"], True)
print(f"       只画竖线覆盖率 {cov_v['covered']}/{cov_v['total']}"
      f"（L 形 {cov_l['covered']}/{cov_l['total']}）")
check("没有回正线像素 → 全部接不住", simulate_coverage([], [(99, 122)], landing, 2)["covered"], 0)

print("\n【8】横段汇聚编码 paint_converging_hseg（设计 3.2，Q9 裁决）")
himg = blank()
bgr_h = cv2.cvtColor(himg, cv2.COLOR_RGB2BGR)
paint_converging_hseg(bgr_h, [(x, PIT_Y) for x in range(PIT_X0, PIT_X1 + 1)],
                      (CORNER_X, PIT_Y), CC, "walk")
himg = cv2.cvtColor(bgr_h, cv2.COLOR_BGR2RGB)
check("拐角左边 (95,128) = 蓝 right（往右走向拐角）", tuple(int(v) for v in himg[PIT_Y, 95]), BLUE)
check("拐角左边 (98,128) = 蓝 right", tuple(int(v) for v in himg[PIT_Y, 98]), BLUE)
check("拐角右边 (100,128) = 红 left（往左走向拐角）", tuple(int(v) for v in himg[PIT_Y, 100]), RED)
check("拐角右边 (108,128) = 红 left", tuple(int(v) for v in himg[PIT_Y, 108]), RED)
check("★ 拐角列被**跳过**（留给竖段着 jump 色）", int(himg[PIT_Y, CORNER_X].sum()), 0)
# 整段单色是 bug：这里用"左右两半颜色不同"这个断言把它钉死
check("★ 横段左右两半方向相反（不是整段单色）",
      tuple(int(v) for v in himg[PIT_Y, 96]) != tuple(int(v) for v in himg[PIT_Y, 104]), True)
# 画完横段再画竖段 → 拐角变成 jump 色
img_full = himg.copy()
for y in range(MAIN_Y + 1, PIT_Y + 1):
    img_full[y, CORNER_X] = JUMP
check("拐角最终是 jump 色（竖段后画，覆盖横段）",
      tuple(int(v) for v in img_full[PIT_Y, CORNER_X]), JUMP)

print("\n【9】判废五条（第二版判据，逐条命中一次）")
mainline = make_mainline()


def judge(pts, img, routes=None):
    return validate_home_line(pts, img, routes if routes is not None else [mainline],
                              ROUTE_CODES, CFG)


# ① 没有 goal
bad1, c1 = make_l_home()
gset = goal_codes_of = {GOAL}
for p in c1:
    if tuple(int(v) for v in bad1[p[1], p[0]]) == GOAL:
        bad1[p[1], p[0]] = JUMP
ok, why = judge(c1, bad1)
check("①没有终点标记 → 判废", ok, False)
print(f"       {why}")
# ② 一个动作像素都没有（只有 goal）
bad2 = blank()
bad2[MAIN_Y, 100] = GOAL
ok, why = judge([(100, MAIN_Y)], bad2)
check("②一个动作像素都没有 → 判废", ok, False)
print(f"       {why}")
# ③ 跨度 < 5（原地起头原地收尾）
bad3 = blank()
for y in range(MAIN_Y + 1, MAIN_Y + 4):
    bad3[y, 100] = JUMP
bad3[MAIN_Y, 100] = GOAL
ok, why = judge([(100, y) for y in range(MAIN_Y + 1, MAIN_Y + 4)] + [(100, MAIN_Y)], bad3)
check("③跨度 3px < 5 → 判废", ok, False)
print(f"       {why}")
# ④ 只画竖线（覆盖跨度 1）
ok, why = judge(vcomp, vimg)
check("④只画竖线（cover_span=1）→ 判废", ok, False)
print(f"       {why}")
# ⑤ 终点没画在主线上（离主线 5px）
bad5, c5 = make_l_home()
for p in c5:
    if tuple(int(v) for v in bad5[p[1], p[0]]) == GOAL:
        bad5[p[1], p[0]] = JUMP          # 擦掉 goal
bad5[MAIN_Y + 5, CORNER_X] = GOAL        # 终点画到离主线 5px 的地方
c5b = split_components(bad5, ROUTE_CODES)
# 注意：终点挪下去后可能与竖段断开 → 取最大的那个组件（竖段）
c5b.sort(key=len, reverse=True)
check("⑤终点离主线 4px（> tol 2）→ 判废", judge(c5b[0], bad5)[0], False)
# 全绿：标准 L 形
ok, why = judge(comp, home)
check("★ 合法 L 形（覆盖 15px + 终点贴主线）→ 判有效", (ok, why), (True, ""))
# 没有主线 → 量不出 d_main → 判废（不是假装通过）
ok, why = validate_home_line(comp, home, [], ROUTE_CODES, CFG)
check("没有主线 → 判废（量不出距离）", ok, False)
print(f"       {why}")
# 空组件
check("空组件 → 判废", judge([], blank())[0], False)

print("\n【10】终点贴主线用 **min** 不是质心（黄圆点有一个像素贴上就过）")
# 标准 L 形，但终点盖成一个 3px 的小圆点（有一个像素正好压在主线上）
big = blank()
hseg = [(x, PIT_Y) for x in range(PIT_X0, PIT_X1 + 1)]
bgr_b = cv2.cvtColor(big, cv2.COLOR_RGB2BGR)
paint_converging_hseg(bgr_b, hseg, (CORNER_X, PIT_Y), CC, "walk")
big = cv2.cvtColor(bgr_b, cv2.COLOR_BGR2RGB)
for y in range(MAIN_Y + 1, PIT_Y + 1):
    big[y, CORNER_X] = JUMP
for dx in (-1, 0, 1):
    big[MAIN_Y, CORNER_X + dx] = GOAL      # 三个 goal 像素，其中一个在主线上
bcomp = split_components(big, ROUTE_CODES)[0]
ok, _why = judge(bcomp, big)
check("goal 有一个像素压在主线 → 通过（不会误杀）", ok, True)
check("（对照）同一个圆点：这也证明判据⑤用的是 min 不是质心",
      far_segment_stats(bcomp, [mainline], ROUTE_CODES, CFG)["cover_span"], 15)

print("\n【11】一图 2 条线、其中 1 条废 → 只禁那 1 条，另 1 条照常（不连坐）")
img2 = home.copy()
for y in range(123, 129):
    img2[y, 150] = JUMP          # 只画竖线 → 废线（覆盖跨度 1）
img2[MAIN_Y, 150] = GOAL
b = new_bot()
b.img_routes = [mainline]
b._build_mainline_cache()
n_before = len(collect_pixels(img2, ROUTE_CODES))
lines2 = b._scan_home_lines("自检2", img2, b.img_routes)
check("只留下 1 条可用", len(lines2), 1)
check("留下的是画对了的那条（L 形）", lines2[0]["cover_span"], 15)
n_after = len(collect_pixels(img2, ROUTE_CODES))
check("废线像素已被逐条抹掉（另一条一个像素都没少）", (n_before, n_after), (28, 21))
check("另一条线仍然武装可用", lines2[0]["armed"], True)

print("\n【12】★ 坑底三点都能进回正（PRD 闸门 1 + 2）")
for pos, tag in (((99, PIT_Y), "中点"), ((PIT_X0, PIT_Y), "左端"), ((PIT_X1, PIT_Y), "右端")):
    b, _h = build_scenario(player=pos)
    check(f"{tag} {pos}：L 形回正线被判有效并加载", len(b.home_lines), 1)
    b.update_cmd_by_route()
    check(f"{tag} {pos}：进回正（此刻主线明明还看得见）", b.is_using_home_route, True)
    check(f"{tag} {pos}：img_route 已切到回正线", b.img_route is b.img_route_home, True)
    check(f"{tag} {pos}：记录了进入位置", b.home_enter_pos, tuple(pos))

print("\n【13】★ 主线上巡逻（含从回正线正上方经过）→ **不**误进回正（PRD 闸门 2）")
b, _h = build_scenario(player=(99, MAIN_Y))
b.update_cmd_by_route()
check("站主线上 → 不进回正", b.is_using_home_route, False)
for x in range(MAIN_X0, MAIN_X1 + 1):
    b.loc_player_global = (x, MAIN_Y)
    b.home_frame = 0
    b.update_cmd_by_route()
    if b.is_using_home_route:
        FAIL.append(f"主线上 x={x} 误进回正")
        print(f"  [FAIL] 主线上 x={x} 误进回正")
print(f"       主线上 {MAIN_X0}..{MAIN_X1} 共 {MAIN_X1 - MAIN_X0 + 1} 个位置："
      f"误进回正 {len([1 for n in FAIL if '误进回正' in n])} 次")
check("主线上全程不误进回正", len([1 for n in FAIL if '误进回正' in n]), 0)

print("\n【14】★ L 形拐角跟随：左端 / 中点 / 右端三个起点都走通并退出（PRD 闸门 3）")
for start, tag in (((PIT_X0, PIT_Y), "左端"), ((99, PIT_Y), "中点"), ((PIT_X1, PIT_Y), "右端")):
    b, _h = build_scenario(player=start)
    traj, entered = walk(b, start)
    end_pos = traj[-1][0] if traj else start
    d_main_end = mainline_dist(end_pos, b.img_routes, ROUTE_CODES)
    print(f"       {tag} {start} → 走了 {len(traj)} 帧 → 终点 {end_pos}，"
          f"离主线 {d_main_end}px，仍在回正={b.is_using_home_route}")
    check(f"{tag}：进过回正", entered, True)
    check(f"{tag}：最终**退出**了回正（没卡住）", b.is_using_home_route, False)
    check(f"{tag}：最终已经爬上平台（y ≤ {MAIN_Y + 2}）", end_pos[1] <= MAIN_Y + 2, True)
    check(f"{tag}：最终离主线 ≤ 2px（主线逻辑能接住）", d_main_end <= 2, True)
    check(f"{tag}：走到了拐角列附近", abs(end_pos[0] - CORNER_X) <= 2, True)

print("\n【15】★ PRD 2.4 反例：只画竖线 → 坑边缘接不住（**反向用例**）")
b = new_bot()
b.img_routes = [mainline]
b._build_mainline_cache()
b.img_route_home = vimg.copy()
b.img_route = b.img_routes[0]
with capture_log(level=logging.ERROR) as msgs:
    b.home_lines = b._scan_home_lines("自检-竖线", b.img_route_home, b.img_routes)
check("只画竖线的回正线 → 0 条可用", len(b.home_lines), 0)
check("给了中文判废原因（不是静默失效）",
      any("只接得住" in m for m in msgs), True)
# 即便强行把它塞进 home_lines，坑底右端也接不住（证明覆盖检查不是形式主义）
forced = [{
    "id": 1, "start": (100, PIT_Y), "goal": (100, MAIN_Y), "span": 6,
    "has_jump": True, "n_pts": 7, "far_pts": [], "cover_xs": (100, 100),
    "cover_span": 1, "bbox": (100, MAIN_Y, 100, PIT_Y),
    "xs": np.asarray([100] * 7, dtype=np.int32),
    "ys": np.asarray(list(range(MAIN_Y, PIT_Y + 1)), dtype=np.int32),
    "armed": True, "disarm_t": 0.0,
}]
b.home_lines = forced
b.img_route_home = vimg
b.loc_player_global = (PIT_X1, PIT_Y)
b.home_d_main, b._main_seg_i = b._main_dist()
b.home_d_home, b._home_line_near = b._home_dist()
check("坑底右端：d_home = 8", b.home_d_home, 8)
check("坑底右端：d_main = 8", b.home_d_main, 8)
check("★ 8 < 8-2 不成立 → 接不住（与 PRD 2.4 表格一致）",
      home_should_return(b.home_d_home, b.home_d_main, 2), False)
check("★ 新入口不放行", b._home_line_pick(), None)

print("\n【16】8s 超时出口 + 冷静期不重进 + 到期解锁 + 去武装 30s 兜底")
# 场景：主线在 y=100（离落点 70px），回正线在 y=167..170（**接不上主线**，只能超时）
far_main = make_mainline(y=100)
far_home = blank()
for i, (x, y) in enumerate([(99, 170), (100, 169), (101, 168), (102, 167)]):
    far_home[y, x] = JUMP
far_home[167, 102] = GOAL
b = new_bot()
b.img_routes = [far_main]
b.img_route_home = far_home
b.img_route = far_main
b._build_mainline_cache()
b.home_lines = [{
    "id": 1, "start": (99, 170), "goal": (102, 167), "span": 6, "has_jump": True,
    "n_pts": 4, "far_pts": [], "cover_xs": (99, 102), "cover_span": 4,
    "bbox": (99, 167, 102, 170),
    "xs": np.asarray([99, 100, 101, 102], dtype=np.int32),
    "ys": np.asarray([170, 169, 168, 167], dtype=np.int32),
    "armed": True, "disarm_t": 0.0,
}]
b.loc_player_global = (99, 170)
b.update_cmd_by_route()
check("掉到落点 → 进入回正", b.is_using_home_route, True)
check("回正线就在脚边（真实搜索取得到像素 → 走得到）",
      b.get_nearest_color_code()[0] is not None, True)
b.home_enter_t = time.time() - 9        # 已经回正 9s > home_timeout=8
b.update_cmd_by_route()
check("超时 → 退出回正（有出口）", b.is_using_home_route, False)
check("★ 这条线被去武装（D2）", b.home_lines[0]["armed"], False)
check("★ 人还站在坑底，但下一帧不会立刻重进", b._home_line_pick(), None)
check("冷静期已上锁", b.home_reenter_lock_t > 0, True)
check("锁定期间新入口不放行（D1）", b._home_reenter_allowed(), False)
# 到期解锁
b.home_reenter_lock_t = time.time() - 11
check("冷静期到期 → 自动解锁", b._home_reenter_allowed(), True)
check("★ 但那条坏线仍未武装 → 还是不进（D2 生效）", b._home_line_pick(), None)
# 去武装兜底：30s 后无条件重新武装
b.home_lines[0]["disarm_t"] = time.time() - 31
check("30s 兜底 → 无条件重新武装", b._home_line_armed(b.home_lines[0]), True)
# 重新武装路径 A：离开 bbox ≥ 4px
b.home_lines[0]["armed"] = False
b.home_lines[0]["disarm_t"] = time.time()
b.loc_player_global = (99, 176)          # 离 bbox(y 167..170) 6px
check("离开 bbox ≥ 4px → 重新武装", b._home_line_armed(b.home_lines[0]), True)

print("\n【17】成功回主线 → **不留**冷静期 + 该线重新武装（PRD 闸门 1 最后一行）")
b, _h = build_scenario(player=(99, PIT_Y))
b.update_cmd_by_route()
check("进回正", b.is_using_home_route, True)
b.loc_player_global = (CORNER_X, MAIN_Y)     # 一跳回到主线（回正线终点）
b.update_cmd_by_route()
b.update_cmd_by_route()   # 第二帧：退出回正要求「连续两帧 y 相同」（防起跳途中被判已回主线）
check("到终点（d_home = d_main = 0）→ 退出", b.is_using_home_route, False)
check("成功退出**不上**冷静期", b.home_reenter_lock_t, 0.0)
check("该线已重新武装（下次掉下去还会接住）", b.home_lines[0]["armed"], True)
check("退出后导航图交还主线", b.img_route is b.img_routes[0], True)

print("\n【18】撞色自检：回正线像素被地图底色吃掉 → 报坐标（不能静默）")
home_raw, _c = make_l_home()
map_bgr = np.zeros(SHAPE + (3,), dtype=np.uint8)
map_bgr[PIT_Y, 95] = (BLUE[2], BLUE[1], BLUE[0])     # 底图里恰好有蓝色
home_masked = mask_route_colors(map_bgr, home_raw.copy(), COLOR_CODE)
eaten = home_pixel_clash(home_raw, home_masked, ROUTE_CODES)
check("报出被吃掉的回正线像素坐标", eaten, [(95, PIT_Y)])
check("没被吃掉时不误报", home_pixel_clash(home_raw, home_raw, ROUTE_CODES), [])
b = new_bot()
b.img_routes = [mainline]
b._build_mainline_cache()
check("被吃掉一个像素不影响这条线仍可用（横段还长）",
      len(b._scan_home_lines("自检", home_masked, b.img_routes)), 1)

print("\n【19】旧图迁移：127,0,127 被涂黑 + 中文提示（Q6）")
img_legacy, _c = make_l_home()
img_legacy[PIT_Y, 96] = LEGACY_ANCHOR_RGB
img_legacy[PIT_Y, 97] = LEGACY_ANCHOR_RGB
wiped = wipe_legacy_anchor(img_legacy)
check("涂黑了 2 个旧落点标记", sorted(wiped), [(96, PIT_Y), (97, PIT_Y)])
check("涂黑后这些位置是黑的", int(img_legacy[PIT_Y, 96].sum()), 0)
check("没有旧标记时返回空", wipe_legacy_anchor(blank()), [])
check("None 图不炸", wipe_legacy_anchor(None), [])
b = new_bot()
b.img_routes = [mainline]
b._build_mainline_cache()
n_home = len(collect_pixels(img_legacy, ROUTE_CODES))
check("涂黑旧标记后这条线仍然可用（旧标记不在色码表里，本来就不算路线像素）",
      len(b._scan_home_lines("自检", img_legacy, b.img_routes)), 1)

print("\n【20】重绘 render_home_image：**没有锚点**（第二版只有段 + goal）")
base = np.zeros(SHAPE + (3,), dtype=np.uint8)     # BGR 底图
out = render_home_image(base, [{
    "points": [(100, 112), (101, 111), (102, 110)],
    "actions": ["jump", "jump"],
    "goal": (102, 110),
}], CC)
rgb = cv2.cvtColor(out, cv2.COLOR_BGR2RGB)
check("终点像素是 goal 色", tuple(int(v) for v in rgb[110, 102]), GOAL)
check("段像素是跳跃色（右跳=青）", tuple(int(v) for v in rgb[111, 101]), RIGHT_JUMP)
check("底图没被就地改坏（返回的是新图）", int(base.sum()), 0)
check("空列表 → 全黑（等价删除全部）",
      int(render_home_image(np.zeros(SHAPE + (3,), dtype=np.uint8), [], CC).sum()), 0)
check("★ 整张图里一个 127,0,127 都没有",
      bool(np.all(rgb == np.array(LEGACY_ANCHOR_RGB, dtype=np.uint8), axis=-1).any()), False)

print("\n【21】可选备注 json：读写都要**静默失败**（引擎运行时不读它）")
notes_dir = os.path.join(tempfile.gettempdir(), "冒险岛自检备注_" + str(os.getpid()))
ok_write = save_home_notes(notes_dir, {"version": 1, "notes": [
    {"start": [98, 130], "name": "左边坑", "note": "从最左边台子掉下去"}]})
check("写进去能原样读回来", load_home_notes(notes_dir).get("notes")[0]["name"], "左边坑")
check("读不存在的备注 → 静默返回空（不抛异常）", load_home_notes("这张图根本不存在"), {})
check("save_home_notes 返回 bool（不抛异常）", isinstance(ok_write, bool), True)
shutil.rmtree(notes_dir, ignore_errors=True)

print("\n【22】中文路径整轮：画一条 → 落盘 → 读回 → 扫描（cv2.imwrite 会静默失败）")
tmpdir = os.path.join(tempfile.gettempdir(), "回正线自检_中文路径")
os.makedirs(tmpdir, exist_ok=True)
try:
    out_img = render_home_image(np.zeros(SHAPE + (3,), dtype=np.uint8), [{
        "points": [(100, 112), (101, 111), (102, 110), (103, 109)],
        "actions": ["jump", "jump", "jump"],
        "goal": (103, 109),
    }], CC)
    p = os.path.join(tmpdir, "route_home.png")
    check("imwrite_unicode 写中文路径成功", imwrite_unicode(p, out_img), True)
    back = cv2.cvtColor(load_image(p), cv2.COLOR_BGR2RGB)
    check("读回后能重新解析出这条线", len(split_components(back, ROUTE_CODES)), 1)
    g = calc_line_geom(split_components(back, ROUTE_CODES)[0], [], ROUTE_CODES, CFG,
                       img_rgb=back)
    check("没有主线时起点退化为最上最左（不崩）", g["start"], (103, 108))
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════════
# 废线夹具：自己画一条「非 L 形 / 终点没贴主线」的非法回正线
# ══════════════════════════════════════════════════════════════════════
_JUMP_RGB = (255, 0, 255)                  # 洋红 = none none jump
_BLUE_RGB = (0, 0, 255)                    # 蓝   = right none none

#: 废线①：**只画一条竖线**（非 L 形）—— PRD 2.4 的反例 → 判废④（覆盖跨度 = 1）
WASTE_VERTICAL = {
    "segs": [((150, 130), (150, 140), _JUMP_RGB)],
    "goal": (150, 130),
}
#: 废线②：L 形画对了，但**终点停在半空**（离主线 4px）→ 判废⑤（终点没贴主线）
WASTE_GOAL_FAR = {
    "segs": [((86, 128), (110, 128), _BLUE_RGB), ((98, 124), (98, 128), _JUMP_RGB)],
    "goal": (98, 127),
}

#: 废线夹具一览（子目录中文名 → 夹具、人话原因）
WASTE_FIXTURES = (
    ("只画竖线", WASTE_VERTICAL, "非 L 形 / 只画一条竖线"),
    ("终点悬空", WASTE_GOAL_FAR, "L 形但终点没贴主线"),
)


def waste_home_image(map_img, sub, spec):
    '''自绘一张「废线」route_home.png —— **落盘到中文路径再读回**。

    ⚠️ 为什么必须用夹具、不能直接读 `minimaps/废都南方工地/route_home.png`：
       那条线 2026-09-13 被重画成了**合法**的 L 形（横段 y=128 汇聚着色 +
       竖段 x=98 跳跃 + 终点 (98,122) 贴主线，34 px / 1 条可用）。于是
       "废线必须被判废"这条规则被绑死在**现网资源当时的临时状态**上 ——
       用户一画好就必然红。断言要的是"规则成立"，不是"文件此刻长这样"，
       所以夹具自己画，跟现网文件怎么改都无关。

    ⚠️ 为什么仍然要落盘到**中文路径**再读回：`cv2.imread` 在中文路径上
       **静默返回 None**（不抛异常）。原用例顺带覆盖了这条真实存在的坑，
       改用夹具后这条覆盖不能丢 —— 落盘走 `imwrite_unicode`、读回走
       `load_image`（内部是 `cv2.imdecode`），和线上是同一套。

    Returns:
        RGB 图（已按现网口径 mask 两遍），可直接喂给 `bot._scan_home_lines`
    '''
    tmp = os.path.join(tempfile.gettempdir(), "回正线自检_废线夹具", sub)
    os.makedirs(tmp, exist_ok=True)
    try:
        bgr = np.zeros(SHAPE + (3,), dtype=np.uint8)
        for (x0, y0), (x1, y1), rgb in spec["segs"]:
            cv2.line(bgr, (x0, y0), (x1, y1),
                     (int(rgb[2]), int(rgb[1]), int(rgb[0])), thickness=1)
        stamp_goal(bgr, spec["goal"], GOAL, 1)
        path = os.path.join(tmp, "route_home.png")
        imwrite_unicode(path, bgr)
        img = cv2.cvtColor(load_image(path), cv2.COLOR_BGR2RGB)
        img = mask_route_colors(map_img, img, COLOR_CODE)
        return mask_route_colors(map_img, img, COLOR_CODE_UP_DOWN)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:                                   # 父目录空了就一并清掉，不留空壳
            os.rmdir(os.path.dirname(tmp))
        except OSError:
            pass


print("\n【23】真实资源：废都南方工地（中文路径）走完整链路 + 废线夹具判废")
try:
    # ⚠️ 冻结夹具，不读 `minimaps/废都南方工地/` 活文件（2026-09-16 改）：
    #    用户重录主线后 route1/route2 像素数从 21/21 变成 65/53，本节几条断言全红 ——
    #    红的是数据不是代码。夹具 = 断言写定时对着的那份数据，见 tools/test_fixtures/。
    img_map = load_image("tools/test_fixtures/废都南方工地/map.png")
    routes = []
    for name in ("route1.png", "route2.png"):
        r = cv2.cvtColor(load_image(f"tools/test_fixtures/废都南方工地/{name}"), cv2.COLOR_BGR2RGB)
        r = mask_route_colors(img_map, r, COLOR_CODE)
        r = mask_route_colors(img_map, r, COLOR_CODE_UP_DOWN)
        routes.append(r)
    b = new_bot()
    b.img_routes = routes
    b._build_mainline_cache()
    check("主线 route1/route2 像素数不变（各 21）",
          [len(b._route_pixels(r)) for r in routes], [21, 21])
    check("主线像素缓存建起来了", b._main_pts_xy is not None, True)

    # ── ① 废线夹具（自绘，与现网文件无关）────────────────────────────
    for _sub, _spec, _why in WASTE_FIXTURES:
        _waste = waste_home_image(img_map, _sub, _spec)
        _n0 = len(collect_pixels(_waste, ROUTE_CODES))
        check(f"夹具「{_why}」中文路径落盘→读回后像素还在（{_n0} px）",
              _n0 > 0, True)
        check(f"★ 废线「{_why}」→ 0 条可用",
              len(b._scan_home_lines("废线夹具", _waste, routes)), 0)
        check(f"★ 废线「{_why}」像素被逐条抹掉",
              len(collect_pixels(_waste, ROUTE_CODES)), 0)

    # ── ② 现网 route_home.png：只断言"状态无关"的不变量 ───────────────
    # 用户随时会重画这条线（废→合法、合法→废都正常），所以**不写死几条**：
    # 写死 0 会在画好后红，写死 1 会在重画/删线后红。
    home_real = cv2.cvtColor(load_image("tools/test_fixtures/废都南方工地/route_home.png"),
                             cv2.COLOR_BGR2RGB)
    check("现网 route_home.png 在中文路径下读得到",
          None if home_real is None else home_real.shape, SHAPE + (3,))
    home_real = mask_route_colors(img_map, home_real, COLOR_CODE)
    home_real = mask_route_colors(img_map, home_real, COLOR_CODE_UP_DOWN)
    n_before = len(collect_pixels(home_real, ROUTE_CODES))
    got = b._scan_home_lines("废都南方工地", home_real, routes)
    _left = split_components(home_real, ROUTE_CODES)
    print(f"       现网 route_home.png：{n_before} 像素 → 可用回正线 {len(got)} 条"
          f"（扫描后图上剩 {len(_left)} 个连通域）")
    check("★ 扫出来的条数 == 扫描后剩下的连通域数（判废不漏、也不连坐）",
          len(got), len(_left))
    check("★ 扫出来的每一条都是合法回正线（终点贴主线 / 覆盖跨度够）",
          all(validate_home_line(c, home_real, routes, ROUTE_CODES, CFG)[0]
              for c in _left), True)
except FileNotFoundError as e:
    print(f"       ⚠️ 跳过：真实资源不可读（{e}）")

print("\n【24】★ 落点横段「三态分区」：跳跃区间 jump band（2026-09-14 加）")
# 背景（用户实测）：坑底回正线比上层主线**长**（主线 x 92~106，坑底 x 94~108）。
# 旧规则「汇聚到单拐角」只有拐角那一列能起跳，角色一帧走好几像素落不上去 →
# 横段两端来回震荡；偶尔蹭到时人还在横移 → 斜着跳出去。
# 新规则：x<band[0] 往右走 / band[0]<=x<=band[1] **原地跳** / x>band[1] 往左走。

HSEG = [(x, PIT_Y) for x in range(PIT_X0, PIT_X1 + 1)]      # x 94..108, y=128


def paint_hseg(jump_band=None, color_code=CC):
    '''在空图上跑一次 paint_converging_hseg，返回 RGB 图。'''
    bgr = cv2.cvtColor(blank(), cv2.COLOR_RGB2BGR)
    paint_converging_hseg(bgr, HSEG, (CORNER_X, PIT_Y), color_code, "walk",
                          jump_band)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def px(img, x, y=PIT_Y):
    return tuple(int(v) for v in img[y, x])


# ── ① jump_band=None → **逐像素等价于老行为**（最重要的一条回归）────────────
img_old = paint_hseg(None)
old_expected = []
for x in range(PIT_X0, PIT_X1 + 1):
    if x == CORNER_X:
        old_expected.append((0, 0, 0))                  # 拐角列：跳过，留给竖段
    else:
        # 老实现的**原始表达式**，一字不改地抄过来当期望值
        old_expected.append(pick_color((x, PIT_Y), (CORNER_X, PIT_Y), "walk", CC))
check("①jump_band=None：横段**逐像素**与老表达式一致（回归）",
      [px(img_old, x) for x in range(PIT_X0, PIT_X1 + 1)], old_expected)
check("①jump_band=None：拐角列仍然被**跳过**（留给竖段着 jump）",
      px(img_old, CORNER_X), (0, 0, 0))
check("①jump_band=None：拐角左边仍是蓝 right", px(img_old, 95), BLUE)
check("①jump_band=None：拐角右边仍是红 left", px(img_old, 104), RED)
# 单像素口径也要一致（手绘器画布预览走的就是这个函数）
# ⚠️ 拐角列在**单像素口径**里是 None（"不着色"），在图上表现为 (0,0,0)
_check_expected = [
    (None if x == CORNER_X
     else pick_color((x, PIT_Y), (CORNER_X, PIT_Y), "walk", CC))
    for x in range(PIT_X0, PIT_X1 + 1)]
check("①hseg_pixel_color(None) == 老 pick_color 表达式（拐角列 = None 不着色）",
      [hseg_pixel_color(x, PIT_Y, (CORNER_X, PIT_Y), CC, "walk", None)
       for x in range(PIT_X0, PIT_X1 + 1)], _check_expected)

# ── ② 三态分区：左段 right / 中间 jump / 右段 left ─────────────────────────
BAND = (98, 102)
img_band = paint_hseg(BAND)
check("②区间左边 (94~97) = 蓝 right（走向主线正下方）",
      [px(img_band, x) for x in range(94, 98)], [BLUE] * 4)
check("②区间内 (98~102) = 洋红「**原地跳**」（已在主线正下方）",
      [px(img_band, x) for x in range(98, 103)], [JUMP] * 5)
check("②区间右边 (103~108) = 红 left（走向主线正下方）",
      [px(img_band, x) for x in range(103, 109)], [RED] * 6)
check("②★ 区间跨度 = 5px（不是老行为只有 1 列能跳）",
      sum(1 for x in range(PIT_X0, PIT_X1 + 1) if px(img_band, x) == JUMP), 5)
check("②★ 与老行为的差集**只在区间内**（区间外一个像素都没变）",
      sorted(x for x in range(PIT_X0, PIT_X1 + 1)
             if px(img_band, x) != px(img_old, x)), [98, 99, 100, 101, 102])

# ── ③ 取色**必须走 pick_color**（不许硬编码 255,0,255）─────────────────────
CC_CUSTOM = dict(CC)
CC_CUSTOM[(7, 8, 9)] = "none none jump"          # 用户把「原地跳」改成 (7,8,9)
del CC_CUSTOM[(255, 0, 255)]
img_custom = paint_hseg(BAND, CC_CUSTOM)
check("③色码表改了 → 区间内跟着变成新色 (7,8,9)（**没有硬编码**）",
      px(img_custom, 100), (7, 8, 9))
check("③色码表改了 → 区间外仍然是蓝/红（走同一张表）",
      (px(img_custom, 95), px(img_custom, 105)), (BLUE, RED))

# ── ④ calc_jump_band：主线正下方的区间（真实地形 y=122 / x 92~106）──────────
main_bgr = cv2.cvtColor(make_mainline(), cv2.COLOR_RGB2BGR)
check("④主线 y=122、坑底 y=128、max_drop=8 → 够得着",
      calc_jump_band(main_bgr, PIT_Y, 8, 1, ROUTE_CODES), (93, 105))
check("④inset=0 → 不内缩（= 主线原样 x 92~106）",
      calc_jump_band(main_bgr, PIT_Y, 8, 0, ROUTE_CODES), (MAIN_X0, MAIN_X1))
check("④inset=2 → 两端各缩 2（94~104）",
      calc_jump_band(main_bgr, PIT_Y, 8, 2, ROUTE_CODES), (94, 104))
check("④inset=3 → 两端各缩 3（95~103）",
      calc_jump_band(main_bgr, PIT_Y, 8, 3, ROUTE_CODES), (95, 103))
# 落差 > max_drop 的主线**不算**（那是上一层平台，跳上去也够不着）
check("④max_drop=5 → 落差 6 的主线**够不着** → 回落 None",
      calc_jump_band(main_bgr, PIT_Y, 5, 1, ROUTE_CODES), None)
check("④max_drop=6 → 正好够得着（122 >= 128-6）",
      calc_jump_band(main_bgr, PIT_Y, 6, 1, ROUTE_CODES), (93, 105))
check("④max_drop=0 → 只看与坑底同一行 → 没有主线 → None",
      calc_jump_band(main_bgr, PIT_Y, 0, 0, ROUTE_CODES), None)

# ── ⑤ 区间退化 / 拿不到主线 → 一律 None（不崩，调用方回落老行为）───────────
narrow = make_mainline(xs=range(100, 102))            # 平台只有 2px 宽
narrow_bgr = cv2.cvtColor(narrow, cv2.COLOR_RGB2BGR)
check("⑤平台 2px + inset=1 → 缩完 x0 > x1 → None（不崩、不画区间）",
      calc_jump_band(narrow_bgr, PIT_Y, 8, 1, ROUTE_CODES), None)
check("⑤同一个平台 inset=0 → 还能给出 (100,101)",
      calc_jump_band(narrow_bgr, PIT_Y, 8, 0, ROUTE_CODES), (100, 101))
check("⑤没有主线图（None）→ None",
      calc_jump_band(None, PIT_Y, 8, 1, ROUTE_CODES), None)
check("⑤没有色码表 → None（认不出主线像素）",
      calc_jump_band(main_bgr, PIT_Y, 8, 1, None), None)
check("⑤空色码表 → None", calc_jump_band(main_bgr, PIT_Y, 8, 1, set()), None)
check("⑤主线是**全黑图**（一个像素都没有）→ None",
      calc_jump_band(cv2.cvtColor(blank(), cv2.COLOR_RGB2BGR), PIT_Y, 8, 1,
                     ROUTE_CODES), None)
check("⑤pit_y 在图外（> 图高）→ None（不抛异常）",
      calc_jump_band(main_bgr, 9999, 8, 1, ROUTE_CODES), None)
check("⑤非法 jump_band（x0 > x1）→ 归一化成 None（老行为）",
      hseg_pixel_color(100, PIT_Y, (CORNER_X, PIT_Y), CC, "walk", (105, 98)),
      pick_color((100, PIT_Y), (CORNER_X, PIT_Y), "walk", CC))
check("⑤paint_converging_hseg 收非法 band 不崩、按老行为画",
      [px(paint_hseg((105, 98)), x) for x in range(PIT_X0, PIT_X1 + 1)],
      old_expected)

# ── ⑥ 有多层主线时：只认**够得着**的那一层 ────────────────────────────────
multi = make_mainline()
for x in range(50, 61):                                 # 上层平台 y=118（落差 10）
    multi[118, x] = BLUE
multi_bgr = cv2.cvtColor(multi, cv2.COLOR_RGB2BGR)
check("⑥max_drop=8 → 落差 10 的上层平台**不算**（(93,105)）",
      calc_jump_band(multi_bgr, PIT_Y, 8, 1, ROUTE_CODES), (93, 105))
check("⑥max_drop=12 → 上层够得着了 → 区间向左扩到 x=51",
      calc_jump_band(multi_bgr, PIT_Y, 12, 1, ROUTE_CODES), (51, 105))

# ── ⑦ render_home_image 透传 jump_band ────────────────────────────────────
blob_band = {"points": [(PIT_X0, PIT_Y), (PIT_X1, PIT_Y),
                        (CORNER_X, PIT_Y), (CORNER_X, MAIN_Y)],
             "actions": ["walk", "walk", "jump"],
             "goal": (CORNER_X, MAIN_Y),
             "corner": (CORNER_X, PIT_Y), "walk_segs": 2,
             "jump_band": BAND}
out_band = cv2.cvtColor(
    render_home_image(np.zeros(SHAPE + (3,), dtype=np.uint8), [blob_band], CC),
    cv2.COLOR_BGR2RGB)
check("⑦render_home_image 给了 band → 区间内是**原地跳**",
      px(out_band, 100), JUMP)
check("⑦render_home_image 给了 band → 区间外是 right / left",
      (px(out_band, 95), px(out_band, 106)), (BLUE, RED))
blob_none = dict(blob_band, jump_band=None)
out_none = cv2.cvtColor(
    render_home_image(np.zeros(SHAPE + (3,), dtype=np.uint8), [blob_none], CC),
    cv2.COLOR_BGR2RGB)
check("⑦没给 band（字段缺省也行）→ 老行为：拐角左边蓝", px(out_none, 95), BLUE)
check("⑦没给 band → 横段上**一个 jump 像素都没有**（只有竖段那 6 个）",
      sum(1 for x in range(PIT_X0, PIT_X1 + 1) if px(out_none, x) == JUMP), 1)
blob_absent = {k: v for k, v in blob_band.items() if k != "jump_band"}
check("⑦blob 里**不带** jump_band 键 → 与显式 None 完全一致",
      int(cv2.cvtColor(render_home_image(
          np.zeros(SHAPE + (3,), dtype=np.uint8), [blob_absent], CC),
          cv2.COLOR_BGR2RGB).sum()),
      int(cv2.cvtColor(render_home_image(
          np.zeros(SHAPE + (3,), dtype=np.uint8), [blob_none], CC),
          cv2.COLOR_BGR2RGB).sum()))

# ── ⑧ 参数读取：缺键 / 非法值一律回落默认（与 home_params 同一条纪律）────────
check("⑧jump_band_params 默认 = DEFAULT_JUMP_BAND_PARAMS",
      (jump_band_params({})["home_jump_band_max_drop"],
       jump_band_params({})["home_jump_band_inset"]),
      (DEFAULT_JUMP_BAND_PARAMS["home_jump_band_max_drop"],
       DEFAULT_JUMP_BAND_PARAMS["home_jump_band_inset"]))
# ⚠️ 5，不是 1：inset=1 会让跳跃区几乎铺满落点横段（角色原地乱跳 = 已修的坏值）
check("⑧DEFAULT_JUMP_BAND_PARAMS 默认值就是 (8, 5)",
      (DEFAULT_JUMP_BAND_PARAMS["home_jump_band_max_drop"],
       DEFAULT_JUMP_BAND_PARAMS["home_jump_band_inset"]), (8, 5))
_jb = jump_band_params({"route": {"home_jump_band_max_drop": -3,
                                  "home_jump_band_inset": -1}})
check("⑧填负数 → 回落默认 8 / 5（不是『关掉』，也不是旧的 1）",
      (_jb["home_jump_band_max_drop"], _jb["home_jump_band_inset"]), (8, 5))
check("⑧字符串数字也能读（配置里可能是 '6'）",
      jump_band_params({"route": {"home_jump_band_max_drop": "6"}})
      ["home_jump_band_max_drop"], 6)
check("⑧★ 新键**不在** DEFAULT_HOME_PARAMS 里（别动「11 个键」那条断言）",
      [k for k in DEFAULT_JUMP_BAND_PARAMS if k in HOME_PARAMS], [])

print("\n【25】真实资源：废都南方工地 主线 y=122 / x 92~106 算出来的 band")
try:
    _img_map = load_image("tools/test_fixtures/废都南方工地/map.png")
    _bands = []
    for _n in ("route1.png", "route2.png"):
        _r = cv2.cvtColor(load_image(f"tools/test_fixtures/废都南方工地/{_n}"),
                          cv2.COLOR_BGR2RGB)
        _r = mask_route_colors(_img_map, _r, COLOR_CODE)
        _r = mask_route_colors(_img_map, _r, COLOR_CODE_UP_DOWN)
        _b = calc_jump_band(cv2.cvtColor(_r, cv2.COLOR_RGB2BGR), PIT_Y, 8, 1,
                            ROUTE_CODES)
        _bands.append(_b)
        print(f"       {_n}（y={PIT_Y}）→ band={_b}")
    _got = [b for b in _bands if b is not None]
    _union = ((min(b[0] for b in _got), max(b[1] for b in _got))
              if _got else None)
    print(f"       → 手绘器会用（多张主线取并集）：{_union}")
    check("★ 真实主线上算出的 band **落在主线 x 92~106 之内**",
          all(MAIN_X0 <= b[0] and b[1] <= MAIN_X1 for b in _got), True)
    check("★ band 非空（坑底 y=128 头顶 6px 就是平台 y=122）", _union is not None,
          True)
    _width = (_union[1] - _union[0] + 1) if _union else 0
    print(f"       → band 宽度 {_width}px（老行为只有拐角 1 列）")
    check("★ band 宽度 ≥ 5px（角色一帧走好几像素也落得进去）", _width >= 5, True)
except FileNotFoundError as e:
    print(f"       ⚠️ 跳过：真实资源不可读（{e}）")

print("\n【26】③ 回正入口可达性闸（2026-10-14）：线太远就别进（进去也是当帧空转）")
# 真机背景（MSBot_2026-10-14_16-51-02.log）：8 条 [回正·小结] 里 6 条是
#   「超时/接不上退出 | 0.0s/0帧 | 一次都没走到起跳点 | 离回正线 22~80px | 离主线 0px」
#   —— 入口放行、进去当帧「回正线接不上」退出 + 去武装，只剩 churn
#   （[回正] 2.2→14.2 次/分、[路线失联] 0.9→3.4 次/分）。
# 几何（对齐真机那一刻的口径差）：
#   * 主线 y=170 穿过人站的位置 ⇒ `_main_dist()` = 0px（真机小结里"离主线 0px"）；
#   * 当前段 `img_route` 是空图 ⇒ `get_nearest_color_code()` 一个像素都搜不到
#     （等价于真机被 E1 过滤掉之后"本帧没有可用的路线像素"）⇒ 只可能走"老兜底"入口；
#   * 插入点① 不会抢先：`_main_dist()`=0 时 `home_should_return(d_home, 0, 0)` 恒为 False
#     （主线优先），所以下面唯一在判的就是被 ③ 加闸的那条路。

def _gate_bot(player):
    _m = make_mainline(y=170)
    _h = blank()
    for _x, _y in [(99, 170), (100, 169), (101, 168), (102, 167)]:
        _h[_y, _x] = JUMP
    _h[167, 102] = GOAL
    _b = new_bot(player_global=player)
    _b.img_routes = [_m]
    _b.img_route_home = _h
    _b.img_route = blank()                 # 本帧当前段一个路线像素都搜不到
    _b._build_mainline_cache()
    _b.home_lines = [{
        "id": 1, "start": (99, 170), "goal": (102, 167), "span": 6, "has_jump": True,
        "n_pts": 4, "far_pts": [], "cover_xs": (99, 99), "cover_span": 1,
        "bbox": (99, 167, 102, 170),
        "xs": np.asarray([99, 100, 101, 102], dtype=np.int32),
        "ys": np.asarray([170, 169, 168, 167], dtype=np.int32),
        "armed": True, "disarm_t": 0.0,
    }]
    return _b


_bn = _gate_bot((99, 170))                 # 站在回正线端点 (99,170) 上
check("③ 近线：最近回正线像素 0px", _bn._home_dist()[0], 0)
check("③ 近线：离主线 0px（真机口径）", _bn._main_dist()[0], 0)
check("③ 近线：主线优先 ⇒ 插入点① 不放行（先堵掉另一条入口）",
      home_should_return(0, 0, 0), False)
with capture_log(level=logging.INFO) as _msgs_n:
    _bn.update_cmd_by_route()
check("③ 近线：0px ≤ 救援半径 40 ⇒ 照常进回正", _bn.is_using_home_route, True)
check("③ 近线：确实走的是「老兜底」那条路（留痕「走丢了」）",
      any("走丢了" in m for m in _msgs_n), True)

_bf = _gate_bot((40, 170))
check("③ 远线：最近回正线像素 59px（> 救援半径 40）", _bf._home_dist()[0], 59)
check("③ 远线：离主线 52px（比离回正线更近 ⇒ 插入点① 也不放行）",
      _bf._main_dist()[0], 52)
check("③ 远线：主线优先 ⇒ 插入点① 不放行", home_should_return(59, 52, 0), False)
with capture_log(level=logging.INFO) as _msgs_f:
    _bf.update_cmd_by_route()
check("★ ③ 远线：**不进去**（加闸前这里会进、然后当帧『接不上』退出 + 去武装）",
      _bf.is_using_home_route, False)
check("★ ③ 远线：留痕说清「够不着 ⇒ 不进去」",
      any("够不着" in m and "不进去" in m for m in _msgs_f), True)
check("③ 远线：不进回正也要把指令清干净（不能留着上一帧的方向键）",
      (_bf.cmd_move_x, _bf.cmd_move_y), ("none", "none"))

print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n in FAIL:
        print(f"   - {n}")
    sys.exit(1)
print("✅ 全部通过")
