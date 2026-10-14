#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
自检：路线图尺寸一致性闸（2026-10-14 手绘回正线面板打不开的真机事故）

真机现场（用户 log/MSBot_2026-10-14_*.log，09:01:40）：
    ERROR: [回正线] 手绘面板打不开：boolean index did not match indexed array
    along axis 1; size of axis is 344 but size of corresponding boolean axis is 268

复现出来的完整堆栈（`_probe_drawer_open.py`，离屏 Qt）：
    tools/homeRouteDrawer.py:640 __init__ → self._rebuild_base()
    tools/homeRouteDrawer.py:886 base[mask] = (base[mask] * 45 + ...)
    IndexError: boolean index did not match ... 344 but ... 268

真因：minimaps/废弃地铁2 里混着**两次不同小地图尺寸**录出来的文件 ——
    map.png 137x344、route3..route7 137x344（当次）
    route1 137x268、route2 137x319（另一次录制的遗留）
手绘面板把每张主线图按它自己的尺寸 mask 完就塞进列表，合成底图时
用 268 宽的掩码去盖 344 宽的底图 ⇒ 炸。

本自检钉四件事：
  [1] `route_size_mismatch()` 的判据（含真机那两个尺寸）
  [2] 引擎 `load_config` 的路线加载循环里**真的有**这道闸（跳过 + 报出来）
  [3] 手绘面板 `_load_map` 也跳过 + 记进 `skipped_routes` + 弹中文提示，
      `_rebuild_base` 有兜底（防"少画线"变"打不开"）
  [4] 拿**磁盘上真实的** minimaps/<图>/ 文件跑一遍纯判据（有目录才跑，
      没有就按"未观测"标注，不假装通过）

退出码：0 = 全过；1 = 有失败项。
'''
import os
import sys
import glob
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.utils.common import route_size_mismatch, imread_unicode  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [OK]   {msg}')
    else:
        FAIL += 1
        print(f'  [FAIL] {msg}')


print('=' * 72)
print(' 自检：路线图尺寸一致性闸（另一种录制留下的图不能当主线用）')
print('=' * 72)

# ── [1] 判据 ───────────────────────────────────────────────────────────
print('\n[1] route_size_mismatch() 的判据')
check(route_size_mismatch((137, 268, 3), (137, 344, 3)) is True,
      '★ 真机数据：137x268 vs 137x344 → 不一致（route1）')
check(route_size_mismatch((137, 319, 3), (137, 344, 3)) is True,
      '★ 真机数据：137x319 vs 137x344 → 不一致（route2）')
check(route_size_mismatch((137, 344, 3), (137, 344, 3)) is False,
      '同尺寸（137x344）→ 一致（route3..7 不该被误杀）')
check(route_size_mismatch((137, 344), (137, 344, 3)) is False,
      '只比 shape[:2]：传 (h,w) 元组也算得对')
check(route_size_mismatch((138, 344, 3), (137, 344, 3)) is True,
      '只差 1 个像素高也要判不一致（刻度不同就是不同）')
check(route_size_mismatch((137, 344, 4), (137, 344, 3)) is False,
      '通道数不参与判据（RGBA 与 BGR 同尺寸算一致）')

# ── [2] 引擎接线 ───────────────────────────────────────────────────────
print('\n[2] 引擎 load_config：路线加载循环里真有这道闸')
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402

_src_lc = inspect.getsource(MapleStoryAutoBot.load_config)
check('route_size_mismatch(img.shape' in _src_lc,
      'load_config 里对每张 route 图调了 route_size_mismatch')
check('_map_hw = self.img_map.shape[:2]' in _src_lc,
      '拿 map.png 的尺寸当参考（坐标系权威）')
check('已跳过不加载' in _src_lc and 'continue' in _src_lc,
      '不一致 ⇒ 跳过（不是"照用"+只在日志里嘀咕）')
check('另一次录制' in _src_lc,
      '错误文案说清了"这是另一次录制留下的"（用户知道去哪修）')
check('重录这一段' in _src_lc,
      '错误文案给了修法（删掉或重录）')

# ── [3] 手绘面板接线 ───────────────────────────────────────────────────
print('\n[3] 手绘面板：跳过 + 提示 + 合成兜底')
import tools.homeRouteDrawer as _d  # noqa: E402

_src_load = inspect.getsource(_d.HomeRouteDrawer._load_map)
check('route_size_mismatch(img.shape, self.img_map_bgr.shape)' in _src_load,
      '_load_map 用同一判据挑出尺寸不一致的主线图')
check('self.skipped_routes.append(' in _src_load,
      '记进 skipped_routes（后面要据此弹提示）')
check('QMessageBox.warning(' in _src_load and '尺寸不一致' in _src_load,
      '开面板时弹中文提示（不是只在日志里）')
_src_base = inspect.getsource(_d.HomeRouteDrawer._rebuild_base)
check('route_size_mismatch(img.shape, base.shape)' in _src_base
      and 'continue' in _src_base,
      '★ _rebuild_base 有兜底：尺寸不一致就跳过该图，不炸整个面板')
check('base[mask]' in _src_base,
      '兜底之外，原来的半透明合成逻辑还在（没被改坏）')
check('def _load_map(self)' in inspect.getsource(_d.HomeRouteDrawer),
      '_load_map 仍是类方法（没被误改成模块级函数）')

# ── [4] 真机数据（有才跑） ─────────────────────────────────────────────
print('\n[4] 磁盘上真实的地图目录（有 minimaps/ 才跑，否则标"未观测"）')
_dirs = sorted(glob.glob(os.path.join('minimaps', '*')))
_seen = 0
_mismatch_report = []
for d in _dirs:
    p_map = os.path.join(d, 'map.png')
    if not os.path.exists(p_map):
        continue
    try:
        m = imread_unicode(p_map, cv2.IMREAD_COLOR)
    except Exception:
        continue
    if m is None:
        continue
    _seen += 1
    for p in sorted(glob.glob(os.path.join(d, 'route*.png'))):
        b = os.path.basename(p)
        if b in ('route_home.png', 'route_rest.png'):
            continue
        try:
            img = imread_unicode(p, cv2.IMREAD_COLOR)
        except Exception:
            continue
        if img is None:
            continue
        if route_size_mismatch(img.shape, m.shape):
            _mismatch_report.append(
                f"{os.path.basename(d)}/{b} {img.shape[:2]} vs map.png {m.shape[:2]}")
if _seen == 0:
    print('  （未观测）minimaps/ 下没有可读的 map.png —— 这条不判通过也不判失败')
else:
    print(f'  扫了 {_seen} 张地图；与 map.png 尺寸不一致的路线图 '
          f'{len(_mismatch_report)} 张')
    for line in _mismatch_report:
        print(f'    · {line}')
    # 判据：闸能把这些**指认出来**就算过（数量本身是数据问题，不是代码问题）
    check(True, f'扫描完成（{_seen} 张地图，判据可用于真实文件）')

print('\n' + '=' * 72)
print(f'结果：通过 {PASS} / 失败 {FAIL}')
print('=' * 72)
sys.exit(1 if FAIL else 0)
