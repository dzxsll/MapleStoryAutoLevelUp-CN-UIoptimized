# -*- coding: utf-8 -*-
'''「[出招] 心跳」自检（2026-10-14 加）。

背景（用户真机反馈 m06529）：
    「攻击就是很慢，或者说怪就在攻击范围内了，但是就是不攻击」
    真机 13:29 那轮（8.5 分钟）量化出五个原因：
      · `aoe_skill.cooldown: 0.77`（引擎默认 0.05 只是防重复按键）⇒ 硬限速 1.3 次/秒；
      · 攻击框被改小成 340×140（默认 400×170）；
      · 索敌框 ±330×±132 ≫ 攻击框 ±170×±70 ⇒ 画面上"在框里"的怪多数打不到；
      · 绳/梯让路 + 路线 up 指令期间，怪逻辑完全不参与；
      · `attack` 指令只存在**一帧**（下一帧就被残留清理清掉）⇒ 按指令采样
        永远抓不到出招，所以"出招频率"只能靠**计数**，不能靠采样。

做法：`MapleStoryAutoBot._note_attack()` 在**出招那一行**上记一笔（纯观测），
      `_log_attack_heartbeat()` 紧跟 `[运行状态]` 的 5 秒节拍把汇总打出来。

本文件钉五件事：
  【1】计数：次数 / 间隔 min·avg·max / 出招时攻击框内的怪数区间
  【2】间隔口径：第一次出招没有"上一次"、跨"停下打怪/回正/爬梯"的巨大间隔
        （≥30s）都不许进平均 —— 否则平均间隔会被抹平成一个好看但没用的数字
  【3】心跳文案：必须带 设定冷却 / 框内怪数；冷却 ≥0.3s 要显式点出"它就是限速器"，
        否则给"时间花在没怪/走过去/爬梯上"的结论（两种病因不能混成一句"很慢"）
  【4】接线白盒：4 处 `cmd_action = "attack"` 都紧跟计数；心跳紧跟 5 秒节拍；
        数字在 `finally` 里清零（含失败路径，否则失败一次就永远重复同一批数字）
  【5】纯观测：不碰指令、不改 `t_last_attack`、异常绝不外泄到主循环
'''
import inspect
import logging
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402

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


def make_stub(attack_mode="aoe_skill", aoe_cd=0.77, dir_cd=0.9):
    '''只造"心跳要用的那几个字段"的桩：不走 __init__（与其它 verify_* 同款）。'''
    s = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    s.cfg = {
        "bot": {"attack": attack_mode},
        "aoe_skill": ({"cooldown": aoe_cd} if aoe_cd is not None else {}),
        "directional_attack": ({"cooldown": dir_cd} if dir_cd is not None else {}),
    }
    s.t_last_attack = 0.0
    # ⚠️ 桩必须把 __init__ 里那 7 个计数器一并建出来（教训 2026-10-14 踩过两次）：
    #    引擎里它们由 __init__ 建、心跳只读；桩不建的话读它们就是 AttributeError，
    #    看起来像"某个不相关的自检坏了"。
    s._atk_n = 0
    s._atk_gap_n = 0
    s._atk_gap_sum = 0.0
    s._atk_gap_min = None
    s._atk_gap_max = 0.0
    s._atk_eng_min = None
    s._atk_eng_max = 0
    return s


def captures(fn):
    '''跑 fn()，把 root logger 的 INFO 消息捞回来。'''
    cap = LogCapture()
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(cap)
    try:
        fn()
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
    return cap.msgs


def one_msg(msgs, needle):
    for m in msgs:
        if needle in m:
            return m
    return ""


_SRC = inspect.getsource(MapleStoryAutoBot)
_NOTE = inspect.getsource(MapleStoryAutoBot._note_attack)
_HB = inspect.getsource(MapleStoryAutoBot._log_attack_heartbeat)

print("=" * 72)
print("[1] 计数：次数 / 间隔 / 出招时框内怪数")
print("=" * 72)
s = make_stub()
check("第一次出招：计数 +1", (s._note_attack(1), s._atk_n)[1], 1)
check("第一次出招：没有'上一次' ⇒ 间隔样本 0", s._atk_gap_n, 0)
check("第一次出招：框内怪数区间 = 1~1",
      (s._atk_eng_min, s._atk_eng_max), (1, 1))

