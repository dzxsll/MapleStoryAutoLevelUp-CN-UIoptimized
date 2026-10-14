# -*- coding: utf-8 -*-
'''「绳梯脚下蹭一下」自救自检（2026-10-14 加，用户 m08620 拍板 D）。

背景（用户 m08454「读取日志，现在有卡楼梯和偏离路线后站着不动的情况」）：
    真机日志 `log/MSBot_2026-10-14_15-07-02.log` 里两次长僵持：
      · 15:16:40 → 15:17:25 停在 全局(78,78)，10 个状态采样（≈50s）；
      · 15:32:22 → 15:33:05 停在 (76,78)，9 个采样（≈45s）；
    两次的指令都是 `(none up none)`，同期 `[按键状态] 指令(none up none)
    近5秒实际发键 94~97 次` ⇒ **键一直在发，不是按键失败**。
    机制（自锁）：路线那根绳子是 1px 竖列，角色停在列上 ⇒ 最近像素就是 up 像素
    ⇒ 只发纯 up；能把角色"挪进绳子"的横向补位在
    `if self.cmd_move_x == "none" and self.is_on_ladder` 里，而旗标要"纵向真动过"
    才置真 ⇒ 不动 ⇒ 旗标假 ⇒ 只发 up ⇒ 更不动。
    对照证据：同一份示范录制里**真人站在 x=78 只按 up 就爬上去了**
    （帧123/132/142/151：全局 y 78→76→74→72→70），而机器人本轮 65 次成功上爬里
    常见 `left up none` / `right up none`（"边走边按上"）。

做法（本轮改动）：
    `_rope_nudge_cfg()`（唯一读配置）+ `_rope_nudge_dir()`（唯一判据点）。
    路线跟随的 up 分支里，旗标为假时问一次：路线要 up/down、**不在梯上**、
    且最近 `rope_nudge_s`(1.0s) 没有竖向进展 ⇒ 往绳梯那一列蹭一下
    （已在列上就按时间片左右交替）；蹭够 `rope_nudge_max_s`(6s) 还不动 ⇒ 放弃，
    退回今天的老行为（交给既有的 `[路线够不着]` / watchdog 报警）。

本文件钉五件事：
  【1】`_rope_nudge_cfg()` 真值表（缺键/None/字符串/关掉/畸形/NaN/越界）
  【2】`_rope_nudge_dir()` 真值表（非 up-down / 在梯上 / 关掉 / 戳未初始化 /
       还没到阈值 / 朝列走 / 列上左右交替 / 超时放弃）
  【3】接线白盒（只调一次、文案在、is_on_ladder 那支还在、`[路线失联]` 文案已修）
  【4】配置 / 界面口径（出厂四值、四个中文名、都在 ADV_FIELD_HIDE['route']）
  【5】真机样本回放（有日志才跑）：僵持窗口蹭得起来、左右都出现、真在爬时不蹭
'''
import glob
import inspect
import os
import re
import sys
import time
from datetime import datetime

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402

PASS = 0
FAIL = 0

# `[运行状态] 帧N 小地图(x, y) 全局(x, y) 段i/n 指令(mx my act) 怪N 名字命中…`
_RE_ST = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*?"
    r"全局\((\d+),\s*(\d+)\).*?指令\((\w+) (\w+) (\w+)\)")


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


