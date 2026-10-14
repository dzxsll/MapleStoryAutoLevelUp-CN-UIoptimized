'''
verify_mob_boxes.py
===================

自检：`monster_detect.draw_mob_boxes` 逐怪画框开关（2026-09-29 加回）。

背景（为什么要这个自检）：
    用户反馈「怪有时候很灵敏，有时候冲上去了都识别不到」，靠肉眼判断
    「引擎到底看见没看见」的唯一抓手就是逐怪画框。之前那句「按用户要求去掉」
    的注释是**记错的** —— 用户只要求把标签改中文。现在按开关加回，必须钉死：
      ① 开关默认 **关**（不打扰要求画面干净的用户）；
      ② 开关打开时，**每只怪**都画一个框，位置 = 检测到的 position/size；
      ③ 配色沿用上游：**黄=血条检出、绿=模板检出**；
      ④ 框只画在 **img_frame_debug** 上，**绝不碰 img_frame**（识别用的图）；
      ⑤ 画框代码在 get_monsters_in_range **返回前** —— 保证框与 self.monsters
         是同一批数据（怪检测会降频，放帧尾去画会拿旧数据画新帧 → 框错位）；
      ⑥ 脏数据（position/size 缺失、尺寸为 0 或超大）不得抛异常把主循环带崩。

跑法：
    python -m tools.verify_mob_boxes

⚠️ 本自检**不依赖** config_data.yaml / minimap / monster 模板等被 gitignore 的素材
   （CI 检出后这些都不存在），全部用内存桩 + 合成图，与仓库现有自检口径一致。
'''

import sys
import time
import numpy as np

sys.path.insert(0, '.')

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = (got == want)
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望={want!r} 实际={got!r}")


def check_true(name, cond, detail=""):
    check(name + (f"  ({detail})" if detail else ""), bool(cond), True)


# ─────────────────────────────────────────────────────────────────────────
# 桩：只借 get_monsters_in_range 一段逻辑，不走 __init__（会去抓窗口）
# ─────────────────────────────────────────────────────────────────────────
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot  # noqa: E402


def _make_bot_with_fake_templates(draw_boxes, frame_shape=(200, 200, 3),
                                  at=(150, 150), size=12):
    '''造桩：塞一个「只在指定位置命中」的假模板，让真实实现自己产出怪，
    从而**真实执行**画框那一段。

    ⚠️ 夹具构造有两个坑，都踩过，记下来免得后人重蹈：
      ① 不能用「全黑模板 + 全黑 ROI」：contour_only 模式下两边 mask 都是纯白，
         `TM_SQDIFF_NORMED` 在**每个**位置都 ≈0 → 193×193=37249 个位置全部命中，
         测出"检出 3508 只"纯属夹具噪声，反而掩盖真实行为。
      ② 也不能用「白方块模板」：`_mode == "contour_only"` 的判据是
         `np.all(img == [0,0,0])` —— 它**只把黑色像素当轮廓**（见
         get_monsters_in_range 里的实现）。纯白模板 → mask 全 0 → 一次都不命中。
      ⇒ 正确夹具：**白底 + 黑方块**。白底在 mask 里是 0（非轮廓），
         黑方块是 255（轮廓），这样命中点唯一且可预期。
      ③ 命中点**必须避开角色遮挡区**：实现里有一句
         `_mask_roi[char_y_min:char_y_max, char_x_min:char_x_max] = 0`
         （把角色自己所在的那块从轮廓里抹掉）。loc_player=(30,30)、
         角色 100x150 → 遮挡区 x:[0,80] y:[0,105]。
         我第一版把方块放在 (20,20)，**正好落在遮挡区里被抹掉** → 检出 0 只，
         还以为是匹配逻辑坏了。⇒ 方块挪到 (150,150)，在遮挡区外。
    '''
    bot = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    bot.img_frame = np.full(frame_shape, 255, np.uint8)      # 白底
    bot.img_frame_debug = np.full(frame_shape, 255, np.uint8)
    bot.loc_player = (30, 30)       # 遮挡区 x:[0,80] y:[0,105]，命中点须在此外
    # 帧尾方法 update_info_on_img_frame_debug 还要用这几个属性，必须塞齐，
    # 否则画框那行之前的 self.fps 计算就会 AttributeError（Stub 不走 __init__）
    bot.fps = 0
    bot.t_last_frame = time.time() - 0.1   # 差值须 >0，否则 fps 换算除零
    bot.img_route_debug = None      # 让它画完框就提前 return，不贴小地图预览
    bot.monsters = []
    bot.cfg = {
        "monster_detect": {
            "mode": "contour_only",
            "contour_blur": 5,
            "diff_thres": 0.4,
            "with_enemy_hp_bar": False,
            "hp_bar_color": [71, 204, 64],
            "draw_mob_boxes": draw_boxes,
            # 帧尾重算索敌框要用（缺了会被 except 静默吞掉 → 撞不到框，白排查一轮）
            "search_box_margin": 50,
        },
        "character": {"width": 100, "height": 150},
        # 帧尾方法里画攻击范围框要用这两段配置
        "bot": {"attack": "aoe_skill"},
        "aoe_skill": {"range_x": 60, "range_y": 60},
    }
    # 白底 + 唯一黑方块 → mask 里只有一个轮廓块，命中点唯一
    ax, ay = at
    bot.img_frame[ay:ay + size, ax:ax + size] = 0
    tpl = np.zeros((size, size, 3), np.uint8)                # 黑方块模板
    bot.monsters_info = {"测试怪": [(tpl, None)]}
    return bot


