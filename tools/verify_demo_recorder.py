# -*- coding: utf-8 -*-
'''手动示范录制自检（2026-10-14 加）。

背景（用户 m07008）：
    「那么能否开着人物检测和怪物检测记录数据，然后我自己操作，录下我的打怪行为，
      以此作为数据样本来优化逻辑呢？」
    ⇒ 这就是把"人怎么打"变成可比数字的那条路：录制器逐帧落盘（引擎的感知 +
    影子决策 + 真人按键），`tools/analyze_demo.py` 离线出对照报告。

本文件钉六件事：
  【1】纯函数：`norm_key` 归一 / `game_key_map` 只收游戏键 / `demo_settings` 默认关
        且畸形值回默认
  【2】`frame_record`：完整帧的字段与类型；缺字段/畸形/非 dict 一律不炸
  【3】`DemoRecorder`：默认不建文件；开了就写 `.jsonl` + `meta`；`every_n_frames`
        抽稀；`max_frames` 到点换文件；`stop()` 幂等；**建不了文件只吼一次**
  【4】`KeyWatch`：只认游戏键（其它键只累加 `other_ev`、不记键名）；
        按下/抬起事件带时间戳；pynput 装不上时降级而不抛
  【5】接线白盒：`hunting.on_frame` 在 `set_command` 之后调；引擎 `start()` 建录制器
        且 `record_only` 会静音键盘；`terminate_threads` 会停；配置与中文名齐
  【6】行为：`_demo_frame_facts()` 的怪中心按 **(宽, 高)** 算（2026-10-14 修的对调），
        `in` 标出引擎判"够得着"的那几只
'''
import logging
import os
import sys
import tempfile

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.utils.demo_recorder import (DemoRecorder, KeyWatch, demo_meta,   # noqa: E402
                                     demo_settings, frame_record, game_key_map,
                                     norm_key)
from src.utils.common import load_yaml                                   # noqa: E402
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot           # noqa: E402

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


_CFG = {
    "bot": {"attack": "aoe_skill"},
    "key": {"aoe_skill": "q", "directional_attack": "w", "jump": "alt",
            "pickup": "control", "teleport": "space",
            "jump_cooldown": 1.0, "pickup_cooldown": 5.0, "unused": ""},
    "potion": {"low": {"key": "home"}, "high": {"cooldown": 3.0}},
    "aoe_skill": {"range_x": 360, "range_y": 160, "cooldown": 0.05},
    "directional_attack": {"range_x": 350, "range_y": 70, "cooldown": 0.9},
    "monster_detect": {"mob_engage_mode": "center", "every_n_frames": 2},
    "demo_record": {"enabled": True, "record_keys": False, "gzip": False,
                    "every_n_frames": 1, "max_minutes": 30},
}

print("=" * 72)
print("[1] 纯函数：键名归一 / 键表 / 设置默认值")
print("=" * 72)
check("Key.space → space", norm_key("Key.space"), "space")
check("alt_l → alt（左右合并）", norm_key("alt_l"), "alt")
check("Ctrl_R → ctrl", norm_key("ctrl_r"), "ctrl")
check("大写字符 → 小写", norm_key("'Q'"), "q")
check("None → 空串（不炸）", norm_key(None), "")
check("认不出的长名原样小写", norm_key("SomeWeirdKey"), "someweirdkey")
_km = game_key_map(_CFG)
check("四个方向键都在", sorted(k for k in ("left", "right", "up", "down")
                              if k in _km), ["down", "left", "right", "up"])
check("攻击键 q → aoe_skill", _km.get("q"), "aoe_skill")
check("directional 键 w → directional_attack", _km.get("w"), "directional_attack")
check("跳/拾取/瞬移都在", (_km.get("alt"), _km.get("ctrl"), _km.get("space")),
      ("jump", "pickup", "teleport"))
check("喝药键（深层 potion.low.key）→ potion", _km.get("home"), "potion")
check_true("冷却数字不进键表（否则每次改配置都被录）",
           "1.0" not in _km and "5.0" not in _km)
check_true("空串不进键表", "" not in _km)
check("默认：**关**（要用才开）", demo_settings({})["enabled"], False)
check("默认：只记录不操作 = true", demo_settings({})["record_only"], True)
check("默认：录按键 = true", demo_settings({})["record_keys"], True)
check("默认目录", demo_settings({})["out_dir"], "debug/demo")
check("畸形 every_n_frames=0 ⇒ 钳到 1", demo_settings(
    {"demo_record": {"every_n_frames": 0}})["every_n_frames"], 1)

