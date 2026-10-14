# -*- coding: utf-8 -*-
'''
自检：怪的「看得见」与「打得到」分开（感知框放大 + 够不着就走过去打）

为什么有这个自检（2026-10-13，用户原话「怪物检测率还是不高」）：
    索敌框原来**就是**攻击框（±(range/2+margin) = 500×270，只占 1366×768 客户区的
    13%）。用户真机 12 帧实测：整帧 53 只怪里 **46 只（87%）落在这个框外** ——
    现象是"身边明摆着有怪，机器人不认"。
    用户拍板方案 A：**看得远 + 够不着就走过去打**；并要求名字定位的
    "画面外假命中"一起治（12 帧里 4 帧算出 loc=(21,-30)）。

钉住的判据（每条都对应一个真机现象）：
    [1] 配置实体（config/config_default.yaml 里真有这四个键）
    [2] get_detect_extent：感知框 = 攻击框口径 + margin，再乘 detect_range_scale；
        竖直方向另有 detect_range_scale_y（缺键 = 与横向同倍数）；
        缺键 → 1.0（老行为）；<1 夹回 1.0（"看得更少"只会白白漏怪）
    [3] get_search_box：clamp 到帧内；位置连续丢失 → 临时放大到整帧；配 0 = 关
    [4] **攻击判定只认攻击框**：框外（看得见、够不着）的怪不出招，而是朝它走
    [5] 变异测试：approach_mob=false → 退回老行为（只打攻击框内的）
    [6] 只走**同一层**；梯子上不走（跨层/梯上硬走会掉下去）
    [7] "打完了"只看**攻击框内**（远处有怪在场也要能判完 —— 否则永远走不掉）
    [8] 朝向只按攻击框内的怪定（否则会朝着一只够不着的远怪转身，打偏近处这只）
    [9] 名字定位护栏 _nametag_pos_plausible（负数/越界 → 假命中；取不到帧尺寸只做
        负数那条 = "无法判定"，不是放行）
    [10] 画框与检测**同源**（get_search_box 唯一出处）+ UI/中文名登记
    [11] **绳/梯让路**：路线在爬（is_on_ladder 或路线指令 y=up/down）时，怪逻辑
         一律不动走位/动作 —— 治用户实测的「会卡在绳子上」（横向移动会把引擎的
         is_on_ladder 判成 False ⇒ 不再补 up ⇒ 吊在绳上反复起跳）
'''
import os
import sys
import logging
import inspect

import numpy as np

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


import src.engine.MapleStoryAutoLevelUp as _mod            # noqa: E402
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402

PLAYER = (700, 400)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        try:
            self.msgs.append(record.getMessage())
        except Exception:                                      # noqa: BLE001
            pass

    def search(self, needle):
        return [m for m in self.msgs if needle in m]


