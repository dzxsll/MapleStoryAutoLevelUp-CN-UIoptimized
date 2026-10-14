# -*- coding: utf-8 -*-
'''verify_mob_color_gate.py
=========================

自检：怪检测的「前景色校验闸」+「模板比搜索框大不许整帧作废」（2026-10-13 加/修）。

背景（为什么要这两条）：
  用户真机实测（沼泽地2，画面里**没有**鳄鱼）：`contour_only` 模式在
  `diff_thres=0.4` 下检出 **17 个框，全部落在平台植被上**（分数 0.359~0.3997）。
  contor_only 只看黑色轮廓（`np.all(img == [0,0,0])`），颜色信息全丢，植被的
  黑轮廓密度/走向和怪足够像 ⇒ 光靠轮廓分数分不开。这是**静默失准**：日志干净、
  界面正常，而每一帧都在对着植被"认"出十几只怪。
  同时发现：一张比搜索框大的模板会让 `return []` **整帧作废**（已找到的怪全被
  丢掉、后面没跑的模板一个都不跑），同样静默。

钉死下面这些（每条都是可复跑的判据，不是注释里的承诺）：
  ① 绿幕模板 + 画面里颜色对得上         → **认**（闸不能把真怪也挡掉）；
  ② 绿幕模板 + 画面里轮廓一模一样、体内颜色差 127 → **不认**（本次修的主症状）；
  ③ 关掉闸（verify_color: false）时 ② 必须被认出来 → 证明 ② 的"0 个框"是闸挡的，
     **不是**夹具根本不匹配（变异测试，防夹具假绿）；
  ④ 没有绿幕的模板（合成/旧模板，既有自检的夹具形态）原样贴回 → 行为不变；
  ⑤ 巨型模板只跳过它自己，**不再整帧作废**（放一个 200x200 的模板在最前面，
     后面的正常模板仍要认出怪来）；
  ⑥ cfg 缺 verify_* 键时按"开"兜底、不抛异常；
  ⑦ `config/config_default.yaml` 里三个键真的存在且默认值 = true / 30 / 0.75。

⚠️ 不依赖 config_data.yaml / minimap / monster 模板等被 gitignore 的素材
   （CI 检出后这些都不存在），全部用内存桩 + 合成图。

跑法：python -m tools.verify_mob_color_gate
'''
import inspect
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, '.')

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = (got == want)
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望={want!r} 实际={got!r}")


def check_true(name, cond, detail=""):
    check(name + (f"  ({detail})" if detail else ""), bool(cond), True)


from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402

GREEN = (0, 255, 0)          # 截模板工具画的绿幕背景色（= 透明底）
SIDE = 20
AT = (10, 10)
FRAME = 120
LOC_PLAYER = (100, 100)      # 角色遮挡区 x/y:[90,110]，与 AT 不重叠


def base_cfg(**md_over):
    md = {
        "mode": "contour_only",
        "contour_blur": 5,
        "diff_thres": 0.4,
        "search_box_margin": 50,
        "max_mob_area_trigger": 1500,
        "with_enemy_hp_bar": False,
        "match_all_mobs": False,
        "verify_color": True,
        "verify_color_tol": 30,
        "verify_min_ratio": 0.75,
    }
    md.update(md_over)
    return {"monster_detect": md, "character": {"width": 20, "height": 20}}


def green_template(fill, side=SIDE, thick=2):
    """绿幕背景 + 纯黑轮廓 + 指定颜色的体内填充（模拟截模板工具的输出）。"""
    t = np.zeros((side, side, 3), np.uint8)
    t[:, :] = GREEN
    cv2.rectangle(t, (4, 4), (side - 5, side - 5), (0, 0, 0), thick)
    cv2.rectangle(t, (4 + thick, 4 + thick),
                  (side - 5 - thick, side - 5 - thick), fill, -1)
    return t


def nogreen_template(side=SIDE, thick=2):
    """白底 + 黑方块（既有自检的合成夹具形态：没有绿幕）。"""
    t = np.zeros((side, side, 3), np.uint8)
    t[:, :] = (255, 255, 255)
    cv2.rectangle(t, (2, 2), (side - 3, side - 3), (0, 0, 0), thick)
    return t


def white_frame():
    return np.full((FRAME, FRAME, 3), 255, np.uint8)