s.t_last_attack = time.time() - 0.5
s._note_attack(3)
check("第二次出招：计数 +1", s._atk_n, 2)
check("第二次出招：间隔样本 +1", s._atk_gap_n, 1)
check_true("第二次出招：间隔 ≈0.5s 被记下", abs(s._atk_gap_sum - 0.5) < 0.05,
           f"sum={s._atk_gap_sum:.3f}")
check("框内怪数区间扩到 1~3", (s._atk_eng_min, s._atk_eng_max), (1, 3))

s.t_last_attack = time.time() - 0.2
s._note_attack(0)
check_true("第三次出招：最小间隔被刷新到 ≈0.2s", abs(s._atk_gap_min - 0.2) < 0.05,
           f"min={s._atk_gap_min:.3f}")
check_true("第三次出招：最大间隔仍 ≈0.5s", abs(s._atk_gap_max - 0.5) < 0.05,
           f"max={s._atk_gap_max:.3f}")
check("框内怪数区间变成 0~3", (s._atk_eng_min, s._atk_eng_max), (0, 3))

print()
print("=" * 72)
print("[2] 间隔口径：第一次 / 跨场景的巨大间隔都不许进平均")
print("=" * 72)
s = make_stub()
s._note_attack(1)
_gn0 = s._atk_gap_n
s.t_last_attack = time.time() - 100.0          # 跨了"停下打怪/回正/爬梯"
s._note_attack(1)
check("100s 的巨大间隔：不进样本", s._atk_gap_n, _gn0)
check("100s 的巨大间隔：但出招次数照样 +1", s._atk_n, 2)

s2 = make_stub()
s2._note_attack(1)
s2.t_last_attack = 0.0                          # 本轮第一次出招（t_last_attack 未初始化）
_gn1 = s2._atk_gap_n
s2._note_attack(1)
check("t_last_attack=0：不算间隔（会话第一刀）", s2._atk_gap_n, _gn1)

print()
print("=" * 72)
print("[3] 心跳文案：出招次数 / 间隔 / 设定冷却 / 框内怪数 / 限速提示")
print("=" * 72)
s = make_stub()
_msgs = captures(s._log_attack_heartbeat)
_msg = one_msg(_msgs, "[出招]")
check_true("零出招也说话（否则'没有行'等于没观测）", bool(_msg), repr(_msgs))
check_true("零出招：写明 0 次", "近5秒出招 0 次" in _msg)
check_true("零出招：间隔写 '没有连续两次出招' 而不是 0.00s",
           "没有连续两次出招" in _msg)
check_true("零出招：仍写设定冷却（0.77s）", "设定冷却 0.77s" in _msg)
check_true("零出招：框内怪数写 '-'（没数据 ≠ 0 只）", "攻击框内 - 个目标" in _msg)
check_true("零出招：不给'限速器'结论", "限速器" not in _msg)

s = make_stub()
s._atk_n, s._atk_gap_n, s._atk_gap_sum = 3, 2, 1.0
s._atk_gap_min, s._atk_gap_max = 0.4, 0.6
s._atk_eng_min, s._atk_eng_max = 1, 2
_msg = one_msg(captures(s._log_attack_heartbeat), "[出招]")
check_true("有出招：写次数", "近5秒出招 3 次" in _msg)
check_true("有出招：写 平均/最小/最大 间隔",
           "间隔 平均0.50s/最小0.40s/最大0.60s" in _msg)
check_true("有出招：写框内怪数区间", "攻击框内 1~2 个目标" in _msg)
check_true("冷却 0.77s ⇒ 明确点出'它就是限速器'", "就是限速器" in _msg)
check_true("限速器提示里给出上限估算（5/0.77≈6.5 次）", "6.5 次" in _msg)

s = make_stub(aoe_cd=0.05)
s._atk_n, s._atk_gap_n, s._atk_gap_sum = 40, 39, 19.5
s._atk_gap_min, s._atk_gap_max = 0.4, 0.6
s._atk_eng_min, s._atk_eng_max = 1, 3
_msg = one_msg(captures(s._log_attack_heartbeat), "[出招]")
check_true("冷却 0.05s 但平均 0.5s ⇒ 结论是'没摸到冷却上限'",
           "没摸到冷却上限" in _msg)
