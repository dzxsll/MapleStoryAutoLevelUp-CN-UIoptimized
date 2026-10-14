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
    '''只绑本次这几个新方法的假引擎（不跑 __init__）。'''
    class S:
        pass

    s = S()
    for _n in ("_ladder_still_s", "_ladder_yield",
               "_rope_stuck_s", "_rope_giveup_now", "_rope_ladder_giveup_tick",
               # P4（2026-10-14）：give-up 那一刻要记一笔账 ⇒ 桩也必须绑上，
               # 否则 give-up 一触发就是 AttributeError（本次就是这么红的）
               "_budget_note_lost",
               # 爬梯走完（2026-10-15，用户 m11669）：路线侧每帧都会问一次
               # （update_cmd_by_route 里无条件调用）⇒ 桩漏绑就是 AttributeError
               "_ladder_finish_cfg", "_ladder_finish_tick"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg if cfg is not None else {}
    s.cmd_move_y = "none"
    s.cmd_move_x = "none"
    s.cmd_action = "none"
    s.is_on_ladder = False
    s.t_ladder_move = 0.0
    s._ladder_gy = None
    s.t_ladder_stale_log = 0.0
    s._last_vert_cmd = "up"
    s._ladder_push_t0 = 0.0
    s._ladder_push_cool = 0.0
    s.t_ladder_finish_log = 0.0
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

    print("\n【2b】route.rope_stuck_s 的真值表（0.0 = 本层关掉）")
    check("缺键 ⇒ 默认 8.0", round(stub({})._rope_stuck_s(), 3), 8.0)
    check("None ⇒ 8.0", round(stub({"route": {"rope_stuck_s": None}})._rope_stuck_s(), 3), 8.0)
    check("字符串 '8' ⇒ 8.0",
          round(stub({"route": {"rope_stuck_s": "8"}})._rope_stuck_s(), 3), 8.0)
    check("3 ⇒ 3.0", round(stub({"route": {"rope_stuck_s": 3}})._rope_stuck_s(), 3), 3.0)
    check("0 ⇒ 0.0（用户明确关掉本层）",
          stub({"route": {"rope_stuck_s": 0}})._rope_stuck_s(), 0.0)
    check("-1 ⇒ 0.0", stub({"route": {"rope_stuck_s": -1}})._rope_stuck_s(), 0.0)
    check("121 ⇒ 0.0（明显写错，不猜）",
          stub({"route": {"rope_stuck_s": 121}})._rope_stuck_s(), 0.0)
    check("畸形 'abc' ⇒ 默认 8.0",
          round(stub({"route": {"rope_stuck_s": "abc"}})._rope_stuck_s(), 3), 8.0)
    check("全局坐标读不到 ⇒ 0.0（fail-closed：测不到进展就不敢替用户放弃）",
          stub({"route": {"rope_stuck_s": 8}}, loc_player_global=None)._rope_stuck_s(), 0.0)

    print("\n【2c】爬绳 give-up：≥8s 没进展 ⇒ 暂停爬绳（怪逻辑恢复）")
    _now = time.time()
    # (a) 才爬 3s ⇒ 照旧让路（老行为不变）
    _s = stub({}, cmd_move_y="up", _rope_req_t0=_now - 3.0, _rope_req_cmd="up",
              t_ladder_move=_now - 3.0)
    check("路线要 up、才 3s 没进展 ⇒ 仍让路（老行为不变）", _s._ladder_yield()[0], True)
    check("……且还没进 give-up 窗口", _s._rope_giveup_now(), False)
    # (b) 爬了 9s 还没动 ⇒ 放弃
    _s = stub({}, cmd_move_y="up", _rope_req_t0=_now - 9.0, _rope_req_cmd="up",
              t_ladder_move=_now - 9.0)
    _y, _why = _s._ladder_yield()
    check("路线要 up、9s 没进展 ⇒ **不让路**（那 17% 的静音在这条上解掉）", _y, False)
    check_true("……并且真的进了 give-up 窗口", _s._rope_giveup_now())
    check("……理由文案为空（让路块据此不再打印「让路」）", _why, "")
    check("……窗口长度 = rope_stuck_s（8s）",
          round(float(_s._rope_giveup_until) - _now, 1), 8.0)
    # (c) 窗口内再问 ⇒ 仍不让路
    check("窗口内再问 ⇒ 仍不让路", _s._ladder_yield()[0], False)
    # (d) 窗口里真出现竖向进展 ⇒ 立刻取消（绝不在人已经爬上去时抢控制权）
    _s.t_ladder_move = time.time()
    check("暂停期间出现竖向进展 ⇒ 立刻取消暂停（控制权还给路线）",
          _s._ladder_yield()[0], True)
    check("……窗口已清零", _s._rope_giveup_now(), False)
    # (e) 暂停结束后重新起表
    _now2 = time.time()
    _s = stub({}, cmd_move_y="up", _rope_req_t0=_now2, _rope_req_cmd="up",
              t_ladder_move=_now2 - 100.0, _rope_giveup_until=_now2 - 1.0)
    check("暂停结束后重新起表 ⇒ 先让路（新的一次尝试）", _s._ladder_yield()[0], True)
    # (f) 关掉本层 ⇒ 无论多久都让路
    _s = stub({"route": {"rope_stuck_s": 0}}, cmd_move_y="up",
              _rope_req_t0=_now - 600.0, _rope_req_cmd="up", t_ladder_move=_now - 600.0)
    check("rope_stuck_s=0（关掉本层）⇒ 600s 也让路（旧行为）", _s._ladder_yield()[0], True)
    check("……且不建窗口", _s._rope_giveup_now(), False)
    # (g) 真在爬（竖向进展新鲜）⇒ 绝不打断
    _s = stub({}, cmd_move_y="up", is_on_ladder=True, _rope_req_t0=_now - 9.0,
              _rope_req_cmd="up", t_ladder_move=_now - 0.1)
    check("在梯子上且刚有竖向进展 ⇒ 让路（绝不因超时把爬梯打断）",
          _s._ladder_yield()[0], True)

    print("\n【2d】爬梯走完 route.ladder_finish：在梯上、路线没给上下 ⇒ 补竖向")
    # 背景（用户 m11669「上梯子要走完，不要检测怪了」+ 真机取证）：梯子上按左右
    # **不动**，而路线这帧可能只给横向像素 ⇒ 键在发、人不动。补上竖向之后
    # `_ladder_yield()` 的"路线给 up/down ⇒ 始终让路"自然生效 ⇒ 怪逻辑静音。
    _s = stub({}, is_on_ladder=False)
    check("不在梯子上 ⇒ 不补（本层只信 is_on_ladder）", _s._ladder_finish_tick(), "")
    _s = stub({}, is_on_ladder=True, cmd_move_y="up")
    check("路线自己给了 up ⇒ 不补（不让路那条已经管了）", _s._ladder_finish_tick(), "")
    _s = stub({}, is_on_ladder=True, cmd_move_y="none", cmd_move_x="right",
              cmd_action="none")
    check("★在梯上、路线这帧只给横向（right none none）⇒ 补 up",
          _s._ladder_finish_tick(), "up")
    check_true("……并且真的起了表（超时判据要用）", _s._ladder_push_t0 > 0.0)
    _s = stub({}, is_on_ladder=True, _last_vert_cmd="down")
    check("方向跟随路线最近一次竖向（down）⇒ 补 down", _s._ladder_finish_tick(), "down")
    _s = stub({}, is_on_ladder=True, _last_vert_cmd="啥也不是")
    check("方向值畸形 ⇒ 退回 up（不猜）", _s._ladder_finish_tick(), "up")
    _s = stub({}, is_on_ladder=True, _rope_giveup_until=time.time() + 3.0)
    check("P0 的爬绳放弃窗口里 ⇒ 不抢（那几秒归 route.rope_stuck_s）",
          _s._ladder_finish_tick(), "")
    _now = time.time()
    _s = stub({}, is_on_ladder=True, _ladder_push_t0=_now - 7.0,
              t_ladder_move=_now - 100.0)
    check("补了 7s、全局 y 一直没动 ⇒ 停手（按上去也没用）",
          _s._ladder_finish_tick(), "")
    check("……冷却 = 同样长的时间（6s）", round(_s._ladder_push_cool - _now, 1), 6.0)
    check("……起表清零（下次重新算）", _s._ladder_push_t0, 0.0)
    check("……冷却期内再问也不补", _s._ladder_finish_tick(), "")
    _s = stub({}, is_on_ladder=True, _ladder_push_t0=_now - 30.0,
              t_ladder_move=_now - 0.5)
    check("补了 30s 但刚有竖向进展 ⇒ 继续补（真在爬就绝不撒手）",
          _s._ladder_finish_tick(), "up")
    _s = stub({"route": {"ladder_finish": False}}, is_on_ladder=True)
    check("ladder_finish: false ⇒ 不补（逐字退回老行为）",
          _s._ladder_finish_tick(), "")
    _s = stub({"route": {"ladder_finish_s": 0}}, is_on_ladder=True)
    check("ladder_finish_s: 0 ⇒ 不补", _s._ladder_finish_tick(), "")
    check("参数缺键 ⇒ (True, 6.0)", stub({})._ladder_finish_cfg(), (True, 6.0))
    check("参数 NaN ⇒ (True, 6.0)",
          stub({"route": {"ladder_finish_s": float("nan")}})._ladder_finish_cfg(),
          (True, 6.0))
    check("参数 121 ⇒ (True, 6.0)（明显写错，不猜）",
          stub({"route": {"ladder_finish_s": 121}})._ladder_finish_cfg(), (True, 6.0))
    check("参数 '3.5' ⇒ (True, 3.5)",
          stub({"route": {"ladder_finish_s": "3.5"}})._ladder_finish_cfg(), (True, 3.5))

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

    # ★ 2026-10-14（P0，用户 m10830）：爬绳超时 ⇒ 暂停爬绳 + 怪逻辑恢复。
    check_true("_rope_stuck_s 全文件只定义一次",
               _src.count("def _rope_stuck_s(self):") == 1)
    check_true("_rope_ladder_giveup_tick 全文件只定义一次",
               _src.count("def _rope_ladder_giveup_tick(self):") == 1)
    check_true("状态机每帧只被 _ladder_yield() 问一次（唯一写状态处）",
               _src.count("self._rope_ladder_giveup_tick()") == 1)
    check_true("让路块里 give-up 会真的打断让路",
               "if _giveup:" in _src and "把控制权交回怪逻辑" in _src)
    _route_src = inspect.getsource(MapleStoryAutoBot.update_cmd_by_route)
    check_true("路线侧真把 up/down 压成 none（不只是让路，也不死按着键）",
               "self._rope_giveup_now()" in _route_src
               and 'self.cmd_move_y = "none"' in
               _route_src.split("self._rope_giveup_now()")[1][:200])
    check_true("压的只是竖向（横向/动作照旧，不许把整条指令清掉）",
               'self.cmd_move_x = "none"' not in
               _route_src.split("self._rope_giveup_now()")[1][:200])
    check_true("日志点名怎么关掉（route.rope_stuck_s 填 0）",
               "`route.rope_stuck_s` 填 0 = 关掉本层" in _src)
    check_true("give-up 是可核对的（日志带「已 Ns、期间没有竖向进展」）",
               "期间没有竖向进展" in _src and "判定这根绳上不去" in _src)

    # ★ 2026-10-15（爬梯走完，用户 m11669）：路线侧替路线补竖向。
    check_true("_ladder_finish_tick 全文件只定义一次",
               _src.count("def _ladder_finish_tick(self):") == 1)
    check_true("_ladder_finish_cfg 是读这两个键的唯一入口",
               _src.count("def _ladder_finish_cfg(self):") == 1)
    check_true("路线侧真把竖向补上（不只是判据、不是写了不接）",
               "self._ladder_finish_tick()" in _route_src
               and "self.cmd_move_y = _ladder_push" in _route_src)
    check_true("补的只是竖向（不许顺手改横向/动作）",
               "self.cmd_move_x = _ladder_push" not in _route_src
               and "self.cmd_action = _ladder_push" not in _route_src)
    check_true("只记「路线最近一次给的竖向」（补的时候要用）",
               "self._last_vert_cmd = str(self.cmd_move_y)" in _route_src)
    _fin_src = _src.split("def _ladder_finish_tick")[1].split(
        "def update_cmd_by_mob_detection")[0]
    check_true("与 P0 的窗口不打架（rope_stuck_s 生效那几秒本层不抢）",
               "_rope_giveup_now()" in _fin_src)
    check_true("超时会冷却（不在梯子顶端把横向走位一直压住）",
               "_ladder_push_cool" in _fin_src and "冷却" in _fin_src)
    check_true("日志点名怎么关掉（route.ladder_finish_s 填 0）",
               "`route.ladder_finish_s` 填 0 = 关掉本层" in _src)
    for _f in ('self._last_vert_cmd = "up"', "self._ladder_push_t0 = 0.0",
               "self._ladder_push_cool = 0.0", "self.t_ladder_finish_log = 0.0"):
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
    check("出厂 route.rope_stuck_s = 8.0", _cfg["route"].get("rope_stuck_s"), 8.0)
    _cn2 = cn_key("route", "rope_stuck_s")
    check_true("有中文名（[爬绳] 那行点名这个键，用户去 yaml 里能对上）",
               _cn2 != "rope_stuck_s" and "爬绳" in _cn2, _cn2)
    check_true("界面高级设置里也收着（同 rope_nudge_* 口径：算法判据）",
               "rope_stuck_s" in _ui.ADV_FIELD_HIDE.get("route", []))
    check_true("出厂注释块写清了判据与关掉的办法",
               "rope_stuck_s" in open(os.path.join(_ROOT, "config", "config_default.yaml"),
                                      encoding="utf-8").read())
    _yaml_txt = open(os.path.join(_ROOT, "config", "config_default.yaml"),
                     encoding="utf-8").read()
    check("出厂 route.ladder_finish = True", _cfg["route"].get("ladder_finish"), True)
    check("出厂 route.ladder_finish_s = 6.0", _cfg["route"].get("ladder_finish_s"), 6.0)
    _cn3 = cn_key("route", "ladder_finish")
    check_true("有中文名（[爬梯] 那行点名这个键，用户去 yaml 里能对上）",
               _cn3 != "ladder_finish" and "爬梯" in _cn3, _cn3)
    _cn4 = cn_key("route", "ladder_finish_s")
    check_true("秒数也有中文名", _cn4 != "ladder_finish_s" and "爬梯" in _cn4, _cn4)
    check_true("界面高级设置里也收着（同上口径：算法判据）",
               "ladder_finish" in _ui.ADV_FIELD_HIDE.get("route", [])
               and "ladder_finish_s" in _ui.ADV_FIELD_HIDE.get("route", []))
    check_true("出厂注释块写清了机制与真机取证",
               "ladder_finish_s: 6.0" in _yaml_txt and "竖向 0/6 帧" in _yaml_txt)

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
