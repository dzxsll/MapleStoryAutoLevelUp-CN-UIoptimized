# -*- coding: utf-8 -*-
'''「名字定位失败帧判定工具」的自检（2026-10-12 加）。

配合 `tools/nametag_diag.py` 与 `MapleStoryAutoLevelUp._dump_nametag_failure`。

## 这个工具的价值全在"结论对不对"

它存在的理由：失败分数低有**两种完全相反**的病因，靠一个数分不开 ——
  · 名牌在画面里但匹配不上 ⇒ 该改匹配链；
  · 画面里压根没有名牌   ⇒ 改匹配链是白费（该查为什么不可见）。
所以用例【1】【2】把这两种情况**各造一遍**，断言结论必须**分别**给出正确的那一个。
结论给反了，就会把人引去改错的地方 —— 比没有工具更糟。

用例【3】钉住"预处理必须与引擎同源"（`prep_nametag_images`）。
用例【4】钉住落盘的安全约束（节流 + 上限 + 绝不抛异常）。

用法：
    python -m tools.verify_nametag_diag
'''
from __future__ import annotations

import os
import shutil
import sys

import numpy as np
import cv2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import (load_yaml, load_image, imwrite_unicode,   # noqa: E402
                              get_mask, prep_nametag_images, resource_path)
import tools.nametag_diag as ND                                       # noqa: E402
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot        # noqa: E402

FAIL: list = []
TMPDIR = "debug/_verify_nametag_diag"


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


CFG = load_yaml("config/config_default.yaml")
THRES = float(CFG["nametag"]["diff_thres"])
UI_Y = int(CFG["ui_coords"]["ui_y_start"])


def make_frame_with_tag(tag_bgr, at=(300, 200), size=(768, 1366)):
    '''造一张合成画面：中灰底 + 指定位置贴上名牌模板。'''
    rng = np.random.default_rng(20261012)
    f = rng.integers(60, 140, size=(size[0], size[1], 3), dtype=np.uint8)
    h, w = tag_bgr.shape[:2]
    y, x = at
    f[y:y + h, x:x + w] = tag_bgr
    return f


print("=" * 70)
print("名字定位失败帧判定工具 · 自检")
print("=" * 70)
shutil.rmtree(resource_path(TMPDIR), ignore_errors=True)

# 拿一个真实存在的模板（优先当前方案里的），没有就现场造一个
_tpl_name = load_yaml("config/config_default.yaml")["nametag"]["name"]
try:
    from src.utils.common import active_config_path, override_cfg
    _ap = active_config_path()
    if _ap:
        _t = override_cfg(load_yaml("config/config_default.yaml"), load_yaml(_ap))
        _tpl_name = _t["nametag"]["name"]
except Exception:                                                # noqa: BLE001
    pass