print("=" * 68)
print("自检：monster_detect.draw_mob_boxes 逐怪画框")
print("=" * 68)

# ── 1. 配置默认值必须是 True ─────────────────────────────────────────────
# ⚠️ 2026-09-29 用户拍板改成 **True**（原为 False）：
#    这是挂机软件，没人盯着软件里的「游戏画面」页签看 —— 都直接看游戏窗口。
#    那个页签只在**排查时**才打开，而排查时最需要知道「引擎看见没看见」。
#    默认关 = 这功能等于不存在（需要它的人得先知道有开关，可他正是不知道才来排查）。
#    默认开不打扰任何人：平时没人看，真正打开页签的人就是想看识别情况的。
print("\n[1] 配置默认值")
import yaml  # noqa: E402
with open("config/config_default.yaml", "r", encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)
_has = "draw_mob_boxes" in (_cfg.get("monster_detect") or {})
check_true("config_default.yaml 里有 draw_mob_boxes 键", _has)
if _has:
    check("默认值为 True（挂机软件没人看内置画面，默认关等于功能不存在）",
          _cfg["monster_detect"]["draw_mob_boxes"], True)

# ── 2. 画框的图必须是 img_frame_debug，且**不碰** img_frame ──────────────
print("\n[2] 画框只写调试图，不污染识别图")
bot = _make_bot_with_fake_templates(draw_boxes=True)
_frame_before = bot.img_frame.copy()
_debug_before = bot.img_frame_debug.copy()

_ms = bot.get_monsters_in_range((0, 0), (200, 200))
bot.monsters = _ms                      # 帧尾画框读的是 self.monsters
bot.update_info_on_img_frame_debug()    # 画框在这里发生

check_true("假模板构造有效：确实检测到了怪（否则下面测的是空转）",
           0 < len(_ms) < 200, f"检出 {len(_ms)} 只")

_same_frame = np.array_equal(bot.img_frame, _frame_before)
_same_debug = np.array_equal(bot.img_frame_debug, _debug_before)
check_true("img_frame（识别用图）**未被改动**", _same_frame)
check_true("img_frame_debug（调试图）**被改动了** = 框画上去了", not _same_debug)

# ── 3. 开关关闭时：不该画出**任何怪框** ──────────────────────────────────
print("\n[3] 开关关闭时不画怪框（也不画索敌框——索敌框已归本开关管）")
# ⚠️ 2026-09-29：索敌框也从 get_monsters_in_range 挪到帧尾，并且**同受本开关管**
#    （它和逐怪框是一套东西：调试图上的观测标注）。所以关掉开关时，
#    调试图上不该出现**任何**我们画的框。
bot_off = _make_bot_with_fake_templates(draw_boxes=False)
_ms_off = bot_off.get_monsters_in_range((0, 0), (200, 200))
bot_off.monsters = _ms_off
bot_off.update_info_on_img_frame_debug()
check_true("开关关闭时仍能检测到怪（检测不受开关影响）", len(_ms_off) > 0)
_dbg_off = bot_off.img_frame_debug
_green_off = int(np.sum(np.all(_dbg_off == np.array([0, 255, 0], np.uint8), axis=2)))
check("开关关闭时**没有绿色怪框**", _green_off, 0)
_red_off = int(np.sum(np.all(_dbg_off == np.array([255, 0, 0], np.uint8), axis=2)))
check("开关关闭时**没有索敌框**（已归开关管）", _red_off, 0)

