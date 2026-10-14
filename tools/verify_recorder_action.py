# -*- coding: utf-8 -*-
'''录制器「按键 → 动作 → 落笔」的自检（2026-09-17 加）。

为什么单独一个脚本：录制器有**两个全程静默**的 bug，都是真机跑起来才暴露：
  ① 判定跳跃**写死 "space"** → config 的 `key.jump` 不是空格时，
     录制时一次跳都记不下来，线里 0 个 jump 像素 ⇒ 「爬不上管子」。
  ② 跳跃圆点被**下一帧的移动线盖掉** → 用户"边跳边按上"时，
     同一像素上后画的灰线把刚画的跳跃色盖成灰。

这类 bug 不钉成用例，下次改录制器还会犯。

⚠️ 用例一律调**真实入口**（`action_from_keys` / `paint_step`），不自己复刻
   "怎么判定、怎么画" —— 复刻一遍就测不到真东西了（项目纪律）。

用法：
    python -m tools.verify_recorder_action
'''
from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml                      # noqa: E402
from tools.routeRecorder import RouteRecorder               # noqa: E402

FAIL: list = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def new_recorder(jump_key="space", teleport_key=None):
    '''造一个**只带必要字段**的录制器（不跑 GUI / 不连游戏）。

    teleport_key=None ⇒ 保留 config_default.yaml 里的原值（"e"，正是老的写死值），
    这样"没设过 key.teleport"的老用例行为不变。
    '''
    cfg = load_yaml("config/config_default.yaml")
    cfg.setdefault("key", {})["jump"] = jump_key
    if teleport_key is not None:
        cfg.setdefault("key", {})["teleport"] = teleport_key
    r = RouteRecorder.__new__(RouteRecorder)
    r.cfg = cfg
    # ⚠️ 必须和真实录制器一致：**两张表合并**进 self.color_code
    #    （见 routeRecorder.py：`self.color_code.update(color_code_up_down)`）——
    #    不合并的话 "none up none"（灰）查不到色，paint_step 会直接 return，
    #    测不出"灰线盖掉跳跃色"这回事。
    r.color_code = {}
    for k, v in cfg["route"]["color_code"].items():
        r.color_code[tuple(int(p) for p in k.split(","))] = v
    for k, v in cfg["route"]["color_code_up_down"].items():
        r.color_code[tuple(int(p) for p in k.split(","))] = v
    r.color_code_up_down = {}
    for k, v in cfg["route"]["color_code_up_down"].items():
        r.color_code_up_down[tuple(int(p) for p in k.split(","))] = v
    r.img_map = np.zeros((80, 120, 3), dtype=np.uint8)
    r.img_route = np.zeros((80, 120, 3), dtype=np.uint8)
    r.loc_player_global = (60, 40)
    r.loc_player_global_last = None
    r._blob_pixels = {}
    r._route1_start = None
    r.idx_routes = 0
    r._home_recording = False
    r.t_last_draw_blob = 0.0
    return r


print("=" * 68)
print("录制器：按键 → 动作 → 落笔 · 自检")
print("=" * 68)

# ══════════════════════════════════════════════════════════════════════
print("\n【1】基础动作映射（跳跃键 = space，上游默认）")
# ══════════════════════════════════════════════════════════════════════
r = new_recorder("space")
check("只按右 → right", r.action_from_keys(["right"]), ("right none none", False))
check("只按左 → left", r.action_from_keys(["left"]), ("left none none", False))
check("只按上 → up", r.action_from_keys(["up"]), ("none up none", False))
check("只按下 → down", r.action_from_keys(["down"]), ("none down none", False))
check("什么都不按 → 空（不画）", r.action_from_keys([]), ("", False))
check("space+右 → 右跳（画团）",
      r.action_from_keys(["space", "right"]), ("right none jump", True))
check("space+左 → 左跳（画团）",
      r.action_from_keys(["space", "left"]), ("left none jump", True))
check("space 单独 → 原地跳（画团）",
      r.action_from_keys(["space"]), ("none none jump", True))
check("space+右+上 → 右跳优先（up 被吞，色码表没有 上+跳）",
      r.action_from_keys(["space", "right", "up"]), ("right none jump", True))
check("space+上 → 原地跳（边跳边按上的退化）",
      r.action_from_keys(["space", "up"]), ("none none jump", True))