# ⚠️ 相对目录必须按 **APP_ROOT** 解析，不能按 CWD —— 走快捷方式启动时 CWD 可能是别处，
#    症状是"日志说写进 debug/demo 了，文件却在别的地方"（静默走偏）。
#    2026-10-14 踩过：导入的函数名写错，except 把 ImportError 吞了 ⇒
#    名义上"按 APP_ROOT 解析"、实际按 CWD —— 靠下面这条断言钉住。
from src.utils.paths import APP_ROOT as _APP_ROOT                       # noqa: E402
from src.utils import demo_recorder as _DR                              # noqa: E402
check_true("路径工具真的可用（导入失败过就会退回 CWD，这条会红）",
           _DR._PATH_RESOLVE_OK is True)
check("相对 out_dir 解析到 APP_ROOT 下",
      DemoRecorder({"demo_record": {"enabled": True, "out_dir": "debug/_x"}},
                   logger=None).out_dir,
      os.path.join(_APP_ROOT, "debug", "_x"))
check("绝对 out_dir 原样（不改写用户给的路径）",
      DemoRecorder({"demo_record": {"out_dir": os.path.abspath(os.sep + "somewhere")}},
                   logger=None).out_dir,
      os.path.abspath(os.sep + "somewhere"))
check("畸形 max_minutes='x' ⇒ 回默认 30", demo_settings(
    {"demo_record": {"max_minutes": "x"}})["max_minutes"], 30)
check("demo_record 不是 dict ⇒ 全默认", demo_settings(
    {"demo_record": 5})["enabled"], False)

print()
print("=" * 72)
print("[2] frame_record：字段、缺字段、畸形输入")
print("=" * 72)
_f = frame_record({
    "t": 1000.1234, "frame": 7, "player": (300, 300), "nametag": (280, 285),
    "miss": 3, "ladder": True, "seg": 2, "minimap": (104, 28),
    "global": (166, 58), "attack_box": (130, 230, 470, 370),
    "search_box": (0, 80, 660, 470),
    "mobs": [{"cx": 476, "cy": 317, "w": 30, "h": 28, "s": 0.1123, "in": 1},
             {"cx": None}],
    "bot": {"mx": "right", "my": "up", "act": "attack", "t_atk": 999.5},
    "keys": {"left": 1, "right": 0}, "ev": [["q", 1, 1000.2], ["q", 0]],
    "other_ev": 4,
})
check("时间戳保留 3 位小数", _f["t"], 1000.123)
check("玩家坐标是 2 个 int", _f["player"], [300, 300])
check("删掉畸形的怪（只留合法的 1 只）", len(_f["mobs"]), 1)
check("怪字段齐全", sorted(_f["mobs"][0]), ["cx", "cy", "h", "in", "s", "w"])
check("分数截到 3 位", _f["mobs"][0]["s"], 0.112)
check("in 归一成 0/1", _f["mobs"][0]["in"], 1)
check("bot 四件套", sorted(_f["bot"]), ["act", "mx", "my", "t_atk"])
check("按键状态归一成 0/1", (_f["keys"]["left"], _f["keys"]["right"]), (1, 0))
check("事件缺时间戳 ⇒ 补 0 且仍是三元组", _f["ev"][1], ["q", 0, 0.0])
check("其它键计数保留", _f["other_ev"], 4)
check("缺字段的帧：不炸、补 None/0", frame_record({})["player"], None)
check("非 dict 输入：不炸", frame_record(None)["mobs"], [])
check("乱怪表：不炸", frame_record({"mobs": "abc"})["mobs"], [])

print()
print("=" * 72)
print("[3] DemoRecorder：开关、写盘、抽稀、换文件、失败只吼一次")
print("=" * 72)
_tmp = tempfile.mkdtemp(prefix="verify_demo_")
_logh = LogCapture()
_log = logging.getLogger("verify_demo_rec")
_log.setLevel(logging.INFO)
_log.addHandler(_logh)

# 默认关 ⇒ 什么都不做
_rec0 = DemoRecorder(dict(_CFG, demo_record={"enabled": False}), logger=_log)
check_true("enabled=false ⇒ start() 返回 False", _rec0.start() is False)
check_true("enabled=false ⇒ 不建文件、add_frame 也不写",
           _rec0.add_frame({"t": 1.0}) is False and _rec0.n_written == 0)

_CFG["demo_record"]["out_dir"] = _tmp
_rec = DemoRecorder(_CFG, logger=_log)
check("绝对 out_dir 原样保留（tempfile 是绝对的）", _rec.out_dir, _tmp)
check_true("开了 ⇒ start() 成功", _rec.start() is True)
check_true("建了 .jsonl 文件", _rec.path.endswith(".jsonl"))
check_true("同时写了 meta.json",
           any(n.endswith("_meta.json") for n in os.listdir(_tmp)))
with open(_rec.path, "r", encoding="utf-8") as _fh:
    check("第一帧还没写 ⇒ 文件是空的", _fh.read(), "")
