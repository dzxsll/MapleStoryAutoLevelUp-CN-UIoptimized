# -*- coding: utf-8 -*-
'''「没出招·归因」日志自检（2026-10-14 加）。

背景（用户真机反馈 m05740）：
    「现在有看到有检测框，也在攻击范围内，但是没有立马攻击」
    真机日志 `log/MSBot_2026-10-14_12-05-06.log`（12:05:06~12:09:37、37 条状态行）
    的归因（只读脚本 `_probe_attack_gap.py`）：
      · 20 条 `怪>=1` 的状态行里，**8 条**的近 3 秒有 `[靠近]`（怪在**攻击框外**、
        机器人正走过去），6 条是**绳/梯让路**，只有 6 条真带了 `attack`；
      · 12:05~12:09 **全程没有** `[停下打怪]` —— 因为那段时间一只怪都没进过攻击框；
      · 12:08:54 有 `[前台检查] 游戏窗口不在前台，暂停发送按键`（框照画、键不发）。

    根子：**画出来的框（索敌框）比出招判据（攻击框）大一圈** ——
      aoe：攻击框 = `range_x/y ÷ 2` = ±170×±70（用户 `range_x/y: 340/140`）；
      索敌框 =（攻击框 + `search_box_margin` 50）× `detect_range_scale` ⇒ ±330×±132
      （用户 1.5）；位置连续丢失 ≥ `stale_pos_full_frame_after`（该键 2026-10-14 起
      默认 **0 = 关掉整帧放大**）帧时还会放大到
      **整帧**。日志里 `[靠近] …最近的在left侧 176px` 给的数是**角色到怪中心的曼哈顿
      距离**（`_pick_approach_target` 里 `abs(_cx-_px)+abs(_cy-_py)`），不是"离框边还差
      多少" —— 所以判"够不着"的唯一权威是**交叠面积**（`_mobs_in_attack_box`）。

做法：文案收在 `src/utils/common.py: mob_no_attack_reason()` 一处（纯函数），
      引擎只负责"凑事实 + 打一行"（`MapleStoryAutoBot._mob_no_attack_log`，纯观测、
      5 秒节流、只在画面里真有怪时说话）。

本文件钉五件事：
  【1】纯函数真值表：7 类归因各自的 code 与正文关键片段；未知 kind 归一成 unknown
  【2】前台**三态**：True / False / **None（未知）**文案必须各不相同 ——
       未知不许被写成"正常"（`03 §6` 教训 3：区分"没发生"和"没观测"）
  【3】触发条件与节流（时钟可控）：没怪不打；5 秒内不重复；6 秒后放开；
       `kind='front'` 在前台正常时**不消费节流**（否则真原因会被它挤掉）
  【4】接线白盒：几个 `return` 现场都留了归因调用；`[靠近]` 行里带索敌框尺寸
  【5】真机现场回归：喂一帧"怪在攻击框外 176px"的现场 ⇒ 不出招 + 有归因行
'''
import inspect
import logging
import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.utils.common import mob_no_attack_reason              # noqa: E402
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
NOATK = "[没出招·归因]"


