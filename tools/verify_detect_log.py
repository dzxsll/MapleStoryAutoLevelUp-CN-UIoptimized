# -*- coding: utf-8 -*-
'''`[怪检测]` 明细行 + `[出招·延迟]` 自检（2026-10-14 加）。

背景（用户 m06852）：
    「记录下怪物的检测信息，现在的攻击还是不够灵敏」
    真机 `log/MSBot_2026-10-14_14-06-12.log`（5.3 分钟）里：
      · `[出招]` 心跳最多 37 次/5 秒、**也有 0 次**的窗口，最长空档 3~11 秒；
      · 17 条带数字的归因**全是** `交叠 0/868 px²`、`中心已在攻击框内的有 0 个`，
        中心离攻击框 **水平 0~51px / 垂直 23~81px** ⇒ 多数是"上下差四十几像素"
        （攻击框 ±180×±80，竖向只有 ±80）—— 眼睛看着"就在眼前"，引擎说够不着；
      · `[运行状态]` 只有「怪N」：怪在**哪**、离框多远、引擎以为角色在哪，全看不到。

做法：文案收在 `src/utils/common.py` 的两个纯函数里
      （`monster_box_detail` 单只明细 / `monster_detect_snapshot` 整行）；
      引擎只负责"凑事实 + 打一行"（`MapleStoryAutoBot._log_monster_detect`，
      纯观测、指纹变了且 ≥1s 立刻打、否则 ≥5s 打一次、开关 `detect_log`）。

本文件钉六件事：
  【1】单只明细真值表：中心∈框 / 四个方向 / 斜向 / 部分交叠 / 非法输入不炸
  【2】整行：表头四段都在；`box=None` / `player=None` 各自少一段；明细上限与
        "还有 N 只没列"；框内的排在前面（★）框外的在后（·）
  【3】引擎行为：真的会打；同一批怪 5 秒内不重复；指纹变了 ≥1s 立刻打；
        5 秒后的心跳行；`detect_log: false` 关得掉；异常只吼一次（失败不静默）
  【4】`[出招·延迟]`：进框 → 出刀 的毫秒数；锁定补招那一刀（engage_n=0）不算；
        算过就清基准（不重复报）
  【5】接线白盒：调用点在 `_engage/_far` 之后、`_md_stop` 之前；判据名只有一处读
        （`mob_engage_mode` 全文件只 `get` 一次）；纯观测不碰指令
  【6】真机现场回归：`mob(476,317)` 那帧 ⇒ 不出招 + 明细行里带 `交叠0 中心外 6px`
        与 `框内 0 / 框外 1`；`center` 与 `min_area` 两口径在**日志**上确实看得出差别
'''
import inspect
import logging
import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.utils.common import (monster_box_detail,            # noqa: E402
                              monster_detect_snapshot)
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402
import src.engine.MapleStoryAutoLevelUp as _mod                # noqa: E402

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望={want!r} 实际={got!r}")


def check_true(name, cond, detail=""):
    check(name + (f"  ({detail})" if detail else ""), bool(cond), True)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        try:
            self.msgs.append(record.getMessage())
        except Exception:                                     # noqa: BLE001
            pass

    def search(self, needle):
        return [m for m in self.msgs if needle in m]


class Clock:
    '''把引擎模块里的 `time.time` 换成可控时钟（节流判据全走 time.time）。'''

    def __init__(self, t0=1000.0):
        self.t = t0
        self._real = None

    def __enter__(self):
        self._real = _mod.time.time
        _mod.time.time = lambda: self.t
        return self

    def __exit__(self, *exc):
        _mod.time.time = self._real
        return False

    def tick(self, dt):
        self.t += dt


PLAYER = (300, 300)
DET = "[怪检测]"
DLY = "[出招·延迟]"
# 玩家 (300,300) + aoe 340×140 ⇒ 攻击框 x130~470 / y230~370
BOX = (130, 230, 470, 370)
NEED = 1500          # min(模板 40×40=1600, max_mob_area_trigger 1500)


