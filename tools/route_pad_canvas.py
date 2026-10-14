# -*- coding: utf-8 -*-
'''把**窄画布**的路线图补边到 `map.png` 的画布尺寸（2026-10-14 加）。

## 它解决什么问题

录制器保存 `route{N}.png` 用的是**当时**的画布大小；而地图底图会随着小地图滚动
**扩容**（见 `routeRecorder.ensure_img_map_capacity`）。于是同一场录制里前几段可能
是 137x268、后几段是 137x344。引擎按 `map.png` 的坐标系对齐，尺寸不一致的那几段被
`[路线校验]` 直接跳过 —— 症状是"路线少了几段、闭环还断了（其实录制时闭环是 0px）"；
手绘回正线面板更会直接抛
`boolean index did not match indexed array along axis 1; size of axis is 344 but ... 268`。

真机样本（2026-10-14 `minimaps/废铁2`）：录制器日志 `[巡逻闭环] 最后一段终点离第一段
0px，闭环正常 ✓`，引擎加载却报 `119px` —— 差别**全部**来自这三个窄画布文件被跳过。

## 判据：为什么可以"纯补边"，绝不做缩放

`expand_canvas()` 扩容时是把旧内容**原样贴到左上角**、在右下补黑边，并同步平移
`loc_*_global` ⇒ 窄画布里的像素坐标**本来就是最终坐标系里的坐标**。
所以正确的修法是"右下补黑边"；**缩放会把整条路线搬到错误位置**（那是静默失准，
比"跳过不加载"危险得多）。

本工具不靠这个推理下结论，而是**验证**：`route{N}.png` 本身就是「地图底图 + 路线像素」
（见 `tools/routeRebuild.py` 开头）。把路线色剥掉得到底图，拿它去 `map.png` 里做模板匹配，
必须**正好落在 (0,0)**（相似度 ≥ `SIM_MIN`、明显不同像素 ≤ `DIFF_MAX`）才允许补边；
偏了 ⇒ 说明那次扩容是往左/往上扩的，补边会把路线搬错 ⇒ 判「无法判定」，**一个字节都不写**。

## 用法

    python -m tools.route_pad_canvas --map 废铁2              # 体检（默认不写盘）
    python -m tools.route_pad_canvas --map 废铁2 --apply       # 真修（先备份到 _pad_backup_<时间>/）
    python -m tools.route_pad_canvas --all                    # 扫所有地图目录（默认跳过 *.bak）
    python -m tools.route_pad_canvas --map 废铁2 --restore      # 从最近一次备份原样还原

退出码：0 = 没有待修的 / 全部修好；1 = 有文件被判「无法判定」或写盘后自检没过（不静默）；
        2 = 环境不对（没有 map.png 之类）。
'''
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import sys
import time
from collections import namedtuple

import cv2
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import (  # noqa: E402
    active_config_path, imwrite_unicode, load_image, load_yaml, override_cfg,
)
from src.utils.paths import resource_path  # noqa: E402


def load_merged_cfg():
    '''默认配置 + 当前方案（`config_custom.yaml` 是**差分**文件，不能单独用）。

    ⚠️ 坑：`active_config_path()` 指向的方案里通常**没有** `route.color_code`
       （那是默认配置里的表，用户没改过就不会被写进差分）——
       直接拿它当完整配置，色码表就是空的，路线像素一个都认不出来。
    '''
    cfg = load_yaml(os.path.join("config", "config_default.yaml"))
    act = active_config_path()
    if act:
        override_cfg(cfg, load_yaml(act))
    return cfg

# ── 判据（**某时点观测值**：2026-10-14 真机样本 6 个窄画布文件全部在 (0,0) 对齐，
#    相似度 0.95~0.97，明显不同像素 0.16~0.23% —— 那 0.2% 就是小地图上会动的角色点。
#    换游戏窗口大小 / 换小地图后要重新看一眼这两个数。）────────────────────────
SIM_MIN = 0.90      # 底图与 map.png 的最小相似度
DIFF_MAX = 0.03     # 允许的"明显不同像素"占比上限
DIFF_PIX = 40       # 单通道差多少算"明显不同"

BACKUP_PREFIX = "_pad_backup_"

AlignVerdict = namedtuple("AlignVerdict", "ok dx dy sim diff_frac reason")


