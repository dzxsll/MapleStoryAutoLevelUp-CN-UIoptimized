# -*- coding: utf-8 -*-
'''verify_stop_to_attack.py
==========================

自检：巡逻中「停下来打怪」（`monster_detect.stop_to_attack`，2026-10-13 加）。

背景（用户原话，2026-10-13）：
    「现在我想要巡逻中，检测到怪，打完怪再移动，而不是在移动中穿插着攻击」。
    旧行为 = 路线每帧给走位 + 本函数叠一个 `attack` ⇒ 边走边打：路过的怪只吃到
    擦身而过的几刀就走远了；directional 还要"先转身"，转完人已经出了攻击框。

本自检钉死下面这些（每条都是可复跑的判据，不是注释里的承诺）：
  ① 配置实体：`config/config_default.yaml` 里三个键存在且默认 = true / 2 / 20；
  ② 框里有怪 → **本帧路线刚写的走位被清零**（站定），并且照常出招；
  ③ 变异测试：`stop_to_attack: false` 时同样的场景**走位必须保留**（老行为），
     证明 ② 的清零确实来自本功能，而不是别处顺手改的；
  ④ 「打完了」判据：连续 `stop_attack_clear_frames` 个**检测帧**框内为空才交回路线，
     数不够时继续站着（防"怪丢一帧就走"）；
  ⑤ ④的计数只认**检测帧**：`every_n_frames: 2` 时中间帧不计数
     （不被降频放大/缩小 —— 拿跳过的帧计数会把"打完"判定算错）；
  ⑥ 安全阀①：框里有怪但**一直出不了招**（怪在正上方，够不着）→ 约 2.5s 放行 + WARNING；
  ⑦ 安全阀②：总时长超 `stop_attack_timeout_s` → 强制放行 + WARNING，
     且**必须先清场一次**才允许再停下（治"满屏都是怪 ⇒ 停 20s→放行 1 帧→又停"）；
  ⑧ 回正线期间**不参与**：走位不被清零，也不建立停留状态；
  ⑨ 登记同步：`config_default.yaml` / `src/ui/ui.py` 的 ADV_FIELD_HIDE / `cfg_names.py`；
  ⑩ 白盒：清零那段夹在「回正 return」**之后**、「没有怪」分支**之前**（顺序钉死，
     挪到回正前面就会在高优先级场景里抢方向）。
  ⑪ 目标锁定（2026-10-14 第一批）：认出过就记住，连续 `target_hold_s` 秒没再看见
     才算"怪物消失"；锁定期不判"打完"、追过去、继续出招（AOE）。
  ⑫ 2026-10-14 第二批（低频识别优化 P1，用户 m05009「落地」）：
     · 放行判据 = 「空够 `stop_attack_clear_s` 秒」∩「空够 `stop_attack_clear_frames`
       个检测帧」∩「本次停留**出过招**」—— 秒数与采样率无关；"出过招"专治
       **秒杀怪**（怪一刀死，旧规则会白站 `target_hold_s` 秒）；
       一次都没出过招 = "打不着"而不是"打完了" ⇒ 不判完，且**留日志**；
       0 = 退回只按帧数判；
  ⑬ 追怪「出现确认」：只认到一帧的远处目标**不追**（防单帧幻影），连续两次采样
     都看见才追；
  ⑭ 追怪「方向最小保持」：老方向那侧还有怪时，`approach_dir_min_s` 内不许掉头
     （治真机日志里 left/right 每隔几秒换一次边的抖动）；老方向那侧一只怪都没有
     ⇒ 立即掉头；
  ⑮ P2 交战期间只扫「攻击框 + `search_box_margin`」（`engaged_scan`）：
     巡逻态仍是全感知框、交战态收紧、`false` 退回老行为、位置失效优先（整帧）；
   ⑯ 「判定打完」那行日志印出的**空检测帧数必须是实数**（≥ `clear_frames`）：
      2026-10-14 真机 log 里 76 条「判定打完」有 61 条印着「（0 个检测帧）」，看着像
      "一个检测帧都没空就判完了" —— 其实是**清零在前、打印在后**，日志把自己的证据
      抹掉了（放行行为本身没错：它要求 ≥2 个检测帧 ∩ ≥`clear_s` 秒 ∩ 出过招）。

⚠️ 不依赖游戏窗口 / minimaps / config_data.yaml / 怪物模板（CI 检出后都没有），
   全部用内存桩 + 假时钟。引擎调用走「绕过 __init__ 的 Stub」，与既有自检同口径
   （见 tools/verify_two_side_mob_attack.py）。

跑法：python -m tools.verify_stop_to_attack
'''
import inspect
import logging
import os
import re
import sys

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
import src.engine.MapleStoryAutoLevelUp as _mod              # noqa: E402

PLAYER = (700, 400)


# ── 假时钟：`update_cmd_by_mob_detection` 里的冷却/超时判据全走 time.time ──
class Clock:
    '''把引擎模块里的 `time.time` 换成可控时钟（与 verify_two_side_mob_attack 同法）。

    ⚠️ `_mod.time` 就是全局的 time 模块 —— 替换会影响同进程的 logging 时间戳，
       所以必须 try/finally 还原。
    '''

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


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        try:
            self.msgs.append(record.getMessage())
        except Exception:
            pass

    def search(self, needle):
        return [m for m in self.msgs if needle in m]


