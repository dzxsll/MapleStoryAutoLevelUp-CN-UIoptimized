# -*- coding: utf-8 -*-
'''方向冲突**落在哪儿**：把「同一位置既有 left 又有 right」的像素聚成团报出来。

为什么需要它：`validate_route_image` 只报**冲突个数**（route1 18 个 / route2 46 个），
用户知道"这条线坏了"，但不知道**坏在哪一段、该回去重录哪一截**。
本脚本给出冲突坐标的连通团中心，用户就能对着小地图直接找到那个折返点。

判据（与引擎同一条）：某个 left 像素，在曼哈顿半径 `--radius`（默认 8，= 引擎的
`search_range` 量级）内能碰到任一 right 像素 ⇒ 该 left 像素算一处冲突。
只要 one 位置附近两个方向都有，引擎在那里就无从判断。

⚠️ PNG 里存的是 **BGR**（录制器 RGB→BGR 翻转过一次），config 的 color_code 键是 RGB
⇒ 比对时必须 `rgb[::-1]`。这是本项目最容易踩的坑（见 route_audit.py:281 的注记）。
'''
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import cv2
import numpy as np

from src.utils.common import load_yaml


def load_route(path):
    # ⚠️ cv2.imread 在 Windows 上读不了非 ASCII 路径（地图名是中文）
    with open(path, "rb") as f:
        return cv2.imdecode(np.frombuffer(f.read(), dtype=np.uint8), cv2.IMREAD_COLOR)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="路线方向冲突定位")
    ap.add_argument("--map", default="沼泽地2")
    ap.add_argument("--routes", default="route1.png,route2.png")
    ap.add_argument("--radius", type=int, default=8, help="曼哈顿半径（引擎 search_range 量级）")
    args = ap.parse_args()

    cfg = load_yaml("config/config_default.yaml")
    cust = load_yaml("config/config_custom.yaml") or {}
    for k, v in cust.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v

    left_rgb, right_rgb = set(), set()
    for rgb_s, cmd in cfg["route"]["color_code"].items():
        rgb = tuple(int(v) for v in str(rgb_s).split(","))
        if len(rgb) != 3:
            continue
        head = str(cmd).split()
        if head and head[0] == "left":
            left_rgb.add(rgb)
        elif head and head[0] == "right":
            right_rgb.add(rgb)

    for name in [s.strip() for s in args.routes.split(",") if s.strip()]:
        path = os.path.join("minimaps", args.map, name)
        if not os.path.exists(path):
            print(f"!! 不存在：{path}")
            continue
        img = load_route(path)
        h, w = img.shape[:2]

        def mask_of(rgbs):
            m = np.zeros((h, w), dtype=bool)
            for rgb in rgbs:
                m |= np.all(img == np.array(rgb[::-1], dtype=np.uint8), axis=2)
            return m

        L, R = mask_of(left_rgb), mask_of(right_rgb)
        if not L.any() or not R.any():
            print(f"\n{name}: 只有单向像素（left {int(L.sum())} / right {int(R.sum())}）—— 无冲突")
            continue

        # 逐个 left 像素判是否有 right 邻居（曼哈顿半径内）
        conflict = np.zeros((h, w), dtype=np.uint8)
        ys, xs = np.nonzero(L)
        rad = args.radius
        for x, y in zip(xs, ys):
            y0, y1 = max(0, y - rad), min(h, y + rad + 1)
            x0, x1 = max(0, x - rad), min(w, x + rad + 1)
            sub = R[y0:y1, x0:x1]
            if not sub.any():
                continue
            yy, xx = np.nonzero(sub)
            if (np.abs(xx + x0 - x) + np.abs(yy + y0 - y)).min() <= rad:
                conflict[y, x] = 1

        n = int(conflict.sum())
        print(f"\n{'=' * 66}\n{name}  {w}x{h}   left {int(L.sum())}px  right {int(R.sum())}px")
        print(f"冲突 left 像素 {n} 个（曼哈顿半径 {rad}）\n{'=' * 66}")
        if n == 0:
            print("  ✓ 没有方向冲突")
            continue
        nlab, lab, stats, cent = cv2.connectedComponentsWithStats(conflict, 8)
        blobs = sorted(((int(round(cent[i][0])), int(round(cent[i][1])), int(stats[i, 4]))
                        for i in range(1, nlab)), key=lambda b: -b[2])
        print("  冲突团（按大小）—— 这些位置就是「去程和回程叠在一起」的地方，重录时在该处按 F8 分段：")
        for x, y, c in blobs:
            print(f"    ({x:>3},{y:>3})  {c:>3}px")
        print(f"  共 {len(blobs)} 团，合计 {n} px")
        if blobs:
            xs_ = [b[0] for b in blobs]
            ys_ = [b[1] for b in blobs]
            print(f"  包围盒 x {min(xs_)}..{max(xs_)}  y {min(ys_)}..{max(ys_)}")


if __name__ == "__main__":
    main()
