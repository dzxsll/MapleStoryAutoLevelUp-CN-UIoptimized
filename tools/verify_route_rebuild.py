# -*- coding: utf-8 -*-
'''路线离线重生成的自检（2026-10-12 加，配合 tools/routeRebuild.py）。

## 为什么必须逐像素钉"重生成 == 原图"

路线图的颜色是**反着存**的（录制器 RGB→BGR，引擎读入再 BGR→RGB，两次抵消；
详见 routeRecorder._stamp_goal 的长注释）。这条约定一旦错位，症状是
**整张路线被引擎判废**、挂机时角色一动不动 —— 而且录制器那边一切正常，
只有真机跑起来才暴露。

重生成工具要复刻的是"底图剥色 + 按轨迹重画 + 压 goal"这一整套。
只要有一处错（颜色反了、底图取错、每段没清 `loc_player_global_last`、
`t_last_draw_blob` 没延续…），重生成的图就会和原图对不上。
⇒ 本自检的用例【3】把这件事**逐像素**钉住。

## 用例

  【1】两个纯函数（去噪判据、折返点判据）的边界
  【2】轨迹格式：写入 → 读回 → 字段/色码表往返一致；写失败不许抛
  【3】★ 重生成 == 原图（逐像素）
  【4】★ 一整圈 A→B→A 被自动切成两段，左右方向冲突从「有」变成「无」
  【5】去噪：孤立尖刺被删掉
  【6】goal 点被重新压上（少了它引擎会在段末死锁）

⚠️ 所有"怎么画"都走 tools.routeRebuild 里那几个函数，本文件**不复刻**绘制逻辑。

用法：
    python -m tools.verify_route_rebuild
'''
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys

import numpy as np
import cv2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml, load_image, imwrite_unicode          # noqa: E402
from src.utils.paths import resource_path                                    # noqa: E402
from src.utils.route_trace import (                                          # noqa: E402
    RouteTraceWriter, load_last_session, load_trace, codes_to_json,
    codes_from_json, split_sessions, TRACE_NAME,
)
import tools.routeRecorder as _rr                                            # noqa: E402
import tools.routeRebuild as RB                                              # noqa: E402

FAIL: list = []
TMP_MAP = "_verify_rebuild_tmp"
TMP_MAP2 = "_verify_rebuild_tmp2"


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


def cfg_and_codes():
    cfg = load_yaml("config/config_default.yaml")
    main = {tuple(int(p) for p in k.split(',')): v
            for k, v in cfg["route"]["color_code"].items()}
    ud = {tuple(int(p) for p in k.split(',')): v
          for k, v in cfg["route"]["color_code_up_down"].items()}
    return cfg, main, ud


def tmp_dir(name):
    return resource_path(os.path.join("minimaps", name))


