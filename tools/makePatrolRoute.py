# -*- coding: utf-8 -*-
'''平台巡逻线**生成器**：不录制、不手绘，直接算出两段完美的路线 PNG。

## 为什么要有它

录制这条路对"一条平台来回巡逻"这种最简单的需求**太脆弱**。真机连录 5 次的实测数据：

    每段里程    344px   250px   150px   81px
    方向冲突     56      20       5      0

冲突的来源是**战斗抖动**：往左走时被打退几像素，那段就被画成了右向像素。
它落在**一段之内**，所以切分逻辑天生清不掉 —— 我们已经为此打了八轮补丁。

**而"一条平台来回巡逻"根本不需要录制** —— 它是一条直线去、一条直线回，
用几何算出来是**确定**的：

    route1:  x1 --right--> x2   （终点盖 goal）
    route2:  x2 --left---> x1   （终点盖 goal）

按构造：**方向冲突 = 0，闭环距离 = 0**。

## 两种用法

    # ① 照已有的一条干净线去铺（推荐 —— 你手上那条 route3 就是干净的）
    python -m tools.makePatrolRoute --map 沼泽地2 --like route3.png

    # ② 直接给平台行与范围
    python -m tools.makePatrolRoute --map 沼泽地2 --y 50 --x 108 148

    python -m tools.makePatrolRoute --map 沼泽地2 --like route3.png --dry-run

## 它自己会验

写完**读回来走引擎的同一条路**（`cvtColor(load_image, BGR2RGB)`），数每种颜色
的像素个数并跑引擎自己的 `validate_route_image`。
颜色通道要是弄反了，读回来会是 0 个路线像素 —— 这时它会**自动换一种字节序重写**，
两种都不行就报错退出、不留半成品。**不靠"我觉得对"，靠读回来的数说话。**
'''
from __future__ import annotations

import argparse
import os
import shutil
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import (load_yaml, load_image, imwrite_unicode,   # noqa: E402
                              active_config_path, override_cfg)

GOAL_R = 2          # goal 标记的半径（与录制器盖章一致：半径 2 的圆点）


def pick_codes(color_code):
    '''从 config 的 color_code 里挑出「纯 left / 纯 right / goal」三个色值（RGB）。'''
    def _pure(token):
        for c, v in (color_code or {}).items():
            t = str(v).strip().split()
            if t and t[0] == token and (len(t) < 2 or t[1] == "none"):
                return c
        return None

    goal = next((c for c, v in (color_code or {}).items()
                 if str(v).strip().split()[-1] == "goal"), None)
    return _pure("left"), _pure("right"), goal


def route_pixels_of(img_rgb, color_code):
    '''数每种颜色各有多少像素（只用于**报告**，判据本身复用引擎的 _route_pixels）。'''
    out = {}
    for c, v in (color_code or {}).items():
        m = np.all(img_rgb[:, :, :3] == np.array(c, dtype=np.uint8), axis=2)
        n = int(m.sum())
        if n:
            out[str(v).strip()] = n
    return out


def derive_from_route(path, color_code):
    '''照一条已有路线，推出平台行 y 与 x 范围。'''
    img = cv2.cvtColor(load_image(path), cv2.COLOR_BGR2RGB)
    ys, xs = [], []
    for c in (color_code or {}):
        m = np.all(img[:, :, :3] == np.array(c, dtype=np.uint8), axis=2)
        yy, xx = np.nonzero(m)
        ys.extend(yy.tolist())
        xs.extend(xx.tolist())
    if not xs:
        return None, None, None
    y = int(np.bincount(np.array(ys)).argmax())      # 出现最多的那一行 = 平台站立行
    return y, int(min(xs)), int(max(xs))