# ══════════════════════════════════════════════════════════════════════
print("\n【2】★ 跳跃键**不是**空格时也要认（2026-09-17 修的 bug ①）")
# ══════════════════════════════════════════════════════════════════════
# 真机反例：config key.jump = c，用户"边按右、边跳、边按上"爬管子，
#   旧版 `if "space" in key_press` 永远不成立 → 记成 "none up none"（灰），
#   线里 **0 个 jump 像素** ⇒ 机器人走到管柱底下只会按 up ⇒ 爬不上去。
rc = new_recorder("c")
check("c 是配置里的跳跃键", rc.jump_key(), "c")
check("★ 按 c → 原地跳（旧版会漏）",
      rc.action_from_keys(["c"]), ("none none jump", True))
check("★ 按 c+右 → 右跳（旧版会漏）",
      rc.action_from_keys(["c", "right"]), ("right none jump", True))
check("★ 按 c+右+上 → 右跳（旧版会漏，且这才是用户真实按法）",
      rc.action_from_keys(["c", "right", "up"]), ("right none jump", True))
# 反向：space 不再是跳跃键时，**不能**被当成跳
check("反向：jump=c 时按 space 不算跳（只按 space+右 → 单纯 right）",
      rc.action_from_keys(["space", "right"]), ("right none none", False))
# 大写 / 带空格的配置值也要能读
rc2 = new_recorder("  C  ")
check("配置值带空格/大写 → 归一化成 'c'", rc2.jump_key(), "c")
check("归一化后仍能认 C 键", rc2.action_from_keys(["c"]), ("none none jump", True))

# ══════════════════════════════════════════════════════════════════════
print("\n【3】★ 跳跃色不许被后来的移动线盖掉（2026-09-17 修的 bug ②）")
# ══════════════════════════════════════════════════════════════════════
# 用户"边跳边按上"：帧 N 画跳跃圆点，帧 N+1 角色还在同一像素上按着上 → 画灰线。
# 旧版没有保护 ⇒ 跳跃圆点被盖成灰 ⇒ 线里没有 jump。
rj = new_recorder("c")
JUMP_RGB = (0, 255, 255)          # 青 = right none jump
UP_RGB = (127, 127, 127)          # 灰 = none up none
rj.loc_player_global = (60, 40)
rj.paint_step("right none jump", True)
_center_bgr = tuple(int(v) for v in rj.img_route[40, 60])
check("① 跳跃画上去后，中心是青（BGR 反着存）", _center_bgr, JUMP_RGB[::-1])
# 下一帧：角色**没动**，按着上 → 画一条 1px 灰线
rj.loc_player_global = (60, 40)
rj.paint_step("none up none", False)
_center2 = tuple(int(v) for v in rj.img_route[40, 60])
check("★ ② 同一像素再画一条灰线后，中心**仍是青**（跳跃色受保护）",
      _center2, JUMP_RGB[::-1])
_n_jump = int(((rj.img_route[:, :, 0] == JUMP_RGB[2])
               & (rj.img_route[:, :, 1] == JUMP_RGB[1])
               & (rj.img_route[:, :, 2] == JUMP_RGB[0])).sum())
check("★ ③ 跳跃色像素一个都没少（圆点 ~13 个）", _n_jump >= 9, True)
# 换一段后保护要清空（不能把上一段的跳"保护"到新段上）
rj._reset_route_canvas()
check("④ 换段后 _blob_pixels 清空", rj._blob_pixels, {})

# ══════════════════════════════════════════════════════════════════════
print("\n【4】blob_cooldown：连按跳不能把整条线刷满")
# ══════════════════════════════════════════════════════════════════════
rb = new_recorder("c")
rb.loc_player_global = (30, 30)
rb.paint_step("none none jump", True)
_t0 = rb.t_last_draw_blob
_pix_after_first = dict(rb._blob_pixels)     # 第一次画完后记下的像素
rb.loc_player_global = (30, 30)
rb.paint_step("none none jump", True)      # 冷却未过 → 不画
check("冷却内连按 → t_last_draw_blob 不变（没重复画）",
      rb.t_last_draw_blob, _t0)
check("冷却内连按 → 记下的像素集合也没变",
      dict(rb._blob_pixels), _pix_after_first)