def mob(cx, cy, size=(40, 40), score=0.1):
    '''按**中心点**造一只怪（引擎的 position 是左上角）。'''
    w, h = size
    return {"name": "m", "position": (cx - w // 2, cy - h // 2),
            "size": (w, h), "score": score}


def base_cfg(**md_over):
    md = {
        "mode": "contour_only", "contour_blur": 5, "diff_thres": 0.4,
        "search_box_margin": 50, "max_mob_area_trigger": 1500,
        "with_enemy_hp_bar": False, "match_all_mobs": False,
        "verify_color": True, "verify_color_tol": 30, "verify_min_ratio": 0.75,
        "every_n_frames": 1,
        "stop_to_attack": True, "stop_attack_clear_frames": 2,
        "stop_attack_timeout_s": 20,
        # ★ 2026-10-14：宽判定余量（`mob_engage_margin`）在本文件里**关掉** ——
        #   本文件钉的是"看得见/打得到"的框几何与名字护栏，余量会把所有几何
        #   期望整体挪 40px。余量自己的行为在 tools/verify_engage_margin.py 里钉。
        "mob_engage_margin": 0,
        # ⚠️ 2026-10-14 P1 加的三个防抖键，本文件**一律关掉**（=0/0/0）：
        #    · stop_attack_clear_s —— [7] 测的是"空 2 个检测帧就判完"的**老口径**；
        #    · approach_confirm_s  —— [4] 测的是"看见框外的怪就朝它走"（不要求连续确认）；
        #    · approach_dir_min_s —— 同上（不要求方向保持）。
        #    这三个键**打开后**的行为在 `tools/verify_stop_to_attack.py` 的【12】【13】【14】测。
        "stop_attack_clear_s": 0.0, "approach_confirm_s": 0.0,
        "approach_dir_min_s": 0.0,
        "detect_range_scale": 2.0, "approach_mob": True,
        "approach_dy_tol": 60, "stale_pos_full_frame_after": 10,
    }
    md.update(md_over)
    return {
        "bot": {"attack": "aoe_skill"},
        "monster_detect": md,
        "character": {"width": 100, "height": 150},
        "directional_attack": {"range_x": 350, "range_y": 70,
                              "cooldown": 0.9, "turn_frames": 3},
        "aoe_skill": {"range_x": 400, "range_y": 170, "cooldown": 0.05},
        "nametag": {"offset": [-1, 30], "diff_thres": 0.01},
    }


def make_stub(cfg, monsters=(), **kw):
    '''能走通索敌链的假引擎（**不跑 __init__**，绑真实方法 ⇒ 测的就是跑的那份）。'''
    class S:
        pass

    s = S()
    for _n in ("get_attack_range", "get_nearest_monster", "get_attack_direction",
               "get_face_direction", "update_cmd_by_mob_detection",
               "get_detect_extent", "get_search_box", "_min_mob_area",
               "_mobs_in_attack_box", "_pick_approach_target",
               # 2026-10-14 加：「没出招·归因」纯观测日志
               "_attack_box_half", "_game_window_front", "_mob_no_attack_log",
               # 2026-10-14 加：[出招] 计数（4 处出招都调它，纯观测）
               "_note_attack", "_log_attack_heartbeat",
               # 2026-10-14 加：「离够得着最近的一只差多少」（归因行里的数字）
               "_engage_gap", "_engage_gap_facts",
                     "_engage_mode", "_log_monster_detect",
               # 2026-10-14 加：梯上让路收紧（让路块只问 `_ladder_yield()`）
               # 2026-10-14 加「宽判定余量」（B）：唯一读点 + 唯一拼框点

               "_engage_margin", "_attack_boxes",

               "_ladder_still_s", "_ladder_yield",
               "_rope_stuck_s", "_rope_giveup_now",
               "_rope_ladder_giveup_tick", "_budget_note_lost",
               # 爬梯走完（2026-10-15，用户 m11669）：路线侧每帧无条件问一次
               "_ladder_finish_cfg", "_ladder_finish_tick",
               "_nametag_pos_plausible"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg
    s.loc_player = PLAYER
    s.monsters = list(monsters)
    s.monsters_info = {"m": [(np.zeros((40, 40, 3), np.uint8), None)]}
    s.img_frame = np.zeros((768, 1366, 3), np.uint8)   # 与真机客户区同尺寸
    s.is_on_ladder = False
    s.is_using_home_route = False
    s.t_last_attack = 0.0
    s.cmd_action = "none"
    s.cmd_move_x = "none"
    s.cmd_move_y = "none"
    s.cmd_move_x_last = "none"
    s._turn_frames_left = 0
    s._turn_dir = None
    s._turn_pending_dir = None
    s._attack_dir_last = None
    s._mob_skip = 0
    s.nametag_miss_streak = 0
    s._perf = lambda *a, **k: None
    # ⚠️ 必须**动态读** s.monsters（冻成闭包会"喂了怪却说没怪"）
    s.get_monsters_in_range = lambda *a, **k: s.monsters
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def frame(s, move_x="none", move_y="none"):
    '''模拟 HuntingState.on_frame：route 写走位 → 怪检测 → 记本帧实际方向。'''
    s.cmd_move_x, s.cmd_move_y, s.cmd_action = move_x, move_y, "none"
    s.update_cmd_by_mob_detection()
    s.cmd_move_x_last = s.cmd_move_x
    return (s.cmd_move_x, s.cmd_move_y, s.cmd_action)


def holding(s):
    return bool(getattr(s, "_stop_hold", False))


print("=" * 72)
print(" 自检：怪的「看得见」与「打得到」分开（感知框 + 走过去打 + 名字护栏）")
print("=" * 72)

# ── ① 配置实体 ──────────────────────────────────────────────────────────────
print("\n[1] 配置实体：config/config_default.yaml 的五个键（不是注释里的承诺）")
import yaml  # noqa: E402
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_root, "config", "config_default.yaml"), encoding="utf-8") as _f:
    _md = (yaml.safe_load(_f) or {}).get("monster_detect") or {}
