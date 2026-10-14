# -*- coding: utf-8 -*-
'''名字定位失败帧的**离线判定工具**（2026-10-12 加）。

## 它回答什么

引擎在连续认不出角色名名牌时，会把**那一帧原图**落到 `debug/名字定位失败/`
（见 `MapleStoryAutoLevelUp._dump_nametag_failure`）。本工具逐帧给出**结论**：

    分数是多少 → 最佳候选在哪 → 和"画面里根本没有名牌"的地板值差多少 → 所以到底怎么回事

而不是丢一堆截图让人自己看。

## 为什么不能只看分数

分数低有两种**完全不同**的病因，靠这一个数分不开：

  · **名牌在画面里、但匹配不上** ⇒ 模板/阈值/算法的问题（该改匹配链）；
  · **画面里压根没有名牌** ⇒ 模板再准也没用（该查为什么不可见：人堆挡住 / 前景挡住 /
    切图黑屏 / 角色不可见…）。

区分办法是**地板值**：把"最佳分数"和"离它很远的那些候选的分数"比 ——
前者明显更低 ⇒ 真找到了；两者接近 ⇒ 最佳那个也只是"画面里最像的一块杂色"，
即**没有名牌**。地板值由画面自己决定，不需要人工定阈值。

## 复刻问题

预处理走的是 `src/utils/common.prep_nametag_images` —— **引擎用的同一个函数**。
自己复刻一份的话，"工具说没事、真机就是认不出"这种分裂最难查（项目纪律）。

用法：
    python -m tools.nametag_diag --dir debug/名字定位失败
    python -m tools.nametag_diag --frame "debug/名字定位失败/xxx_frame.png"
    python -m tools.nametag_diag --dir <目录> --mode histogram_eq --no-save
'''
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import (load_yaml, load_image, get_mask,           # noqa: E402
                              prep_nametag_images, imwrite_unicode,
                              active_config_path, override_cfg,
                              crop_frame_to_client, resolve_title_bar_height)

# 判定用的比例：真命中时"最佳"应当明显低于地板值。跨图实测 0.0007 vs 0.0214 = 30 倍，
# 留足余量取 5 倍 —— 低于它就说明"最佳"并不突出，多半只是画面里最像的一块杂色。
GOOD_RATIO = 5.0


def crop_to_client(frame, client):
    """把**整窗**抓帧裁成游戏客户区（诊断专用）。

    背景：`debug/capture_raw_*.png` 这类抓帧是**整窗**（含标题栏 + 左右边框，
    实测 1368x800），而引擎处理的是**客户区**（1366x768）。不裁的话
    `ui_y_start=680` 这个搜索区会整体错位，分数全是垃圾。

    几何：横向左右边框等宽 → 居中裁；纵向余下的都算标题栏（含上边框）。
    ⚠️ 引擎那条路走的是 `src/utils/common.crop_frame_to_client`；这里自己算是因为
       那个函数的返回契约（可能带诊断信息）与本工具只需要一张图不同 ——
       本函数**只**用于离线诊断，不参与挂机。
    """
    cw, ch = int(client[0]), int(client[1])
    h, w = frame.shape[:2]
    if (w, h) == (cw, ch):
        return frame
    if w < cw or h < ch:
        return None
    bx = (w - cw) // 2
    by = h - ch - bx
    if by < 0 or by + ch > h:
        return None
    return frame[by:by + ch, bx:bx + cw]


def non_overlapping_topk(res, th, tw, k=5):
    """从 SQDIFF 结果图里取前 k 个**互不重叠**的最优位置 → [(x, y, score), ...]。

    为什么要互不重叠：同一个真位置周围一大片都会是低分（模板滑动一个像素分数变化很小），
    只看前几个排序结果会全是同一个地方的邻居，看不出"还有没有别的地方也很像"。
    """
    flat = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0).ravel()
    order = np.argsort(flat)[:min(4000, flat.size)]
    picked = []
    for oi in order:
        y, x = divmod(int(oi), res.shape[1])
        if all(abs(x - px) >= tw or abs(y - py) >= th for px, py, _ in picked):
            picked.append((x, y, float(flat[oi])))
        if len(picked) >= k:
            break
    return picked


