# -*- coding: utf-8 -*-
"""把「精灵帧截图/游戏素材」（黑底、无前缀命名）导成项目口径的怪物模板。

为什么需要这一步 —— 两个**硬性**口径差，不解决就是"文件放进去了也没用"：

① 文件名必须以目录名开头
   引擎加载模板用的是 `glob(monster/<怪名>/<怪名>*.png)`
   （src/engine/MapleStoryAutoLevelUp.py:930，tools/verify_monster.py:65 同款）。
   素材常见的 `move_0.png` / `stand_1.png` 这类名字**一个都不会被加载**，
   而且不会报错 —— 是最典型的静默失效。

② 底色必须是纯绿 (0,255,0)「绿幕」，且 `contour_only` 模式还要求
   模板自身带一圈纯黑描边
   - 掩码口径：`get_mask(img, (0,255,0))` 里**绿 = 透明**；
     `contour_only` 的模板掩码是 `np.all(img == [0,0,0])`（纯黑像素）。
   - 黑底素材的直接后果：整片背景黑被当成轮廓 ⇒ 满屏误报；
     反过来，若素材**没有**自己的黑描边（游戏里的描边是渲染时画的），
     掩码几乎是空的 ⇒ 一次都匹配不上。

本工具的转换（只新增文件，不动原素材）：
   1. 与图象四边连通的纯黑 → (0,255,0)
      （用连通域判定，怪自己的黑描边/眼睛不与边框连通，不会被误伤）
   2. 四周各补 1px 绿幕（游戏的黑描边画在精灵外侧，不补边画不下）
   3. 沿剪影外沿补一圈 1px 纯黑描边（复刻渲染出来的那圈黑边）
   4. 存成 `monster/<怪名>/<怪名>_<原文件名>.png`（满足口径①）

用法：
   python -m tools.mob_frames_import --mob 鳄鱼              # 预演（默认不写）
   python -m tools.mob_frames_import --mob 鳄鱼 --write      # 真的落盘
   python -m tools.mob_frames_import --mob 鳄鱼 --src D:\\素材\\鳄鱼 --write
   python -m tools.mob_frames_import --mob 鳄鱼 --list       # 只看现在有哪些模板

⚠️ 每张模板都会让引擎多跑 2 次模板匹配（原图 + 左右镜像）。
   monster/ 下模板越多，每帧怪检测越慢 —— 加之前先用
   `python -m tools.verify_monster` 在留档帧上量一量值不值。
"""
import argparse
import glob
import os
import sys

import cv2
import numpy as np

try:      # 打包后 __file__ 落在 _internal 里，必须走 APP_ROOT
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.common import imread_unicode, imwrite_unicode      # noqa: E402

GREEN = (0, 255, 0)
BLACK = (0, 0, 0)


def border_black_mask(img):
    """与图象四边连通的纯黑像素（= 黑底背景）。

    用连通域而不是单点洪水填充：背景可能被精灵切成好几块，四面都要查。
    怪**自己**的黑描边/瞳孔不与边框连通 ⇒ 不会被算进背景。
    """
    black = np.all(img == BLACK, axis=2).astype(np.uint8)
    _n, lab = cv2.connectedComponents(black, connectivity=8)
    edge = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
    edge.discard(0)
    if not edge:
        return np.zeros(img.shape[:2], dtype=bool)
    return np.isin(lab, list(edge))


def already_green_bg(img):
    """四边是不是已经是绿幕（= 已经转过的模板，别重复转）。"""
    for line in (img[0, :], img[-1, :], img[:, 0], img[:, -1]):
        if not np.all(line == GREEN):
            return False
    return True


