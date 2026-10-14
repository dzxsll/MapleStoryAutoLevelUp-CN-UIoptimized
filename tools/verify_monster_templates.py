# -*- coding: utf-8 -*-
'''
自检：怪物模板的**加载**（多尺度 + alpha 透明底）与匹配健壮性

为什么有这个自检（2026-10-13，用户原话：「https://github.com/zhoufanglu/maplestory-auto-ds
参考该项目怪物识别模块」）：
    把参考项目 `src/engine/MonsterDetector.py`（1972 行）与我们 engine 里对应的
    `load_config` 模板加载 + `get_monsters_in_range` 逐条对照后，我们**缺**四样东西：
      ① 多尺度匹配 `monster_detect.scales`（官方素材与游戏内渲染尺寸对不上时的唯一解法）
      ② RGBA 透明底素材直接可用（参考项目 `IMREAD_UNCHANGED` + alpha 掩码；
         我们原来 `load_image(file)` 默认 `IMREAD_COLOR`，**alpha 直接被丢掉**、
         透明区变黑块 —— 而默认模式 contour_only 的"轮廓"就是黑像素）
      ③ 模板尺寸下限（<10px 跳过）
      ④ 单张模板匹配失败只跳过这张，不作废整帧（grayscale/color 分支连"模板比 ROI 大"
         这道闸都没有，`cv2.matchTemplate` 会直接抛异常）
    本自检把这四条逐条钉住。

⚠️ 有一处我们**故意不照抄**参考项目：它把 alpha 之外的像素置成**黑** `[0,0,0]`；
   我们置**绿** `(0,255,0)`。理由：我们的轮廓模式只看黑像素，模板背景一旦是黑，
   整块背景立刻变成"轮廓"（等于给每张模板加了一个巨大的假轮廓）。

钉住的判据：
    [1] `flatten_alpha_to_green`：RGBA→3 通道 + 透明区填绿；3 通道原样返回；灰度升 3 通道；
        形状不认识 → ValueError（不静默）
    [2] `_mob_scales`：缺键/None/空/非列表/全非法 → 回退 `[1.0]`（fail-closed）；
        混入非法值只丢那一条；去重 + 升序
    [3] `_mob_template_variants`：`scale=1.0` **不做 resize**（与老行为逐像素一致）；
        每尺度 = 原图 + 左右镜像；尺度升序
    [4] `_mob_hit_points`：命中点封顶 50 且**留最好的**（防平坦画面 O(n²) 卡死）
    [5] 端到端（绑真的 `get_monsters_in_range`，合成画面）：
        · 基准：40x40 黑框怪 → 找到 1 只，位置 (60,60)、尺寸 (40,40)
        · **无回归**：配置里删掉 `scales` 键 与 `scales: [1.0]` 结果完全一致
        · 多尺度：`scales: [1.0, 0.98]` 两个变体命中同一只怪 → 出来仍**只有 1 只**（NMS 合并）
        · 大模板（265x265 > ROI）→ 跳过单张、不抛异常
        · 极小模板（8x8）→ 跳过
    [6] 源码/配置判据：三处 matchTemplate 都有 `except cv2.error`；命中点三处都走
        `_mob_hit_points`；`config_default.yaml` 有 `scales: [1.0]`；`cfg_names` 有中文名
'''
import os
import sys
import inspect

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.common import get_mask, flatten_alpha_to_green, nms   # noqa: E402
from src.utils.cfg_names import cn_key                              # noqa: E402
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot      # noqa: E402

FAIL = 0
PASS = 0


def check(name, got, want):
    global FAIL, PASS
    if got == want:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望={want!r} 实际={got!r}")


