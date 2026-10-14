# -*- coding: utf-8 -*-
'''「录制轨迹 → 腿图(route.json)」的自检（2026-10-13 加，配合 tools/route2json.py）。

## 它防的是什么（真机现场：**全程不报错，只是导出 0 条腿**）

火焰之地1 录制（326 帧 / y 只在 114~116 一层 / x 68~182）：

    录制体检（引擎口径）   route1 130px 移动117 ✓   route2 125px 移动112 ✓   闭环 0px ✓
    route2json --dry-run   0 条 leg / 总长 0px（"压缩掉 240px 的来回蹭动，占 100%"）

三个缺陷，都在**不报错**的路径上：

  ① 层带法只拿"游程**末帧**的 x"当终点 ⇒ 182→68→182 的净位移是 0 ⇒ `continue`
     ⇒ 整条路 0 条腿。整张图只有一层 y 时，全部帧是**一个游程**，必然踩中。
  ② `load_trace` 只留 `(x, y, t)`，把录制器的 `seg`（用户按 F 键定的段边界）、
     `a`/`b`（动作 / 是否画"团"=跳跃传送）全扔了 ⇒ 简化器只能去猜折返点。
  ③ `load_trace` 还会**静默**丢掉 `on=0`（录制暂停）的帧 —— 沼泽地1 整趟 600 帧
     全是 on=0，于是它打印 `读到帧数 : 0   跳过坏行: 0`：两个 0 看着像"文件坏了"，
     实际是"录制器全程暂停、PNG 一笔没画"。现在丢帧必须把**原因和条数**打出来，
     `--allow-paused` 可以把这类轨迹的坐标救成 legs（但 `p=0` 的帧永远不收）。

本自检把这些判据钉死：**同层折返不许归零**、**段边界以 `seg` 为准**、
**腿必须横平竖直且首尾相接**、**跳跃腿要带 kind**、**丢帧不许静默**。

用法：
    python -m tools.verify_route2json

⚠️ 本脚本**只读**：不写任何文件（连临时文件都不建 —— 2026-10-13 那次地图被清空
   的事故之后，"自检不许写盘"当成纪律）。没有真实轨迹时相关小节自动 SKIP。
'''
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.route2json import (_axis_legs, _close_loop, _cut_by_seg,       # noqa: E402
                             _drop_reverse_pairs, _first_axis, _kind_of,
                             _snap_to_waypoint, leg_len, load_trace, load_waypoints,
                             simplify, simplify_bands, simplify_sweep)

FAIL: list = []
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# ⚠️ 现场轨迹要**按候选找**：2026-10-13 02:35 那次 `火焰之地1/` 被删成了 `火焰之地1.bak/`
#    （GUI 删除流程的盘上特征：留 .bak + 反登记），写死路径会让整个现场复现无声变成 SKIP。
LIVE_CANDIDATES = (
    os.path.join(ROOT, "minimaps", "火焰之地1", "record_trace.jsonl"),
    os.path.join(ROOT, "minimaps", "火焰之地1.bak", "record_trace.jsonl"),
)
PAUSED = os.path.join(ROOT, "minimaps", "沼泽地1", "record_trace.jsonl")
MIN_LEG = 8


def pick_live():
    for p in LIVE_CANDIDATES:
        if os.path.exists(p):
            return p
    return LIVE_CANDIDATES[0]


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    if not cond:
        FAIL.append(name)


def mk(pts, segs=0, acts="", blobs=0):
    '''[(x,y)] → 轨迹帧 6 元组；segs/acts/blobs 可以是标量（铺满）或等长序列。'''
    n = len(pts)

    def _at(v, i):
        return v[i] if isinstance(v, (list, tuple)) else v

    return [(int(p[0]), int(p[1]), 0.1 * i, _at(segs, i),
             _at(acts, i) if _at(acts, i) else "", int(_at(blobs, i)))
            for i, p in enumerate(pts)]