check_true("冷却 0.05s：不再说'限速器'", "限速器" not in _msg)

s = make_stub(aoe_cd=0.05)
s._atk_n, s._atk_gap_n, s._atk_gap_sum = 100, 99, 2.97
s._atk_gap_min, s._atk_gap_max = 0.02, 0.04
_msg = one_msg(captures(s._log_attack_heartbeat), "[出招]")
check_true("冷却 0.05s 且平均 0.03s：两句结论都不给（正常高速出招）",
           "限速器" not in _msg and "没摸到冷却上限" not in _msg)

s = make_stub(attack_mode="directional", dir_cd=0.9)
_msg = one_msg(captures(s._log_attack_heartbeat), "[出招]")
check_true("directional 模式：读 directional_attack.cooldown",
           "设定冷却 0.90s" in _msg)

s = make_stub()
s.cfg = {"bot": {"attack": "aoe_skill"}}         # 配置段残缺
_msg = one_msg(captures(s._log_attack_heartbeat), "[出招]")
check_true("配置残缺：不抛异常，冷却按 0.00s 写", "设定冷却 0.00s" in _msg)

s = make_stub()
s._atk_n, s._atk_gap_n = 3, "x"                  # int("x") 会炸
_msgs = captures(s._log_attack_heartbeat)
check_true("统计失败：打一行'心跳统计失败'（失败不静默）",
           any("心跳统计失败" in m for m in _msgs), repr(_msgs))
check("统计失败：计数照样清零（否则永远重复同一批数字）", s._atk_n, 0)
check("统计失败：间隔计数也清零", s._atk_gap_n, 0)

s = make_stub()
s._atk_n, s._atk_gap_n, s._atk_gap_sum = 2, 1, 0.5
s._atk_gap_min, s._atk_gap_max, s._atk_eng_min, s._atk_eng_max = 0.5, 0.5, 1, 1
captures(s._log_attack_heartbeat)
check("正常路径：finally 清零 次数", s._atk_n, 0)
check("正常路径：finally 清零 间隔样本", s._atk_gap_n, 0)
check("正常路径：finally 清零 间隔和", s._atk_gap_sum, 0.0)
check("正常路径：finally 清零 区间", (s._atk_gap_min, s._atk_eng_min), (None, None))

print()
print("=" * 72)
print("[4] 接线白盒：4 处出招都计数；心跳紧跟 [运行状态] 的 5 秒节拍")
print("=" * 72)
check("源码里 `_note_attack(` 出现 5 次（1 个定义 + 4 处调用）",
      _SRC.count("_note_attack("), 5)
_pos = []
_i = _SRC.find('self.cmd_action = "attack"')
while _i >= 0:
    _pos.append(_i)
    _i = _SRC.find('self.cmd_action = "attack"', _i + 1)
check("源码里 `cmd_action = \"attack\"` 出现 4 次", len(_pos), 4)
for _k, _p in enumerate(_pos, 1):
    check_true(f"第 {_k} 处出招后面 220 字符内紧跟计数",
               "_note_attack(" in _SRC[_p:_p + 220])
check_true("心跳调用点缩进与 [运行状态] 同级（同一个 5 秒 if 里）",
           "\n            self._log_attack_heartbeat()" in _SRC)
check_true("心跳在 [运行状态] **之后**",
           _SRC.index("[运行状态] 帧") < _SRC.index("self._log_attack_heartbeat()"))
check_true("两者距离很近（同一个 5 秒节拍）",
           _SRC.index("self._log_attack_heartbeat()") - _SRC.index("[运行状态] 帧") < 700,
           f"gap={_SRC.index('self._log_attack_heartbeat()') - _SRC.index('[运行状态] 帧')}")
for _f in ("_atk_n = 0", "_atk_gap_n = 0", "_atk_gap_sum = 0.0",
           "_atk_gap_min = None", "_atk_gap_max = 0.0",
           "_atk_eng_min = None", "_atk_eng_max = 0"):
    check_true(f"__init__ 初始化 {_f}", _f in _SRC)