def stub(cfg=None, **kw):
    '''只绑本次两个新方法的假引擎（不跑 __init__）。'''
    class S:
        pass

    s = S()
    for _n in ("_rope_nudge_cfg", "_rope_nudge_dir"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg if cfg is not None else {}
    s.is_on_ladder = False
    s.loc_player_global = (100, 200)
    s.t_ladder_move = 0.0
    s.t_rope_nudge_log = 0.0
    s._rope_nudge_n = 0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _cfg4(**kw):
    '''构造 route 段（只给要改的键）。'''
    return {"route": dict(kw)}


def main():
    global PASS, FAIL

    print("【1】route.rope_nudge_* 的真值表（rope_nudge_s <= 0 = 关掉本层）")
    check("缺键 ⇒ 出厂四值", stub({})._rope_nudge_cfg(), (1.0, 120.0, 0.6, 6.0))
    check("rope_nudge_s=None ⇒ 1.0",
          stub(_cfg4(rope_nudge_s=None))._rope_nudge_cfg()[0], 1.0)
    check("rope_nudge_s='1.5' ⇒ 1.5",
          stub(_cfg4(rope_nudge_s="1.5"))._rope_nudge_cfg()[0], 1.5)
    check("rope_nudge_s=0 ⇒ 0.0（用户明确关掉）",
          stub(_cfg4(rope_nudge_s=0))._rope_nudge_cfg()[0], 0.0)
    check("rope_nudge_s=-1 ⇒ -1.0（负数也是关掉，不是畸形）",
          stub(_cfg4(rope_nudge_s=-1))._rope_nudge_cfg()[0], -1.0)
    check("rope_nudge_s=31 ⇒ 退回 1.0（超过 30，明显写错，不猜）",
          stub(_cfg4(rope_nudge_s=31))._rope_nudge_cfg()[0], 1.0)
    check("rope_nudge_s='abc' ⇒ 1.0",
          stub(_cfg4(rope_nudge_s="abc"))._rope_nudge_cfg()[0], 1.0)
    check("rope_nudge_s=NaN ⇒ 1.0",
          stub(_cfg4(rope_nudge_s=float("nan")))._rope_nudge_cfg()[0], 1.0)
    check("rope_nudge_ms=5 ⇒ 120（太短，夹回出厂）",
          stub(_cfg4(rope_nudge_ms=5))._rope_nudge_cfg()[1], 120.0)
    check("rope_nudge_ms=5000 ⇒ 120（太长）",
          stub(_cfg4(rope_nudge_ms=5000))._rope_nudge_cfg()[1], 120.0)
    check("rope_nudge_cycle_s=0.05 ⇒ 0.6（比蹭的时长还短，夹回）",
          stub(_cfg4(rope_nudge_cycle_s=0.05))._rope_nudge_cfg()[2], 0.6)
    check("rope_nudge_max_s=999 ⇒ 6.0（超上限）",
          stub(_cfg4(rope_nudge_max_s=999))._rope_nudge_cfg()[3], 6.0)
    check("rope_nudge_max_s=0 ⇒ 0.0（合法的「永不放弃」）",
          stub(_cfg4(rope_nudge_max_s=0))._rope_nudge_cfg()[3], 0.0)

    print("\n【2】_rope_nudge_dir() 真值表")
    _now = time.time()
    check("关掉本层（rope_nudge_s=0）⇒ 不蹭",
          stub(_cfg4(rope_nudge_s=0), t_ladder_move=_now - 9).\
          _rope_nudge_dir(100, "up"), "")
    check("路线没要上下 ⇒ 不蹭",
          stub({}, t_ladder_move=_now - 9)._rope_nudge_dir(100, "none"), "")
    check("在梯子上（is_on_ladder）⇒ 不蹭（按左右会松手）",
          stub({}, is_on_ladder=True, t_ladder_move=_now - 9).
          _rope_nudge_dir(100, "up"), "")
    check("拿不到全局坐标 ⇒ 不蹭（fail-closed）",
          stub({}, loc_player_global=None, t_ladder_move=_now - 9).
          _rope_nudge_dir(100, "up"), "")
    _s = stub({})
    check("戳还没初始化 ⇒ 不蹭（本帧只把戳起上）", _s._rope_nudge_dir(100, "up"), "")
    check_true("……并且真的把戳设成了现在（否则会被当成「早该放弃」）",
               _s.t_ladder_move >= _now, f"{_s.t_ladder_move} vs {_now}")
    check("刚动过（0.3s 前）⇒ 不蹭",
          stub({}, t_ladder_move=time.time() - 0.3)._rope_nudge_dir(100, "up"), "")
    check("列在左边 ⇒ 往 left 蹭",
          stub({}, t_ladder_move=time.time() - 2.0, loc_player_global=(100, 200)).
          _rope_nudge_dir(93, "up"), "left")
    check("列在右边 ⇒ 往 right 蹭",
          stub({}, t_ladder_move=time.time() - 2.0, loc_player_global=(100, 200)).
          _rope_nudge_dir(107, "up"), "right")
    check("差 1px（列上）＋时间片头部 ⇒ 先往 right（第 0 档）",
          stub({}, t_ladder_move=time.time() - 1.05, loc_player_global=(100, 200)).
          _rope_nudge_dir(101, "up"), "right")
    check("……第 1 档 ⇒ 改往 left（左右交替才扫得中那一列）",
          stub({}, t_ladder_move=time.time() - 1.65, loc_player_global=(100, 200)).
          _rope_nudge_dir(100, "up"), "left")
    check("同一档的后半段 ⇒ 不蹭（只按 up 等游戏判定）",
          stub({}, t_ladder_move=time.time() - 1.50, loc_player_global=(100, 200)).
          _rope_nudge_dir(100, "up"), "")
    check("蹭够 max(6s) 还不动 ⇒ 放弃，退回老行为",
          stub({}, t_ladder_move=time.time() - 7.5, loc_player_global=(100, 200)).
          _rope_nudge_dir(100, "up"), "")
    # 时间片会让一半的帧天然不蹭，单点断言会被"正好落在不蹭的那半"骗到 ⇒ 扫一遍
    _last_nudge_age = 0.0
    for _k in range(1, 900):
        _a = _k / 100.0
        _s = stub({}, t_ladder_move=time.time() - _a, loc_player_global=(100, 200))
        if _s._rope_nudge_dir(100, "up"):
            _last_nudge_age = _a
    check_true("……放弃时刻就是「start(1.0) + max(6.0) = 7.0s」，不是别的数",
               6.95 <= _last_nudge_age <= 7.15, f"最后一次可蹭 age={_last_nudge_age:.2f}s")
    _any = False
    for _k in range(100, 6000):
        _a = _k / 100.0
        _s = stub(_cfg4(rope_nudge_max_s=0), t_ladder_move=time.time() - _a,
                  loc_player_global=(100, 200))
        if _s._rope_nudge_dir(100, "up"):
            _any = True
            break
    check_true("max_s=0 ⇒ 永不放弃（用户明确要一直蹭）", _any)
    check("down 像素同样适用（下楼卡住也要能蹭）",
          stub({}, t_ladder_move=time.time() - 2.0, loc_player_global=(100, 200)).
          _rope_nudge_dir(93, "down"), "left")

    print("\n【3】接线白盒（源码断言）")
    _src = inspect.getsource(MapleStoryAutoBot)
    # ⚠️ 这段路线跟随在 `update_cmd_by_route()` 里，不在 `update_cmd_by_mob_detection()`
    #    —— 第一版钉错了方法名，6 条接线断言全红（自检自己踩的坑）。
    _hunt = inspect.getsource(MapleStoryAutoBot.update_cmd_by_route)
    check_true("路线跟随里只问 _rope_nudge_dir() 一次",
               _hunt.count("self._rope_nudge_dir(") == 1)
    check_true("问的时候用的是本帧扫到的 up 像素 x（up_x）",
               "self._rope_nudge_dir(\n                                _up_x," in _hunt
               or "_up_x, _ud_cmd[1]" in _hunt.replace("\n", " "))
    check_true("is_on_ladder 那支原封不动地留着（真在爬 ⇒ 用路线横向命令稳住）",
               "if self.cmd_move_x == \"none\":" in _hunt
               and "if self.is_on_ladder:" in _hunt
               and "only complement cmd_move_x when player is on ladder" in _hunt)
    check_true("有 [绳梯自救] 这行留痕（用户能看出它什么时候开始蹭）",
               "[绳梯自救]" in _hunt)
    check_true("留痕里点名了怎么关掉（route.rope_nudge_s 填 0）",
               "route.rope_nudge_s` 填 0" in _hunt)
    check_true("日志有 1.5s 节流（不许每帧刷屏）",
               'getattr(self, "t_rope_nudge_log", 0.0) > 1.5' in _hunt)
    check_true("_rope_nudge_cfg 全文件只定义一次",
               _src.count("def _rope_nudge_cfg(self):") == 1)
    check_true("_rope_nudge_dir 全文件只定义一次",
               _src.count("def _rope_nudge_dir(self, up_x, ud):") == 1)
    for _f in ("self.t_rope_nudge_log = 0.0", "self._rope_nudge_n = 0"):
        check_true(f"__init__ 里有 {_f}", _f in _src)
    check_true("只改文案、没改接引行为：still 打 hit['command']",
               "重新接引：{hit['command']}" in _src)
    check_true("误导的「（距离 {hit['distance']}px）」已不在",
               "（距离 {hit['distance']}px）" not in _src)
    check_true("改打成到角色的曼哈顿距离 + 路线像素坐标",
               "路线像素 {_hp}，到角色 {_d_play}px" in _src)
    check_true("锚点是 goal 像素这种「接不动」的接引会被点明",
               "**不会让角色移动**" in _src)

    print("\n【4】配置 / 界面口径")
    with open(os.path.join(_ROOT, "config", "config_default.yaml"),
              encoding="utf-8") as _fh:
        _cfg = yaml.safe_load(_fh)
    _r = _cfg["route"]
    check("出厂 route.rope_nudge_s = 1.0", _r.get("rope_nudge_s"), 1.0)
    check("出厂 route.rope_nudge_ms = 120", _r.get("rope_nudge_ms"), 120)
    check("出厂 route.rope_nudge_cycle_s = 0.6", _r.get("rope_nudge_cycle_s"), 0.6)
    check("出厂 route.rope_nudge_max_s = 6.0", _r.get("rope_nudge_max_s"), 6.0)
    from src.utils.cfg_names import cn_key                        # noqa: E402
    for _k in ("rope_nudge_s", "rope_nudge_ms", "rope_nudge_cycle_s",
               "rope_nudge_max_s"):
        _cn = cn_key("route", _k)
        check_true(f"{_k} 有中文名（日志点名 rope_nudge_s，用户去 yaml 能对上）",
                   _cn != _k and "绳梯脚下自救" in _cn, _cn)
    import src.ui.ui as _ui                                       # noqa: E402
    _hide = _ui.ADV_FIELD_HIDE.get("route", [])
    for _k in ("rope_nudge_s", "rope_nudge_ms", "rope_nudge_cycle_s",
               "rope_nudge_max_s"):
        check_true(f"{_k} 在 ADV_FIELD_HIDE['route'] 里收着（算法判据）",
                   _k in _hide)

    print("\n【5】真机样本回放（[运行状态] 采样 + 假时钟）")
    _logs = [p for p in sorted(glob.glob(os.path.join(_ROOT, "log", "MSBot_*.log")))
             if "运行状态" in open(p, encoding="utf-8", errors="ignore").read(
                 max(1, os.path.getsize(p)))]
    if not _logs:
        print("  [SKIP] log/ 下没有带 [运行状态] 的日志 —— 本段只在有日志时才算证据")
    else:
        # 取最长的那份（= 本轮的 15:07 那一轮，28 分钟、384 个采样）
        _path = max(_logs, key=lambda p: os.path.getsize(p))
        _samples = []
        for _ln in open(_path, encoding="utf-8", errors="ignore"):
            _m = _RE_ST.match(_ln)
            if _m:
                _samples.append((
                    datetime.strptime(_m.group(1), "%Y-%m-%d %H:%M:%S").timestamp(),
                    int(_m.group(2)), int(_m.group(3)),
                    _m.group(4), _m.group(5)))
        if len(_samples) < 10:
            print(f"  [SKIP] {os.path.basename(_path)} 里可解析的状态行只有 "
                  f"{len(_samples)} 条，样本太少")
        else:
            import src.engine.MapleStoryAutoLevelUp as _mod        # noqa: E402
            _real_time = time

            class _Clock:
                def __init__(self, t0):
                    self.t = t0

                def time(self):
                    return self.t

                def __getattr__(self, _name):
                    return getattr(_real_time, _name)

            _clk = _Clock(_samples[0][0] - 1.0)
            _mod.time = _clk
            try:
                _s = stub({})
                _i = 0
                _last_gy = None
                _n_elig = _n_nudge = _n_fresh = _n_fresh_nudge = 0
                _dirs = {"left": 0, "right": 0}
                _stall_frames = []      # (起始时刻, 帧数, 蹭了几帧)
                _run = None
                _t_end = _samples[-1][0]
                while _clk.t <= _t_end:
                    while _i + 1 < len(_samples) and _samples[_i + 1][0] <= _clk.t:
                        _i += 1
                    _t, _gx, _gy, _mx, _my = _samples[_i]
                    if _last_gy is not None and _gy != _last_gy:
                        _s.t_ladder_move = _clk.t      # 跟踪块的喂点（只认全局 y）
                    _last_gy = _gy
                    _s.loc_player_global = (_gx, _gy)
                    _s.is_on_ladder = False            # 日志里没有旗标列，取保守值
                    _fresh = (_clk.t - _s.t_ladder_move) < 1.0
                    if _my in ("up", "down"):
                        _n_elig += 1
                        if _fresh:
                            _n_fresh += 1
                        _nd = _s._rope_nudge_dir(_gx, _my)
                        if _nd:
                            _n_nudge += 1
                            _dirs[_nd] += 1
                            if _fresh:
                                _n_fresh_nudge += 1
                        # 僵持窗口 = 可蹭期（1s~7s 没动）的连续帧
                        _age = _clk.t - _s.t_ladder_move
                        if 1.0 <= _age <= 7.0:
                            if _run is None:
                                _run = [_clk.t, 0, 0]
                            _run[1] += 1
                            if _nd:
                                _run[2] += 1
                        elif _run is not None:
                            _stall_frames.append(tuple(_run))
                            _run = None
                    elif _run is not None:
                        _stall_frames.append(tuple(_run))
                        _run = None
                    _clk.t += 1.0 / 27.7
                if _run is not None:
                    _stall_frames.append(tuple(_run))
            finally:
                _mod.time = _real_time

            _stalls = [w for w in _stall_frames if w[1] >= 20]   # ≥0.7s 才算窗口
            _n_ok = sum(1 for w in _stalls if w[2] > 0)
            print(f"       日志 {os.path.basename(_path)}：状态采样 {len(_samples)} 条、"
                  f"回放帧里「路线要上下」{_n_elig} 帧、蹭了 {_n_nudge} 帧"
                  f"（left {_dirs['left']} / right {_dirs['right']}）、"
                  f"可蹭期窗口 {len(_stalls)} 个（其中 {_n_ok} 个真的蹭了）")
            check_true("回放样本够大（不是空跑）", _n_elig > 1000, f"{_n_elig} 帧")
            check_true("僵持窗口里真的会蹭（这正是用户 m08454 那两次卡楼梯）",
                       _n_nudge > 0 and _n_ok > 0,
                       f"蹭 {_n_nudge} 帧 / 窗口 {_n_ok}/{len(_stalls)}")
            check_true("左右两个方向都出现过（小地图 x 有抖动，单向扫不中）",
                       _dirs["left"] > 0 and _dirs["right"] > 0, str(_dirs))
            check_true("真在爬（新鲜戳）时一帧都不蹭 —— 绝不干扰正常爬梯",
                       _n_fresh_nudge == 0,
                       f"{_n_fresh_nudge} / {_n_fresh} 帧")

    print(f"\n===== verify_rope_nudge: 通过 {PASS} / 失败 {FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