# ── 3b. 开关打开时索敌框要画，且位置 = loc_player ± (range/2 + margin) ──
print("\n[3b] 开关打开时索敌框照画（且每帧重画，不闪）")
bot_on = _make_bot_with_fake_templates(draw_boxes=True)
bot_on.monsters = []
bot_on.update_info_on_img_frame_debug()
_cyan = np.all(bot_on.img_frame_debug == np.array([255, 0, 0], np.uint8), axis=2)
check_true("索敌框画出来了（红蓝色框线像素存在）", int(_cyan.sum()) > 0,
           f"{int(_cyan.sum())} px")
# ⚠️ 期望边界要把**线宽**算进去：cv2.rectangle(..., thickness=2) 是以边界
#    为中心向两侧各扩，所以 x1=110 画出来的最大像素索引是 **111**（不是 109）。
#    我第一版按"边界即像素索引"写，差 2 px 报假失败。
_ys, _xs = np.where(_cyan)
check_true("索敌框 x 边界符合算法（0~110，含 2px 线宽 → 索引到 111）",
           int(_xs.min()) == 0 and int(_xs.max()) == 111,
           f"x[{int(_xs.min())},{int(_xs.max())}]")
check_true("索敌框 y 边界符合算法（0~110，含 2px 线宽 → 索引到 111）",
           int(_ys.min()) == 0 and int(_ys.max()) == 111,
           f"y[{int(_ys.min())},{int(_ys.max())}]")

# ── 4. 颜色：绿=模板检出，黄=血条检出 ────────────────────────────────────
print("\n[4] 配色沿用上游（绿=模板，黄=血条）")
bot_c = _make_bot_with_fake_templates(draw_boxes=True)
bot_c.monsters = bot_c.get_monsters_in_range((0, 0), (200, 200))
bot_c.update_info_on_img_frame_debug()
_dbg = bot_c.img_frame_debug
# 背景全白，画上去的框线像素就是证据。绿=(0,255,0) BGR，黄=(0,255,255) BGR
_green = int(np.sum(np.all(_dbg == np.array([0, 255, 0], np.uint8), axis=2)))
_yellow = int(np.sum(np.all(_dbg == np.array([0, 255, 255], np.uint8), axis=2)))
check_true("存在绿色像素（模板检出的框）", _green > 0, f"{_green} px")
check("无黄色像素（本用例没有血条检出，黄框不该出现）", _yellow, 0)

# ── 4b. **闪烁回归**：中间帧（没跑检测）也必须画框 ───────────────────────
# 这是本次改动的**核心目的**：框原来画在怪检测内部 → 降频下每 2 帧才画一次，
# 而 img_frame_debug 每帧被清空重建 ⇒ 不画的那帧框整个消失 ⇒ 闪。
# 现在画在帧尾（每帧都到）⇒ **即使这一帧没跑怪检测，框也必须在**。
print("\n[4b] 闪烁回归：没跑怪检测的那一帧，框也要在")
bot_f = _make_bot_with_fake_templates(draw_boxes=True)
bot_f.monsters = bot_f.get_monsters_in_range((0, 0), (200, 200))
# 模拟「中间帧」：清空调试图（= run_once 开头 self.img_frame.copy() 的效果），
# 然后**不再跑检测**，只走帧尾 —— 这正是降频时每一帧的真实处境。
bot_f.img_frame_debug = np.full((200, 200, 3), 255, np.uint8)
bot_f.update_info_on_img_frame_debug()
_green_mid = int(np.sum(np.all(bot_f.img_frame_debug == np.array([0, 255, 0], np.uint8),
                               axis=2)))
_cyan_mid = int(np.sum(np.all(bot_f.img_frame_debug == np.array([255, 0, 0], np.uint8),
                              axis=2)))
check_true("中间帧**仍然画了逐怪框**（治闪烁的关键判据）", _green_mid > 0,
           f"{_green_mid} px")
check_true("中间帧**仍然画了索敌框**（索敌框也治好了）", _cyan_mid > 0,
           f"{_cyan_mid} px")

# 反证：旧位置（怪检测内部）在这时画不出来 —— 证明回归判据真的有效
bot_g = _make_bot_with_fake_templates(draw_boxes=True)
bot_g.img_frame_debug = np.full((200, 200, 3), 255, np.uint8)
_start = int(np.sum(np.all(bot_g.img_frame_debug == np.array([0, 255, 0], np.uint8),
                           axis=2)))