check_true("心跳被 try/except 包住（异常不许外泄）", "except Exception" in _HB)
check_true("finally 里清零（失败路径也清）", "finally:" in _HB
           and "_atk_n = 0" in _HB.split("finally:")[-1])
check_true("心跳文案标识 `[出招]` 固定", '"[出招] 近5秒出招' in _HB)
check_true("攻击框标签写清'打得到'", "攻击框（打得到）" in _SRC)
check_true("攻击框标签顺带解释蓝框（索敌框）", "蓝框=索敌框" in _SRC)
check_true("索敌框仍用 cv2.rectangle（不加中文标签，避免 PIL 拖帧）",
           "cv2.rectangle(self.img_frame_debug" in _SRC)
check_true("`[名字定位·分数]` 不再断言'非常接近阈值'（真因是名牌被挡）",
           "非常接近阈值" not in _SRC)
check_true("`[名字定位·分数]` 改说'也可能被宠物名/称号压住'",
           "名牌被宠物名/称号压住" in _SRC)

print()
print("=" * 72)
print("[5] 纯观测：不碰指令 / 不改出招节流 / 异常不外泄")
print("=" * 72)
check_true("_note_attack 不写指令（无 self.cmd_）", "self.cmd_" not in _NOTE)
check_true("_note_attack 不改 t_last_attack", "self.t_last_attack =" not in _NOTE)
check_true("_log_attack_heartbeat 不写指令", "self.cmd_" not in _HB)
check_true("_note_attack 被 try/except 包住", "except Exception" in _NOTE)

s = make_stub()
try:
    s._note_attack(None)
    _ok = True
except Exception:                                         # noqa: BLE001
    _ok = False
check_true("engage_n=None 不炸", _ok)

s = make_stub()
s.t_last_attack = "abc"                                   # float("abc") 会炸
_n_before = s._atk_n
try:
    s._note_attack(1)
    _ok = True
except Exception:                                         # noqa: BLE001
    _ok = False
check_true("t_last_attack 是垃圾值也不炸", _ok)
check("垃圾值：宁可不记这一笔，也不许把主循环带崩", s._atk_n, _n_before)

s = make_stub()
try:
    s._log_attack_heartbeat()
    _ok = True
except Exception:                                         # noqa: BLE001
    _ok = False
check_true("心跳本身不抛（桩只有最小字段）", _ok)

print()
print("=" * 72)
print("[6] [开火预算]（2026-10-14 加，用户 m10830：把'人眼的实时反应'拆成低频确认流程）")
print("=" * 72)
_FB = inspect.getsource(MapleStoryAutoBot._log_fire_budget)
_NL = inspect.getsource(MapleStoryAutoBot._budget_note_lost)

# 6.1 没数据也必须说话（"没有行"不等于"没问题"）
_s = make_stub()
_msgs = captures(_s._log_fire_budget)
_m = one_msg(_msgs, "[开火预算]")
check_true("没数据也打一行（否则没观测 = 看不出问题）", bool(_m), repr(_msgs))
check_true("没数据：覆盖率写 0%", "覆盖率 0%" in _m, _m)
check_true("没数据：三段写 '-' 且带 n=0（没数据 ≠ 0 秒）",
           "可见→确认 -(n=0)" in _m and "进框→出刀 -(n=0)" in _m, _m)
check_true("没数据：没打成的账写 '-'", "没打成的账：-" in _m, _m)

# 6.2 覆盖率算得对（近 60s 内"距上次出招 ≤1s"的累计时间占比）
_s = make_stub()
_now = time.time()
_s._budget_atk_ts = [_now - 3.0, _now - 2.5, _now - 2.0, _now - 1.5]
_s._budget_see2ok = [0.04]
_s._budget_ok2box = [1.2]
_s._budget_box2hit = [0.03, 0.05]
_s._budget_lost = {"高差(帧)": 3, "未确认(次)": 5}
_m = one_msg(captures(_s._log_fire_budget), "[开火预算]")
check_true("覆盖率：3 个 0.5s 间隔 + 末尾 1.0s = 2.5s/60s ⇒ 4%",
           "覆盖率 4%" in _m, _m)
