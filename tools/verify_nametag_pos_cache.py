# -*- coding: utf-8 -*-
'''「名字锚点按地图存盘」的自检（2026-10-14 加，配合 MapleStoryAutoBot
._load_nametag_pos / _save_nametag_pos）。

## 它防的是什么（真机现场：只走不打，而且不报错）

用户问「名字和称号都看不到的时候怎么定位角色」。引擎的处置是「位置冻住」——
匹配失败时 `loc_nametag` 保持**上一帧**的值，这在挂机途中基本无害
（同一张图里角色屏幕位置几乎是死的）。但**开局那几帧没有上一帧**：

    self.loc_nametag = (0, 0)          # __init__ 里的初值
    loc_player = (0 + w//2, 0 - offset[1])

⇒ 攻击框落在画面左上角 ⇒ 症状是「只走路、不打怪」，而且**一句错都不报**。

治法（本自检钉的就是它）：把每次命中的锚点按地图存到
`minimaps/<图>/nametag_pos.json`，开局先读回来用。

## 三条口径（缺一条这个功能就会变成新的静默故障）

1. **fail-closed**：文件缺失 / 读不动 / 字段不全 / 坐标算出来在画面外
   ⇒ 一律当"没存过"，绝不猜、绝不放行；
2. **fail-open**：写盘失败**不许**影响挂机（它只是"下次开局更好看"的加速器），
   只留一条 WARNING，且不刷屏；
3. **不动识别逻辑**：匹配/阈值/兜底一行没改，存盘只是"顺带记一笔"。

用法：
    python -m tools.verify_nametag_pos_cache
'''
from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml                                                     # noqa: E402

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402
from src.utils.cfg_names import cn_key                          # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FAIL: list = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


def make_bot(path, offset=None, pos_cache=True, loc=(0, 0)):
    '''造一个只带本功能所需状态的最小实例（不碰 Qt、不读图片）。'''
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.cfg = {
        "bot": {"map": "测试图"},
        "nametag": {"offset": list(offset or [-50, 30]), "pos_cache": pos_cache},
        "directional_attack": {"range_y": 70},
    }
    b.loc_nametag = tuple(loc)
    b.loc_nametag_from_cache = False
    b.t_nametag_save = 0.0
    b.nametag_pos_save_failed = False
    b.img_frame = None            # 取不到帧尺寸 ⇒ 合理性只做「不能是负数」那条
    b._nametag_pos_path = lambda: path
    return b


TMP = tempfile.mkdtemp(prefix="nametag_pos_cache_")
POS = os.path.join(TMP, "nametag_pos.json")

print("=" * 70)
print("名字锚点存盘 · 自检")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】存盘 → 读回：往返一致（含 loc_player 反算）")
# ══════════════════════════════════════════════════════════════════════════
b = make_bot(POS, loc=(600, 400))
check_true("① 写盘成功", b._save_nametag_pos(w_match=60, score=0.0021, force=True))
check_true("② 文件真的落地了", os.path.exists(POS))
data = json.load(open(POS, encoding="utf-8"))
check("③ 存的是**锚点**坐标（不是 loc_player）", data["loc_nametag"], [600, 400])
# offset[1] = 30 ⇒ 角色中心 y = 400 - 30 = 370；w//2 = 30 ⇒ x = 600 + 30 = 630
check("④ 顺带存的 loc_player 与引擎公式一致（600+60//2, 400-30）",
      data["loc_player"], [630, 370])
check("⑤ 记了这张图的名字（换图不会串味）", data["map"], "测试图")
check_true("⑥ 记了当时的分数（排查用）", data["score"] == 0.0021)
check_true("⑦ 文件里有人话说明（删掉它=回滚）",
           "删掉本文件" in data["note"] and "minimaps" not in data["note"])
print(f"      · 文件内容：{json.dumps(data, ensure_ascii=False)[:150]}…")

