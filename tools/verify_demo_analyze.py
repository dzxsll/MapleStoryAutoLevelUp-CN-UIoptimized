# -*- coding: utf-8 -*-
'''示范录制**分析器**自检（2026-10-14 加）。

配套 `tools/analyze_demo.py`（离线报告）与 `src/utils/demo_recorder.py`（录制）。
本文件钉六件事：
  【1】几何纯函数：怪框、点是否在框内、点到框的 dx/dy、交叠面积（含非法输入）
  【2】`frame_facts`：最近怪按曼哈顿距离选；`eng_engage_n` 只数 `in=1` 的；
        `human_attack` 由按键决定；缺 `attack_box`/`player` 不炸
  【3】分歧分类三态：引擎没看见怪 / 引擎判够不着 / 引擎够得着却没出招
  【4】延迟口径：只在 engage 0→>0 起表、一次"进框"只出一个样本、离开框作废
  【5】梯上/平台的出手率 与 竖向差直方图、报告渲染的关键段都在
  【6】端到端：录制器真写文件 → 读回（含坏行容忍）→ 出报告；`--out` 落地
'''
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.utils.demo_recorder import DemoRecorder                      # noqa: E402
from tools.analyze_demo import (analyze_demo, find_inputs, find_meta,  # noqa: E402
                                frame_facts, gap_to_box, hist_text,
                                load_records, main as analyze_main,
                                mob_rect, overlap_area, point_in_box,
                                quantiles, render_report)

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


#: 玩家 (300,300)、攻击框 ±180×±80（aoe 360×160）
BOX = (120, 220, 480, 380)


def rec(t, mobs, eng_act=False, human=False, ladder=False, player=(300, 300),
        box=BOX, miss=0, seg=1):
    '''造一条记录（字段与 `frame_record` 同形）。'''
    return {
        "t": t, "frame": int(t * 10), "player": list(player) if player else None,
        "nametag": [280, 285], "miss": miss, "ladder": ladder, "seg": seg,
        "minimap": [104, 28], "global": [166, 58],
        "attack_box": list(box) if box else None,
        "search_box": [0, 80, 660, 470], "mobs": mobs,
        "bot": {"mx": "none", "my": "none",
                "act": "attack" if eng_act else "none", "t_atk": t},
        "keys": {"aoe_skill": 1 if human else 0},
        "ev": [["q", 1, t]] if human else [], "other_ev": 0,
    }


def mob(cx, cy, w=30, h=30, in_=0, score=0.1):
    return {"cx": cx, "cy": cy, "w": w, "h": h, "s": score, "in": in_}


print("=" * 72)
print("[1] 几何纯函数")
print("=" * 72)
check("mob_rect：中心 ± 宽高一半", mob_rect(mob(100, 200, 30, 40)),
      (85.0, 180.0, 115.0, 220.0))
check("mob_rect：畸形 ⇒ None", mob_rect({"cx": None}), None)
check("点在框内", point_in_box(BOX, 300, 300), True)
check("点在框外（下）", point_in_box(BOX, 300, 381), False)
check("没有框 ⇒ 不在框内（fail-closed）", point_in_box(None, 300, 300), False)
check("点到框：框内 ⇒ (0,0)", gap_to_box(BOX, 300, 300), (0.0, 0.0))
check("点到框：框外下方 ⇒ (0, 25)", gap_to_box(BOX, 300, 405), (0.0, 25.0))
check("点到框：框外左上 ⇒ (30, 10)", gap_to_box(BOX, 90, 210), (30.0, 10.0))
check("点到框：框右侧 ⇒ (20, 0)", gap_to_box(BOX, 500, 300), (20.0, 0.0))
check("交叠：整只在框内 ⇒ 全面积", overlap_area(mob(300, 300, 30, 30), BOX), 900.0)
check("交叠：完全在外 ⇒ 0", overlap_area(mob(900, 900), BOX), 0.0)
check("交叠：压边（怪 x∈[472,502]、框右边 480 ⇒ 只 8px×30px）",
      overlap_area({"cx": 487, "cy": 300, "w": 30, "h": 30}, BOX), 8.0 * 30.0)
check("交叠：非法怪 ⇒ 0（不炸）", overlap_area({"cx": "x"}, BOX), 0.0)
check("分位数：空 ⇒ 全 None", quantiles([])["p50"], None)
check("分位数：p50 取中位", quantiles([0, 10, 20])["p50"], 10.0)

print()
print("=" * 72)
print("[2] frame_facts：最近怪 / 引擎判定计数 / 人手出手 / 缺字段")
print("=" * 72)
_f = frame_facts(rec(1.0, [mob(500, 300, in_=0), mob(320, 300, in_=1)],
                     human=True, ladder=True, miss=7))
