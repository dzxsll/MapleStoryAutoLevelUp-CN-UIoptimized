# -*- coding: utf-8 -*-
'''丢名字时存画面（诊断埋点）自检（2026-10-14 加）。

背景（用户真机反馈 + 日志取证）：
    2026-10-14 12:43 那轮真机只跑了 4.6 分钟，`[名字定位] 已连续 N 帧没匹配到`
    告警就有 **33 条**（miss 最高 71），可日志里**一张画面都没有** ⇒ 分不清到底是
      · 名牌被画面边缘裁掉（角色太靠上）  · 名牌被地形/其它玩家挡住
      · 角色根本不在画面里                · 分数判错（模板不贴合）
    这四种里的哪一种。用户拍板：加一个"丢名字时存一张画面"的诊断埋点。

做法：`MapleStoryAutoBot._dump_nametag_miss(streak)`（纯观测），调用点与
      「已连续 N 帧」告警**同源**（`_streak >= 30 and _streak % 30 == 0`）。
      存两张图到 `debug/`：
        · `nt_miss_<时间>_miss<N>_full.png`：整帧 + 绿框（上次命中的名牌位置）+
          黄十字（由此推出的角色位置）+ 红框（本帧索敌框）；
        · `nt_miss_<时间>_miss<N>_crop.png`：名牌本该在的位置 ±(模板 + 60px)，放大 3 倍。

本自检钉的是：
    [1] 配置 / 中文名 / 界面登记（以及 stale_pos_full_frame_after 默认 0 的语义来源）
    [2] 行为：存两张图、尺寸与三个标记的像素、文件名带 miss 数
    [3] 节流（5 秒）与开关（miss_dump: false）
    [4] fail-open：无帧 / 无模板 / 目录不可写 / 坐标离谱 —— 一律不抛异常、失败只吼一次
    [5] 白盒：调用点在告警块内；纯观测（不碰指令、不写存盘、不改 loc_nametag）

用法：
    python -m tools.verify_nametag_miss_dump
'''
import logging
import os
import shutil
import sys
import tempfile
import time

import numpy as np
import yaml

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    ok = bool(got)
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}")
    if not ok:
        FAIL.append(name)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


def _nav():
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    return _root


_ROOT = _nav()

TMP = tempfile.mkdtemp(prefix="nt_miss_dump_")
FRAME_H, FRAME_W = 200, 300
GREEN = (0, 255, 0)      # BGR
YELLOW = (0, 255, 255)
RED = (0, 0, 255)


def _files():
    return sorted(_f for _f in os.listdir(TMP) if _f.startswith("nt_miss_"))


def _base_cfg(**nt):
    _n = {"mode": "grayscale", "diff_thres": 0.01, "offset": [0, 30],
          "miss_dump": True, "relax_ratio": 2.0, "relax_max_jump": 12}
    _n.update(nt)
    return {"nametag": _n,
            "monster_detect": {"detect_range_scale": 2.0, "detect_range_scale_y": 1.1,
                               "search_box_margin": 50, "engaged_scan": True,
                               "stale_pos_full_frame_after": 0},
            "bot": {"attack": "aoe_skill"},
            "directional_attack": {"range_x": 400, "range_y": 70},
            "aoe_skill": {"range_x": 340, "range_y": 140}}


SEARCH_BOX = (10, 10, 290, 190)


def make_stub(cfg=None, frame=None, loc_nametag=(100, 50), tpl=(20, 60),
              search_box=SEARCH_BOX, debug_dir=TMP):
    '''造一个只够跑 _dump_nametag_miss 的假引擎（**不跑 __init__**）。

    绑的是引擎**真方法**（保证"测的就是跑的那份"）；`get_search_box` 用假的，
    这样画红框这一步可以被像素级验证，而不必把整条攻击链都搭起来。
    '''
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    class S:
        pass

    s = S()
    s.cfg = cfg if cfg is not None else _base_cfg()
    s.img_frame = np.zeros((FRAME_H, FRAME_W, 3), np.uint8) if frame is None else frame
    s.img_nametag = None if tpl is None else np.zeros((tpl[0], tpl[1], 3), np.uint8)
    s.loc_nametag = tuple(loc_nametag)
    s.nametag_score = 0.0233
    s.nametag_miss_streak = 30
    s.loc_player = (130, 20)
    s._stop_hold = False
    s.t_nt_miss_dump = 0.0
    s.nt_miss_dump_failed = False
    s.debug_dir = debug_dir
    s._get_search_box_calls = 0

    def _sb():
        s._get_search_box_calls += 1
        return search_box

    s.get_search_box = _sb
    s._nt_miss_dump_dir = getattr(MapleStoryAutoBot, "_nt_miss_dump_dir").__get__(s)
    s._dump_nametag_miss = getattr(
        MapleStoryAutoBot, "_dump_nametag_miss").__get__(s)
    return s