def convert_sprite(img):
    """黑底精灵帧 → 项目口径模板。

    返回 (新图, stats)；stats = dict(bg=背景像素数, sil=剪影像素数, ring=补的描边像素数)。
    """
    bg = border_black_mask(img)
    out = img.copy()
    out[bg] = GREEN
    h, w = out.shape[:2]
    pad = np.full((h + 2, w + 2, 3), GREEN, np.uint8)   # 补 1px 边（画描边用）
    pad[1:-1, 1:-1] = out
    sil = ~np.all(pad == GREEN, axis=2)
    ring = cv2.dilate(sil.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & ~sil
    pad[ring] = BLACK
    return pad, {"bg": int(bg.sum()), "sil": int(sil.sum()), "ring": int(ring.sum())}


def ref_ring_black_ratio(mob, out_dir):
    """口径自证：拿一张现有模板，量它「外沿一圈里纯黑占比」。

    项目里那些**原生**模板（界面 F3 从真机画面截的、或仓库自带的）才是有意义的
    参照物 —— 游戏渲染出来的黑描边就落在剪影外沿那一圈。本工具转出来的模板
    外沿一圈必然是 100% 黑（构造如此），拿它自证是循环论证，所以：

    判「原生」的依据 = 剪影碰到图象边缘（四边不全绿）；本工具的输出**四周都补过
    1px 绿幕**，四边必然全绿。优先用原生模板；都没有才退而用转出来的。

    返回 (文件名 or None, 纯黑占比, 是否原生)。
    """
    cands = []
    for f in sorted(glob.glob(os.path.join(out_dir, f"{mob}*.png"))):
        img = imread_unicode(f, cv2.IMREAD_COLOR)
        if img is None:
            continue
        ng = ~np.all(img == GREEN, axis=2)
        if ng.sum() == 0:
            continue
        ring = ng & ~cv2.erode(ng.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        if ring.sum() == 0:
            continue
        ratio = float((np.all(img == BLACK, axis=2) & ring).sum()) / float(ring.sum())
        cands.append({"name": os.path.basename(f), "ratio": ratio,
                      "native": not already_green_bg(img)})
    if not cands:
        return None, 0.0, False
    cands.sort(key=lambda c: (not c["native"],))      # 原生优先
    best = cands[0]
    return best["name"], best["ratio"], best["native"]


def convert_dir(src_dir, mob, out_dir=None, only=None, write=False):
    """把 src_dir 下「还没转过」的 png 转成模板。返回 (结果列表, 跳过列表)。"""
    out_dir = out_dir or os.path.join(REPO_ROOT, "monster", mob)
    done, skipped = [], []
    for f in sorted(glob.glob(os.path.join(src_dir, "*.png"))):
        stem = os.path.splitext(os.path.basename(f))[0]
        if stem.startswith(mob + "_"):
            skipped.append((f, "文件名已是 <怪名>_ 前缀（已经转过 / 是原生模板）"))
            continue
        if only and stem not in only:
            continue
        img = imread_unicode(f, cv2.IMREAD_COLOR)
        if img is None:
            skipped.append((f, "读不出来（不是图片？）"))
            continue
        if already_green_bg(img):
            skipped.append((f, "四边已是绿幕（已经转过，别重复转）"))
            continue
        new, stats = convert_sprite(img)
        dst = os.path.join(out_dir, f"{mob}_{stem}.png")
        done.append({"src": f, "dst": dst, "size": (img.shape[1], img.shape[0]),
                     "new_size": (new.shape[1], new.shape[0]), "stats": stats})
        if write:
            os.makedirs(out_dir, exist_ok=True)
            imwrite_unicode(dst, new)
    return done, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="把黑底精灵帧导成项目口径的怪物模板")
    ap.add_argument("--mob", required=True, help="怪物目录名（= 文件名前缀），如 鳄鱼")
    ap.add_argument("--src", default=None, help="素材目录（默认 monster/<怪名>）")
    ap.add_argument("--only", default=None, help="只转这几张（逗号分隔的原文件名，不带 .png）")
    ap.add_argument("--write", action="store_true", help="真的落盘（默认只预演）")
    ap.add_argument("--list", action="store_true", help="只列出当前会加载的模板")
    a = ap.parse_args(argv)

    out_dir = os.path.join(REPO_ROOT, "monster", a.mob)
    src_dir = a.src or out_dir

    if a.list:
        files = sorted(glob.glob(os.path.join(out_dir, f"{a.mob}*.png")))
        print(f"monster/{a.mob}/ 下会被引擎加载的模板：{len(files)} 张"
              f"（引擎每次检测匹配 {len(files) * 2} 次，含左右镜像）")
        for f in files:
            img = imread_unicode(f, cv2.IMREAD_COLOR)
            print(f"   {os.path.basename(f):<28} {img.shape[1]}x{img.shape[0]}"
                  f"  绿幕底={'是' if already_green_bg(img) else '否'}")
        return 0

    if not os.path.isdir(src_dir):
        print(f"[错误] 素材目录不存在：{src_dir}")
        return 2
    only = set(x.strip() for x in a.only.split(",")) if a.only else None

    name, ratio, is_native = ref_ring_black_ratio(a.mob, out_dir)
    print("=" * 78)
    print(f" 精灵帧导入：{a.mob}   素材目录 {src_dir}")
    print("=" * 78)
    if name:
        kind = "原生模板（真机截的）" if is_native else "本工具转出来的（自证偏循环，仅作参考）"
        print(f" 口径自证：{name} 的外沿一圈里纯黑占比 {ratio * 100:.0f}%  [{kind}]")
        print("           占比高 ⇒ 「外沿补一圈黑」与项目现有口径一致")
    else:
        print(" 口径自证：目录里还没有可比对的模板（第一次导入属正常）")
    print()

    done, skipped = convert_dir(src_dir, a.mob, out_dir=out_dir, only=only, write=a.write)
    for f, why in skipped:
        print(f"  [跳过] {os.path.basename(f):<26} {why}")
    if not done:
        print("\n 没有可转换的素材。")
        return 0
    print(f" {'原文件':<24}{'新模板':<30}{'尺寸':<14}{'背景黑':>7}{'描边补':>7}")
    for d in done:
        print(f" {os.path.basename(d['src']):<24}{os.path.basename(d['dst']):<30}"
              f"{d['size'][0]}x{d['size'][1]}→{d['new_size'][0]}x{d['new_size'][1]}"
              f"   {d['stats']['bg']:>6}{d['stats']['ring']:>7}")
    n = len(glob.glob(os.path.join(out_dir, f"{a.mob}*.png")))
    if a.write:
        print(f"\n 已写入 {len(done)} 张。现在 monster/{a.mob}/ 下共 {n} 张模板"
              f"（= 每次检测 {n * 2} 次匹配）。")
        print(" 回滚：删掉这些 <怪名>_ 前缀的新文件即可（原素材没动）。")
    else:
        print(f"\n （预演模式，未写文件；加 --write 才落盘。写完会有 {n + len(done)} 张）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