def base_cfg(**md_over):
    md = {
        "every_n_frames": 1,
        "search_box_margin": 50,
        "max_mob_area_trigger": 1500,
        "stop_to_attack": True,
        "stop_attack_clear_frames": 2,
        "stop_attack_clear_s": 0.0,
        "stop_attack_timeout_s": 20,
        "target_hold_s": 0.0,          # 0 = 锁定关（只测归因，不测锁定）
        "approach_mob": True,
        "approach_dy_tol": 60,
        "approach_confirm_s": 0.5,
        "approach_dir_min_s": 0.4,
        "detect_range_scale": 1.5,     # 用户侧取值（索敌框 ±330×±132）
        "detect_range_scale_y": 1.1,

        # ★ 2026-10-14：宽判定余量（`mob_engage_margin`）在本文件里**关掉** ——

        #   本文件钉的是别的判据（归因文案 / 明细行），余量会把所有几何期望挪 40px。

        #   余量自己的行为在 tools/verify_engage_margin.py 里钉。

        "mob_engage_margin": 0,
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
    '''能走通索敌链 + 归因日志的假引擎（**不跑 __init__**）。'''
    class S:
        pass

    s = S()
    for _n in ("get_attack_range", "get_nearest_monster", "get_attack_direction",
               "get_face_direction", "update_cmd_by_mob_detection",
               "get_detect_extent", "get_search_box",
               "_min_mob_area", "_mobs_in_attack_box", "_pick_approach_target",
               # 2026-10-14 加（本文件的对象）
               "_attack_box_half", "_game_window_front", "_mob_no_attack_log",
               # 2026-10-14 加：[出招] 计数（4 处出招都调它，纯观测）
               "_note_attack", "_log_attack_heartbeat",
               # 2026-10-14 加：「离够得着最近的一只差多少」（归因行里的数字）
               "_engage_gap", "_engage_gap_facts",
               # 2026-10-14 加：[怪检测] 明细行（纯观测；它要读判据名与攻击框）
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


# ══════════════════════════════════════════════════════════════════════════
print("=" * 72)
print("[1] 纯函数真值表：每类归因的 code + 正文关键片段")
print("=" * 72)
_KW = dict(engage_n=1, far_n=3, half_x=170, half_y=70, approach=True,
           stop_to_attack=False, front=None, cooldown_left=0.04)

_c, _t = mob_no_attack_reason("front", **_KW)
check("  front.code", _c, "front")
check_true("  front 说清了「键一个都不发」", "一个键都不发" in _t)
check_true("  front 提醒界面自己在前台也算不在前台", "界面自己在前台" in _t)

_c, _t = mob_no_attack_reason("no_engage", **_KW)
check("  no_engage.code", _c, "no_engage")
check_true("  no_engage 说「攻击框里一个都没有」", "攻击框里一个都没有" in _t)
check_true("  no_engage 点明画出来的是索敌框", "索敌框" in _t)
check_true("  no_engage：approach 开 ⇒ 说'正在走过去打'", "正在走过去打" in _t)
_, _t2 = mob_no_attack_reason("no_engage", **dict(_KW, approach=False))
check_true("  no_engage：approach 关 ⇒ 说'一律不管'", "一律不管" in _t2)

_c, _t = mob_no_attack_reason("approach_pending", **_KW)
check("  approach_pending.code", _c, "approach_pending")
check_true("  说清「本帧没追过去」", "本帧没追过去" in _t)
check_true("  列出两个可能的缘由（确认/跨层）",
           "连续两次" in _t and "approach_dy_tol" in _t)

_c, _t = mob_no_attack_reason("approach_off", **_KW)
check("  approach_off.code", _c, "approach_off")
check_true("  说「这不是故障，是配置」", "不是故障" in _t)

_c, _t = mob_no_attack_reason("cooldown", **_KW)
check("  cooldown.code", _c, "cooldown")
check_true("  冷却剩余秒数进文案", "0.04s" in _t)
_, _t0 = mob_no_attack_reason("cooldown", **dict(_KW, engage_n=0))
check_true("  engage_n=0 时改口径（锁定补招）", "锁定中的目标暂时不在攻击框里" in _t0)
check_true("  engage_n=0 时不再说「攻击框里有 1 个」", "攻击框里有 1 个" not in _t0)

_c, _t = mob_no_attack_reason("direction", **_KW)
check("  direction.code", _c, "direction")
check_true("  说清「判不出攻击方向」", "判不出攻击方向" in _t)

_c, _t = mob_no_attack_reason("home_route", **_KW)
check("  home_route.code", _c, "home_route")
check_true("  说清「回正期间不碰走位与动作」",
           "回正期间怪逻辑" in _t and "完全不碰" in _t)

_c, _t = mob_no_attack_reason("我这行是手滑写错的", **_KW)
check("  未知 kind 归一成 unknown（不抛异常）", _c, "unknown")
check_true("  未知 kind 求人转发日志", "原因未知" in _t and "开发者" in _t)
_c, _t = mob_no_attack_reason(None)
check("  kind=None 也不炸（归一成 unknown）", _c, "unknown")

# ── 事实尾巴：两个框的尺寸 / 两个开关 / 前台态 ─────────────────────────────
_c, _t = mob_no_attack_reason("no_engage", **_KW)
check_true("  尾巴带攻击框尺寸", "攻击框 ±170×±70px" in _t)
check_true("  尾巴带画面目标数（engage+far）", "画面 4 个目标" in _t)
check_true("  尾巴带两个开关的**当前**取值",
           "stop_to_attack=关 / approach_mob=开" in _t)
_c, _t = mob_no_attack_reason("no_engage", **dict(_KW, engage_n="x", far_n=None))
check_true("  非法入参不炸（按 0 处理）", "画面 0 个目标" in _t)

# ── ★ 2026-10-14 加：「离够得着最近的一只差多少」的数字（用户 m06668 的取证口）──
#    用户报「红色攻击框里有绿色的检测框，但是就是不攻击」——**没有数字就只能猜**：
#    是判据太严（差一点点）？还是怪在索敌框里、本来就不该打（差得远）？还是
#    「中心明明在框里却仍不算」？这三种处境必须能从日志里分开，所以有了这段子句。
_c, _t = mob_no_attack_reason("no_engage", **dict(_KW, gap_area=620, gap_need=868,
                                                 gap_dx=0, gap_dy=6, center_in=0))
check_true("  ★ gap：写出「交叠 A/B px²」和还差多少",
           "交叠 620/868 px²" in _t and "还差 248" in _t)
check_true("  gap：写出中心离框多远 + 中心在框内的个数",
           "中心离攻击框 0px 水平 / 6px 垂直" in _t
           and "中心已在攻击框内的有 0 个" in _t)
_c, _t = mob_no_attack_reason("no_engage", **dict(_KW, gap_area=420, gap_need=868,
                                                 gap_dx=0, gap_dy=0, center_in=1))
check_true("  gap：中心已在框内 ⇒ 中间那句改写成「中心已在攻击框内」",
           "，中心已在攻击框内；" in _t and "中心已在攻击框内的有 1 个" in _t)
_c, _t = mob_no_attack_reason("no_engage", **_KW)
check_true("  gap：**不给数字 ⇒ 一个字都不加**（老文案原样，旧日志/旧判据仍对得上）",
           "交叠" not in _t and "还差" not in _t)
_c, _t = mob_no_attack_reason("no_engage", **dict(_KW, gap_area="脏", gap_need=None))
check_true("  gap：缺 gap_need ⇒ 不加子句（脏 gap_area 也带不进来）", "交叠" not in _t)
_c, _t = mob_no_attack_reason("no_engage", **dict(_KW, gap_area="脏", gap_need="八",
                                                 gap_dx=None, gap_dy="z", center_in="w"))
check_true("  gap：脏值一律按 0 处理（交叠 0/0 px²、不抛异常）",
           "交叠 0/0 px²" in _t and "中心已在攻击框内；" in _t)

print()
print("=" * 72)
print("[2] 前台三态：True / False / None（未知）必须各说各话")
print("=" * 72)
_, _tf = mob_no_attack_reason("no_engage", **dict(_KW, front=False))
_, _tt = mob_no_attack_reason("no_engage", **dict(_KW, front=True))
_, _tn = mob_no_attack_reason("no_engage", **dict(_KW, front=None))
check_true("  front=False ⇒ 文案写「不在前台」", "前台=**不在前台**" in _tf)
check_true("  front=True  ⇒ 文案写「在前台」", "前台=在前台" in _tt)
check_true("  front=None  ⇒ 文案写「未知」（不许写成正常）", "前台=**未知**" in _tn)
check_true("  三态互不相同", len({_tf, _tt, _tn}) == 3)

print()
print("=" * 72)
print("[3] 触发条件与节流（时钟可控，直接驱动 _mob_no_attack_log）")
print("=" * 72)
_logh = LogCapture()
logging.getLogger().addHandler(_logh)
try:
    _cfg = base_cfg()
    # ① 画面里没有怪 ⇒ 一条都不打（不出招是正常的，不刷屏）
    _logh.msgs.clear()
    with Clock(1000.0):
        _s = make_stub(_cfg, monsters=[])
        _s._mob_no_attack_log("no_engage")
        check("  画面没怪 ⇒ 不打（正常的沉默）", len(_logh.search(NOATK)), 0)

    # ② 有怪 ⇒ 打一行，并且 facts 由引擎补齐（不用调用方手抄）
    _logh.msgs.clear()
    with Clock(1000.0):
        _s = make_stub(_cfg, monsters=[mob(700, 300)])
        _s._mob_no_attack_log("no_engage")
        _hit = _logh.search(NOATK)
        check("  有怪 ⇒ 打一行", len(_hit), 1)
        check_true("  行里带引擎补的攻击框尺寸（±170×±70）",
                   "攻击框 ±170×±70px" in _hit[0])
        check_true("  行里带 far_n（1 个目标在框外）", "1 个目标全在**攻击框外**" in _hit[0])

    # ③ 5 秒节流：同一个 5s 窗口里只留一行
    _logh.msgs.clear()
    with Clock(1000.0) as _ck:
        _s = make_stub(_cfg, monsters=[mob(700, 300)])
        _s._mob_no_attack_log("no_engage")
        _ck.tick(1.0)
        _s._mob_no_attack_log("no_engage")
        _ck.tick(3.0)
        _s._mob_no_attack_log("no_engage")
        check("  5 秒内重复调用 ⇒ 只留一行", len(_logh.search(NOATK)), 1)
        _ck.tick(6.0)                       # 累计 10s > 5s
        _s._mob_no_attack_log("no_engage")
        check("  6 秒后再来 ⇒ 放开（第二行）", len(_logh.search(NOATK)), 2)
        check_true("  节流时间戳被更新", float(_s.t_mob_noatk_log) > 1000.0)

    # ④ kind='front' 只在前台**真的不在**时才说；前台正常 ⇒ 不消费节流
    class _Kb:
        def __init__(self, act):
            self._act = act

        def is_game_window_active(self):
            return self._act

    _logh.msgs.clear()
    with Clock(1000.0):
        _s = make_stub(_cfg, monsters=[mob(700, 300)])
        _s.kb = _Kb(True)
        _s._mob_no_attack_log("front")
        check("  前台正常 ⇒ front 不打", len(_logh.search(NOATK)), 0)
        check("  前台正常 ⇒ **不消费**节流（时间戳仍为 0）", float(_s.t_mob_noatk_log), 0.0)
        _s.kb = _Kb(False)
        _s._mob_no_attack_log("front")
        _hit = _logh.search(NOATK)
        check("  前台不在 ⇒ front 打一行", len(_hit), 1)
        check_true("  文案说清「键一个都不发」", "一个键都不发" in _hit[0])
        _s.kb = _Kb(False)
        _s._mob_no_attack_log("no_engage")
        check("  front 那行消费了节流 ⇒ 紧接着的 no_engage 被挡",
              len(_logh.search(NOATK)), 1)

    # ⑤ kb 不存在 / kb 抛异常 ⇒ front 未知 ⇒ 不冒充"正常"，也不打 front
    _logh.msgs.clear()
    with Clock(1000.0):
        _s = make_stub(_cfg, monsters=[mob(700, 300)])
        check("  kb 未创建 ⇒ _game_window_front() = None（未知）",
              _s._game_window_front(), None)
        _s._mob_no_attack_log("front")
        check("  前台未知 ⇒ front 不打（不冒充正常）", len(_logh.search(NOATK)), 0)

        class _Boom:
            def is_game_window_active(self):
                raise RuntimeError("win32 炸了")

        _s.kb = _Boom()
        check("  查询抛异常 ⇒ 仍是 None（fail-closed 不冒充正常）",
              _s._game_window_front(), None)

    # ⑥ 文案生成炸了也不许静默、不许带崩主循环
    _logh.msgs.clear()
    with Clock(1000.0):
        _s = make_stub(_cfg, monsters=[mob(700, 300)])
        _s.cfg = None                      # 让 _attack_box_half 走异常分支
        try:
            _s._mob_no_attack_log("no_engage")
            _ok = True
        except Exception:                                     # noqa: BLE001
            _ok = False
        check_true("  cfg 残缺也不抛异常（纯观测代码的底线）", _ok)
finally:
    logging.getLogger().removeHandler(_logh)

print()
print("=" * 72)
print("[4] 接线白盒：relax 的那几个 return 现场都留了归因调用")
print("=" * 72)
_src = inspect.getsource(MapleStoryAutoBot.update_cmd_by_mob_detection)
_code = "\n".join(ln for ln in _src.splitlines() if not ln.strip().startswith("#"))
check("  调用点数（front/home_route/approach_pending/approach_off/cooldown×2/direction）",
      _code.count("_mob_no_attack_log("), 7)
for _needle, _why in (('"front"', "窗口不在前台"),
                      ('"home_route"', "回正线"),
                      ('"approach_pending"', "看见了但本帧没追"),
                      ('"approach_off"', "approach_mob 关着"),
                      ('"cooldown"', "冷却没到（两处：AOE / 锁定补招）"),
                      ('"direction"', "directional 判不出方向")):
    check_true(f"  留了 {_why} 的归因调用（{_needle}）", _needle in _code)
check_true("  ★ [靠近] 行里给出**索敌框**尺寸（否则用户只能看到框、看不到差多少）",
           "画出来的框是索敌框" in _code)
check_true("  [靠近] 行里同时给出攻击框尺寸", "攻击框 ±{_ax}×±{_ay}px" in _code)
check_true("  绳/梯让路那行也说清了「框照旧画」", "框照旧画" in _code)

_hlp = inspect.getsource(MapleStoryAutoBot._mob_no_attack_log)
check_true("  助手：没怪直接返回", "if not getattr(self, \"monsters\", None):" in _hlp)
check_true("  助手：5 秒节流", "< 5.0" in _hlp)
check_true("  助手：前台判据在节流**之后**（不每帧打 win32 API）",
           _hlp.index("t_mob_noatk_log") < _hlp.index("_game_window_front()"))
check_true("  助手：front 正常时不消费节流",
           "kind == \"front\" and facts.get(\"front\") is not False" in _hlp)
check_true("  助手：纯观测（不改任何 cmd_*）",
           "self.cmd_action =" not in _hlp and "self.cmd_move" not in _hlp)
check_true("  助手：文案异常也不静默", "归因文案生成失败" in _hlp)

_half = inspect.getsource(MapleStoryAutoBot._attack_box_half)
check_true("  _attack_box_half 与 get_attack_range 同口径（aoe 折半）",
           "range_x\", 0)) // 2" in _half)
_imp = inspect.getsource(_mod)
check_true("  common.py 的纯函数已 import", "mob_no_attack_reason," in _imp)
check_true("  [没出招·归因] 前缀只在助手这一处拼", _imp.count('"[没出招·归因] '), 1)

print()
print("=" * 72)
print("[5] 真机现场回归：12:05 日志里「怪在攻击框外、框却画着」的那一帧")
print("=" * 72)
# 玩家 (300,300)、aoe 340×140 ⇒ 攻击框 x130~470 / y230~370（±170×±70）
# 实心 40×40 的怪放 (476,317) ⇒ 曼哈顿 176px（**与日志同量级**）、整只在攻击框外
_logh2 = LogCapture()
logging.getLogger().addHandler(_logh2)
try:
    with Clock(1000.0) as _ck2:
        _c = base_cfg()
        _s = make_stub(_c, monsters=[mob(476, 317)])
        check("  这一帧的攻击框内目标数 = 0（够不着）",
              len(_s._mobs_in_attack_box()), 0)
        _mv = frame(_s)
        check("  ★ 不出招（cmd_action 不是 attack）", _mv[2] != "attack", True)
        _hit = _logh2.search(NOATK)
        check("  ★ 留了归因行（这一帧为什么不打，一行说清）", len(_hit), 1)
        check_true("  归因行说清是「攻击框外」的怪 + 为什么本帧没追过去",
                   ("全在**攻击框外**" in _hit[0] or "攻击框外有 1 个目标" in _hit[0])
                   and "本帧没追过去" in _hit[0])
        check_true("  归因行给出两个框的对比（攻击框 ±170×±70）",
                   "攻击框 ±170×±70px" in _hit[0])
        # ★ 2026-10-14 加：这一帧还必须带上「差多少才够得着」的数字 ——
        #    怪 40×40 放中心 (476,317) ⇒ 怪框 x456~496，与攻击框（右边界 470）
        #    交叠 14×40 = 560 px²，门槛 = _min_mob_area() = min(1600, 1500) = 1500
        #    ⇒ 中心离框 6px、中心在框内的有 0 个。
        _g1 = _s._engage_gap()
        check("  ★ 数字与几何一致（交叠 560 / 门槛 1500 / 中心离框 6px / 0 个在框内）",
              (_g1[0], _g1[1], _g1[2], _g1[4]), (560, 1500, 6, 0))
        check_true("  ★ 归因行真的带上了这些数字（不是只算不打）",
                   "交叠 560/1500 px²" in _hit[0] and "6px 水平" in _hit[0]
                   and "中心已在攻击框内的有 0 个" in _hit[0])
        _ap = _logh2.search("[靠近]")
        check("  第一帧还没凑齐「连续两次确认」⇒ 还没开始走", len(_ap), 0)

        # 第二帧（0.2s 后，仍在确认窗口内）⇒ 确认通过 ⇒ 变成"走过去打"
        _logh2.msgs.clear()
        _ck2.tick(0.2)
        _mv2 = frame(_s)
        check("  确认通过后：朝怪的方向走（right）", _mv2[0], "right")
        check("  仍然不出招（还没进攻击框）", _mv2[2] != "attack", True)
        _ap2 = _logh2.search("[靠近]")
        check("  ★ 这时打的是 [靠近]（走过去打），归因行让位", len(_ap2), 1)
        check_true("  [靠近] 行里带**两个框的尺寸**（用户能自己看出差多少）",
                   "索敌框 约 ±" in _ap2[0] and "攻击框 ±170×±70px" in _ap2[0])
        _no2 = _logh2.search(NOATK)
        check("  同一个 5s 窗口内不再重复打归因行（节流）", len(_no2), 0)

    # 对照：曼哈顿 176px 但**交叠够了**（怪有大半进了攻击框）⇒ 必须出招
    _logh2.msgs.clear()
    with Clock(2000.0):
        _c2 = base_cfg()
        _s2 = make_stub(_c2, monsters=[mob(420, 300)])
        check_true("  对照组：攻击框内有 1 个目标",
                   len(_s2._mobs_in_attack_box()) == 1)
        _mv3 = frame(_s2)
        check("  ★ 对照组必须出招（判据是**交叠面积**，不是距离）", _mv3[2], "attack")
        check("  出招的帧不打归因行", len(_logh2.search(NOATK)), 0)

    # 对照：approach_mob 关着（用户 12:09 之后的状态）⇒ 框外的一律不管
    _logh2.msgs.clear()
    with Clock(3000.0):
        _c3 = base_cfg(approach_mob=False)
        _s3 = make_stub(_c3, monsters=[mob(476, 317)])
        _mv4 = frame(_s3)
        check("  approach 关：框外的怪不追（方向不动）", _mv4[0], "none")
        check("  approach 关：也不出招", _mv4[2] != "attack", True)
        _hit3 = _logh2.search(NOATK)
        check("  ★ 留一行说清「这是配置，不是故障」", len(_hit3), 1)
        check_true("  文案点出 approach_mob 关着", "approach_mob 关着" in _hit3[0])

    # ★ 2026-10-14 加（用户 m06668 的正主）：**中心已经在攻击框里，却仍不算够得着** ——
    #    40×40 的怪放中心 (453,300) ⇒ 怪框 x433~473，攻击框右边界 470 ⇒ 交叠
    #    37×40 = 1480 px² < 1500（差 20px²）⇒ 老口径 min_area 下**不算**够得着，
    #    可它中心 (453,300) 明明落在攻击框里。这一档正是用户看到的
    #    「红色攻击框里有绿色的检测框，但是就是不攻击」。
    _logh2.msgs.clear()
    with Clock(4000.0):
        _c4 = base_cfg()
        _s4 = make_stub(_c4, monsters=[mob(453, 300)])
        _g4 = _s4._engage_gap()
        check("  ★ 中心在框内、只差 20px² ⇒ 老口径仍不算够得着（绿框在红框里却不打）",
              (_g4[0], _g4[1], _g4[4]), (1480, 1500, 1))
        check("  这只确实没被算作够得着", len(_s4._mobs_in_attack_box()), 0)
        _mv5 = frame(_s4)
        check("  于是这一帧不出招", _mv5[2] != "attack", True)
        _hit4 = _logh2.search(NOATK)
        check("  留了归因行（这一帧为什么不打）", len(_hit4), 1)
        check_true("  ★ 行里点明「中心已在攻击框内的有 1 个」（判据与直觉不一致的铁证）",
                   "交叠 1480/1500 px²" in _hit4[0]
                   and "中心已在攻击框内的有 1 个" in _hit4[0])
finally:
    logging.getLogger().removeHandler(_logh2)

print()
print("=" * 72)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