def check_true(name, cond, detail=""):
    global FAIL, PASS
    if cond:
        PASS += 1
        print(f"  [OK]   {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


# ── 合成素材/画面 ────────────────────────────────────────────────────────────
def template(size=(40, 40), ring=3):
    '''绿幕底 + 一个黑色方框（= contour_only 眼里的"怪轮廓"）。'''
    w, h = size
    t = np.full((h, w, 3), (0, 255, 0), np.uint8)
    t[0:ring, 0:w] = 0
    t[h - ring:h, 0:w] = 0
    t[0:h, 0:ring] = 0
    t[0:h, w - ring:w] = 0
    return t


def scene(mob_xy=(60, 60), mob_size=(40, 40), ring=3):
    '''灰底 + 一个与 template() 同形的黑框怪（灰 ≠ 黑 ⇒ 不会被当成轮廓）。'''
    fr = np.full((200, 200, 3), 128, np.uint8)
    x, y = mob_xy
    w, h = mob_size
    fr[y:y + ring, x:x + w] = 0
    fr[y + h - ring:y + h, x:x + w] = 0
    fr[y:y + h, x:x + ring] = 0
    fr[y:y + h, x + w - ring:x + w] = 0
    return fr


def cfg(**md_over):
    md = {"mode": "contour_only", "contour_blur": 5, "diff_thres": 0.4,
          "with_enemy_hp_bar": False, "verify_color": True,
          "verify_color_tol": 30, "verify_min_ratio": 0.75}
    md.update(md_over)
    return {"bot": {"attack": "aoe_skill"}, "monster_detect": md,
            "character": {"width": 40, "height": 60},
            "aoe_skill": {"range_x": 400, "range_y": 170, "cooldown": 0.05}}


def make_bot(c, tpls, frame=None):
    '''不跑 __init__（它会去抓窗口），绑**真**方法 ⇒ 测的就是跑的那一份。'''
    class S:
        pass

    s = S()
    # 普通方法：绑到实例上（self = s）
    s.get_monsters_in_range = getattr(MapleStoryAutoBot, "get_monsters_in_range").__get__(s)
    # 静态方法：原样挂（本来就不吃 self）
    for _n in ("_mob_foreground_mask", "_mob_color_ok", "_mob_hit_points",
               "_mob_template_variants", "_mob_scales"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n))
    s.cfg = c
    s.loc_player = (5, 5)          # 贴着左上角：角色框清零区 (0,0)-(25,35)，不碰 (60,60)
    s.img_frame = scene() if frame is None else frame
    s.monsters_info = tpls
    return s


def run(c, tpls):
    b = make_bot(c, tpls)
    out = b.get_monsters_in_range((0, 0), (200, 200))
    return [(m["name"], tuple(m["position"]), tuple(m["size"])) for m in out], out


print("=" * 72)
print(" 自检：怪物模板加载（多尺度 + alpha 透明底）+ 匹配健壮性")
print("       对照参考项目 zhoufanglu/maplestory-auto-ds 的 MonsterDetector")
print("=" * 72)

