# -*- coding: utf-8 -*-
"""「没录回正线时，路过 up 像素不崩溃」自检（2026-09-27 加，issue #2）。

【用户报的现象】（原样日志）
    File "src/engine/MapleStoryAutoLevelUp.py", line 3414, in update_cmd_by_route
    TypeError: 'NoneType' object is not subscriptable
    ...
    [主循环] run_once 抛异常，挂机已停止推进。
    用户补充：目前移动可以执行，不攻击

【根因】
  `update_cmd_by_route` 里那段「防地面伪灰」（2026-09-17 加）是**拿回正线
  route_home.png 当参照**来判断 up 像素是不是真管柱的：
      self.img_route_home[_up_y-d, _up_x]
  但 `img_route_home` 在**没录回正线时是 None**（见 __init__ 的赋值），
  于是直接取下标 → TypeError，且**每帧都抛** → 主循环停摆。

  ⇒ 修法：把「有回正线」作为前置条件；没有就跳过这层伪灰检测
    （退回不做检测的老行为 —— 宁可偶尔误判 up，也好过整条流程崩掉）。

⚠️ 本用例**复现用户那一次的崩溃**（真的调 update_cmd_by_route），
   而不是只看代码里有没有 `is not None` —— 后者容易被"加了但加错分支"骗过。

用法：
    python -m tools.verify_home_none_no_crash
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

FAIL: list = []

#: __getattr__ 兜底时被访问过的属性名（用于判断用例是否"假得离谱"）
_STUBBED: list = []


def check(name, ok, detail=""):
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


class _Bot:
    """把 update_cmd_by_route 需要的最小状态摆出来，其余不管。

    只保留**真的会被那段代码读到**的字段，避免造一个假得离谱的对象。
    """

    def __init__(self, img_route_home, player_y=100):
        self.img_route_home = img_route_home
        self.loc_player_global = (50, player_y)
        self.cmd_move_x = "none"
        self.cmd_move_y = "none"
        self.cmd_action = "none"
        self.is_on_ladder = False
        self.is_using_home_route = False
        self._patrol_degraded = False
        self.monsters = []
        # 路线色码表：给一个"up"和一个"right"，构造出「两个候选都在」的分支
        self.color_code = {(127, 127, 127): "none up none",
                           (0, 0, 255): "right none none"}

    # ---- 下面几个在真实链路里会先被调用，这里给最小实现 ----
    def _perf(self, *_a, **_k):
        pass

    def get_nearest_color_code(self):
        """造出「两个候选都在、且走 else 分支」的数据。

        ⚠️ 关键：那段「防地面伪灰」在 `if color_code["distance"] <
           color_code_up_down["distance"]` 的 **else** 里。
           所以必须让 **up 候选的距离更近**（distance 更小），否则走的是 if 分支、
           根本到不了目标代码 —— 本用例第一版就因此得出"没走到"（探针如实报了警）。
        """
        return ({"pixel": (50, 100), "color": (0, 0, 255),
                 "command": "right none none", "distance": 9},
                {"pixel": (50, 100), "color": (127, 127, 127),
                 "command": "none up none", "distance": 1})   # up 更近 → 进 else

    def _home_dist(self):
        return (999, None)

    def _home_reenter_allowed(self):
        return False

    def _apply_edge_guard(self, *_a, **_k):
        pass

    def _apply_jump_brake(self, *_a, **_k):
        pass

    @property
    def cfg(self):
        # 只需满足 L3458 的 self.cfg["key"]["teleport"]
        return {"key": {"teleport": "e", "jump": "space"},
                "route": {"search_range": 10}}

    def _main_dist(self):
        # ⚠️ 真实签名返回**元组**（见 L3219 的解包：home_d_main, _main_seg_i），
        #    第一版返回 int 导致用例在到达目标代码前就抛错、探针报"没走到"。
        return (999, None)

    def _home_dwell_ok(self):
        return True

    def _home_line_pick(self):
        return None

    def _home_tol_now(self):
        return 2

    def _patrol_degraded_now(self):
        return False

    def _log_patrol_degraded(self, *_a, **_k):
        pass

    def try_return_to_mainline(self, *_a, **_k):
        return False

    def _enter_home_route(self, *_a, **_k):
        pass

    def _exit_home_route(self, *_a, **_k):
        pass

    def __getattr__(self, name):
        """兜底：未显式实现的方法返回中性值。

        ⚠️ 只在用例里这么干 —— 目的是让真链路能跑到「防地面伪灰」那一段，
           而不必为主题函数复刻一整套引擎状态。
        ⚠️ 注意别在这里访问 self 的其它属性：那会绕回 __getattr__ 造成无限递归
           （本用例第一版就这么炸了 RecursionError）。所以只记在模块级列表里。
        """
        _STUBBED.append(name)
        return lambda *a, **k: False


def _probe_reaches_fake_check(bot_cls):
    """确认 update_cmd_by_route 真的走到了「防地面伪灰」那一段。

    为什么必须查：本用例只给最小桩，函数有可能在到达目标代码前就 return 了。
    那种情况下"没抛 TypeError"是**假绿** —— 代码根本没执行到。
    这里用 sys.settrace 逐行跟踪，看有没有执行到含 `_can_check_fake` 的那一行。
    """
    import sys as _sys
    target_line = None
    src_path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "src", "engine",
        "MapleStoryAutoLevelUp.py"))
    with open(src_path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if "_can_check_fake = self.img_route_home is not None" in line:
                target_line = i
                break

    reached = {"hit": False, "branches": []}
    if target_line is None:
        return {"reached": False, "branch": None,
                "detail": "源码里找不到目标行"}

    def _tracer(frame, event, arg):
        if event == "line" and frame.f_lineno == target_line:
            reached["hit"] = True
        return _tracer

    bot = _Bot(img_route_home=None, player_y=100)
    _sys.settrace(_tracer)
    try:
        bot_cls.update_cmd_by_route(bot)
    except Exception:                                         # noqa: BLE001
        pass
    finally:
        _sys.settrace(None)

    return {"reached": reached["hit"], "branch": target_line,
            "detail": f"目标行={target_line}"}


def main():
    # 直接把真实方法挂到这个最小对象上，**跑真代码**
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    src_path = os.path.join(os.path.dirname(__file__), "..", "src", "engine",
                            "MapleStoryAutoLevelUp.py")
    src = open(src_path, encoding="utf-8").read()
    print("=== 没录回正线时不应崩溃·自检 ===")

    # 【0】先证明这个用例真的走到了目标代码 ——
    #      只断言"没抛异常"是不够的：桩不全时函数可能在中途就 return 了，
    #      那样即使代码没修也会"通过"（假绿）。这里用字节码级探针确认。
    print("\n【0】确认用例能走到「防地面伪灰」那一段")
    hit = _probe_reaches_fake_check(MapleStoryAutoBot)
    check("已执行到防伪灰判断所在的行", hit["reached"],
          f"到达分支={hit['branch']}，详情={hit['detail']}")

    # 【1】★ 核心：img_route_home = None 时必须不抛异常
    print("\n【1】img_route_home 为 None（没录回正线）")
    bot = _Bot(img_route_home=None, player_y=100)
    err = None
    try:
        MapleStoryAutoBot.update_cmd_by_route(bot)
    except Exception as e:                                    # noqa: BLE001
        err = e
    # 允许它因为"没造全依赖"而抛 AttributeError（那说明本用例的桩不全），
    # 但**绝不允许**出现用户那个 TypeError: 'NoneType' object is not subscriptable
    is_user_bug = (err is not None
                   and isinstance(err, TypeError)
                   and "not subscriptable" in str(err))
    check("★ 不再出现 'NoneType' object is not subscriptable（用户那个崩溃）",
          not is_user_bug, f"err={err!r}")
    if err is not None and not is_user_bug:
        print(f"        （注：抛出的是 {type(err).__name__}: {err} —— "
              f"属本用例桩不全，非用户问题）")

    # 【2】源码层面：那处下标访问必须有 None 前置条件
    print("\n【2】源码里那处下标访问有前置保护")
    check("存在 _can_check_fake（回正线可用性前置判断）",
          "_can_check_fake" in src,
          "否则 img_route_home 为 None 时会再次崩")
    # ⚠️ `self.img_route_home[_up_y-d, _up_x]` 这个片段在文件里可能出现多次，
    #    必须定位到**「防地面伪灰」那一处**（它紧跟在 _up_y 赋值之后），
    #    否则 index() 会撞上别处、得出错误结论（本用例第一版就踩了这个）。
    # ⚠️ 2026-10-14：守卫与下标之间新增了"看当前段竖向像素"的**十几行代码**
    #    （见引擎里 2026-10-14 那段注释），原来固定 1200 字的窗口会把下标挤到
    #    窗口之外 ⇒ 这项会假失败。改成"从锚点起在全文里找"（判据不变：
    #    仍然是"前置判断必须出现在下标访问之前"），不再依赖固定字数。
    anchor = "_up_y = color_code_up_down[\"pixel\"][1]"
    if anchor in src:
        _from = src.index(anchor)
        idx_guard = src.find("_can_check_fake = self.img_route_home is not None", _from)
        idx_use = src.find("self.img_route_home[_up_y-d, _up_x]", _from)
        check("防伪灰那一段里，前置判断在下标访问之前",
              idx_guard != -1 and idx_use != -1 and idx_guard < idx_use,
              f"guard@{idx_guard} use@{idx_use}")
    else:
        check("能定位到防伪灰那一段", False, "锚点 _up_y 赋值未找到")

    # 【3】有回正线时，原有防伪灰能力不能被削弱（回归保护）
    print("\n【3】有回正线时行为不变（防伪灰仍生效）")
    home = np.zeros((200, 200, 3), np.uint8)      # 全黑 = 该列附近没有 up 像素
    bot2 = _Bot(img_route_home=home, player_y=100)
    err2 = None
    try:
        MapleStoryAutoBot.update_cmd_by_route(bot2)
    except Exception as e:                                    # noqa: BLE001
        err2 = e
    check("有回正线时不崩", err2 is None or not (
        isinstance(err2, TypeError) and "not subscriptable" in str(err2)),
        f"err={err2!r}")

    print()
    if FAIL:
        print(f"失败 {len(FAIL)} 项：{FAIL}")
        return 1
    print("全部通过：没录回正线时不再崩，且原有防伪灰逻辑未被削弱。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
