# -*- coding: utf-8 -*-
'''手动示范录制（2026-10-14 加）：把「引擎看到的」与「人怎么操作」一起落盘。

用户原话（m07008）：
    「那么能否开着人物检测和怪物检测记录数据，然后我自己操作，录下我的打怪行为，
      以此作为数据样本来优化逻辑呢？」

为什么这条路比继续猜阈值强：
    · 引擎的"够得着"判据（交叠面积 / 中心在框内）、攻击框大小、确认两次的节奏、
      爬梯期间让不让打 —— 这些**阈值**在真机上到底偏紧还是偏松，只有"人在同样的
      画面上怎么做"这一个参照物能回答；
    · 而且人手动玩时，引擎**照样在算它的决策**（`cmd_move_x/y/action`）——
      于是同一帧里同时有"引擎想怎么打"和"人怎么打"，把两者的差集列出来就是
      "逻辑哪里不对"的硬名单（`tools/analyze_demo.py` 干这件事）。

怎么做到"只记录不操作"：
    `src/input/KeyBoardController.py:442` 的主循环是 `if not self.is_enable: continue`
    ⇒ `kb.disable()` 是**彻底静音**（一个键都不发），而引擎的感知/决策链照跑。
    配置里 `demo_record.record_only: true` 就会在开局把它关掉并打一行醒目日志。

数据格式（JSONL，一行一帧；字段名压缩过，30fps 下省一半体积）：
    {"t":1750000000.123,"frame":4312,"player":[300,300],"nametag":[280,280],
     "miss":0,"ladder":false,"seg":2,"minimap":[104,28],"global":[166,58],
     "attack_box":[130,230,470,370],"search_box":[0,80,660,470],
     "mobs":[{"cx":476,"cy":317,"w":40,"h":40,"s":0.11,"in":0}],
     "bot":{"mx":"right","my":"none","act":"none","t_atk":1750000000.0},
     "keys":{"left":0,"right":1,"attack":0},
     "ev":[["attack",1,1750000000.317],["attack",0,1750000000.351]],
     "other_ev":0}
    字段说明见 `demo_meta()` 与 `tools/analyze_demo.py` 的报告头。

⚠️ 三条纪律（写进 docstring 免得后人踩）：
    ① **只记游戏键**：监听器只把"配置里那几个动作键 + 四个方向键"写进 `keys/ev`；
       其它按键只累加 `other_ev` 计数、**不记键名** —— 否则你在界面里打字（聊天、
       改角色名）会被当成操作样本，标签就废了（也不该把键盘输入原文落盘）。
    ② **纯观测**：所有写盘动作都在 try/except 里，异常只吼一次并自行关掉录制，
       绝不影响挂机；关掉开关（`enabled: false`）时一行都不写、不做任何事。
    ③ **感知不是真值**：录到的是**引擎自己的**感知（含误检、含丢名字）。
       这正是要优化的对象，但分析时不能当成画面的真值 —— 报告里分列标注。
'''
import gzip
import json
import os
import time

try:      # 相对路径按 APP_ROOT 解析（与 CWD 解耦；见 src/utils/paths.py 文件头）
    from src.utils.paths import resource_path as _resolve_path
    _PATH_RESOLVE_OK = True
except Exception:                                              # noqa: BLE001
    # ⚠️ 这里**不许静默**：退回原行为时留个记号，start() 会明确警告一句。
    #    （2026-10-14 踩过：函数名写错成 resolve_path，except 吞掉 ImportError，
    #      结果是"以为按 APP_ROOT 解析、其实按 CWD 写"—— 找了一轮才发现。）
    _PATH_RESOLVE_OK = False

    def _resolve_path(p):        # 拿不到路径工具就退回原行为（不抛、不改语义）
        return p

#: 配置段名（顶层）
DEMO_SECTION = "demo_record"