def cleanup(*names):
    for n in names:
        p = tmp_dir(n)
        shutil.rmtree(p, ignore_errors=True)
        shutil.rmtree(p + ".bak", ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("路线离线重生成 · 自检")
print("=" * 70)

cfg, CODES, CODES_UD = cfg_and_codes()

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】纯函数边界")
# ══════════════════════════════════════════════════════════════════════════
# found=去程；back=回程。坐标(段号无关)用 (t, x, y, action, is_blob)
def mk_pts(pairs, t0=1000.0):
    return [(t0 + i * 0.1, x, y, a, b) for i, (x, y, a, b) in enumerate(pairs)]


_rt = mk_pts([(x, 60, "right none none", 0) for x in range(60, 141, 4)]
             + [(x, 63, "left none none", 0) for x in range(140, 59, -4)])
_i = RB.find_turn_split(_rt, min_leg=15, loop_tol=20)
check_true("① 一整圈 A→B→A → 能切，且切在离起点最远处",
           _i is not None and abs(_rt[_i][1] - 140) <= 4)

_oneway = mk_pts([(x, 60, "right none none", 0) for x in range(60, 141, 4)])
check("② 单程（终点没回到起点）→ 不切", RB.find_turn_split(_oneway), None)

_short = mk_pts([(60, 60, "right none none", 0), (62, 60, "right none none", 0),
                 (60, 60, "left none none", 0), (58, 60, "left none none", 0)])
check("③ 太短的一个来回 → 不切（去程 < min_leg）",
      RB.find_turn_split(_short, min_leg=15), None)

# 去噪：a(0,0) → b(60,0)【尖刺】 → c(1,0)  ⇒ b 该被删
_spike = mk_pts([(0, 0, "right none none", 0), (60, 0, "right none none", 0),
                 (1, 0, "right none none", 0)])
_kept, _rm = RB.denoise_spikes(_spike, spike_dist=12, back_tol=4)
check("④ 孤立尖刺被删掉 1 个", _rm, 1)
check("⑤ 删的是中间那个尖刺（首尾两点还在）",
      [(p[1], p[2]) for p in _kept], [(0, 0), (1, 0)])

# 反向：正常连续移动**一个都不能删**
_walk = mk_pts([(x, 60, "right none none", 0) for x in range(0, 40, 2)])
_kept2, _rm2 = RB.denoise_spikes(_walk, spike_dist=12, back_tol=4)
check("⑥ 正常连续移动 → 一个都不删", (_rm2, len(_kept2)), (0, len(_walk)))

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】轨迹格式：写 → 读 往返")
# ══════════════════════════════════════════════════════════════════════════
cleanup(TMP_MAP2)
_md = tmp_dir(TMP_MAP2)
os.makedirs(_md, exist_ok=True)
# 色码表往返（JSON 的键只能是字符串，元组键要拍平再还原）
check("① 色码表往返一致（主表）", codes_from_json(codes_to_json(CODES)), CODES)
check("② 色码表往返一致（上下表）",
      codes_from_json(codes_to_json(CODES_UD)), CODES_UD)

_tw = RouteTraceWriter(_md, meta={"map": TMP_MAP2, "blob_cooldown": 0.5,
                                  "codes": codes_to_json(CODES),
                                  "codes_ud": codes_to_json(CODES_UD)})
check("③ 轨迹文件建得起来", os.path.exists(os.path.join(_md, TRACE_NAME)), True)
_tw.frame(t=1.5, x=10, y=20, action="right none none", is_blob=False,
          seg=0, on=True, pos_ok=True)
_tw.frame(t=1.6, x=14, y=20, action="", is_blob=False, seg=0, on=False, pos_ok=True)
_tw.event("save", seg=0, x=14, y=20)
_tw.close()

_h, _recs = load_last_session(os.path.join(_md, TRACE_NAME))
_frames = [r for r in _recs if r.get("k") == "f"]
check("④ 读回 2 个帧记录", len(_frames), 2)
check("⑤ 帧字段不丢（t/x/y/a/b/seg/on/p）",
      (_frames[0]["x"], _frames[0]["y"], _frames[0]["a"], _frames[0]["seg"],
       _frames[0]["on"], _frames[0]["p"]),
      (10, 20, "right none none", 0, 1, 1))
check("⑥ 暂停帧的 on=0 也记下来了", _frames[1]["on"], 0)
check("⑦ 会话头里的色码表读得回来", codes_from_json(_h.get("codes")), CODES)
check("⑧ 事件带上了坐标（goal 位置靠它）",
      [(r["ev"], r["x"], r["y"]) for r in _recs if r.get("k") == "e"],
      [("save", 14, 20)])

# 多会话：再写一个会话，load_last_session 必须只取最后一个
_tw2 = RouteTraceWriter(_md, meta={"map": TMP_MAP2})
_tw2.frame(t=9.0, x=99, y=99, action="left none none", is_blob=False,
           seg=0, on=True, pos_ok=True)
_tw2.close()
_h2, _recs2 = load_last_session(os.path.join(_md, TRACE_NAME))
_last2 = [r for r in _recs2 if r.get("k") == "f"]
_sess = split_sessions(load_trace(os.path.join(_md, TRACE_NAME)))
check("⑨ 整个文件里确实是 2 个会话", len(_sess), 2)
check("⑩ load_last_session 只取最后一个会话（拿到 x=99 那条）",
      (_last2[-1]["x"], _last2[-1]["y"]), (99, 99))

# 写失败绝不允许抛（磁盘满/权限不足/目录其实是个文件…）
_bad = os.path.join(_md, "not_a_dir")
with open(_bad, "w", encoding="utf-8") as f:
    f.write("x")
try:
    _tw3 = RouteTraceWriter(_bad)
    _tw3.frame(t=1, x=1, y=1, action="a", is_blob=False, seg=0, on=1, pos_ok=1)
    _tw3.event("save", seg=0, x=1, y=1)
    _tw3.close()
    check("⑫ 写不进去时**不抛异常**且自报 broken", _tw3.broken, True)
except Exception as e:                                       # noqa: BLE001
    check(f"⑫ 写不进去时不许抛异常（实际抛了 {e!r}）", False, True)
cleanup(TMP_MAP2)


# ══════════════════════════════════════════════════════════════════════════
def build_case(map_name, samples, goal_pt, seg_events=None, raw_shape=(120, 200),
               seed=20261012):
    '''造一个"像真录制器那样"录出来的地图目录：route1.png + record_trace.jsonl。

    ⚠️ 这里的绘制**独立写一遍**（不复用 RB.paint_segment），否则就成了
       "paint_segment 自己和自己比"，测不出底图剥色 / 轨迹往返 / 分段这些plumbing。
       两边共用的是录制器真实的 `paint_step` —— 颜色约定由它保证，本文件不碰。
    '''
    cleanup(map_name)
    md = tmp_dir(map_name)
    os.makedirs(md, exist_ok=True)

    rng = np.random.default_rng(seed)
    raw = rng.integers(0, 256, size=(raw_shape[0], raw_shape[1], 3), dtype=np.uint8)

    painter = RB.make_painter(cfg, CODES, CODES_UD, raw.shape)
    # 底图 = 和录制器 _reset_route_canvas 一样，把地图里的路线色涂黑
    painter.img_route = painter.remove_color_code_pixels(raw.copy())
    painter.loc_player_global_last = None
    painter._blob_pixels = {}

    # 独立重放（等价于录制器主循环里那一小段）
    with RB._FrozenTime(_rr):
        for (t, x, y, a, b) in samples:
            RB._REPLAY_NOW[0] = float(t)
            painter.loc_player_global = (int(x), int(y))
            painter.paint_step(a, bool(b))
        # 段末压 goal（和 save_route 一致：最后一笔之后）
        RB.stamp_goal(painter, painter.img_route, goal_pt)
    orig = painter.img_route.copy()
    imwrite_unicode(os.path.join(md, "route1.png"), orig)

    # 轨迹
    tw = RouteTraceWriter(md, meta={
        "map": map_name, "jump": "space",
        "blob_cooldown": cfg["route_recoder"]["blob_cooldown"],
        "codes": codes_to_json(CODES), "codes_ud": codes_to_json(CODES_UD)})
    tw.event("arm", t=samples[0][0], x=samples[0][1], y=samples[0][2], seg=0)
    for (t, x, y, a, b) in samples:
        tw.frame(t=t, x=x, y=y, action=a, is_blob=bool(b), seg=0, on=1, pos_ok=1)
    for ev in (seg_events or []):
        tw.event(ev["ev"], t=ev["t"], x=ev["x"], y=ev["y"], seg=ev["seg"])
    tw.event("save", t=samples[-1][0] + 0.05, x=goal_pt[0], y=goal_pt[1], seg=0)
    tw.close()
    return md, orig


def run_rebuild(map_name, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = RB.run(map_name, **kw)
    return rc, buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════
print("\n【3】★ 重生成 == 原图（逐像素；这一步钉住颜色口径与底图剥色）")
# ══════════════════════════════════════════════════════════════════════════
_samples3 = mk_pts([(x, 60, "right none none", 0) for x in range(40, 120, 4)]
                   + [(120, 50, "none up none", 0), (120, 40, "none up none", 0)])
_goal3 = (120, 40)
_md3, _orig3 = build_case(TMP_MAP, _samples3, _goal3)
_out3 = os.path.join(_md3, "_out")
_rc, _log = run_rebuild(TMP_MAP, out_dir=_out3, do_denoise=False, do_split=False)
check("① 重生成返回码 0", _rc, 0)
_re3 = load_image(os.path.join(_out3, "route1.png"))
check("② 尺寸一致", _re3.shape, _orig3.shape)
_diff = int((_re3.astype(np.int16) != _orig3.astype(np.int16)).sum())
check("③ ★ 逐像素完全一致（差 0 个通道值）", _diff, 0)

# 显式钉住"反着存"这条约定：config 里 right = RGB(0,0,255)，图上必须存成 BGR(255,0,0)
_px = tuple(int(v) for v in _re3[60, 100])
check("④ 约定钉死：config right=RGB(0,0,255) → 图上存 BGR(255,0,0)", _px, (255, 0, 0))

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】★ 一整圈 A→B→A 自动切成两段，左右冲突消失")
# ══════════════════════════════════════════════════════════════════════════
# 去程 y=60（right/蓝），回程 y=63（left/红）—— 相距 3px，在冲突半径 8 以内，
# 正是真机上"回程不完全踩在去程上"的样子。
_rt_samples = (mk_pts([(x, 60, "right none none", 0) for x in range(60, 141, 4)])
               + mk_pts([(x, 63, "left none none", 0) for x in range(140, 59, -4)],
                        t0=1100.0))
_md4, _orig4 = build_case(TMP_MAP, _rt_samples, (60, 63))
_painter4 = RB.make_painter(cfg, CODES, CODES_UD, _orig4.shape)
_before = RB.audit_image(_painter4, _orig4, CODES)
check_true("① 切开**之前**原图里确实有左右方向冲突（复现真机故障）",
           _before["conflicts"] > 0)

_out4 = os.path.join(_md4, "_out")
_rc4, _log4 = run_rebuild(TMP_MAP, out_dir=_out4, do_denoise=False, do_split=True)
check("② 返回码 0", _rc4, 0)
check("③ 生成了两段", (os.path.exists(os.path.join(_out4, "route1.png")),
                       os.path.exists(os.path.join(_out4, "route2.png"))), (True, True))
_img1 = load_image(os.path.join(_out4, "route1.png"))
_img2 = load_image(os.path.join(_out4, "route2.png"))
_a1 = RB.audit_image(_painter4, _img1, CODES)
_a2 = RB.audit_image(_painter4, _img2, CODES)
check("④ 第一段（去程）只有 right、没有 left",
      (_a1["conflicts"], _a1["left"] == 0, _a1["right"] > 0), (0, True, True))
check("⑤ 第二段（回程）只有 left、没有 right",
      (_a2["conflicts"], _a2["right"] == 0, _a2["left"] > 0), (0, True, True))
check_true("⑥ 报告里提到了自动切段", "自动切成 2 段" in _log4)
# --no-split 时不许切（开关要真的有效，否则用户没法关掉）
_out4b = os.path.join(_md4, "_out_nosplit")
_rc4b, _log4b = run_rebuild(TMP_MAP, out_dir=_out4b, do_denoise=False, do_split=False)
check("⑦ --no-split 时不切段（只出 route1.png）",
      (os.path.exists(os.path.join(_out4b, "route1.png")),
       os.path.exists(os.path.join(_out4b, "route2.png"))), (True, False))

# ══════════════════════════════════════════════════════════════════════════
print("\n【5】去噪：孤立尖刺被删掉")
# ══════════════════════════════════════════════════════════════════════════
_sp = mk_pts([(x, 60, "right none none", 0) for x in range(40, 120, 4)])
_sp.insert(10, (_sp[10][0] - 0.05, 44, 20, "right none none", 0))   # 尖刺：(44,20)
_md5, _orig5 = build_case(TMP_MAP, _sp, (116, 60), seed=7)
# 原图上尖刺位置附近应该有路线像素（BGR 蓝 = right）
_spike_has = bool((_orig5[20, 44] == (255, 0, 0)).all())
check_true("① 原图上尖刺点确实画上了路线像素", _spike_has)
_out5 = os.path.join(_md5, "_out")
_rc5, _log5 = run_rebuild(TMP_MAP, out_dir=_out5, do_denoise=True, do_split=False)
check("② 返回码 0", _rc5, 0)
_re5 = load_image(os.path.join(_out5, "route1.png"))
check("③ 重生成后尖刺像素没了", bool((_re5[20, 44] == (255, 0, 0)).all()), False)
check_true("④ 报告里说了删掉几个孤立点（只有真删了才会出现这句）",
           "去噪删掉" in _log5)
# 关掉去噪 → 尖刺必须还在（证明删它的是去噪，不是别的原因）
_out5b = os.path.join(_md5, "_out_nodenoise")
run_rebuild(TMP_MAP, out_dir=_out5b, do_denoise=False, do_split=False)
_re5b = load_image(os.path.join(_out5b, "route1.png"))
check("⑤ 关掉去噪 → 尖刺还在（证明是去噪删的）",
      bool((_re5b[20, 44] == (255, 0, 0)).all()), True)

# ══════════════════════════════════════════════════════════════════════════
print("\n【6】goal 点必须被重新压上（少了它引擎在段末死锁）")
# ══════════════════════════════════════════════════════════════════════════
GOAL_BGR = (0, 255, 255)          # config goal = RGB(255,255,0) → 图上存 BGR


def count_color(img, bgr):
    '''数某个 BGR 颜色的像素数（通道逐一比，顺序别搞反：img 就是 BGR 排列）。'''
    return int(((img[:, :, 0] == bgr[0]) & (img[:, :, 1] == bgr[1])
                & (img[:, :, 2] == bgr[2])).sum())


check_true("① 原图上有 goal 像素（用例前提）", count_color(_orig3, GOAL_BGR) >= 9)
check_true("② 重生成后 goal 像素仍在", count_color(_re3, GOAL_BGR) >= 9)
# 段末 goal 必须压在**最后画的移动线之上**（录制器为此刻意把 save 放到帧末）
check("③ goal 落在段末坐标上（120,40）",
      tuple(int(v) for v in _re3[40, 120]), GOAL_BGR)
# 切段后两段都要有各自的 goal（不然角色走到折返点不知道该切段）
_g1, _g2 = count_color(_img1, GOAL_BGR), count_color(_img2, GOAL_BGR)
check_true("④ 切成两段后**两段各自**都有 goal", _g1 >= 9 and _g2 >= 9)

# ══════════════════════════════════════════════════════════════════════════
print("\n【7】闭环检查：报告里给出终点到起点的距离")
# ══════════════════════════════════════════════════════════════════════════
# 用例【4】的一整圈：切完两段后，最后一段终点 = 回程终点 (60,63)，
# 第一段起点 = 去程起点 (60,60) → 曼哈顿 3px
check_true("① 闭环距离按曼哈顿算并写进报告（3px）", "= 3px" in _log4)

# ══════════════════════════════════════════════════════════════════════════
print("\n【8】★ 跳跃/传送「团」像素（走 is_draw_blob 分支 + 冻结时钟）")
# ══════════════════════════════════════════════════════════════════════════
# 这一段是**补覆盖缺口**的：2026-10-13 之前录制器一个跳跃像素都录不出来
#   （KeyBoardListener 认不出 Alt 修饰键），所以「轨迹里有团像素」这条路
#   **从来没有被执行过**，藏在里面的 `_FrozenTime` 包装 bug 一直没被发现。
#   跳跃修好后第一次跑真机 routeRebuild 立刻崩在
#       routeRecorder.py:1444  dt = time.time() - self.t_last_draw_blob
#       TypeError: '_T' object is not callable
#   （包装把 time 换成 `SimpleNamespace(time=_T())` **实例**，只吃得下
#     `time.time()`，吃不下裸调 `time()`）。本用例钉住这条路径。
_jump_rgb = next(rgb for rgb, act in CODES.items() if "jump" in act)
_jump_act = CODES[_jump_rgb]
_jump_bgr = (_jump_rgb[2], _jump_rgb[1], _jump_rgb[0])      # 存盘是 BGR
_jb = cfg["route_recoder"]["blob_cooldown"]

_jmps = []
_t8 = 1000.0
for _x in range(40, 100, 4):
    if _x == 68:
        # 团那两帧连着来：走 … →(跳)→ 走。间隔必须 > blob_cooldown，否则被冷却吃掉
        _t8 += _jb + 0.05
        _jmps.append((_t8, _x + 2, 60, _jump_act, 1))
        _t8 += 0.1
    _t8 += 0.1
    _jmps.append((_t8, _x, 60, "right none none", 0))

_md8, _orig8 = build_case(TMP_MAP, _jmps, (96, 60), seed=11)
check_true(f"① 用例前提：原图上真有「团」像素（{_jump_act}）",
           count_color(_orig8, _jump_bgr) >= 9)
_out8 = os.path.join(_md8, "_out")
_rc8, _log8 = run_rebuild(TMP_MAP, out_dir=_out8, do_denoise=False, do_split=False)
check("② 含团像素的轨迹重生成返回码 0（昔日在此 TypeError）", _rc8, 0)
check_true("③ 报告里没有 TypeError 字样", "TypeError" not in _log8)
_re8 = load_image(os.path.join(_out8, "route1.png"))
check_true("④ ★ 重生成后「团」像素仍在", count_color(_re8, _jump_bgr) >= 9)
check("⑤ ★ 逐像素完全一致",
      int((_re8.astype(np.int16) != _orig8.astype(np.int16)).sum()), 0)

print("\n" + "=" * 70)
cleanup(TMP_MAP, TMP_MAP2)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
