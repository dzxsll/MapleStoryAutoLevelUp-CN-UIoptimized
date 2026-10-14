# -*- coding: utf-8 -*-
"""录制轨迹（record_trace.jsonl）的**格式定义与读写**（2026-10-12 加）。

## 这是什么、为什么要有它

录制器原来只落图片（`route{N}.png`），**帧一过就只剩像素了**。于是：

  · 路线录坏了，只能回游戏**重走一遍**，没有任何离线调参的余地；
  · 折返点靠人手点 F8，时机偏了就录错（真实事故：A→B→A 被存成一段，
    去程 right 和回程 left 叠在同一张图上，引擎分不清 → 「启动后直奔右边一直走」）；
  · 10fps 采样下丢帧/定位抖动造成的杂点，事后无从分辨也补不回来。

轨迹文件把「用户当时**做了什么操作**」原样记下来（每帧：时刻、角色全局坐标、
动作、是否画团、段号），`tools/routeRebuild.py` 就能**离线重生成**路线图：
不用再进游戏，反复调参数直到满意。

## 为什么格式定义单独一个模块

写入方是录制器、读取方是重生成工具。字段名/版本号若各写一份，**两边一定会漂**
（项目里已经吃过"录制器说没事、挂机时不启用"这种分裂的亏）。
⇒ 写与读都从这里取，谁也别自己拼字符串。

## 两个刻意的设计

1. **时间戳存绝对时间（time.time()），不存相对时间。**
   因为录制器的 `blob_cooldown`（跳跃圆点冷却）判的是 `time.time() - t_last_draw_blob`。
   重放时只有用**同样的绝对时刻**，才能复现出"这一帧的跳到底画没画"，
   否则重生成出来的图和原图在跳跃点上对不上。

2. **写失败绝不允许影响录制。**
   录像是用户**不可复现的劳动**（要真机走一遍），轨迹只是优化辅助。
   磁盘满 / 权限不足 / 路径异常时，一律静默降级成"没有轨迹"，
   只在第一次失败时提示一句，绝不抛出打断录制。

格式（JSONL，每行一个 JSON 对象，UTF-8）：

    {"k":"h", ...}                                    会话头（每个会话第一行）
    {"k":"f","t":..,"x":..,"y":..,"a":..,"b":..,"seg":..,"on":..,"p":..}
    {"k":"e","t":..,"ev":"arm|waypoint|save|home_start|home_save|cancel_home|map_saved", ...}

`k` = kind。一整个文件是**多个会话顺序追加**（用户可能反复 F4 重录），
用 `split_sessions()` 切开；重生成默认只取**最后一个**会话。
"""
from __future__ import annotations

import json
import os

from src.utils.logger import logger

# 文件名与版本：重生成工具靠它找文件、判断自己认不认这个格式
TRACE_NAME = "record_trace.jsonl"
TRACE_VERSION = 1

# 事件名白名单。写进来的是自由字符串，读的时候按这个集合过滤 ——
# 这样"以后加了新事件、旧工具读到"不会炸，只是不认识而已。
KNOWN_EVENTS = (
    "arm",          # 用户点了【开始录】
    "waypoint",     # 折返点：存下这一段并开新段
    "save",         # 保存这一段（F3 / 面板）
    "home_start",   # 回正线起头（我在落地点）
    "home_save",    # 回正线收尾（已回到主线）
    "cancel_home",  # 放弃正在录的回正线
    "map_saved",    # 存 map.png
)


# ── 色码表 <-> JSON ───────────────────────────────────────────────────────────
# 色码表在内存里是 {(r,g,b): "action"} 的元组键字典；JSON 的键只能是字符串，
# 所以落盘时拍平成 "r,g,b"（和 config_default.yaml 里的写法一致），读回来再还原。

def codes_to_json(codes) -> dict:
    '''{(r,g,b): "action"} → {"r,g,b": "action"}。'''
    out = {}
    for k, v in (codes or {}).items():
        try:
            r, g, b = (int(p) for p in k)
        except Exception:                                    # noqa: BLE001
            continue
        out[f"{r},{g},{b}"] = v
    return out


def codes_from_json(obj) -> dict:
    '''{"r,g,b": "action"} → {(r,g,b): "action"}；坏键直接跳过。'''
    out = {}
    for k, v in (obj or {}).items():
        try:
            parts = [int(p) for p in str(k).split(",")]
        except Exception:                                    # noqa: BLE001
            continue
        if len(parts) >= 3:
            out[(parts[0], parts[1], parts[2])] = v
    return out


# ── 写 ────────────────────────────────────────────────────────────────────────