#: 四个方向键（引擎用 Interception 的固定名发出去，见 KeyBoardController）
ARROW_KEYS = ("left", "right", "up", "down")

#: 特殊键名的归一表（pynput 会给出 alt_l / ctrl_r / shift_l 这类左右写法）
_ALIAS = {
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "ctrl_l": "ctrl", "ctrl_r": "ctrl", "control": "ctrl",
    "shift_l": "shift", "shift_r": "shift",
    "return": "enter", "escape": "esc", "page_up": "pgup",
    "page_down": "pgdn", "caps_lock": "capslock",
}


def norm_key(name):
    '''把各种写法的键名归一成小写短名（`Key.space` / `space` / `'Q'` → 同一个）。

    Args:
        name: pynput 的 `Key.*`、`KeyCode.char` 或配置里的字符串；None ⇒ `""`。

    Returns:
        str: 归一后的短名（`"space"` / `"q"` / `"left"` …）；认不出 ⇒ `""`。
    '''
    if name is None:
        return ""
    try:
        _s = str(name).strip().lower()
    except Exception:                                         # noqa: BLE001
        return ""
    if not _s:
        return ""
    # pynput 的 Key.left 会写成 "Key.left"；KeyCode.char 直接是字符
    if "." in _s:
        _s = _s.split(".", 1)[1]
    _s = _s.strip("'\"")
    if len(_s) == 1:
        return _s
    return _ALIAS.get(_s, _s)


def demo_settings(cfg):
    '''读 `demo_record` 段并归一（缺键/畸形一律回默认值，**默认关**）。

    Returns:
        dict: enabled / out_dir / record_only / record_keys / every_n_frames /
              max_minutes / gzip / max_frames
    '''
    _raw = {}
    try:
        _raw = (cfg or {}).get(DEMO_SECTION) or {}
        if not isinstance(_raw, dict):
            _raw = {}
    except Exception:                                         # noqa: BLE001
        _raw = {}

    def _b(key, dflt):
        try:
            return bool(_raw.get(key, dflt))
        except Exception:                                     # noqa: BLE001
            return dflt

    def _i(key, dflt, lo=None):
        try:
            _v = int(_raw.get(key, dflt))
        except Exception:                                     # noqa: BLE001
            return dflt
        return _v if (lo is None or _v >= lo) else dflt

    return {
        "enabled": _b("enabled", False),
        "out_dir": str(_raw.get("out_dir") or "debug/demo"),
        "record_only": _b("record_only", True),
        "record_keys": _b("record_keys", True),
        "every_n_frames": max(1, _i("every_n_frames", 1, 1)),
        "max_minutes": _i("max_minutes", 30, 1),
        "gzip": _b("gzip", True),
        "max_frames": _i("max_frames", 0, 0),
    }


def game_key_map(cfg):
    '''「要录的键 → 动作名」映射（只录游戏键，见文件头纪律 ①）。

    来源：四个方向键（固定）+ `key.*` 里的按键类取值 + `potion.*.key`。
    取值不是字符串的（`jump_cooldown` / `pickup_cooldown` 这类数字、空串）跳过。

    Returns:
        dict: {归一后的键名: 动作名}；认不出的键不进表（＝不录）。
    '''
    out = {k: k for k in ARROW_KEYS}
    try:
        _key = (cfg or {}).get("key") or {}
        if isinstance(_key, dict):
            for _act, _v in _key.items():
                if _act in ("jump_cooldown", "pickup_cooldown", "pickup_jitter"):
                    continue
                if not isinstance(_v, str) or not _v.strip():
                    continue
                _n = norm_key(_v)
                if _n:
                    out[_n] = str(_act)
    except Exception:                                         # noqa: BLE001
        pass
    # 喝药键（形状随版本变：可能 potion.key，也可能 potion.<档>.key）—— 扫一遍
    try:
        _pot = (cfg or {}).get("potion") or {}
        _stack = [("potion", _pot)]
        while _stack:
            _path, _node = _stack.pop()
            if isinstance(_node, dict):
                for _k, _v in _node.items():
                    if _k == "key" and isinstance(_v, str) and _v.strip():
                        _n = norm_key(_v)
                        if _n:
                            out[_n] = "potion"
                    elif isinstance(_v, dict):
                        _stack.append((f"{_path}.{_k}", _v))
    except Exception:                                         # noqa: BLE001
        pass
    return out