def paste(frame, tpl, at=AT):
    x, y = at
    out = frame.copy()
    out[y:y + tpl.shape[0], x:x + tpl.shape[1]] = tpl
    return out


def as_templates(**named):
    """name -> [(img, mask)]，与引擎 load_config 的 monsters_info 同构。"""
    out = {}
    for name, img in named.items():
        out[name] = [(img, np.full(img.shape[:2], 255, np.uint8))]
    return out


def detect(frame, templates, cfg, loc=LOC_PLAYER):
    bot = object.__new__(MapleStoryAutoBot)
    bot.cfg = cfg
    bot.monsters_info = templates
    bot.img_frame = frame
    bot.loc_player = loc
    bot.img_frame_debug = frame.copy()
    return bot.get_monsters_in_range((0, 0), (frame.shape[1], frame.shape[0]))


def names_near(monsters, at=AT, tol=3):
    return [m["name"] for m in monsters
            if abs(int(m["position"][0]) - at[0]) <= tol
            and abs(int(m["position"][1]) - at[1]) <= tol]


print("=" * 72)
print(" 自检：怪物检测的前景色校验闸 + 巨型模板不再整帧作废")
print("=" * 72)

TPL_TRUE = green_template(fill=(0, 0, 255))        # 体内红
TPL_WRONG = green_template(fill=(0, 128, 0))       # 体内暗绿：轮廓一样、颜色差 127
TPL_NOGREEN = nogreen_template()

cfg_on = base_cfg()

# ── 1. 真怪：颜色对得上 → 必须认出来 ─────────────────────────────────────
print("\n[1] 绿幕模板 + 画面里原样贴着（颜色对得上）→ 认")
f_true = paste(white_frame(), TPL_TRUE)
ms = detect(f_true, as_templates(怪A=TPL_TRUE), cfg_on)
check("检出的怪名（近 %s）" % (AT,), names_near(ms), ["怪A"])
if ms:
    check_true("分数确实低于阈值（说明走的是模板匹配这条路）",
               float(ms[0]["score"]) < cfg_on["monster_detect"]["diff_thres"],
               f"score={float(ms[0]['score']):.4f}")

# ── 2. 假怪：轮廓一模一样、体内颜色不对 → 必须不认 ───────────────────────
print("\n[2] 绿幕模板 + 轮廓一模一样、体内颜色差 127 → 不认（本次修的主症状）")
f_wrong = paste(white_frame(), TPL_WRONG)
ms_wrong = detect(f_wrong, as_templates(怪A=TPL_TRUE), cfg_on)
check("假怪检出的框数", len(ms_wrong), 0)

# ── 3. 变异测试：关掉闸，同一个假怪夹具必须被认出来 ──────────────────────
print("\n[3] 变异测试：verify_color=false 时同一个假怪夹具必须被认出（防夹具假绿）")
cfg_off = base_cfg(verify_color=False)
ms_off = detect(f_wrong, as_templates(怪A=TPL_TRUE), cfg_off)
check_true("关闸后假怪被认出（⇒ [2] 的 0 个框是闸挡的，不是夹具不匹配）",
           len(ms_off) >= 1, f"框数={len(ms_off)}")
if ms_off:
    # ⚠️ 实测更正（2026-10-13，第一版断言写错了）：**不能**断言"假怪的分数 == 真怪的分数"。
    #    理由：TM_SQDIFF_NORMED 的归一化分母含 ROI 侧能量 ΣI²，假怪体内换了个颜色，
    #    分母就变了 ⇒ 同轮廓下真怪 0.1152、假怪 0.3915。
    #    真正要钉的是：关闸后它的分数**本来就低于阈值**（0.39 < 0.4）——也就是说轮廓这条
    #    路会认它，挡住它的是前景色闸，而不是"分数够不着"。
    check("关闸后假怪的位置就在贴模板处", names_near(ms_off), ["怪A"])
    check_true("关闸后它的分数低于阈值（⇒ 挡它的确实是闸，不是分数）",
               float(ms_off[0]["score"]) < cfg_off["monster_detect"]["diff_thres"],
               f"关闸score={float(ms_off[0]['score']):.4f} < {cfg_off['monster_detect']['diff_thres']}")

