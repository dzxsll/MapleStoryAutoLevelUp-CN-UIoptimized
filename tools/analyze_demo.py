# -*- coding: utf-8 -*-
'''示范录制离线分析：把「人怎么打」与「引擎怎么判」逐帧对照，产出 markdown 报告。

配套 `src/utils/demo_recorder.py`（录制）与 `config_default.yaml` 的 `demo_record:` 段。
用法：
    python -m tools.analyze_demo debug/demo/demo_20261014_1530.jsonl.gz
    python -m tools.analyze_demo debug/demo            # 目录：读里面所有 demo_*.jsonl*
    python -m tools.analyze_demo debug/demo --out debug/demo/report.md

回答什么问题（这是它存在的**全部**理由）：
    1. 人出手时，怪离得多远？交叠多少？中心在不在攻击框里？在不在梯子上？
       —— 这些分位数就是"够得着"判据该松到哪里的**参照物**。
    2. 「人出手了、引擎那一帧没出手」有多少帧？各自什么原因（引擎判够不着 /
       根本没看见怪 / 够得着却在冷却 / 梯上让路 …）？
    3. 「引擎出手了、人没出手」有多少帧？（多数说明引擎在打人眼里的空枪）
    4. 第一只怪进攻击框 → 引擎出刀 / 人出刀，各用了多久？
    5. 梯子/绳索上，人打不打？引擎打不打？（"爬梯期间让不让打"这个待拍板的问题，
       这里能用数据回答）

⚠️ 三条口径（不写清就会养出错误结论）：
    · 录到的是**引擎自己的感知**（含误检、含丢名字），不是画面真值；报告里凡涉及
      "有没有怪"都以引擎当时看到的为准，并在结论里标注这个前提。
    · `mobs[].in` 是**引擎当时的判定**（用当时的 `mob_engage_mode`）—— 它比
      事后用现在的配置重算更权威；本工具不再重新判定"够不够得着"。
    · 人出手≠必须出手（可能只是想走过去、或手滑），所以分歧清单是**线索**不是判据；
      报告只给数字与例子，不自动改配置。
'''
import gzip
import json
import os
import sys
from collections import Counter

#: 可能的"攻击"动作名（按 `bot.attack` 选其中一个，但录到的另一个也算人出手了）
ATTACK_ACTS = ("aoe_skill", "directional_attack")

#: 人出手却引擎没出手时的分类（顺序就是判定的优先级）
HUMAN_ONLY_KINDS = (
    ("engine_saw_nothing", "引擎这一帧**没看见任何怪**（漏检 / 丢名字连累）"),
    ("engine_unreachable", "引擎看见了怪，但判它**够不着**（`mobs[].in` 全 0）"),
    ("engine_engaged_no_attack", "引擎判够得着却没出招（冷却 / 锁定 / 走位 / 让路）"),
)