check("detect_range_scale 默认 2.0（看得远一倍）", _md.get("detect_range_scale"), 2.0)
check("approach_mob 默认开", _md.get("approach_mob"), True)
check("approach_dy_tol 默认 60", _md.get("approach_dy_tol"), 60)
check("★ detect_range_scale_y 默认 1.1（竖直收窄：真机检测耗时 141→70ms）",
      _md.get("detect_range_scale_y"), 1.1)
# ★ 2026-10-14：当天先上调到 25 做临时护栏，**当天即撤回 10**（用户按 F2 重标名字模板，
#   复测真位置分 0.0000~0.0016、假阳性档 0.0232~0.0265 ⇒ miss 涨不到 10）；
#   当晚用户再拍板：**0 = 关掉整帧放大**（12:43 那轮 4.6 分钟名字告警 33 条、
#   miss 最高 71 ⇒ 放大被反复触发，满屏怪 + 追 400~550px 外的目标，先关掉这个副作用，
#   改用 nametag.miss_dump 存画面去查"为什么丢名字"）。
check("★ stale_pos_full_frame_after 默认 0（关掉整帧放大；正数才启用）",
      _md.get("stale_pos_full_frame_after"), 0)

# ── ② 感知框口径 ────────────────────────────────────────────────────────────
print("\n[2] get_detect_extent：感知框 = 攻击框口径 + margin，再乘 detect_range_scale")
cfg = base_cfg()
del cfg["monster_detect"]["detect_range_scale"]     # 老配置没有这个键
st = make_stub(cfg)
check("缺键 → 1.0（老口径：aoe 400/2+50=250, 170/2+50=135）",
      st.get_detect_extent(), (250, 135))
st2 = make_stub(base_cfg())
check("scale=2.0 → 翻倍（500, 270）", st2.get_detect_extent(), (500, 270))
st2y = make_stub(base_cfg(detect_range_scale_y=1.1))
check("★ 竖直单独收窄：scale=2.0 + scale_y=1.1 → (500, 148)"
      "（135 × 1.1 = 148.5，Python round 取偶 ⇒ 148）", st2y.get_detect_extent(), (500, 148))
st2y0 = make_stub(base_cfg(detect_range_scale_y=0.2))
check("scale_y<1 夹回 1.0 → 竖直回到老口径（500, 135）",
      st2y0.get_detect_extent(), (500, 135))
st3 = make_stub(base_cfg(detect_range_scale=0.2))
check("scale<1 夹回 1.0（看得更少只会白白漏怪）", st3.get_detect_extent(), (250, 135))
cfg_d = base_cfg(detect_range_scale=1.0)
cfg_d["bot"] = {"attack": "directional"}
st4 = make_stub(cfg_d)
check("directional 口径 = range_x+margin, range_y+margin",
      st4.get_detect_extent(), (400, 120))