check_true("可见→确认 取中位（0.04s）", "可见→确认 0.04s(n=1)" in _m, _m)
check_true("确认→进框 取中位（1.20s）", "确认→进框 1.20s(n=1)" in _m, _m)
check_true("进框→出刀 取中位（两个样本取第 2 个 = 0.05s）",
           "进框→出刀 0.05s(n=2)" in _m, _m)
check_true("没打成的账：逐项带次数", "未确认(次) 5" in _m and "高差(帧) 3" in _m, _m)

# 6.3 算完清零（失败路径也清），否则下一次会重复报同一批数字
_s = make_stub()
_s._budget_atk_ts = [time.time()]
_s._budget_see2ok = [0.04]
_s._budget_lost = {"未确认(次)": 1}
captures(_s._log_fire_budget)
check("清零：三段样本表清空", (_s._budget_see2ok, _s._budget_ok2box, _s._budget_box2hit),
      ([], [], []))
check("清零：没打成的账清空", _s._budget_lost, {})
_m2 = one_msg(captures(_s._log_fire_budget), "[开火预算]")
check_true("第二次调用：账目已是空的", "没打成的账：-" in _m2, _m2)

# 6.4 跟 detect_log 同一个开关
_s = make_stub()
_s.cfg["monster_detect"] = {"detect_log": False}
check("detect_log=False：一行都不打", captures(_s._log_fire_budget), [])

# 6.5 垃圾数据：不炸、但只吼一次（不许每 5 秒刷屏，也不许静默）
_s = make_stub()
_s._budget_atk_ts = ["这不是数字"]
_msgs = captures(_s._log_fire_budget)
check_true("垃圾数据：打一行「统计失败」（失败不静默）",
           any("统计失败" in _x for _x in _msgs), repr(_msgs))
_msgs2 = captures(_s._log_fire_budget)
check_true("垃圾数据：第二次不再重复（只吼一次）",
           not any("统计失败" in _x for _x in _msgs2), repr(_msgs2))

# 6.6 接线与纯观测
check("源码里 `_log_fire_budget` 定义 1 次", _SRC.count("def _log_fire_budget(self):"), 1)
check("源码里 `_budget_note_lost` 定义 1 次", _SRC.count("def _budget_note_lost(self, why):"), 1)
check("心跳里调用 `_log_fire_budget()` 1 次", _SRC.count("self._log_fire_budget()"), 1)
check_true("调用点与 [出招] 心跳同级缩进（同一个 5 秒 if 里）",
           "\n            self._log_fire_budget()" in _SRC)
check_true("调用点紧跟 `_log_attack_heartbeat()` 之后",
           _SRC.index("self._log_attack_heartbeat()")
           < _SRC.index("self._log_fire_budget()"))
check_true("调用点在 [运行状态] 同一个 5 秒节拍里（距离够近）",
           _SRC.index("self._log_fire_budget()") - _SRC.index("[运行状态] 帧") < 900,
           f"gap={_SRC.index('self._log_fire_budget()') - _SRC.index('[运行状态] 帧')}")
for _why in ("被让路(帧)", "爬绳放弃(次)", "未确认(次)", "高差(帧)"):
    check_true(f"记账点齐全：{_why}", f'_budget_note_lost("{_why}")' in _SRC)
check_true("_log_fire_budget 不写指令（纯观测）", "self.cmd_" not in _FB)
check_true("_log_fire_budget 有 getattr 兜底（桩不走 __init__ 也不炸）",
           "_budget_atk_ts\", None)" in _FB or "getattr(self, \"_budget_atk_ts\"" in _FB)
check_true("_log_fire_budget 被 try/except 包住", "except Exception" in _FB)
check_true("finally 里清账（失败路径也清）", "finally:" in _FB
           and "_budget_lost = {}" in _FB.split("finally:")[-1])
check_true("_budget_note_lost 自己建 dict（不在 __init__ 也能用）",
           "isinstance(_d, dict)" in _NL)
for _f in ("self._budget_atk_ts = []", "self._budget_see2ok = []",
           "self._budget_ok2box = []", "self._budget_box2hit = []",
           "self._budget_lost = {}", "self._budget_warned = False"):
    check_true(f"__init__ 初始化 {_f}", _f in _SRC)

print()
print("=" * 72)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