b2 = make_bot(POS, loc=(0, 0))
check_true("⑧ 新实例开局读得回来", b2._load_nametag_pos(w_match=60))
check("⑨ loc_nametag 被换成存盘值", b2.loc_nametag, (600, 400))
check_true("⑩ 打上了「来自存盘」的标记（告警文案据此分流）", b2.loc_nametag_from_cache)

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】fail-closed：读不动 / 字段不全 / 画面外 一律当「没存过」")
# ══════════════════════════════════════════════════════════════════════════
missing = os.path.join(TMP, "不存在.json")
b3 = make_bot(missing, loc=(0, 0))
check("① 文件不存在 → False 且不改 loc_nametag",
      (b3._load_nametag_pos(w_match=60), b3.loc_nametag), (False, (0, 0)))
check_true("② 文件不存在 → 不打「来自存盘」标记", not b3.loc_nametag_from_cache)

broken = os.path.join(TMP, "坏.json")
with open(broken, "w", encoding="utf-8") as fp:
    fp.write("{ 这不是 json")
b4 = make_bot(broken, loc=(0, 0))
check("③ 坏 json → False（不抛异常）", b4._load_nametag_pos(w_match=60), False)
check("④ 坏 json 不改 loc_nametag", b4.loc_nametag, (0, 0))

nofield = os.path.join(TMP, "缺字段.json")
json.dump({"map": "测试图"}, open(nofield, "w", encoding="utf-8"))
b5 = make_bot(nofield, loc=(0, 0))
check("⑤ 缺 loc_nametag 字段 → False", b5._load_nametag_pos(w_match=60), False)

badtype = os.path.join(TMP, "类型错.json")
json.dump({"loc_nametag": [None, 400]}, open(badtype, "w", encoding="utf-8"))
b6 = make_bot(badtype, loc=(0, 0))
check("⑥ 坐标不是数字 → False", b6._load_nametag_pos(w_match=60), False)

outside = os.path.join(TMP, "画面外.json")
json.dump({"loc_nametag": [-999, -999]}, open(outside, "w", encoding="utf-8"))
b7 = make_bot(outside, loc=(0, 0))
check("⑦ ★ 存盘坐标算出来在画面外 → 不采用（复用假命中护栏）",
      b7._load_nametag_pos(w_match=60), False)
check("⑧ 不采用时 loc_nametag 保持原值", b7.loc_nametag, (0, 0))

off = os.path.join(TMP, "开关关掉.json")
b8 = make_bot(off, pos_cache=False, loc=(600, 400))
check("⑨ nametag.pos_cache=false → 不写盘", b8._save_nametag_pos(60, force=True), False)
check_true("⑩ nametag.pos_cache=false → 不读盘（文件也没被写出来）",
           not os.path.exists(off))
b9 = make_bot(POS, pos_cache=False, loc=(0, 0))
check("⑪ 开关关掉时即使文件在也不读", b9._load_nametag_pos(w_match=60), False)

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】fail-open：写盘失败不影响挂机，且只吼一次")
# ══════════════════════════════════════════════════════════════════════════
blocker = os.path.join(TMP, "我是文件不是目录")
with open(blocker, "w", encoding="utf-8") as fp:
    fp.write("x")
bad_path = os.path.join(blocker, "sub", "nametag_pos.json")
b10 = make_bot(bad_path, loc=(600, 400))
check("① 写盘失败 → 返回 False（不抛异常）",
      b10._save_nametag_pos(60, force=True), False)
check_true("② 失败被记账（用于「只吼一次」）", b10.nametag_pos_save_failed)
check("③ 失败后再写一次仍然只是 False", b10._save_nametag_pos(60, force=True), False)

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】节流：5 秒内不重复写，force=True 例外")
# ══════════════════════════════════════════════════════════════════════════
thr = os.path.join(TMP, "节流.json")
b11 = make_bot(thr, loc=(600, 400))
check_true("① 第一次写（t_nametag_save 初始为 0 ⇒ 不节流）",
           b11._save_nametag_pos(60, score=0.001, force=False))
b11.loc_nametag = (700, 500)
check("② 紧接着再写 → 被节流挡掉", b11._save_nametag_pos(60, force=False), False)
check("③ ★ 被挡掉时文件内容**没变**（还是上一笔）",
      json.load(open(thr, encoding="utf-8"))["loc_nametag"], [600, 400])
check_true("④ force=True 能立刻刷进去", b11._save_nametag_pos(60, force=True))
check("⑤ force 之后文件是新值",
      json.load(open(thr, encoding="utf-8"))["loc_nametag"], [700, 500])