# ── ① alpha 透明底 ──────────────────────────────────────────────────────────
print("\n[1] flatten_alpha_to_green：官方 RGBA 素材直接用（透明区填**绿**，不是黑）")
_rgba = np.zeros((6, 6, 4), np.uint8)
_rgba[:, :, :3] = (10, 20, 30)
_rgba[:, :, 3] = 0
_rgba[2:4, 2:4, 3] = 255
_out = flatten_alpha_to_green(_rgba)
check("RGBA → 3 通道", _out.shape, (6, 6, 3))
check("透明区填成绿幕 (0,255,0)", tuple(int(v) for v in _out[0, 0]), (0, 255, 0))
check("不透明区保留原色", tuple(int(v) for v in _out[2, 2]), (10, 20, 30))
check("前景掩码 = 不透明的那 4 个像素",
      int(get_mask(_out, (0, 255, 0)).sum() // 255), 4)
_bgr = np.full((4, 4, 3), 7, np.uint8)
check_true("已是 3 通道 → 原样返回（不做拷贝）", flatten_alpha_to_green(_bgr) is _bgr)
check("灰度单通道 → 升成 3 通道", flatten_alpha_to_green(np.full((4, 4), 9, np.uint8)).shape,
      (4, 4, 3))
try:
    flatten_alpha_to_green(np.zeros((4, 4, 5), np.uint8))
    check_true("形状不认识 → ValueError（不静默）", False)
except ValueError:
    check_true("形状不认识 → ValueError（不静默）", True)

# ── ② scales 解析（fail-closed） ────────────────────────────────────────────
print("\n[2] _mob_scales：缺键/坏值一律回退 [1.0]，合法值去重升序")
_sc = MapleStoryAutoBot._mob_scales
check("缺键 → [1.0]", _sc({}), [1.0])
check("None → [1.0]", _sc(None), [1.0])
check("空列表 → [1.0]", _sc({"scales": []}), [1.0])
check("写成标量 1.5 → [1.0]", _sc({"scales": 1.5}), [1.0])
check("全是非数字 → [1.0]", _sc({"scales": ["x", None]}), [1.0])
check("混入非法值 → 只丢那几条 + 升序",
      _sc({"scales": [1.15, 0, "x", 0.7, -1, 1.0]}), [0.7, 1.0, 1.15])
check("重复值去重", _sc({"scales": [1.0, 1.0, 2.0]}), [1.0, 2.0])
check(">10 丢掉 → 回退 [1.0]", _sc({"scales": [12.0]}), [1.0])

# ── ③ 模板展开（多尺度 × 镜像） ─────────────────────────────────────────────
print("\n[3] _mob_template_variants：scale=1.0 不 resize；每尺度 = 原图 + 镜像")
_t = template((40, 40))
_v1 = MapleStoryAutoBot._mob_template_variants(_t, [1.0])
check("scales=[1.0] → 2 个变体（原图 + 镜像）", len(_v1), 2)
check_true("scale=1.0 真的是同一张图（一个像素都没动）", _v1[0][0] is _t)
check_true("镜像与 cv2.flip(img,1) 逐像素一致", np.array_equal(_v1[1][0], cv2.flip(_t, 1)))
check_true("掩码口径 = get_mask(img,(0,255,0))",
           np.array_equal(_v1[0][1], get_mask(_t, (0, 255, 0))))
_v2 = MapleStoryAutoBot._mob_template_variants(_t, [0.5, 1.0])
check("2 个尺度 → 4 个变体", len(_v2), 4)
check("0.5 尺度 → 20x20（最近邻缩放）", _v2[0][0].shape[:2], (20, 20))
check("1.0 尺度 → 40x40（不缩放）", _v2[2][0].shape[:2], (40, 40))

# ── ④ 命中点封顶 ────────────────────────────────────────────────────────────
print("\n[4] _mob_hit_points：封顶 50，且**留最好的**（防平坦画面卡死）")
_hp = MapleStoryAutoBot._mob_hit_points
_res = np.full((20, 20), 0.9, np.float64)
_res[3, 4] = 0.05
_res[10, 12] = 0.15
check("阈值内命中点很少时：全给（行序与原来 np.where 一致）",
      list(_hp(_res, 0.4)), [(4, 3), (12, 10)])
check("cap=1 → 只留分数最小的那个（SQDIFF 越小越像）", list(_hp(_res, 0.4, cap=1)), [(4, 3)])
check("阈值外一个不留", list(_hp(_res, 0.04)), [])
_flat = np.zeros((60, 60), np.float64)
check("★ 平坦画面（6 万个命中点）→ 只出 50 个", len(list(_hp(_flat, 0.5))), 50)

# ── ⑤ 端到端（真 get_monsters_in_range） ────────────────────────────────────
print("\n[5] 端到端：合成画面 + 真检测链（contour_only / NMS / 尺寸闸）")
_base = cfg(scales=[1.0])
_got, _raw = run(_base, {"测试怪": MapleStoryAutoBot._mob_template_variants(_t, [1.0])})
check("基准：只找到 1 只", len(_got), 1)
check("基准：名字与尺寸正确", (_got[0][0], _got[0][2]) if _got else None,
      ("测试怪", (40, 40)))
# ⚠️ 位置允许 ±2px：合成模板只有 40px，`cv2.GaussianBlur` 在**模板边界**用反射填充，
#    而同一块轮廓在 200px 画面里是居中的（外面是 0）⇒ 两者的模糊边缘略有差别，
#    最优偏移可能挪 1~2px（真机模板是截图裁出来的，同样有这种边界效应）。
check_true("基准：位置落在 (60,60) ±2px",
           bool(_got) and abs(_got[0][1][0] - 60) <= 2 and abs(_got[0][1][1] - 60) <= 2,
           f"实际 {_got[0][1]}" if _got else "无结果")
check_true("分数确实很低（真的匹配上了）", _raw and float(_raw[0]["score"]) < 0.2,
           f"score={float(_raw[0]['score']):.4f}" if _raw else "无结果")

_nokey = cfg()                                  # 老配置：**没有** scales 这个键
_got_nokey, _ = run(_nokey, {"测试怪": MapleStoryAutoBot._mob_template_variants(_t, [1.0])})
check("★ 无回归：缺 scales 键 与 scales=[1.0] 结果完全一致", _got_nokey, _got)

_vm = MapleStoryAutoBot._mob_template_variants(_t, [1.0, 0.98])
check("变体尺寸：40x40 与 39x39 都在（0.98×40=39.2→39）",
      sorted({v[0].shape[0] for v in _vm}), [39, 40])
_both = [{"name": "m", "position": (60, 60), "size": (40, 40), "score": 0.05},
         {"name": "m", "position": (60, 60), "size": (39, 39), "score": 0.06}]
check("NMS(IoU 0.4) 会把这两个重叠框合并成 1 个", len(nms(_both, iou_threshold=0.4)), 1)
# ★ 2026-10-14 用户拍板：「升序」。`score` 是 TM_SQDIFF_NORMED（**越小越好**），
#   原来的 `reverse=True` 会在同一簇里留下**最差**的框；而血条分支的 score 固定 1.0
#   ⇒ 降序下血条框会压过真模板框（参考项目 zhoufanglu/maplestory-auto-ds 也是升序）。
_nms_order = [{"name": "好", "position": (0, 0), "size": (40, 40), "score": 0.10},
              {"name": "差", "position": (2, 0), "size": (40, 40), "score": 0.90}]
check("★ NMS 留下的是分数更好（更小）的那个",
      [m["name"] for m in nms(_nms_order, iou_threshold=0.4)], ["好"])
_nms_hp = [{"name": "血条框", "position": (0, 0), "size": (40, 40), "score": 1.0},
           {"name": "模板框", "position": (2, 0), "size": (40, 40), "score": 0.30}]
check("★ 血条框(score=1.0) 不会压过模板框(0.30)",
      [m["name"] for m in nms(_nms_hp, iou_threshold=0.4)], ["模板框"])
_nms_src = inspect.getsource(nms)
# 正向钉住升序那一行（注释里提到 `reverse=True` 不算，所以只能查代码形态）。
check_true("nms 源码里就是升序那一行", "boxes.sort(key=lambda x: x[4])" in _nms_src)
check_true("nms 源码里没有降序代码（防止悄悄改回）",
           "x[4], reverse=True" not in _nms_src)
_got_multi, _ = run(cfg(scales=[1.0, 0.98]), {"测试怪": _vm})
check("★ 多尺度：两个尺度命中同一只怪 ⇒ 仍然只有 1 只（不是 2 只）",
      len(_got_multi), 1)

_got_big, _ = run(cfg(scales=[1.0]),
                  {"大怪": MapleStoryAutoBot._mob_template_variants(
                      template((265, 265)), [1.0])})
check("大模板（265x265 > 200x200 ROI）→ 跳过单张、不抛异常、结果为空", _got_big, [])
_got_tiny, _ = run(cfg(scales=[1.0]),
                   {"小怪": MapleStoryAutoBot._mob_template_variants(
                       template((8, 8), ring=1), [1.0])})
check("极小模板（8x8 < 10px）→ 跳过、结果为空", _got_tiny, [])

# ── ⑥ 源码 / 配置判据 ───────────────────────────────────────────────────────
print("\n[6] 源码与配置实体：三条分支的健壮性 + 配置里有这个键")
_src = inspect.getsource(MapleStoryAutoBot.get_monsters_in_range)
check("三个模式分支都包了 `except cv2.error`（单张失败不作废整帧）",
      _src.count("except cv2.error"), 3)
check("三处命中点都走 _mob_hit_points（统一封顶）", _src.count("self._mob_hit_points("), 3)
check_true("不再有裸的 `np.where(res <= diff_thres)`", "np.where(res <=" not in _src)
check_true("三个模式分支都保留了「比搜索框大就跳过」的日志（跳过单张、不作废整帧）",
           _src.count("比搜索框") >= 3, f"出现 {_src.count('比搜索框')} 次")

import yaml                                                          # noqa: E402
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_root, "config", "config_default.yaml"), encoding="utf-8") as _f:
    _md = (yaml.safe_load(_f) or {}).get("monster_detect") or {}