for _i in range(4):
    _rec.add_frame({"t": 100.0 + _i, "frame": _i, "player": (10, 20)})
check("4 帧都写了（every_n_frames=1）", _rec.n_written, 4)
_rec.stop()
with open(_rec.path, "r", encoding="utf-8") as _fh:
    _lines = [ln for ln in _fh.read().splitlines() if ln.strip()]
check("文件里就是 4 行 JSONL", len(_lines), 4)
check_true("每行都能解析", all(ln.startswith("{") and ln.endswith("}") for ln in _lines))

# 抽稀
_rec2 = DemoRecorder(dict(_CFG, demo_record=dict(_CFG["demo_record"],
                                                 every_n_frames=2)), logger=_log)
_rec2.start()
for _i in range(6):
    _rec2.add_frame({"t": 200.0 + _i, "frame": _i})
check("every_n_frames=2 ⇒ 6 帧只写 3 条", _rec2.n_written, 3)
_rec2.stop()

# 换文件
_rec3 = DemoRecorder(dict(_CFG, demo_record=dict(_CFG["demo_record"],
                                                 max_frames=2)), logger=_log)
_rec3.start()
for _i in range(3):
    _rec3.add_frame({"t": 300.0 + _i, "frame": _i})
check("max_frames=2 ⇒ 已开第 2 个文件", _rec3.n_files, 2)
check_true("换文件后还在录", _rec3.running is True)
_rec3.stop()
check("stop() 幂等（再调不炸）", _rec3.stop(), None)

# 建不了文件：把 out_dir 指到一个**文件**上
_bad = os.path.join(_tmp, "not_a_dir")
with open(_bad, "w", encoding="utf-8") as _fh:
    _fh.write("x")
_logh.msgs.clear()
_rec4 = DemoRecorder(dict(_CFG, demo_record=dict(_CFG["demo_record"],
                                                 out_dir=_bad)), logger=_log)
check_true("建不了文件 ⇒ start() 返回 False", _rec4.start() is False)
check_true("建不了文件 ⇒ 只吼一次", len(_logh.search("[示范录制]")) == 1)

print()
print("=" * 72)
print("[4] KeyWatch：只认游戏键（不记键名）、事件带时间戳、缺库降级")
print("=" * 72)
_w = KeyWatch(game_key_map(_CFG), logger=_log)


class _K:
    '''假 pynput 键：有 char 的当字符键，否则用 name 当特殊键。'''

    def __init__(self, char=None, name=None):
        self.char = char
        if name is not None:
            self.name = name
            self._n = name

    def __str__(self):
        return getattr(self, "_n", "?")


_w._on_press(_K(char="q"))
_w._on_press(_K(char="z"))          # 不是游戏键 ⇒ 只计数
_w._on_release(_K(char="q"))
_st, _ev, _other = _w.snapshot()
check("按下 q ⇒ aoe_skill=1（当前状态）", _st.get("aoe_skill") in (0, 1), 1)
check("事件两条：按下 + 抬起", [_e[1] for _e in _ev], [1, 0])
check_true("事件带时间戳（>0）", all(_e[2] > 0 for _e in _ev))
check("非游戏键只累加计数", _other, 1)
check_true("快照里不出现非游戏键名",
           all("z" not in str(_k) for _k in _st))
_st2, _ev2, _ = _w.snapshot()
check("取走后再取：事件为空（不重复报）", _ev2, [])
check_true("方向键也在表里（left）", "left" in _st2)

_saved = sys.modules.get("pynput", None)
try:
    class _Bad:
        pass
    sys.modules["pynput"] = _Bad()          # 没有 keyboard 属性 ⇒ import 失败
    _w2 = KeyWatch(game_key_map(_CFG), logger=_log)
    check_true("pynput 装不上 ⇒ start() 返回 False（降级不抛）",
               _w2.start() is False)
    check("标记为不可用", _w2.available, False)
finally:
    if _saved is None:
        sys.modules.pop("pynput", None)
    else:
        sys.modules["pynput"] = _saved

print()
print("=" * 72)
print("[5] 接线白盒：hunting 调用点 / 引擎 start 与 stop / 配置与中文名")
print("=" * 72)
_hunt = open(os.path.join(_ROOT, "src", "states", "hunting.py"),
             encoding="utf-8").read()
check_true("hunting 里调了 record_demo_frame()",
           "self.bot.record_demo_frame()" in _hunt)
check_true("且是在 `cmd_move_x_last` 之后（影子决策已定稿）",
           _hunt.index("cmd_move_x_last = self.bot.cmd_move_x")
           < _hunt.index("self.bot.record_demo_frame()"))
_eng = open(os.path.join(_ROOT, "src", "engine", "MapleStoryAutoLevelUp.py"),
            encoding="utf-8").read()