def base_cfg(**md_over):
    md = {
        "mode": "contour_only", "contour_blur": 5, "diff_thres": 0.4,
        "search_box_margin": 50, "max_mob_area_trigger": 1500,
        "with_enemy_hp_bar": False, "match_all_mobs": False,
        "verify_color": True, "verify_color_tol": 30, "verify_min_ratio": 0.75,
        "every_n_frames": 1,
        "stop_to_attack": True, "stop_attack_clear_frames": 2,
        # ⚠️ 2026-10-14：出厂默认是 0.3 秒（见 config_default.yaml），但**夹具**默认 0.0
        #    = 只按帧数判的老口径 —— 否则本节所有老用例都要跟着假时钟一起改。
        #    P1 的时间窗在【12】里显式打开。
        "stop_attack_clear_s": 0.0,
        "stop_attack_timeout_s": 20,
        # ★ 2026-10-14：宽判定余量（`mob_engage_margin`）在本文件里**关掉** ——
        #   本文件钉的是"停下打怪"的站定/清场/安全阀逻辑，余量会把所有几何
        #   期望整体挪 40px。余量自己的行为在 tools/verify_engage_margin.py 里钉。
        "mob_engage_margin": 0,
    }
    md.update(md_over)
    return {
        "bot": {"attack": "directional"},
        "monster_detect": md,
        "character": {"width": 100, "height": 150},
        "directional_attack": {"range_x": 350, "range_y": 70,
                               "cooldown": 0.9, "turn_frames": 3},
        "aoe_skill": {"range_x": 400, "range_y": 170, "cooldown": 0.05},
    }


def mob(cx, cy, size=(40, 40), score=0.1):
    '''按**中心点**造一只怪（引擎的 position 是左上角）。'''
    w, h = size
    return {"name": "m", "position": (cx - w // 2, cy - h // 2),
            "size": (w, h), "score": score}


def make_stub(cfg, monsters=(), facing="right", **kw):
    '''能走通索敌链的假引擎（**不跑 __init__**）。'''
    class S:
        pass

    s = S()
    for _n in ("get_attack_range", "get_nearest_monster", "get_attack_direction",
               "get_face_direction", "update_cmd_by_mob_detection",
               # 2026-10-13 加：感知框/攻击框分开（见引擎里的 get_search_box）
               "get_detect_extent", "get_search_box",
               "_min_mob_area", "_mobs_in_attack_box",
               # 2026-10-14 加：追怪挑目标（出现确认 + 方向最小保持）统一走它
               "_pick_approach_target",
               # 2026-10-14 加：「没出招·归因」纯观测日志（它要读攻击框尺寸与前台状态）
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
    # ⚠️ 真实尺寸模板：`get_nearest_monster` 的重叠阈值取
    #    min(min_mob_area, max_mob_area_trigger)；留空会让阈值变 0、任何怪都算命中。
    s.monsters_info = {"m": [(np.zeros((40, 40, 3), np.uint8), None)]}
    s.is_on_ladder = False
    s.is_using_home_route = False
    s.t_last_attack = 0.0
    s.cmd_action = "none"
    s.cmd_move_x = "none"
    s.cmd_move_y = "none"
    s.cmd_move_x_last = facing
    s._turn_frames_left = 0
    s._turn_dir = facing
    s._turn_pending_dir = None
    s._attack_dir_last = None
    # 打怪逻辑要用 img_frame.shape 夹搜索框边界（只当尺寸来源，不真跑图像处理）
    s.img_frame = np.zeros((600, 800, 3), np.uint8)
    s._perf = lambda *a, **k: None
    s._mob_skip = 0
    # ⚠️ 必须**动态读** s.monsters：冻成闭包会让"喂了怪却说没怪"，非常难查。
    s.get_monsters_in_range = lambda *a, **k: s.monsters
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def frame(s, move_x="none", move_y="none"):
    '''模拟 HuntingState.on_frame 里与打怪有关的那一段：

        route 写走位 → update_cmd_by_mob_detection → 记录本帧实际发出的方向

    （`src/states/hunting.py`：update_cmd_by_route → check_reach_goal →
      update_cmd_by_mob_detection → kb.set_command → 记 cmd_move_x_last）
    '''
    s.cmd_move_x, s.cmd_move_y, s.cmd_action = move_x, move_y, "none"
    s.update_cmd_by_mob_detection()
    s.cmd_move_x_last = s.cmd_move_x
    return (s.cmd_move_x, s.cmd_move_y, s.cmd_action)


def holding(s):
    return bool(getattr(s, "_stop_hold", False))


print("=" * 72)
print(" 自检：巡逻中「停下来打怪」（monster_detect.stop_to_attack）")
print("=" * 72)

# ── ① 配置实体 ──────────────────────────────────────────────────────────────
print("\n[1] 配置实体：config/config_default.yaml 的三个键（不是注释里的承诺）")
import yaml  # noqa: E402
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_root, 'config', 'config_default.yaml'), encoding='utf-8') as _f:
    _md = (yaml.safe_load(_f) or {}).get('monster_detect') or {}
check("stop_to_attack 默认开", _md.get('stop_to_attack'), True)
check("stop_attack_clear_frames 默认 2", _md.get('stop_attack_clear_frames'), 2)
check("stop_attack_timeout_s 默认 20", _md.get('stop_attack_timeout_s'), 20)
check("target_hold_s 默认 3.0（2026-10-14 目标锁定）", _md.get('target_hold_s'), 3.0)
check("stop_attack_clear_s 默认 0.3（2026-10-14 P1 时间窗）",
      _md.get('stop_attack_clear_s'), 0.3)
check("approach_confirm_s 默认 0.5（2026-10-14 P1 出现确认）",
      _md.get('approach_confirm_s'), 0.5)
check("approach_dir_min_s 默认 0.4（2026-10-14 P1 方向最小保持）",
      _md.get('approach_dir_min_s'), 0.4)
check("engaged_scan 默认开（2026-10-14 P2 交战收紧）", _md.get('engaged_scan'), True)

# ── ② 有怪 → 站定 + 出招 ───────────────────────────────────────────────────
print("\n[2] 框里有怪 → 路线刚写的走位被清零（站定），且照常出招")
# 怪在右边、朝向已经是对的 ⇒ AOE 本帧直接出招、且**不再多走一步**
#   ⇒ "站定"的清零能从 cmd_move_x 上看出来。
# ⚠️ 这两处口径 2026-10-13 定：
#   ① 用 AOE 而不是 directional —— directional 出招时会主动把 cmd_move_x 设成
#      "none"（先转身后出招那条路径），从 x 上看不出"是站定清零还是出招清零"；
#   ② 路线走位用 y="none" —— 路线给 up/down 时会被**绳/梯让路**整段让给路线
#      （治用户实测「会卡在绳子上」，见 tools/verify_mob_sight.py 的 [11]）。
_AOE = base_cfg(); _AOE["bot"] = {"attack": "aoe_skill"}
st = make_stub(_AOE, [mob(760, 400)], facing="right")
cmd = frame(st, move_x="right", move_y="none")
check_true("停下时状态位被立起来", holding(st))
check("★ 出招帧的 cmd_action", cmd[2], "attack")
check("★ 路线给的横向走位被清零（站定）", cmd[0], "none")
check("   纵向不夹带（路线 y=none；站定也不许自己给方向）", cmd[1], "none")

