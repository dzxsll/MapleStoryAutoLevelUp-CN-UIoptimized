# -*- coding: utf-8 -*-
"""自检：精灵帧导入（tools/mob_frames_import.py）的口径转换。

为什么要有这个自检：这条链路上有两个**静默失效**形态，出问题时都不报错 ——
  ① 文件名不带 `<怪名>_` 前缀 ⇒ 引擎的 glob 一个都不加载（"放进去=没放"）；
  ② 底色不是绿幕、或模板自身没有黑描边 ⇒ `contour_only` 的掩码要么满屏
     （整片黑底当轮廓 → 满屏误报），要么空（没有描边 → 一次都匹配不上）。
自检用**合成夹具**（不依赖 monster/ 素材，CI 里也能跑），把这些口径钉住。

判读：全绿 = exit 0；任一 FAIL = exit 1。
"""
import glob
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.common import get_mask, imread_unicode, imwrite_unicode   # noqa: E402
from tools.mob_frames_import import (GREEN, BLACK, already_green_bg,      # noqa: E402
                                     border_black_mask, convert_dir,
                                     convert_sprite)

PASS, FAIL = 0, 0
MOB = "怪A"


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}" + (f"  ({extra})" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  ({extra})" if extra else ""))


def fixture_black_bg(w=10, h=8, eye=False):
    """黑底 + 一块红方块当"怪"。eye=True 时在方块内部加一个**不接触边框**的纯黑点。"""
    img = np.full((h, w, 3), BLACK, np.uint8)
    img[2:6, 3:7] = (0, 0, 255)
    if eye:
        img[3, 4] = BLACK
    return img