def jitter_row(x0, x1, y, step=1):
    '''从 x0 走到 x1（含端点）的一条横向轨迹，每帧抖 ±2px（真机 y 就是这么飘的）。'''
    dirn = 1 if x1 >= x0 else -1
    out = []
    for k, x in enumerate(range(x0, x1 + dirn, dirn)):
        out.append((x, y + (2 if k % 3 == 0 else (0 if k % 3 == 1 else -2))))
    return out


# ══════════════════════════════════════════════════════════════════════════
print("\n【1】★ 同层折返不许归零（线上那个 0 条 leg 的 bug）")
# ══════════════════════════════════════════════════════════════════════════
_row = jitter_row(182, 68, 116) + jitter_row(68, 182, 116)
_fr = mk(_row, segs=0)                     # seg 全 0 = 用户没按 F（漏分段）
check("① 主路径（极值点收尾）在同层来回上出 2 条腿", len(simplify_sweep(_fr, MIN_LEG)), 2)
check("② 回落层带法在同一份数据上仍然是 0 条（这就是线上现象，固化成对照）",
      len(simplify_bands([(f[0], f[1]) for f in _fr], MIN_LEG)), 0)
_l0 = simplify_sweep(_fr, MIN_LEG)
check("③ 第一条腿方向 left", _l0[0]["dir"], "left")
check("④ 第二条腿方向 right", _l0[1]["dir"], "right")
check_true("⑤ 来回的折返点 (68,116) 被真的走到（不是半途收尾）",
           _l0[0]["to"][0] == 68)

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】段边界以录制器的 seg 字段为准")
# ══════════════════════════════════════════════════════════════════════════
_seg = [0] * len(jitter_row(182, 68, 116)) + [1] * len(jitter_row(68, 182, 116)) \
    + [2] * 20
_pts = jitter_row(182, 68, 116) + jitter_row(68, 182, 116) + [(182, 116)] * 20
_fr2 = mk(_pts, segs=_seg)
check("① 切出 3 段", len(_cut_by_seg(_fr2)), 3)
_l2 = simplify_sweep(_fr2, MIN_LEG)
check("② 两段 + 一段静止（原地不动）⇒ 2 条腿", len(_l2), 2)
check("③ 没有凭空多出接缝腿（每条腿都 ≥ min_leg）",
      all(abs(l["to"][0] - l["from"][0]) + abs(l["to"][1] - l["from"][1]) >= MIN_LEG
          for l in _l2), True)
check("④ 末腿终点回到首腿起点（闭环 0px，不需要补合腿）",
      abs(_l2[-1]["to"][0] - _l2[0]["from"][0]) + abs(_l2[-1]["to"][1] - _l2[0]["from"][1]),
      0)

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】段尾吸附到录制器自己的 waypoint/save 事件（众数会被停留时间带偏）")
# ══════════════════════════════════════════════════════════════════════════
_pts3 = ([(182, 116)] * 3 + [(x, 116) for x in range(180, 70, -1)]
         + [(70, 116)] * 4 + [(64, 116)])          # 末帧 64 = 6px 过冲毛刺
_l3raw = simplify_sweep(mk(_pts3, segs=0), MIN_LEG, close_max=0)
check("① 不吸附时末端取**实际走到的最远点** 64（沿轴不许取众数："
      "停留 4 帧的 70 会把真实段尾顶掉）", _l3raw[-1]["to"][0], 64)
_l3wp = simplify_sweep(mk(_pts3, segs=0), MIN_LEG, close_max=0, waypoints={0: (68, 116)})
check("② 吸附后末端 = 录制器记下的段尾 68（把单帧毛刺拉回真实位置）",
      _l3wp[-1]["to"][0], 68)
_manual = [{"dir": "left", "from": [182, 116], "to": [70, 116],
            "kind": "walk", "act": ""}]
check_true("③ 吸附只在同一条轴上做（横向腿不改 y）",
           _snap_to_waypoint(_manual, (68, 116)) and _manual[-1]["to"] == [68, 116])