rb.t_last_draw_blob = time.time() - 999    # 手动让冷却过期
rb.loc_player_global = (50, 30)
rb.paint_step("none none jump", True)
# ⚠️ 2026-09-25 修：原断言是 `rb.t_last_draw_blob > _t0`，比较两次 time.time()。
#    但 Windows 系统时钟精度约 15.6ms（实测 2000 次连续调用有 1429 次拿到**完全相同**
#    的值），CI 慢机器上两次调用极易落在同一刻度 ⇒ `>` 不成立 ⇒ **假红**
#    （GitHub Actions windows-latest 实测失败：got=False want=True）。
#    时间戳本身不是被测语义，**"冷却过后确实又画了一个圆点"** 才是 —— 改成断言
#    新位置被记进 _blob_pixels（(50,30) 是第二次才用的坐标，冷却没过时不该出现）。
check("冷却过后 → 会再画一次（新位置进了 blob 像素表）",
      (50, 30) in rb._blob_pixels, True)
check("冷却过后 → 原位置仍在（不是清空重画）",
      (30, 30) in rb._blob_pixels, True)

print("\n【5】★ 跳跃必须**边沿触发**：10fps 轮询会漏掉轻按的跳（2026-09-17 修 bug ③）")
# ══════════════════════════════════════════════════════════════════════
# 录制器帧率 fps_limit_route_recorder = 10（100ms/帧），而一次"跳"是轻按（<100~200ms）
# ⇒ 靠 `key_pressing` 轮询**大概率整帧都没看到** ⇒ 跳丢了。
# 真机反例：用户跳跃键 = c，重录两遍都是 0 个跳跃像素。
# ⇒ 改成"按下时置位、主循环取走"（同 quit_requested 的设计）。
from src.input.KeyBoardListener import KeyBoardListener   # noqa: E402


class _FakeKey:
    def __init__(self, ch):
        self.char = ch


def new_listener(jump_key="c"):
    '''造一个**不启动线程/不挂 pynput** 的监听器，只测 on_press 的置位逻辑。'''
    kb = KeyBoardListener.__new__(KeyBoardListener)
    kb.cfg = {"key": {"jump": jump_key}}
    kb.key_pressing = []
    kb.func_keys = {}
    kb.movement_keys = {}
    kb.t_func_key = [0] * 12
    kb.is_pressed_func_key = [False] * 12
    kb.func_key_handlers = {}
    kb.quit_requested = False
    kb._jump_pending = False
    return kb


kb = new_listener("c")
check("刚建好 → 没有待处理的跳", kb.consume_jump_press(), False)
# 关键场景：用户**轻按一下 c 就松开**（松手后 key_pressing 里已经没有 c 了）
kb.on_press(_FakeKey("c"))
kb.on_release(_FakeKey("c"))
check("轻按 c 再松开 → key_pressing 里**没有** c（轮询会漏）",
      "c" in kb.key_pressing, False)
check("★ 但边沿标志还在 → consume 拿到 True（这就是修复点）",
      kb.consume_jump_press(), True)
check("★ 取走即清（不会连续多帧都当跳）", kb.consume_jump_press(), False)
# 非跳跃键不该置位
kb2 = new_listener("c")
kb2.on_press(_FakeKey("a"))
check("按 a（不是跳跃键）→ 不置位", kb2.consume_jump_press(), False)
# jump=space 时按空格要认
kb3 = new_listener("space")
kb3.on_press(_FakeKey("space"))
check("jump=space → 按空格置位", kb3.consume_jump_press(), True)
# 录制器侧：key_press 空 + jump_latched=True → 仍要判成跳
r_latch = new_recorder("c")
check("★ 录制器：key_press 为空但 jump_latched=True → 原地跳",
      r_latch.action_from_keys([], jump_latched=True), ("none none jump", True))
check("★ 录制器：key_press 为空、也没 latch → 不画",
      r_latch.action_from_keys([], jump_latched=False), ("", False))
check("★ 录制器：latch + 按着右 → 右跳（用户真实按法）",
      r_latch.action_from_keys(["right"], jump_latched=True),
      ("right none jump", True))