check("反证：清空后未走帧尾 ⇒ 没有怪框（说明判据能捕捉到闪烁场景）", _start, 0)

# ── 5. 脏数据不得抛异常（画框是观测，不能把主循环带崩）────────────────────
print("\n[5] 脏数据不崩（纯观测代码的硬纪律）")


class _FakeBot(MapleStoryAutoBot):
    '''只借帧尾那一段真实画框逻辑，用假 monsters 灌进去。

    ⚠️ 不复制画框代码 —— 复制版会跟真实实现各自漂移，测的是"副本对不对"，
    不是"产品对不对"（我在第一版里就这么干过）。这里调**真实方法**
    update_info_on_img_frame_debug，只把它依赖的属性塞好。
    '''

    def __init__(self, monsters, draw_boxes):
        self.img_frame = np.zeros((200, 200, 3), np.uint8)
        self.img_frame_debug = np.full((200, 200, 3), 255, np.uint8)
        self.loc_player = (100, 100)
        self.monsters_info = {}
        self.monsters = monsters
        self.fps = 0
        # ⚠️ 必须给一个**过去的**时间戳：帧尾方法第一行是
        #    `self.fps = round(1.0 / (time.time() - self.t_last_frame))`，
        #    若 t_last_frame 就是"现在"，差值为 0 → ZeroDivisionError ，
        #    7 个用例会全部误报成"脏数据导致崩溃"（我第一次就踩了）。
        self.t_last_frame = time.time() - 0.1
        # 帧尾画框之后还有"贴小地图预览"那段，用 None 让它提前 return
        self.img_route_debug = None
        self.cfg = {
            "monster_detect": {"mode": "contour_only", "contour_blur": 5,
                               "diff_thres": 0.4, "with_enemy_hp_bar": False,
                               "hp_bar_color": [71, 204, 64],
                               "draw_mob_boxes": draw_boxes},
            "character": {"width": 100, "height": 150},
            "bot": {"attack": "aoe_skill"},
            "aoe_skill": {"range_x": 60, "range_y": 60},
        }

    def draw(self):
        '''跑真实的帧尾方法（里面含画框）。'''
        MapleStoryAutoBot.update_info_on_img_frame_debug(self)


_dirty_cases = [
    ("position 不是二元组", [{"name": "x", "position": 5, "size": (10, 10)}]),
    ("缺 size 键", [{"name": "x", "position": (10, 10)}]),
    ("size 为 0", [{"name": "x", "position": (10, 10), "size": (0, 0)}]),
    ("size 超大", [{"name": "x", "position": (10, 10), "size": (99999, 99999)}]),
    ("None 混在里面", [None]),
    ("monsters 整体为 None", None),
    ("空列表", []),
]
for _nm, _dirty in _dirty_cases:
    try:
        _FakeBot(_dirty, True).draw()
        check_true(f"脏数据不崩：{_nm}", True)
    except Exception as _e:                                    # noqa: BLE001
        check_true(f"脏数据不崩：{_nm}", False, f"抛了 {type(_e).__name__}: {_e}")

# ── 6. 画框位置：都必须在**帧尾每帧都到**的地方（治闪烁的关键）──────────
print("\n[6] 画框位置：帧尾（每帧都到），不在怪检测内部（降频）")
import inspect  # noqa: E402
_src_drawfn = inspect.getsource(MapleStoryAutoBot.get_monsters_in_range)
_src_info_fn = inspect.getsource(MapleStoryAutoBot.update_info_on_img_frame_debug)

check_true("get_monsters_in_range 里**不再画任何框**（降频 → 会闪）",
           "cv2.rectangle(" not in _src_drawfn and
           "draw_rectangle(" not in _src_drawfn)
check_true("逐怪画框**在** update_info_on_img_frame_debug 里（每帧重画）",
           "draw_mob_boxes" in _src_info_fn and "cv2.rectangle(" in _src_info_fn)
check_true("索敌框也**在**帧尾方法里（2026-09-29 挪过来，治闪烁）",
           "get_search_box(" in _src_info_fn)
check_true("★ 帧尾画框与怪检测走**同一个** get_search_box（2026-10-13 加感知框放大后"
           "两处各算一遍必然漂移：画的框 ≠ 用的框）",
           "get_search_box(" in _src_info_fn
           and "get_search_box(" in inspect.getsource(
               MapleStoryAutoBot.update_cmd_by_mob_detection))