# ── ③ 变异测试：关掉功能必须回到"边走边打" ─────────────────────────────────
print("\n[3] 变异测试：stop_to_attack=false → 走位保留（证明 [2] 的清零来自本功能）")
_AOE_OFF = base_cfg(stop_to_attack=False); _AOE_OFF["bot"] = {"attack": "aoe_skill"}
st_off = make_stub(_AOE_OFF, [mob(760, 400)], facing="right")
cmd_off = frame(st_off, move_x="right", move_y="none")
check("  关掉后不再立状态位", holding(st_off), False)
check("★ 关掉后路线走位保留（老行为：边走边打）", cmd_off[0], "right")
check("  关掉后照样出招（没有把打怪一起关掉）", cmd_off[2], "attack")

# ── ④ 打完了才走 ───────────────────────────────────────────────────────────
print("\n[4] 「打完了」= 连续 clear_frames(2) 个检测帧框内为空（target_hold_s=0 的老口径）")
# ⚠️ 2026-10-14：默认多了「目标锁定」（target_hold_s=3.0）—— 锁定期间**不判**打完，
#    所以本节的判据只在 target_hold_s=0（关掉锁定）时成立。锁定打开时的行为见 [11]。
_AOE_NO_LOCK = base_cfg(target_hold_s=0); _AOE_NO_LOCK["bot"] = {"attack": "aoe_skill"}
st = make_stub(_AOE_NO_LOCK, [mob(760, 400)], facing="right")
frame(st, move_x="right", move_y="none")
check("  第 1 帧有怪 → 站着", holding(st), True)
st.monsters = []                       # 怪死了
check("  空帧 #1：还站着（防「怪丢一帧就走」）",
      frame(st, move_x="right", move_y="none")[0], "none")
check("  空帧 #1：仍然是停留态", holding(st), True)
check("  空帧 #1：残留攻击指令被清（防打空气）", st.cmd_action, "none")
check("★ 空帧 #2：判定打完 → 交回路线（走位恢复）",
      frame(st, move_x="right", move_y="none")[0], "right")
check("  判定打完 → 状态位松开", holding(st), False)

# ── ⑤ 计数只认检测帧 ───────────────────────────────────────────────────────
print("\n[5] 计数只认**检测帧**（every_n_frames=2 时中间帧不计数）")
# 降频语义（引擎原文）：`_skip = _mob_skip + 1`，`_skip >= _iv` 才真跑检测
#   ⇒ **降频开启时第 1 帧也是跳过帧**（_skip=1 < 2）。所以"站定"只会发生在检测帧上。
#   这条既是限制也是口径，必须钉住：拿跳过帧计数会把"打完"判定算错。
_AOE_IV = base_cfg(every_n_frames=2, target_hold_s=0); _AOE_IV["bot"] = {"attack": "aoe_skill"}
st = make_stub(_AOE_IV, [mob(760, 400)], facing="right")
frame(st, move_x="right", move_y="none")          # 跳过帧
check("  降频下第 1 帧 = 跳过帧 → 不参与（也不会站住）", holding(st), False)
frame(st, move_x="right", move_y="none")          # 检测帧、有怪
check("  第 2 帧 = 检测帧，有怪 → 站着", holding(st), True)
st.monsters = []
cmd = frame(st, move_x="right", move_y="none")    # 跳过帧（不跑检测）
check("  跳过帧：不计数", int(getattr(st, "_stop_hold_empty", 0)), 0)
check("  跳过帧：走位仍被清零（停留态跨帧保持）", cmd[0], "none")
frame(st, move_x="right", move_y="none")          # 检测帧、框内空 #1
check("  检测帧的空帧 #1：计 1 次", int(getattr(st, "_stop_hold_empty", 0)), 1)
check("  空帧 #1 时还站着", holding(st), True)
frame(st, move_x="right", move_y="none")          # 又一个跳过帧
check("  又跳过一帧：计数不变", int(getattr(st, "_stop_hold_empty", 0)), 1)
check("★ 检测帧的空帧 #2 → 放行（计数没被跳过的帧搅乱）",
      frame(st, move_x="right", move_y="none")[0], "right")

# ── ⑥ 安全阀①：一直出不了招 ───────────────────────────────────────────────
print("\n[6] 安全阀①：停下后一直出不了招（转身磨蹭/帧率崩）→ 约 2.5s 后放行 + WARNING")
# ⚠️ 2026-10-13 改夹具：原来这里用「够不着的怪（正上方 dy=100）」来造"出不了招"，
#    但感知框放大后**够不着的怪根本不会让角色停下**（改走 approach_mob"走过去打"，
#    见 tools/verify_mob_sight.py 的 [4]/[5]）⇒ 那个夹具已经造不出这个场景。
#    现在能踩到这条阀的场景是「怪在攻击框里、但出招路径一直没走完」——最现实的
#    就是**先转身 + 帧率崩**：directional 要先转身（turn_frames=3）且转身期间不出招，
#    本机实测一帧 0.3~0.6s，三四帧就超过 idle_s(2.5s)。这里用慢帧复现。
_logh = LogCapture()
logging.getLogger().addHandler(_logh)
try:
    with Clock(1000.0) as ck:
        # 怪中心 (760,400)：在**攻击框**里（右框 x∈[700,1050]、y∈[365,435]），
        # 但角色朝向是 left ⇒ 本帧只转身、不出招（转身期间 cmd_move_x 会被设为
        # 目标方向 —— 那一小步是出招必需的，不算"边走边打"）。
        st = make_stub(base_cfg(), [mob(760, 400)], facing="left")
        frame(st, move_x="left", move_y="none")
        check("  认得出怪 → 先站住", holding(st), True)
        ck.tick(1.0)
        frame(st, move_x="left", move_y="none")
        check("  1.0s 还没到放行线（2.5s）→ 仍站着", holding(st), True)
        ck.tick(2.0)                                  # 累计 3.0s
        cmd = frame(st, move_x="left", move_y="none")
        check("★ 超过 idle 线 → 放行", holding(st), False)
        # "走位交回路线"要看**出招逻辑不碰走位**的那一帧：directional 出招时会主动把
        # cmd_move_x 设成 "none"，不压冷却就分不清"是站定清零还是出招清零"。
        st.t_last_attack = ck.t
        check("★ 放行后走位交回路线（不再被清零）",
              frame(st, move_x="left", move_y="none")[0], "left")
        check("★ 放行后要求先清场（否则会在同一只怪上反复停）",
              bool(getattr(st, "_stop_hold_need_clear", False)), True)
        check_true("  WARNING 里说清了原因（不许静默）",
                   _logh.search("一直出不了招"),
                   detail=f"抓到 {len(_logh.msgs)} 条日志")