# ── ③ 索敌框：clamp + stale 放大 ────────────────────────────────────────────
print("\n[3] get_search_box：clamp 到帧内；位置连续丢失 → 临时放大到整帧")
st = make_stub(base_cfg())
check("默认：loc ± (500, 270)", st.get_search_box(), (200, 130, 1200, 670))
st.loc_player = (50, 50)
check("贴边 → clamp 到 0", st.get_search_box(), (0, 0, 550, 320))
st.loc_player = PLAYER
st.nametag_miss_streak = 10
check("丢失 10 帧 → 整帧（0,0,1366,768）", st.get_search_box(), (0, 0, 1366, 768))
st2 = make_stub(base_cfg(stale_pos_full_frame_after=0))
st2.nametag_miss_streak = 99
check("配 0 = 关（丢了也不放大）", st2.get_search_box(), (200, 130, 1200, 670))

# ── ④ 看得见 ≠ 打得到：框外不出招，而是走过去 ──────────────────────────────
print("\n[4] 框外（看得见、够不着）→ 不出招，朝它走；攻击判定只认攻击框")
_log = LogCapture()
logging.getLogger().addHandler(_log)
try:
    # 怪中心 (1000,400)：感知框内（dx=300 ≤ 500）、攻击框外（攻击框只到 ±200）、
    # 同一层（dy=0 ≤ approach_dy_tol=60）⇒ 应该"走过去打"，且本帧不出招。
    st = make_stub(base_cfg(), [mob(1000, 400)])
    # ⚠️ 路线走位用 y="none"：路线给 up/down 时会被**绳/梯让路**整段接管
    #    （见本文件 [11]），那种场景下 approach 本来就不该跑。
    cmd = frame(st, move_x="left", move_y="none")
    check("  框外的怪不触发停下来", holding(st), False)
    check("★ 框外的怪不触发出招（攻击判定只认攻击框）", cmd[2], "none")
    check("★ 朝它走：方向键指向怪那一侧", cmd[0], "right")
    check("★ 走的时候不夹带上下（交给路线会打架）", cmd[1], "none")
    check_true("  留痕：日志里说清了「靠近」（不许静默）",
               _log.search("[靠近]"), f"抓到 {len(_log.msgs)} 条日志")
finally:
    logging.getLogger().removeHandler(_log)

# ── ⑤ 变异测试：关掉 approach_mob → 退回老行为 ──────────────────────────────
print("\n[5] 变异测试：approach_mob=false → 老行为（框外的当没看见，走位保留）")
st = make_stub(base_cfg(approach_mob=False), [mob(1000, 400)])
cmd = frame(st, move_x="left", move_y="up")
check("★ 关掉后不再朝它走（走位仍是路线给的）", (cmd[0], cmd[1]), ("left", "up"))
check("  关掉后也不出招", cmd[2], "none")

# ── ⑥ 只走同一层；梯子上不走 ────────────────────────────────────────────────
print("\n[6] 只走**同一层**（跨层要爬梯子/跳，硬走会掉下去）；梯子上不走")
st = make_stub(base_cfg(), [mob(1000, 560)])          # dy=160 > 60
cmd = frame(st, move_x="left", move_y="up")
check("★ 跨层（dy=160>60）→ 不横着走", (cmd[0], cmd[1]), ("left", "up"))
st = make_stub(base_cfg(), [mob(1000, 400)], is_on_ladder=True)
cmd = frame(st, move_x="none", move_y="up")
check("★ 梯子上 → 不抢方向（上下由路线配）", (cmd[0], cmd[1]), ("none", "up"))

# ── ⑦ 够得着 → 停下打；"打完了"只看攻击框内 ────────────────────────────────
print("\n[7] 攻击框内 → 停下打；「打完了」只看**攻击框内**（远处有怪在场也要能判完）")
# ⚠️ 2026-10-14：默认多了「目标锁定」（`monster_detect.target_hold_s: 3.0`）——
#    锁定期间**不**判"打完"，所以本节的判据只在 target_hold_s=0（关掉锁定）时成立。
#    锁定打开后的行为（不判完 / 追过去 / 继续出招 / 到点才判消失）在
#    `tools/verify_stop_to_attack.py` 的【11】节里测。
st = make_stub(base_cfg(target_hold_s=0, stop_attack_clear_s=0), [mob(760, 400)], cmd_move_x_last="right")
# 路线给"right"、朝向已经是 right ⇒ AOE 不再多走一步，"站定"的清零看得见。
# （路线 y 用 none：up/down 会被绳/梯让路整段接管 —— 见 [11]。）
cmd = frame(st, move_x="right", move_y="none")
check("  攻击框内 → 路线给的横向走位被清零（站定）", cmd[0], "none")
check("  同帧出招", cmd[2], "attack")
check("  纵向不夹带（路线 y=none）", cmd[1], "none")
check("  状态位立起来", holding(st), True)
st_turn = make_stub(base_cfg(), [mob(760, 400)], facing="left")
check("  朝向不对时仍允许「转身那一小步」（≠ 边走边打）",
      frame(st_turn, move_x="right", move_y="none")[0], "right")