# ══════════════════════════════════════════════════════════════════════════
print("=" * 72)
print("[1] 配置 / 中文名 / 界面登记")
print("=" * 72)
with open(os.path.join(_ROOT, "config", "config_default.yaml"), encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f) or {}
_nt = _cfg.get("nametag") or {}
_md = _cfg.get("monster_detect") or {}
check("★ nametag.miss_dump 默认开（诊断埋点）", _nt.get("miss_dump"), True)
check("★ stale_pos_full_frame_after 默认 0（= 关掉整帧放大，判据在 get_search_box）",
      _md.get("stale_pos_full_frame_after"), 0)
check("nametag.relax_ratio 仍为 2.0（上一轮收紧的结论没被本改动碰掉）",
      _nt.get("relax_ratio"), 2.0)

from src.utils.cfg_names import KEY_CN

check_true("cfg_names.KEY_CN 有 miss_dump 的中文名", "miss_dump" in KEY_CN)

with open(os.path.join(_ROOT, "src", "ui", "ui.py"), encoding="utf-8") as _f:
    _ui = _f.read()
check_true("ui.py 把 miss_dump 收进 ADV_FIELD_HIDE（诊断开关不给用户乱按）",
           "'miss_dump'" in _ui)
check_true("ui.py 仍收起 stale_pos_full_frame_after",
           "'stale_pos_full_frame_after'" in _ui)

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

_SRC = open(os.path.join(_ROOT, "src", "engine", "MapleStoryAutoLevelUp.py"),
            encoding="utf-8").read()
check_true("引擎 import 了 imwrite_unicode（中文路径写图安全）",
           "imwrite_unicode," in _SRC)
check_true("__init__ 里初始化 t_nt_miss_dump / nt_miss_dump_failed",
           "self.t_nt_miss_dump = 0.0" in _SRC
           and "self.nt_miss_dump_failed = False" in _SRC)

# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[2] 行为：存两张图 + 三个标记的像素 + 文件名")
print("=" * 72)
s = make_stub()
s._dump_nametag_miss(30)
_fs = _files()
check("一次调用 ⇒ 两个文件（full + crop）", len(_fs), 2)
check_true("文件是 PNG", all(_f.endswith(".png") for _f in _fs))
check_true("文件名带 miss 数（_miss30_）", all("_miss30_" in _f for _f in _fs))
check_true("文件名带 full / crop", any("_full.png" in _f for _f in _fs)
           and any("_crop.png" in _f for _f in _fs))
check_true("存到了 debug_dir（传进去的目录）", all(
    os.path.dirname(os.path.join(TMP, _f)) == TMP for _f in _fs))

import cv2

_full_p = os.path.join(TMP, [f for f in _fs if "_full" in f][0])
_crop_p = os.path.join(TMP, [f for f in _fs if "_crop" in f][0])
_full = cv2.imread(_full_p)
_crop = cv2.imread(_crop_p)
check("整帧图尺寸 = 画面尺寸（200x300）", (_full.shape[0], _full.shape[1]), (200, 300))
# 模板 (20,60) ⇒ m = max(60,20)+60 = 120；名牌 (100,50) ⇒
#   cx0,cy0 = 0,0；cx1 = min(300, 100+60+120) = 280；cy1 = min(200, 50+20+120) = 190
check("裁图尺寸 = (190×280) 放大 3 倍（最近邻）",
      (_crop.shape[0], _crop.shape[1]), (570, 840))
check_true("绿框：上次命中的名牌左上角像素是绿的", tuple(_full[50, 100]) == GREEN)
check_true("绿框：名牌右下角也画到了", tuple(_full[70, 160]) == GREEN)
check_true("黄十字：角色位置中心是黄的（x+w/2, y-offset）",
           tuple(_full[20, 130]) == YELLOW)
check_true("红框：索敌框左上角是红的（= get_search_box 的返回值）",
           tuple(_full[10, 10]) == RED)
check("画框时确实问了本帧索敌框（调用 1 次）", s._get_search_box_calls, 1)
check_true("裁图里也画了绿框（名牌本该在的位置）",
           tuple(_crop[int((50 - 0) * 3), int((100 - 0) * 3)]) == GREEN)
check_true("整帧图是**副本**，原图没被涂改（诊断不许污染画面）",
           tuple(s.img_frame[50, 100]) == (0, 0, 0))

_log_i = LogCapture()
logging.getLogger().addHandler(_log_i)
try:
    s_info = make_stub()
    s_info._dump_nametag_miss(90)
    _msg = " | ".join(_log_i.lines)
    check_true("打了一行 INFO（含 [名字定位·丢画面] 与 miss 数）",
               "[名字定位·丢画面]" in _msg and "miss=90" in _msg)
    check_true("INFO 里说明了三个标记各自是什么",
               "绿框" in _msg and "黄十字" in _msg and "红框" in _msg)
finally:
    logging.getLogger().removeHandler(_log_i)

# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[3] 节流 5 秒 + 开关 miss_dump")
print("=" * 72)
_n0 = len(_files())
s._dump_nametag_miss(60)
check("5 秒内再来一次 ⇒ 不新增文件", len(_files()), _n0)
s.t_nt_miss_dump = time.time() - 5.1
s._dump_nametag_miss(61)
check("过了 5 秒 ⇒ 放行（新增两个）", len(_files()), _n0 + 2)
check_true("这一批的文件名带 _miss61_", any("_miss61_" in _f for _f in _files()))

_n1 = len(_files())
s_off = make_stub(cfg=_base_cfg(miss_dump=False))
s_off._dump_nametag_miss(30)
check("miss_dump: false ⇒ 一张都不存", len(_files()), _n1)

s_missing = make_stub(cfg={"nametag": {"offset": [0, 30]}})
s_missing._dump_nametag_miss(33)
check("nametag 段缺 miss_dump 键 ⇒ 按默认**开**（存了）", len(_files()), _n1 + 2)

# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[4] fail-open：没帧 / 没模板 / 目录不可写 / 坐标离谱")
print("=" * 72)
_n2 = len(_files())
s_noframe = make_stub()
s_noframe.img_frame = None
s_noframe._dump_nametag_miss(30)
check("没有画面 ⇒ 不存、不抛异常", len(_files()), _n2)

s_empty = make_stub()
s_empty.img_frame = np.zeros((0, 0, 3), np.uint8)
s_empty._dump_nametag_miss(30)
check("空画面 ⇒ 不存、不抛异常", len(_files()), _n2)

s_notpl = make_stub(tpl=None)
s_notpl._dump_nametag_miss(34)
check("拿不到名字模板 ⇒ 用兜底尺寸照样存（2 张）", len(_files()), _n2 + 2)

s_neg = make_stub(loc_nametag=(-50, -30))
s_neg.t_nt_miss_dump = 0.0
s_neg._dump_nametag_miss(35)
check("名牌坐标为负 ⇒ 不崩（crop clamp 后仍非空）", len(_files()), _n2 + 4)

s_far = make_stub(loc_nametag=(9999, 9999))
s_far.t_nt_miss_dump = 0.0
s_far._dump_nametag_miss(36)
check("名牌坐标远在画面外 ⇒ 不崩（crop 空则回退整帧）", len(_files()), _n2 + 6)

# 目录不可写：拿一个**文件**当目录
_bad = os.path.join(TMP, "not_a_dir")
with open(_bad, "w", encoding="utf-8") as _f:
    _f.write("x")
_log = LogCapture()
logging.getLogger().addHandler(_log)
try:
    s_bad = make_stub(debug_dir=_bad)
    s_bad._dump_nametag_miss(30)          # 不抛异常
    check_true("目录不可写 ⇒ 不抛异常（fail-open）", True)
    check("第一次失败 ⇒ 置 nt_miss_dump_failed", s_bad.nt_miss_dump_failed, True)
    _n_warn = len([_l for _l in _log.lines if "丢画面" in _l and "失败" in _l])
    check("第一次失败 ⇒ 吼一行 WARNING", _n_warn, 1)
    s_bad.t_nt_miss_dump = 0.0
    s_bad._dump_nametag_miss(30)
    _n_warn2 = len([_l for _l in _log.lines if "丢画面" in _l and "失败" in _l])
    check("再失败 ⇒ 只吼一次（不刷屏）", _n_warn2, 1)
finally:
    logging.getLogger().removeHandler(_log)

# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[5] 白盒：调用点在告警块内；纯观测")
print("=" * 72)
_i_streak = _SRC.index("if _streak >= 30 and _streak % 30 == 0:")
check_true("调用点紧跟『_streak >= 30 and _streak % 30 == 0』（与告警同源）",
           "self._dump_nametag_miss(_streak)" in _SRC[_i_streak:_i_streak + 300])
_fun = _SRC[_SRC.index("def _dump_nametag_miss(self, streak):"):]
_fun = _fun[:_fun.index("def _nametag_pos_plausible(")]
check_true("纯观测：不碰指令（self.cmd_）", "self.cmd_" not in _fun)
check_true("纯观测：不写位置存盘", "_save_nametag_pos" not in _fun)
check_true("纯观测：不改 loc_nametag", "self.loc_nametag =" not in _fun)
check_true("纯观测：不改 nametag_miss_streak",
           "self.nametag_miss_streak =" not in _fun)
check_true("5 秒节流写在函数里（不靠调用点克制）", "< 5.0" in _fun)
check_true("存储失败只吼一次（nt_miss_dump_failed 兜底）",
           "nt_miss_dump_failed" in _fun)
check_true("整个函数体被 try/except 包住（异常不外泄到主循环）",
           "except Exception as e:" in _fun)
check_true("自检可以覆盖目录（self.debug_dir），不改用户 debug/",
           "getattr(self, \"debug_dir\", None)" in _SRC)

# ══════════════════════════════════════════════════════════════════════════
shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 72)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项：" + ", ".join(FAIL))
    print("=" * 72)
    sys.exit(1)
print("✅ 全部通过")
print("=" * 72)