finally:
    logging.getLogger().removeHandler(_logh)

# ── ⑦ 安全阀②：总时长 + 清场一次才允许再停 ─────────────────────────────────
print("\n[7] 安全阀②：停留超时 → 强制放行；且必须先清场一次才允许再停下")
_logh = LogCapture()
logging.getLogger().addHandler(_logh)
try:
    with Clock(1000.0) as ck:
        cfg = base_cfg(stop_attack_timeout_s=2)
        cfg["bot"] = {"attack": "aoe_skill"}       # 冷却 0.05s ⇒ 每帧都能出招
        st = make_stub(cfg, [mob(760, 400)], facing="right")
        frame(st, move_x="right", move_y="none")
        check("  AOE 有怪 → 站住", holding(st), True)
        ck.tick(1.0)
        st.t_last_attack = ck.t                    # 模拟这一帧刚出过招
        frame(st, move_x="right", move_y="none")
        check("  停留 1.0s（上限 2s）→ 仍站着", holding(st), True)
        ck.tick(1.5)                               # 累计 2.5s > 2s，但 0.5s 前才出过招
        st.t_last_attack = ck.t - 0.5
        cmd = frame(st, move_x="right", move_y="none")
        check("★ 超过总时长上限 → 强制放行", holding(st), False)
        # 同上：先让出招进冷却，才看得出"走位有没有交回路线"（AOE 冷却 0.05s）。
        st.t_last_attack = ck.t
        check("★ 放行后走位交回路线（不再被清零）",
              frame(st, move_x="right", move_y="none")[0], "right")
        check_true("  WARNING 里说清了原因（不许静默）",
                   _logh.search("强制恢复巡逻"), detail=f"抓到 {len(_logh.msgs)} 条日志")
        check("★ 放行后打上「先清场」的封条",
              bool(getattr(st, "_stop_hold_need_clear", False)), True)
        ck.tick(0.1)
        frame(st, move_x="right", move_y="none")     # 怪还在
        check("★ 封条生效：满屏都是怪时不会马上又停下", holding(st), False)
        st.monsters = []
        frame(st, move_x="right", move_y="none")     # 清场一次
        check("★ 清场后解封", bool(getattr(st, "_stop_hold_need_clear", False)), False)
        st.monsters = [mob(760, 400)]
        frame(st, move_x="right", move_y="none")
        check("★ 解封后重新遇到怪 → 照常停下打", holding(st), True)
finally:
    logging.getLogger().removeHandler(_logh)

# ── ⑧ 回正期间不参与 ───────────────────────────────────────────────────────
print("\n[8] 回正线期间不参与（回正 = 回家，优先级高于打怪）")
st = make_stub(base_cfg(), [mob(760, 400)], facing="right", is_using_home_route=True)
cmd = frame(st, move_x="left", move_y="up")
check("★ 回正时走位不被清零", (cmd[0], cmd[1]), ("left", "up"))
check("  回正时不建立停留状态", holding(st), False)
check("  但怪照样被记下来（画面框/日志要用）", len(st.monsters), 1)

# ── ⑨ 登记同步 ─────────────────────────────────────────────────────────────
print("\n[9] 新键在 UI 隐藏清单与中文名表里都登记了")
_ui = open(os.path.join(_root, 'src', 'ui', 'ui.py'), encoding='utf-8').read()
_names = open(os.path.join(_root, 'src', 'utils', 'cfg_names.py'), encoding='utf-8').read()
_i0 = _ui.index("'monster_detect':")
_hide = _ui[_i0:_ui.index(']', _i0)]
check("  ui.py ADV_FIELD_HIDE 收起了 stop_attack_clear_frames",
      "'stop_attack_clear_frames'" in _hide, True)
check("  ui.py ADV_FIELD_HIDE 收起了 stop_attack_timeout_s",
      "'stop_attack_timeout_s'" in _hide, True)
check("  ui.py ADV_FIELD_HIDE 收起了 target_hold_s（算法内部参数）",
      "'target_hold_s'" in _hide, True)
for _k2 in ("stop_attack_clear_s", "approach_confirm_s", "approach_dir_min_s"):
    check(f"  ui.py ADV_FIELD_HIDE 收起了 {_k2}（算法内部参数）",
          f"'{_k2}'" in _hide, True)
check("  ★ engaged_scan 是**开关**，不许收起（用户要能按）",
      "'engaged_scan'" in _hide, False)
check("  ui.py ADV_FIELD_HIDE **没有**收起开关本身（用户要能按）",
      "'stop_to_attack'" in _hide, False)
for _k in ("stop_to_attack", "stop_attack_clear_frames", "stop_attack_timeout_s",
           "target_hold_s", "stop_attack_clear_s", "approach_confirm_s",
           "approach_dir_min_s", "engaged_scan"):
    check(f"  cfg_names.py 有 {_k} 的中文名", f'"{_k}"' in _names, True)