def _box4(v):
    '''把 (x0,y0,x1,y1) 归一成 4 个 int；拿不到 ⇒ None。'''
    if not v:
        return None
    try:
        return [int(v[0]), int(v[1]), int(v[2]), int(v[3])]
    except Exception:                                         # noqa: BLE001
        return None


def _pt2(v):
    '''把 (x,y) 归一成 2 个 int；拿不到 ⇒ None。'''
    if not v:
        return None
    try:
        return [int(v[0]), int(v[1])]
    except Exception:                                         # noqa: BLE001
        return None


def frame_record(facts):
    '''把一帧的事实组装成**待写盘的那一行**（纯函数 → 可离线单测）。

    Args:
        facts (dict): 引擎给的原始事实（见 `MapleStoryAutoBot._demo_frame_facts`）：
            t / frame / player / nametag / miss / ladder / seg / minimap / global /
            attack_box / search_box / mobs / bot / keys / ev / other_ev

    Returns:
        dict: 可 `json.dumps` 的一行；缺的字段补 None/0（**绝不抛异常**：
              观测行不能带崩主循环，也不能因为一个字段缺失就整帧丢）。
    '''
    _f = facts if isinstance(facts, dict) else {}
    out = {
        "t": round(float(_f.get("t") or 0.0), 3),
        "frame": int(_f.get("frame") or 0),
        "player": _pt2(_f.get("player")),
        "nametag": _pt2(_f.get("nametag")),
        "miss": int(_f.get("miss") or 0),
        "ladder": bool(_f.get("ladder")),
        "seg": _f.get("seg", None),
        "minimap": _pt2(_f.get("minimap")),
        "global": _pt2(_f.get("global")),
        "attack_box": _box4(_f.get("attack_box")),
        "search_box": _box4(_f.get("search_box")),
        "mobs": [],
        "bot": {},
        "keys": {},
        "ev": [],
        "other_ev": int(_f.get("other_ev") or 0),
    }
    for _m in (_f.get("mobs") or []):
        try:
            _cx, _cy = int(_m["cx"]), int(_m["cy"])
            _w, _h = int(_m["w"]), int(_m["h"])
            _s = round(float(_m.get("s") or 0.0), 3)
            out["mobs"].append({"cx": _cx, "cy": _cy, "w": _w, "h": _h,
                                "s": _s, "in": 1 if _m.get("in") else 0})
        except Exception:                                     # noqa: BLE001
            continue
    _bot = _f.get("bot") or {}
    try:
        out["bot"] = {
            "mx": str(_bot.get("mx", "none")),
            "my": str(_bot.get("my", "none")),
            "act": str(_bot.get("act", "none")),
            "t_atk": round(float(_bot.get("t_atk") or 0.0), 3),
        }
    except Exception:                                         # noqa: BLE001
        out["bot"] = {"mx": "none", "my": "none", "act": "none", "t_atk": 0.0}
    for _k, _v in (_f.get("keys") or {}).items():
        try:
            out["keys"][str(_k)] = 1 if _v else 0
        except Exception:                                     # noqa: BLE001
            continue
    for _e in (_f.get("ev") or []):
        try:
            out["ev"].append([str(_e[0]), 1 if _e[1] else 0,
                              round(float(_e[2]) if len(_e) > 2 else 0.0, 3)])
        except Exception:                                     # noqa: BLE001
            continue
    return out


