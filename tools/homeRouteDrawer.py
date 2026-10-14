# -*- coding: utf-8 -*-
'''
手绘回正线面板 —— tools/homeRouteDrawer.py（v0.9 **第二版**）

给谁用：**非程序员**。角色掉进坑里回不来，又不想真的掉一次去录 ——
在这个面板上用鼠标点几下，就画出「落点区间 → 沿坑底走 → 跳回主线」的 L 形回正线。

为什么不用 cv2 窗口（设计 1.2 取舍 #4）：
    PRD 要「一排单选 + 已有线列表 + 单条删除 + 红字提示」，cv2 画这些是灾难；
    而且手绘**不需要抓帧、不需要游戏窗口**，跟主界面同进程反而省掉
    QProcess 双向管道那一整层。

与引擎的关系（设计 10.1「单一真源」）：
    本面板只写 minimaps/<图>/route_home.png。判废 / 覆盖 / 几何**现算**，
    全部走 src/utils/home_route.py 的同一份函数 —— 手绘器看到的结论，
    和引擎开跑时的结论，必须是同一条链路算出来的。

⚠️ v0.9 **第二版**改了什么（方案重构，2026-09-13）：
    1. **落点锚（127,0,127）整个删除** —— 不再往图上盖任何"触发像素"。
       引擎的进/退判定改成比「角色到回正线」和「角色到主线」两条距离。
    2. 画法改 **L 形**：横段沿坑底铺开（**逐像素汇聚着色**），竖段跳回主线。
       **禁止用 cv2.line 把整条横段画成一个颜色** —— 那样落在拐角另一侧的人
       会被推着走反、永远到不了拐角（Q9）。
    3. 第 1 步改成 **拖蓝带标落点区间**（按下-拖动-松开），实时显示
       「接住落点 N/M」；蓝带**只在内存 + 可选备注 json 里，绝不写进 PNG**。
    4. 去掉第一版「离主线太近就报错」—— 新方案下坑底离主线 6px 是**正常**的。

⚠️ 三条铁律（踩了就是静默故障）：
    1. 盖章顺序 **段 → goal**（goal 最后盖，压在段像素之上）
    2. 读写图片一律 load_image / imwrite_unicode（cv2.imread 遇中文路径静默返回 None）
    3. 坐标反算一律 round，**禁止 //**（4 倍图上 (403,300) 会算成 (100,75) 而不是 (101,75)）

用法：
    python -m tools.homeRouteDrawer --new_map 废都南方工地
'''
from __future__ import annotations

import argparse
import os
import re
import sys
import time

import cv2
import numpy as np

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QButtonGroup, QListWidget, QScrollArea, QMessageBox,
    QGroupBox, QComboBox, QLineEdit, QCheckBox,
)

from src.utils.logger import logger
from src.utils.common import (
    load_image, imwrite_unicode, mask_route_colors, route_size_mismatch,
    load_yaml, override_cfg,
    active_config_path,
)
from src.utils.paths import resource_path
from src.utils.home_route import (
    manhattan, color_code_maps, draw_params, home_params, code_dict_to_str,
    split_components, goal_codes, calc_line_geom, far_segment_stats,
    validate_home_line, home_pixel_clash, wipe_legacy_anchor,
    simulate_coverage, paint_converging_hseg, corner_index, dist_to_pixels,
    pick_color, render_home_image, screen_to_map,
    calc_jump_band, jump_band_params, hseg_pixel_color,
    load_home_notes, save_home_notes,
)


# ══════════════════════════════════════════════════════════════════════
# 常量 / 文案（全部中文，界面上不出现术语）
# ══════════════════════════════════════════════════════════════════════

#: 一排单选：**先选动作、再点下一个点**（PRD Q3，最不容易点错）
#: 前 4 个是"这一段怎么走"，正好对应 pick_color 的四个分支；
#: 第 5 个 **goal 是"这里就是终点"**，不是一种走法。
#:
#: ⚠️ 为什么终点必须有个**显式**按钮（team-lead 2026-09-13 拍板）：
#:    原来靠「最后 1 下点自动盖黄点」的隐式规则推断，用户画到一半以为画完了
#:    就去按保存 → 中间那个点被当成终点 → 存进去一条"体检过得去、但走完了
#:    不知道该停在哪"的线。这是**静默失败**，比存不进去糟得多。
ACTIONS = [
    ("walk", "走过去", "这一段正常走就行（左右走 / 上下走都算）"),
    ("jump", "跳一下", "这一段要跳 —— 掉进坑里跳回台子就选它"),
    ("up", "爬上去", "这一段是往上爬梯子 / 上绳子"),
    ("down", "下去", "这一段是往下爬梯子 / 下绳子"),
    ("goal", "🎯 回到主线（终点）",
     "**最后一下点这个** —— 点在**主线的彩色线**上，自动盖上黄色终点标记。\n"
     "（角色走到这里就算回到主线了。不点它，这条线走完了也不知道该停在哪。）\n"
     "⚠️ 到终点这一段按「走过去」画：跳/爬上来之后通常还要走两步才踩到主线，"
     "所以先点一个「跳一下」的中间点，再点这里。"),
]
DEFAULT_ACTION = "walk"
#: 「回到主线」这一段的走法固定为"走"（见上面 ACTIONS 的说明）
GOAL_SEGMENT_ACTION = "walk"

#: 怎么找落点（需求方**不开游戏**也能画的关键：坐标在引擎日志里）
HINT_FIND_LANDING = (
    "不知道落点在哪？**不用开着游戏画**：\n"
    "　① 先在游戏里正常挂机，故意掉下去一次；\n"
    "　② 打开 log 目录里最新的那个日志文件，搜「**角色位置**」；\n"
    "　③ 把后面那对数字（比如 (99, 128)）——或干脆把整行——粘到下面的框里，"
    "点【📍 把这点设为落点】。")

HINT_FIRST = (
    "①（可选）先勾上「框选落点区间」，在图上**按住左键拖一条蓝带**，"
    "把整个坑底框进去 —— 这样画完就知道接不接得住。\n"
    "② 再选「这一段怎么走」，在图上点：第 1 下 = **落点**（坑里 / 台子下面）。\n"
    "③ 中间几下点 = 往回走的路（走 / 跳 / 爬梯子）。\n"
    "④ **最后一下**一定要选「🎯 回到主线（终点）」，再点在**主线的彩色线**上。\n"
    "　 画法要点：**先沿坑底横着点一段（把坑底都盖上），再点「跳一下」跳回主线** —— "
    "只画一条竖线的话，角色掉在坑的左右两边会接不住。\n"
    + HINT_FIND_LANDING)
HINT_AFTER_FIRST = ("继续选动作、继续点；画到最后选「🎯 回到主线（终点）」再点最后一下。\n"
                    "点错了按【撤销上一点】（或在图上点右键）。")
HINT_NEED_GOAL = ("还差最后一步：选「🎯 回到主线（终点）」，再在**主线的彩色线**上点一下。\n"
                  "（不点终点的话，角色走完这条线也不知道该停在哪。）")
HINT_GOAL_DONE = ("终点已经点好了（黄点）✓ 直接点【保存这条回正线】就行；\n"
                  "要改就先【撤销上一点】。")


# ══════════════════════════════════════════════════════════════════════
# 纯函数（不碰 Qt，离线自检可以直接调 —— "测的就是跑的"）
# ══════════════════════════════════════════════════════════════════════

def _color_mask(img_rgb, codes):
    '''图上命中 codes（RGB 元组集合/字典）的布尔掩膜。'''
    code_set = {tuple(int(v) for v in c) for c in (codes or [])}
    if img_rgb is None or not code_set:
        return np.zeros((0, 0), dtype=bool)
    arr = img_rgb[:, :, :3].astype(np.int32)
    mask = np.zeros(arr.shape[:2], dtype=bool)
    for c in code_set:
        mask |= np.all(arr == np.array(c, dtype=np.int32), axis=-1)
    return mask


def _rgb_at(img_rgb, pt):
    '''取 img_rgb 上 pt 处的 RGB 元组（越界返回 None）。'''
    if img_rgb is None or pt is None:
        return None
    y, x = int(pt[1]), int(pt[0])
    if y < 0 or x < 0 or y >= img_rgb.shape[0] or x >= img_rgb.shape[1]:
        return None
    return tuple(int(v) for v in img_rgb[y, x][:3])


def load_merged_cfg(user_cfg=None) -> dict:
    '''取完整配置：默认配置 + 用户配置方案（缺键一律走 home_route 的回落默认）。'''
    cfg = load_yaml("config/config_default.yaml")
    if user_cfg is not None:
        return override_cfg(cfg, user_cfg)
    custom = active_config_path()
    if custom:
        try:
            return override_cfg(cfg, load_yaml(custom))
        except Exception as e:      # 用户配置坏了也不能拦住手绘
            logger.warning(f"[手绘回正线] 用户配置读失败，用默认配置：{e}")
    return cfg


def mask_route_all(img_map_bgr, img_rgb, cfg):
    '''按引擎同一条链路做 **mask 两连**（color_code / up_down）。

    ⚠️ v0.9 第二版：**没有第三次 mask** 了 —— 落点锚那第三张色码表已删除。
    ⚠️ 少 mask 任何一次，界面上看到的像素就和引擎实际用的不是一回事。
    '''
    cc, cc_ud = color_code_maps(cfg)
    out = img_rgb
    out = mask_route_colors(img_map_bgr, out, code_dict_to_str(cc))
    out = mask_route_colors(img_map_bgr, out, code_dict_to_str(cc_ud))
    return out


def audit_components(img_rgb, route_imgs_rgb, cfg):
    '''列出图上**每一条**回正线，逐条给出几何 + 覆盖跨度 + 体检结论。

    与引擎 `_scan_home_lines` 用的是同一批 home_route 函数 —— 界面列表、
    保存后的报告、保存前的拦截，三处口径完全一致。

    Returns:
        list[dict]: 每条一个 dict，字段：
            id / start / goal / pts / span / cover_span / cover_xs /
            has_jump / n_pts / ok / why
    '''
    cc, cc_ud = color_code_maps(cfg)
    route_codes = set(cc) | set(cc_ud)
    comps = split_components(img_rgb, route_codes)

    out = []
    for i, comp in enumerate(comps, 1):
        geom = calc_line_geom(comp, route_imgs_rgb, route_codes, cfg,
                              img_rgb=img_rgb)
        far = far_segment_stats(comp, route_imgs_rgb, route_codes, cfg)
        ok, why = validate_home_line(comp, img_rgb, route_imgs_rgb, route_codes,
                                     cfg)
        out.append({
            "id": i,
            "start": geom["start"],
            "goal": geom["goal"],
            "pts": comp,
            "span": geom["span"],
            "cover_xs": far["cover_xs"],
            "cover_span": far["cover_span"],
            "has_jump": geom["has_jump"],
            "n_pts": geom["n_pts"],
            "ok": ok,
            "why": why,
        })
    return out