# ── ⑩ 白盒：顺序钉死 ───────────────────────────────────────────────────────
print("\n[10] 白盒：清零那段必须夹在「回正 return」之后、「没有怪」之前")
_src = inspect.getsource(MapleStoryAutoBot.update_cmd_by_mob_detection)
_i_home = _src.index("if self.is_using_home_route:")
_i_hold = _src.index("self._stop_hold = True")
_i_clear = _src.index('self.cmd_move_y = "none"')
_i_nomob = _src.index("# Check if no mob to attack")
check_true("  回正判定 → 停留判定 → 清零 → 没有怪分支（顺序）",
           _i_home < _i_hold < _i_clear < _i_nomob,
           detail=f"{_i_home} < {_i_hold} < {_i_clear} < {_i_nomob}")
check("  清场封条存在（防「停 20s→放行 1 帧→又停」）",
      "_stop_hold_need_clear" in _src, True)
check("  两条安全阀都有 WARNING（不许静默）",
      ("一直出不了招" in _src) and ("强制恢复巡逻" in _src), True)
check("  清零的是**两轴**（上下也要清，不只左右）",
      ('self.cmd_move_x = "none"' in _src[_i_hold:_i_nomob])
      and ('self.cmd_move_y = "none"' in _src[_i_hold:_i_nomob]), True)

# ── ⑪ 目标锁定（2026-10-14 加）──────────────────────────────────────────────
print("\n[11] 目标锁定：认出过就记住它，直到「连续 target_hold_s 秒没再看见」")
# 病根（真机日志 MSBot_2026-10-14_10-03-15.log 11:02 段）：老口径是"攻击框里连续
#   clear_frames(2) 个**检测帧**没怪就放行"，AOE + every_n_frames=2 下约 0.15 秒；
#   怪一走神就判"打完" ⇒ 日志里 [停下打怪]/[判定打完] 每秒抖好几次、基本没打。
# ⚠️ 本节只测**锁定语义**，所以把 P1 的秒数窗关掉（`stop_attack_clear_s=60`）：
#    否则"出过招 + 空够秒数 ⇒ 收手"会先于锁定过期生效（那条规则见【12】④）。
_logh = LogCapture()
logging.getLogger().addHandler(_logh)
try:
    with Clock(1000.0) as ck:
        cfg = base_cfg(target_hold_s=3.0, stop_attack_clear_s=60.0)
        cfg["bot"] = {"attack": "aoe_skill"}       # 冷却 0.05s
        cfg["monster_detect"]["approach_mob"] = True   # 追怪要它开着（这里显式钉住）
        st = make_stub(cfg, [mob(760, 400)], facing="right")
        frame(st, move_x="right", move_y="none")
        check("  ① 攻击框里认出怪 → 建立锁定（时间戳 > 0）",
              float(getattr(st, "_mob_lock_seen_t", 0.0)) > 0, True)
        check("  ① 记住的是它的中心点", getattr(st, "_mob_lock_pos", None), (760, 400))

        # ② 怪走出攻击框（AOE ±200 之外）但仍在"看得见"的名单里 ⇒ 不判打完 + 追
        st.monsters = [mob(1080, 400)]
        ck.tick(0.2)
        cmd = frame(st, move_x="left", move_y="none")
        check("★ ② 走出攻击框**不**判打完（仍在停留态）", holding(st), True)
        # ⚠️ P1 起"追"要先过「出现确认」（连续两次采样，见【13】）⇒ 这一帧**不该**出现
        #    追怪日志。为什么不用 cmd_move_x 判：AOE 的"锁定期间继续出招"本来就会把
        #    朝向转向最后看见的位置（那是出招必需的），会盖掉"追"的观测面。
        check_true("  ② 第一帧还不够（P1 出现确认）—— 追怪日志还没出现",
                   not _logh.search("[锁定]"))
        ck.tick(0.1)
        cmd = frame(st, move_x="left", move_y="none")
        check("★ ② 连续两帧都看见 ⇒ 追过去（走位改成朝目标，盖掉了路线的 left）", cmd[0], "right")
        check_true("  ② 追的动作只打一条 INFO（不刷屏）", _logh.search("[锁定]"))

        # ③ 彻底看不见 ⇒ 守住最后位置继续出招；连续 3s 没再看见才算"怪物消失"
        st.monsters = []
        ck.tick(0.2)
        cmd = frame(st, move_x="left", move_y="none")
        check("  ③ 看不见目标 → 仍然站着（不恢复巡逻）", holding(st), True)
        check("★ ③ AOE 继续出招（'持续攻击'，不是干站着）", cmd[2], "attack")
        check("  ③ 走位被清零（守住最后位置）", cmd[0], "none")
        ck.tick(3.3)                                # > target_hold_s=3.0
        cmd = frame(st, move_x="left", move_y="none")
        check("★ ③ 连续 3s 没再看见 → 判「怪物已消失」、恢复巡逻", holding(st), False)
        check("  ③ 恢复巡逻后走位交回路线", cmd[0], "left")
        check_true("  ③ INFO 里说清了原因（不许静默）", _logh.search("目标已消失"),
                   detail=f"抓到 {len(_logh.msgs)} 条日志")

        # ④ 安全阀仍然生效：锁定再长也不会把角色钉死（出不了招 ⇒ 2.5s 放行）
        cfg2 = base_cfg(target_hold_s=30.0, stop_attack_clear_s=60.0)
        st2 = make_stub(cfg2, [mob(760, 400)], facing="left")   # 朝向反 ⇒ 一直出不了招
        frame(st2, move_x="left", move_y="none")
        check("  ④ 锁定期照常站住", holding(st2), True)
        ck.tick(3.0)
        frame(st2, move_x="left", move_y="none")
        check("★ ④ 锁定 30s 也拦不住 idle 安全阀（2.5s 出不了招 → 放行）",
              holding(st2), False)
finally:
    logging.getLogger().removeHandler(_logh)