class KeyWatch:
    '''全局键盘监听：只关心**游戏键**（`game_key_map` 里有的那些）。

    用 `pynput` 的 Listener（环境里已装）拿**按下/抬起事件时间戳** —— 30fps 的
    逐帧采样会漏掉"点一下"（33ms 以下），事件流不会。

    ⚠️ 其它键不记键名，只累加 `other_ev`（见文件头纪律 ①）。
    构造时**不 import pynput**（CI/自检环境未必有）；`start()` 里才 import，
    缺库 ⇒ 记一行日志并降级成"不录按键"（其余数据照录）。
    '''

    def __init__(self, key_map=None, logger=None):
        self.key_map = dict(key_map or {})
        self.logger = logger
        self._state = {}
        self._events = []
        self._listener = None
        self.available = None        # None=还没试；True/False=试过
        self.n_other = 0

    def start(self):
        '''开始监听（失败 ⇒ 返回 False，绝不抛）。'''
        try:
            from pynput import keyboard as _kb
        except Exception as _e:                                # noqa: BLE001
            self.available = False
            if self.logger:
                self.logger.warning(
                    f"[示范录制] 装不上 pynput（{_e!r}）⇒ **按键不录**，"
                    f"其余数据照录。要录按键：pip install pynput")
            return False
        try:
            self._listener = _kb.Listener(on_press=self._on_press,
                                          on_release=self._on_release)
            self._listener.daemon = True
            self._listener.start()
            self.available = True
            if self.logger:
                self.logger.info(
                    f"[示范录制] 按键监听已开（认这些键："
                    f"{', '.join(sorted(self.key_map))}）")
            return True
        except Exception as _e:                                # noqa: BLE001
            self.available = False
            if self.logger:
                self.logger.warning(f"[示范录制] 按键监听启动失败：{_e!r}")
            return False

    def stop(self):
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:                                     # noqa: BLE001
            pass
        self._listener = None

    # ── pynput 回调 ────────────────────────────────────────────────────────
    def _key_name(self, key):
        '''pynput 的 key 对象 → 归一短名。'''
        _c = getattr(key, "char", None)
        if _c:
            return norm_key(_c)
        return norm_key(key)

    def _on_press(self, key):
        _n = self._key_name(key)
        if not _n:
            return
        if _n not in self.key_map:
            self.n_other += 1
            return
        self._state[_n] = 1
        self._events.append([_n, 1, time.time()])

    def _on_release(self, key):
        _n = self._key_name(key)
        if not _n or _n not in self.key_map:
            return
        self._state[_n] = 0
        self._events.append([_n, 0, time.time()])

    # ── 给录制器用的快照 ───────────────────────────────────────────────────
    def snapshot(self):
        '''返回 `(当前按键状态, 自上次取走后新增的事件)`；线程安全性够用即可。'''
        try:
            _ev = self._events
            self._events = []
        except Exception:                                     # noqa: BLE001
            _ev = []
        _st = {}
        for _n in self.key_map:
            _st[self.key_map[_n]] = 1 if self._state.get(_n) else 0
        # 同一动作可能挂了多个键（比如 aoe 与 directional 都用），取"按下过的那个"
        for _e in _ev:
            _act = self.key_map.get(_e[0])
            if _e[1]:
                _st[_act] = 1
        return _st, _ev, int(self.n_other)