def analyze_frame(frame_bgr, tpl_gray, tpl_bgr, ui_y, mode, thres):
    """跑一次引擎同款匹配 → 一个结论字典。"""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    roi = gray[:ui_y, :]
    img_roi, img_nametag = prep_nametag_images(roi, tpl_gray, mode)
    if img_roi is None:
        return {"error": f"不认识的 mode: {mode}"}
    mask = get_mask(tpl_bgr, (0, 255, 0))
    th, tw = img_nametag.shape[:2]
    roi_p = cv2.copyMakeBorder(img_roi, th, th, tw, tw, borderType=cv2.BORDER_REPLICATE)
    res = cv2.matchTemplate(roi_p, img_nametag, cv2.TM_SQDIFF_NORMED, mask=mask)

    cands = non_overlapping_topk(res, th, tw, k=6)
    best_x, best_y, best = cands[0]
    # 地板值：**离最佳位置足够远**的候选里最低的那个（同一个真位置的邻居要排除）
    FAR = (max(tw, 40), max(th, 40))
    others = [s for (x, y, s) in cands[1:] if abs(x - best_x) > FAR[0] or abs(y - best_y) > FAR[1]]
    floor = min(others) if others else float("nan")
    # ⚠️ 分子/分母都可能为 0：像素级完美匹配时 SQDIFF 正好是 0.0，floor/best 会除零。
    #    真机帧不会给到 0，但工具绝不能因为一个合成用例就崩（自检实测踩到）。
    ratio = (floor / best) if (best > 1e-9 and others) else float("inf")

    g = gray
    return {
        "best": best, "bx": best_x - tw, "by": best_y - th,
        "floor": floor, "ratio": ratio, "cands": cands,
        "gray_mean": float(g.mean()), "gray_std": float(g.std()),
        "frame_hash": hashlib.sha1(frame_bgr.tobytes()).hexdigest()[:12],
        "tw": tw, "th": th, "mask_px": int((mask > 0).sum()),
    }


def verdict(a, thres):
    """把一个结论字典翻成**人话**。"""
    if a.get("error"):
        return "✗ " + a["error"], "bad"
    b, fl, r = a["best"], a["floor"], a["ratio"]
    if a["gray_std"] < 1.0:
        return (f"画面几乎全黑/全平（灰度 std={a['gray_std']:.2f}）—— "
                f"抓帧拿到的是空画面，跟模板没关系"), "bad"
    if b < thres and r >= GOOD_RATIO:
        _gain = (thres / b) if b > 1e-9 else float("inf")
        _g = "∞（像素级完美匹配）" if _gain == float("inf") else f"{_gain:.1f}"
        return (f"✓ **名牌就在画面里，匹配良好**：最佳 {b:.4f} 比阈值 {thres} 好 "
                f"{_g} 倍，而且比地板值 {fl:.4f} 低 {r:.0f} 倍。"
                f"\n         ⇒ 匹配链没问题；这一帧认不出是因为**别的环节**"
                f"（看下方候选位置是否落在合理区域）"), "ok"
    if b < thres:
        return (f"⚠ **勉强过阈值**：最佳 {b:.4f} < {thres}，但只比地板值 {fl:.4f} 低 "
                f"{r:.1f} 倍（< {GOOD_RATIO:.0f} 倍）——判别余量很薄，"
                f"多半是名牌**被部分遮挡**。"), "warn"
    if not (fl == fl) or b >= fl * 0.85:                 # nan 或 与地板值相当
        return (f"✗ **画面里根本找不到名牌**：最佳 {b:.4f} ≈ 地板值 "
                f"{'?' if fl != fl else f'{fl:.4f}'}，两者几乎一样高。"
                f"\n         ⇒ 最佳那块只是「画面里最像的杂色」，不是名牌。"
                f"该查**为什么不可见**：被人堆盖住 / 被前景挡住 / 切图黑屏 / 角色不可见。"), "bad"
    return (f"? 情况少见：最佳 {b:.4f} ≥ 阈值但明显低于地板值 {fl:.4f} —— "
            f"像是「名牌在、但变形了」（缩放/加称号/渲染变化）。"), "warn"