# ── 色码表 ────────────────────────────────────────────────────────────────────
def find_color_code(cfg, _top=True):
    '''从配置里递归找 color_code 表（键形如 "255,0,0"，值是 "left none none"）。

    ⚠️ 不写死层级：这张表在 config_default.yaml 里是 `route.color_code`，
       但配置方案可以覆盖 / 搬家。最外层找不到就抛 —— 别默默当空表，
       空表的后果是"一个路线像素都认不出来"，会把好文件也判成"底图全黑"。
    ⚠️ 递归时必须 `return None` 而不是抛：`cfg` 里第一个 dict 子树（如 `bot`）
       里没有这张表是**正常**的，抛出去会让整次查找在第一个兄弟节点就炸。
    '''
    for _k, _v in cfg.items():
        if isinstance(_v, dict):
            if "255,0,0" in _v:
                return _v
            found = find_color_code(_v, _top=False)
            if found:
                return found
    if _top:
        raise KeyError("配置里找不到 color_code 表（route.color_code）")
    return None


def codes_bgr(cfg):
    '''色码表的**文件字节序**版本。

    录制器是"反着存"的（写入前 RGB→BGR，引擎读入再 BGR→RGB，两次抵消 ——
    见 `routeRecorder._stamp_goal` 的长注释）。所以要按文件里的字节序去比，
    用配置里的 RGB 原值去比会一个像素都匹配不上。
    '''
    return [tuple(int(x) for x in k.split(","))[::-1] for k in find_color_code(cfg)]


def route_mask(img, codes):
    '''哪些像素是"路线像素"（跳跃/传送/移动/goal 都算）。'''
    hit = np.zeros(img.shape[:2], dtype=bool)
    for c in codes:
        hit |= (img[:, :, :3] == np.asarray(c, dtype=np.uint8)).all(axis=2)
    return hit


# ── 对齐判定 ──────────────────────────────────────────────────────────────────
def check_align(img, ref, codes):
    '''窄画布能不能"右下补黑边"补成 ref —— 用**底图对齐**验证，不靠假设。

    Returns:
        AlignVerdict: ok=True 才能补边；否则 reason 说明为什么不能修（fail-closed）。
    '''
    if img.shape[:2] == ref.shape[:2]:
        return AlignVerdict(True, 0, 0, 1.0, 0.0, "尺寸已一致")
    if img.shape[0] > ref.shape[0] or img.shape[1] > ref.shape[1]:
        return AlignVerdict(False, 0, 0, 0.0, 1.0, "比 map.png 还大 —— 不是「窄画布」，补边修不了")

    bg = img[:, :, :3].copy()
    bg[route_mask(bg, codes)] = 0                     # 剥掉路线色，只留底图
    nz = bg.max(axis=2) > 0
    if not nz.any():
        return AlignVerdict(False, 0, 0, 0.0, 1.0, "剥掉路线色后底图是全黑 —— 判不了对齐")

    ys = np.where(nz.any(axis=1))[0]
    xs = np.where(nz.any(axis=0))[0]
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    tpl = bg[y0:y1 + 1, x0:x1 + 1]
    res = cv2.matchTemplate(ref[:, :, :3], tpl, cv2.TM_CCOEFF_NORMED)
    _, sim, _, loc = cv2.minMaxLoc(res)
    bx, by = int(loc[0]), int(loc[1])
    dx, dy = bx - x0, by - y0
    region = ref[by:by + tpl.shape[0], bx:bx + tpl.shape[1], :3]
    diff = np.abs(region.astype(np.int16) - tpl.astype(np.int16)).max(axis=2)
    usable = tpl.max(axis=2) > 0
    frac = float(((diff > DIFF_PIX) & usable).sum()) / max(1, int(usable.sum()))
    sim = float(sim)

    if sim < SIM_MIN or frac > DIFF_MAX:
        return AlignVerdict(False, dx, dy, sim, frac,
                            f"底图与 map.png 对不上（相似度 {sim:.3f} < {SIM_MIN} 或"
                            f" 明显不同 {frac * 100:.2f}% > {DIFF_MAX * 100:.0f}%）"
                            f" —— 可能不是同一次录制 / 不是同刻度")
    if dx != 0 or dy != 0:
        return AlignVerdict(False, dx, dy, sim, frac,
                            f"底图在 map.png 里偏了 (dx={dx}, dy={dy})：那次扩容是往**左/上**扩的，"
                            f"补边会把整条路线搬错位置 —— 要改的是坐标平移，本工具不干")
    return AlignVerdict(True, 0, 0, sim, frac, "底图在 (0,0) 对齐 ✓")