# ── ⑫ P1：时间窗 + 「出过招」判据（2026-10-14）────────────────────────────
print("\n[12] P1 放行判据：空够 clear_s 秒 ∩ 出过招（帧数只是最小值）")
# 为什么要有秒数：帧数阈值是**采样率相关**的 —— 真机 29.4fps + every_n_frames=2 时
#   2 个检测帧 = 136ms，而 2026-10-13 沼泽地2 标定时只有 3.3fps ⇒ 600ms。
# 为什么要有"出过招"：**秒杀怪**的正解 —— 怪一刀就死，之后框里一直空着，
#   旧规则会白站 target_hold_s(3.0) 秒；反过来一次都没出过招 = "打不着"而非"打完了"。
_logh2 = LogCapture()
logging.getLogger().addHandler(_logh2)
try:
    with Clock(2000.0) as ck2:
        _c = base_cfg(stop_attack_clear_s=0.3, target_hold_s=0)
        _c["bot"] = {"attack": "aoe_skill"}
        st = make_stub(_c, [mob(760, 400)], facing="right")
        frame(st, move_x="right", move_y="none")
        check("  前置：停下这一帧就出过招", float(st.t_last_attack) >= float(st._stop_hold_t0), True)
        st.monsters = []
        frame(st, move_x="right", move_y="none")        # 空帧 #1
        frame(st, move_x="right", move_y="none")        # 空帧 #2（帧数够了、秒数没够）
        check("★ 空够 2 个检测帧但只过了 0.0s → 不判完（秒数没到）", holding(st), True)
        ck2.tick(0.31)
        cmd = frame(st, move_x="right", move_y="none")
        check("★ 空窗 ≥ 0.3s 且**出过招** → 判打完、走位交回路线",
              (holding(st), cmd[0]), (False, "right"))
        check_true("  INFO 说清了原因（出过招 + 空窗秒数）",
                   _logh2.search("出过招"), detail=f"抓到 {len(_logh2.msgs)} 条")

        # ② 一次都没出过招（怪在框里但打不着）⇒ **不判**"打完"，也不能把角色钉死
        ck2.tick(1.0)
        st2 = make_stub(base_cfg(stop_attack_clear_s=0.3, target_hold_s=0),
                        [mob(760, 400)], facing="left")   # directional + 朝向反 ⇒ 只转身
        frame(st2, move_x="left", move_y="none")
        check("  前置：确实一次都没出过招", float(st2.t_last_attack), 0.0)
        st2.monsters = []
        ck2.tick(0.5)
        frame(st2, move_x="left", move_y="none")
        ck2.tick(0.4)
        frame(st2, move_x="left", move_y="none")
        check("★ 空够秒数但**一次都没出过招** → 不判「打完」（那是打不着）",
              holding(st2), True)
        check_true("  并且留了可查的日志（不许静默）", _logh2.search("一次都没出过招"),
                   detail=f"抓到 {len(_logh2.msgs)} 条")
        ck2.tick(3.0)
        frame(st2, move_x="left", move_y="none")       # 安全阀是在**帧里**判的
        check("★ 但 idle 安全阀照旧放行（不会把角色钉死）", holding(st2), False)

        # ③ clear_s = 0 ⇒ 逐字退回老口径（只按检测帧数判）
        ck2.tick(1.0)
        _c3 = base_cfg(stop_attack_clear_s=0.0, target_hold_s=0)
        _c3["bot"] = {"attack": "aoe_skill"}
        st3 = make_stub(_c3, [mob(760, 400)], facing="right")
        frame(st3, move_x="right", move_y="none")
        st3.monsters = []
        frame(st3, move_x="right", move_y="none")
        check("  clear_s=0（老口径）：空 1 帧仍站着", holding(st3), True)
        cmd = frame(st3, move_x="right", move_y="none")
        check("★ clear_s=0：空 2 帧即放行", (holding(st3), cmd[0]), (False, "right"))

        # ④ ★★ 2026-10-14 第三次改：**锁定期也放行**（真机 log/MSBot_2026-10-14_11-42-16.log
        #    实证的漏洞 —— 当时 target_hold_s=3.0，日志里只有"目标已消失（连续 3s）"，
        #    停留时长清一色 3.1~4.2s：一刀秒掉的怪也白站 3 秒，"出过招"这条永远轮不到）。
        ck2.tick(1.0)
        _c4 = base_cfg(stop_attack_clear_s=0.3, target_hold_s=3.0)   # ← 锁定开着
        _c4["bot"] = {"attack": "aoe_skill"}
        st4 = make_stub(_c4, [mob(760, 400)], facing="right")
        frame(st4, move_x="right", move_y="none")
        check("  前置：锁定已建立 + 出过招", (float(st4._mob_lock_seen_t) > 0.0,
                                          float(st4.t_last_attack) >= float(st4._stop_hold_t0)),
              (True, True))
        st4.monsters = []
        frame(st4, move_x="right", move_y="none")       # 空帧 #1
        ck2.tick(0.31)
        cmd = frame(st4, move_x="right", move_y="none")  # 空帧 #2：秒数 + 帧数都够了
        check("★ 锁定期内「出过招 + 空够秒数」照样收手（不再白等 3s）",
              (holding(st4), cmd[0]), (False, "right"))
        check_true("  而且走的是「出过招」那条日志，不是锁定过期",
                   _logh2.search("出过招") and not _logh2.search("目标已消失"),
                   detail=f"抓到 {len(_logh2.msgs)} 条")

        # ⑤ ★★ 配套：出过招 + 框空够秒数，但**画面上还看得见怪** ⇒ 不收手（继续打/追）。
        #    没有这条，"出过招就收手"会把还活着的怪扔下就走（比锁定 3s 追人的老行为更差）。
        ck2.tick(1.0)
        _c5 = base_cfg(stop_attack_clear_s=0.3, target_hold_s=3.0)
        _c5["bot"] = {"attack": "aoe_skill"}
        st5 = make_stub(_c5, [mob(760, 400)], facing="right")
        frame(st5, move_x="right", move_y="none")
        st5.monsters = [mob(1160, 400)]                 # 走出攻击框、但还在画面里
        frame(st5, move_x="right", move_y="none")       # 空帧 #1
        ck2.tick(0.4)                                   # 秒数够了
        frame(st5, move_x="right", move_y="none")       # 空帧 #2
        check("★ 框空 + 出过招，但画面上还有怪 ⇒ **不**判打完（继续打/追）",
              holding(st5), True)
        ck2.tick(3.2)                                   # 超过 target_hold_s=3.0
        cmd = frame(st5, move_x="right", move_y="none")
        check("★ 一直没再看见 ⇒ 才由锁定过期放行（走位交回路线）",
              (holding(st5), cmd[0]), (False, "right"))