# ── 4. 没有绿幕的模板（既有自检夹具形态）行为不变 ────────────────────────
print("\n[4] 无绿幕模板（白底黑方块）原样贴回 → 仍然认（既有夹具行为不变）")
f_ng = paste(white_frame(), TPL_NOGREEN)
ms_ng = detect(f_ng, as_templates(怪B=TPL_NOGREEN), cfg_on)
check("无绿幕夹具：检出的怪名", names_near(ms_ng), ["怪B"])
fg_none = MapleStoryAutoBot._mob_foreground_mask(np.full((4, 4, 3), GREEN, np.uint8))
check("整张都是绿幕（空前景）→ 掩码为 None（放行，不误挡）", fg_none, None)

# ── 5. 巨型模板只跳过自己，不再整帧作废 ─────────────────────────────────
print("\n[5] 巨型模板（200x200 > 搜索框 120x120）不再整帧作废")
tpl_huge = green_template(fill=(0, 0, 255), side=200, thick=4)
# 顺序里"超大"在前 —— 老实现会在它这里 return [] 把整帧作废
ms_huge = detect(paste(white_frame(), TPL_TRUE),
                 as_templates(超大=tpl_huge, 怪A=TPL_TRUE), cfg_on)
check("巨型模板在前时，后面的正常模板仍能认出怪", names_near(ms_huge), ["怪A"])

src = inspect.getsource(MapleStoryAutoBot.get_monsters_in_range)
check_true("源码里已经没有 `# template bigger than roi` 的整帧 return",
           "# template bigger than roi" not in src)
check_true("源码里有「跳过这一张」的说明（口径留在码上）", "跳过这一张" in src)

# ── 6. cfg 缺 verify_* 键 → 按"开"兜底、不抛异常 ────────────────────────
print("\n[6] cfg 缺 verify_* 键（段被整段覆盖/旧配置）→ 默认开、不抛异常")
cfg_missing = base_cfg()
for k in ("verify_color", "verify_color_tol", "verify_min_ratio"):
    cfg_missing["monster_detect"].pop(k)
try:
    ms_missing = detect(f_wrong, as_templates(怪A=TPL_TRUE), cfg_missing)
    check_true("缺键不抛异常", True)
    check("缺键时假怪仍被挡（默认开）", len(ms_missing), 0)
except Exception as e:
    check_true("缺键不抛异常", False, f"{type(e).__name__}: {e}")

# ── 7. _mob_color_ok 的放行分支（形状不一致 / 空 patch）────────────────
print("\n[7] _mob_color_ok 的放行分支：形状对不上、空 patch 一律放行")
fg = MapleStoryAutoBot._mob_foreground_mask(TPL_TRUE)
check("形状不一致 → True（放行）",
      MapleStoryAutoBot._mob_color_ok(np.zeros((5, 5, 3), np.uint8), TPL_TRUE, fg, 30, 0.75),
      True)
check("空 patch → True（放行）",
      MapleStoryAutoBot._mob_color_ok(np.zeros((0, 0, 3), np.uint8), TPL_TRUE, fg, 30, 0.75),
      True)

# ── 8. 配置实体：三个键必须真的在 config_default.yaml 里 ────────────────
print("\n[8] config/config_default.yaml 的默认值（配置实体，不是注释承诺）")
try:
    from src.utils.common import load_yaml
    _md = (load_yaml(os.path.join(".", "config", "config_default.yaml")) or {}).get("monster_detect") or {}
    check("verify_color 默认", _md.get("verify_color"), True)
    check("verify_color_tol 默认", _md.get("verify_color_tol"), 30)
    check("verify_min_ratio 默认", _md.get("verify_min_ratio"), 0.75)
except Exception as e:
    check_true("config_default.yaml 可读", False, f"{type(e).__name__}: {e}")

# ── 9. UI 与中文名登记（同一判据被复制到多处的同步检查）────────────────
print("\n[9] 新键在 UI 隐藏清单与中文名表里都登记了")
_src_ui = open(os.path.join("src", "ui", "ui.py"), encoding="utf-8").read()
_src_cn = open(os.path.join("src", "utils", "cfg_names.py"), encoding="utf-8").read()
for k in ("verify_color", "verify_color_tol", "verify_min_ratio"):
    check_true(f"ui.py ADV_FIELD_HIDE 里有 {k}", f"'{k}'" in _src_ui)
    check_true(f"cfg_names.py 里有 {k} 的中文名", f'"{k}"' in _src_cn)

print("\n" + "=" * 72)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