def pad_to_ref(img, ref_shape):
    '''右下补黑边（与录制器 `expand_canvas` 的做法一致：新区域就是黑的）。'''
    out = np.zeros((ref_shape[0], ref_shape[1], 3), dtype=np.uint8)
    h = min(img.shape[0], ref_shape[0])
    w = min(img.shape[1], ref_shape[1])
    out[:h, :w] = img[:h, :w, :3]
    return out


# ── 计划 / 执行 ───────────────────────────────────────────────────────────────
def _route_sort_key(path):
    '''route1 < route2 < ... < route10 < route_home（只是给报告排个序）。'''
    name = os.path.basename(path)
    if name == "route_home.png":
        return (2, 0, name)
    m = re.match(r"route(\d+)\.png$", name)
    return (1, int(m.group(1)) if m else 9999, name)


def list_route_files(map_dir):
    return sorted(glob.glob(os.path.join(map_dir, "route*.png")), key=_route_sort_key)


def plan_map(map_name, cfg):
    '''体检一张图，**不写任何文件**。

    Returns:
        dict: {map_dir, ref, ref_shape, jobs, ok_files, refused, missing}
              jobs = [(path, name, img, verdict)]（verdict.ok 才是待修）
    '''
    map_dir = resource_path(os.path.join("minimaps", map_name))
    ref_path = os.path.join(map_dir, "map.png")
    if not os.path.exists(ref_path):
        return {"map_dir": map_dir, "ref": None, "ref_shape": None,
                "jobs": [], "ok_files": [], "refused": [], "missing": ref_path}
    ref = load_image(ref_path)
    if ref is None:
        return {"map_dir": map_dir, "ref": None, "ref_shape": None,
                "jobs": [], "ok_files": [], "refused": [], "missing": ref_path}
    codes = codes_bgr(cfg)

    jobs, ok_files, refused = [], [], []
    for path in list_route_files(map_dir):
        img = load_image(path)
        name = os.path.basename(path)
        if img is None:
            refused.append((name, "读不出来（文件坏了？）"))
            continue
        v = check_align(img, ref, codes)
        if img.shape[:2] == ref.shape[:2]:
            ok_files.append((name, v))
        elif v.ok:
            jobs.append((path, name, img, v))
        else:
            refused.append((name, v.reason))
    return {"map_dir": map_dir, "ref": ref, "ref_shape": ref.shape[:2],
            "jobs": jobs, "ok_files": ok_files, "refused": refused, "missing": None}


def report_map(map_name, plan, applied=False):
    print(f"\n== {map_name}")
    if plan["missing"]:
        print(f"   ✗ 没有 map.png（{plan['missing']}）—— 这张图没有底图，补边没有参照系")
        return
    print(f"   参照画布 map.png = {plan['ref_shape']}")
    for name, v in plan["ok_files"]:
        print(f"   ✓ {name:16s} 已一致")
    for _p, name, _img, v in plan["jobs"]:
        act = "已补边" if applied else "待补边"
        print(f"   → {name:16s} {act}：相似度 {v.sim:.3f} / 明显不同 {v.diff_frac * 100:.2f}%"
              f" / 偏移 (dx={v.dx}, dy={v.dy})")
    for name, why in plan["refused"]:
        print(f"   ✗ {name:16s} 无法判定：{why}")