# 唯一那只怪死了，但**远处还有一只**（仍在感知框里）：必须能判"打完"，否则永远走不掉
st.monsters = [mob(1000, 400)]
frame(st, move_x="right", move_y="none")
frame(st, move_x="right", move_y="none")
check("★ 攻击框空了 2 个检测帧 → 判完（远处那只不算）", holding(st), False)
st.monsters = []                       # 连远处那只也没了（不然会转成"走过去打"）
cmd = frame(st, move_x="right", move_y="none")
check("★ 判完后走位交回路线", (cmd[0], cmd[1]), ("right", "none"))

# ── ⑧ 朝向只按攻击框内的怪定 ────────────────────────────────────────────────
print("\n[8] 朝向只按**攻击框内**的怪定（否则朝着一只够不着的远怪转身，打偏近处这只）")
# (880,400) 在攻击框内（曼哈顿 180）；(690,560) 在框外但在**左上方**（曼哈顿 170 ⇒
# 老口径"按最近"会选它，于是角色朝左转身，把右边这只打偏）
st = make_stub(base_cfg(), [mob(880, 400), mob(690, 560)])
check("  老口径（全部怪）确实会选左上方那只", st.get_face_direction(), "left")
check("★ 新口径（只按攻击框内的怪）→ 朝右（近处这只）",
      st.get_face_direction(st._mobs_in_attack_box()), "right")
check_true("★ AOE 分支确实传的是攻击框内那批（白盒）",
           "get_face_direction(_engage)" in inspect.getsource(
               MapleStoryAutoBot.update_cmd_by_mob_detection))

# ── ⑨ 名字定位护栏 ──────────────────────────────────────────────────────────
print("\n[9] 名字定位护栏 _nametag_pos_plausible（画面外假命中）")
st = make_stub(base_cfg())
_ok, _c = st._nametag_pos_plausible((0, -60), 40)     # cand=(20,-90)
check("① 负数（真机 loc=(21,-30) 那种）→ 判假", _ok, False)
check("  并且把算出来的位置一起交回来（便于日志）", _c, (20, -90))
_ok, _c = st._nametag_pos_plausible((1400, 100), 40)  # cand=(1420,70) 越右边界
check("② 越出帧边界 → 判假", _ok, False)
check("  帧内的正常命中 → 接受", st._nametag_pos_plausible((300, 300), 40)[0], True)
check("  贴边（cand=(1360,300)，帧宽 1366）→ 接受（4px 余量）",
      st._nametag_pos_plausible((1340, 300), 40)[0], True)
st_noimg = make_stub(base_cfg())
st_noimg.img_frame = None
check("★ 取不到帧尺寸 → 只做①（这是「无法判定」，不是放行）",
      st_noimg._nametag_pos_plausible((1400, 100), 40)[0], True)
check("  同上，但负数那条仍然拦得住",
      st_noimg._nametag_pos_plausible((0, -60), 40)[0], False)
_src_nt = inspect.getsource(MapleStoryAutoBot.get_player_location_by_nametag)
check_true("★ 调用方真的用了这个护栏（白盒）", "_nametag_pos_plausible(" in _src_nt)
check_true("★ 拒绝时按「未命中」处理（miss_streak 累加仍在）",
           "self.nametag_miss_streak += 1" in _src_nt)

