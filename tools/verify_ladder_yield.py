# -*- coding: utf-8 -*-
'''「梯上让路收紧」自检（2026-10-14 加，用户 m08016 拍板 A）。

背景（用户 m07230「记录下怪物的检测信息，现在的攻击还是不够灵敏」）：
    示范录制样本（`MapleStoryAutoLevelUp-CN/debug/demo/demo_20261014_143657.jsonl.gz`，
    6262 帧 / 226.3s / 27.7fps）读数：
      · 梯上帧 **2749（43.9%）**，而引擎梯上出手率 **0.0%**（平台上 16.5%）；
      · 那 2749 帧里 **75.5%** 路线根本没给 up/down（`bot.my == none`）；
      · 按段看：105 个梯上段里 **44~63 段整段小地图全局 y 零变化**（粘滞误判），
        人在这些帧里出手 **910 次**，全被让路吃掉。
    根因：让路判据 `bool(self.is_on_ladder) or cmd_move_y in (up, down)` 的第一项
    是一面**只进不出**的旗子（清除的两条出口都要横向移动）。

做法（本轮改动）：
    `_ladder_yield()` —— 唯一判据点，让路块只问它一次；`_ladder_still_s()` 读
    `route.ladder_still_s`（默认 0.7s）。旗标为真**且**最近窗口内小地图全局 y
    变过才让路；判据不可用（配置 ≤0 / 全局坐标读不到）⇒ fail-closed 退回老行为。
    跟踪块新增"竖向进展戳"（唯一喂点，只认全局 y 变化）。

本文件钉五件事：
  【1】`_ladder_still_s()` 真值表（缺键/None/畸形/0/负数/>30/全局坐标缺失）
  【2】`_ladder_yield()` 三路真值表 + 文案里带"为什么"（用户要能自己核对）
  【3】接线白盒：让路块**只**调 `_ladder_yield()`、老的一行判据已删、
        跟踪块是"竖向进展戳"的唯一喂点、`__init__` 三个字段在
  【4】配置 / 界面口径：出厂 0.7、中文名、`ADV_FIELD_HIDE['route']` 里收着
  【5】真机样本回归（有样本才跑）：老规则 vs 新规则在梯上帧的让路比例
'''
import glob
import gzip
import inspect
import json
import os
import sys
import time

import yaml

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