def base_cfg(**md_over):
    md = {
        "every_n_frames": 1,
        "search_box_margin": 50,
        "max_mob_area_trigger": 1500,
        "stop_to_attack": True,
        "stop_attack_clear_frames": 2,
        "stop_attack_clear_s": 0.0,
        "stop_attack_timeout_s": 20,
        "target_hold_s": 0.0,
        "approach_mob": True,
        "approach_dy_tol": 60,
        "approach_confirm_s": 0.5,
        "approach_dir_min_s": 0.4,
        "detect_range_scale": 1.5,
        "detect_range_scale_y": 1.1,

        # ★ 2026-10-14：宽判定余量（`mob_engage_margin`）在本文件里**关掉** ——

        #   本文件钉的是别的判据（归因文案 / 明细行），余量会把所有几何期望挪 40px。

        #   余量自己的行为在 tools/verify_engage_margin.py 里钉。

        "mob_engage_margin": 0,
        "mob_engage_mode": "center",
    }
    md.update(md_over)
    return {
        "bot": {"attack": "aoe_skill"},
        "monster_detect": md,
        "character": {"width": 100, "height": 150},
        "aoe_skill": {"range_x": 340, "range_y": 140, "cooldown": 0.05},
        "directional_attack": {"range_x": 350, "range_y": 70,
                               "cooldown": 0.9, "turn_frames": 3},
    }