# ── ⑩ 画框与检测同源 + 登记 ─────────────────────────────────────────────────
print("\n[10] 画框与检测**同源**（get_search_box 唯一出处）+ UI/中文名登记")
_src_det = inspect.getsource(MapleStoryAutoBot.update_cmd_by_mob_detection)
_src_info = inspect.getsource(MapleStoryAutoBot.update_info_on_img_frame_debug)
check_true("★ 怪检测用 get_search_box()（不再是就地手算）",
           "get_search_box()" in _src_det)
check_true("★ 帧尾画框也用 get_search_box()（否则画的框 ≠ 用的框）",
           "get_search_box()" in _src_info)
check_true("  帧尾那段不再手算 search_box_margin（口径只有一处）",
           "search_box_margin" not in _src_info)
_src_file = os.path.join(_root, "src", "ui", "ui.py")
with open(_src_file, encoding="utf-8") as _f:
    _ui = _f.read()
check_true("  ui.py 收起 approach_dy_tol（算法内部参数）",
           "'approach_dy_tol'" in _ui)
check_true("  ui.py 收起 stale_pos_full_frame_after（算法内部参数）",
           "'stale_pos_full_frame_after'" in _ui)
check_true("  ui.py **没有**收起 detect_range_scale（这是用户要调的旋钮）",
           "'detect_range_scale'" not in _ui)
check_true("  ui.py **没有**收起 detect_range_scale_y（2026-10-13 起它有了主界面控件："
           "「看得远倍数 Y」）",
           "'detect_range_scale_y'" not in _ui)
check_true("  ui.py **没有**收起 approach_mob（开关留给用户按）",
           "'approach_mob'" not in _ui)
with open(os.path.join(_root, "src", "utils", "cfg_names.py"), encoding="utf-8") as _f:
    _names = _f.read()
for _k in ("detect_range_scale", "detect_range_scale_y", "approach_mob",
           "approach_dy_tol", "stale_pos_full_frame_after"):
    check_true(f"  cfg_names.py 有 {_k} 的中文名", f'"{_k}"' in _names)

# ── ⑪ 绳/梯让路（治「卡在绳子上」）───────────────────────────────────────────
print("\n[11] 绳/梯让路：路线在爬时怪逻辑一律不动走位/动作（治「卡在绳子上」）")
# 真机症状（2026-10-13 用户原话「会卡在绳子上」）：梯子口的"离开梯子"判据是
#   小地图全局 x 变化 ≥2px，而**靠近/转身的横向移动恰好会让 x 变** ⇒ is_on_ladder
#   被误判成 False ⇒ 引擎不再补 up、怪逻辑又把 y 清零 ⇒ 角色吊在绳上反复起跳。
st = make_stub(base_cfg(), [mob(760, 400)], is_on_ladder=True)
cmd = frame(st, move_x="left", move_y="up")           # 攻击框内有怪
check("① 在梯子上（is_on_ladder）→ 立不起停留态", holding(st), False)
check("   走位原样保留（路线还在爬）", (cmd[0], cmd[1]), ("left", "up"))
check("   本帧不出招（绳子上的动作留给路线）", cmd[2], "none")

st = make_stub(base_cfg(), [mob(760, 400)])           # is_on_ladder 被误清成 False
cmd = frame(st, move_x="none", move_y="up")           # 但路线指令仍是 up
check("② 路线指令 y=up（引擎状态被误清）→ 同样让路", holding(st), False)
check("   走位保留 up（这是爬上去的唯一手段）", cmd[1], "up")

st = make_stub(base_cfg(), [mob(1000, 400)])          # 攻击框外的怪（会触发靠近）
cmd = frame(st, move_x="none", move_y="down")
check("③ 路线在爬时不**靠近**（远处有怪也不横着走）", cmd[0], "none")

st = make_stub(base_cfg(), [mob(760, 400)], is_on_ladder=True,
               _stop_hold=True, _stop_hold_t0=0.0)