finally:
    logging.getLogger().removeHandler(_logh2)

# ── ⑬ P1：追怪「出现确认」─────────────────────────────────────────────────
print("\n[13] P1 追怪「出现确认」：只认到一帧的远怪不追（防单帧幻影目标）")
# 怪放左边、路线给 right ⇒ "追"与"不追"在 cmd_move_x 上是可区分的。
with Clock(3000.0) as ck3:
    _c = base_cfg(target_hold_s=0, approach_confirm_s=0.5)
    _c["bot"] = {"attack": "aoe_skill"}
    st = make_stub(_c, [mob(200, 400)], facing="right")
    cmd = frame(st, move_x="right", move_y="none")
    check("★ 只认到一帧 → 不追（走位仍是路线的 right）", (cmd[0], cmd[1]), ("right", "none"))
    ck3.tick(0.1)                                  # 下一帧（新时刻）⇒ 第二次采样
    cmd = frame(st, move_x="right", move_y="none")
    check("★ 连续两次采样都看见 → 追过去（改朝左）", (cmd[0], cmd[1]), ("left", "none"))
    ck3.tick(0.6)                                  # 隔太久 ⇒ 计数重来
    cmd = frame(st, move_x="right", move_y="none")
    check("  间隔 > approach_confirm_s → 计数清零，重新确认", (cmd[0], cmd[1]),
          ("right", "none"))
    # ⚠️ 0 = **关掉**这道防抖（与 `target_hold_s: 0` 同一个约定）⇒ 看见一帧就追。
    #    这条必须钉住：夹具们就是靠它保住"老口径"的（见 verify_mob_sight [4]）。
    ck3.tick(0.1)
    _c0 = base_cfg(target_hold_s=0, approach_confirm_s=0.0)
    _c0["bot"] = {"attack": "aoe_skill"}
    st0 = make_stub(_c0, [mob(200, 400)], facing="right")
    check("★ approach_confirm_s=0（关）⇒ 第一帧就追过去",
          frame(st0, move_x="right", move_y="none")[0], "left")

# ── ⑭ P1：方向最小保持（治左右抖动）───────────────────────────────────────
print("\n[14] P1 追怪方向最小保持：老方向那侧还有怪时不许掉头（治真机抖动）")


def _flip_seq(dir_min, ck):
    '''跑一遍"右边更近 → 左边更近"的序列，返回第 2 帧与第 3 帧实际发出的方向。'''
    _c = base_cfg(target_hold_s=0, approach_confirm_s=0.5, approach_dir_min_s=dir_min)
    _c["bot"] = {"attack": "aoe_skill"}
    st = make_stub(_c, [mob(1100, 400), mob(200, 400)], facing="none")
    frame(st, move_x="none", move_y="none")        # 确认 #1（右 400 < 左 500）
    ck.tick(0.1)
    _first = frame(st, move_x="none", move_y="none")[0]     # 确认 #2 ⇒ 追右
    st.monsters = [mob(1400, 400), mob(250, 400)]  # 左 450 < 右 700 ⇒ 候选方向变左
    ck.tick(0.1)
    _third = frame(st, move_x="none", move_y="none")[0]
    return _first, _third


with Clock(4000.0) as ck4:
    _first, _third = _flip_seq(0.4, ck4)
    check("  第 1 次确认后追右（右边更近）", _first, "right")
    check("★ 下一帧最近的在左边，但右侧还有怪、还没到最小保持 ⇒ **仍追右**",
          _third, "right")
    ck4.tick(1.0)
    _c = base_cfg(target_hold_s=0, approach_confirm_s=0.5, approach_dir_min_s=0.4)
    _c["bot"] = {"attack": "aoe_skill"}
    st = make_stub(_c, [mob(1100, 400), mob(200, 400)], facing="none")
    frame(st, move_x="none", move_y="none")
    ck4.tick(0.1)
    frame(st, move_x="none", move_y="none")        # 建立 right
    st.monsters = [mob(1400, 400), mob(250, 400)]
    ck4.tick(0.1)
    check("  保持窗口内仍追右", frame(st, move_x="none", move_y="none")[0], "right")
    ck4.tick(0.45)                                 # > 最小保持(0.4)、≤ 出现确认(0.5)
    cmd = frame(st, move_x="none", move_y="none")
    check("★ 超过最小保持 ⇒ 允许掉头追左边那只", cmd[0], "left")
    st.monsters = [mob(1400, 400)]                 # 老方向（左）那侧一只怪都没有
    ck4.tick(0.05)
    cmd = frame(st, move_x="none", move_y="none")
    check("★ 老方向那侧一只怪都没有 ⇒ 立即掉头（不为保持方向背对目标）",
          cmd[0], "right")
    ck4.tick(1.0)
    _first2, _third2 = _flip_seq(0.0, ck4)
    check("★ 变异测试：approach_dir_min_s=0 ⇒ 同样场景立刻改追左（证明保持来自本键）",
          (_first2, _third2), ("right", "left"))

# ── ⑮ P2：交战期间只扫攻击框 ──────────────────────────────────────────────
print("\n[15] P2 交战期间只扫「攻击框 + search_box_margin」（engaged_scan）")
# 实测（_probe_detect_cost.py，帧 800×1368）：全感知框 660×264 = 17.1ms、
#   攻击框 340×140 = 7.2ms（-58%）；匹配耗时 ∝ 扫描面积，所以收紧 = 直接省时间。
_FH, _FW = 600, 800