def apply_map(map_name, plan, codes, log=print):
    '''补边写盘：先整目录备份，再逐文件"补边 + 写 + 重新读回来验"。

    验证三件事（任一条不过就**从备份还原**并报错，绝不留下半成品）：
      ① 尺寸 == map.png；② 路线像素**数量不变**；③ 路线像素**坐标不变**。
    '''
    ref_shape = plan["ref_shape"]
    map_dir = plan["map_dir"]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak_dir = os.path.join(map_dir, f"{BACKUP_PREFIX}{stamp}")
    os.makedirs(bak_dir, exist_ok=True)
    backup = {}
    for path, name, img, _v in plan["jobs"]:
        dst = os.path.join(bak_dir, name)
        shutil.copy2(path, dst)
        backup[path] = dst

    ok = True
    for path, name, img, _v in plan["jobs"]:
        before = route_mask(img, codes)
        out = pad_to_ref(img, ref_shape)
        if not imwrite_unicode(path, out):
            log(f"   ✗ {name} 写盘失败 —— 请从 {bak_dir} 还原")
            ok = False
            continue
        back = load_image(path)
        if back is None or back.shape[:2] != ref_shape:
            log(f"   ✗ {name} 写盘后尺寸不对（{None if back is None else back.shape}）"
                f" —— 从备份还原")
            shutil.copy2(backup[path], path)
            ok = False
            continue
        after = route_mask(back, codes)
        same_count = int(after.sum()) == int(before.sum())
        # 坐标不变 = 补边后左上角那一块与原来逐像素相同、新增区域没有路线像素
        same_pos = bool(np.array_equal(after[:before.shape[0], :before.shape[1]], before)) and \
            not after[:before.shape[0], before.shape[1]:].any() and \
            not after[before.shape[0]:, :].any()
        if not (same_count and same_pos):
            log(f"   ✗ {name} 自检没过（路线像素 {int(before.sum())} → {int(after.sum())}，"
                f"坐标{'未变' if same_pos else '被搬动了'}）—— 从备份还原")
            shutil.copy2(backup[path], path)
            ok = False
            continue
        log(f"   ✓ {name} 已补边 → {back.shape[:2]}（路线像素 {int(after.sum())} 个，坐标未变）")
    log(f"   备份：{bak_dir}")
    return ok


def restore_map(map_name, log=print):
    '''从**最近一次**备份原样还原（补边是可逆的：备份是逐文件 copy2）。'''
    map_dir = resource_path(os.path.join("minimaps", map_name))
    baks = sorted(glob.glob(os.path.join(map_dir, f"{BACKUP_PREFIX}*")))
    if not baks:
        log(f"   ✗ {map_name} 没有 {BACKUP_PREFIX}* 备份目录")
        return False
    bak = baks[-1]
    n = 0
    for f in sorted(os.listdir(bak)):
        src = os.path.join(bak, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(map_dir, f))
            n += 1
    log(f"   ✓ 已从 {bak} 还原 {n} 个文件")
    return True


def iter_map_dirs(include_backup=False):
    root = resource_path("minimaps")
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or name.startswith("_"):
            continue
        if name.endswith(".bak") and not include_backup:
            continue
        if not os.path.exists(os.path.join(d, "map.png")):
            continue
        yield name


def main(argv=None):
    ap = argparse.ArgumentParser(description="把窄画布的路线图补边到 map.png 的尺寸")
    ap.add_argument("--map", default=None, help="地图名（minimaps/<名字>）")
    ap.add_argument("--all", action="store_true", help="扫所有地图目录")
    ap.add_argument("--apply", action="store_true", help="真写盘（默认只体检）")
    ap.add_argument("--restore", action="store_true", help="从最近一次备份还原")
    ap.add_argument("--include-backup", action="store_true",
                    help="--all 时连 *.bak 目录一起扫")
    args = ap.parse_args(argv)

    if not args.map and not args.all:
        ap.print_help()
        return 2
    cfg = load_merged_cfg()
    codes = codes_bgr(cfg)

    names = [args.map] if args.map else list(iter_map_dirs(args.include_backup))
    if not names:
        print("没有可体检的地图目录")
        return 0

    if args.restore:
        if not args.map:
            print("--restore 要配合 --map 用（还原哪张图得说清楚）")
            return 2
        return 0 if restore_map(args.map) else 1

    bad = 0
    printed = 0
    for name in names:
        try:
            plan = plan_map(name, cfg)
        except Exception as e:                                    # noqa: BLE001
            print(f"\n== {name}\n   ✗ 体检失败：{e}")
            bad += 1
            continue
        todo = bool(plan["jobs"])
        if args.all and not todo and not plan["refused"] and not plan["missing"]:
            continue                                   # --all 只报"有事"的图
        report_map(name, plan, applied=False)
        printed += 1
        if plan["missing"]:
            bad += 1            # 点名要修的图却没有 map.png ⇒ 判不了，按 fail-closed 记账
        if plan["refused"]:
            bad += len(plan["refused"])
        if todo and args.apply:
            if not apply_map(name, plan, codes):
                bad += 1
            else:
                report_map(name, plan_map(name, cfg), applied=True)
    if printed == 0:
        print("全部地图的路线图尺寸都与 map.png 一致 ✓")
    print(f"\n=== {'有需要人看的项' if bad else '没有无法判定的项'}：{bad} ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