def mob(cx, cy, size=(40, 40)):
    '''按**中心点**造一只怪（引擎的 position 是左上角）。'''
    w, h = size
    return {"name": "m", "position": (cx - w // 2, cy - h // 2),
            "size": (w, h), "score": 0.1}


def make_stub(cfg, monsters=(), **kw):
    '''能走通索敌链 + 明细行的假引擎（**不跑 __init__**）。'''
    class S:
        pass

    s = S()
    for _n in ("get_attack_range", "get_nearest_monster", "get_attack_direction",
               "get_face_direction", "update_cmd_by_mob_detection",
               "get_detect_extent", "get_search_box",
               "_min_mob_area", "_mobs_in_attack_box", "_pick_approach_target",
               "_attack_box_half", "_game_window_front", "_mob_no_attack_log",
               "_note_attack", "_log_attack_heartbeat",
               "_engage_gap", "_engage_gap_facts",
               # 2026-10-14 加（本文件的对象）
               "_engage_mode", "_log_monster_detect",

               # 2026-10-14 加：梯上让路收紧（`update_cmd_by_mob_detection` 的让路块只问它）

               # 2026-10-14 加「宽判定余量」（B）：唯一读点 + 唯一拼框点


               "_engage_margin", "_attack_boxes",


               "_ladder_still_s", "_ladder_yield"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg
    s.loc_player = PLAYER
    s.monsters = list(monsters)
    s.monsters_info = {"m": [(np.zeros((40, 40, 3), np.uint8), None)]}
    s.is_on_ladder = False
    s.is_using_home_route = False
    s.t_last_attack = 0.0
    s.t_mob_noatk_log = 0.0
    s.t_detect_log = 0.0
    s._det_log_sig = None
    s._det_log_failed = False
    s._eng_first_t = None
    s.cmd_action = "none"
    s.cmd_move_x = "none"
    s.cmd_move_y = "none"
    s.cmd_move_x_last = "none"
    s._turn_frames_left = 0
    s._turn_dir = "none"
    s._turn_pending_dir = None
    s._attack_dir_last = None
    s.img_frame = np.zeros((600, 800, 3), np.uint8)
    s._perf = lambda *a, **k: None
    s._mob_skip = 0
    s._atk_n = 0
    s._atk_gap_n = 0
    s._atk_gap_sum = 0.0
    s._atk_gap_min = None
    s._atk_gap_max = 0.0
    s._atk_eng_min = None
    s._atk_eng_max = 0
    s.get_monsters_in_range = lambda *a, **k: s.monsters
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def frame(s, move_x="none", move_y="none"):
    '''模拟 HuntingState.on_frame 里与打怪有关的那一段。'''
    s.cmd_move_x, s.cmd_move_y, s.cmd_action = move_x, move_y, "none"
    s.update_cmd_by_mob_detection()
    s.cmd_move_x_last = s.cmd_move_x
    return (s.cmd_move_x, s.cmd_move_y, s.cmd_action)


_SRC = inspect.getsource(_mod)
_FUN = inspect.getsource(MapleStoryAutoBot._log_monster_detect)
_NOTE = inspect.getsource(MapleStoryAutoBot._note_attack)


# ══════════════════════════════════════════════════════════════════════════
print("=" * 72)
print("[1] 单只明细：中心∈框 / 四个方向 / 斜向 / 部分交叠 / 非法输入")
print("=" * 72)
check("  中心∈框（不写距离）",
      monster_box_detail((280, 280), (40, 40), BOX),
      "(300,300) 40×40 交叠1600 中心∈框")
check("  中心外·右",
      monster_box_detail((456, 297), (40, 40), BOX),
      "(476,317) 40×40 交叠560 中心外 6px×0px→")
check("  中心外·左",
      monster_box_detail((80, 280), (40, 40), BOX),
      "(100,300) 40×40 交叠0 中心外 30px×0px←")
check("  中心外·上",
      monster_box_detail((280, 200), (40, 40), BOX),
      "(300,220) 40×40 交叠400 中心外 0px×10px↑")
check("  中心外·下",
      monster_box_detail((280, 360), (40, 40), BOX),
      "(300,380) 40×40 交叠400 中心外 0px×10px↓")
check("  斜向（先上下后左右）",
      monster_box_detail((80, 200), (40, 40), BOX),
      "(100,220) 40×40 交叠0 中心外 30px×10px↑←")
check("  部分交叠（中心在框外、框还压着怪）",
      monster_box_detail((460, 290), (40, 40), BOX),
      "(480,310) 40×40 交叠400 中心外 10px×0px→")
check("  零尺寸也不炸", monster_box_detail((300, 300), (0, 0), BOX),
      "(300,300) 0×0 交叠0 中心∈框")
check("  非法坐标 ⇒ (?)", monster_box_detail("ab", (40, 40), BOX), "(?)")
check("  全 None ⇒ (?)", monster_box_detail(None, None, None), "(?)")
check("  缺框 ⇒ (?)", monster_box_detail((300, 300), (40, 40), None), "(?)")


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[2] 整行：表头四段 / 明细上限 / 框内排在前 / 缺参数各少一段")
print("=" * 72)
_row = monster_detect_snapshot([mob(300, 300)], [mob(476, 317)], box=BOX,
                               mode="center", need=NEED, player=PLAYER)
check_true("  表头写了检出总数", "检出 2 只" in _row)
check_true("  表头写了框内/框外", "攻击框内 1 / 框外 1" in _row)
check_true("  表头写了判据名", "判据 center" in _row)
check_true("  表头写了门槛与攻击框坐标",
           "（门槛 1500px²）" in _row and "攻击框 x[130,470] y[230,370]" in _row)
check_true("  表头写了半宽半高（±170×±70）", "（±170×±70）" in _row)
check_true("  表头写了角色位置", "角色(300,300)" in _row)
check_true("  框内的排在前、标 ★", "★1(300,300) 40×40 交叠1600 中心∈框" in _row)
check_true("  框外的标 ·", "·2(476,317) 40×40 交叠560 中心外 6px×0px→" in _row)

_row2 = monster_detect_snapshot([], [mob(100, 100), mob(120, 120), mob(140, 140),
                                     mob(160, 160), mob(180, 180), mob(200, 200)],
                                box=BOX, mode="min_area", need=868, detail_n=4)
check_true("  detail_n=4 ⇒ 只列 4 只", _row2.count("·") == 4)
check_true("  列不下的要说明还剩几只", "（还有 2 只没列）" in _row2)

_row3 = monster_detect_snapshot([], [], box=None, mode="center", need=NEED)
check_true("  没框 ⇒ 不写坐标段", "攻击框 x[" not in _row3 and "检出 0 只" in _row3)
check_true("  没角色位置 ⇒ 不写角色段", "角色" not in _row3)
check_true("  一只都没有 ⇒ 不写明细段", "明细" not in _row3)
_row4 = monster_detect_snapshot(None, None, player=(1, 2))
check_true("  None 列表也不炸", "检出 0 只" in _row4 and "角色(1,2)" in _row4)
check_true("  怪缺字段 ⇒ 该只退化成 (?) 而整行仍在",
           "(?)" in monster_detect_snapshot([{}], []))


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[3] 引擎行为：打不打 / 节流两档 / 关得掉 / 异常只吼一次")
print("=" * 72)
_logh = LogCapture()
logging.getLogger().addHandler(_logh)
try:
    with Clock(2000.0) as _ck:
        _s = make_stub(base_cfg(), monsters=[mob(476, 317)])
        frame(_s)
        _l1 = _logh.search(DET)
        check("  有怪 ⇒ 打一行", len(_l1), 1)
        check_true("  行里有攻击框坐标与判据", "攻击框 x[" in _l1[0]
                   and "判据 center" in _l1[0])
        check_true("  行里有那只怪的数字", "交叠560" in _l1[0] and "中心外 6px" in _l1[0])
        frame(_s)
        check("  同一批怪、0.1 秒内 ⇒ 不重复", len(_logh.search(DET)), 1)
        _ck.tick(1.2)
        frame(_s)
        check("  同一批怪、1.2 秒 ⇒ 还没到 5 秒，仍不重复",
              len(_logh.search(DET)), 1)
        _ck.tick(4.0)                      # 累计 5.2 秒
        frame(_s)
        check("  累计 5.2 秒 ⇒ 心跳行放开", len(_logh.search(DET)), 2)
        # 指纹变了 ⇒ 立刻（只要距上次 >= 1s）
        _ck.tick(1.1)
        _s.monsters = [mob(300, 300)]
        frame(_s)
        _l3 = _logh.search(DET)
        check("  框内那批怪变了、且距上次 >= 1s ⇒ 立刻打", len(_l3), 3)
        check_true("  新行就是新那一帧（框内 1 只）", "攻击框内 1 / 框外 0" in _l3[-1])

    _logh.msgs.clear()
    with Clock(3000.0):
        _s2 = make_stub(base_cfg(detect_log=False), monsters=[mob(476, 317)])
        frame(_s2)
        check("  detect_log: false ⇒ 一行都不打", len(_logh.search(DET)), 0)

    _logh.msgs.clear()
    with Clock(4000.0) as _ck2:
        _s3 = make_stub(base_cfg(), monsters=[mob(476, 317)])
        # 让"明细行本体"内部炸掉（临时替换整行生成函数），但**不许**影响索敌链 ——
        # 必须证明"观测行失败 = 只吼一次，挂机照走"。
        _orig_snap = _mod.monster_detect_snapshot
        _mod.monster_detect_snapshot = lambda *a, **k: 1 / 0
        try:
            frame(_s3)
        finally:
            _mod.monster_detect_snapshot = _orig_snap
        frame(_s3)
        _w = [m for m in _logh.msgs if "明细行生成失败" in m]
        check("  明细行生成失败 ⇒ 吼一行 WARNING（失败不静默）", len(_w), 1)
        check("  置了 _det_log_failed 标记", _s3._det_log_failed, True)
        _ck2.tick(20.0)
        _s3._det_log_sig = None            # 就算节流放开也不许再吼
        frame(_s3)
        check("  再失败 ⇒ 只吼一次（不刷屏）",
              len([m for m in _logh.msgs if "明细行生成失败" in m]), 1)
        check("  失败也不影响出招判定（这帧仍不炸）", isinstance(frame(_s3), tuple), True)
finally:
    logging.getLogger().removeHandler(_logh)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[4] [出招·延迟]：进框 → 出刀 的毫秒数（只算框内真有怪的那一刀）")
print("=" * 72)
logging.getLogger().addHandler(_logh)      # [3] 的 finally 摘掉了，这里再挂上
_logh.msgs.clear()
with Clock(5000.0) as _ck3:
    _s4 = make_stub(base_cfg(), monsters=[mob(300, 300)])
    _s4._eng_first_t = 5000.0 - 0.42          # 420ms 前第一只怪进框
    _s4._note_attack(1)
    _d1 = _logh.search(DLY)
    check("  真出刀 ⇒ 打一行延迟", len(_d1), 1)
    check_true("  毫秒数正确", "用了 420ms" in _d1[0])
    check_true("  行里带检测间隔与设定冷却",
               "检测每 1 帧一次" in _d1[0] and "设定冷却 0.05s" in _d1[0])
    check("  算过就清基准（不重复报）", _s4._eng_first_t, None)
    _s4._note_attack(1)
    check("  基准已清 ⇒ 第二刀不再报", len(_logh.search(DLY)), 1)
    # 锁定补招那一刀（engage_n=0）不许算进灵敏度
    _s4._eng_first_t = 4999.0
    _s4._note_attack(0)
    check("  engage_n=0（锁定补招）⇒ 不报延迟", len(_logh.search(DLY)), 1)
    check("  ……也不许把基准清掉（它还得留给真正那一刀）", _s4._eng_first_t, 4999.0)
    _s4._note_attack(2)
    check_true("  接下来那一刀照样报得出", len(_logh.search(DLY)) == 2)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[5] 接线白盒：调用点位置 / 判据名只有一处读 / 纯观测")
print("=" * 72)
check("_SRC 里有明细行调用点", _SRC.count("self._log_monster_detect(_engage, _far)"), 1)
_i_eng = _SRC.index("_engage = self._mobs_in_attack_box()")
_i_log = _SRC.index("self._log_monster_detect(_engage, _far)")
_i_md = _SRC.index("_md_stop = self.cfg.get(\"monster_detect\") or {}")
check_true("  调用点在 _engage/_far 之后、_md_stop 之前",
           _i_eng < _i_log < _i_md)
check("  判据名全文件只 get 一次（不给第二套判据留门）",
      _SRC.count('get("mob_engage_mode"'), 1)
check_true("  进框时刻：非空且未记过 ⇒ 记时间",
           "if _engage:" in _SRC and "self._eng_first_t = time.time()" in _SRC)
check_true("  框内空了 ⇒ 作废（不许跨交战算延迟）",
           "elif getattr(self, \"_eng_first_t\", None) is not None:" in _SRC
           and "self._eng_first_t = None" in _SRC)
check_true("  延迟行在 _note_attack 里、且在 engage 计数之后",
           DLY[1:-1] in _NOTE and _NOTE.index("_atk_eng_min") < _NOTE.index("进攻击框"))
check_true("  延迟只对 engage_n>0 的那一刀算", 'if _e > 0:' in _NOTE)
check_true("  纯观测：明细行不碰指令", "self.cmd_" not in _FUN)
check_true("  纯观测：明细行不写位置存盘", "_save_nametag_pos" not in _FUN)
check_true("  明细行整个函数体被 try 包住", "except Exception" in _FUN)
check_true("  开关读的是 detect_log", '"detect_log"' in _FUN)


# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 72)
print("[6] 真机现场回归：mob(476,317)「看着在框里却够不着」那一帧")
print("=" * 72)
_logh.msgs.clear()
with Clock(6000.0):
    _s5 = make_stub(base_cfg(), monsters=[mob(476, 317)])
    _mv = frame(_s5)
    check("  这一帧不出招（够不着）", _mv[2] != "attack", True)
    _hit = _logh.search(DET)
    check("  明细行有一行", len(_hit), 1)
    check_true("  行里写了「框内 0 / 框外 1」", "攻击框内 0 / 框外 1" in _hit[0])
    check_true("  行里写了它离框多远（6px 水平）", "中心外 6px×0px→" in _hit[0])
    check_true("  行里写了交叠面积（560 px²，压边但中心在外）", "交叠560" in _hit[0])

with Clock(7000.0):
    # `center` vs `min_area` 在**日志**上必须看得出差别（这是本次拍的板）
    _logh.msgs.clear()
    _s6 = make_stub(base_cfg(mob_engage_mode="center"), monsters=[mob(453, 300)])
    frame(_s6)
    _c1 = _logh.search(DET)
    check_true("  center：交叠 1480/1500 差 20px²，但中心在框内 ⇒ 算够得着",
               "攻击框内 1 / 框外 0" in _c1[0])
    _logh.msgs.clear()
    _s7 = make_stub(base_cfg(mob_engage_mode="min_area"), monsters=[mob(453, 300)])
    frame(_s7)
    _c2 = _logh.search(DET)
    check_true("  min_area：同一帧 ⇒ 框内 0（老口径的「差 1px 就不算」）",
               "攻击框内 0 / 框外 1" in _c2[0])
    check_true("  两口径的日志行必须不同（否则日志在骗人）", _c1[0] != _c2[0])


print()
print("=" * 72)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