class DemoRecorder:
    '''把每帧的事实写成一个 JSONL 文件（满了/超时了自动换下一个）。

    用法（引擎里）：
        rec = DemoRecorder(cfg, logger=logger)
        rec.start()                      # 只有 enabled=true 才真的建文件
        rec.add_frame(facts)             # 每帧一次；内部按 every_n_frames 抽稀
        rec.stop()
    '''

    def __init__(self, cfg, logger=None, key_watch=None, clock=None,
                 out_dir=None):
        self.cfg = cfg or {}
        self.logger = logger
        self.settings = demo_settings(self.cfg)
        if out_dir:
            self.settings["out_dir"] = str(out_dir)
        self.clock = clock or time.time
        self.key_watch = key_watch
        self.key_map = game_key_map(self.cfg)
        # ⚠️ 落盘目录：相对路径**按 APP_ROOT 解析**（与 CWD 解耦）——
        #   本项目的统一约定，见 src/utils/paths.py 的文件头。不这么做的话，
        #   用户从不同快捷方式启动时 CWD 不同，录出来的文件会散落各处，
        #   而"日志说写进 debug/demo 了"与"实际在哪"对不上（静默走偏）。
        self.out_dir = _resolve_path(self.settings["out_dir"])
        self.running = False
        self.failed = False
        self.n_frames = 0            # 已处理帧数（用于 every_n_frames）
        self.n_written = 0           # 累计写出的帧数（跨文件，报告用）
        self.n_in_file = 0           # **本文件**已写帧数（max_frames 判据用）
        self.n_files = 0
        self.path = None
        self._fh = None
        self._t_open = 0.0
        self._t_flush = 0.0

    # ── 生命周期 ───────────────────────────────────────────────────────────
    def start(self):
        '''建文件 + 开按键监听。`enabled: false` ⇒ 什么都不做（返回 False）。'''
        if not self.settings["enabled"]:
            return False
        if self.settings["record_keys"] and self.key_watch is None:
            self.key_watch = KeyWatch(self.key_map, logger=self.logger)
            self.key_watch.start()
        try:
            self._open_file()
        except Exception as _e:                                # noqa: BLE001
            self._fail(f"建文件失败：{_e!r}")
            return False
        self.running = True
        _rec = "**只记录不操作**（键盘已静音）" if self.settings["record_only"] \
            else "键盘**没静音**（引擎会同时操作，样本会被污染！）"
        if self.logger and not _PATH_RESOLVE_OK:
            self.logger.warning(
                "[示范录制] 路径工具不可用 ⇒ 相对目录按**当前工作目录**解析，"
                "文件可能不在程序目录下（日志下面会打绝对路径）")
        if self.logger:
            self.logger.info(
                f"[示范录制] 已开始：{_rec}；写盘目录 "
                f"{self.out_dir}"
                f"（每帧 1 条，{self.settings['max_minutes']} 分钟或满一文件就换下一个）")
        return True

    def stop(self):
        '''收尾：关文件、停监听（可重复调用）。'''
        self.running = False
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:                                     # noqa: BLE001
            pass
        self._fh = None
        if self.key_watch is not None:
            self.key_watch.stop()
        if self.logger and self.n_written:
            self.logger.info(f"[示范录制] 已停止：共写 {self.n_written} 帧 / "
                             f"{self.n_files} 个文件")

    def _open_file(self):
        _d = self.out_dir
        os.makedirs(_d, exist_ok=True)
        _ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.clock()))
        _ext = ".jsonl.gz" if self.settings["gzip"] else ".jsonl"
        self.path = os.path.join(_d, f"demo_{_ts}{_ext}")
        if self.settings["gzip"]:
            self._fh = gzip.open(self.path, "at", encoding="utf-8")
        else:
            self._fh = open(self.path, "a", encoding="utf-8")
        self._t_open = self.clock()
        self._t_flush = self._t_open
        self.n_in_file = 0
        self.n_files += 1
        # 兄弟文件：meta（配置快照 + 键表）—— 分析时靠它把动作名映射回键
        try:
            _meta = os.path.join(
                _d, f"demo_{_ts}_{self.n_files}_meta.json")
            with open(_meta, "w", encoding="utf-8") as _mf:
                json.dump(demo_meta(self.cfg, self.key_map),
                          _mf, ensure_ascii=False, indent=2)
        except Exception as _e:                                # noqa: BLE001
            if self.logger:
                self.logger.warning(f"[示范录制] meta 写失败（不影响数据）：{_e!r}")

    def _fail(self, why):
        '''录制出错：吼一次、自己关掉（**绝不**影响挂机）。'''
        if self.failed:
            return
        self.failed = True
        self.running = False
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:                                     # noqa: BLE001
            pass
        self._fh = None
        if self.logger:
            self.logger.warning(f"[示范录制] {why} —— 录制已自行关闭"
                                f"（只吼这一次；挂机不受影响）")

    # ── 每帧 ───────────────────────────────────────────────────────────────
    def add_frame(self, facts):
        '''写一帧（`every_n_frames` 抽稀；到点换文件；异常 ⇒ 自行关闭）。'''
        if not self.running:
            return False
        self.n_frames += 1
        if self.settings["every_n_frames"] > 1 and \
                self.n_frames % self.settings["every_n_frames"]:
            return False
        try:
            _f = dict(facts or {})
            _f.setdefault("t", self.clock())
            if self.settings["record_keys"] and self.key_watch is not None:
                _st, _ev, _other = self.key_watch.snapshot()
                _f["keys"] = _st
                _f["ev"] = _ev
                _f["other_ev"] = _other
            _line = json.dumps(frame_record(_f), ensure_ascii=False,
                               separators=(",", ":"))
            self._fh.write(_line + "\n")
            self.n_written += 1
            self.n_in_file += 1
            _now = self.clock()
            if _now - self._t_flush >= 5.0:
                self._fh.flush()
                self._t_flush = _now
            _too_long = (_now - self._t_open) >= self.settings["max_minutes"] * 60
            _too_many = (self.settings["max_frames"] > 0
                         and self.n_in_file >= self.settings["max_frames"])
            if _too_long or _too_many:
                self._fh.close()
                self._fh = None
                self.running = True       # 换文件后继续录
                self._open_file()
            return True
        except Exception as _e:                                # noqa: BLE001
            self._fail(f"写盘失败：{_e!r}")
            return False