# ★ 覆盖**真实调用点**：read_keys() 必须把监听器的边沿标志取出来并透传
#   （只测 action_from_keys(jump_latched=...) 抓不到"忘了传"这种错 —— 变异测试实测 0 红）
kb_r = new_listener("c")
kb_r.key_pressing = ["right"]
kb_r.on_press(_FakeKey("c"))          # 用户轻按一下 c（这一帧的轮询里看不到）
kb_r.on_release(_FakeKey("c"))
r_latch.kb = kb_r
_kp, _jl = r_latch.read_keys()
check("★ read_keys() 透传 latch（轻按 c 后）",
      (_kp, _jl), (["right"], True))
_kp2, _jl2 = r_latch.read_keys()
check("★ read_keys() 取走即清（第二帧不再当跳）", (_kp2, _jl2), (["right"], False))
# 没有 consume_jump_press 的旧监听器也不能炸
class _OldKb:
    key_pressing = ["right"]
r_latch.kb = _OldKb()
check("旧监听器（没有 consume_jump_press）→ 不炸、按轮询走",
      r_latch.read_keys(), (["right"], False))

print("\n【5.5】★ 爬管道的按键组合：↑ 是**按住**的，不需要边沿触发（用户问过）")
# ══════════════════════════════════════════════════════════════════════
# 用户问："我爬管道的时候还需要按↑键，还需要改么？"
# 答：**不用**。轮询丢键只惩罚"轻点一下"的键（跳/传送）；
#     ↑ 爬管道必须**按住**（不按住游戏也不爬），一按好几秒，10fps 采样几十帧都看得到。
# 实证：用户重录的那条线里有 **42 个上（灰）像素** —— ↑ 被完整记录了，
#       丢的只有跳（0 个）。本节点把这个行为钉住。
# ══════════════════════════════════════════════════════════════════════
r_up = new_recorder("c")
check("① 按住 ↑（不跳）→ none up none（画线，不是团）",
      r_up.action_from_keys(["up"]), ("none up none", False))
# 连按 3 帧都要读到 —— 证明是"按住"语义，不是边沿
_three = [r_up.action_from_keys(["up"]) for _ in range(3)]
check("② 连按 3 帧都读到 up（按住语义，不会只触发一次）",
      _three, [("none up none", False)] * 3)
# ⚠️ 有意设计：↑ 优先级高于左右（色码表里**根本没有** "右+上" 这个组合）。
#    引擎侧也是这个逻辑：靠近管柱发 up；上了管柱后由 is_on_ladder 互补出左右。
check("③ 按住 右+↑ → up 优先（右被吞，**有意设计**）",
      r_up.action_from_keys(["right", "up"]), ("none up none", False))
check("④ 按住 左+↑ → up 优先", r_up.action_from_keys(["left", "up"]),
      ("none up none", False))
# 用户真实按法：站 82 点，**边按右、边跳、边按上**
check("⑤ ★ 右+跳+↑（用户真实按法）→ 右跳（画团），↑ 下一帧再接",
      r_up.action_from_keys(["right", "up"], jump_latched=True),
      ("right none jump", True))
# 跳完之后继续按住 ↑ → 录成 up（爬管道那一段）
check("⑥ 跳完继续按住 ↑ → none up none（管柱那一段就是这样录下来的）",
      r_up.action_from_keys(["right", "up"], jump_latched=False),
      ("none up none", False))
check("⑦ 跳的边沿取走后，同一帧的 ↑ 仍然生效（不会两边都丢）",
      r_up.action_from_keys(["up"], jump_latched=False), ("none up none", False))

print("\n【6】★ 录制器必须跟随主界面「配置方案」（2026-09-17 真机日志抓到的 bug ④）")
# 真机实证：主界面选的是用户方案（key.jump='c'），但录制器日志打印
# `[录制] 跳跃键 = 'space'` ⇒ 录制时按 c 一次跳都记不下来 ⇒ 回正线 0 跳跃像素。
# 根因：argparse 的 --cfg 默认值是 'custom'，而 config/config_custom.yaml 必然存在
#       ⇒ `if args.cfg` 恒为真 ⇒ active_config_path() 成了死代码。
import ast                                                    # noqa: E402
import inspect                                                # noqa: E402
from src.utils.common import active_config_path               # noqa: E402

_cfgsrc = inspect.getsource(__import__("tools.routeRecorder",
                                       fromlist=["x"]))