def _open(path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def find_inputs(target):
    '''把命令行参数变成文件列表（目录 ⇒ 里面所有 `demo_*.jsonl*`，按名字排序）。'''
    if os.path.isdir(target):
        _out = []
        for _n in sorted(os.listdir(target)):
            if _n.startswith("demo_") and (_n.endswith(".jsonl")
                                           or _n.endswith(".jsonl.gz")):
                _out.append(os.path.join(target, _n))
        return _out
    return [target]


def find_meta(target):
    '''找同目录的 `demo_*_meta.json`（取最新修改的那个）；找不到 ⇒ {}。'''
    _dir = target if os.path.isdir(target) else os.path.dirname(target) or "."
    try:
        _cands = [os.path.join(_dir, _n) for _n in os.listdir(_dir)
                  if _n.startswith("demo_") and _n.endswith("_meta.json")]
    except Exception:                                         # noqa: BLE001
        return {}
    if not _cands:
        return {}
    _cands.sort(key=lambda p: os.path.getmtime(p))
    try:
        with open(_cands[-1], "r", encoding="utf-8") as _f:
            _m = json.load(_f)
        return _m if isinstance(_m, dict) else {}
    except Exception:                                         # noqa: BLE001
        return {}


def load_records(paths):
    '''读 JSONL（坏行只计数、不中断）→ `(records, n_bad)`，按时间排序。'''
    _recs, _bad = [], 0
    for _p in paths:
        try:
            with _open(_p) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _r = json.loads(_line)
                    except Exception:                         # noqa: BLE001
                        _bad += 1
                        continue
                    if isinstance(_r, dict):
                        _recs.append(_r)
                    else:
                        _bad += 1
        except Exception as _e:                               # noqa: BLE001
            print(f"⚠️ 读不了 {_p}：{_e!r}", file=sys.stderr)
    _recs.sort(key=lambda _r: float(_r.get("t") or 0.0))
    return _recs, _bad


# ── 纯几何（与引擎同口径：`mobs[]` 存的是中心 + 宽高） ──────────────────────
def mob_rect(mob):
    '''怪框左上角与右下角：`(x0, y0, x1, y1)`；拿不到 ⇒ None。'''
    try:
        _cx, _cy = float(mob["cx"]), float(mob["cy"])
        _w, _h = float(mob["w"]), float(mob["h"])
    except Exception:                                         # noqa: BLE001
        return None
    return (_cx - _w / 2.0, _cy - _h / 2.0, _cx + _w / 2.0, _cy + _h / 2.0)


def point_in_box(box, x, y):
    '''点是否在框内（box = (x0,y0,x1,y1)）。'''
    if not box:
        return False
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def gap_to_box(box, x, y):
    '''点到框的 `(dx, dy)`（已在框内 = 0）。'''
    if not box:
        return None, None
    _dx = 0.0 if box[0] <= x <= box[2] else (box[0] - x if x < box[0] else x - box[2])
    _dy = 0.0 if box[1] <= y <= box[3] else (box[1] - y if y < box[1] else y - box[3])
    return _dx, _dy


def overlap_area(mob, box):
    '''怪框与攻击框的交叠面积（px²）—— 与引擎 `min_area` 口径一致。'''
    _r = mob_rect(mob)
    if _r is None or not box:
        return 0.0
    _iw = max(0.0, min(box[2], _r[2]) - max(box[0], _r[0]))
    _ih = max(0.0, min(box[3], _r[3]) - max(box[1], _r[1]))
    return _iw * _ih


def frame_facts(rec, attack_acts=ATTACK_ACTS):
    '''把一行记录压成分析用的事实（纯函数，测起来容易）。

    Returns:
        dict: t / eng_attacked / human_attack / mob_n / eng_engage_n /
              near（最近怪的 dx、dy、dist、overlap、in、中心是否在框内）/ ladder / seg
    '''
    _box = rec.get("attack_box")
    _mobs = rec.get("mobs") or []
    _p = rec.get("player") or (None, None)
    _px, _py = (_p[0], _p[1]) if len(_p) >= 2 else (None, None)
    _near = None
    _center_in = 0
    _max_ov = 0.0
    _eng_in = 0
    for _m in _mobs:
        try:
            _cx, _cy = float(_m["cx"]), float(_m["cy"])
        except Exception:                                     # noqa: BLE001
            continue
        _eng_in += 1 if _m.get("in") else 0
        if point_in_box(_box, _cx, _cy):
            _center_in += 1
        _ov = overlap_area(_m, _box)
        _max_ov = max(_max_ov, _ov)
        _dist = None
        if _px is not None and _py is not None:
            _dist = abs(_cx - _px) + abs(_cy - _py)
        _dx, _dy = gap_to_box(_box, _cx, _cy)
        _cand = {"cx": _cx, "cy": _cy, "dx": _dx, "dy": _dy, "dist": _dist,
                 "overlap": _ov, "in": 1 if _m.get("in") else 0,
                 "center_in": 1 if point_in_box(_box, _cx, _cy) else 0}
        if _near is None or (_dist is not None
                             and (_near["dist"] is None or _dist < _near["dist"])):
            _near = _cand
    _keys = rec.get("keys") or {}
    _bot = rec.get("bot") or {}
    return {
        "t": float(rec.get("t") or 0.0),
        "frame": int(rec.get("frame") or 0),
        "seg": rec.get("seg"),
        "ladder": bool(rec.get("ladder")),
        "miss": int(rec.get("miss") or 0),
        "mob_n": len(_mobs),
        "eng_engage_n": _eng_in,
        "center_in": _center_in,
        "max_overlap": _max_ov,
        "eng_attacked": str(_bot.get("act") or "none") == "attack",
        "human_attack": any(_keys.get(_a) for _a in attack_acts),
        "near": _near,
    }


# ── 统计小工具 ────────────────────────────────────────────────────────────
def quantiles(values, ps=(0.1, 0.5, 0.9)):
    '''分位数（线性插值）；空 ⇒ 全 None。'''
    _v = sorted(float(_x) for _x in values if _x is not None)
    if not _v:
        return {f"p{int(_p * 100)}": None for _p in ps}
    _out = {}
    for _p in ps:
        _i = _p * (len(_v) - 1)
        _lo, _hi = int(_i), min(int(_i) + 1, len(_v) - 1)
        _out[f"p{int(_p * 100)}"] = round(_v[_lo] + (_v[_hi] - _v[_lo]) * (_i - _lo), 1)
    return _out


def hist_text(values, edges, width=24):
    '''文本直方图（markdown 表格用）：`[a,b) | n | bar`。'''
    _v = [float(_x) for _x in values if _x is not None]
    _lines = []
    if not _v:
        return ["（没有样本）"]
    _n = len(_v)
    for _i in range(len(edges) - 1):
        _lo, _hi = edges[_i], edges[_i + 1]
        _c = sum(1 for _x in _v if (_lo <= _x < _hi) or
                 (_i == len(edges) - 2 and _x == _hi))
        _bar = "█" * int(round(width * _c / max(1, _n)))
        _lines.append(f"| [{_lo:g}, {_hi:g}) | {_c} | {_bar} |")
    return _lines


def _rate(a, b):
    return (100.0 * a / b) if b else 0.0


# ── 主分析（纯函数：喂记录 → 出数字） ─────────────────────────────────────
def analyze_demo(records, meta=None, attack_acts=ATTACK_ACTS):
    '''把记录算成一份统计字典（**纯函数**，自检直接喂合成记录断言数字）。'''
    _meta = meta or {}
    _recs = [r for r in (records or []) if isinstance(r, dict)]
    _facts = [frame_facts(r, attack_acts) for r in _recs]
    _span = 0.0
    if len(_facts) >= 2:
        _span = max(0.0, _facts[-1]["t"] - _facts[0]["t"])

    _f_human = [f for f in _facts if f["human_attack"]]
    _f_eng = [f for f in _facts if f["eng_attacked"]]
    _only_human = [f for f in _facts if f["human_attack"] and not f["eng_attacked"]]
    _only_eng = [f for f in _facts if f["eng_attacked"] and not f["human_attack"]]
    _both = [f for f in _facts if f["eng_attacked"] and f["human_attack"]]

    # ── 人出手时的几何分位数（这是"判据该松到哪"的直接参照物）────────────
    _geom = {"dist": [], "dx": [], "dy": [], "overlap": [], "mob_n": [],
             "center_in": [], "miss": []}
    for _f in _f_human:
        _n = _f["near"] or {}
        _geom["dist"].append(_n.get("dist"))
        _geom["dx"].append(_n.get("dx"))
        _geom["dy"].append(_n.get("dy"))
        _geom["overlap"].append(_n.get("overlap"))
        _geom["mob_n"].append(_f["mob_n"])
        _geom["center_in"].append(_f["center_in"])
        _geom["miss"].append(_f["miss"])
    _human_geom = {_k: quantiles(_v) for _k, _v in _geom.items()}
    _human_geom["n"] = len(_f_human)
    # 人出手、但引擎判"够不着"（`in` 全 0）的那些帧：竖向差多少？
    _eng0 = [f for f in _f_human if f["eng_engage_n"] == 0]
    _eng0_seen = [f for f in _eng0 if f["mob_n"] > 0]
    _human_geom_eng0 = {
        "n": len(_eng0),
        "pct_of_human": round(_rate(len(_eng0), max(1, len(_f_human))), 1),
        "dy": quantiles([((f["near"] or {}).get("dy")) for f in _eng0]),
        "dx": quantiles([((f["near"] or {}).get("dx")) for f in _eng0]),
        "overlap": quantiles([((f["near"] or {}).get("overlap")) for f in _eng0]),
        "mob_n0": sum(1 for f in _eng0 if f["mob_n"] == 0),
        # 拆开：只有「这一帧看见了怪」的那些帧，dx/dy 才有意义
        # （`_eng0` 里混着"一只怪都没看见"的帧，对它们算 dx/dy 会得到一堆 None）
        "seen": {
            "n": len(_eng0_seen),
            "dx": quantiles([((f["near"] or {}).get("dx")) for f in _eng0_seen]),
            "dy": quantiles([((f["near"] or {}).get("dy")) for f in _eng0_seen]),
            "dist": quantiles([((f["near"] or {}).get("dist"))
                               for f in _eng0_seen]),
        },
    }

    # ── 分歧分类 ──────────────────────────────────────────────────────────
    _kinds = Counter()
    _examples = []
    for _f in _only_human:
        if _f["mob_n"] == 0:
            _k = "engine_saw_nothing"
        elif _f["eng_engage_n"] == 0:
            _k = "engine_unreachable"
        else:
            _k = "engine_engaged_no_attack"
        _kinds[_k] += 1
        if len(_examples) < 12 and (_k != "engine_unreachable"
                                    or len(_examples) < 8):
            _n = _f["near"] or {}
            _examples.append({
                "t": _f["t"], "kind": _k, "mob_n": _f["mob_n"],
                "engage_n": _f["eng_engage_n"], "ladder": _f["ladder"],
                "center_in": _f["center_in"],
                "dx": _n.get("dx"), "dy": _n.get("dy"),
                "dist": _n.get("dist"), "overlap": _n.get("overlap"),
                "miss": _f["miss"],
            })

    # ── 梯子/绳索上打不打（"爬梯期间让不让打"就靠这个回答）─────────────────
    _lad = [f for f in _facts if f["ladder"]]
    _nolad = [f for f in _facts if not f["ladder"]]
    _ladder = {
        "frames": len(_facts), "on": len(_lad),
        "pct_on": round(_rate(len(_lad), max(1, len(_facts))), 1),
        "human_on": len([f for f in _lad if f["human_attack"]]),
        "human_off": len([f for f in _nolad if f["human_attack"]]),
        "eng_on": len([f for f in _lad if f["eng_attacked"]]),
        "eng_off": len([f for f in _nolad if f["eng_attacked"]]),
    }
    _ladder["human_rate_on"] = round(_rate(_ladder["human_on"], max(1, len(_lad))), 1)
    _ladder["human_rate_off"] = round(_rate(_ladder["human_off"], max(1, len(_nolad))), 1)
    _ladder["eng_rate_on"] = round(_rate(_ladder["eng_on"], max(1, len(_lad))), 1)
    _ladder["eng_rate_off"] = round(_rate(_ladder["eng_off"], max(1, len(_nolad))), 1)

    # ── 进框 → 出刀 的延迟（引擎 / 人 各一条）────────────────────────────
    def _latencies(_pred):
        '''「怪**刚进**攻击框 → 第一次出刀」的毫秒数（一次"进框"只出一个样本）。

        ⚠️ 只在 engage 0→>0 的**那一刻**起表：否则把"已经打了一会儿"的每一帧
           都当成新起点，样本数会虚高、分位数会偏小（2026-10-14 首版踩过）。
        '''
        _out = []
        _t0 = None
        _prev = 0
        _armed = False
        for _f in _facts:
            _cur = _f["eng_engage_n"]
            if _cur > 0 and _prev == 0:
                _t0 = _f["t"]
                _armed = True
            if _cur == 0:
                _t0 = None
                _armed = False
            if _armed and _pred(_f):
                _out.append((_f["t"] - _t0) * 1000.0)
                _armed = False
            _prev = _cur
        return _out

    _lat_eng = _latencies(lambda f: f["eng_attacked"])
    _lat_human = _latencies(lambda f: f["human_attack"])

    # ── 竖向差直方图（"漏打是因为上下差几十像素"的直接证据）───────────────
    _dy_visible_noattack = []
    for _f in _facts:
        if _f["mob_n"] and not _f["eng_attacked"]:
            _n = _f["near"] or {}
            if _n.get("dy") is not None:
                _dy_visible_noattack.append(_n["dy"])
    _edges = (0, 10, 20, 40, 60, 80, 120, 100000)

    # ── 结论/建议（只陈述数字与前提，不自动改配置）────────────────────────
    _recs_out = []
    _g = _human_geom
    if _g.get("n"):
        _dy_p50 = _g["dy"].get("p50")
        _dx_p50 = _g["dx"].get("p50")
        _ol_p50 = _g["overlap"].get("p50")
        _recs_out.append(
            f"人出手时：到最近怪的中心距离 p50={_g['dist'].get('p50')}px、"
            f"相对攻击框 dx p50={_dx_p50}px / dy p50={_dy_p50}px、"
            f"交叠 p50={_ol_p50}px²、框内怪数 p50={_g['mob_n'].get('p50')}")
    if _human_geom_eng0["n"]:
        _h = _human_geom_eng0
        _s = _h.get("seen") or {}
        _recs_out.append(
            f"人出手的帧里 `mobs[].in` 全 0 的 {_h['n']} 帧（{_h['pct_of_human']}%）"
            f"要拆开：**一只怪都没看见** {_h['mob_n0']} 帧 + "
            f"**看见了但判「够不着」** {_s.get('n')} 帧；后者的"
            f"dx p50={(_s.get('dx') or {}).get('p50')}px / "
            f"p90={(_s.get('dx') or {}).get('p90')}px、"
            f"dy p50={(_s.get('dy') or {}).get('p50')}px / "
            f"p90={(_s.get('dy') or {}).get('p90')}px ⇒ "
            f"判断据松紧就拿这 {_s.get('n')} 帧当参照物")
    if _ladder["on"]:
        _recs_out.append(
            f"梯子/绳索上：人出手率 {_ladder['human_rate_on']}%（{_ladder['human_on']}/"
            f"{_ladder['on']} 帧）vs 平台上 {_ladder['human_rate_off']}%；"
            f"引擎在梯上出手率 {_ladder['eng_rate_on']}% vs 平台上 "
            f"{_ladder['eng_rate_off']}% ⇒ 这一列直接回答「爬梯期间该不该让打」")
    _recs_out.append(
        "⚠️ 这是**一个人、一张图、一段时间**的观测（不是设计参数）；"
        "录到的「有没有怪」是**引擎自己**看到的，含误检与丢名字；"
        "人出手也可能只是想走过去，所以分歧清单是线索而不是判据。")

    return {
        "n": len(_facts),
        "seconds": round(_span, 1),
        "t_first": _facts[0]["t"] if _facts else None,
        "t_last": _facts[-1]["t"] if _facts else None,
        "frames_human": len(_f_human),
        "frames_eng": len(_f_eng),
        "both": len(_both),
        "only_human": len(_only_human),
        "only_eng": len(_only_eng),
        "neither": len(_facts) - len(_f_human) - len(_f_eng) + len(_both),
        "human_only_kinds": dict(_kinds),
        "human_only_examples": _examples,
        "human_geom": _human_geom,
        "human_geom_eng0": _human_geom_eng0,
        "ladder": _ladder,
        "latency_eng": quantiles(_lat_eng),
        "latency_human": quantiles(_lat_human),
        "latency_eng_n": len(_lat_eng),
        "latency_human_n": len(_lat_human),
        "dy_hist": _dy_visible_noattack,
        "dy_hist_edges": _edges,
        "only_eng_geom": {
            "n": len(_only_eng),
            "mob_n": quantiles([f["mob_n"] for f in _only_eng]),
            "dist": quantiles([((f["near"] or {}).get("dist")) for f in _only_eng]),
            "dy": quantiles([((f["near"] or {}).get("dy")) for f in _only_eng]),
        },
        "attack_acts": list(attack_acts),
        "meta": _meta,
        "recommend": _recs_out,
    }


# ── 渲染 ──────────────────────────────────────────────────────────────────
def render_report(res, title="示范录制分析"):
    '''统计字典 → markdown 报告（纯函数）。'''
    _L = [f"# {title}", ""]
    if not res.get("n"):
        _L.append("**没有可用记录**（文件是空的？开关 `demo_record.enabled` 打开了吗？）")
        return "\n".join(_L)
    _L += [
        f"- 帧数：**{res['n']}**（时长 {res['seconds']}s，约 "
        f"{res['n'] / max(1.0, res['seconds']):.1f} 帧/秒）",
        f"- 人出手帧：**{res['frames_human']}** ／ 引擎出招帧：**{res['frames_eng']}**"
        f"（同时：{res['both']}；谁都没：{res['neither']}）",
        f"- 「人出手、引擎没出手」：**{res['only_human']}** 帧;"
        f"「引擎出手、人没出手」：**{res['only_eng']}** 帧",
        "",
    ]
    _m = res.get("meta") or {}
    if _m:
        _md = _m.get("monster_detect") or {}
        _L += ["## 录制时的配置", "",
               f"- 攻击方式：`{_m.get('attack_mode')}`；"
               f"够得着判据：`{_md.get('mob_engage_mode')}`",
               f"- 群体技能：range {(_m.get('aoe') or {}).get('range_x')}×"
               f"{( _m.get('aoe') or {}).get('range_y')}、cool "
               f"{(_m.get('aoe') or {}).get('cooldown')}",
               f"- `every_n_frames`={( _m.get('settings') or {}).get('every_n_frames')}、"
               f"`record_only`={(_m.get('settings') or {}).get('record_only')}",
               ""]
    _L += ["## 1. 人出手时的几何（判据该松到哪里的参照物）", ""]
    _g = res["human_geom"]
    if _g.get("n"):
        _L += ["| 指标 | p10 | p50 | p90 |", "| --- | --- | --- | --- |"]
        _rows = (("到最近怪的中心距离(px)", "dist"),
                 ("相对攻击框 dx(px)", "dx"),
                 ("相对攻击框 dy(px)", "dy"),
                 ("与攻击框交叠(px²)", "overlap"),
                 ("框内怪数", "mob_n"),
                 ("中心在框内的怪数", "center_in"),
                 ("名字定位 miss", "miss"))
        for _label, _k in _rows:
            _q = _g[_k]
            _L.append(f"| {_label} | {_q.get('p10')} | {_q.get('p50')} | {_q.get('p90')} |")
        _L.append("")
    else:
        _L += ["（录制里没有「人出手」的帧：按键没录到？`record_keys` 开了吗？）", ""]
    _h = res["human_geom_eng0"]
    _s = _h.get("seen") or {}
    _L += [
        f"人出手帧里 `mobs[].in` 全 0 的：{_h['n']} 帧（占人出手帧 "
        f"{_h['pct_of_human']}%）—— 必须拆成两件事：",
        f"- **引擎这一帧一只怪都没看见**：{_h['mob_n0']} 帧（几何无从谈起）",
        f"- **看见了怪、但判「够不着」**：{_s.get('n')} 帧"
        f"（这批才有几何可比）：dx p50={(_s.get('dx') or {}).get('p50')}px / "
        f"p90={(_s.get('dx') or {}).get('p90')}px、"
        f"dy p50={(_s.get('dy') or {}).get('p50')}px / "
        f"p90={(_s.get('dy') or {}).get('p90')}px、"
        f"到最近怪中心距离 p50={(_s.get('dist') or {}).get('p50')}px",
        "",
        "> 分母提醒：上表 `到最近怪的距离 / dx / dy / 交叠` 只统计"
        "**这一帧看见了怪**的人出手帧（看不见怪时这些量是 None，被"
        "`quantiles` 丢掉），而 `框内怪数 / 中心在框内的怪数 / miss` 统计"
        "**全部**人出手帧 —— 所以「交叠 p50=1332 而中心在框内 p50=0」"
        "不是矛盾，是两行分母不同。",
        "",
        "## 2. 分歧清单（人出手 → 引擎没出手）的分类", "",
    ]
    _kinds = res["human_only_kinds"]
    if _kinds:
        _L += ["| 原因 | 帧数 | 占人出手帧 |", "| --- | --- | --- |"]
        for _k, _desc in HUMAN_ONLY_KINDS:
            _c = _kinds.get(_k, 0)
            _L.append(f"| {_desc} | {_c} | "
                      f"{round(_rate(_c, max(1, res['frames_human'])), 1)}% |")
        _L.append("")
        _L += ["例子（时间 / 画面怪数 / 引擎判够得着数 / dx / dy / 交叠 / 梯上）：", ""]
        _L += ["| t | 原因 | 怪数 | 判够得着 | dx | dy | 交叠 | 梯上 |",
               "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for _e in res["human_only_examples"]:
            _L.append(f"| {_e['t']:.2f} | {_e['kind']} | {_e['mob_n']} | "
                      f"{_e['engage_n']} | {_e['dx']} | {_e['dy']} | "
                      f"{_e['overlap']} | {'是' if _e['ladder'] else '否'} |")
        _L.append("")
    else:
        _L += ["（没有这类帧 —— 引擎出手比人还积极）", ""]

    _oe = res["only_eng_geom"]
    _L += ["## 3. 反向分歧（引擎出手 → 人没出手）", "",
           f"- {_oe['n']} 帧；这些帧里画面怪数 p50={_oe['mob_n'].get('p50')}、"
           f"到最近怪距离 p50={_oe['dist'].get('p50')}px、"
           f"dy p50={_oe['dy'].get('p50')}px",
           "- 这条**不是**错：人可能正在赶路/捡东西；但若数值很大（怪很远），"
           "说明引擎在打人眼里的空枪。", ""]

    _L += ["## 4. 「进攻击框 → 出刀」延迟", "",
           f"- 引擎：{res['latency_eng_n']} 个样本，"
           f"p50={res['latency_eng'].get('p50')}ms / p90="
           f"{res['latency_eng'].get('p90')}ms",
           f"- 人：{res['latency_human_n']} 个样本，"
           f"p50={res['latency_human'].get('p50')}ms / p90="
           f"{res['latency_human'].get('p90')}ms", ""]

    _L += ["## 5. 有怪但引擎没出招时的竖向差分布", ""]
    _L += ["| dy 区间(px) | 帧数 | 分布 |", "| --- | --- | --- |"]
    _L += hist_text(res["dy_hist"], res["dy_hist_edges"])
    _L.append("")

    _l = res["ladder"]
    _L += ["## 6. 梯子/绳索上打不打（回答「爬梯期间让不让打」）", "",
           f"- 梯上帧占比：{_l['pct_on']}%（{_l['on']}/{_l['frames']}）",
           f"- 人出手率：梯上 {_l['human_rate_on']}%（{_l['human_on']} 帧）"
           f" vs 平台 {_l['human_rate_off']}%（{_l['human_off']} 帧）",
           f"- 引擎出手率：梯上 {_l['eng_rate_on']}%（{_l['eng_on']} 帧）"
           f" vs 平台 {_l['eng_rate_off']}%（{_l['eng_off']} 帧）", ""]

    _L += ["## 7. 结论与建议", ""]
    for _r in res.get("recommend") or []:
        _L.append(f"- {_r}")
    _L += ["", "---", "",
           "*本报告只给数字与例子，不自动改配置：阈值改动仍要你拍板*"]
    return "\n".join(_L)


def main(argv=None):
    _argv = list(sys.argv[1:] if argv is None else argv)
    _out = None
    if "--out" in _argv:
        _i = _argv.index("--out")
        if _i + 1 < len(_argv):
            _out = _argv[_i + 1]
        _argv = _argv[:_i] + _argv[_i + 2:]
    if not _argv:
        print(__doc__)
        return 2
    _paths = []
    for _t in _argv:
        _paths += find_inputs(_t)
    if not _paths:
        print(f"❌ 没找到 demo 记录（看的是：{_argv}）")
        return 2
    # 参数给了、但文件根本不存在 ⇒ 非零退出（失败绝不静默；别拿空报告冒充成功）
    _missing = [p for p in _paths if not os.path.isfile(p)]
    _readable = [p for p in _paths if os.path.isfile(p)]
    if _missing:
        print(f"⚠️ 跳过不存在的文件：{_missing}", file=sys.stderr)
    if not _readable:
        print(f"❌ 没有可读的 demo 记录（看的是：{_argv}）")
        return 2
    _recs, _bad = load_records(_readable)
    if not _recs:
        print("⚠️ 可读文件里一帧都没有（录制开关没开？录到一半就退了？）",
              file=sys.stderr)
    _meta = find_meta(_argv[0])
    _res = analyze_demo(_recs, _meta)
    _txt = render_report(_res, title=f"示范录制分析（{len(_paths)} 个文件）")
    if _out:
        os.makedirs(os.path.dirname(_out) or ".", exist_ok=True)
        with open(_out, "w", encoding="utf-8") as _f:
            _f.write(_txt + "\n")
        print(f"✅ 报告已写入 {_out}（{len(_recs)} 帧，坏行 {_bad}）")
    else:
        print(_txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