_manual2 = [{"dir": "left", "from": [182, 116], "to": [70, 116],
             "kind": "walk", "act": ""}]
check_true("④ 差太多（> tol）就不许猜：端点保持原样",
           not _snap_to_waypoint(_manual2, (10, 116)) and _manual2[-1]["to"] == [70, 116])
_manual3 = [{"dir": "left", "from": [182, 116], "to": [70, 116],
             "kind": "walk", "act": ""}]
check_true("⑤ 事件 y 差太多时不改（否则会把横腿拉成斜线）",
           not _snap_to_waypoint(_manual3, (68, 150)) and _manual3[-1]["to"] == [70, 116])

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】跳跃/传送的「团」要变成 kind，而不是退化成普通步行线")
# ══════════════════════════════════════════════════════════════════════════
_dn = [(100, 120), (100, 118), (100, 112)]          # 跳跃（画团）
_fr4 = mk(_dn, segs=0, blobs=1, acts="jump none none")
_l4 = simplify_sweep(_fr4, MIN_LEG, close_max=0)
check_true("① 画过团的段 ⇒ kind == 'jump'", bool(_l4) and _l4[0]["kind"] == "jump")
check("② 动作串也带出来了（P1 细分用）", _l4[0]["act"], "jump none none")
check("③ 纯步行的段 kind == 'walk'", _kind_of(mk(_dn, acts="left none none"))[0], "walk")
_rev = [{"dir": "down", "from": [100, 100], "to": [100, 120], "kind": "jump", "act": ""},
        {"dir": "up", "from": [100, 120], "to": [100, 100], "kind": "jump", "act": ""},
        {"dir": "right", "from": [100, 100], "to": [140, 100], "kind": "walk", "act": ""}]
check("④ 原路上下的一对（同 x、正反）整对删掉，前后仍接得上",
      [l["dir"] for l in _drop_reverse_pairs(_rev)], ["right"])

# ══════════════════════════════════════════════════════════════════════════
print("\n【5】腿必须横平竖直、且首尾相接（构造保证）")
# ══════════════════════════════════════════════════════════════════════════
_mix = (jitter_row(100, 160, 200) + [(160, y) for y in range(199, 159, -1)]
        + jitter_row(160, 120, 160))
_l5 = simplify_sweep(mk(_mix, segs=0), MIN_LEG, close_max=0)
check_true("① 出腿了", len(_l5) >= 2)
check_true("② 每条腿只有一个轴在动（没有斜线）",
           all(l["from"][0] == l["to"][0] or l["from"][1] == l["to"][1] for l in _l5))
check_true("③ 相邻腿首尾相接（from == 上一条的 to）",
           all(_l5[k]["from"] == _l5[k - 1]["to"] for k in range(1, len(_l5))))
check_true("④ 没有零长度腿", all(l["from"] != l["to"] for l in _l5))
check("⑤ 先横后纵：_first_axis 看帧序", _first_axis([(120, 200), (120, 180)],
                                                    (100, 200), MIN_LEG), "h")
check("⑥ 先纵后横：_first_axis 看帧序", _first_axis([(100, 180), (120, 180)],
                                                    (100, 200), MIN_LEG), "v")

# ══════════════════════════════════════════════════════════════════════════
print("\n【6】闭环补合：近的补、远的照实报")
# ══════════════════════════════════════════════════════════════════════════
_near = [{"dir": "right", "from": [100, 100], "to": [140, 100], "kind": "walk", "act": ""},
         {"dir": "down", "from": [140, 100], "to": [140, 120], "kind": "walk", "act": ""}]
_ln = _close_loop([dict(l) for l in _near], 60)
check("① 缺口 40px ⇒ 补一条回到起点", _ln[-1]["to"], [100, 100])
check("② 补的腿仍然横平竖直（斜缺口拆两条）", len(_ln), 4)
_far = [{"dir": "right", "from": [100, 100], "to": [140, 100], "kind": "walk", "act": ""},
        {"dir": "right", "from": [140, 100], "to": [300, 100], "kind": "walk", "act": ""}]