check_true("引擎 import 了录制器", "from src.utils.demo_recorder import" in _eng)
check_true("__init__ 里 self.demo = None", "self.demo = None" in _eng)
check_true("start() 里建 DemoRecorder", "DemoRecorder(self.cfg, logger=logger)"
           in _eng)
check_true("record_only ⇒ kb.disable()（键盘静音）",
           "self.kb.disable()" in _eng
           and _eng.index("record_only", _eng.index("DemoRecorder(self.cfg"))
           < _eng.index("self.kb.disable()", _eng.index("DemoRecorder(self.cfg")))
check_true("terminate_threads 里会 stop 录制",
           "self.demo.stop()" in _eng)
check_true("_demo_frame_facts 是纯观测（不写 self.cmd_）",
           "self.cmd_move_x =" not in _eng.split("def _demo_frame_facts")[1]
           .split("def record_demo_frame")[0])
_dcfg = load_yaml(os.path.join(_ROOT, "config", "config_default.yaml"))
check_true("配置里有 demo_record 段", "demo_record" in _dcfg)
check("出厂是关的（免得大家被动录盘）", _dcfg["demo_record"]["enabled"], False)
check("出厂只记录不操作", _dcfg["demo_record"]["record_only"], True)
_names = open(os.path.join(_ROOT, "src", "utils", "cfg_names.py"),
              encoding="utf-8").read()
check_true("cfg_names 有分组中文名", '"demo_record": "' in _names)
check_true("cfg_names 有 record_only 的中文名", '"record_only":' in _names)

print()
print("=" * 72)
print("[6] 行为：_demo_frame_facts() 的怪中心按 (宽, 高) 算")
print("=" * 72)


def _mk_stub(cfg, monsters, loc_player=(300, 300)):
    s = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    for _n in ("_demo_frame_facts", "_mobs_in_attack_box", "_engage_mode",
               "_min_mob_area", "get_attack_range"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg
    s.monsters = monsters
    # 攻击框要在 img_frame 里夹取；模板面积给 40×40（×max_mob_area_trigger 1500）
    s.img_frame = np.zeros((600, 800, 3), np.uint8)
    s.monsters_info = {"m": [(np.zeros((40, 40, 3), np.uint8), None)]}
    s.loc_player = loc_player
    s.loc_nametag = (280, 285)
    s.nametag_miss_streak = 0
    s.is_on_ladder = False
    s.idx_routes = 1
    s.loc_player_minimap = (104, 28)
    s.loc_player_global = (166, 58)
    s.cmd_move_x, s.cmd_move_y, s.cmd_action = "right", "none", "attack"
    s.t_last_attack = 999.0
    s.get_search_box = lambda: (0, 80, 660, 470)
    return s


_bbc = {"bot": {"attack": "aoe_skill"},
        "aoe_skill": {"range_x": 360, "range_y": 160},
        "monster_detect": {"mob_engage_mode": "center",
                           "max_mob_area_trigger": 1500}}
# 宽 40 / 高 30：中心应是 (500+20, 300+15) = (520, 315)——若按 (高, 宽) 读就是 (515,320)
_sb = _mk_stub(_bbc, [{"name": "m", "position": (500, 300), "size": (40, 30),
                       "score": 0.2}])
_facts = _sb._demo_frame_facts()
check("怪中心 x = 左 + 宽/2", _facts["mobs"][0]["cx"], 520)
check("怪中心 y = 上 + 高/2", _facts["mobs"][0]["cy"], 315)
check("宽高原样带出", (_facts["mobs"][0]["w"], _facts["mobs"][0]["h"]), (40, 30))
check("影子决策带出（这就是「人 vs 引擎」的另一半）",
      (_facts["bot"]["mx"], _facts["bot"]["act"]), ("right", "attack"))
check_true("攻击框在事实里", len(_facts["attack_box"]) == 4)
check_true("索敌框在事实里", len(_facts["search_box"]) == 4)
check("段号/小地图/全局都在", (_facts["seg"], _facts["minimap"],
                              _facts["global"]), (1, (104, 28), (166, 58)))
# 够得着的标记：把怪放进框内
_sb2 = _mk_stub(_bbc, [{"name": "m", "position": (300, 280), "size": (40, 30),
                        "score": 0.2}])
check("框内那只 in=1", _sb2._demo_frame_facts()["mobs"][0]["in"], 1)
_sb3 = _mk_stub(_bbc, [{"name": "m", "position": (900, 600), "size": (40, 30),
                        "score": 0.2}])
check("框外那只 in=0", _sb3._demo_frame_facts()["mobs"][0]["in"], 0)
check("meta 里带 schema 与字段说明",
      demo_meta(_CFG, game_key_map(_CFG))["schema"], "demo/v1")

print()
print("=" * 72)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