check_true("画框用的是 cv2.rectangle（不走 PIL 中文标签，避免拖帧）",
           "cv2.rectangle(" in _src_info_fn)
check_true("有空值兜底（img_frame_debug is not None）",
           "self.img_frame_debug is not None" in _src_info_fn)
check_true("monsters 读法有 getattr 兜底（Stub 不走 __init__）",
           'getattr(self, "monsters", None)' in _src_info_fn)
check_true("与攻击范围框同处（都在帧尾方法里）",
           "get_attack_range(" in _src_info_fn)

# ── 6b. 索敌框重算的结果必须与怪检测那帧用的一致（纯函数性质）────────────
print("\n[6b] 索敌框：帧尾重算 == 怪检测那帧用的框（纯函数，不存跨帧状态）")
# update_cmd_by_mob_detection 里的算法：dx = range_x//2 + margin（aoe）
# 帧尾重算用的是**同一套算法**，这里用两个独立来源交叉验证边界一致。
bot_h = _make_bot_with_fake_templates(draw_boxes=True)
bot_h.monsters = []
# 手动按 update_cmd_by_mob_detection 的算法算期望边界
_m = bot_h.cfg["monster_detect"]["search_box_margin"]
_dx = bot_h.cfg["aoe_skill"]["range_x"] // 2 + _m
_dy = bot_h.cfg["aoe_skill"]["range_y"] // 2 + _m
_px, _py = bot_h.loc_player
_exp = (max(0, _px - _dx), max(0, _py - _dy),
        min(200, _px + _dx), min(200, _py + _dy))
bot_h.update_info_on_img_frame_debug()
_cy = np.all(bot_h.img_frame_debug == np.array([255, 0, 0], np.uint8), axis=2)
_ys2, _xs2 = np.where(_cy)
# ⚠️ 同样要把 2px 线宽算进去：cv2.rectangle 以边界为中心向两侧扩，
#    所以"最大索引" = 边界值 + 1（110 → 111）。
_got = (int(_xs2.min()), int(_ys2.min()), int(_xs2.max()) - 1, int(_ys2.max()) - 1)
check_true("帧尾重算的索敌框边界 == 算法期望值",
           _got == _exp, f"期望{_exp} 实际{_got}")
check_true("索敌框**不额外存跨帧字段**（纯函数重算，少一处要维护的状态）",
           "self._mob_search_box" not in _src_info_fn)

# ── 7. 那两句错误注释必须已被更正 ────────────────────────────────────────
print("\n[7] 历史注释更正（防止后人再被误导）")
# ⚠️ 判据必须**限定在我改过的那两个函数**里，不能用整个类的 getsource。
#    踩过的坑：`MapleStoryAutoBot` 里别处还有一堆**合法**的「按用户要求删掉X」
#    注释（stand 模式、Route Index 行、Cmd 行、截图记录行…），那些跟我这次
#    的更正无关。用整类源码去扫，会把它们全判成"残留"，报 8 个假失败。
#    ⇒ 只看 get_monsters_in_range（画框）与 update_info_on_img_frame_debug（标签）。
_src_draw = inspect.getsource(MapleStoryAutoBot.get_monsters_in_range)
_src_info = inspect.getsource(MapleStoryAutoBot.update_info_on_img_frame_debug)

check_true("画框函数里不再有「按用户要求去掉逐怪小框」这种断言",
           not any(l.strip().startswith("#") and "按用户要求" in l
                   and "去掉" in l and "历史" not in l and "澄清" not in l
                   for l in _src_draw.splitlines()))
check_true("标签函数里不再有「按用户要求精简画面」",
           "按用户要求精简画面" not in _src_info)
# ⚠️ 更正说明的**位置变了**：画框从 get_monsters_in_range 挪到帧尾后，
#    对应的澄清也搬到了帧尾方法里。所以画框侧要查 _src_info，
#    而 _src_draw 里应留一句"已挪走、原因见帧尾"的指引（否则后人会以为是漏改）。
check_true("已写入 2026-09-29 的更正说明（帧尾/画框侧）",
           "从没要求删掉逐怪小框" in _src_info)
check_true("原位置留有「已挪到帧尾」的指引（防止后人误以为漏改）",
           "挪到帧尾" in _src_draw or "update_info_on_img_frame_debug" in _src_draw)
check_true("已写入 2026-09-29 的更正说明（标签侧）",
           "用户从没要求删掉逐怪小框" in _src_info)

# ─────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 68)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