check("config/config_default.yaml：monster_detect.scales 默认 [1.0]", _md.get("scales"), [1.0])
check("cfg_names 有 monster_detect.scales 的中文名",
      cn_key("monster_detect", "scales"), "模板缩放比例（多尺度匹配）")

_src_load = inspect.getsource(MapleStoryAutoBot.load_config)
check_true("加载器用 IMREAD_UNCHANGED 读模板（保住 alpha）",
           "cv2.IMREAD_UNCHANGED" in _src_load)
check_true("加载器走 flatten_alpha_to_green", "flatten_alpha_to_green(" in _src_load)
check_true("加载器按 scales 展开模板（_mob_template_variants）",
           "_mob_template_variants(" in _src_load)
check_true("旧的 `imgs.append((img, get_mask(img, (0, 255, 0))))` 已消失",
           "imgs.append((img, get_mask(img, (0, 255, 0))))" not in _src_load)
check_true("没有绿幕/透明底的模板会 warning（不静默）",
           "没有绿幕、也没有透明底" in _src_load)
check_true("加载时对 <10px 的模板会 warning（每帧只跳过 + 加载时吼一次）",
           "边长 <10px" in _src_load)
check_true("两侧的跳过判据都是 10px（加载警告与匹配跳过同口径）",
           "min(img.shape[0], img.shape[1]) < 10" in _src_load
           and "h_temp < 10 or w_temp < 10" in _src)
# 2026-10-14：`tools/verify_monster.py` 原来自己抄了一份加载器（load_image + get_mask +
# flip）。引擎加上 alpha/多尺度之后它没跟着改 ⇒ 验证器用的是**另一套**判据，
# 透明底素材会被它读成黑块（IMREAD_COLOR 丢 alpha），验证结论与挂机行为对不上。
# 这条断言就是钉死"只能有一份加载判据"。
_vm = open(os.path.join(_root, "tools", "verify_monster.py"), encoding="utf-8").read()
check_true("tools/verify_monster.py 走引擎同款加载（_mob_template_variants）",
           "_mob_template_variants(" in _vm)
check_true("tools/verify_monster.py 有 alpha 支持（flatten_alpha_to_green）",
           "flatten_alpha_to_green(" in _vm)
check_true("tools/verify_monster.py 不再自己抄一份模板加载（旧的 imgs.append 已消失）",
           "imgs.append((img," not in _vm)

print("\n" + "=" * 72)
if FAIL:
    print(f"❌ 自检失败 {FAIL} 项（通过 {PASS} 项）")
    sys.exit(1)
print(f"✅ 全部通过（{PASS} 项）")