def verify_saved_image(path, img_map_bgr, cfg, goal_pt, goal_radius=1):
    '''**落盘回放校验**：写完 PNG 再按引擎同一条链路读回来，确认像素没被吃掉。

    为什么必须有这一步（设计 1.3 难点 3）：
        mask_route_colors 会把「地图底图里出现同色」的位置在路线图上涂黑。
        一撞色就有一批回正线像素凭空消失，而"消失"在运行时是静默的
        （只是这条线再也接不住人 / 走不到头）。所以每次保存都验一次。

    ⚠️ v0.9 第二版改成数**回正线像素总数**（第一版只数锚点像素 —— 锚点没了，
       被吃掉的可能是横段/竖段上任何一个像素，只数锚点等于没自检）。

    Returns:
        dict: {
          "ok": bool,             # 段像素没少 + 终点还活着
          "goal_alive": bool,
          "eaten": [(x, y), ...], # 被地图底色吃掉的回正线像素
          "n_before": int, "n_after": int,
          "img_rgb": ndarray,     # mask 后的 RGB 图（保存后报告就用它，口径与引擎一致）
          "reason": str,          # 人话原因（ok=True 时为空串）
        }
    '''
    res = {"ok": False, "goal_alive": False, "eaten": [], "n_before": 0,
           "n_after": 0, "img_rgb": None, "reason": ""}
    cc, cc_ud = color_code_maps(cfg)
    route_codes = set(cc) | set(cc_ud)

    raw = cv2.cvtColor(load_image(path), cv2.COLOR_BGR2RGB)
    res["n_before"] = len([1 for _ in _iter_code_pixels(raw, route_codes)])
    img = mask_route_all(img_map_bgr, raw.copy(), cfg)
    res["eaten"] = home_pixel_clash(raw, img, route_codes)
    res["img_rgb"] = img
    res["n_after"] = len([1 for _ in _iter_code_pixels(img, route_codes)])

    # 终点：盖的是半径 r 的圆点，中心被吃掉时邻近像素可能还在 → 就近找一圈
    g_set = goal_codes(cc)
    if goal_pt is not None:
        gx, gy = int(goal_pt[0]), int(goal_pt[1])
        r = max(1, int(goal_radius))
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if _rgb_at(img, (gx + dx, gy + dy)) in g_set:
                    res["goal_alive"] = True
                    break
            if res["goal_alive"]:
                break

    if not res["eaten"] and res["goal_alive"]:
        res["ok"] = True
        return res

    if res["eaten"]:
        res["reason"] = (
            f"这条线有 {len(res['eaten'])} 个像素被**地图底色吃掉了**"
            f"（{res['eaten'][:8]}{' …' if len(res['eaten']) > 8 else ''}）——\n"
            f"这张地图的底图（map.png）里恰好有回正线用到的那个颜色。\n\n"
            f"怎么办：把这条线往旁边挪 1~2 像素重画；老是这样就换一个"
            f"底图里没出现过的颜色再画。")
    else:
        res["reason"] = (
            f"保存后读回来，终点 {goal_pt} 上的黄色标记不见了 —— 多半是被地图底色吃掉了。\n\n"
            f"怎么办：把最后那个点挪 1~2 像素重画（终点要落在主线的彩色线上）。")
    return res


def _iter_code_pixels(img_rgb, codes):
    '''图上命中 codes 的像素（yield (x, y)）；给"数像素"用，不建大列表。'''
    if img_rgb is None:
        return
    for y in range(img_rgb.shape[0]):
        for x in range(img_rgb.shape[1]):
            if _rgb_at(img_rgb, (x, y)) in codes:
                yield (x, y)


#: 图外坐标：传给 stamp_goal 时它会因越界直接 return，等于"这次不盖章"
_OFF_IMAGE = (-1, -1)


def make_blob(points, actions, goal_set, jump_band=None):
    '''把「点 + 段动作 + 有没有点过终点」打包成 render_home_image 要的 blob。

    ⚠️ 终点**只**在 goal_set=True 时才盖在 pts[-1] 上；没点过就给图外坐标
       （stamp_goal 遇越界直接 return）—— 绝不拿"最后一下点"兜底，
       那正是被废止的隐式规则。

    ★ v0.9 第二版：额外带上 `corner` / `walk_segs`，让**落点横段逐像素汇聚着色**
       （左半 right、右半 left，都指向拐角）—— 见 render_home_image 的说明。

    ★ 2026-09-14 三态分区：再带上 `jump_band`（主线正下方的 x 区间，由
      calc_jump_band 算出来）。落点横段在区间内的像素会着成「**原地跳**」，
      角色一进区间就能起跳上平台，不必再精确落到拐角那一列（老行为的震荡 bug）。
      **不给 / 给 None → 逐像素等价于老行为**，一个字节都不变。

    Args:
        points: [(x, y), ...]，第 1 个是落点
        actions: 段动作，len = len(points) - 1（最后一段由 GOAL_SEGMENT_ACTION 决定）
        goal_set: 用户有没有**显式**选了「回到主线（终点）」再点
        jump_band: (x0, x1) | None —— 「主线正下方」的 x 区间（见 calc_jump_band）

    Returns:
        dict: render_home_image 的 blob
    '''
    pts = list(points)
    ci = corner_index(pts, actions) if pts else 0
    return {
        "points": pts,
        "actions": list(actions),
        "goal": pts[-1] if (goal_set and pts) else _OFF_IMAGE,
        "corner": (pts[ci] if pts else None),
        "walk_segs": int(ci),
        "jump_band": jump_band,
    }


#: 画布网格间隔（**原图**像素）—— 20px 一条暗灰细线，方便拿日志坐标肉眼核对
GRID_STEP = 20
#: 网格线 / 刻度数字的颜色（RGB；暗灰，且路线像素所在位置一律跳过，不挡路）
GRID_RGB = (58, 58, 58)
GRID_LABEL_RGB = (125, 125, 125)

#: 日志时间戳（先砍掉，否则 "2026-09-13 02:16:54" 里的 "13 02" 会被当成坐标）
_LOG_TS_RE = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]?\s*")
#: 坐标识别的三道正则，从严格到宽松（取**第一对**能配上的整数）
#:   ① 括号包着的一对：(98, 130) / [98,130] / （98，130）
#:   ② 有分隔符的一对：98,130 / 98，130 / 98x130
#:   ③ 空格隔开的一对：98 130
_COORD_RES = (
    re.compile(r"[\(\[（]\s*(-?\d+)\s*[,，、\s]\s*(-?\d+)\s*[\)\]）]"),
    re.compile(r"(-?\d+)\s*[,，、xX×*]\s*(-?\d+)"),
    re.compile(r"(-?\d+)\s+(-?\d+)"),
)


def parse_coord_text(text):
    '''从用户粘贴的任意文本里抠出坐标 (x, y)；抠不出来返回 None。

    **为什么要这么宽容**：需求方是照着日志抄坐标的，而他大概率会整行复制：
        `[回正] 角色位置 (99, 128) 附近找不到回正线像素`
    让他自己把 99 / 128 挑出来填两个框，太容易填错（而且错了不报错，
    落点直接画到别处）。所以这里直接吃整行，抠出第一对整数。

    也兼容手敲：`99,128` / `99 128` / `[99,128]` / `（99，128）`。
    '''
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    s = _LOG_TS_RE.sub("", s)
    for rx in _COORD_RES:
        m = rx.search(s)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None


def coord_error(x, y, w, h) -> str:
    '''坐标能不能用；返回人话原因（空串 = 能用）。

    ⚠️ 越界必须给中文提示而不是崩：日志里同一个文件有好多行，
       需求方抄错行（比如抄到帧号、时间戳）是常态。
    '''
    if x is None or y is None:
        return ("没看懂这个坐标 —— 请填两个数字（X 和 Y），"
                "或者把日志里带「角色位置」的那一整行粘进来。")
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return ("坐标只能是数字 —— 请填两个数字（X 和 Y），"
                "或者把日志里带「角色位置」的那一整行粘进来。")
    if not (0 <= x < int(w)) or not (0 <= y < int(h)):
        return (f"坐标 ({x}, {y}) 超出这张图的范围了 —— "
                f"这张图只能填 X 0~{int(w) - 1}、Y 0~{int(h) - 1}。\n"
                f"（多半是抄错行了：要找日志里「角色位置」后面那一对数字。）")
    return ""


def trim_actions(points, actions) -> list:
    '''把段动作裁到「点数 - 1」条（撤销一个点之后配套调用）。

    ⚠️ max(0, ...) 不能省：点被撤到 0 个时 `点数 - 1 = -1`，
       少了它就会 `pop from empty list` 抛 IndexError ——
       而「只点了 1 个点就右键撤销」是用户一键就能触发的。
    '''
    out = list(actions)
    while len(out) > max(0, len(points) - 1):
        out.pop()
    return out


def band_to_pts(p0, p1):
    '''蓝带两端点 → 落点区间里的**全部像素**（矩形内的每个整数点）。

    ⚠️ 落点区间是"角色可能掉下去的一整片地方"，不是一条线 —— 所以按**矩形**取，
       这样 15px 宽的坑底两端都算得到（只取两个端点会漏掉中间与两端）。
    '''
    if p0 is None or p1 is None:
        return []
    x0, x1 = sorted((int(p0[0]), int(p1[0])))
    y0, y1 = sorted((int(p0[1]), int(p1[1])))
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def save_gate_error(points, goal_set, has_mainline) -> str:
    '''保存前的**硬门槛**：返回人话原因（空串 = 可以存）。

    为什么单独抽成纯函数：门槛的判定只有一份，面板和自检用的是同一个，
    而且它不碰 Qt —— 离线就能验「缺终点时一定拦下」这件事。
    '''
    if len(points) < 2:
        return ("还没画完 —— 至少点两下：\n"
                "第 1 下 = 落点（角色掉下去站的位置），"
                "最后 1 下 = 选「🎯 回到主线（终点）」后再点，落在主线的彩色线上。")
    if not goal_set:
        return ("还没点「回到主线」—— 这条线没有终点（黄色标记），"
                "角色走完这条线也不知道该停在哪，等于没画完。"
                "选「🎯 回到主线（终点）」，再在主线的彩色线上点一下。")
    if not has_mainline:
        return ("这张图没有可用的主线，量不出回正线离主线多远 —— "
                "请先录一段主线，再画回正线。")
    return ""


