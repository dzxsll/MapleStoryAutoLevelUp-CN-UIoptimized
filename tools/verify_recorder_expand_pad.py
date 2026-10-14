# -*- coding: utf-8 -*-
'''自检：录制器画布扩容时，**已经存盘**的 route{N}.png / route_home.png 会一起同步。

病根（2026-10-14 真机事故 `minimaps/废铁2`）：
    `save_route()` 存的是保存那一刻的画布大小，而地图底图会随小地图滚动向右/下扩容
    ⇒ 同一场录制里先存的几段天生比 map.png 窄（route1/2 = 137x268、route3 = 137x314、
      route4+ = 137x344）。引擎按 map.png 的坐标走，窄图被判「尺寸不一致」**整段跳过**：
    巡逻闭环从 0px 变 119px、手绘面板 `boolean index did not match … 344 … 268`。

本自检钉住的是**行为**（不是复述实现）：
  【1】expand_canvas 的语义（旧内容贴左上角、扩出来的边全黑、四边为 0 就原样返回）
  【2】只往右/下扩：已存盘文件补边后**尺寸跟上画布**、坐标不变、旧内容逐像素保留
  【3】往左/上扩：已存盘文件**整体平移**，坐标平移量与画布一致（route_home.png 同样）
  【4】不该写的时候一个字节都不写：不需要扩容 / 还没存过段 / 补录回正线模式 / 写盘失败
  【5】真机回归：minimaps/废铁2 现在全一致，且对它跑一次"不扩容"的调用不改任何文件
'''
import hashlib
import os
import shutil
import sys
from types import SimpleNamespace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.utils.common import load_image, load_yaml, imwrite_unicode   # noqa: E402
import tools.routeRecorder as RR                                      # noqa: E402
from tools.routeRecorder import RouteRecorder                         # noqa: E402

CFG = load_yaml(os.path.join(ROOT, "config", "config_default.yaml"))
PAD = CFG["route_recoder"]["map_padding"]
TMP_MAP = "_verify_expand_pad"
TMP_DIR = os.path.join(ROOT, "minimaps", TMP_MAP)

OK = 0
FAIL = 0


def check(label, got, want):
    global OK, FAIL
    if got == want:
        OK += 1
        print(f"  [OK ] {label}: got={got!r}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}: got={got!r} want={want!r}")