_tpl_path = f"nametag/{_tpl_name}.png"
if not os.path.exists(_tpl_path):
    os.makedirs("nametag", exist_ok=True)
    _synth = np.zeros((22, 43, 3), dtype=np.uint8)
    _synth[:, :] = (0, 255, 0)
    cv2.putText(_synth, "AB", (3, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    imwrite_unicode(_tpl_path, _synth)
    print(f"  （现场造了个测试模板：{_tpl_path}）")
tpl_bgr = load_image(_tpl_path)
tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
print(f"  模板 = {_tpl_path}  {tpl_bgr.shape[1]}x{tpl_bgr.shape[0]}")

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】名牌**在**画面里 → 结论必须说「匹配良好」并给出正确位置")
# ══════════════════════════════════════════════════════════════════════════
f1 = make_frame_with_tag(tpl_bgr, at=(200, 300))
a1 = ND.analyze_frame(f1, tpl_gray, tpl_bgr, UI_Y, "grayscale", THRES)
t1, k1 = ND.verdict(a1, THRES)
check("① 结论分类 = ok", k1, "ok")
check_true("② 最佳分数过阈值", a1["best"] < THRES)
check("③ 位置对得上（贴的是 (300,200)）", (a1["bx"], a1["by"]), (300, 200))
check_true("④ 比地板值低足够多倍（说明是真的突出，不是杂色）",
           a1["ratio"] >= ND.GOOD_RATIO)
print(f"      实际：best={a1['best']:.4f} floor={a1['floor']:.4f} "
      f"ratio={a1['ratio']:.0f}x @({a1['bx']},{a1['by']})")

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】★ 名牌**不在**画面里 → 结论必须说「找不到」，不许说成「匹配弱」")
# ══════════════════════════════════════════════════════════════════════════
f2 = make_frame_with_tag(tpl_bgr, at=(200, 300))
h, w = tpl_bgr.shape[:2]
f2[200:200 + h, 300:300 + w] = 0          # 把名牌抹掉
a2 = ND.analyze_frame(f2, tpl_gray, tpl_bgr, UI_Y, "grayscale", THRES)
t2, k2 = ND.verdict(a2, THRES)
check("① 结论分类 = bad", k2, "bad")
check_true("② 最佳分数**不过**阈值", a2["best"] >= THRES)
check_true("③ 最佳分数与地板值相当（余量 ≈ 1 倍）", a2["ratio"] < 2.0)
check_true("④ 结论文字点明「画面里根本找不到名牌」", "找不到名牌" in t2)
check_true("⑤ 结论文字把人引向「为什么不可见」，而不是让人去调阈值",
           "不可见" in t2)
print(f"      实际：best={a2['best']:.4f} floor={a2['floor']:.4f} ratio={a2['ratio']:.1f}x")

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】预处理必须与引擎**同源**（prep_nametag_images）")
# ══════════════════════════════════════════════════════════════════════════
r1, n1 = prep_nametag_images(tpl_gray, tpl_gray, "grayscale")
check("① grayscale → 原样返回（同一对象）", (r1 is tpl_gray, n1 is tpl_gray), (True, True))
# ⚠️ 模板灰度**全 ≥150**（绿底灰度≈150、白字≈255），喂它进 white_mask 只会得到一个
#    恒为 255 的图 —— 测不出"二值化"这件事。要喂一张**暗+亮都有**的图才算测到。
_mixed = np.zeros((20, 43), dtype=np.uint8)
_mixed[10:] = 255
rm, _nm = prep_nametag_images(_mixed, tpl_gray, "white_mask")
check("② white_mask 对暗+亮混合图 → 真的分成 0 与 255",
      sorted(set(np.unique(rm).tolist())), [0, 255])
r3, n3 = prep_nametag_images(tpl_gray, tpl_gray, "histogram_eq")
check("③ histogram_eq → 二值化",
      sorted(set(np.unique(n3).tolist()))[-3:], [0, 255])
r4, n4 = prep_nametag_images(tpl_gray, tpl_gray, "不存在的模式")
check("④ 不认识的模式 → (None, None)，由调用方报错", (r4, n4), (None, None))
# 引擎必须调的就是这个函数（否则工具与引擎会分裂）
import inspect                                                   # noqa: E402
_src = inspect.getsource(MapleStoryAutoBot._locate_by_nametag_single)
check_true("⑤ 引擎的名字定位主体里调的确实是 prep_nametag_images",
           "prep_nametag_images(" in _src)
# ── 多模板（2026-10-12 加）──────────────────────────────────────────────
check_true("⑥ get_player_location_by_nametag 是**多模板入口**（逐个试取最好）",
           hasattr(MapleStoryAutoBot, "get_player_location_by_nametag")
           and hasattr(MapleStoryAutoBot, "_locate_by_nametag_single"))
_wrap = inspect.getsource(MapleStoryAutoBot.get_player_location_by_nametag)
check_true("⑦ 单模板时必须走原路（零开销、零行为变化）",
           "len(tpls) <= 1" in _wrap and "_locate_by_nametag_single()" in _wrap)
check_true("⑧ 必须把胜出那次的状态还原回去（否则最后一名覆盖第一名）",
           "nametag_miss_streak" in _wrap and "best[1]" in _wrap)
# ⚠️ 空列表经 load_yaml 回来可能是 ()（项目里 list/tuple 混着用），所以判"空"而不是判 []
check_true("⑨ config 里有 nametag.alt_names（默认空 = 老行为）",
           "alt_names" in CFG["nametag"] and not CFG["nametag"]["alt_names"])

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】失败帧落盘的安全约束（节流 / 上限 / 绝不抛）")
# ══════════════════════════════════════════════════════════════════════════
DUMPS = resource_path(os.path.join("debug", "名字定位失败"))
_before = set(os.listdir(DUMPS)) if os.path.isdir(DUMPS) else set()


def make_bot(max_n=2, interval=0.0, after=30):
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.cfg = {"nametag": dict(CFG["nametag"]), "ui_coords": dict(CFG["ui_coords"])}
    b.cfg["nametag"].update({"fail_dump_after": after, "fail_dump_max": max_n,
                             "fail_dump_interval": interval, "mode": "grayscale"})
    b.img_frame = np.zeros((24, 32, 3), dtype=np.uint8)   # 小图，便于清理
    b.nametag_miss_streak = 99
    b.loc_nametag = (10, 10)
    b.nametag_ever_hit = True
    b.nametag_score = 0.0224
    b._nt_dump_n = 0
    b._nt_dump_t = 0.0
    return b


b = make_bot(max_n=2, interval=0.0)
for _ in range(6):
    b._dump_nametag_failure()
check("① 上限生效：连调 6 次只落 2 张", b._nt_dump_n, 2)

b = make_bot(max_n=5, interval=9999.0)
b._dump_nametag_failure()
b._dump_nametag_failure()
check("② 节流生效：间隔内第二次不落盘", b._nt_dump_n, 1)

b = make_bot(max_n=5, interval=0.0, after=30)
b.nametag_miss_streak = 29
b._dump_nametag_failure()
check("③ 连败帧数不到阈值 → 不落盘", b._nt_dump_n, 0)

b = make_bot(max_n=5, interval=0.0)
b.cfg["nametag"]["fail_dump_after"] = 0
b._dump_nametag_failure()
check("④ 设 0 = 关闭", b._nt_dump_n, 0)

# 坏输入绝不抛（主循环不能被它带崩）
b = make_bot(max_n=5, interval=0.0)
b.img_frame = None
try:
    b._dump_nametag_failure()
    check("⑤ img_frame=None 时不抛异常", True, True)
except Exception as e:                                           # noqa: BLE001
    check(f"⑤ img_frame=None 时不许抛（实际抛了 {e!r}）", False, True)

b = make_bot(max_n=5, interval=0.0)
b.cfg = None                                                     # 配置整个坏掉
try:
    b._dump_nametag_failure()
    check("⑥ 配置坏掉时也不抛异常", True, True)
except Exception as e:                                           # noqa: BLE001
    check(f"⑥ 配置坏掉时不许抛（实际抛了 {e!r}）", False, True)

# 真落盘的元数据文件里必须带"怎么判定"的指引
b = make_bot(max_n=1, interval=0.0)
b._dump_nametag_failure()
if os.path.isdir(DUMPS):
    _new = [f for f in os.listdir(DUMPS) if f not in _before]
    _txt = [f for f in _new if f.endswith(".txt")]
    check_true("⑦ 落了 frame.png", any(f.endswith("_frame.png") for f in _new))
    if _txt:
        _body = open(os.path.join(DUMPS, _txt[0]), encoding="utf-8").read()
        check_true("⑧ 元数据里写了分数与阈值", "最佳分数" in _body and "阈值" in _body)
        check_true("⑨ 元数据里给了判定命令", "nametag_diag" in _body)
    else:
        check("⑦ 落了 frame.png", False, True)

# 清理本次自检产生的落盘文件
for f in (os.listdir(DUMPS) if os.path.isdir(DUMPS) else []):
    if f not in _before:
        try:
            os.remove(os.path.join(DUMPS, f))
        except OSError:
            pass
shutil.rmtree(resource_path(TMPDIR), ignore_errors=True)

print("\n" + "=" * 70)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