def coverage_text(cov) -> str:
    '''覆盖率结果 → 人话（"接住落点 15/15 ✓"）。

    ⚠️ v0.9 第二版把第一版的「离主线 D px / 触发半径 R px」换成这个：
       新方案下"离主线 6px"是**正常**的（坑底本来就贴着平台），
       用户真正需要知道的是「我框的这排落点，这条线接不接得住」。
    '''
    if not cov or not cov.get("total"):
        return "还没框落点区间 —— 勾上「框选落点区间」再在图上拖一条蓝带。"
    total, covered = int(cov["total"]), int(cov["covered"])
    if covered >= total:
        return f"接住落点 {covered}/{total} ✓（框进来的落点全都接得住）"
    miss = cov.get("missed") or []
    xs = sorted({int(p[0]) for p in miss})
    tail = f"，够不着的是 x={xs[0]}" + (f"~{xs[-1]}" if len(xs) > 1 else "") if xs else ""
    return (f"接住落点 {covered}/{total} —— 还差 {total - covered} 个{tail}。\n"
            f"把横段画长一点（沿着坑底铺满蓝带覆盖的范围）再接一次。")


def jump_off_platform(pts, actions, move_pts, all_pts=None, y_tol=3):
    '''竖段（跳/爬回主线的那一段）的落点是不是落在**平台 x 范围之外**。

    为什么需要它（QA 发现、team-lead 确认的盲区）：
        判据⑤「终点必须贴主线」只看 goal **像素集到最近主线像素的 min 距离**。
        goal 是半径 1~2 的圆（手绘 5px / 录制 13px），所以竖段画在 x=107
        （平台只到 106）时，圆会碰到平台边缘 (106,122) → min ≤ tol →
        **判合格放行**；可角色跳到 (107,122) 是**平台外面**（那里没地板）
        → 落空 → 8s 空转后超时。判据本身区分不了（圆必然碰到平台边缘）。

    ★ 平台 x 范围 = **主线里除 goal 外**的色码像素的 x 范围
      ∪ **goal 圆的中心 x**：
        · 只看移动像素 → 圆吃掉了平台边缘几个像素（实测 route1.png：
          平台到 106、移动像素只到 101）→ 平台边缘会被误判成"平台外"；
        · 只看全部像素 → 圆又往平台外多撑 2px（撑到 108）→ x=107 被误判成
          "平台内"，**盲区原样保留**；
        · 圆的**中心**就是录制时那个**真实平台点**（圆是对称的）→ 加上它才对。

    ⚠️ 只警告、**不阻断保存** —— 这张图可能有别的平台（终点落在另一条主线的
       x 范围里是合法的），而且这只是提醒"这一条可能落空"。

    Args:
        pts: 已点出来的点 [(x, y), ...]
        actions: 段动作（len = len(pts) - 1）
        move_pts: 主线里除 goal 外的全部像素 [(x, y), ...]
        all_pts: 主线**全部**像素（含 goal），用来补 goal 圆的中心；None 则只按移动像素判
        y_tol: "同一个高度"的容差（只看终点 y 附近的主线像素）

    Returns:
        dict | None: 落在平台外 → {'goal','x0','x1','side','msg'}；否则 None
    '''
    pts = list(pts or [])
    if len(pts) < 2:
        return None
    acts = list(actions or [])
    # 还没画竖直段（全是"走"）→ 谈不上"跳上去落空"，不提示
    if not acts or all(str(a) == "walk" for a in acts):
        return None
    gx, gy = int(pts[-1][0]), int(pts[-1][1])
    y_tol = int(y_tol)

    xs = [int(p[0]) for p in (move_pts or []) if abs(int(p[1]) - gy) <= y_tol]
    if all_pts:
        mv_set = {(int(p[0]), int(p[1])) for p in (move_pts or [])}
        g_near = [(int(p[0]), int(p[1])) for p in all_pts
                  if abs(int(p[1]) - gy) <= y_tol and (int(p[0]), int(p[1])) not in mv_set]
        if g_near:
            gx0, gx1 = min(p[0] for p in g_near), max(p[0] for p in g_near)
            # 同一高度挤了好几个 goal 圆（跨度 > 6px）时定不出中心 → 宁可不加
            if gx1 - gx0 <= 6:
                xs.append((gx0 + gx1) // 2)
    if not xs:
        return None                      # 这个高度没有主线像素 → 交给判据⑤ 去管
    x0, x1 = min(xs), max(xs)
    if x0 <= gx <= x1:
        return None                      # 终点 x 落在平台范围内 → 跳上去踩得着
    side = "左" if gx < x0 else "右"
    return {
        "goal": (gx, gy), "x0": x0, "x1": x1, "side": side,
        "msg": (f"竖段画到平台外面了（终点 x={gx} 在平台 x{x0}~{x1} 的{side}边）"
                f" —— 角色跳上去会落空，只能干等 8s 超时。"
                f"请把竖段画在平台正上方（终点 x 落在 {x0}~{x1} 里）。"),
    }


def line_summary(line) -> str:
    '''一条线 → 列表里的一行中文。'''
    s = line.get("start") or (-1, -1)
    g = line.get("goal") or (-1, -1)
    cov = line.get("cover_xs")
    cov_txt = f"落点覆盖 x{cov[0]}~{cov[1]}" if cov else "落点覆盖 未知"
    txt = (f"#{line['id']}  起点({s[0]},{s[1]}) → 终点({g[0]},{g[1]})"
           f" · 跨度 {line['span']}px · {cov_txt}")
    if line.get("has_jump"):
        txt += " · 含跳跃"
    if not line.get("ok"):
        txt += "  ✗ 这条用不了"
    return txt


# ══════════════════════════════════════════════════════════════════════
# 画布（放大后的小地图）
# ══════════════════════════════════════════════════════════════════════

class _CanvasLabel(QLabel):
    '''放大的小地图画布。

    只做一件事：把鼠标坐标原样上报。**不做任何滚动补偿** —— 画布是
    QScrollArea 里的**满尺寸** QLabel，label 的 (0,0) 就是图的 (0,0)，
    滚动偏移由 Qt 自动处理，我们拿到的 position() 已经是画布坐标。

    ⚠️ v0.9 第二版多上报两件事：**松开** 和 **按住的是哪个键** ——
       落点区间是"按下-拖动-松开"画出来的（设计 6.1）。
    '''

    def __init__(self, on_press, on_move, on_right, on_release=None, parent=None):
        super().__init__(parent)
        self._on_press = on_press
        self._on_move = on_move
        self._on_right = on_right
        self._on_release = on_release
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

    def mousePressEvent(self, ev):
        x, y = int(ev.position().x()), int(ev.position().y())
        if ev.button() == Qt.LeftButton:
            self._on_press(x, y)
        elif ev.button() == Qt.RightButton:
            self._on_right(x, y)
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        x, y = int(ev.position().x()), int(ev.position().y())
        self._on_move(x, y, ev.buttons())
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        x, y = int(ev.position().x()), int(ev.position().y())
        if ev.button() == Qt.LeftButton and self._on_release is not None:
            self._on_release(x, y)
        else:
            super().mouseReleaseEvent(ev)


# ══════════════════════════════════════════════════════════════════════
# 主面板
# ══════════════════════════════════════════════════════════════════════

class HomeRouteDrawer(QDialog):
    '''手绘回正线面板（PySide6 QDialog，与主界面同进程）。

    设计第 6 节要点：4 倍放大画布 / 先选动作再点下一个点 / **落点区间蓝带** /
    实时「接住落点 N/M」/ 撤销·清空·单条删除 / 保存后按引擎同一条链路回读校验 /
    一张图支持多条 / 横段逐像素汇聚着色。
    '''

    def __init__(self, map_name, cfg=None, parent=None):
        super().__init__(parent)
        self.map_name = str(map_name or "")
        self.cfg = load_merged_cfg(cfg)
        self.dp = draw_params(self.cfg)
        self.hp = home_params(self.cfg)
        # ⚠️ 第二版只有**两张**色码表（落点锚那张已删）
        self.cc, self.cc_ud = color_code_maps(self.cfg)
        #: pick_color 要查 up/down 那两个色，所以传**合并后**的表
        self.merged_cc = dict(self.cc)
        self.merged_cc.update(self.cc_ud)
        self.route_codes = set(self.cc) | set(self.cc_ud)

        self.map_dir = resource_path(os.path.join("minimaps", self.map_name))
        self.path_home = os.path.join(self.map_dir, "route_home.png")

        self.img_map_bgr = None      # 地图底图（BGR）
        self.route_imgs_rgb = []     # 主线 RGB（已 mask）—— 量覆盖 / 几何的地基
        self.img_out_bgr = None      # route_home.png 的**原始**内容（BGR），落盘真源
        self.home_rgb = None         # img_out_bgr 的 RGB 视图（审计用）
        self.base_rgb = None         # 底图合成：地图 + 主线 + 已存回正线
        self.main_pts = []           # 主线全部像素（算覆盖用）
        self.main_move_pts = []      # 主线里**除 goal 外**的像素（判"竖段在不在平台上方"用）
        self._qimg_buf = None        # 持有 ndarray，否则 QImage 的 buffer 会被回收

        self.cur_points = []         # 本次正在画的点 [(x, y), ...]
        self.cur_actions = []        # 段动作，len = len(points) - 1
        self.goal_set = False        # 有没有**显式**点过「回到主线（终点）」
        self.landing_seg = None      # 落点区间 ((x0,y0),(x1,y1))；None = 没框
        self._dragging = False       # 正在拖蓝带
        self._drag_from = None
        self._band_cache = {}        # 跳跃区间缓存：pit_y → (x0, x1) | None（主线不变就不重算）
        self.lines = []              # 已有线（audit_components 的结果）
        self.scale = int(self.dp["home_draw_scale"])
        self.saved_any = False       # 本次会话是否保存成功过（给主界面打日志用）
        self.last_msg = ""           # 最后一次保存/失败的中文结论

        self.setWindowTitle(f"手绘回正线 — {self.map_name}")
        self.setModal(True)
        self.resize(1180, 920)
        self._build_ui()
        if not self._load_map():
            return
        self._auto_fit_scale()
        self._rebuild_base()
        self._refresh_lines()
        self._rebuild_view()

    # ── 界面骨架 ────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        self.lbl_title = QLabel(f"🖊 手绘回正线 — {self.map_name}")
        self.lbl_title.setStyleSheet("font-size: 15px; font-weight: bold;")
        head.addWidget(self.lbl_title)
        head.addStretch(1)
        head.addWidget(QLabel("放大倍数："))
        self.cmb_scale = QComboBox()
        self.cmb_scale.addItems([f"{i} 倍" for i in range(
            int(self.dp["home_draw_scale_min"]), int(self.dp["home_draw_scale_max"]) + 1)])
        self.cmb_scale.setCurrentText(f"{self.scale} 倍")
        self.cmb_scale.setToolTip(
            "小地图只有 268×187，放大后才点得准。\n"
            "放大 4 倍时，鼠标差 1 个屏幕像素 = 地图上的 0.25 像素。")
        self.cmb_scale.currentTextChanged.connect(self._on_scale_changed)
        head.addWidget(self.cmb_scale)
        root.addLayout(head)

        # ── ① 落点区间（蓝带）────────────────────────────────────────
        gbox_band = QGroupBox("① 先框出「会掉下去的那一片」（推荐，也可以跳过）")
        lay_band = QVBoxLayout()
        self.chk_band = QCheckBox("框选落点区间（勾上后：在图上**按住左键拖**出一条蓝带）")
        self.chk_band.setToolTip(
            "把角色可能掉下去的地方整片框进来（比如整个坑底）。\n"
            "框好之后，下面会实时告诉你这条线接得住几个落点。\n"
            "蓝带只是给你看的，**不会画进 route_home.png**。")
        self.chk_band.toggled.connect(self._on_band_toggled)
        lay_band.addWidget(self.chk_band)
        self.lbl_band = QLabel("落点区间：还没框（跳过也行，只是看不到「接住几个」）")
        self.lbl_band.setWordWrap(True)
        self.lbl_band.setStyleSheet("color: #666;")
        lay_band.addWidget(self.lbl_band)
        gbox_band.setLayout(lay_band)
        root.addWidget(gbox_band)

        # ── ② 动作单选排 ─────────────────────────────────────────────
        gbox_act = QGroupBox("② 这一段怎么走（先选动作、再点下一个点）：")
        row_act = QHBoxLayout()
        self.btn_group = QButtonGroup(self)
        self.radio_actions = {}
        for key, text, tip in ACTIONS:
            rb = QRadioButton(text)
            rb.setToolTip(tip)
            self.btn_group.addButton(rb)
            self.radio_actions[key] = rb
            row_act.addWidget(rb)
        self.radio_actions[DEFAULT_ACTION].setChecked(True)
        row_act.addStretch(1)
        gbox_act.setLayout(row_act)
        root.addWidget(gbox_act)

        self.lbl_how = QLabel(HINT_FIRST)
        self.lbl_how.setWordWrap(True)
        self.lbl_how.setStyleSheet("color: #555;")
        root.addWidget(self.lbl_how)

        # ── 落点坐标输入（不开游戏也能画：坐标从引擎日志里抄）────────────────
        row_coord = QHBoxLayout()
        row_coord.addWidget(QLabel("落点坐标："))
        self.edit_x = QLineEdit()
        self.edit_x.setPlaceholderText("X")
        self.edit_x.setFixedWidth(90)
        self.edit_x.setToolTip(
            "可以直接把日志里那一整行粘进来（会自动认出坐标），也可以只填一个数字。")
        self.edit_y = QLineEdit()
        self.edit_y.setPlaceholderText("Y")
        self.edit_y.setFixedWidth(90)
        self.edit_y.setToolTip("同上；两个框里随便哪个粘整行都行。")
        self.edit_x.textChanged.connect(self._on_coord_text_changed)
        self.edit_y.textChanged.connect(self._on_coord_text_changed)
        row_coord.addWidget(self.edit_x)
        row_coord.addWidget(QLabel(","))
        row_coord.addWidget(self.edit_y)
        self.btn_set_landing = QPushButton("📍 把这点设为落点")
        self.btn_set_landing.setToolTip(
            "把上面这个坐标设成落点（角色掉下去站的位置）。\n"
            "和直接在图上点第 1 下**完全一样**，只是不用靠眼睛找。")
        self.btn_set_landing.clicked.connect(self._set_landing_from_input)
        row_coord.addWidget(self.btn_set_landing)
        self.lbl_mouse = QLabel("鼠标：（—, —）")
        self.lbl_mouse.setStyleSheet("color: #666; font-family: Consolas, monospace;")
        row_coord.addStretch(1)
        row_coord.addWidget(self.lbl_mouse)
        root.addLayout(row_coord)

        # 画布（QScrollArea + 满尺寸 QLabel）
        self.canvas = _CanvasLabel(self._on_canvas_click, self._on_canvas_move,
                                   self._on_canvas_right, self._on_canvas_release)
        scroll = QScrollArea()
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll = scroll
        root.addWidget(scroll, 1)

        # 实时提示（两行：画到哪了 / 这条线体检结论）
        self.lbl_drawn = QLabel("还没点 —— 先在图上点第 1 下（落点）")
        self.lbl_stat = QLabel("")
        self.lbl_stat.setWordWrap(True)
        root.addWidget(self.lbl_drawn)
        root.addWidget(self.lbl_stat)

        # 已有回正线列表 + 删除
        gbox_lines = QGroupBox("这张图已经画好的回正线")
        lay_lines = QVBoxLayout()
        self.list_lines = QListWidget()
        self.list_lines.setMaximumHeight(110)
        lay_lines.addWidget(self.list_lines)
        row_del = QHBoxLayout()
        row_del.addWidget(QLabel("给这条起个名字（可不填，只是方便你认）："))
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("比如：左边坑 / 右边台子下面")
        row_del.addWidget(self.edit_name, 1)
        self.btn_delete = QPushButton("🗑️ 删掉选中的这条")
        self.btn_delete.setToolTip("只删这一条，其它回正线不受影响。")
        self.btn_delete.clicked.connect(self._delete_selected)
        row_del.addWidget(self.btn_delete)
        lay_lines.addLayout(row_del)
        gbox_lines.setLayout(lay_lines)
        root.addWidget(gbox_lines)

        # 操作按钮
        row_btn = QHBoxLayout()
        self.btn_undo = QPushButton("↩ 撤销上一点")
        self.btn_undo.setToolTip("或在图上点鼠标右键")
        self.btn_undo.clicked.connect(self._undo_point)
        self.btn_clear = QPushButton("🧹 清空重画")
        self.btn_clear.clicked.connect(self._clear_points)
        row_btn.addWidget(self.btn_undo)
        row_btn.addWidget(self.btn_clear)
        row_btn.addStretch(1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        self.btn_save = QPushButton("💾 保存这条回正线")
        self.btn_save.setDefault(True)
        self.btn_save.setStyleSheet("font-weight: bold;")
        self.btn_save.clicked.connect(self._save_line)
        row_btn.addWidget(self.btn_close)
        row_btn.addWidget(self.btn_save)
        root.addLayout(row_btn)

    # ── 载入地图 / 主线 / 已有回正线 ─────────────────────────────────
    def _load_map(self) -> bool:
        '''读底图 + 主线 + 已有回正线。读不动就弹中文提示并关闭。'''
        path_map = os.path.join(self.map_dir, "map.png")
        if not os.path.exists(path_map):
            QMessageBox.warning(
                self, "这张图还没存地图",
                f"找不到：\n  {path_map}\n\n"
                f"手绘回正线要拿这张地图的小地图当底图才画得了。\n"
                f"先在界面上【录制挂机路线】把这张图录一遍（会自动存下 map.png），"
                f"再来画回正线。")
            QApplication.processEvents()
            self.reject()
            return False
        try:
            self.img_map_bgr = load_image(path_map, cv2.IMREAD_COLOR)
        except Exception as e:
            QMessageBox.critical(self, "地图读不出来", f"{path_map}\n\n{e}")
            self.reject()
            return False
        self.h, self.w = self.img_map_bgr.shape[:2]

        # 主线（排除 route_home / route_rest，跟引擎同一套过滤）
        self.route_imgs_rgb = []
        try:
            names = sorted(f for f in os.listdir(self.map_dir)
                           if f.startswith("route") and f.endswith(".png"))
        except OSError:
            names = []
        # 尺寸与 map.png 不一致的主线图（另一次录制的遗留）在这里被挑出来
        self.skipped_routes = []
        for f in names:
            if f in ("route_home.png", "route_rest.png"):
                continue
            try:
                img = cv2.cvtColor(load_image(os.path.join(self.map_dir, f)),
                                   cv2.COLOR_BGR2RGB)
            except Exception as e:
                logger.warning(f"[手绘回正线] 主线 {f} 读失败，跳过：{e}")
                continue
            # ⚠️ 必须与 map.png 同尺寸（同坐标系）。不跳过的话下面 _rebuild_base
            #    会拿 268 宽的掩码去盖 344 宽的底图 →
            #    IndexError: boolean index did not match ... 344 but ... 268
            #    （2026-10-14 真机：用户的地图目录里混着两次录制的文件）。
            if route_size_mismatch(img.shape, self.img_map_bgr.shape):
                self.skipped_routes.append((f, img.shape[:2]))
                logger.warning(
                    f"[手绘回正线] 主线 {f} 尺寸 {img.shape[:2]} 与 map.png "
                    f"{self.img_map_bgr.shape[:2]} 不一致 ⇒ 已跳过不显示"
                    f"（另一次录制的，坐标系不同，画上去会错位）。")
                continue
            self.route_imgs_rgb.append(mask_route_all(self.img_map_bgr, img, self.cfg))
        self.main_pts = []
        for img in self.route_imgs_rgb:
            self.main_pts.extend(_iter_code_pixels(img, self.route_codes))
        # ⚠️ 另存一份**除 goal 外**的主线像素：goal 是半径 1 的十字，会往平台外
        #    多画 2px；把它算进 x 范围，x=107/108（平台外）就会被误当成"平台内"，
        #    「竖段画在平台外」的黄字提示就白加了（QA 发现的盲区）。
        _goal_set = goal_codes(self.merged_cc)
        _move_codes = self.route_codes - _goal_set
        self.main_move_pts = []
        for img in self.route_imgs_rgb:
            self.main_move_pts.extend(_iter_code_pixels(img, _move_codes))

        # 有尺寸不一致的主线图 ⇒ 明确告诉用户（否则"少画了几条线"看着像 bug）
        if self.skipped_routes:
            _lines = "\n".join(f"  {n} —— {h} x {w}"
                              for n, (h, w) in self.skipped_routes)
            logger.error(
                f"[手绘回正线] minimaps/{self.map_name} 里有 "
                f"{len(self.skipped_routes)} 张主线图与 map.png "
                f"{self.h}x{self.w} 尺寸不一致，已跳过不显示：{_lines}")
            QMessageBox.warning(
                self, "有主线图尺寸不一致（已跳过）",
                f"下面这些主线图的尺寸和 map.png（{self.w} x {self.h}）不一样：\n\n"
                f"{_lines}\n\n"
                f"它们多半是**另一次录制**留下的 —— 那时小地图窗口大小不同，"
                f"坐标系就不一样，画在这张地图上会错位，所以这次没显示。\n\n"
                f"引擎也不该按它们走（会走到错的地方），建议删掉这几张，"
                f"或按现在的小地图尺寸重录。")
            QApplication.processEvents()

        # 已有回正线（**原始**内容，不 mask —— 那是"写盘真源"）
        if os.path.exists(self.path_home):
            try:
                self.img_out_bgr = load_image(self.path_home, cv2.IMREAD_COLOR)
            except Exception as e:
                logger.warning(f"[手绘回正线] route_home.png 读失败，当空图处理：{e}")
                self.img_out_bgr = None
        if self.img_out_bgr is None:
            self.img_out_bgr = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        if self.img_out_bgr.shape[:2] != (self.h, self.w):
            logger.warning(
                f"[手绘回正线] route_home.png 尺寸 {self.img_out_bgr.shape[:2]} 与 "
                f"map.png {(self.h, self.w)} 不一致 —— 已按空图处理（旧线作废）。")
            self.img_out_bgr = np.zeros((self.h, self.w, 3), dtype=np.uint8)

        # 旧图迁移：第一版的落点锚（127,0,127）就地涂黑（第二版没有锚点了）
        wiped = wipe_legacy_anchor(cv2.cvtColor(self.img_out_bgr, cv2.COLOR_BGR2RGB))
        if wiped:
            logger.info(
                f"[手绘回正线] route_home.png 上发现 {len(wiped)} 个旧版落点标记"
                f"（127,0,127），已自动忽略 —— 第二版不再需要它。")
        return True

    def _auto_fit_scale(self):
        '''屏幕放不下就自动降级（下限 home_draw_scale_min），但不改变用户的选择意图。'''
        f_min = int(self.dp["home_draw_scale_min"])
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        max_w = (avail.width() - 90) if avail else 1400
        max_h = (avail.height() - 330) if avail else 900
        f = self.scale
        while f > f_min and (self.w * f > max_w or self.h * f > max_h):
            f -= 1
        self.scale = max(f_min, f)
        self.cmb_scale.blockSignals(True)
        self.cmb_scale.setCurrentText(f"{self.scale} 倍")
        self.cmb_scale.blockSignals(False)

    # ── 画图 ────────────────────────────────────────────────────────
    def _rebuild_base(self):
        '''合成底图（原尺寸 RGB）：地图 + 主线（半透明）+ 已存回正线（不透明）。'''
        base = cv2.cvtColor(self.img_map_bgr, cv2.COLOR_BGR2RGB).astype(np.int16)
        for img in self.route_imgs_rgb:
            if route_size_mismatch(img.shape, base.shape):
                # 兜底：_load_map 已经跳过尺寸不一致的主线图，这里再挡一次 ——
                # 掩码与底图尺寸不同会抛 IndexError，而"手绘面板打不开"
                # 比"少画几条参考线"严重得多（2026-10-14 真机）。
                logger.warning("[手绘回正线] 跳过一张尺寸不一致的主线图（不该发生）")
                continue
            mask = _color_mask(img, self.route_codes)
            if mask.any():
                base[mask] = (base[mask] * 45 + img[mask].astype(np.int16) * 55) // 100
        self.home_rgb = cv2.cvtColor(self.img_out_bgr, cv2.COLOR_BGR2RGB)
        hmask = np.any(self.img_out_bgr != 0, axis=-1)
        base_rgb = base.astype(np.uint8)
        if hmask.any():
            base_rgb[hmask] = self.home_rgb[hmask]
        self.base_rgb = np.ascontiguousarray(base_rgb)

    def _to_big(self, pt):
        '''原图坐标 → 放大图上该像素**块**的中心索引。

        ⚠️ 一个原图像素在放大图上占 [x*f, x*f + f - 1] 这 f 个屏幕像素，
           它的中心索引是 **x*f + (f-1)//2**，不是 x*f + f//2。
           后者在 f=4 时等于 x*f+2 → 反算 402/4 = 100.5 → Python 银行家
           取整得 100（偶数），**点在自己画的点上却落到了隔壁像素**。
           用 (f-1)//2 时偏移量恒 < 0.5，round 一定回到自己。
        '''
        f = self.scale
        c = (f - 1) // 2
        return (int(pt[0]) * f + c, int(pt[1]) * f + c)

    def _rebuild_view(self):
        '''重画画布（底图放大 + 当前正在画的线画在上面 —— 放大后才看得清标记）。'''
        f = self.scale
        big = cv2.resize(self.base_rgb, (self.w * f, self.h * f),
                         interpolation=cv2.INTER_NEAREST)

        # ── 网格：每 GRID_STEP（20）个原图像素一条暗灰细线 + 刻度数字 ──────────
        occ = _color_mask(self.base_rgb, self.route_codes) | np.any(
            self.img_out_bgr != 0, axis=-1)
        occ_big = cv2.resize(occ.astype(np.uint8), (self.w * f, self.h * f),
                             interpolation=cv2.INTER_NEAREST).astype(bool)
        grid = np.zeros(big.shape[:2], dtype=bool)
        for gx in range(0, self.w, GRID_STEP):
            grid[:, min(gx * f, self.w * f - 1)] = True
        for gy in range(0, self.h, GRID_STEP):
            grid[min(gy * f, self.h * f - 1), :] = True
        grid &= ~occ_big
        big[grid] = GRID_RGB
        for gx in range(0, self.w, GRID_STEP):
            cv2.putText(big, str(gx), (min(gx * f + 2, self.w * f - 22), 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, GRID_LABEL_RGB, 1,
                        cv2.LINE_AA)
        for gy in range(GRID_STEP, self.h, GRID_STEP):
            cv2.putText(big, str(gy), (2, min(gy * f - 3, self.h * f - 3)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, GRID_LABEL_RGB, 1,
                        cv2.LINE_AA)

        # 已存回正线的起点：画个白色靶心，一眼能认出"坑在哪"
        for line in self.lines:
            if line.get("start"):
                self._draw_bullseye(big, line["start"], (255, 255, 255), f)

        # ── 落点区间蓝带（半透明；**只在内存/画布上，不写进 PNG**）──────────
        if self.landing_seg is not None:
            (bx0, by0), (bx1, by1) = self.landing_seg
            x0, x1 = sorted((int(bx0), int(bx1)))
            y0, y1 = sorted((int(by0), int(by1)))
            p0 = self._to_big((x0, y0))
            p1 = self._to_big((x1, y1))
            ov = big.copy()
            cv2.rectangle(ov, p0, p1, (70, 150, 255), thickness=-1)
            big = cv2.addWeighted(ov, 0.35, big, 0.65, 0)
            cv2.rectangle(big, p0, p1, (70, 150, 255), thickness=max(1, f // 2))

        # ── 正在画的这条 ───────────────────────────────────────────────
        pts = self.cur_points
        thick = max(1, f // 2)
        ci = corner_index(pts, self.cur_actions) if pts else 0
        # ★ 落点横段的「原地跳」区间（主线正下方）—— 与落盘**同一个函数**算，
        #   画布上看到的色必须等于写进 PNG 的色（所见即所得，否则最难查）。
        jump_band = self._jump_band(pts, self.cur_actions) if len(pts) >= 2 else None
        for i in range(len(pts) - 1):
            action = self.cur_actions[i] if i < len(self.cur_actions) else "walk"
            p0, p1 = pts[i], pts[i + 1]
            horizontal = abs(p1[0] - p0[0]) >= abs(p1[1] - p0[1])
            if i < ci and str(action) == "walk" and horizontal:
                # ★ 落点横段：**逐像素分区着色**（与落盘同一条规则 hseg_pixel_color）
                for x in range(min(p0[0], p1[0]), max(p0[0], p1[0]) + 1):
                    rgb = hseg_pixel_color(x, p0[1], (pts[ci][0], p0[1]),
                                           self.merged_cc, "walk", jump_band)
                    if rgb is None:      # 老行为（没 band 时）：拐角列留给竖段
                        continue
                    cv2.rectangle(big, self._to_big((x, p0[1])),
                                  self._to_big((x, p0[1])),
                                  (int(rgb[0]), int(rgb[1]), int(rgb[2])),
                                  thickness=max(1, f))
                continue
            rgb = pick_color(p0, p1, action, self.merged_cc)
            # ⚠️ big 是 **RGB**（QImage.Format_RGB888），pick_color 返回的也是 RGB
            #    —— 不要再 [::-1]，否则"向右走=蓝"在画布上会显示成红（R/B 对调）。
            cv2.line(big, self._to_big(p0), self._to_big(p1),
                     (int(rgb[0]), int(rgb[1]), int(rgb[2])),
                     thickness=thick, lineType=cv2.LINE_8)
        for i, p in enumerate(pts):
            color = (255, 255, 255) if i == 0 else (200, 200, 200)
            cv2.circle(big, self._to_big(p), max(2, f // 2), color, thickness=1)
            cv2.putText(big, str(i + 1),
                        (self._to_big(p)[0] + 3, self._to_big(p)[1] - 3),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1,
                        cv2.LINE_AA)
        if pts:
            self._draw_bullseye(big, pts[0], (255, 120, 255), f)
        # 拐角：菱形标记（设计 6.2 要求"画成明显的菱形 + 提示条写拐角 x=99"）
        if pts and 0 < ci < len(pts):
            c = pts[ci]
            cc0 = self._to_big(c)
            r = max(3, f + 1)
            cv2.polylines(big, [np.array([
                (cc0[0], cc0[1] - r), (cc0[0] + r, cc0[1]),
                (cc0[0], cc0[1] + r), (cc0[0] - r, cc0[1])], dtype=np.int32)],
                True, (0, 220, 220), thickness=2)
        if self.goal_set and pts:
            cv2.circle(big, self._to_big(pts[-1]), max(2, f), (255, 255, 0),
                       thickness=-1)
            cv2.circle(big, self._to_big(pts[-1]), max(2, f), (90, 90, 0),
                       thickness=1)

        big = np.ascontiguousarray(big)
        hh, ww = big.shape[:2]
        qimg = QImage(big.data, ww, hh, 3 * ww, QImage.Format_RGB888)
        self._qimg_buf = big          # 必须持有：QImage 不拷贝数据
        self.canvas.setFixedSize(ww, hh)
        self.canvas.setPixmap(QPixmap.fromImage(qimg))

    @staticmethod
    def _draw_bullseye(img, pt, color_bgr, f):
        '''落点靶心：白圈 + 十字 + 实心点。1 像素的起点在 4 倍图上太难找。'''
        c = (f - 1) // 2
        cx, cy = int(pt[0]) * f + c, int(pt[1]) * f + c
        r = max(3, f)
        cv2.circle(img, (cx, cy), r, color_bgr, thickness=2)
        cv2.circle(img, (cx, cy), max(1, r // 3), color_bgr, thickness=-1)
        cv2.line(img, (cx - r - 2, cy), (cx + r + 2, cy), color_bgr, 1)
        cv2.line(img, (cx, cy - r - 2), (cx, cy + r + 2), color_bgr, 1)

    # ── 交互 ────────────────────────────────────────────────────────
    def _cur_action(self) -> str:
        for key, rb in self.radio_actions.items():
            if rb.isChecked():
                return key
        return DEFAULT_ACTION

    def _on_scale_changed(self, text):
        try:
            self.scale = int(str(text).replace("倍", "").strip())
        except (TypeError, ValueError):
            return
        self._rebuild_view()

    def _on_canvas_move(self, sx, sy, buttons=None):
        x, y = screen_to_map(sx, sy, self.scale, self.w, self.h)
        self.lbl_mouse.setText(f"鼠标：({x}, {y})")
        # 拖蓝带：按住左键移动时实时预览
        if self._dragging and buttons is not None and (buttons & Qt.LeftButton):
            self._drag_to(x, y)

    # ── 落点区间（蓝带）─────────────────────────────────────────────
    def _on_band_toggled(self, checked):
        '''勾上「框选落点区间」后，画布左键变成"拖蓝带"，不再用来点路线点。'''
        if checked:
            self.lbl_how.setText(
                "现在在**框落点区间**：在图上按住左键拖出一条蓝带，把会掉下去的地方框进去。\n"
                "框好之后**取消勾选**，再回去点路线（第 1 下 = 落点）。")
        else:
            self.lbl_how.setText(HINT_FIRST if not self.cur_points else HINT_AFTER_FIRST)
        self._rebuild_view()

    def _band_start(self, x, y):
        self._dragging = True
        self._drag_from = (x, y)
        self.landing_seg = ((x, y), (x, y))
        self._rebuild_view()

    def _drag_to(self, x, y):
        if self._drag_from is None:
            return
        self.landing_seg = (self._drag_from, (x, y))
        self._rebuild_view()

    def _band_end(self, x, y):
        if not self._dragging:
            return
        self._dragging = False
        if self._drag_from is not None:
            self.landing_seg = (self._drag_from, (x, y))
        self._drag_from = None
        self._refresh_band_label()
        self._rebuild_view()
        self._update_hint()

    def _refresh_band_label(self):
        if self.landing_seg is None:
            self.lbl_band.setText("落点区间：还没框（跳过也行，只是看不到「接住几个」）")
            return
        (x0, y0), (x1, y1) = self.landing_seg
        ax, bx = sorted((int(x0), int(x1)))
        ay, by = sorted((int(y0), int(y1)))
        self.lbl_band.setText(
            f"落点区间：x {ax} ~ {bx}（宽 {bx - ax + 1}px）、"
            f"y {ay} ~ {by}（高 {by - ay + 1}px）　← 蓝带，不会画进 route_home.png")

    def _coverage(self, home_pts=None):
        '''用引擎同一条判定式算「框进来的落点接住几个」。'''
        if self.landing_seg is None:
            return None
        landing = band_to_pts(*self.landing_seg)
        if home_pts is None:
            home_pts = [q for q in self._preview_pts()]
        return simulate_coverage(home_pts, self.main_pts, landing,
                                 tol=int(self.hp["home_dist_tol"]))

    def _jump_band(self, pts=None, actions=None):
        '''★ 当前这条线的「主线正下方」x 区间（角色站这里面**原地起跳**就能上平台）。

        算不出来（没主线 / 坑底头顶上方没主线 / 退化）→ **返回 None**，
        落点横段回落到"汇聚到拐角"的老行为 —— 绝不因此报错崩掉。

        ⚠️ 主线可能有多张（route1/route2/...）：取**所有主线区间的最小外包**
           —— 角色在任一主线正下方起跳都上得去，少算一个就等于漏掉一个能跳的点。

        Args:
            pts / actions: 当前这条线的点和段动作（缺省用 self.cur_*）

        Returns:
            (x0, x1) | None
        '''
        pts = self.cur_points if pts is None else pts
        actions = self.cur_actions if actions is None else actions
        if not pts:
            return None
        try:
            # ⚠️ 坑底 y **不能**取 `pts[corner_index(...)]`：corner_index 返回的是
            #    第一个非 walk 段的**起点下标 +1**，也就是竖段的**顶端**（= 主线上
            #    那个点，y=122），不是坑底（y=128）—— 拿它当 pit_y 会去数"比 122
            #    还高 8px"的主线，整段区间直接算歪（实测算成 x 94~104 而不是
            #    93~105）。落点横段的 y 要按 render_home_image 的口径现找：
            #    拐角之前、动作是 walk、**横着**的那几段所在的 y。
            ci = corner_index(pts, actions) if pts else 0
            acts = list(actions or [])
            hseg_ys = []
            for i in range(len(pts) - 1):
                a = acts[i] if i < len(acts) else "walk"
                p0, p1 = pts[i], pts[i + 1]
                if (i < ci and str(a) == "walk"
                        and abs(p1[0] - p0[0]) >= abs(p1[1] - p0[1])):
                    hseg_ys.append(int(p0[1]))
            pit_y = int(hseg_ys[-1]) if hseg_ys else int(pts[0][1])
            # ⚠️ 按 pit_y 缓存：拖蓝带时 _rebuild_view 每帧都调这里，不缓存的话
            #    每次都要扫一遍主线图（还会刷一行日志）。主线在本会话里不变。
            cache = getattr(self, "_band_cache", None)
            if cache is None:
                cache = self._band_cache = {}
            if pit_y in cache:
                return cache[pit_y]
            _p = jump_band_params(self.cfg)
            best = None
            for img in (self.route_imgs_rgb or []):
                if img is None:
                    continue
                # ⚠️ calc_jump_band 收 **BGR**（与 cv2 落盘口径一致），
                #    而 self.route_imgs_rgb 是 RGB —— 这里必须转一次，别省。
                band = calc_jump_band(
                    cv2.cvtColor(img, cv2.COLOR_RGB2BGR), pit_y,
                    max_drop=int(_p["home_jump_band_max_drop"]),
                    inset=int(_p["home_jump_band_inset"]),
                    color_code=self.route_codes)
                if band is None:
                    continue
                best = band if best is None else (min(best[0], band[0]),
                                                  max(best[1], band[1]))
            cache[pit_y] = best
            if best is not None:
                logger.info(f"[手绘回正线] 落点横段 y={pit_y}：主线正下方跳跃区间 "
                            f"x {best[0]}~{best[1]}（区间内着「原地跳」色）")
            else:
                logger.info(f"[手绘回正线] 落点横段 y={pit_y}：头顶上方没找到主线"
                            f"（落差 ≤ {int(_p['home_jump_band_max_drop'])}px），"
                            f"横段按老规则「汇聚到拐角」着色")
            return best
        except Exception as e:      # 兜底：算 band 失败也要能存线（= 老行为）
            logger.warning(f"[手绘回正线] 跳跃区间算不出来，按老规则着色：{e}")
            return None

    # ── 落点：填坐标 与 点画布 走**同一条路径** ──────────────────────────
    def _set_landing(self, x, y, center=True) -> bool:
        '''把 (x, y) 设为落点（= 第 1 个点）—— 画布点击和「填坐标」按钮**都调这一个**。

        为什么必须共用：两条路要是各写一份，就会出现"填坐标画出来的线和点出来
        的不一样"（比如忘了重置 goal_set），而这类差异在自检里压根测不到。
        '''
        err = coord_error(x, y, self.w, self.h)
        if err:
            QMessageBox.warning(self, "这个坐标用不了", err)
            return False
        if self.cur_points:
            self.cur_points[0] = (x, y)
        else:
            self.cur_points = [(x, y)]
            self.cur_actions = []
            self.goal_set = False
            self.lbl_how.setText(HINT_AFTER_FIRST)
        self._rebuild_view()
        self._update_hint()
        if center:
            self._center_on(x, y)
        return True

    def _center_on(self, x, y):
        '''把画布滚到 (x, y) 露脸的位置（填坐标时不然可能滚到屏幕外看不见）。'''
        f = self.scale
        px = int(x) * f + (f - 1) // 2
        py = int(y) * f + (f - 1) // 2
        try:
            self.scroll.ensureVisible(px, py, f * 10, f * 10)
        except Exception:
            pass

    def _fill_coord_boxes(self, x, y):
        '''把解析出来的坐标回填到 X / Y 两个框（带防递归锁）。'''
        self._filling_coord = True
        try:
            self.edit_x.setText(str(int(x)))
            self.edit_y.setText(str(int(y)))
        finally:
            self._filling_coord = False

    def _on_coord_text_changed(self, _text=None):
        '''任一框里出现"能认出的一对坐标"就自动回填到两个框（给个即时反馈）。'''
        if getattr(self, "_filling_coord", False):
            return
        for box in (self.edit_x, self.edit_y):
            pt = parse_coord_text(box.text())
            if pt is not None:
                self._fill_coord_boxes(*pt)
                return

    def _set_landing_from_input(self):
        '''【📍 把这点设为落点】：吃"两个数字"或"粘进来的一整行日志"。'''
        raw_x = self.edit_x.text().strip()
        raw_y = self.edit_y.text().strip()
        pt = parse_coord_text(raw_x) or parse_coord_text(raw_y)
        if pt is None and raw_x and raw_y:
            pt = parse_coord_text(f"{raw_x}, {raw_y}")
        if pt is None:
            QMessageBox.information(
                self, "还没填坐标",
                "请填两个数字（X 和 Y），或者把日志里带「角色位置」的那一整行粘进来。\n\n"
                "例如粘贴这一整行也行：\n"
                "  [回正] 角色位置 (99, 128) 附近找不到回正线像素")
            return
        self._fill_coord_boxes(*pt)
        if self._set_landing(*pt):
            self.lbl_how.setText(
                f"落点已设为 ({pt[0]}, {pt[1]}) —— 已在图上标出（画着靶心的那个点）。\n"
                + HINT_AFTER_FIRST)

    def _on_canvas_right(self, sx, sy):
        '''右键 = 撤销上一点（非程序员比找按钮快）。'''
        self._undo_point()

    def _on_canvas_release(self, sx, sy):
        if self._dragging:
            x, y = screen_to_map(sx, sy, self.scale, self.w, self.h)
            self._band_end(x, y)

    def _on_canvas_click(self, sx, sy):
        x, y = screen_to_map(sx, sy, self.scale, self.w, self.h)

        # 蓝带模式：左键只用来拖落点区间，不点路线点
        if self.chk_band.isChecked():
            self._band_start(x, y)
            return

        action = self._cur_action()
        if not self.cur_points:
            if action == "goal":
                self.lbl_how.setText(
                    "⚠️ 先点**落点**（角色掉下去之后站的位置）—— 终点要等走到主线"
                    "那一步再点。现在请改选「走过去 / 跳一下 / 爬上去 / 下去」。")
                return
            self._set_landing(x, y, center=False)
            return
        if self.goal_set:
            self.lbl_how.setText(HINT_GOAL_DONE)
        else:
            if (x, y) == self.cur_points[-1]:
                return
            self.cur_points.append((x, y))
            if action == "goal":
                self.cur_actions.append(GOAL_SEGMENT_ACTION)
                self.goal_set = True
                self.lbl_how.setText(HINT_GOAL_DONE)
            else:
                self.cur_actions.append(action)
        self._rebuild_view()
        self._update_hint()

    def _undo_point(self):
        if not self.cur_points:
            return
        if self.goal_set:
            self.goal_set = False
        self.cur_points.pop()
        self.cur_actions = trim_actions(self.cur_points, self.cur_actions)
        self.lbl_how.setText(HINT_FIRST if not self.cur_points else HINT_AFTER_FIRST)
        self._rebuild_view()
        self._update_hint()

    def _clear_points(self):
        self.cur_points = []
        self.cur_actions = []
        self.goal_set = False
        self.lbl_how.setText(HINT_FIRST)
        self._rebuild_view()
        self._update_hint()

    # ── 实时体检 ────────────────────────────────────────────────────
    def _preview_pts(self):
        '''把"正在画的这条"渲到一张黑图上，返回它画出来的像素（引擎同一条链路）。

        Returns:
            list[(x, y)]: 这条线的全部像素；没画出来 → []
        '''
        pts = list(self.cur_points)
        if len(pts) < 2:
            return []
        blob = make_blob(pts, self.cur_actions, self.goal_set, self._jump_band(pts, self.cur_actions))
        preview_bgr = render_home_image(
            np.zeros((self.h, self.w, 3), dtype=np.uint8), [blob], self.merged_cc,
            goal_radius=int(self.dp.get("home_draw_goal_radius", 1)))
        preview_rgb = cv2.cvtColor(preview_bgr, cv2.COLOR_BGR2RGB)
        comps = split_components(preview_rgb, self.route_codes)
        if not comps:
            return []
        # 起点所在的那个连通域 = 这一条
        target = None
        for comp in comps:
            if pts[0] in comp:
                target = comp
                break
        return target or comps[0]

    def _preview_line(self):
        '''（正在画的这条）→ (line_dict | None, preview_rgb)。'''
        pts = list(self.cur_points)
        if len(pts) < 2:
            return None, None
        blob = make_blob(pts, self.cur_actions, self.goal_set, self._jump_band(pts, self.cur_actions))
        preview_bgr = render_home_image(
            np.zeros((self.h, self.w, 3), dtype=np.uint8), [blob], self.merged_cc,
            goal_radius=int(self.dp.get("home_draw_goal_radius", 1)))
        preview_rgb = cv2.cvtColor(preview_bgr, cv2.COLOR_BGR2RGB)
        comps = split_components(preview_rgb, self.route_codes)
        if not comps:
            return None, preview_rgb
        comp = None
        for c in comps:
            if pts[0] in c:
                comp = c
                break
        comp = comp or comps[0]
        geom = calc_line_geom(comp, self.route_imgs_rgb, self.route_codes,
                              self.cfg, img_rgb=preview_rgb)
        far = far_segment_stats(comp, self.route_imgs_rgb, self.route_codes, self.cfg)
        ok, why = validate_home_line(comp, preview_rgb, self.route_imgs_rgb,
                                     self.route_codes, self.cfg)
        return {"id": 0, "start": geom["start"], "goal": geom["goal"], "pts": comp,
                "span": geom["span"], "cover_xs": far["cover_xs"],
                "cover_span": far["cover_span"], "has_jump": geom["has_jump"],
                "n_pts": geom["n_pts"], "ok": ok, "why": why}, preview_rgb

    def _update_hint(self):
        '''刷新底部两行：画到哪了 + 这条线体检结论（实时，含「接住落点 N/M」）。'''
        pts = self.cur_points
        if not pts:
            self.lbl_drawn.setText("还没点 —— 先在图上点第 1 下（落点）")
            self.lbl_stat.setText("")
            return
        if len(pts) == 1:
            d = None
            if self.main_pts:
                xs = np.asarray([p[0] for p in self.main_pts], dtype=np.int32)
                ys = np.asarray([p[1] for p in self.main_pts], dtype=np.int32)
                d = dist_to_pixels(xs, ys, pts[0])
            txt = f"落点已定：({pts[0][0]},{pts[0][1]})"
            if d is None:
                self.lbl_drawn.setText(txt)
                self.lbl_stat.setText(
                    '<span style="color:#c00">这张图没有可用的主线 —— '
                    '请先录一段主线再画回正线。</span>')
            else:
                # ⚠️ 第二版**不再**因为"离主线近"报错：坑底离主线 6px 是正常的
                self.lbl_drawn.setText(txt + f" · 离主线 {d}px")
                self.lbl_stat.setText(
                    f'<span style="color:#060">落点已定 ✓（离主线 {d}px）。'
                    f'接着沿坑底**横着**点一段，再点「跳一下」跳回主线。</span>')
            return

        line, _ = self._preview_line()
        # ★ 竖段画在平台外 → 黄字警告（**不阻断保存**，这张图可能有别的平台）
        _pw = jump_off_platform(pts, self.cur_actions, self.main_move_pts, self.main_pts)
        warn_span = (f'<span style="color:#c60">⚠ {_pw["msg"]}</span>' if _pw else "")
        if line is None:
            self.lbl_drawn.setText(" → ".join(f"({p[0]},{p[1]})" for p in pts)
                                   + f"　共 {len(pts)} 点")
            self.lbl_stat.setText(warn_span)
            return
        if not self.goal_set:
            self.lbl_drawn.setText(
                f"落点({pts[0][0]},{pts[0][1]}) → 现在在({pts[-1][0]},{pts[-1][1]})"
                f"　共 {len(pts)} 点")
            self.lbl_stat.setText(
                f'<span style="color:#c66">⚠ {HINT_NEED_GOAL.splitlines()[0]}</span>'
                + (f'<br>{warn_span}' if warn_span else ""))
            return

        cov = self._coverage()
        ci = corner_index(pts, self.cur_actions)
        corner_txt = (f"拐角 x={pts[ci][0]}"
                      if 0 < ci < len(pts) else "拐角 —（没画竖直段）")
        self.lbl_drawn.setText(
            f"落点({pts[0][0]},{pts[0][1]}) → 终点({pts[-1][0]},{pts[-1][1]})"
            f"　共 {len(pts)} 点 · {corner_txt}")
        detail = (f"这条线：跨度 {line['span']}px · 落点覆盖 "
                  f"{line['cover_span']}px"
                  f"{' · 含跳跃' if line['has_jump'] else ''}")
        cov_txt = coverage_text(cov) if cov is not None else ""
        if line["ok"]:
            color = "#060" if (cov is None or cov["covered"] >= cov["total"]) else "#c60"
            self.lbl_stat.setText(
                f'<span style="color:{color}">{detail} · ✓ 合格'
                f'{"　|　" + cov_txt if cov_txt else ""}</span>'
                + (f'<br>{warn_span}' if warn_span else ""))
        else:
            self.lbl_stat.setText(
                f'<span style="color:#c00">{detail} · ✗ {line["why"]}'
                f'{"　|　" + cov_txt if cov_txt else ""}</span>'
                + (f'<br>{warn_span}' if warn_span else ""))

    # ── 已有线列表 ──────────────────────────────────────────────────
    def _refresh_lines(self):
        self.lines = audit_components(self.home_rgb, self.route_imgs_rgb, self.cfg)
        notes = load_home_notes(self.map_name)
        name_of = {}
        for n in (notes.get("notes") or []):
            s = n.get("start")
            if isinstance(s, (list, tuple)) and len(s) >= 2:
                name_of[(int(s[0]), int(s[1]))] = str(n.get("name") or "")
        self.list_lines.clear()
        for line in self.lines:
            txt = line_summary(line)
            extra = name_of.get(tuple(line.get("start") or ()), "")
            if extra:
                txt += f" · {extra}"
            self.list_lines.addItem(txt)
        has = bool(self.lines)
        self.btn_delete.setEnabled(has)

    def _delete_selected(self):
        row = self.list_lines.currentRow()
        if row < 0:
            QMessageBox.information(self, "先选一条",
                                    "在下面的列表里点选要删的那条回正线。")
            return
        if row >= len(self.lines):
            return
        line = self.lines[row]
        reply = QMessageBox.question(
            self, "删掉这条回正线？",
            f"要删的是：\n  {line_summary(line)}\n\n"
            f"只删这一条，这张图其它回正线不受影响。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        for x, y in line["pts"]:
            self.img_out_bgr[y, x] = (0, 0, 0)
        self._write_home_png(self.img_out_bgr)
        self._rebuild_base()
        self._refresh_lines()
        self._rebuild_view()
        logger.info(f"[手绘回正线] 已删掉「{self.map_name}」的第 {line['id']} 条回正线")
        self.last_msg = f"已删掉第 {line['id']} 条回正线"

    # ── 保存 ────────────────────────────────────────────────────────
    def _write_home_png(self, img_bgr) -> bool:
        '''落盘。全黑（一条线都不剩）就直接删文件，免得引擎每次开跑都报错。'''
        if not np.any(img_bgr != 0):
            try:
                if os.path.exists(self.path_home):
                    os.remove(self.path_home)
            except OSError as e:
                logger.warning(f"[手绘回正线] 删除空的 route_home.png 失败：{e}")
            return True
        return bool(imwrite_unicode(self.path_home, img_bgr))

    def _overlap_warnings(self, lines) -> list:
        '''两条线的**落点区间**（起始段 x 范围）重叠 → **只警告不阻断**（P2-1）。

        ⚠️ v0.9 第二版：第一版比的是"两个落点锚的曼哈顿距离 < 2R"，锚点没了，
           改成比**起始段 x 范围**是否相交 —— 与引擎 `_scan_home_lines` 的
           P2-1 警告同口径（掉在重叠区可能走错线）。
        '''
        warns = []
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                ci, cj = lines[i].get("cover_xs"), lines[j].get("cover_xs")
                if not ci or not cj:
                    continue
                if ci[0] <= cj[1] and cj[0] <= ci[1]:
                    warns.append(
                        f"第 {lines[i]['id']} 条与第 {lines[j]['id']} 条的**落点区间"
                        f"重叠了**（x {ci[0]}~{ci[1]} / x {cj[0]}~{cj[1]}）—— "
                        f"角色掉在重叠区可能接到另一条线上。\n"
                        f"  　　没拦你，已经存好了；建议把两条线画在不同的坑上。")
        return warns

    def _save_line(self) -> bool:
        '''保存这一条。三步：先在内存里判废 → 写盘 → 回读校验（被吃了就回滚）。'''
        pts = list(self.cur_points)
        gate = save_gate_error(pts, self.goal_set, bool(self.route_imgs_rgb))
        if gate:
            title = "还没点「回到主线」" if (len(pts) >= 2 and not self.goal_set) else "还没画完"
            QMessageBox.warning(self, title, f"{gate}\n\n"
                                             f"已经画的点都还在画布上，改完直接再点保存就行。")
            return False

        # ── 落点覆盖：<100% 时**红字 + 二次确认**（留"仍要保存"的逃生口，设计 6.4）──
        cov = self._coverage()
        if cov is not None and cov["total"] and cov["covered"] < cov["total"]:
            reply = QMessageBox.question(
                self, "这条线接不满落点区间",
                f"{coverage_text(cov)}\n\n"
                f"现在保存的话，掉在够不着的那些落点上时，角色还是回不来"
                f"（只会原地打怪）。\n\n仍要保存吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return False

        dp = self.dp
        blob = make_blob(pts, self.cur_actions, self.goal_set, self._jump_band(pts, self.cur_actions))
        cand_bgr = render_home_image(
            self.img_out_bgr, [blob], self.merged_cc,
            goal_radius=int(dp.get("home_draw_goal_radius", 1)))
        cand_rgb = cv2.cvtColor(cand_bgr, cv2.COLOR_BGR2RGB)

        # ── ① 保存前：按引擎同一条链路判废（❌ 直接拦下，不写盘）──────────
        comps = split_components(cand_rgb, self.route_codes)
        target = None
        for comp in comps:
            if pts[0] in comp:
                target = comp
                break
        if target is None:
            QMessageBox.critical(
                self, "没画上去",
                "这条线一个像素都没画出来 —— 请重画（两点别点在同一个位置）。")
            return False
        ok, why = validate_home_line(target, cand_rgb, self.route_imgs_rgb,
                                     self.route_codes, self.cfg)
        if not ok:
            QMessageBox.warning(
                self, "这条线用不了，先不保存",
                f"{why}\n\n"
                f"已经画的点都还在画布上，改完直接再点【保存这条回正线】就行。")
            return False
        geom = calc_line_geom(target, self.route_imgs_rgb, self.route_codes,
                              self.cfg, img_rgb=cand_rgb)

        # ── ② 写盘 ────────────────────────────────────────────────────
        backup = self.img_out_bgr.copy()
        if not self._write_home_png(cand_bgr):
            QMessageBox.critical(
                self, "保存失败",
                f"写不进：\n  {self.path_home}\n\n"
                f"目录不可写 / 被别的程序占用时会出现这个。")
            return False

        # ── ③ 落盘回放校验：被地图底色吃掉就回滚 ────────────────────────
        v = verify_saved_image(self.path_home, self.img_map_bgr, self.cfg,
                               pts[-1],
                               goal_radius=int(dp.get("home_draw_goal_radius", 1)))
        if not v["ok"]:
            self._write_home_png(backup)     # 回滚，别留一条永远接不住人的线
            self.img_out_bgr = backup
            self._rebuild_base()
            self._refresh_lines()
            self._rebuild_view()
            QMessageBox.critical(self, "这个点被地图底色吃掉了", v["reason"])
            self.last_msg = "保存失败：" + v["reason"].split("——")[0]
            return False

        # ── ④ 成功 ────────────────────────────────────────────────────
        self.img_out_bgr = cand_bgr
        self._rebuild_base()
        self._refresh_lines()
        self._save_note(pts[0], self.landing_seg)

        saved_lines = audit_components(v["img_rgb"], self.route_imgs_rgb, self.cfg)
        detail = "\n".join(
            f"　第 {l['id']} 条：起点 {l['start']} → 终点 {l['goal']}，"
            f"跨度 {l['span']}px，落点覆盖 {l['cover_span']}px"
            f"{'，含跳跃' if l['has_jump'] else ''}"
            f"{'' if l['ok'] else '（这条用不了：' + l['why'] + '）'}"
            for l in saved_lines) or "　（没有）"
        warns = self._overlap_warnings(saved_lines)

        msg = (f"已保存到 route_home.png ✓\n\n"
               f"这次画的这条：跨度 {geom['span']}px，落点覆盖 "
               f"{geom['n_pts']} 个像素。\n"
               f"以后角色离它比离主线近（差 2px 以上）就自动走回主线。\n\n"
               f"这张图现在共有 {len(saved_lines)} 条回正线：\n{detail}")
        if cov is not None and cov["total"]:
            msg += f"\n\n落点覆盖：{coverage_text(cov)}"
        # ★ 竖段画在平台外：判据⑤ 拦不住（goal 圆必然碰到平台边缘）→ 保存时再提醒一次
        _pw = jump_off_platform(pts, self.cur_actions, self.main_move_pts, self.main_pts)
        if _pw:
            msg += f"\n\n⚠ 提醒（不影响保存）：\n　{_pw['msg']}"
        if warns:
            msg += "\n\n⚠ 提醒（不影响保存）：\n　" + "\n　".join(warns)
        QMessageBox.information(self, "保存成功", msg)

        self.last_msg = (f"已保存 1 条回正线：起点 {pts[0]} → 终点 {pts[-1]}，"
                         f"跨度 {geom['span']}px；这张图现在共 {len(saved_lines)} 条")
        logger.info(f"[手绘回正线] {self.map_name}：{self.last_msg}")
        self.saved_any = True
        self._clear_points()
        return True

    def _save_note(self, start, landing_seg=None):
        '''可选备注（引擎运行时**不读**，只是列表里有个中文名字好看）。

        v0.9 第二版：备注里多存**落点区间**（设计 6.1 允许；**绝不写进 PNG**）。
        '''
        name = self.edit_name.text().strip()
        if not name and landing_seg is None:
            return
        data = load_home_notes(self.map_name) or {"version": 1, "notes": []}
        notes = [n for n in (data.get("notes") or [])
                 if tuple(n.get("start") or ()) != tuple(start)]
        item = {
            "start": [int(start[0]), int(start[1])],
            "name": name,
            "note": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if landing_seg is not None:
            (ax, ay), (bx, by) = landing_seg
            item["landing"] = [[int(min(ax, bx)), int(min(ay, by))],
                               [int(max(ax, bx)), int(max(ay, by))]]
        notes.append(item)
        save_home_notes(self.map_name, {"version": 1, "notes": notes})
        self.edit_name.clear()

    # ── 设计 3.5 的对外入口 ─────────────────────────────────────────
    def open_for(self, map_name=None) -> int:
        '''（重新）载入某张图并打开面板，返回 QDialog 的退出码。'''
        if map_name and map_name != self.map_name:
            self.map_name = str(map_name)
            self.setWindowTitle(f"手绘回正线 — {self.map_name}")
            self.lbl_title.setText(f"🖊 手绘回正线 — {self.map_name}")
            self.map_dir = resource_path(os.path.join("minimaps", self.map_name))
            self.path_home = os.path.join(self.map_dir, "route_home.png")
            self.cur_points, self.cur_actions = [], []
            self.landing_seg = None
            if self._load_map():
                self._rebuild_base()
                self._refresh_lines()
                self._rebuild_view()
        return self.exec()


def open_drawer(map_name, cfg=None, parent=None):
    '''打开手绘面板并等用户关掉。

    Returns:
        (saved: bool, msg: str)：是否保存成功过 + 中文结论（给主界面打日志）
    '''
    dlg = HomeRouteDrawer(map_name, cfg=cfg, parent=parent)
    dlg.exec()
    return dlg.saved_any, dlg.last_msg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="手绘回正线（不用开游戏、不用真的掉下去）")
    parser.add_argument('--new_map', type=str, default='new_map',
                        help='地图名（minimaps/<名>/）')
    parser.add_argument('--map', type=str, default='',
                        help='同 --new_map（兼容旧参数写法）')
    args = parser.parse_args(argv)
    map_name = args.map or args.new_map

    map_dir = resource_path(os.path.join("minimaps", map_name))
    if not os.path.isdir(map_dir):
        print(f"地图目录不存在：{map_dir}")
        print("先在主界面录一张图，或确认名字写对了。")
        return 1

    app = QApplication.instance() or QApplication(sys.argv)
    saved, msg = open_drawer(map_name)
    print(msg or "（没有保存任何回正线）")
    return 0


if __name__ == '__main__':
    sys.exit(main())