check("③ 缺口 200px > close_max ⇒ 不猜，原样返回",
      len(_close_loop([dict(l) for l in _far], 60)), 2)
check("④ close_max=0 ⇒ 不补", len(_close_loop([dict(l) for l in _near], 0)), 2)

# ══════════════════════════════════════════════════════════════════════════
print("\n【7】_kind_of 吃字符串动作串不许抛（踩过一次 TypeError）")
# ══════════════════════════════════════════════════════════════════════════
try:
    _k = _kind_of(mk([(1, 1), (2, 1)], acts="right none none"))
    check("① 动作串取众数，不抛异常", _k, ("walk", "right none none"))
except Exception as e:                                        # noqa: BLE001
    check("① 动作串取众数，不抛异常", f"{type(e).__name__}: {e}", "不抛")

# ══════════════════════════════════════════════════════════════════════════
print("\n【8】接线：主路径真的接上了（函数写完没人调 = 永远不会触发）")
# ══════════════════════════════════════════════════════════════════════════
_src = open(os.path.join(ROOT, "tools", "route2json.py"), encoding="utf-8").read()
for _n, _frag in (("load_trace 保留 seg 字段", 'rec.get("seg")'),
                  ("load_trace 保留动作串 a", 'rec.get("a")'),
                  ("load_trace 保留画团标记 b", 'rec.get("b")'),
                  ("main 传 waypoints", "waypoints=_wps"),
                  ("main 有 --no-seg 开关", '"--no-seg"'),
                  ("simplify 优先走主路径", "simplify_sweep("),
                  ("没有 seg 时回落层带法", "simplify_bands("),
                  ("段尾用事件而非众数", "load_waypoints(")):
    check_true(f"{_n}", _frag in _src)

# ══════════════════════════════════════════════════════════════════════════
print("\n【9】现场复现（火焰之地1 的真实轨迹；不存在则 SKIP）")
# ══════════════════════════════════════════════════════════════════════════
LIVE = pick_live()
if not os.path.exists(LIVE):
    print(f"  [SKIP] {os.path.relpath(LIVE, ROOT)} 不存在 —— 略过现场复现")
else:
    print(f"  （现场轨迹：{os.path.relpath(LIVE, ROOT)}）")
    _hdr, _frames, _bad, _drop = load_trace(LIVE)
    check_true("① load_trace 现在给 6 元组（带 seg/act/blob）",
               bool(_frames) and len(_frames[0]) == 6)
    check_true("② 帧里有 seg 信息（录制器写的）",
               any(f[3] is not None for f in _frames))
    _live = simplify(_frames, MIN_LEG)
    _fallback = simplify([(f[0], f[1]) for f in _frames], MIN_LEG)
    check_true("③ ★ 现场由 0 条腿变成 ≥2 条腿", len(_live) >= 2)
    check_true("④ 现场每条腿都横平竖直",
               all(l["from"][0] == l["to"][0] or l["from"][1] == l["to"][1]
                   for l in _live))
    check_true("⑤ 现场闭环接得上（末腿终点 ≈ 首腿起点 ≤ 10px）",
               abs(_live[-1]["to"][0] - _live[0]["from"][0])
               + abs(_live[-1]["to"][1] - _live[0]["from"][1]) <= 10)
    check("⑥ 传 2 元组（老调用方那样）⇒ 回落层带法、不抛异常（这条路径实测抛 IndexError）",
          len(_fallback), 0)
    check("⑦ 现场：层带法（回落路径）仍然是 0 条腿 —— 这就是被修掉的那个现象",
          len(simplify_bands([(f[0], f[1]) for f in _frames], MIN_LEG)), 0)