def demo_meta(cfg, key_map=None):
    '''给分析工具看的元信息（配置快照 + 键表 + 判据）。'''
    _s = demo_settings(cfg)
    _md = {}
    try:
        _md = dict((cfg or {}).get("monster_detect") or {})
    except Exception:                                         # noqa: BLE001
        _md = {}
    _out = {}
    for _k in ("mob_engage_mode", "max_mob_area_trigger", "every_n_frames",
               "stop_to_attack", "approach_mob", "approach_dy_tol",
               "detect_range_scale", "detect_range_scale_y",
               "search_box_margin", "stale_pos_full_frame_after"):
        _out[_k] = _md.get(_k)
    try:
        _aoe = dict((cfg or {}).get("aoe_skill") or {})
        _dir = dict((cfg or {}).get("directional_attack") or {})
        _bot = dict((cfg or {}).get("bot") or {})
    except Exception:                                         # noqa: BLE001
        _aoe, _dir, _bot = {}, {}, {}
    return {
        "schema": "demo/v1",
        "settings": _s,
        "key_map": dict(key_map or game_key_map(cfg)),
        "attack_mode": _bot.get("attack"),
        "aoe": {"range_x": _aoe.get("range_x"), "range_y": _aoe.get("range_y"),
                "cooldown": _aoe.get("cooldown")},
        "directional": {"range_x": _dir.get("range_x"),
                        "range_y": _dir.get("range_y"),
                        "cooldown": _dir.get("cooldown")},
        "monster_detect": _out,
        "fields": {
            "mobs[]": "cx,cy=中心; w,h=宽高; s=匹配分; in=1 表示**引擎判它够得着**",
            "bot": "mx/my/act=引擎本帧的决策（影子决策）; t_atk=上次出招时刻",
            "keys": "动作名→0/1（按下的键）",
            "ev": "[键名, 1按下/0抬起, 时间戳]",
            "other_ev": "非游戏键的按下次数（**不记键名**）",
        },
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