def _tight_expect(st, margin=50):
    _ab = [st.get_attack_range(is_left=True)]
    if st.cfg["bot"]["attack"] == "directional":
        _ab.append(st.get_attack_range(is_left=False))
    return (max(0, min(b[0] for b in _ab) - margin),
            max(0, min(b[1] for b in _ab) - margin),
            min(_FW, max(b[2] for b in _ab) + margin),
            min(_FH, max(b[3] for b in _ab) + margin))


_c = base_cfg(engaged_scan=True, detect_range_scale=2.0)
_c["bot"] = {"attack": "aoe_skill"}
st = make_stub(_c)
st._stop_hold = False
_wide = st.get_search_box()
st._stop_hold = True
_tight = st.get_search_box()
check("  巡逻态：全感知框（(200,130,1200,670) 夹到帧内）", _wide, (200, 130, _FW, _FH))
check("★ 交战态：收紧到「攻击框 + margin」", _tight, _tight_expect(st))
check_true("★ 面积确实小了（这就是省下的那 58%）",
           (_tight[2] - _tight[0]) * (_tight[3] - _tight[1])
           < (_wide[2] - _wide[0]) * (_wide[3] - _wide[1]), True)
_c["monster_detect"]["engaged_scan"] = False
check("  engaged_scan=false ⇒ 交战态也扫全感知框（逐字退回老行为）",
      st.get_search_box(), _wide)
_c["monster_detect"]["engaged_scan"] = True
_c["monster_detect"]["stale_pos_full_frame_after"] = 10
st.nametag_miss_streak = 10
check("★ 位置失效优先：交战中也放大到整帧（「框错了」比「框大了」更糟）",
      st.get_search_box(), (0, 0, _FW, _FH))
st.nametag_miss_streak = 0
_c["monster_detect"]["search_box_margin"] = 0
check("  margin=0 ⇒ 收紧到正好是攻击框本身", st.get_search_box(),
      _tight_expect(st, margin=0))
_c["monster_detect"]["search_box_margin"] = 50
_c2 = base_cfg(engaged_scan=True)                  # 默认 directional ⇒ 左右两个框取并集
_c2["bot"] = {"attack": "directional"}
st2 = make_stub(_c2)
st2._stop_hold = True
check("★ directional：左右两个攻击框取**并集**再外扩", st2.get_search_box(),
      _tight_expect(st2))

_src3 = inspect.getsource(MapleStoryAutoBot.update_cmd_by_mob_detection)
# ⚠️ 先剥掉注释行再数：源码注释里会**引用**这个函数名（本文件也踩过同样的坑：
#    `verify_hotkey_f12_close.py` 的 docstring 命中判据）。
_src3_code = "\n".join(ln for ln in _src3.splitlines()
                       if not ln.strip().startswith("#"))
check("★ 白盒：两个调用点都走 _pick_approach_target（挑目标的规则只有一处）",
      _src3_code.count("_pick_approach_target("), 2)
check("  老的「手抄取最近」循环已删掉（不会再分叉两套规则）",
      "for _m2 in _far:" in _src3_code or "for _m in _far:" in _src3_code, False)

# ── ⑯ 「判定打完」那行日志必须印出**真实的**空检测帧数 ─────────────────────
print("\n[16] 日志留证：判定打完那行要印出真实的空检测帧数（不许是 0）")
# 病根（2026-10-14 真机 log/MSBot_2026-10-14_12-05-06.log）：76 条「判定打完」里 61 条
#   印着「（0 个检测帧）」⇒ 看起来像"一个检测帧都没空就判完了"（会让人怀疑放行太早）。
#   实际是清零在前、打印在后 —— **日志把自己的证据抹掉了**，行为本身没错。
_logh3 = LogCapture()
logging.getLogger().addHandler(_logh3)
try:
    with Clock(4000.0) as ck3:
        _c6 = base_cfg(stop_attack_clear_s=0.3, target_hold_s=3.0)
        _c6["bot"] = {"attack": "aoe_skill"}
        st6 = make_stub(_c6, [mob(760, 400)], facing="right")
        frame(st6, move_x="right", move_y="none")       # 站定 + 出招（一次就够）
        st6.monsters = []
        frame(st6, move_x="right", move_y="none")       # 空检测帧 #1
        check("  前置：空 1 帧还不够（帧数没到）", holding(st6), True)
        ck3.tick(0.31)
        cmd = frame(st6, move_x="right", move_y="none")  # 空检测帧 #2
        check("★ 空 2 个检测帧 + 秒数够 + 出过招 → 判打完", (holding(st6), cmd[0]),
              (False, "right"))
        _done = [m for m in _logh3.msgs if "判定打完" in m]
        check_true("  有「判定打完」那行", len(_done) == 1, detail=f"抓到 {_done}")
        _m = re.search(r"（(\d+) 个检测帧）", _done[0]) if _done else None
        check_true("  ★ 印出的空检测帧数是实数（≥ clear_frames=2）",
                   bool(_m) and int(_m.group(1)) >= 2,
                   detail=f"印的是 {_m.group(1) if _m else '没匹配到'}")
        check_true("  ★ 不许再出现「（0 个检测帧）」",
                   bool(_done) and "（0 个检测帧）" not in _done[0])
finally:
    logging.getLogger().removeHandler(_logh3)

_src16 = "\n".join(ln for ln in inspect.getsource(
    MapleStoryAutoBot.update_cmd_by_mob_detection).splitlines()
    if not ln.strip().startswith("#"))
_i16 = _src16.index("_empty_frames = int(")
check_true("  白盒：先留证（_empty_frames = ...）", _i16 > 0)
check_true("  白盒：留证**之后**才清零（_stop_hold_empty = 0）",
           _src16.index("self._stop_hold_empty = 0", _i16) > _i16)
check_true("  白盒：日志里印的是留证变量，不是被清零的字段",
           "（{_empty_frames} 个检测帧）" in _src16)

print()
print("=" * 72)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