def stub(cfg=None, **kw):
    '''只绑本次两个新方法的假引擎（不跑 __init__）。'''
    class S:
        pass

    s = S()
    for _n in ("_ladder_still_s", "_ladder_yield"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg if cfg is not None else {}
    s.cmd_move_y = "none"
    s.cmd_move_x = "none"
    s.cmd_action = "none"
    s.is_on_ladder = False
    s.t_ladder_move = 0.0
    s._ladder_gy = None
    s.t_ladder_stale_log = 0.0
    s.loc_player_global = (100, 200)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def main():
    global PASS, FAIL

    print("【1】route.ladder_still_s 的真值表（0.0 = 本条判据不可用）")
    check("缺键 ⇒ 默认 0.7", round(stub({})._ladder_still_s(), 3), 0.7)
    check("None ⇒ 0.7", round(stub({"route": {"ladder_still_s": None}})._ladder_still_s(), 3), 0.7)
    check("字符串 '0.7' ⇒ 0.7", round(stub({"route": {"ladder_still_s": "0.7"}})._ladder_still_s(), 3), 0.7)
    check("1.5 ⇒ 1.5", round(stub({"route": {"ladder_still_s": 1.5}})._ladder_still_s(), 3), 1.5)
    check("0 ⇒ 0.0（用户明确关掉本层）",
          stub({"route": {"ladder_still_s": 0}})._ladder_still_s(), 0.0)
    check("-1 ⇒ 0.0", stub({"route": {"ladder_still_s": -1}})._ladder_still_s(), 0.0)
    check("31 ⇒ 0.0（明显写错，不猜）",
          stub({"route": {"ladder_still_s": 31}})._ladder_still_s(), 0.0)
    check("畸形 'abc' ⇒ 默认 0.7",
          round(stub({"route": {"ladder_still_s": "abc"}})._ladder_still_s(), 3), 0.7)
    check("全局坐标读不到 ⇒ 0.0（fail-closed）",
          stub({"route": {"ladder_still_s": 0.7}}, loc_player_global=None)._ladder_still_s(), 0.0)

    print("\n【2】_ladder_yield() 三路真值表")
    _s = stub({}, cmd_move_y="up")
    _y, _why = _s._ladder_yield()
    check("路线给 up ⇒ 让路", _y, True)
    check_true("文案里说得清是路线在爬", "路线指令 y=up" in _why, _why)
    _s = stub({}, cmd_move_y="down", is_on_ladder=True)
    _y, _why = _s._ladder_yield()
    check("路线给 down + 旗标 ⇒ 让路", _y, True)
    check_true("文案两处都提到", "路线指令 y=down" in _why and "is_on_ladder" in _why, _why)

    _s = stub({}, cmd_move_y="none", is_on_ladder=False)
    check("不下梯、没路线 ⇒ 不让路（怪逻辑照跑）", _s._ladder_yield()[0], False)
    check("不让路时没有理由文案", _s._ladder_yield()[1], "")

    _s = stub({}, is_on_ladder=True, t_ladder_move=time.time() - 0.1)
    _y, _why = _s._ladder_yield()
    check("旗标 + 刚有竖向移动 ⇒ 让路", _y, True)
    check_true("文案给的是「多久没动」的可核对数字", "ms 内有竖向移动" in _why, _why)

    _s = stub({}, is_on_ladder=True, t_ladder_move=time.time() - 2.0)
    _y, _why = _s._ladder_yield()
    check("旗标粘着（2s 没动）⇒ **不让路**", _y, False)
    check_true("理由里带「旗标粘着」", "旗标粘着" in _why, _why)

    _s = stub({"route": {"ladder_still_s": 0}}, is_on_ladder=True,
              t_ladder_move=time.time() - 600)
    _y, _why = _s._ladder_yield()
    check("配置 0（关掉本层）⇒ 退回老行为：仍让路", _y, True)
    check_true("理由说明是 fail-closed", "fail-closed" in _why, _why)

    _s = stub({}, is_on_ladder=True, t_ladder_move=time.time() - 600,
              loc_player_global=None)
    check("全局坐标读不到 ⇒ 仍让路（不许把角色吊在绳上）",
          _s._ladder_yield()[0], True)

    print("\n【3】接线白盒（源码断言）")
    _src = inspect.getsource(MapleStoryAutoBot)
    _yield_src = inspect.getsource(MapleStoryAutoBot.update_cmd_by_mob_detection)
    check_true("让路块只问 _ladder_yield() 一次",
               _yield_src.count("self._ladder_yield()") == 1)
    check_true("老的一行判据（is_on_ladder or cmd_move_y）已不在",
               "bool(self.is_on_ladder) or str(self.cmd_move_y)" not in _yield_src)
    check_true("让路日志走 _why（不再自己拼分支）",
               'f"[绳/梯] 判定正在爬（{_why}）→ 怪逻辑让路："' in _yield_src)
    check_true("新增「不再让路」这行归因（用户能看出是哪条生效）",
               "不再让路" in _yield_src and "route.ladder_still_s` 填 0" in _yield_src)
    # ⚠️ 2026-10-14 更新（D 绳梯自救落地后）：戳现在有**两处**写入点，
    #   第二处在 `_rope_nudge_dir()` 里 —— 那是"从没记过竖向进展"时的**起表**，
    #   否则 `time.time() - 0.0` 是个天文数字，蹭的判据会一上来就"超时放弃"。
    #   所以这里钉的是"跟踪块那个喂点唯一 + 另一处只许在 _rope_nudge_dir 里"。
    check_true("竖向进展戳的喂点唯一（跟踪块）",
               _src.count("self.t_ladder_move = time.time()") == 2)
    check_true("……第二处起表只许在 _rope_nudge_dir() 里（D 的起表，不是乱写）",
               "self.t_ladder_move = time.time()" in
               _src.split("def _rope_nudge_dir")[1]
               .split("def update_cmd_by_mob_detection")[0])
    check_true("喂点只看全局 y（_ladder_gy 比较）",
               "self._ladder_gy is not None" in _src and "_gy_now != self._ladder_gy" in _src)
    check_true("不改旗标本身（不因为它过期就把 is_on_ladder 清掉）",
               "self.is_on_ladder = False" not in _src.split("def _ladder_yield")[1]
               .split("def update_cmd_by_mob_detection")[0])
    for _f in ("self.t_ladder_move = 0.0", "self._ladder_gy = None",
               "self.t_ladder_stale_log = 0.0"):
        check_true(f"__init__ 里有 {_f}", _f in _src)

    print("\n【4】配置 / 界面口径")
    with open(os.path.join(_ROOT, "config", "config_default.yaml"), encoding="utf-8") as _fh:
        _cfg = yaml.safe_load(_fh)
    check("出厂 route.ladder_still_s = 0.7", _cfg["route"].get("ladder_still_s"), 0.7)
    from src.utils.cfg_names import cn_key                          # noqa: E402
    _cn = cn_key("route", "ladder_still_s")
    check_true("有中文名（日志点名这个键，用户去 yaml 里能对上）",
               _cn != "ladder_still_s" and "让路" in _cn, _cn)
    import src.ui.ui as _ui                                        # noqa: E402
    check_true("界面高级设置里收着（算法判据，放出来只会把爬梯搞坏）",
               "ladder_still_s" in _ui.ADV_FIELD_HIDE.get("route", []))

    print("\n【5】真机样本回归（老规则 vs 新规则）")
    _files = sorted(glob.glob(os.path.join(_ROOT, "debug", "demo", "demo_*.jsonl*")))
    if not _files:
        print("  [SKIP] 没有示范录制样本（debug/demo/demo_*.jsonl.gz）——"
              "本段只在有样本时才算证据")
    else:
        _path = _files[-1]
        _recs = []
        with (gzip.open(_path, "rt", encoding="utf-8")
              if _path.endswith(".gz") else open(_path, encoding="utf-8")) as _fh:
            for _ln in _fh:
                if _ln.strip():
                    _recs.append(json.loads(_ln))

        # 假时钟：`_ladder_yield()` 比的是"距上次竖向移动多少秒"，
        # 一次回放只花不到 1 秒真实时间 ⇒ 必须让时间按帧推进（27.7fps），
        # 否则每一帧都算"刚刚动过"（第一版就踩了这个坑，读数 99.9%）。
        import src.engine.MapleStoryAutoLevelUp as _mod         # noqa: E402
        _real_time = time

        class _Clock:
            def __init__(self, t0):
                self.t = t0

            def time(self):
                return self.t

            def __getattr__(self, _name):
                return getattr(_real_time, _name)

        _clk = _Clock(1000.0)
        _mod.time = _clk
        try:
            _s = stub({})
            _last_gy = None
            _stamp = -1e9            # 老行为：一开始就当"从没动过"
            _n_ladder = _old_yield = _new_yield = 0
            for _r in _recs:
                _g = _r.get("global")
                if _g:
                    _gy = int(_g[1])
                    if _last_gy is not None and _gy != _last_gy:
                        _stamp = _clk.t   # 跟踪块的喂点（只认全局 y 变化）
                    _last_gy = _gy
                _lad = bool(_r.get("ladder"))
                _my = str(((_r.get("bot") or {}).get("my")) or "none")
                _s.is_on_ladder = _lad
                _s.cmd_move_y = _my
                _s.t_ladder_move = _stamp
                _s.loc_player_global = (int(_g[0]), int(_g[1])) if _g else None
                _old = _lad or _my in ("up", "down")
                _new = _s._ladder_yield()[0]
                if _lad:
                    _n_ladder += 1
                    if _old:
                        _old_yield += 1
                    if _new:
                        _new_yield += 1
                _clk.t += 1.0 / 27.7
        finally:
            _mod.time = _real_time
        _ratio = _new_yield / float(max(1, _n_ladder))
        print(f"       样本 {os.path.basename(_path)}：梯上帧 {_n_ladder}、"
              f"老规则让路 {_old_yield}、新规则让路 {_new_yield}（{_ratio:.1%}）")
        check("老规则：梯上帧一律让路", _old_yield, _n_ladder)
        check_true("新规则：让路明显减少（粘滞帧放出来）", _new_yield < _old_yield * 0.9,
                   f"{_new_yield} < {int(_old_yield * 0.9)}")
        # 读数（2026-10-14 那份样本）：梯上帧 2749 → 新规则让路 1397（50.8%），
        #   其中真爬段 71.5% 仍让路、粘滞段只剩 18.1% 让路（老规则两者都是 100%）。
        #   上下界放宽到 35%~95%：换一份样本（真人打法不同）不该把自检搞红，
        #   这条钉的是"**明显**少了、但没放成 0"。
        check_true("新规则：真在爬的帧仍让路（不能放太狠）", 0.35 <= _ratio <= 0.95,
                   f"让路率 {_ratio:.1%} 应在 35%~95%")

    print(f"\n===== verify_ladder_yield: 通过 {PASS} / 失败 {FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