check("最近怪按曼哈顿距离选（远的 200 vs 近的 20）", _f["near"]["cx"], 320.0)
check("引擎判够得着的只数 in=1", _f["eng_engage_n"], 1)
check("中心在框内的只数（320,300 在框内）", _f["center_in"], 1)
check("人手出手由按键决定", _f["human_attack"], True)
check("引擎出手由 bot.act 决定（这里没出手）", _f["eng_attacked"], False)
check("梯上标记带出", _f["ladder"], True)
check("miss 带出", _f["miss"], 7)
_f2 = frame_facts(rec(2.0, [], eng_act=True))
check("没怪 ⇒ near=None、怪数 0", (_f2["near"], _f2["mob_n"]), (None, 0))
check("引擎出手照样记", _f2["eng_attacked"], True)
_f3 = frame_facts({"t": 3.0, "mobs": [mob(300, 300)], "keys": {},
                   "bot": {"act": "none"}})
check("缺 attack_box/player ⇒ 不炸，dx/dy 为 None",
      (_f3["near"]["dx"], _f3["near"]["dist"]), (None, None))
check("缺 keys ⇒ 人手出手=False", _f3["human_attack"], False)

print()
print("=" * 72)
print("[3] 分歧分类三态")
print("=" * 72)
_recs = [
    # 引擎没看见怪，但人出手了
    rec(10.0, [], human=True),
    # 看见了，但判够不着（中心在框下方 45px）
    rec(10.1, [mob(300, 425, in_=0)], human=True),
    rec(10.2, [mob(300, 425, in_=0)], human=True),
    # 够得着却没出招
    rec(10.3, [mob(320, 300, in_=1)], human=True),
    # 引擎出手 + 人出手（不算分歧）
    rec(10.4, [mob(320, 300, in_=1)], human=True, eng_act=True),
    # 引擎出手、人没出手（反向分歧）
    rec(10.5, [mob(320, 300, in_=1)], eng_act=True),
]
_res = analyze_demo(_recs, {"monster_detect": {"mob_engage_mode": "center"}})
check("总帧数", _res["n"], 6)
check("人出手帧（10.0~10.4 共 5 帧）", _res["frames_human"], 5)
check("引擎出手帧", _res["frames_eng"], 2)
check("人出手、引擎没出手（前 4 帧；第 5 帧两边都出手）", _res["only_human"], 4)
check("引擎出手、人没出手", _res["only_eng"], 1)
check("引擎没看见怪 ⇒ 1 帧", _res["human_only_kinds"].get("engine_saw_nothing"), 1)
check("引擎判够不着 ⇒ 2 帧", _res["human_only_kinds"].get("engine_unreachable"), 2)
check("够得着却没出招 ⇒ 1 帧（10.3）",
      _res["human_only_kinds"].get("engine_engaged_no_attack"), 1)
check("人出手时引擎判够不着的占比（3/5 = 60%）",
      _res["human_geom_eng0"]["pct_of_human"], 60.0)
check("那两帧的竖向差 dy=45", _res["human_geom_eng0"]["dy"]["p50"], 45.0)
check("其中「引擎一只怪都没看见」的帧数", _res["human_geom_eng0"]["mob_n0"], 1)
check("例子清单非空", bool(_res["human_only_examples"]), True)
check("反向分歧里怪的距离 p50=20", _res["only_eng_geom"]["dist"]["p50"], 20.0)
_r2 = analyze_demo([rec(20.0, [mob(320, 300, in_=1)], human=True)], {})
check("够得着却没出招：这一帧被归到第三类",
      _r2["human_only_kinds"].get("engine_engaged_no_attack"), 1)
check("空记录不炸", analyze_demo([], {})["n"], 0)

print()
print("=" * 72)
print("[4] 延迟口径：进框起表、一次进框一个样本、离开作废")
print("=" * 72)
_lat = [
    rec(30.0, []),
    rec(30.1, [mob(320, 300, in_=1)]),                     # 进框（起表）
    rec(30.2, [mob(320, 300, in_=1)], eng_act=True),       # 出刀 ⇒ 100ms
    rec(30.3, [mob(320, 300, in_=1)], eng_act=True),       # 同一批：不许再计
    rec(30.4, []),                                         # 离开框
    rec(30.5, [mob(320, 300, in_=1)]),                     # 再进框
    rec(30.6, [mob(320, 300, in_=1)], eng_act=True),       # 出刀 ⇒ 100ms
]
_rl = analyze_demo(_lat, {})
check("引擎延迟样本数 = 2（不是 3）", _rl["latency_eng_n"], 2)
check("延迟 p50 = 100ms", _rl["latency_eng"]["p50"], 100.0)
_lat2 = [
    rec(40.0, [mob(320, 300, in_=1)]),
    rec(40.1, [mob(320, 300, in_=1)]),
    rec(40.2, [mob(320, 300, in_=1)]),                     # 一直没出刀
    rec(40.3, []),                                         # 离开 ⇒ 作废
    rec(40.4, [mob(320, 300, in_=1)]),
    rec(40.5, [mob(320, 300, in_=1)], eng_act=True),       # 只这里出刀 ⇒ 100ms
]
_rl2 = analyze_demo(_lat2, {})
check("离开框作废：只有 1 个样本", _rl2["latency_eng_n"], 1)
check("且延迟从**后一次**进框算（100ms）", _rl2["latency_eng"]["p50"], 100.0)