cmd = frame(st, move_x="none", move_y="up")
check("④ 已经站定 → 路线一爬就立刻松开（不许钉在绳上）", holding(st), False)
check("   松开后走位交回路线", cmd[1], "up")

st = make_stub(base_cfg(), [mob(760, 400)], is_on_ladder=True, cmd_action="attack")
st.cmd_move_x, st.cmd_move_y = "left", "up"
st.update_cmd_by_mob_detection()
check("⑤ 残留 attack 被清掉（否则在绳上一路按技能键、还挡掉顶上的 jump）",
      st.cmd_action, "none")

_src_climb = inspect.getsource(MapleStoryAutoBot.update_cmd_by_mob_detection)
check_true("★ 让路块位置：回正 → 绳/梯让路 → 停留（顺序白盒）",
           0 <= _src_climb.find("is_using_home_route")
           < _src_climb.find("_climbing")
           < _src_climb.find("_stop_hold"), True)
check_true("★ 让路时留痕（不许静默）", "[绳/梯]" in _src_climb, True)

# ══════════════════════════════════════════════════════════════════════════
print("\n[12] mob_engage_mode：怪「算够得着」的两种判据（2026-10-14 加）")
# 由来（用户 m06668「红色攻击框里有绿色的检测框，但是就是不攻击」）：
#   老口径 min_area 要求「与攻击框的交叠面积 >= 本图**最小**那张模板的面积」——
#   对体型最小的怪，这等于"必须整个框都落在攻击框里"，差 1px 就不算够得着。
#   本节的场景：PLAYER (700,400) + aoe 400×170 ⇒ 攻击框 x500~900 / y315~485；
#   fixture 的怪 40×40 ⇒ 门槛 = min(1600, max_mob_area_trigger 1500) = 1500。
#   ⚠️ 2026-10-14：**出厂默认已由 `min_area` 改成 `center`**（用户拍板"宽判定"：
#      "人不能准确计算，只能预估大概在范围内就会攻击"）。老口径仍然保留且必须
#      可回退，所以下面照样把它钉死 —— 只是期望值反过来。
_mob_edge = mob(883, 400)          # 怪框 x863~903 ⇒ 交叠 37×40 = 1480 < 1500
check("  场景自检：这只怪的中心确实在攻击框里（883 ≤ 900）",
      bool(500 <= 883 <= 900 and 315 <= 400 <= 485), True)
check("★ min_area（可回退的老口径）：只差 20px² 就不算够得着",
      len(make_stub(base_cfg(mob_engage_mode="min_area"), [_mob_edge])
          ._mobs_in_attack_box()), 0)
check("★ center：中心在攻击框内 ⇒ 算够得着（用户直觉的那一档）",
      len(make_stub(base_cfg(mob_engage_mode="center"), [_mob_edge])
          ._mobs_in_attack_box()), 1)
check("★ center 不放宽过头：中心在框外（950）就不算",
      len(make_stub(base_cfg(mob_engage_mode="center"), [mob(950, 400)])
          ._mobs_in_attack_box()), 0)
check("★ 畸形值 ⇒ 退回 min_area（fail-closed，不猜）",
      len(make_stub(base_cfg(mob_engage_mode="centre"), [_mob_edge])
          ._mobs_in_attack_box()), 0)
check("★ None ⇒ 也退回 min_area",
      len(make_stub(base_cfg(mob_engage_mode=None), [_mob_edge])
          ._mobs_in_attack_box()), 0)
check("★ 缺键 ⇒ 也退回 min_area（老配置不写这个键照样跑）",
      len(make_stub(base_cfg(), [_mob_edge])._mobs_in_attack_box()), 0)
with open(os.path.join(_root, "config", "config_default.yaml"),
          encoding="utf-8") as _f12:
    _md12 = (yaml.safe_load(_f12) or {}).get("monster_detect") or {}
check("★ 配置实体里出厂默认 = center（宽判定，2026-10-14 用户拍板；回退改 min_area）",
      _md12.get("mob_engage_mode"), "center")

print("\n" + "=" * 72)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 72)
sys.exit(1 if FAIL else 0)