def main(argv=None):
    ap = argparse.ArgumentParser(description="名字定位失败帧的离线判定")
    ap.add_argument("--dir", default=None, help="失败帧目录（debug/名字定位失败）")
    ap.add_argument("--frame", default=None, help="单张帧图")
    ap.add_argument("--mode", default=None, help="覆盖 nametag.mode（默认用 config 里的）")
    ap.add_argument("--name", default=None, help="覆盖 nametag.name（模板文件名，不含 .png）")
    ap.add_argument("--no-save", action="store_true", help="不保存候选裁图")
    ap.add_argument("--out-dir", default=None, help="候选裁图输出目录")
    a = ap.parse_args(argv)

    cfg = load_yaml("config/config_default.yaml")
    # ⚠️ 必须跟随**当前生效的方案**（和引擎 / 录制器同一条路）——
    #    只读 config_default 的话，模板名会是出厂占位值 "example"，
    #    工具直接报"找不到模板"，等于没法用（实测踩到）。
    _ap = active_config_path()
    if _ap:
        try:
            cfg = override_cfg(cfg, load_yaml(_ap))
        except Exception as e:                               # noqa: BLE001
            print(f"[警告] 读取当前方案 {_ap} 失败，按出厂默认继续：{e}")
    nt = cfg["nametag"]
    mode = str(a.mode or nt.get("mode") or "grayscale").strip().lower()
    thres = float(nt["diff_thres"])
    ui_y = int(cfg["ui_coords"]["ui_y_start"])
    tpl_name = a.name or nt["name"]
    tpl_path = f"nametag/{tpl_name}.png"
    if not os.path.exists(tpl_path):
        print(f"[错误] 找不到模板 {tpl_path}")
        return 2
    tpl_bgr = load_image(tpl_path)
    tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
    mask_px = int((get_mask(tpl_bgr, (0, 255, 0)) > 0).sum())

    frames = []
    if a.frame:
        frames = [a.frame]
    elif a.dir:
        # 认两种命名：引擎落盘的 *_frame.png，以及工具箱抓的 capture_raw_*.png
        # （后者是**整窗**，会在下面自动裁成客户区）
        frames = sorted(glob.glob(os.path.join(a.dir, "*_frame.png"))
                        + glob.glob(os.path.join(a.dir, "capture_raw_*.png")))
    if not frames:
        print("[错误] 没有可分析的帧。用法：--dir debug/名字定位失败 或 --frame <图>")
        return 2

    out_dir = a.out_dir or os.path.join(a.dir or os.path.dirname(frames[0]),
                                        "_判图")
    print("=" * 74)
    print("名字定位失败帧 · 离线判定")
    print("=" * 74)
    print(f"  模板     : {tpl_path}  {tpl_bgr.shape[1]}x{tpl_bgr.shape[0]}  "
          f"参与匹配像素={mask_px}（{100.0*mask_px/(tpl_bgr.shape[0]*tpl_bgr.shape[1]):.1f}%）")
    print(f"  模式     : {mode}      阈值: {thres}      搜索区: 0..{ui_y}")
    print(f"  帧数     : {len(frames)}")
    print()

    verdicts, hashes = [], []
    # 抓帧可能是**整窗**（含标题栏/边框），引擎用的却是**客户区**。
    # 不裁的话搜索区会整体错位，分数全是垃圾 —— 用项目自己的裁切函数，
    # 保证和引擎看到的坐标一致（别自己算偏移）。
    # ⚠️ 配置里 game_window.size 的字段顺序和"宽x高"不一定一致（实测取出来是 768x1366）。
    #    这里归一化：游戏窗口是横屏，宽取大的那个；再用**帧本身**校验能不能裁出来。
    _a, _b = (int(v) for v in cfg["game_window"]["size"])
    _client = (max(_a, _b), min(_a, _b))
    for p in frames:
        frame = load_image(p)
        if (frame.shape[1], frame.shape[0]) != _client:
            _c = crop_to_client(frame, _client)
            if _c is None:
                print(f"── {os.path.basename(p)}")
                print(f"   [跳过] 尺寸 {frame.shape[1]}x{frame.shape[0]} 裁不成客户区 "
                      f"{_client[0]}x{_client[1]}")
                continue
            frame = _c
        an = analyze_frame(frame, tpl_gray, tpl_bgr, ui_y, mode, thres)
        txt, kind = verdict(an, thres)
        verdicts.append(kind)
        hashes.append(an.get("frame_hash"))
        print(f"── {os.path.basename(p)}")
        print(f"   灰度 mean/std = {an.get('gray_mean', 0):.1f}/{an.get('gray_std', 0):.1f}"
              f"   帧指纹 = {an.get('frame_hash')}")
        print(f"   最佳分数 = {an.get('best', -1):.4f} @ 搜索区({an.get('bx')},{an.get('by')})"
              f"   地板值 = {an.get('floor', float('nan')):.4f}"
              f"   余量 = {an.get('ratio', float('inf')):.1f}x")
        print("   前几个互不重叠的候选：")
        for x, y, s in an.get("cands", [])[:5]:
            print(f"      score={s:.4f}  @({x - an.get('tw', 0)},{y - an.get('th', 0)})")
        print(f"   判定：{txt}")
        # 把最佳候选裁出来（放大 4 倍）供人眼确认
        if not a.no_save and "bx" in an:
            h, w = an["th"], an["tw"]
            y0, y1 = max(0, an["by"]), min(frame.shape[0], an["by"] + h)
            x0, x1 = max(0, an["bx"]), min(frame.shape[1], an["bx"] + w)
            crop = frame[y0:y1, x0:x1]
            if crop.size:
                os.makedirs(out_dir, exist_ok=True)
                out = os.path.join(out_dir, os.path.basename(p).replace("_frame.png", "_best.png"))
                imwrite_unicode(out, cv2.resize(crop, None, fx=4, fy=4,
                                                interpolation=cv2.INTER_NEAREST))
                print(f"   最佳候选裁图 → {out}")
        print()

    # ── 跨帧结论 ─────────────────────────────────────────────────────────
    print("=" * 74)
    print("总结")
    print("=" * 74)
    if len(hashes) >= 2:
        from collections import Counter
        _c = Counter(h for h in hashes if h)
        _dup, _n = (_c.most_common(1)[0] if _c else (None, 0))
        if _n >= 2:
            print(f"  ⚠️ 有 {_n}/{len(hashes)} 帧**逐字节完全相同**（指纹 {_dup}）——")
            print("     画面很可能是**冻结**的（抓帧反复拿到同一张图）。")
            print("     这跟模板/阈值都无关，去查抓帧链路：游戏是否最小化、被遮挡、")
            print("     或窗口停止渲染（很多游戏失焦后会暂停绘制）。")
    n_bad = sum(1 for k in verdicts if k == "bad")
    n_ok = sum(1 for k in verdicts if k == "ok")
    n_warn = sum(1 for k in verdicts if k == "warn")
    print(f"  匹配良好 {n_ok} 帧 / 勉强过阈值 {n_warn} 帧 / 画面里没有名牌 {n_bad} 帧")
    if n_bad and not n_ok:
        print("  ⇒ 结论：**不是匹配链弱，是那些帧里名牌不可见**。")
        print("     下一步该查可见性（人堆/前景/切图/角色状态），而不是调阈值或换模板。")
    elif n_ok:
        print("  ⇒ 结论：**匹配链在这些帧上是好的** —— 认不出发生在别的时刻，")
        print("     需要拿「认不出那一刻」的帧（引擎已按节流自动落盘）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
