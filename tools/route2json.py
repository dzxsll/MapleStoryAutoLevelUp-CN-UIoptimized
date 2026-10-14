# -*- coding: utf-8 -*-
'''把录制轨迹**简化成有序折线**（P0：只产出 JSON + 报告，不碰引擎）。

## 它是干什么的

录制的轨迹是"角色实际去过哪"，里面混着**战斗抖动**（往左走时被打退几像素）。
直接的后果（真机实测）：

    每段里程    344px   250px   150px   81px
    方向冲突     56      20       5      0

**冲突不是录制手笨，是"逆问题"本身无解** —— 从"实际去过哪"反推"想走哪"，
输入已经被污染了。而下面这件事是**正问题**：

> 轨迹只用来定**拐点在哪**，方向由折线自己决定。

于是：
  · 沿途抖动**不参与**任何判断（它被"净位移"吃掉）
  · 每个 leg 只对应一个方向 ⇒ **方向冲突按构造为 0**
  · 老的「route1/route2/… + goal 像素 + 段号推进」整套复杂度**消失**，
    只剩一条有序的 legs 数组

## 算法（2026-10-13 改：主路径 + 回落路径）

**主路径（有 `seg` 字段时）**—— 用录制器给出的答案，不再自己猜：

  1. **筛**：只留 `on==1`（已开始录）且 `p==1`（定位有效）的帧
  2. **切**：按帧里的 `seg` 字段切段。`seg` 就是**用户按 F 键定的段边界**
        （`src/utils/route_trace.py` 的 `event()` 写着："段边界是用户点的，
         不是能推出来的"）—— 这是**正问题**的输入。
  3. **段内收尾**：每段反复取"离当前起点**最远**的那一帧"当拐点，
        直到剩下的位移 < `--min-leg`。**极值点收尾**天然覆盖两种形状：
          · 单向走（段内只有一条腿）
          · 段内折返（用户漏按 F）—— 远点就是折返点，不会像旧版那样归零
  4. **吸附**：横向腿的 y 取**众数**（平台行）、纵向腿的 x 同理
        ⇒ ±2px 的像素级抖动被抹平，线是一条干净的直线
  5. **`kind`**：腿经过的帧里有 `b==1`（画团 = 跳跃/传送）⇒ `kind="jump"`，
        否则 `walk`；`act` 取该段帧里出现最多的动作串（`"right none none"`…）

**回落路径（没有 `seg` 的老轨迹）**—— `simplify_bands()`，层带法，一字未改。

### ★ 2026-10-13 修的两个真缺陷（真机数据打回来的）

  ① **层带法把"同层折返"整段吃掉**：火焰之地1 的轨迹 y 只在 114~116（只有一层），
     `_bands()` 只出一个层 ⇒ 全部 326 帧归成**一个游程**；而它只拿游程的
     **末帧 x** 当终点 ⇒ 182→68→182 的净位移是 0 ⇒ `continue` ⇒
     **导出 0 条 leg**（原始轨迹 240px 全被当"来回蹭动"压缩掉 100%）。
  ② **`load_trace` 把 `seg`/`a`/`b` 扔了**：录制器已经按 F 键把折返点标好了，
     却逼着简化器去解它自己在下面第 12 行承认"无解"的逆问题；
     跳跃/传送的"团"信息（`b==1`）也跟着丢，跳跃腿只能退化成普通步行线。

## 用法

    python -m tools.route2json --map 沼泽地2                 # 读该图的 record_trace.jsonl
    python -m tools.route2json --map 沼泽地2 --dry-run        # 只报告，不写 route.json
    python -m tools.route2json --map 沼泽地2 --min-leg 12     # 拐点更少（更粗的简化）

输出 `minimaps/<map>/route.json`（P0 阶段**没有任何代码读它** ——
先看数据质量，再决定要不要让引擎用它导航）。
'''
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def load_trace(path, allow_paused=False):
    '''读 jsonl → (header, frames, bad, drop)；坏行跳过（录制中途被杀会留半行）。

    ⚠️ 2026-10-13：frames 从 `(x, y, t)` 扩成 `(x, y, t, seg, act, blob)`。
       `seg` = 录制器按 F 键切出来的段边界；`act` = 这一帧的动作串；
       `blob` = 1 表示这一帧画的是"团"（跳跃/传送）而不是线。
       旧版只留 (x, y, t)，等于**把录制器已经给出的答案扔掉**（见文件头 ★）。

    ⚠️ 2026-10-13（第二处）：**丢帧不再静默**。原来只有一句
       `if rec.get("on") == 1 and rec.get("p", 1) == 1:`，于是"录制器全程暂停"
       的那种录制会打出 `读到帧数 : 0   跳过坏行: 0` —— 两个 0 看着像文件坏了，
       实际是 600 帧全被 on=0 滤掉了（实测就是 沼泽地1，见 config 里"白录"那条）。
       现在把丢帧的**原因与条数**回传给 main() 打印：
         `paused`    = on=0（录制暂停，面板【开始·暂停】/ F1）—— 位置是真的，只是没画线；
         `unlocated` = p=0（那一帧还没定位上）—— **坐标本身不可信**。
       `allow_paused=True`（CLI `--allow-paused`）只放宽 `on`，**绝不放宽 `p`**：
       拿"没定位上"的坐标去拟合腿，等于把噪声当路线。
    '''
    header, frames, bad = None, [], 0
    drop = {"paused": 0, "unlocated": 0}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:                                # noqa: BLE001
                bad += 1
                continue
            k = rec.get("k")
            if k == "h":
                header = rec
            elif k == "f":
                if rec.get("p", 1) != 1:
                    drop["unlocated"] += 1
                    continue
                if rec.get("on") != 1:
                    drop["paused"] += 1
                    if not allow_paused:
                        continue
                frames.append((int(rec["x"]), int(rec["y"]), rec.get("t"),
                               rec.get("seg"), rec.get("a") or "",
                               1 if rec.get("b") else 0))
    return header, frames, bad, drop