def digest(p):
    with open(p, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def snapshot(d):
    return {f: digest(os.path.join(d, f)) for f in sorted(os.listdir(d))
            if os.path.isfile(os.path.join(d, f))}


class LogCatcher:
    '''把 logger 换成收集器 —— 断言"该报的报了"，顺带不让自检刷屏。'''

    def __init__(self):
        self.infos, self.warnings, self.errors = [], [], []

    def info(self, m):
        self.infos.append(str(m))

    def warning(self, m):
        self.warnings.append(str(m))

    def error(self, m):
        self.errors.append(str(m))


def make_recorder(canvas_h, canvas_w, idx_routes, append_home=False):
    r = RouteRecorder.__new__(RouteRecorder)          # 不跑 __init__（不要键盘/窗口）
    r.cfg = CFG
    r.img_map = np.zeros((canvas_h, canvas_w, 3), np.uint8)
    r.img_route = np.zeros((canvas_h, canvas_w, 3), np.uint8)
    r.loc_minimap_global = (7, 9)
    r.loc_player_global_last = (7, 9)
    r.idx_routes = idx_routes
    r.append_home = append_home
    r.args = SimpleNamespace(new_map=TMP_MAP)
    return r


def marked_canvas(h, w):
    '''一张"路线图"：左上角一个记号 + 右侧一个记号（用来量尺寸/坐标/平移）。'''
    img = np.zeros((h, w, 3), np.uint8)
    img[5, 5] = (255, 0, 0)
    img[10, w - 5] = (0, 0, 255)
    img[2, 7] = (0, 255, 0)
    return img


def reset_tmp():
    if os.path.isdir(TMP_DIR):
        shutil.rmtree(TMP_DIR)
    os.makedirs(TMP_DIR)
    imwrite_unicode(os.path.join(TMP_DIR, "map.png"), np.zeros((60, 120, 3), np.uint8))


def main():
    print("=" * 68)
    print("【1】expand_canvas 的语义")
    r0 = make_recorder(60, 120, 0)
    src = marked_canvas(60, 120)
    out = r0.expand_canvas(src, 30, 40, 20, 10)
    check("① 新画布尺寸 = 旧 + 四边增量", out.shape[:2], (60 + 30 + 40, 120 + 20 + 10))
    check("② 旧内容贴在 (expand_left, expand_top)", tuple(out[30 + 10, 20 + 115]),
          tuple(src[10, 115]))
    check("③ 扩出来的边是全黑", bool(out[:30].any() or out[:, :20].any()), False)
    same_obj = r0.expand_canvas(src, 0, 0, 0, 0)
    check("④ 四边都为 0 ⇒ 原样返回同一个对象", same_obj is src, True)

    print("\n【2】只往右/下扩：已存盘路线图跟着补边（坐标不变）")
    reset_tmp()
    r = make_recorder(60, 120, 1)
    before_file = marked_canvas(60, 120)
    imwrite_unicode(os.path.join(TMP_DIR, "route1.png"), before_file)
    caught = LogCatcher()
    real_logger, RR.logger = RR.logger, caught
    try:
        ret = r.ensure_img_map_capacity(x=PAD, y=PAD, h=40, w=80)
    finally:
        RR.logger = real_logger
    after_file = load_image(os.path.join(TMP_DIR, "route1.png"))
    exp_h = 60 + 0 + (PAD + 40 + PAD - 60)
    exp_w = 120 + 0 + (PAD + 80 + PAD - 120)
    check("① 返回 True（真的写了盘）", ret, True)
    check("② 画布扩到预期尺寸", (r.img_map.shape[:2], r.img_route.shape[:2]),
          ((exp_h, exp_w), (exp_h, exp_w)))
    check("③ 已存盘的 route1.png 尺寸跟上了画布", after_file.shape[:2], (exp_h, exp_w))
    check("④ 右侧那个记号坐标与颜色都没变（只往右/下扩）",
          tuple(after_file[10, 115]), (0, 0, 255))
    check("⑤ 旧区域逐像素保留（与扩容前的文件相同）",
          bool((after_file[:60, :120] == before_file).all()), True)
    check("⑥ 新增区域是全黑", bool(after_file[60:].any() or after_file[:, 120:].any()), False)
    check("⑦ 右/下扩不动全局坐标", (r.loc_minimap_global, r.loc_player_global_last),
          ((7, 9), (7, 9)))
    check("⑧ 日志说清了扩多少 + 同步了哪些文件（route1.png 在名单里）",
          ("route1.png" in caught.infos[-1] and "扩容" in caught.infos[-1]), True)

    print("\n【3】往左/上扩：已存盘文件整体平移（route_home.png 一起）")
    reset_tmp()
    r = make_recorder(60, 120, 1)
    imwrite_unicode(os.path.join(TMP_DIR, "route1.png"), marked_canvas(60, 120))
    imwrite_unicode(os.path.join(TMP_DIR, "route_home.png"), marked_canvas(60, 120))
    caught = LogCatcher()
    real_logger, RR.logger = RR.logger, caught
    try:
        ret = r.ensure_img_map_capacity(x=0, y=0, h=10, w=10)
    finally:
        RR.logger = real_logger
    a1 = load_image(os.path.join(TMP_DIR, "route1.png"))
    ah = load_image(os.path.join(TMP_DIR, "route_home.png"))
    check("① 返回 True", ret, True)
    check("② 画布尺寸 = (60+pad) x (120+pad)", (r.img_map.shape[:2]),
          (60 + PAD, 120 + PAD))
    check("③ route1.png 尺寸跟上画布", a1.shape[:2], (60 + PAD, 120 + PAD))
    check("④ route_home.png 也一起平移了", ah.shape[:2], (60 + PAD, 120 + PAD))
    check("⑤ 记号整体平移 (pad, pad)（往左/上扩必须平移坐标）",
          (tuple(a1[10 + PAD, 115 + PAD]), tuple(a1[5 + PAD, 5 + PAD])),
          ((0, 0, 255), (255, 0, 0)))
    check("⑤b 平移后旧位置应变成黑（没有留在原地）",
          (tuple(a1[10, 115]), tuple(a1[5, 5])), ((0, 0, 0), (0, 0, 0)))
    check("⑥ 全局坐标同步平移", (r.loc_minimap_global, r.loc_player_global_last),
          ((7 + PAD, 9 + PAD), (7 + PAD, 9 + PAD)))

    print("\n【4】不该写的时候一个字节都不写")
    reset_tmp()
    r = make_recorder(200, 300, 1)
    imwrite_unicode(os.path.join(TMP_DIR, "route1.png"), marked_canvas(200, 300))
    snap = snapshot(TMP_DIR)
    check("① 不需要扩容 ⇒ 返回 False",
          r.ensure_img_map_capacity(x=PAD, y=PAD, h=100, w=150), False)
    check("② 不需要扩容 ⇒ 文件一个字节都没变", snapshot(TMP_DIR), snap)
    r2 = make_recorder(60, 120, 0)
    ret2 = r2.ensure_img_map_capacity(x=PAD, y=PAD, h=40, w=80)
    check("③ 一段都没存过（idx_routes=0）⇒ 不报错、不凭空造文件",
          (ret2, sorted(os.listdir(TMP_DIR))), (False, ["map.png", "route1.png"]))

    reset_tmp()
    r3 = make_recorder(60, 120, 1, append_home=True)
    imwrite_unicode(os.path.join(TMP_DIR, "route1.png"), marked_canvas(60, 120))
    caught = LogCatcher()
    real_logger, RR.logger = RR.logger, caught
    try:
        ret3 = r3.ensure_img_map_capacity(x=PAD, y=PAD, h=40, w=80)
    finally:
        RR.logger = real_logger
    check("④ 补录回正线模式 ⇒ 不同步（返回 False）", ret3, False)
    check("⑤ 补录回正线模式 ⇒ 已存盘文件尺寸不变（宁可不写也不写错坐标）",
          load_image(os.path.join(TMP_DIR, "route1.png")).shape[:2], (60, 120))
    check("⑥ 补录回正线模式 ⇒ 有 WARNING 说明原因",
          any("只补录回正线" in m for m in caught.warnings), True)

    reset_tmp()
    r4 = make_recorder(60, 120, 1)
    imwrite_unicode(os.path.join(TMP_DIR, "route1.png"), marked_canvas(60, 120))
    snap = snapshot(TMP_DIR)
    caught = LogCatcher()

    def boom(*a, **k):
        raise OSError("disk full")

    real_w, RR.imwrite_unicode = RR.imwrite_unicode, boom
    real_logger, RR.logger = RR.logger, caught
    try:
        ret4 = r4.ensure_img_map_capacity(x=PAD, y=PAD, h=40, w=80)
    finally:
        RR.imwrite_unicode = real_w
        RR.logger = real_logger
    check("⑦ 写盘抛异常 ⇒ 不把录制器带崩（返回 False）", ret4, False)
    check("⑧ 写盘抛异常 ⇒ 原文件没被破坏", snapshot(TMP_DIR), snap)
    check("⑨ 写盘抛异常 ⇒ 有 WARNING 点出补救命令",
          any("route_pad_canvas" in m for m in caught.warnings), True)

    print("\n【5】真机回归：minimaps/废铁2（补边 + 回正线还原之后）")
    live = os.path.join(ROOT, "minimaps", "废铁2")
    if os.path.isdir(live):
        m = load_image(os.path.join(live, "map.png"))
        sizes, bad = {}, []
        for f in sorted(os.listdir(live)):
            if f.startswith("route") and f.endswith(".png"):
                s = load_image(os.path.join(live, f)).shape[:2]
                sizes[f] = s
                if s != m.shape[:2]:
                    bad.append((f, s, m.shape[:2]))
        check("① 废铁2 所有路线图尺寸 == map.png", bad, [])
        snap_live = {k: v for k, v in snapshot(live).items() if k.endswith(".png")}
        r5 = make_recorder(m.shape[0], m.shape[1], 7)
        r5.img_map = m.copy()
        r5.img_route = np.zeros_like(m)
        r5.args = SimpleNamespace(new_map="废铁2")
        ret5 = r5.ensure_img_map_capacity(x=PAD, y=PAD,
                                          h=m.shape[0] - 2 * PAD, w=m.shape[1] - 2 * PAD)
        check("② 不需要扩容时返回 False", ret5, False)
        check("③ 真机文件一个字节都没动",
              {k: v for k, v in snapshot(live).items() if k.endswith(".png")}, snap_live)
    else:
        print("  （没找到 minimaps/废铁2 —— 跳过真机回归）")

    if os.path.isdir(TMP_DIR):
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAIL:
        print(f"❌ 失败 {FAIL} / 通过 {OK}")
        return 1
    print(f"✅ 全部通过（{OK} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