check_true("⑥ 真命中之后就不再是「开局用存盘位置」状态",
           b11.loc_nametag_from_cache is False)
check_true("⑦ 写盘走临时文件 + os.replace（不留半截 json）",
           ".tmp" in inspect.getsource(MapleStoryAutoBot._save_nametag_pos)
           and "os.replace" in inspect.getsource(MapleStoryAutoBot._save_nametag_pos))

# ══════════════════════════════════════════════════════════════════════════
print("\n【5】接线：load_config 要用、命中那一刻要存、告警要分流")
# ══════════════════════════════════════════════════════════════════════════
_lc = inspect.getsource(MapleStoryAutoBot.load_config)
check_true("① load_config 里调了 _load_nametag_pos()", "_load_nametag_pos(" in _lc)
check_true("② 读盘发生在名字模板加载**之后**（那时才知道模板宽度）",
           _lc.index("img_nametag_gray = load_image") < _lc.index("_load_nametag_pos("))
check_true("③ 读盘用的是模板宽度，不是拍脑袋的常数",
           "_load_nametag_pos(w_match=self.img_nametag.shape[1])" in _lc)

_nt = inspect.getsource(MapleStoryAutoBot.get_player_location_by_nametag)
check_true("④ 命中那一刻调了 _save_nametag_pos()", "_save_nametag_pos(" in _nt)
check_true("⑤ 存盘挂在「命中」分支里（不是失败分支）",
           _nt.index("self.nametag_hit = True")
           < _nt.index("_save_nametag_pos(")
           < _nt.index("self.nametag_miss_streak += 1"))
check_true("⑥ 传给存盘的是真实的匹配宽度 w", "_save_nametag_pos(w_match=w" in _nt)

_ro = inspect.getsource(MapleStoryAutoBot.run_once)
check_true("⑦ 告警按「开局用的存盘位置」分流", "loc_nametag_from_cache" in _ro)
check_true("⑧ 走存盘位置时是 WARNING（比 ERROR 轻，但不是静默）",
           "帧没认出角色名名牌" in _ro and "正用**上次保存的位置**" in _ro)
check_true("⑨ 没有存盘位置时仍是 ERROR + 讲清后果",
           "位置仍是初始值" in _ro and "攻击框会落在画面左上角" in _ro)
check_true("⑩ 两条告警都指得出文件位置（可指认的销账判据）",
           "_nametag_pos_path()" in _ro)

# ══════════════════════════════════════════════════════════════════════════
print("\n【6】不动识别逻辑：匹配 / 阈值 / 兜底一行没改")
# ══════════════════════════════════════════════════════════════════════════
check_true("① 匹配仍是 find_pattern_sqdiff",
           "find_pattern_sqdiff" in _nt or "cv2.matchTemplate" in _nt)
check_true("② 阈值仍是 nametag.diff_thres", 'self.cfg["nametag"]["diff_thres"]' in _nt)
check_true("③ 切块兜底还在（split_width）", "split_width" in _nt)
check_true("④ 定位函数体里没有 json 读写（I/O 全在 _save_nametag_pos 里）",
           "json." not in _nt)

# ══════════════════════════════════════════════════════════════════════════
print("\n【7】配置与词表：默认开、有人话说明、有中文名")
# ══════════════════════════════════════════════════════════════════════════
_cfg_text = open(os.path.join(REPO, "config", "config_default.yaml"),
                 encoding="utf-8").read()
_cfg = yaml.safe_load(_cfg_text)
check("① config_default 里 nametag.pos_cache 默认 true",
      _cfg["nametag"].get("pos_cache"), True)
check_true("② 注释里写明了存盘位置与回滚办法",
           "minimaps/<图>/nametag_pos.json" in _cfg_text and "删掉那个 json" in _cfg_text)
check_true("③ cfg_names 有中文名（高级设置放出该段时不会露出英文 key）",
           bool(cn_key("nametag", "pos_cache")) and cn_key("nametag", "pos_cache") != "pos_cache")
check_true("④ 方法写在名字定位那一带（不是别的类里）",
           "_nametag_pos_path" in inspect.getsource(MapleStoryAutoBot)
           and hasattr(MapleStoryAutoBot, "_load_nametag_pos")
           and hasattr(MapleStoryAutoBot, "_save_nametag_pos"))

shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 70)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