def load_waypoints(path):
    '''读 jsonl 的 `k=="e"` 事件 → {段号: (x, y)}：录制器自己记下的**段尾坐标**。

    ★ 为什么必须用它，而不是靠帧坐标算：
      `waypoint`/`save` 事件是用户按 F 那一瞬间的角色坐标（`route_trace.event`
      的注释：save 还刻意推迟到帧末，因为 goal 要压在最后一笔线上）。
      而"帧坐标的众数"会被**停留时间**带偏 —— 实测 火焰之地1：
      段0 的真实段尾是 68（save 事件），帧众数却是 **70**（角色在 70 附近多停了两帧），
      于是吸附出 70、段1 又从 68 起 ⇒ 凭空多出 4px 的接缝，
      补合逻辑再给补一条 4px 的腿（打印出来还假装"闭环 0px ✓"）。
    '''
    wps = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:                            # noqa: BLE001
                    continue
                if rec.get("k") == "e" and rec.get("ev") in ("waypoint", "save") \
                        and rec.get("seg") is not None:
                    if "x" in rec and "y" in rec:
                        wps[int(rec["seg"])] = (int(rec["x"]), int(rec["y"]))
    except OSError:
        return {}
    return wps


def _mode(vals):
    '''出现最多的那个值（平台行必须用众数，**不能用均值** —— 见下）。'''
    c = {}
    for v in vals:
        c[v] = c.get(v, 0) + 1
    return max(c.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def _bands(points, min_frames=6, min_sep=6):
    '''层带 = y 直方图里的**峰**（帧数够多，且彼此至少隔 `min_sep`）。

    ## 为什么不能"差 ≤ gap 就并"（上一个版本就是这么错的）

    那版是链式的：`48-50-52-54-56-58` 每步只差 2px，于是一路并成一串 ——
    **两个真实平台（y=50 与 y=58，差 8px）被并成了一层**。
    层级一旦合并，闭环的几何就跟着歪（实测闭环 70px）。

    正确的判据是"**看谷**"：两层之间必须有一段**角色很少停留的中间高度**
    （= 爬绳/跳跃的过路高度）。落到实现上就是"**求峰 + 峰间最小间隔**"：
      · 峰：这一行帧数够多、且是邻域内的极大 ⇒ 角色**真的站在这里**
      · 峰间至少隔 `min_sep`：比这更近的两行属于同一层
        （角色走动时自身 y 本来就会飘几像素 —— 实测 y=50 与 y=54 只差 4px，
         那是同一层的抖动，不是两层）
    '''
    if not points:
        return []
    ys = [p[1] for p in points]
    lo, hi = min(ys), max(ys)
    h = [0] * (hi - lo + 1)
    for y in ys:
        h[y - lo] += 1

    def _at(i):
        return h[i] if 0 <= i < len(h) else 0

    # 峰 = 帧数够 + 是 ±min_sep 窗口内的极大
    peaks = []
    for i in range(len(h)):
        if h[i] < min_frames:
            continue
        w = h[max(0, i - min_sep): i + min_sep + 1]
        if w and h[i] == max(w):
            peaks.append(i + lo)
    # 高的优先，且彼此至少隔 min_sep（≈ 谷不够深就并成一层）
    peaks.sort(key=lambda y: (-_at(y - lo), y))
    bands = []
    for y in peaks:
        if all(abs(y - b) >= min_sep for b in bands):
            bands.append(y)
    return sorted(bands)


def _level_of(y, bands, tol=4):
    '''这个 y 属于哪一层？离所有层都太远（跳跃过路）→ None。

    ⚠️ None 只是"这一帧不算在层上"，**不打断游程** ——
       所以"跳上去了又落回同一层"会被自动合并成一条 leg（跳跃被吃掉）。
    '''
    best, bd = None, 10 ** 9
    for c in bands:
        d = abs(y - c)
        if d < bd:
            bd, best = d, c
    return best if bd <= tol else None


def simplify_bands(points, min_leg, min_sep=6, min_frames=6, close_max=60):
    '''脏轨迹 → 有序 legs（**多平台版 · 层带法**）。

    ⚠️ 2026-10-13：这条现在是**回落路径**（轨迹没有 `seg` 字段时才走），
       主路径见下面的 `simplify()`。层带法有个致命盲区，真机实测（火焰之地1）：
       整张图只有一层 y 时 ⇒ 全部帧归成**一个游程** ⇒ 而它只拿游程**末帧 x**
       当终点（`x_end = points[j1][0]`）⇒ "走出去 114px 再走回来"净位移为 0
       ⇒ `continue` ⇒ **整条路 0 条 leg**。
       下面"在极值点收尾"的描述是**一代/二代**的做法 —— 三代改成层带游程时弄丢了。

    ## 三代算法的教训（每一代都是被真机数据打回来的）

      一代：整条 leg 取 **y 均值** → 跨层时均值落在**两个平台之间**（不可行走的行）
      二代：窗口净位移分步 → 修好了平台行，但**窗口一长，10px 的爬绳开头就被当 None 丢掉**
            （实测 `up` leg 从 y=64 起，而下一层是 78 —— 14px 凭空消失、闭环 48px）

      三代（本版）：**先找层带，再按层走**
        · 每帧的 y 归到某个层带（够不上任何层 = 跳跃过路 → None，**不打断游程**）
        · 层的**变化** = 一次纵向过渡 ⇒ 补一条 up/down leg
        · 同一层内的连续帧 = 一条横向 leg
        · ★ 每条 leg 的起点**强制接上一条的终点** ⇒ **首尾相接是构造保证的**，不再有缝

    ## 为什么这样能同时抗抖动又不丢爬绳

      抖动（横向 ±几像素的来回蹭）**不改变层**，也不改变"从哪走到哪"的端点 ——
      它只是让同一层内多出一些帧，被直接吸收。
      而爬绳是**层的变化**，10px 也算，一次就抓到。
    '''
    n = len(points)
    if n < 2:
        return []
    bands = _bands(points, min_frames=min_frames, min_sep=min_sep)
    if not bands:
        return []

    # ① 按层切游程（None 跳过 = 不打断 ⇒ 跳跃被合并掉）
    runs = []
    for j, (_x, y) in enumerate(points):
        L = _level_of(y, bands)
        if L is None:
            continue
        if runs and runs[-1][0] == L:
            runs[-1][2] = j
        else:
            runs.append([L, j, j])

    # ② 出 leg：每条 leg 的起点**接上一条的终点**（层变了就补纵向）
    out = []
    cur = None                       # 上一条 leg 的终点 (x, y)
    for (L, j0, j1) in runs:
        if j1 <= j0:
            continue
        x_end = points[j1][0]
        if cur is None:
            cur = (points[j0][0], L)
        elif cur[1] != L:            # ★ 层变了 → 补一条纵向过渡
            out.append({"dir": "down" if L > cur[1] else "up",
                        "from": [cur[0], cur[1]], "to": [cur[0], L],
                        "kind": "walk", "act": ""})
            cur = (cur[0], L)
        if abs(x_end - cur[0]) < min_leg:
            continue                 # 这一层基本没横向动，跳过（cur 保持不变）
        out.append({"dir": "right" if x_end > cur[0] else "left",
                    "from": [cur[0], L], "to": [x_end, L],
                    "kind": "walk", "act": ""})
        cur = (x_end, L)

    # ③ ★ 跳跃吸收：紧邻的 down 后接**同 x** 的 up（或反之）= 跳下去了又跳回来。
    #    它不是"换层"，是**同一次跳跃**，两条一起删。
    #    为什么"先记判据、后实现"是对的：上一轮我在说明里写下"跳跃会回到同一层"，
    #    但没写代码 —— 真机数据立刻把它暴露出来（legs 5/6 就是同 x 的 down+up 对）。
    #    删除是**安全**的：a.from == b.to（原路上下），所以删完前后两条仍然接得上 ✓
    #   （③ 的实现统一在 _drop_reverse_pairs —— 主路径与本回落路径**共用一份判据**。
    #     同一判据复制到多处、改一处忘一处，症状不是报错而是静默失效。）
    cleaned = _drop_reverse_pairs(out)

    # ④ ★ 自动补合：末 leg 终点离首 leg 起点太远 ⇒ 补 leg 把环接上。
    #    为什么必须有：**用户走一趟经常"爬上去就没再下来"**，那份轨迹本身不闭环。
    #    而引擎的巡逻是**闭环模型** —— 接不上就"走完最后一段 → 指令真空 → 干站不动
    #    → 看门狗抽随机动作乱走"（这正是你日志里 44px 那次的后果）。
    #    与其让整张图不可用，不如补一条能走回去的路。
    #    ⚠️ 缺口是**斜的**时必须补两条（先纵后横）：实测缺口 Δ(16, 24)，
    #       只补横向会画出一条**穿过两层**的线（角色会贴着一层的地面往前冲出平台）。
    #    ⚠️ 只补 ≤ `close_max` 的缺口：再远就不该"猜"了，那是走丢了，不是少走两步。
    #   （④ 的实现同样统一在 _close_loop，理由同 ③）
    return _close_loop(cleaned, close_max)


# ═════════════════════ 主路径：seg 切段 + 段内极值点收尾 ═════════════════════

def _cut_by_seg(frames):
    '''按 `seg` 字段切段 → [[seg, i0, i1], ...]；拿不到 seg ⇒ None（回落层带法）。

    `seg` **变化**就切一刀（含回退 —— 轨迹文件是追加模式，回退说明接着的是
    另一次录制；若不切，两次录制的坐标会被硬接到一条腿上）。
    '''
    if not frames:
        return None
    for f in frames:
        # `len(f) < 6` 也要回落到层带法：老调用方会传 (x, y) 的 2 元组 /
        # (x, y, t, seg) 的 4 元组（`simplify` 的文档就是这么写的），
        # 直接取 f[3]/f[5] 会抛 IndexError（实测踩过）。
        if len(f) < 6 or f[3] is None:
            return None
    out = []
    for i, f in enumerate(frames):
        if out and out[-1][0] == f[3]:
            out[-1][2] = i
        else:
            out.append([f[3], i, i])
    return out


def _snap(vals, target, tol=4):
    '''坐标吸附到平台行/列：取 target 的 `tol` 邻域里**出现最多**的值。

    ⚠️ 必须用众数，不能用均值 —— 均值会落在两层之间（一代的教训）。
    ⚠️ 并列时取**离 target 最近**的那个：±2px 抖动会让 114/116/118 三行帧数相同，
       若按大小定序（`_mode` 的 `-kv[0]`）平台行会被整行挪 2px（实测取到 114）。

    ⚠️ **主路径现在不调它了**：`_axis_legs` 改用极值点本身的坐标（见那里的注释）——
       沿轴方向的众数会被"停留时间"带偏（实机：在 70 停 4 帧就把 68 顶掉）。
       留着它是给 P1 的层/列判定用，也是把上面两条教训留在代码边上。
    '''
    near = [v for v in vals if abs(v - target) <= tol]
    if not near:
        return target
    c = {}
    for v in near:
        c[v] = c.get(v, 0) + 1
    return max(c.items(), key=lambda kv: (kv[1], -abs(kv[0] - target)))[0]


def _mk(a, b, kind, act):
    '''a→b 轴对齐成一条 leg（调用方保证只有一条轴在动；两轴都不动 ⇒ None）。'''
    if a[0] == b[0] and a[1] == b[1]:
        return None
    if a[0] == b[0]:
        return {"dir": "down" if b[1] > a[1] else "up",
                "from": list(a), "to": list(b), "kind": kind, "act": act}
    return {"dir": "right" if b[0] > a[0] else "left",
            "from": list(a), "to": list(b), "kind": kind, "act": act}


def _first_axis(mid, a, min_leg):
    '''这段路**先**走哪条轴（"h"/"v"）：看帧序里哪条轴先走够 `min_leg`。

    为什么不能写死"先纵后横"：上绳子有两种次序（先走到绳下再爬 / 先爬再走），
    写死一种就会画出一条**斜线**，引擎会贴着平台边缘斜着冲出去（掉下去）。
    '''
    for (x, y) in mid:
        dx, dy = abs(x - a[0]) >= min_leg, abs(y - a[1]) >= min_leg
        if dx and not dy:
            return "h"
        if dy and not dx:
            return "v"
    return "h"


def _axis_legs(a, b, first, min_leg, kind, act, mid):
    '''"斜的"极值段 → 轴对齐的 1~2 条 leg（腿必须横平竖直，不能穿层）。

    ⚠️ 沿轴坐标一律用**极值点 b 自己的坐标**，不许吸附成众数：
       实机 182→68 走出去又走回来时，若对 b 做众数吸附，终点会短 1~4px，
       剩下的尾巴会再生成一条 5px 的碎腿 + 一个闭环补合腿（实测三条腿）。
       众数只对"跨轴坐标"有意义（平台行），而跨轴这里取的是 `a` 那一侧，
       根本不需要猜 —— a 就是上一条腿的终点，本来就在层上。
    '''
    bx, by = b[0], b[1]
    h_ok, v_ok = abs(bx - a[0]) >= min_leg, abs(by - a[1]) >= min_leg
    if h_ok and v_ok:
        m = (a[0], by) if first == "v" else (bx, a[1])
        return [x for x in (_mk(a, m, kind, act), _mk(m, (bx, by), kind, act)) if x]
    if v_ok:
        return [x for x in (_mk(a, (a[0], by), kind, act),) if x]
    if h_ok:
        return [x for x in (_mk(a, (bx, a[1]), kind, act),) if x]
    return []


def _kind_of(part):
    '''这一段帧是什么腿：有"团"（`b==1` = 跳跃/传送）⇒ jump，否则 walk。

    `act` 取该段出现最多的动作串（`"right none none"`…），留给 P1 细分。
    '''
    blob = any(f[5] for f in part)
    acts = [f[4] for f in part if f[4]]
    if not acts:
        return ("jump" if blob else "walk"), ""
    # ⚠️ 这里不能用 _mode()：它是给坐标写的，用 `-kv[0]` 做并列定序 ⇒ 传字符串会抛
    #    TypeError（实测踩过一次）。动作串的众数另外算。
    cnt = {}
    for s in acts:
        cnt[s] = cnt.get(s, 0) + 1
    return ("jump" if blob else "walk"), max(cnt.items(), key=lambda kv: kv[1])[0]


def _snap_to_waypoint(legs, wp, tol=6):
    '''把这一段最后一条 leg 的终点**吸附到录制器记下的段尾**（`waypoint`/`save`）。

    只在**同一条轴**上吸附（横向腿只改 x、纵向腿只改 y）——
    否则会把一条横平竖直的腿拉成斜线（斜线 = 引擎贴着平台边缘冲出去）。
    差得太多（> `tol`）就不动它：那说明这一段的帧和事件对不上，
    宁可照实报出来，也不许"猜"一个端点。
    '''
    if not legs or not wp:
        return False
    last = legs[-1]
    fx, fy = last["from"]
    tx, ty = last["to"]
    wx, wy = wp
    # ⚠️ 跨轴也要在 tol 内：若事件记的是**另一层/另一列**的段尾（说明这一段的帧
    #    和事件对不上），只按沿轴吸附会把腿的端点挪到错误的位置 —— 宁可不动。
    if tx == fx:                                  # 纵向腿：只吸附 y
        if abs(ty - wy) <= tol and abs(tx - wx) <= tol:
            last["to"] = [tx, wy]
            return True
        return False
    if ty == fy:                                  # 横向腿：只吸附 x
        if abs(tx - wx) <= tol and abs(ty - wy) <= tol:
            last["to"] = [wx, ty]
            return True
        return False
    return False


def simplify_sweep(frames, min_leg, close_max=60, waypoints=None):
    '''主路径：按 `seg` 切段 + 段内**极值点收尾** → 有序 legs；无 seg ⇒ None。

    段内收尾的判据是一条纯几何事实：**离起点最远的那个点就是拐点**。
      · 单向走        ⇒ 远点 = 终点 ⇒ 一条腿
      · 段内折返(漏按F) ⇒ 远点 = 折返点 ⇒ 先出去再回来两条腿
                          （旧版层带法在这里输出 **0 条**，实测火焰之地1）
      · ±2px 抖动      ⇒ 抖不出比终点更远的点，被整段吸收
    '''
    cuts = _cut_by_seg(frames)
    if not cuts:
        return None
    out = []
    for (_seg, i0, i1) in cuts:
        part = frames[i0:i1 + 1]
        if len(part) < 2:
            continue
        kind, act = _kind_of(part)
        pts = [(f[0], f[1]) for f in part]
        n0 = len(out)
        cur = pts[0]
        j = 0
        while j < len(pts) - 1:
            rest = pts[j + 1:]
            # ★ 极值点必须用**切比雪夫距离**（取两轴里大的那个），不能用曼哈顿距离。
            #   实机 y 有 ±2px 抖动时，曼哈顿会挑出一个"x 短 1px 但 y 差 4px"的点：
            #     182→68 那一段实测挑到 (69,114)，dist=|69-182|+|114-118|=117
            #     而真正的折返点 (68,118) 只有 114 ⇒ 终点短 1px
            #   短掉的尾巴会再生成一条碎腿 + 一个闭环补合腿（三条腿的来路）。
            #   切比雪夫下 (68,118)=114 > (69,114)=113 ⇒ 抖动再也顶不掉折返点。
            k = max(range(len(rest)),
                    key=lambda i: (max(abs(rest[i][0] - cur[0]), abs(rest[i][1] - cur[1])),
                                   abs(rest[i][0] - cur[0]) + abs(rest[i][1] - cur[1])))
            far = rest[k]
            if abs(far[0] - cur[0]) + abs(far[1] - cur[1]) < min_leg:
                break
            mid = rest[:k + 1]
            legs = _axis_legs(cur, far, _first_axis(mid, cur, min_leg),
                              min_leg, kind, act, mid)
            if not legs:
                break
            out.extend(legs)
            cur = tuple(legs[-1]["to"])
            j = j + 1 + k
        # ★ 段尾吸附到录制器自己记下的 waypoint/save 事件（理由见 _snap_to_waypoint）
        if waypoints:
            _snap_to_waypoint(out[n0:], waypoints.get(int(_seg)))
    return _close_loop(_drop_reverse_pairs(out), close_max)


def _drop_reverse_pairs(legs):
    '''删掉"同 x 的 down 紧接着 up"（或反之）= 原路上下，是同一次动作。

    删除是**安全**的：a.from == b.to，删完前后两条仍然接得上。
    （旧版注释：上一轮只在说明里写了"跳跃会回到同一层"、没写代码，
     真机数据立刻把 legs 5/6 打出来。）
    '''
    out, i = [], 0
    while i < len(legs):
        if i + 1 < len(legs):
            a, b = legs[i], legs[i + 1]
            if (a["dir"] in ("up", "down") and b["dir"] in ("up", "down")
                    and a["dir"] != b["dir"]
                    and a["from"][0] == b["from"][0]
                    and a["to"] == b["from"] and b["to"] == a["from"]):
                i += 2
                continue
        out.append(legs[i])
        i += 1
    return out


def _close_loop(legs, close_max):
    '''末 leg 终点离首 leg 起点太远 ⇒ 补 leg 把环接上（引擎的巡逻是闭环模型）。

    ⚠️ 缺口是**斜的**时必须补两条（先纵后横）：只补横向会画出一条**穿过两层**的线。
    ⚠️ 只补 ≤ `close_max` 的缺口：再远就不该"猜"了，那是走丢了，不是少走两步。
    '''
    if not legs or close_max <= 0:
        return legs
    tgt = legs[0]["from"]
    ex, ey = legs[-1]["to"]
    gap = abs(ex - tgt[0]) + abs(ey - tgt[1])
    if 0 < gap <= close_max:
        x1, y1 = tgt[0], tgt[1]
        if ey != y1:
            legs.append({"dir": "down" if y1 > ey else "up",
                         "from": [ex, ey], "to": [ex, y1], "kind": "walk", "act": ""})
            ey = y1
        if ex != x1:
            legs.append({"dir": "right" if x1 > ex else "left",
                         "from": [ex, ey], "to": [x1, ey], "kind": "walk", "act": ""})
    return legs


def simplify(frames, min_leg, min_sep=6, min_frames=6, close_max=60, use_seg=True,
             waypoints=None):
    '''轨迹帧 → 有序 legs。**主路径 = seg 切段 + 极值点收尾 + 段尾吸附 waypoint**；
    没有 `seg` 字段（老轨迹）或显式 `use_seg=False` ⇒ 回落层带法 `simplify_bands`。
    '''
    if use_seg:
        legs = simplify_sweep(frames, min_leg, close_max=close_max,
                              waypoints=waypoints)
        if legs is not None:
            return legs
    pts = [(f[0], f[1]) for f in frames]
    return simplify_bands(pts, min_leg, min_sep=min_sep, min_frames=min_frames,
                          close_max=close_max)


def raw_len(points):
    return sum(abs(points[k + 1][0] - points[k][0]) + abs(points[k + 1][1] - points[k][1])
               for k in range(len(points) - 1))


def leg_len(legs):
    return sum(abs(l["to"][0] - l["from"][0]) + abs(l["to"][1] - l["from"][1]) for l in legs)


def main(argv=None):
    ap = argparse.ArgumentParser(description="把录制轨迹简化成有序折线（P0）")
    ap.add_argument("--map", default=None, help="地图名")
    ap.add_argument("--trace", default=None, help="直接指定 record_trace.jsonl")
    ap.add_argument("--out", default=None, help="输出路径（默认 minimaps/<map>/route.json）")
    ap.add_argument("--min-leg", type=int, default=8,
                    help="一个 leg 至少要走这么多像素（默认 8；调大 = 拐点更少）")
    ap.add_argument("--min-sep", type=int, default=6,
                    help="两层之间至少差这么多像素才算两层（默认 6）。\n"
                         "调小(如 3) = 把 y=50 与 y=54 当**两个不同平台**；\n"
                         "调大 = 更倾向当成同一层的抖动。")
    ap.add_argument("--min-frames", type=int, default=6,
                    help="一个平台行至少停留这么多帧才算一层（默认 6）")
    ap.add_argument("--close-max", type=int, default=60,
                    help="末 leg 终点离首 leg 起点 ≤ 这么多像素时，自动补 leg 把环接上\n"
                         "（默认 60；填 0 = 不补，缺口大就照实报出来）")
    ap.add_argument("--no-seg", action="store_true",
                    help="不用录制器的 seg 字段切段（回落层带法；留着做对照实验 ——\n"
                         "0 条 leg 那个 bug 就是靠它证明的）")
    ap.add_argument("--allow-paused", action="store_true",
                    help="连「录制暂停」期间的帧也收（on=0）。默认不收：暂停的录制\n"
                         "PNG 里一笔没画，引擎现阶段的像素路径用不了它；但这类轨迹的\n"
                         "**坐标是真的**，想把这趟白录救成 legs 就用这个开关。\n"
                         "（p=0「那一刻没定位上」的帧永远不收 —— 坐标不可信）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写 route.json")
    a = ap.parse_args(argv)

    trace = a.trace
    if not trace:
        if not a.map:
            print("[错误] 要给 --map 或 --trace。")
            return 2
        trace = os.path.join("minimaps", a.map, "record_trace.jsonl")
    if not os.path.exists(trace):
        print(f"[错误] 找不到轨迹文件：{trace}\n"
              f"        （录一次就有；或者用 --trace 指定别的位置。）")
        return 2

    header, frames, bad, drop = load_trace(trace, allow_paused=a.allow_paused)
    print("=" * 72)
    print("录制轨迹 → 有序折线（P0：只产出，不影响挂机）")
    print("=" * 72)
    print(f"  轨迹文件 : {trace}")
    print(f"  地图     : {(header or {}).get('map')}   录制帧率: {(header or {}).get('fps')}")
    print(f"  读到帧数 : {len(frames)}   跳过坏行: {bad}")
    if drop["paused"] or drop["unlocated"]:
        _bits = []
        if drop["paused"]:
            _bits.append(f"on=0（录制暂停）{drop['paused']} 帧")
        if drop["unlocated"]:
            _bits.append(f"p=0（当时没定位上，坐标不可信）{drop['unlocated']} 帧")
        print(f"  ⚠️ 另丢弃 : " + "，".join(_bits))
        if drop["paused"] and not a.allow_paused:
            print("             ↑ 这趟录制处在「暂停」状态（面板【开始·暂停】/ F1）："
                  "PNG 里一笔线都没画，")
            print("               但轨迹坐标是真的 ⇒ 想救成 legs 加 `--allow-paused`"
                  "（救出来的腿只能走腿图，")
            print("               引擎现阶段的像素路径仍然用不了这张图）。")
    _wps = load_waypoints(trace)
    _cuts = None if a.no_seg else _cut_by_seg(frames)
    print("  分段依据 : " + (f"录制器的 seg 字段（{len(_cuts)} 段）" if _cuts
                            else "无 seg 字段 → 回落层带法")
          + ("   [--no-seg 强制层带法]" if a.no_seg else ""))
    if _wps:
        print(f"  段尾基准 : 用上录制器的 waypoint/save 事件 {len(_wps)} 个"
              f"（段号 {sorted(_wps)}）")
    if len(frames) < 2:
        print("[错误] 有效帧不足，没法简化。")
        return 2

    pts = [(f[0], f[1]) for f in frames]
    legs = simplify(frames, a.min_leg, min_sep=a.min_sep, min_frames=a.min_frames,
                    close_max=a.close_max, use_seg=not a.no_seg, waypoints=_wps)

    _raw, _leg = raw_len(pts), leg_len(legs)
    print()
    print(f"  原始轨迹 : {len(pts)} 帧 / 曼哈顿路程 {_raw}px")
    print(f"  简化结果 : {len(legs)} 条 leg / 总长 {_leg}px"
          f"   （压缩掉 {_raw - _leg}px 的来回蹭动，占 "
          f"{(100.0 * (_raw - _leg) / _raw if _raw else 0):.0f}%）")
    print()
    print("  ── legs（引擎将来就按这个顺序走）──")
    for k, l in enumerate(legs):
        d = (l["to"][0] - l["from"][0], l["to"][1] - l["from"][1])
        print(f"   {k:>2}. {l['dir']:<5} {l['from']} → {l['to']}"
              f"   长 {abs(d[0]) + abs(d[1]):>3}px   Δ{d}"
              f"   [{l.get('kind', '?')}{' ' + l['act'] if l.get('act') else ''}]")

    # ── 关键指标：这套 legs 栅格化之后**会不会有方向冲突** ──────────────────
    # 每条 leg 只对应一个方向、且已轴对齐 ⇒ 冲突恒为 0。这里把"为什么"打印出来，
    # 让人一眼看到它和"录制出来的 PNG"的差别在哪。
    dirs = [l["dir"] for l in legs]
    print()
    print(f"  方向序列 : {' → '.join(dirs)}")
    # ⚠️ 这句以前写成"方向冲突 **0**（按构造）"—— **过头了**，实测就能打脸：
    #    沼泽地1 救回来的腿 5/6（right 70→150、left 150→136）在 y=48 上反向重叠。
    #    准话是：**每条 leg 只有一个方向**，所以"一个位置上既像左又像右"这种
    #    像素图里的歧义在腿图里不存在；但腿**与腿**之间照样能反向重叠 ——
    #    那是真实的折返，靠**走的顺序**消歧义（先 5 后 6），不是靠阈值。
    print("  ★ 方向歧义：每条 leg 只对应一个方向（不靠调阈值）")
    print("    （注意：腿与腿之间仍可能反向重叠 —— 那是真折返，靠顺序消歧义）")
    if legs:
        f0, lz = legs[0]["from"], legs[-1]["to"]
        gap = abs(lz[0] - f0[0]) + abs(lz[1] - f0[1])
        print(f"  闭环     : 末 leg 终点 {tuple(lz)} ↔ 首 leg 起点 {tuple(f0)}"
              f"   差 {gap}px"
              f"{'  ✓ 接得上' if gap <= 10 else '  ⚠ 超过 search_range(10)，需要补一条 leg'}")

    if a.dry_run:
        print("\n  （--dry-run：没有写任何文件）")
        return 0

    out = a.out or os.path.join(os.path.dirname(trace), "route.json")
    payload = {
        "version": 1,
        "map": (header or {}).get("map"),
        "source": os.path.basename(trace),
        "min_leg": a.min_leg,
        "legs": legs,
    }
    # ⚠️ 不覆盖已有的 route.json（万一以后手改过，别让一次导出把它冲掉）
    if os.path.exists(out):
        bak = out + ".bak"
        os.replace(out, bak)
        print(f"\n  （已把旧的 route.json 备份到 {os.path.basename(bak)}）")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  已写入：{out}")
    print("  ⚠️ P0 阶段**没有任何代码读它** —— 先看数据质量，再决定要不要让引擎用它导航。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