class RouteTraceWriter:
    '''把录制过程追加成 JSONL。**所有方法都不抛异常**（见模块头第 2 条）。

    用法：
        tw = RouteTraceWriter(map_dir, meta={...})
        tw.frame(t=..., x=..., y=..., action=..., ...)
        tw.event("save", seg=1, x=140, y=50)
        tw.close()
    '''

    def __init__(self, map_dir, meta=None):
        self.path = os.path.join(map_dir, TRACE_NAME)
        self._fp = None
        self._broken = False          # True = 已放弃写轨迹（不再重试、不再刷日志）
        try:
            os.makedirs(map_dir, exist_ok=True)
            # 追加模式：同一次运行 / 多次录制都往同一个文件里按会话累加
            self._fp = open(self.path, "a", encoding="utf-8")
            self._write({"k": "h", "v": TRACE_VERSION, "t": _now(), **(meta or {})})
        except Exception as e:                               # noqa: BLE001
            self._fail(e)

    # -- 对外 ---------------------------------------------------------------
    @property
    def broken(self) -> bool:
        return self._broken

    def frame(self, t, x, y, action, is_blob, seg, on, pos_ok):
        '''记一帧。字段短名是为了让文件小一点（10fps 一录就是几百上千行）。

            t  绝对时刻（time.time()）—— 重放时复现 blob 冷却是靠它，见模块头
            x,y 角色在全局地图上的坐标
            a  action 串（"" = 这一帧没有动作）
            b  is_draw_blob（1 = 画团：跳/传送；0 = 画线）
            seg 当前段号（= 已存下的段数，从 0 起）
            on 1/0 = 正在记录 / 已暂停
            p  1/0 = 这一帧定位是否有效
        '''
        self._write({"k": "f", "t": _round(t), "x": int(x), "y": int(y),
                     "a": action, "b": 1 if is_blob else 0,
                     "seg": int(seg), "on": 1 if on else 0,
                     "p": 1 if pos_ok else 0})

    def event(self, ev, **kw):
        '''记一个用户操作事件（折返点 / 保存 / 回正线开关 …）。

        为什么必须记：**段边界是用户点的，不是能推出来的**。
        `save_route` 还被刻意推迟到帧末执行（goal 要压在最后一笔线上），
        光看像素根本还原不出"用户是在哪一帧按的"。
        '''
        self._write({"k": "e", "t": _round(kw.pop("t", None)), "ev": ev, **kw})

    def close(self):
        try:
            if self._fp is not None:
                self._fp.close()
        except Exception:                                    # noqa: BLE001
            pass
        self._fp = None

    # -- 内部 ---------------------------------------------------------------
    def _write(self, obj):
        if self._broken or self._fp is None:
            return
        try:
            self._fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._fp.flush()          # 崩溃/强杀也不丢已有轨迹
        except Exception as e:                                # noqa: BLE001
            self._fail(e)

    def _fail(self, e):
        '''第一次失败时记一条，然后**彻底闭嘴**（每秒 10 帧刷日志比不写还糟）。'''
        if not self._broken:
            logger.warning(f"[轨迹] 写不了录制轨迹（不影响录制，只是没法离线重生成）："
                           f"{self.path} → {e}")
        self._broken = True
        try:
            if self._fp is not None:
                self._fp.close()
        except Exception:                                    # noqa: BLE001
            pass
        self._fp = None


def _now():
    import time
    return _round(time.time())


def _round(t):
    try:
        return round(float(t), 4)
    except Exception:                                        # noqa: BLE001
        import time
        return round(time.time(), 4)


# ── 读 ────────────────────────────────────────────────────────────────────────

def load_trace(path):
    '''读整个 JSONL → [记录, ...]。坏行跳过（半行 / 手工编辑过的都见过）。'''
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:                                # noqa: BLE001
                continue
    return out


def split_sessions(records):
    '''按会话头（k=="h"）切开。头之前若有杂散记录，归进一个"无名会话"。'''
    sessions, cur = [], []
    for r in records:
        if r.get("k") == "h":
            if cur:
                sessions.append(cur)
            cur = [r]
        else:
            cur.append(r)
    if cur:
        sessions.append(cur)
    return sessions


def session_header(session):
    '''取会话头（没有就返回 {}）。'''
    for r in session or []:
        if r.get("k") == "h":
            return r
    return {}


def load_last_session(path):
    '''读文件里**最后一个**会话 → (header, [记录...])。

    为什么默认只要最后一个：用户反复 F4 重录时，旧会话对应的 route{N}.png
    早被覆盖了，拿旧轨迹去重生成只会把新路线改坏。
    '''
    sessions = split_sessions(load_trace(path))
    if not sessions:
        return {}, []
    return session_header(sessions[-1]), sessions[-1]


def trace_path(map_dir):
    return os.path.join(map_dir, TRACE_NAME)