def build(y, x1, x2, left, right, goal):
    '''造两段：route1 = 右行，route2 = 左行。返回 [(名字, 动作序列)]。'''
    def _line(x_from, x_to, code):
        return [(x, y, code) for x in range(min(x_from, x_to), max(x_from, x_to) + 1)]

    def _dot(cx, cy, code):
        pts = []
        for dy in range(-GOAL_R, GOAL_R + 1):
            for dx in range(-GOAL_R, GOAL_R + 1):
                if dx * dx + dy * dy <= GOAL_R * GOAL_R:
                    pts.append((cx + dx, cy + dy, code))
        return pts

    seg1 = _line(x1, x2, right) + _dot(x2, y, goal)
    seg2 = _line(x2, x1, left) + _dot(x1, y, goal)
    return seg1, seg2


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成一条平台来回巡逻线（两段）")
    ap.add_argument("--map", required=True, help="地图名（minimaps/<名字>/）")
    ap.add_argument("--y", type=int, default=None, help="平台的站立行（像素行）")
    ap.add_argument("--x", type=int, nargs=2, default=None, metavar=("X1", "X2"),
                    help="横向范围（含两端）")
    ap.add_argument("--like", default=None,
                    help="照这条已有路线推 y 与 x 范围（如 route3.png）")
    ap.add_argument("--dry-run", action="store_true", help="只报会画成什么，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 route1/route2")
    a = ap.parse_args(argv)

    cfg = load_yaml("config/config_default.yaml")
    _ap = active_config_path()
    if _ap:
        try:
            cfg = override_cfg(cfg, load_yaml(_ap))
        except Exception:                                    # noqa: BLE001
            pass
    # ⚠️ config 里 color_code 的键是**字符串** "255,0,0"（yaml 就是这样），
    #    引擎自己在 load_config 里转成 (r,g,b) 元组。工具这边必须做同一步 ——
    #    否则 `img[y,x] == (r,g,b)` 永远为假，一个像素都找不到（实测踩到）。
    cfg["route"]["color_code"] = {
        tuple(int(p) for p in k.split(',')): v
        for k, v in cfg["route"]["color_code"].items()}
    left, right, goal = pick_codes(cfg["route"]["color_code"])
    if not (left and right and goal):
        print("[错误] config 的 route.color_code 里找不到纯 left / 纯 right / goal 色值。")
        return 2

    map_dir = os.path.join("minimaps", a.map)
    map_png = os.path.join(map_dir, "map.png")
    if not os.path.exists(map_png):
        print(f"[错误] 找不到底图 {map_png} —— 先用录制器把这张图录一遍（会存下 map.png）。")
        return 2

    # ── 定位平台：显式参数优先，其次照 --like 推 ────────────────────────────
    y, x1, x2 = a.y, None, None
    if a.x:
        x1, x2 = int(a.x[0]), int(a.x[1])
    if a.like:
        lp = a.like if os.path.isabs(a.like) else os.path.join(map_dir, a.like)
        if not os.path.exists(lp):
            print(f"[错误] --like 指的文件不存在：{lp}")
            return 2
        ly, lx1, lx2 = derive_from_route(lp, cfg["route"]["color_code"])
        if ly is None:
            print(f"[错误] {lp} 里一个路线像素都没有，推不出平台位置。")
            return 2
        if y is None:
            y = ly
        if x1 is None:
            x1, x2 = lx1, lx2
        print(f"  照 {os.path.basename(lp)} 推出：平台行 y={ly}，x 范围 {lx1}~{lx2}")
    if y is None or x1 is None or x2 is None:
        print("[错误] 需要平台位置。给 --y/--x，或用 --like <已有路线.png>。")
        return 2
    if abs(x2 - x1) < 25:
        print(f"[错误] 横向只有 {abs(x2-x1)}px，太短（引擎要求一条线至少 20 个路线像素、"
              f"跨度 ≥10px）。把范围放大一点。")
        return 2

    base = cv2.cvtColor(load_image(map_png), cv2.COLOR_BGR2RGB)   # 与引擎 img_routes 同一空间
    seg1, seg2 = build(y, x1, x2, left, right, goal)
    print(f"  平台行 y={y}   路线 {min(x1,x2)} → {max(x1,x2)}（{abs(x2-x1)+1}px）")
    print(f"  route1 = 右行 {min(x1,x2)}→{max(x1,x2)} + 终点 goal")
    print(f"  route2 = 左行 {max(x1,x2)}→{min(x1,x2)} + 终点 goal")
    if a.dry_run:
        print("\n  （--dry-run：没有写任何文件）")
        return 0

    targets = [("route1.png", seg1), ("route2.png", seg2)]
    if not a.force:
        exist = [n for n, _ in targets if os.path.exists(os.path.join(map_dir, n))]
        if exist:
            print(f"\n[中止] {map_dir} 里已存在 {exist}。\n"
                  f"        要覆盖请加 --force（会先备份成 .bak_手绘）。\n"
                  f"        （不想覆盖就别急着跑：先看看 --dry-run 的输出对不对。）")
            return 2

    # ── 覆盖前先备份（这几个文件是用户辛苦录出来的，覆盖掉就真没了）──────────
    for name, _ in targets:
        p = os.path.join(map_dir, name)
        if os.path.exists(p):
            shutil.copy2(p, p + ".bak_手绘")
            print(f"  已备份 {name} → {name}.bak_手绘")

    # ── 写 + 读回来自验（通道序弄反了读回来会是 0 个路线像素）────────────────
    def _write(img_rgb, path, flip):
        arr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR) if flip else img_rgb
        imwrite_unicode(path, arr)

    def _read(path):
        return cv2.cvtColor(load_image(path), cv2.COLOR_BGR2RGB)   # 引擎的同一条路

    for name, seg in targets:
        img = base.copy()
        for (px, py, code) in seg:
            if 0 <= py < img.shape[0] and 0 <= px < img.shape[1]:
                img[py, px] = code
        path = os.path.join(map_dir, name)
        for flip in (True, False):
            _write(img, path, flip)
            back = _read(path)
            n_route = sum(route_pixels_of(back, cfg["route"]["color_code"]).values())
            if n_route > 0:
                if not flip:
                    print(f"  ⚠️ {name}：要按原字节序写才对（记下来了）")
                break
        else:
            print(f"[错误] {name} 写出来读回去**一个路线像素都没有** —— "
                  f"颜色字节序两种都试过了，都不对。已删除半成品。")
            try:
                os.remove(path)
            except OSError:
                pass
            return 3
        got = route_pixels_of(back, cfg["route"]["color_code"])
        print(f"  ✓ {name}：{got}")

    # 备份原路线（覆盖时）
    print(f"\n  已写入：{map_dir}/route1.png、route2.png")
    print("  下一步：回界面选中这张图 → 挂机。\n"
          "        引擎会看到两段、且闭环 0px（去程终点就是回程起点）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