_cfg_default = None
for _node in ast.walk(ast.parse(_cfgsrc)):
    if (isinstance(_node, ast.Call)
            and getattr(_node.func, "attr", "") == "add_argument"
            and _node.args and _node.args[0] == "--cfg"):
        for _kw in _node.keywords:
            if _kw.arg == "default":
                _cfg_default = getattr(_kw.value, "value", "<非字面量>")
check("① --cfg 的默认值必须是 None（否则 active_config_path() 永远走不到）",
      _cfg_default, None)

_ap = active_config_path()
check("② active_config_path() 必须返回主界面选中的方案（ui_state.last_config_path）",
      (_ap is None) or _ap.endswith(".yaml"), True)
# 死代码回归：只要 default 不是 None，config_custom.yaml 就把方案顶掉了
_legacy_blocked = bool(_cfg_default) and os.path.exists(
    os.path.join("config", f"config_{_cfg_default}.yaml"))
check("③ 不传 --cfg 时不能落到 config_custom.yaml（方案必须生效）",
      _legacy_blocked, False)
print(f"     当前环境中 active_config_path() = {_ap}")

print("\n【7】★ 瞬移键也必须读配置（2026-10-14 修：jump_key 的同类 bug 当时漏修）")
# ══════════════════════════════════════════════════════════════════════
# 真机反例：config `key.teleport: space`，用户录制时按 空格+方向 想录瞬移，
#   而录制器里写死了 `"e" in key_press` ⇒ 一路落到末尾返回 ("", False)
#   ⇒ **一个传送像素都不画，全程静默**（引擎侧 4 个 teleport 色码本来全支持）。
# 本节点钉两件事：① 瞬移键从配置读；② 老默认值 "e" 的行为不能变。
# ⚠️ 这里**不加**"源码里不许出现写死 e"的字符串判据：下面的 ② 与 ⑦ 是**行为判据**，
#    写死 e 时必红；而字符串判据会被 docstring 里对旧写法的引用误伤
#    （tools/verify_hotkey_f12_close.py 踩过这个 code_only 坑）。
# ══════════════════════════════════════════════════════════════════════
r_tp = new_recorder("c", teleport_key="space")
check("① 瞬移键读自 config（teleport=space）", r_tp.teleport_key(), "space")
check("★ ② space+左 → left none teleport（旧版写死 e 时这里会漏成 left 线）",
      r_tp.action_from_keys(["space", "left"]), ("left none teleport", True))
check("★ ③ space+右 → right none teleport",
      r_tp.action_from_keys(["space", "right"]), ("right none teleport", True))
check("★ ④ space+上 → none up teleport",
      r_tp.action_from_keys(["space", "up"]), ("none up teleport", True))
check("★ ⑤ space+下 → none down teleport",
      r_tp.action_from_keys(["space", "down"]), ("none down teleport", True))
check("⑥ space 单独（没有方向）→ 不画（色码表里没有'原地传送'）",
      r_tp.action_from_keys(["space"]), ("", True))
check("★ ⑦ 反向：瞬移键=space 时按 e 不算传送（写死 e 的回归会在这里红）",
      r_tp.action_from_keys(["e", "left"]), ("left none none", False))
check("⑧ 同时按跳跃键+瞬移键+右 → 跳优先（色码表没有'跳+传送'）",
      r_tp.action_from_keys(["space", "right", "c"]), ("right none jump", True))

r_tp_e = new_recorder("c", teleport_key="e")
check("⑨ 老默认 teleport=e 时按 e → left none teleport（老行为不变）",
      r_tp_e.action_from_keys(["e", "left"]), ("left none teleport", True))
r_tp_up = new_recorder("c", teleport_key="  SPACE  ")
check("⑩ 配置值带空格/大写 → 归一化成 'space'", r_tp_up.teleport_key(), "space")
check("⑪ 归一化后仍认 space",
      r_tp_up.action_from_keys(["space", "down"]), ("none down teleport", True))
r_tp_none = new_recorder("c", teleport_key="")
check("⑫ teleport 留空 → 回落 'e'（读不到也不能变成'谁都不认'）",
      r_tp_none.teleport_key(), "e")
check("⑬ 留空回落后按 e 仍能录传送（与 jump_key 同一条回落口径）",
      r_tp_none.action_from_keys(["e", "right"]), ("right none teleport", True))

print("\n" + "=" * 68)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
