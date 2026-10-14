# -*- coding: utf-8 -*-
'''「段号越界」的自检（2026-10-12 加，配合 MapleStoryAutoBot._clamp_route_index）。

## 它防的是什么（真机现场，挂机**完全停摆**）

用户把一张图**从 10 段重录成 2 段**，界面随后在**同一个进程里**热重载配置 ⇒
`img_routes` 变成 2 条，而 `idx_routes` 还停在上一轮的值（比如 8）：

    self.img_route = self.img_routes[self.idx_routes]      # IndexError

而**崩溃点排在"路线推进"之前** ⇒ 索引永远回不来 ⇒ 主循环每帧都炸
（实测日志「累计失败 149 帧」），角色彻底不动，而且只留一堆堆栈。

这类 bug 在离线环境里几乎没法"跑"出来（要真机 + 热重载 + 段数变少三个条件同时满足），
所以只能把**判据**抽出来钉死：`_clamp_route_index` 的边界，
以及 `run_once` 那个读取点**必须**带兜底。

用法：
    python -m tools.verify_route_index_bounds
'''
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot   # noqa: E402

FAIL: list = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


def make_bot(n_routes, idx):
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.img_routes = [f"route{i+1}" for i in range(n_routes)]
    b.idx_routes = idx
    return b


print("=" * 70)
print("段号越界 · 自检")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】_clamp_route_index()：路线条数变化后段号必须回到合法范围")
# ══════════════════════════════════════════════════════════════════════════
# ★ 真机现场：10 段 → 2 段，段号还停在 8
b = make_bot(2, 8)
b._clamp_route_index()
check("① ★ 10 段重录成 2 段、段号 8 → 钳回 0", b.idx_routes, 0)

b = make_bot(2, 2)                 # 恰好等于长度（最常见的差一错）
b._clamp_route_index()
check("② 段号 == 条数（差一）→ 钳回 0", b.idx_routes, 0)

b = make_bot(3, -1)                # 负数（理论上不该有，但绝不能崩）
b._clamp_route_index()
check("③ 段号负数 → 钳回 0", b.idx_routes, 0)

b = make_bot(0, 5)                 # 一条路线都没有（全判废）
b._clamp_route_index()
check("④ 一条路线都没有 → 段号归 0（不是 IndexError）", b.idx_routes, 0)

# ── 反向：合法的段号**不许被动**（否则每次重载都会把巡逻打断回第一段）──────
for _n, _i in ((2, 0), (2, 1), (9, 8)):
    b = make_bot(_n, _i)
    b._clamp_route_index()
    check(f"⑤ {_n} 条、段号 {_i} 合法 → 原样保留", b.idx_routes, _i)

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】★ run_once 的读取点必须带兜底（主循环绝不能被 IndexError 打崩）")
# ══════════════════════════════════════════════════════════════════════════
_src = inspect.getsource(MapleStoryAutoBot.run_once)
check_true("① run_once 里取 route 用了**带边界的**写法（不是直接下标）",
           "0 <= self.idx_routes < len(self.img_routes)" in _src)
check_true("② run_once 里**不存在**裸下标 self.img_routes[self.idx_routes]",
           "self.img_routes[self.idx_routes]" not in _src)

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】重建路线列表的地方必须调钳位（否则修了也白修）")
# ══════════════════════════════════════════════════════════════════════════
_lc = inspect.getsource(MapleStoryAutoBot.load_config)
check_true("① load_config 重建 img_routes 之后调了 _clamp_route_index()",
           "_clamp_route_index()" in _lc)
check_true("② 钳位发生在 img_routes 清空之后（顺序不能颠倒）",
           _lc.index("self.img_routes = []") < _lc.index("_clamp_route_index()"))

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】段号变化必须**在日志里看得见**（2026-10-14 加）")
# ══════════════════════════════════════════════════════════════════════════
# 现场：用户报「从到达最后 route 的终点，会跳跃到后面几个 route」，
# 而当时推进切段那一行是 `logger.debug(...)` —— **debug 不写进日志文件**，
# 于是"段号到底跳没跳、为什么跳"在日志里**一条证据都没有**，只能靠猜。
# 判据：两处段号变化都必须打 INFO，且带 from→to 与原因。
_cr = inspect.getsource(MapleStoryAutoBot.check_reach_goal)
# ⚠️ 判据不能用 `"Change to new route" not in _cr` —— 那段源码的**注释里**正好引用了
#    旧那行（`原来这里只有 logger.debug("Change to new route:…")`），会把判据自己命中。
#    （同类坑：verify_hotkey_f12_close 的 docstring 命中，见那里的 code_only()。）
#    只钉精确的 debug 调用形态：`logger.debug(f"Change to new route`。
check_true("① 推进切段打的是 INFO 级（旧那行 debug 调用已删掉）",
           "[切段] 第 " in _cr and 'logger.debug(f"Change to new route' not in _cr)
check_true("② 推进切段带 from→to 段号",
           "第 {_old_route + 1}/{len(self.img_routes)} 段 → " in _cr)
check_true("③ 推进切段带触发原因（踩到 goal / 离本段终点够近）",
           "'踩到 goal 像素' if self.cmd_action == 'goal'" in _cr)
_rt = inspect.getsource(MapleStoryAutoBot.try_return_to_mainline)
check_true("④ 退出回正线换段（唯一会跨段跳的地方）也打一行 [切段]",
           "[切段] 退出回正线" in _rt)
check_true("⑤ 那一行只在段号**真的变了**时才打（否则会每帧刷屏）",
           "if int(seg) != self.idx_routes:" in _rt)
check_true("⑥ 两处日志同前缀 [切段]，grep 一处就能看全部段号变化",
           "[切段] 第 " in _cr and "[切段] 退出回正线" in _rt)

print("\n" + "=" * 70)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
