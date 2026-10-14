# -*- coding: utf-8 -*-
'''路线色码普查 + 跳跃点邻域几何。

问题：用户报「无法跳跃」。跳跃像素确实录进去了（jump_pixel_report 显示每段 13px，
色码 `right none jump`），但 20 分钟运行里 [按键状态] 的指令元组**一次 jump 都没出现过**。
⇒ 跳跃是在引擎侧被吞掉的。本脚本回答"为什么"：
  打印每个色码的像素数 + 连通块中心，重点是**跳跃点周围有没有 up/down（梯子）像素**
  —— 因为 `MapleStoryAutoLevelUp.py:4753` 那层「还没和梯子对齐 → 先只走路」会静默吞掉跳跃。
'''
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import cv2
import numpy as np

from src.utils.common import load_yaml

cfg = load_yaml("config/config_default.yaml")
_cust = load_yaml("config/config_custom.yaml")
# 自定义配置常是**部分**配置（没 route 段），所以只做浅合并：custom 有的键覆盖 default。
for _k, _v in (_cust or {}).items():
    if isinstance(_v, dict) and isinstance(cfg.get(_k), dict):
        cfg[_k].update(_v)
    else:
        cfg[_k] = _v
cc = cfg["route"]["color_code"]

# color_code 是 dict：key = "R,G,B" 字符串，value = "move_x move_y action"
codes = []
for rgb_s, cmd in cc.items():
    rgb = tuple(int(v) for v in str(rgb_s).split(","))
    if len(rgb) != 3:
        continue
    codes.append((rgb, str(cmd)))

for name in ("route1.png", "route2.png"):
    path = os.path.join("minimaps", "沼泽地2", name)
    # ⚠️ cv2.imread 在 Windows 上读不了非 ASCII 路径（地图名是中文）⇒ 必须走 fromfile+imdecode
    with open(path, "rb") as _f:
        img = cv2.imdecode(np.frombuffer(_f.read(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print(f"!! 读不到 {path}")
        continue
    h, w = img.shape[:2]
    print(f"\n{'='*66}\n{name}  {w}x{h}\n{'='*66}")
    jump_pts = None
    updown_pts = []
    for rgb, cmd in codes:
        mask = np.all(img == np.array(rgb[::-1], dtype=np.uint8), axis=2).astype(np.uint8)
        n = int(mask.sum())
        if n == 0:
            continue
        nlab, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
        blobs = []
        for i in range(1, nlab):
            cx, cy = cent[i]
            blobs.append((int(round(cx)), int(round(cy)), int(stats[i, 4])))
        blobs.sort(key=lambda b: -b[2])
        show = "  ".join(f"({x},{y})x{c}" for x, y, c in blobs[:6])
        more = f" …共{len(blobs)}块" if len(blobs) > 6 else ""
        print(f"  {cmd:<18} RGB{rgb}  {n:>4}px  {show}{more}")
        if "jump" in cmd:
            jump_pts = [(x, y) for x, y, _ in blobs]
        if ("up" in cmd or "down" in cmd):
            updown_pts += [(x, y, cmd) for x, y, _ in blobs]

    # 跳跃点邻域：把 up/down 像素按到跳跃点的距离排序
    if jump_pts:
        print(f"\n  ── 跳跃点邻域（关键：附近有没有 up/down 像素）──")
        for jx, jy in jump_pts:
            near = sorted(updown_pts, key=lambda t: abs(t[0] - jx) + abs(t[1] - jy))[:4]
            if not near:
                print(f"    跳跃点({jx},{jy})：**附近没有任何 up/down 像素**")
                continue
            for ux, uy, ucmd in near:
                dx, dy = ux - jx, uy - jy
                flag = "★|dx|>2 ⇒ 会触发「还没对齐→先只走不跳」" if abs(dx) > 2 else "|dx|≤2 ⇒ 对齐，不吞"
                print(f"    跳跃点({jx},{jy}) ← up/down 像素({ux},{uy}) {ucmd}  dx={dx:+d} dy={dy:+d}  {flag}")