def main():
    print("=" * 74)
    print(" 自检：精灵帧导入（黑底 → 绿幕 + 补边 + 补 1px 黑描边 + 前缀命名）")
    print("=" * 74)

    img = fixture_black_bg()
    new, stats = convert_sprite(img)

    print("\n[1] 与四边连通的纯黑 → 绿幕；尺寸四周各 +1")
    check("尺寸 +2（补 1px 边）", new.shape[:2] == (img.shape[0] + 2, img.shape[1] + 2),
          f"{img.shape[1]}x{img.shape[0]} → {new.shape[1]}x{new.shape[0]}")
    check("四条边全是绿幕",
          all(np.all(line == GREEN) for line in (new[0, :], new[-1, :], new[:, 0], new[:, -1])))
    check("怪本体像素没被动过", np.array_equal(new[3:7, 4:8], img[2:6, 3:7]))
    check("背景黑全部被算成背景", stats["bg"] == 8 * 10 - 4 * 4, f"bg={stats['bg']}")

    print("\n[2] 怪自己的黑（不与边框连通）必须保留")
    img2 = fixture_black_bg(eye=True)
    m2 = border_black_mask(img2)
    check("体内的黑点不算背景", not m2[3, 4])
    new2, _s2 = convert_sprite(img2)
    check("转换后它仍然是纯黑", np.array_equal(new2[4, 5], BLACK), f"{tuple(new2[4, 5])}")

    print("\n[3] 沿剪影外沿补的那一圈确实补上了（contour_only 的掩码靠它）")
    # ⚠️ 别再拿转换后的图重算「外沿一圈」——那时补的黑描边**自己就是剪影的一部分**，
    #    重算出来的一圈落在更外面（是绿幕），必然 0%。正确判据是：转换后图里的纯黑
    #    像素**正好等于**补出来的描边像素，且每一个都紧贴绿幕（= 确实在最外一圈）。
    blackmask = np.all(new == BLACK, axis=2)
    greenmask = np.all(new == GREEN, axis=2)
    gdil = cv2.dilate(greenmask.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    check("补的描边像素数 > 0", stats["ring"] > 0, f"ring={stats['ring']}")
    check("纯黑像素数 == 补的描边数", int(blackmask.sum()) == stats["ring"],
          f"black={int(blackmask.sum())} ring={stats['ring']}")
    check("每个黑描边像素都紧贴绿幕（确实在最外一圈）",
          bool(blackmask.sum() > 0 and gdil[blackmask].all()))

    print("\n[4] 已经是绿幕的素材不会被重复转")
    check("四边绿幕 → already_green_bg 为真", already_green_bg(new))
    check("黑底素材 → 为假", not already_green_bg(img))

    tmp = tempfile.mkdtemp(prefix="mob_frames_import_")
    try:
        src = os.path.join(tmp, "src")
        out = os.path.join(tmp, "out")
        os.makedirs(src, exist_ok=True)
        imwrite_unicode(os.path.join(src, "move_0.png"), img)
        imwrite_unicode(os.path.join(src, f"{MOB}_old.png"), img)      # 已带前缀 → 必须跳过
        imwrite_unicode(os.path.join(src, "green_dump.png"), new)      # 已是绿幕 → 必须跳过

        print("\n[5] 命名口径：输出名必须是 <怪名>_<原名>.png（引擎 glob 的口径）")
        done, skipped = convert_dir(src, MOB, out_dir=out, write=True)
        dst = os.path.join(out, f"{MOB}_move_0.png")
        check("落盘文件存在", os.path.exists(dst), os.path.basename(dst))
        check("引擎同款 glob 能扫到它", len(glob.glob(os.path.join(out, f"{MOB}*.png"))) == 1)
        check("已带前缀的素材被跳过", any(os.path.basename(f) == f"{MOB}_old.png"
                                          for f, _w in skipped))
        check("已是绿幕的素材被跳过", any(os.path.basename(f) == "green_dump.png"
                                          for f, _w in skipped))
        check("只转出 1 张", len(done) == 1, f"done={len(done)}")

        print("\n[6] 默认（不传 write）不落盘 —— 预演不会污染素材目录")
        out2 = os.path.join(tmp, "out2")
        done2, _sk2 = convert_dir(src, MOB, out_dir=out2, write=False)
        check("预演报告了 1 张", len(done2) == 1)
        check("但目录里没有新文件", not os.path.exists(out2) or not os.listdir(out2))

        print("\n[7] 转换后的模板在项目掩码口径下是「剪影 = 前景」")
        tpl = imread_unicode(dst, cv2.IMREAD_COLOR)
        fg = get_mask(tpl, GREEN).astype(bool)
        non_green = ~np.all(tpl == GREEN, axis=2)
        check("前景像素数 == 非绿像素数", int(fg.sum()) == int(non_green.sum()),
              f"fg={int(fg.sum())}")
        check("前景不是整张（不是全透明/全前景）", 0 < int(fg.sum()) < tpl.shape[0] * tpl.shape[1])
        _blk = np.all(tpl == BLACK, axis=2)
        check("补的黑描边也算前景（不透明）", bool(_blk.sum() > 0 and fg[_blk].all()),
              f"black={int(_blk.sum())}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n[8] 口径的**载体**没被改掉（改了写入方忘了改判定方 = 静默失效）")
    eng = os.path.join(REPO_ROOT, "src", "engine", "MapleStoryAutoLevelUp.py")
    vm = os.path.join(REPO_ROOT, "tools", "verify_monster.py")
    with open(eng, encoding="utf-8") as f:
        eng_txt = f.read()
    with open(vm, encoding="utf-8") as f:
        vm_txt = f.read()
    check("引擎按 <怪名>*.png 加载模板（口径①）",
          'monster/{monster_name}/{monster_name}*.png' in eng_txt)
    check("引擎用 (0,255,0) 当透明色（口径②）", 'get_mask(img, (0, 255, 0))' in eng_txt)
    check("引擎 contour_only 掩码是纯黑像素", "np.all(img_monster == [0, 0, 0]" in eng_txt)
    check("离线验证器同款 glob（分毫不差）", 'f"{d}*.png"' in vm_txt)

    print("\n" + "=" * 74)
    print(f" 结果：通过 {PASS} / 失败 {FAIL}")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