# ══════════════════════════════════════════════════════════════════════════
print("\n【10】丢帧不许静默：on=0 要报原因与条数、--allow-paused 只放宽 on")
# ══════════════════════════════════════════════════════════════════════════
import io                                                      # noqa: E402
import json as _json                                           # noqa: E402
from unittest import mock                                      # noqa: E402


def _fake_trace(n_ok=2, n_on0=3, n_p0=2):
    '''造一份"内存里"的 jsonl；用 mock 顶替 open ⇒ 连临时文件都不落盘。'''
    _ls = [_json.dumps({"k": "h", "map": "自检图", "fps": 10}, ensure_ascii=False)]
    for _i in range(n_ok):
        _ls.append(_json.dumps({"k": "f", "x": 10 + _i, "y": 50, "t": _i * .1,
                                "seg": 0, "a": "right none none", "on": 1, "p": 1}))
    for _i in range(n_on0):
        _ls.append(_json.dumps({"k": "f", "x": 20 + _i, "y": 50, "t": 1 + _i * .1,
                                "seg": 1, "a": "right none none", "on": 0, "p": 1}))
    for _i in range(n_p0):
        _ls.append(_json.dumps({"k": "f", "x": 30 + _i, "y": 50, "t": 2 + _i * .1,
                                "seg": 2, "a": "right none none", "on": 1, "p": 0}))
    return "\n".join(_ls) + "\n"


_FT = _fake_trace()
with mock.patch("builtins.open", return_value=io.StringIO(_FT)):
    _h2, _f2, _b2, _d2 = load_trace("路径被 mock 顶掉了")
check("① on=0 默认不收，且**原因与条数要回传**（旧版只会打 `读到帧数 0 / 坏行 0`）",
      (len(_f2), _d2["paused"], _d2["unlocated"]), (2, 3, 2))
with mock.patch("builtins.open", return_value=io.StringIO(_FT)):
    _h3, _f3, _b3, _d3 = load_trace("路径被 mock 顶掉了", allow_paused=True)
check("② --allow-paused ⇒ 把暂停期间的帧救回来（2 好帧 + 3 暂停帧）", len(_f3), 5)
check("③ ★ 但 p=0（那一刻没定位上）的帧**永远**不收：坐标不可信，fail-closed",
      (_d3["unlocated"], any(f[0] >= 30 for f in _f3)), (2, False))
check_true("④ main 把 allow_paused 传进 load_trace", "allow_paused=a.allow_paused" in _src)
check_true("⑤ main 会打印丢弃原因（不再用两个 0 糊过去）", "另丢弃" in _src)
check_true("⑥ CLI 真有 --allow-paused 开关", '"--allow-paused"' in _src)

if not os.path.exists(PAUSED):
    print("  [SKIP] 沼泽地1 的轨迹不在 —— 略过「暂停录制」现场复现")
else:
    _hp, _fp, _bp, _dp = load_trace(PAUSED)
    check_true("⑦ 现场 沼泽地1：默认 0 帧，**但 drop.paused > 0**"
               "（旧版这里只会打两个 0，看不出是「全程暂停」）",
               len(_fp) == 0 and _dp["paused"] > 0)
    _hp2, _fp2, _bp2, _dp2 = load_trace(PAUSED, allow_paused=True)
    _legs_p = simplify(_fp2, MIN_LEG, waypoints=load_waypoints(PAUSED))
    check_true("⑧ 现场 沼泽地1：--allow-paused 之后能从轨迹里救出腿",
               len(_fp2) > 0 and len(_legs_p) >= 1)
    print(f"        （救回 {len(_fp2)} 帧 ⇒ {len(_legs_p)} 条腿 / "
          f"总长 {leg_len(_legs_p)}px）")

print("\n" + "=" * 70)
if FAIL:
    print(f"自检失败 {len(FAIL)} 项：")
    for _n in FAIL:
        print(f"   - {_n}")
    sys.exit(1)
print("全部通过")
sys.exit(0)