print()
print("=" * 72)
print("[5] 梯上/平台出手率、直方图、报告关键段")
print("=" * 72)
_lad = [
    rec(50.0, [mob(320, 300, in_=1)], ladder=True, human=True),
    rec(50.1, [mob(320, 300, in_=1)], ladder=True, eng_act=True),
    rec(50.2, [mob(320, 300, in_=1)], ladder=True),
    rec(50.3, [mob(320, 300, in_=1)], ladder=False, human=True, eng_act=True),
    rec(50.4, [mob(320, 300, in_=1)], ladder=False, eng_act=True),
]
_rlad = analyze_demo(_lad, {})
_l = _rlad["ladder"]
check("梯上帧数", _l["on"], 3)
check("梯上占比 60%", _l["pct_on"], 60.0)
check("人在梯上出手 1 帧 ⇒ 33.3%", _l["human_rate_on"], 33.3)
check("人在平台出手 1 帧 ⇒ 50.0%", _l["human_rate_off"], 50.0)
check("引擎在梯上出手 1 帧 ⇒ 33.3%", _l["eng_rate_on"], 33.3)
check("引擎在平台出手 2 帧 ⇒ 100.0%", _l["eng_rate_off"], 100.0)
check_true("直方图有行", len(hist_text([5, 25, 45], (0, 10, 40, 100))) == 3)
check_true("直方图没样本时说清楚", "没有样本" in hist_text([], (0, 1))[0])
_txt = render_report(_rlad, title="自检报告")
for _need in ("# 自检报告", "## 1. 人出手时的几何", "## 2. 分歧清单",
              "## 3. 反向分歧", "## 4. 「进攻击框 → 出刀」延迟",
              "## 5. 有怪但引擎没出招时的竖向差分布",
              "## 6. 梯子/绳索上打不打", "## 7. 结论与建议",
              "不是设计参数", "不自动改配置"):
    check_true(f"报告里有「{_need}」", _need in _txt)
check_true("空记录的报告说清楚原因",
           "没有可用记录" in render_report(analyze_demo([], {})))

print()
print("=" * 72)
print("[6] 端到端：录制 → 读回（含坏行）→ 报告 → --out")
print("=" * 72)
_tmp = tempfile.mkdtemp(prefix="verify_demo_an_")
_cfg = {
    "bot": {"attack": "aoe_skill"},
    "key": {"aoe_skill": "q", "directional_attack": "w"},
    "aoe_skill": {"range_x": 360, "range_y": 160, "cooldown": 0.05},
    "directional_attack": {"range_x": 350, "range_y": 70, "cooldown": 0.9},
    "monster_detect": {"mob_engage_mode": "center"},
    "demo_record": {"enabled": True, "record_keys": False, "gzip": False,
                    "out_dir": _tmp},
}
_rec = DemoRecorder(_cfg, logger=None)
check_true("录制器开起来了", _rec.start() is True)
for _i, _r in enumerate(_res and _recs or []):
    _rec.add_frame(dict(_r, t=10.0 + _i * 0.1))
_rec.stop()
_path = _rec.path
check_true("文件存在", os.path.exists(_path))
with open(_path, "a", encoding="utf-8") as _fh:
    _fh.write("{这不是合法 JSON\n")          # 坏行：只计数、不中断
_loaded, _bad = load_records([_path])
check("读回 6 帧", len(_loaded), 6)
check("坏行计数 = 1", _bad, 1)
check_true("meta 能定位到", bool(find_meta(_tmp)))
_res_e2e = analyze_demo(_loaded, find_meta(_tmp))
check("端到端：人出手帧 = 5", _res_e2e["frames_human"], 5)
check("端到端：配置从 meta 带出（够得着判据 center）",
      _res_e2e["meta"]["monster_detect"]["mob_engage_mode"], "center")
_ins = find_inputs(_tmp)
check("目录筛选只挑 demo_*.jsonl（meta 不算数据文件）",
      sorted(os.path.basename(p) for p in _ins), [os.path.basename(_path)])
_out = os.path.join(_tmp, "report.md")
check("main(--out) 返回 0", analyze_main([_path, "--out", _out]), 0)
check_true("报告落到磁盘", os.path.exists(_out)
           and "示范录制分析" in open(_out, encoding="utf-8").read())
check("main(没有参数) 打印用法并返回 2", analyze_main([]), 2)
check("main(路径不存在) 返回 2", analyze_main([os.path.join(_tmp, "nope.jsonl")]), 2)
shutil.rmtree(_tmp, ignore_errors=True)

print()
print("=" * 72)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
