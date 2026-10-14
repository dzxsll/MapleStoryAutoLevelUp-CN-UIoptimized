'''
Execute this script:
python mapleStoryAutoLevelUp.py --map cloud_balcony --monster brown_windup_bear,pink_windup_bear
'''
# Standard import
import time
import random
import argparse
import glob
import sys
import logging
import os
import datetime
import threading
import shutil
import tempfile
import traceback
import json

# Library import
import numpy as np
import cv2
import yaml

# Local import
from src.utils.logger import (
    logger, jump_trace_on, perf_trace_on, perf_summary_line)
from src.utils.paths import resource_path, ensure_dir
from src.utils.common import (find_pattern_sqdiff, draw_rectangle, screenshot, nms,
    load_image, get_mask, flatten_alpha_to_green, imwrite_unicode,
    get_minimap_loc_size, get_player_location_on_minimap,
    override_cfg, load_yaml,
    click_in_game_window, mask_route_colors, route_size_mismatch,
    activate_game_window, normalize_pixel_coordinate,
    get_window_client_size, crop_frame_to_client, put_text_cn,
    resolve_title_bar_height,
    monster_detect_interval, edge_guard_step_cap,
    nametag_relax_decision,
    mob_no_attack_reason,
    monster_detect_snapshot,
    route_too_narrow_for_patrol,
)
# ── 手动示范录制（2026-10-14 加）：逐帧落盘 + 录真人按键 + 影子决策 ──────────
# 见 src/utils/demo_recorder.py 的文件头（用户 m07008 提的路子：他自己玩，
# 拿"人在同样画面上怎么做"当参照物，反推引擎的判据/阈值该松还是该紧）。
from src.utils.demo_recorder import DemoRecorder, demo_settings
from src.input.KeyBoardController import KeyBoardController, press_key
from src.input.KeyBoardListener import KeyBoardListener
from src.input.GameWindowCapturor import GameWindowCapturor
from src.engine.FiniteStateMachine import FiniteStateMachine
from src.states.hunting import HuntingState
# ── 回正线领域层（v0.9 第二版「谁近走谁」）──────────────────────────────────
# 连通域划分 / 几何现算 / 覆盖检查 / 逐条判废 / 距离 全是纯函数，
# 放在 utils 里是为了让「引擎跑的」和「离线自检测的」是**同一份代码**。
from src.utils.home_route import (
    manhattan, home_params, dist_to_pixels, home_should_return,
    split_components, collect_pixels,
    calc_line_geom, far_segment_stats, cover_stats, validate_home_line,
    home_pixel_clash, wipe_legacy_anchor, DEFAULT_HOME_PARAMS,
    home_summary_line,
)
# ── 卡死告警（2026-09-15 加）：判定卡死后**弹置顶提示窗**，不再乱动 ──────────
# 见 config_default.yaml 的 `watchdog.on_stuck` 与 src/utils/stuck_alert.py 的文件头。
from src.utils import stuck_alert
# ── 血蓝监控 + 自动喝药（2026-09-27 加）────────────────────────────────────
# 三个纯逻辑模块移植自 Alan-xzwzz 的 PR #3（MIT，署名见各文件头）：
#   bars.py          条状 UI 识别（HSV 阈值 + 按列统计）
#   bar_smoothing.py 读数平滑（抑制血条闪烁造成的 0 值跳变）
#   potions.py       喝药决策（四层防误触）+ 带前台守卫的按键执行
from src.utils.bars import BarsDetector
from src.utils.bar_smoothing import BarsSmoother
from src.utils.potions import PotionDrinker, press_potion
from src.utils.env_precheck import check_bars_environment, format_for_log

class MapleStoryAutoBot:
    '''
    MapleStoryAutoBot
    '''
    def __init__(self, args):
        '''
        Init MapleStoryAutoBot
        '''
        self.args = args # User args
        self.cfg = None # Configuration
        self.idx_routes = 0 # Index of route map
        self.monsters_info = {} # monster information
        self.monsters = [] # monster detected in current frame
        self.fps = 0 # Frame per second
        self.video_writer = None # For video recording feature
        self.video_tmp_path = None   # 实际写入的临时文件（纯 ASCII 路径）
        self.video_final_path = None # 最终要落到的 video/ 位置
        self.color_code = {} # For color code instruction
        self.color_code_up_down = {} # Color code only contain 'up' and 'down'
        self.thread_auto_bot = None # thread for running autobot
        self.cmd_move_x = "none" # "left" "right"
        self.cmd_move_y = "none" # "up" "down"
        self.cmd_action = "none" # "jump" "attack" ....
        # 失联救援锚点：最近一次成功匹配的路线色码像素（get_nearest_color_code 维护）
        self.loc_last_route_pixel = None
        # Signals (for UI)
        self.image_debug_signal = None
        self.route_map_viz_signal = None
        # Flags
        self.is_first_frame = True # first frame flag
        self.is_terminated = False # Close all object and thread if True
        self.is_on_ladder = False # Character is on ladder or not
        # ── 「真的在爬」的竖向进展戳（2026-10-14 加，用户拍板 A）──────────────
        # 为什么：`is_on_ladder` **只进不出**（清除的两条出口都要横向移动），
        #   怪逻辑的绳/梯让路被粘滞旗标一路吃掉 —— 示范录制实测梯上帧占 43.9%，
        #   其中 44/105 段整段全局 y 零变化（人在这些帧里出手 910 次）。
        #   ⇒ 让路判据改成「旗标为真 **且** 最近 route.ladder_still_s 秒内有竖向进展」。
        # 怎么喂：跟踪块里唯一写入点，只认**小地图全局 y 变化**（零误报，见那里注释）。
        self.t_ladder_move = 0.0     # 最近一次"小地图全局 y 变了"的时刻
        self._ladder_gy = None       # 上一帧的小地图全局 y（判竖向进展）
        self.t_ladder_stale_log = 0.0 # 「旗标粘着、不再让路」那行日志的 5s 节流戳
        # ── 绳梯脚下"蹭一下"自救（2026-10-14 加，用户拍板 D）──────────────────
        # 见 config/config_default.yaml 的 route.rope_nudge_* 长注释与 `_rope_nudge_dir()`。
        # 为什么要有：角色停在绳子上时引擎只发纯 up，而"挪进绳子"的横向补位
        #   只在 is_on_ladder 为真时才加 ⇒ 不动就永远不动（真机 45~50s 僵持）。
        # 这里只存日志节流戳与一个纯观测计数（判据全在 `_rope_nudge_dir()`）。
        self.t_rope_nudge_log = 0.0   # 「绳梯自救」那行日志的 1.5s 节流戳
        self._rope_nudge_n = 0        # 本次开跑累计蹭过的帧数（纯观测）
        # ── 爬梯"走完"（2026-10-15 加，用户 m11669）──────────────────────────
        # 见 config/config_default.yaml 的 route.ladder_finish* 长注释与
        # `_ladder_finish_tick()`。为什么要有：梯子上按左右**不动**（人还挂在绳上），
        #   而路线有时这帧只给横向像素 ⇒ 键一直在发、人不动，怪逻辑趁机站桩打怪。
        #   真机取证（log/MSBot_2026-10-15_00-36-31.log，26 分钟）：
        #   全局(210,60) 停 30s（竖向指令 0/6 帧、5/6 帧有怪）、(78,74) 停 15s、
        #   (180,64) 停 15s —— 同一段里 `旗标粘着：6550ms ⇒ 不再让路` 反复出现。
        self._last_vert_cmd = "up"     # 路线最近一次给的竖向指令（补的时候用）
        self._ladder_push_t0 = 0.0     # 本轮"补竖向"的起表时刻（0 = 没在补）
        self._ladder_push_cool = 0.0   # 补超时仍无进展 ⇒ 冷却到这个时刻
        self.t_ladder_finish_log = 0.0 # 「[爬梯] …」那行日志的节流戳
        self.is_show_debug_window = not args.disable_viz #
        self.is_need_show_debug_window = not args.disable_viz #
        self.is_disable_control = args.disable_control
        self.is_ui = args.is_ui # Whether is using UI framework to invoke engine
        self.is_frame_done = False #
        # Coordinate (top-left coordinate)
        self.loc_nametag = (0, 0) # nametag location on game screen
        self.loc_minimap = (0, 0) # minimap location on game screen
        self.loc_player = (0, 0) # player location on game screen
        # 名字定位健康度（2026-09-10 加）：连续多少帧没匹配上角色名名牌。
        # 匹配失败时 loc_nametag 保持旧值 → 位置会「冻住」却毫无提示，
        # 所以由主循环在连续失败时告警（见 update 里的 [名字定位] 告警）。
        self.nametag_miss_streak = 0
        self.nametag_hit = False
        # 开局至今有没有成功命中过。用来区分两种严重程度完全不同的情况：
        #   命中过再失败 → 无害（同图内角色屏幕位置几乎是死的，沿用上次结果即可）
        #   一次都没命中 → 严重（位置停在初始值，攻击框跑到画面左上角，且不报错）
        self.nametag_ever_hit = False
        # 「上次命中的名字锚点」按地图存盘（2026-10-14 加，见 _load_nametag_pos /
        # _save_nametag_pos）。为什么需要：名字认不到时引擎沿用**上一帧**的锚点
        # （位置被冻住），可"开局那几帧"没有上一帧 ⇒ 位置停在 (0,0)，
        # 攻击框落到画面左上角 = 只走不打且不报错。
        self.loc_nametag_from_cache = False   # 开局的 loc_nametag 是否来自存盘文件
        self.t_nametag_save = 0.0             # 上次写盘时间（节流用）
        self.nametag_pos_save_failed = False  # 写盘失败只吼一次（fail-open）
        # ── 丢名字存画面（2026-10-14 加，诊断用，见 _dump_nametag_miss）────────
        self.t_nt_miss_dump = 0.0             # 上次存图时间（5 秒节流用）
        self.nt_miss_dump_failed = False      # 存图失败只吼一次（fail-open）
        # ── 名字定位「抗闪」（2026-10-14 加，判据见 src/utils/common.py 的
        #    nametag_relax_decision）────────────────────────────────────────
        # 分数落在"半峰"（认不出但又远没到完全失配）时，只要位置不跳就采信这一帧，
        # 目的是**别让 miss 计数涨过 stale_pos_full_frame_after**（涨过之后索敌框
        # 放大到整帧 ⇒ 怪数虚高 + 去追画面外的目标，真机日志实证）。
        self._nt_strict_pos = None     # 上次**严格命中**算出的角色位置（放宽判据的锚点）
        self.nametag_relaxed = False   # 本帧是不是"放宽采信"（观测用）
        self.nametag_relax_hits = 0
        # 「没出招·归因」日志的节流时间戳（2026-10-14）。读的时候一律 getattr 兜底：
        # 离线自检的 Stub 不走 __init__（见 _mob_no_attack_log）。
        self.t_mob_noatk_log = 0.0    # 「没出招·归因」的节流时间戳
        # 手动示范录制（2026-10-14 加）：`demo_record.enabled: true` 时在 start()
        # 里换成 DemoRecorder 实例，否则恒为 None（`record_demo_frame` 直接返回）。
        self.demo = None
        # [怪检测] 明细行（2026-10-14 加，纯观测）：用户 m06852「记录下怪物的检测
        #   信息，现在的攻击还是不够灵敏」—— 只写「怪N」看不出它在哪、为什么不算
        #   够得着。这里记节流时间戳、上一帧"框内集合"的指纹、以及**第一只怪进框**
        #   的时刻（出招时用它算"进框→首刀"的毫秒数 = 灵敏度）。
        #   一律 getattr 兜底（离线自检的 Stub 不走 __init__）。
        self.t_detect_log = 0.0       # [怪检测] 的节流时间戳
        self._det_log_sig = None      # 上一帧攻击框内那批怪的指纹（变了就立刻打）
        self._det_log_failed = False  # 明细行生成失败只吼一次
        self._eng_first_t = None      # 第一只怪进攻击框的时刻（灵敏度基准）
        # [出招] 心跳（2026-10-14 加，纯观测）：用户反馈「攻击就是很慢 / 怪在攻击
        #   范围内了就是不攻击」—— 把「真的出招了几次 / 间隔多少 / 出招时框内几只怪」
        #   摊在日志里，一眼分清是 (a) 引擎不出招（框内没怪 / 冷却未到）还是
        #   (b) 出了招但游戏没反应。一律 getattr 兜底（离线自检的 Stub 不走 __init__）。
        self._atk_n = 0               # 本窗口出招次数
        self._atk_gap_n = 0           # 有效间隔样本数（第一次出招没有"上一次"）
        self._atk_gap_sum = 0.0
        self._atk_gap_min = None
        self._atk_gap_max = 0.0
        self._atk_eng_min = None      # 出招时攻击框内的怪数（区间）
        self._atk_eng_max = 0
        # [开火预算]（2026-10-14 加，纯观测，P4；用户 m10830「把'人眼的实时反应'
        #   拆解成'适合低频检测的确认流程'」）：把"从看见一只怪到真正出刀"拆成
        #   四段计时 + 丢失原因计数，每 5 秒随 [运行状态] 打一行 `[开火预算]`。
        #   为什么需要它：`[出招·延迟]` 只量最后一段（进框→出刀，真机 p50 33ms），
        #   而审计（log/MSBot_2026-10-14_17-40-51.log 段11）显示时间花在
        #   "该不该打/走不走得过去"上 ⇒ 没有分段计时就只能猜是哪一段慢。
        #   一律 getattr 兜底（离线自检的 Stub 不走 __init__）。
        self._budget_atk_ts = []      # 最近 60s 的出招时刻（算覆盖率）
        self._budget_vis_t0 = None    # 本段"看见框外可追目标"的起始时刻
        self._budget_confirm_t = None # 上一次"确认可追"的时刻（量 确认→进框）
        self._budget_see2ok = []      # 可见→确认（秒）
        self._budget_ok2box = []      # 确认→进框（秒）
        self._budget_box2hit = []     # 进框→出刀（秒）
        self._budget_lost = {}        # 丢失原因计数（高差/未确认/让路/爬绳放弃）
        self._budget_warned = False   # 统计失败只吼一次
        self.loc_player_minimap = (0, 0) # player location on minimap
        self.loc_minimap_global = (0, 0) # minimap location on global map
        self.loc_player_global = (0, 0) # player location on global map
        self.loc_watch_dog = (0, 0) # watch dog location on global map
        # Images
        self.frame = None # raw image
        self.img_frame = None # game window frame
        self.img_frame_gray = None # game window frame graysale
        self.img_frame_debug = None # game window frame for visualization
        self.img_route = None # route map
        # 回正线（minimaps/<图>/route_home.png）：偏离主线 / 掉出平台后走的「回家的路」。
        # 单独一份、不进 img_routes —— 主线是巡逻/驻守路径，回正线是回到主线的路径，
        # 两者语义不同，混用会导致「掉出去后沿巡逻路线走，反而越走越远」。
        # 2026-09-12 新增（思路与上游 legacy 的 route_rest.png 相同）。
        self.img_route_home = None
        self.is_using_home_route = False   # 当前是否在走回正线
        # ── v0.9 第二版回正跨帧状态（全部只在 _enter/_exit_home_route 里维护）──
        # ⚠️ 纪律：新增状态必须配一个明确出口，否则会造出「既不走也不打」的死局。
        #    这些字段的出口就是 _exit_home_route（正常回主线 / 接不上 / 8s 超时 三条路）。
        self.home_lines = []        # list[dict]：加载期从 route_home.png 扫出的回正线（已剔废线）
        self.home_line = None       # 当前这次回正走的是哪条线（dict | None）
        # 【新】本帧的两个距离 —— **每帧只在 update_cmd_by_route 插入点①算一次**，
        # 插入点②（超时 / 驻留 / try_return）只读不算，保证一帧内口径唯一。
        self.home_d_home = None     # 到最近「已武装」回正线的距离（None = 没有可用回正线）
        self.home_d_main = None     # 到最近主线像素的距离（None = 量不出 / 没有主线）
        self._home_line_near = None # 插入点①缓存：最近的那条已武装回正线
        self._main_seg_i = 0        # 插入点①缓存：最近主线像素属于第几段（退出时切回它）
        self._main_pts_xy = None    # (xs, ys) np.int32：主线全部色码像素（load 时建一次）
        self._main_pts_seg = None   # 每个主线像素属于第几段（与上面等长）
        self.home_enter_pos = None  # 进入回正时的角色全局坐标 (x, y)（最小驻留门用）
        self.home_enter_t = 0.0     # 进入回正的时刻（8s 超时出口用；0 = 没在回正）
        self.home_frame = 0         # 已处于回正状态的帧数（最小驻留门用）
        self.home_last_goal_log = 0.0   # 「踩到回正线终点」日志节流
        # ⚠️ 回正**失败**后的入口冷静期（QA 实测必修，见 _exit_home_route 的说明）：
        #    没有它，超时退出的那一帧会被「主线附近找不到像素」的老路径**立刻**拉回
        #    回正状态 → home_enter_t / home_frame 归零 → 8s 超时永远走不到 → 死循环。
        self.home_reenter_lock_t = 0.0  # 上次"回正失败"的时刻；0 = 没锁
        self.t_last_home_lock_log = 0.0 # 「冷静期内不回正」日志节流
        self.img_route_debug = None # route map for visualization
        self.img_minimap = np.zeros((10, 10, 3), dtype=np.uint8) # minimap on game screen
        # Timers
        self.t_last_frame = time.time() # Last frame timer, for fps calculation
        self.t_watch_dog = time.time() # Last movement timer
        self.t_last_attack = time.time() # Last attack timer for cooldown
        self.t_last_minimap_update = time.time()
        # 运行状态日志（2026-09-12 加）：主循环每 5 秒打一行关键状态。
        # ⚠️ 为什么必须有这一行：主循环原来**一帧不打日志**，于是「点开始、角色不动」
        #    这件事在日志里完全查不到 —— 用户原话「日志里啥都没有」。
        #    有了它，「循环在跑但指令是 none」和「循环已经死了」一眼可分。
        self.n_frame = 0 # 主循环已处理帧数
        self.t_last_status_log = time.time() # 上次打运行状态的时间
        self.n_loop_err = 0 # 主循环连续异常帧数
        self.t_last_loop_err = 0.0 # 上次打异常告警的时间（节流用）
        # ── 主循环性能埋点（2026-09-14 加，见 _perf 与 perf_summary_line）────
        # 背景：配置 fps_limit_main=30，**真机实测只有 1.8~2 fps**，而全项目原来
        # 没有任何地方记录主循环的实际帧率（键盘线程会打印 fps=，主循环不会）
        # → "一帧 500ms 到底花在哪"完全靠猜。这几个累加器就是量它用的。
        # perf_on 在 load_config 末尾用 perf_trace_on(cfg) 赋**一次**（别每帧 try）。
        self.perf_on = False        # 只影响打不打日志，不影响任何判定与行为
        self.perf_acc = {}          # {阶段名: 本窗口累计秒}
        # ⚠️ 实际执行次数：怪检测会**降频**（每 N 帧才跑一次），它的次数 < 帧数。
        #    汇总时用**它**当分母，日志里显示的才是"跑一次要多久"，
        #    而不是被摊薄后的假数字（见 _perf / 汇总处）。
        self.perf_cnt = {}          # {阶段名: 实际执行次数}
        self.perf_max = {}          # {阶段名: 单帧峰值秒}
        self.perf_n = 0             # 本窗口帧数（在 loop 里 +1，不在 run_once）
        self.perf_gap = 0.0         # 本窗口帧间隔累计秒（含限帧 sleep）
        self.t_last_perf_log = time.time()
        self.t_perf_prev = time.time()

        # ── 血蓝监控 + 自动喝药（2026-09-27 加）────────────────────────────
        # 全是"库里没有就自己降级"的可选组件：配置里没 bars/potion 段时
        # 三者都是 None，主循环整段跳过 —— 这样老配置不会因为本次改动崩掉。
        self.bars_detector = None   # BarsDetector：识别血条/蓝条
        self.bars_smoother = None   # BarsSmoother：读数平滑（抗闪烁）
        self.potion_drinker = None  # PotionDrinker：喝药决策
        self.bars_ok = False        # 环境预检是否通过（不通过则不喝药）
        self.hp_percent = None      # 最近一次平滑后的血量（None = 当前不可信）
        self.mp_percent = None      # 最近一次平滑后的蓝量
        self.potion_enabled = True  # 喝药总开关（界面可实时切换）
        self.n_potion_drunk = 0     # 本次运行累计喝了几次（给界面显示）
        self.t_last_bars_log = time.time()  # 血蓝读数日志节流（每 5 秒一行）

        # 上一帧**实际发出**的左右指令（2026-09-12 加）
        # ⚠️ 为什么需要它：出招必须**先转身再按技能键**，否则技能会朝旧朝向飞出去，
        #    表现就是「后方的怪打不到」。上游 config 里躺着 character_turn_delay: 0.02，
        #    但整个 src/ 里一处引用都没有 —— 是没做完的半成品，这里把它补上。
        # 由 hunting.on_frame 在 set_command 之后更新。
        self.cmd_move_x_last = "none"
        # ★ 2026-09-17：转身状态机（「回头打怪」用，见 update_cmd_by_mob_detection）
        #   _turn_frames_left：还要按住方向键**转向**多少帧（>0 时不出招）
        #   _turn_dir        ：上一帧转向的目标方向（怪换边时重新计时）
        #
        #   为什么需要它：原来「朝向不对 → 本帧只发方向键、下一帧就出招」，
        #   转向只维持 1 帧（≈33ms）。而**出招那一帧仍然按着方向键**
        #   —— 技能是按「当前朝向」飞出去的，同帧按方向+技能，技能会朝**转身前**
        #   的朝向飞走。于是每次攻击都朝路线前进方向甩出去，后方的怪永远打不到，
        #   表现就是用户说的「后面有怪却一直在往前面砍空气」。
        self._turn_frames_left = 0
        self._turn_dir = None
        # ★ 2026-09-29：转身期间目标换边时，把"转完该打哪边"暂存在这里。
        #   用途见 update_cmd_by_mob_detection 的「换边抑制」说明：
        #   目标在左右之间横跳时**不打断**正在进行的转身，否则转身永远转不完
        #   ⇒ 一帧都出不了招（issue #12 的「原地左右抽搐但不攻击」）。
        self._turn_pending_dir = None
        # ★ 2026-09-29：上一次判定的攻击方向，用于**迟滞**（见 get_attack_direction）。
        #   作用：两侧都有怪且距离相当时，防止方向逐帧翻面导致「原地左右抽搐、
        #   一帧都不出招」（issue #12）。只在另一侧明显更近时才允许翻面。
        self._attack_dir_last = None
        # 上次打「回正线接不上」告警的时间（节流用，避免每帧刷屏）
        self.t_last_home_fail_log = 0.0
        # 单段路线自动往返（2026-09-12 加）：只录了一段时，走到头自动掉头往回走。
        # 详见 _apply_pingpong 的说明。
        self.route_reverse = False
        self._pingpong_start = None
        # 每一段的终点（goal 圆点的质心）+ 主方向 + 跨度 —— 用来判断"这段走完了"。
        # ⚠️ 不能只靠"踩到 goal 像素"：goal 半径才 2px，而玩家坐标是**质心**、
        #    踩不中就判不到 —— 短路线（平台就那么短）会直接走过去 → 永远不切段 →
        #    冲出平台（用户实测：到终点停不下来一直往前走）。
        #    ⚠️★ 2026-09-16 订正：这里原来写的是"角色一帧能走十几像素"，**那是推断、
        #       从没实测**，实测只有 ~0.3 px/帧（≈4~5 px/秒，实测依据见 _near_seg_goal
        #       里"上限三"那段），差了 20~50 倍。症状是真的，但这个数字是猜的 ——
        #       别再拿它当依据（它正是 goal_reach_range=15 的来源，而那 15px 后来
        #       把短路线的巡逻行程吞成了 2px）。
        #       至于"踩不中"的真正机理：没查实（可能是坐标取整/质心偏差），
        #       但"越过终点也算到"（方向感知）这个修法与速度无关，是对的。
        self._seg_goals = []
        self._seg_dir = []
        self._seg_span = []
        self._goal_armed = True
        # Images
        self.img_map = None
        self.img_routes = []
        self.img_nametag = None
        self.img_nametag_gray = None
        self.img_login_button = None

        # Database
        self.data = load_yaml("config/config_data.yaml")
        # Threads & Objects
        self.kb = None # Keyboard controller
        self.capture = None # Game window capturor

        # Finite State Machine
        # 国服怀旧服无符文系统（已核实客户端元数据）；巡逻模式已移除，状态机只剩「打怪」一态
        self.fsm = FiniteStateMachine()
        self.fsm.add_state(HuntingState    ("hunting"     , self))
        self.fsm.set_init_state("hunting")

    def update_signals(self, image_debug_signal, route_map_viz_signal):
        '''
        Update signal from UI framework.
        For debug window viz
        '''
        self.image_debug_signal = image_debug_signal
        self.route_map_viz_signal = route_map_viz_signal

    def _check_cfg_completeness(self, cfg):
        '''
        拿 config_default.yaml 当基准，检查传进来的 cfg 有没有缺键，缺了就报 ERROR。

        ⚠️ 为什么必须查（2026-09-12 实测踩坑，代价是一整天的排查）：
           界面保存的用户配置是「局部配置」——只写用户改过的键。一旦某条路径把某个段
           **整段替换**成局部版本，config_default 里的默认键就整段丢了。
           本次实例：nametag 段只剩 name/offset，引擎第一帧读 cfg["nametag"]["mode"]
           → KeyError → 主循环线程**静默死亡** → 症状「点开始、角色不动、日志全静默」。
           在这里把它报成 ERROR，就把「静默崩溃」变成了「开跑第一眼就能看见」。

        返回缺失键列表（空列表 = 配置完整）。
        '''
        try:
            base = load_yaml("config/config_default.yaml")
        except Exception as e:
            logger.warning(f"[配置校验] 读不到 config_default.yaml，跳过校验: {e}")
            return []
        missing = []

        def walk(b, c, path):
            for k, v in b.items():
                p = f"{path}.{k}" if path else k
                if not isinstance(c, dict) or k not in c:
                    missing.append(p)
                elif isinstance(v, dict):
                    walk(v, c[k], p)

        walk(base, cfg, "")
        if missing:
            shown = ", ".join(missing[:12])
            more = f" …（共 {len(missing)} 个）" if len(missing) > 12 else ""
            logger.error(
                f"[配置校验] 配置缺少 {len(missing)} 个必需键：{shown}{more}\n"
                f"        这些键本该由 config/config_default.yaml 提供 —— 说明保存配置时"
                f"把默认值丢了（典型原因：用户配置只写了部分键，合并时按「整段替换」处理）。\n"
                f"        不修的话引擎会在运行中因缺键崩溃，症状是「角色不动 + 日志突然静默」。")
        return missing

    def _check_key_names(self, cfg):
        '''校验配置里的按键名，驱动认不认（2026-09-28 加，issue #6 的真凶）。

        为什么必须有这一层（真机现场）：
            界面上的按键是**捕获式**的（你按哪个键就填哪个），产出的是 Qt 的叫法。
            而 Interception 驱动有**自己的键名表**。两者不总是一致 ——
            最典型的就是 **Ctrl 键：界面写 "Control"，驱动只认 "ctrl"**。

            后果极其隐蔽（issue #6 那位用户）：
              · 引擎照常算出 attack 指令，日志里能看到 `指令(none none attack)`
              · 但 `key_down("control")` 每一帧都抛 UnknownKeyError
              · 而失败上报是"每类只报一次"的 warning，很容易淹没
              ⇒ 用户看到的是「**走位完全正常，但一次都不打怪**」
                —— 因为方向键名（left/right/up/down）驱动都认，只有技能键死了。

        `InterceptionController.normalize_key` 已经能自动纠正已知的同义词
        （control→ctrl 等）。这里做的是**兜底**：万一还有没覆盖到的键名，
        在启动时就**明确报出来**，而不是让它静默失效一整天。

        ⚠️ 只报不拦：这是"可能不影响"的问题（比如用户填的键恰好是驱动认的别名），
           而且按键链路本身有 normalize_key 兜着。宁可多一句提示，也别把挂机堵死。
        '''
        try:
            from interception._keycodes import get_key_information, UnknownKeyError
        except Exception:                                            # noqa: BLE001
            return                    # 驱动没装/导入失败 → 别的检查会报，这里不添乱

        from src.input.InterceptionController import normalize_key

        # 收集配置里所有"会真的发给驱动"的键
        key_cfg = cfg.get("key", {}) or {}
        items = []
        for name in ("directional_attack", "aoe_skill", "jump", "teleport",
                     "return_home"):
            k = key_cfg.get(name)
            if k:
                items.append((f"按键绑定里的「{name}」", k))
        for i, k in enumerate(((cfg.get("buff_skill", {}) or {}).get("keys")) or []):
            if k:
                items.append((f"buff 技能第 {i + 1} 个", k))

        # 药水键（hp/mp 各档位的 key）
        pot = cfg.get("potion", {}) or {}
        for kind in ("hp", "mp"):
            for entry in (pot.get(kind) or []):
                if isinstance(entry, dict) and entry.get("key"):
                    items.append((f"喝药（{kind}/{entry.get('name', '?')}）",
                                  entry["key"]))

        bad = []
        for where, raw in items:
            k = normalize_key(raw)
            try:
                get_key_information(k)
            except UnknownKeyError:
                bad.append((where, raw, k))
            except Exception:                                        # noqa: BLE001
                pass

        if bad:
            lines = [
                f"[按键校验] 有 {len(bad)} 个按键，驱动不认识 —— "
                f"**这几个键按下去游戏不会有任何反应**："]
            for where, raw, k in bad:
                lines.append(f"        · {where}：填的是 {raw!r}"
                             f"（转换后 {k!r} 仍不被驱动接受）")
            lines += [
                "        影响：对应的功能会**静默失效** ——",
                "          技能键不认识 → 角色走位正常但一次都不出招；",
                "          药水键不认识 → 血蓝低了不会自动喝。",
                "        怎么改：在界面上把这个键**重新按一次**（换一个更常见的键，",
                "          比如字母键或数字键），或者直接编辑 config 里的键名。",
                "        常见对照：Ctrl 要写成 ctrl、PageDown 要写成 pgdn、",
                "          Insert 要写成 insert。"]
            logger.error("\n".join(lines))
        else:
            logger.info(f"[按键校验] {len(items)} 个按键都正常（驱动认识）。")

    def _setup_bars_and_potions(self, cfg):
        '''装配血蓝识别 + 喝药决策（2026-09-27 加）。

        失败策略：**降级，不崩**。
            这一段是可选功能，任何一步出问题（配置没写、roi 配错、
            环境对不上）都只把功能关掉并打日志，绝不让 load_config 抛异常 ——
            否则"喝药配置写错"会连带把整个挂机工具堵死，代价完全不成比例。

        环境预检放在这里的原因：
            血蓝的 roi 是按 1366x768 窗口化量死的，窗口一大一小位置就偏。
            预检**不通过就不启用喝药**（读数不可信时宁可不喝），
            但**不影响**挂机本身 —— 照常打怪，只是不自动喝药。
        '''
        self.bars_detector = None
        self.bars_smoother = None
        self.potion_drinker = None
        self.bars_ok = False
        self.hp_percent = None
        self.mp_percent = None
        self.n_potion_drunk = 0

        bars_cfg = cfg.get("bars")
        potion_cfg = cfg.get("potion")
        if not bars_cfg or not potion_cfg:
            # 老配置 / 用户把段删了 —— 静默跳过（这是合法状态，不是错误）
            logger.info("[血蓝监控] 配置里没有 bars / potion 段，本次不启用自动喝药。")
            return

        # ── 环境预检：不对就拦住（说清怎么改），只关掉喝药 ──────────────
        try:
            ok, title, message = check_bars_environment(cfg)
        except Exception as e:                                       # noqa: BLE001
            # 预检自身出错 → 不敢启用（读数可能不可信），但不拦挂机
            logger.warning(f"[血蓝监控] 环境预检本身出错，本次不启用自动喝药：{e}")
            return

        self.bars_ok = bool(ok)
        if not ok:
            logger.error(format_for_log(False, title, message))
            logger.error(
                "[血蓝监控] 因为上面这个原因，本次**不启用自动喝药**"
                "（挂机打怪照常进行，只是血蓝低了不会帮你按药水键）。")
            return

        logger.info(format_for_log(True, title, message))

        # ── 构建检测器 / 平滑器 / 决策器 ───────────────────────────────
        try:
            self.bars_detector = BarsDetector.from_dict(bars_cfg)
            # 用一帧的尺寸做一次完整校验（此刻还没有真帧，用配置里的画面尺寸）
            target_h, target_w = cfg["game_window"]["size"]
            self.bars_detector.validate((int(target_h), int(target_w), 3))
        except Exception as e:                                       # noqa: BLE001
            self.bars_detector = None
            # ⚠️ bars_ok 必须一起复位（自检 verify_bars_potions 的用例 15 抓出来的）：
            #    界面靠 bars_ok 决定"要不要显示实时读数"，若环境过了但配置非法，
            #    bars_ok 留着 True 会让界面显示一行永远不更新的读数，
            #    反而不如老实显示"没在工作"。
            self.bars_ok = False
            logger.error(
                f"[血蓝监控] 血条/蓝条配置不合法，本次不启用自动喝药：{e}\n"
                f"        这些值本该由 config/config_default.yaml 提供；"
                f"如果改过 bars 段，把它改回出厂值或删掉 config_custom.yaml 里的覆盖。")
            return

        try:
            # 平滑窗口 / 最大连续无效帧：与 PR 出厂值一致（见交接文档的实测结论）
            self.bars_smoother = BarsSmoother(window=3, max_invalid_frames=10)
            self.potion_drinker = PotionDrinker.from_dict(potion_cfg)
            self.potion_drinker.enabled = bool(potion_cfg.get("enabled", True))
            self.potion_enabled = self.potion_drinker.enabled
        except Exception as e:                                       # noqa: BLE001
            self.bars_detector = None
            self.bars_smoother = None
            self.potion_drinker = None
            self.bars_ok = False     # 同上：功能没起来，界面不该显示"在工作"
            logger.error(
                f"[血蓝监控] 喝药配置不合法，本次不启用自动喝药：{e}\n"
                f"        检查 config 的 potion 段：每个档位要有 name / threshold / key，"
                f"key 可以是 null（表示这一档不按键）。")
            return

        # 配置摘要（一行，方便"到底配了什么"一眼可查）
        hp_keys = ", ".join(f"{t.name}<{t.threshold:.0f}%→{t.key or '不喝'}"
                            for t in self.potion_drinker.hp.tiers) or "无"
        mp_keys = ", ".join(f"{t.name}<{t.threshold:.0f}%→{t.key or '不喝'}"
                            for t in self.potion_drinker.mp.tiers) or "无"
        logger.info(
            f"[血蓝监控] 已启用自动喝药。\n"
            f"        血量：{hp_keys}（冷却 {self.potion_drinker.hp.cooldown_sec}s）\n"
            f"        魔法：{mp_keys}（冷却 {self.potion_drinker.mp.cooldown_sec}s）\n"
            f"        连续 {self.potion_drinker.hp.confirm_frames} 帧低于阈值才按键；"
            f"按键前会确认游戏窗口在最前面。")

    def _step_bars_and_potions(self, img_frame):
        '''每帧：识别血蓝 → 平滑 → 决定要不要喝 → 真按键（2026-09-27 加）。

        ⚠️ 全段 try/except 兜底：它跑在主循环里，抛异常 = 整个挂机停摆。
           "少喝一瓶"远比"脚本死掉"轻 —— 所以任何异常都只记日志、不往外抛。
        '''
        if self.bars_detector is None:
            return

        try:
            hp_res, mp_res = self.bars_detector.detect(img_frame)
            self.hp_percent, self.mp_percent = self.bars_smoother.update(hp_res, mp_res)

            if self.potion_drinker is None:
                return

            # 界面上的总开关（用户随时可关）——同步进决策器
            self.potion_drinker.enabled = self.potion_enabled

            decision = self.potion_drinker.evaluate(self.hp_percent, self.mp_percent)
            if decision.should_drink:
                # ⚠️ 按键走 press_potion：它内部有**前台守卫 + return**，
                #    不在游戏窗口前台时一个键都不发（见 potions.py 的说明）。
                #    kb=None → 用模块级的 press_key（与引擎其他按键同一条路）。
                if press_potion(decision.key, kb=self.kb):
                    self.n_potion_drunk += 1

            # 读数日志（每 5 秒一行）：没有它，"血蓝监控到底有没有在工作"
            # 在日志里完全看不出来 —— 与 [运行状态] 同一个教训。
            if time.time() - self.t_last_bars_log >= 5:
                self.t_last_bars_log = time.time()
                hp_s = f"{self.hp_percent:.1f}%" if self.hp_percent is not None else "未知"
                mp_s = f"{self.mp_percent:.1f}%" if self.mp_percent is not None else "未知"
                logger.info(
                    f"[血蓝] 血{hp_s} 蓝{mp_s}"
                    f" 已喝{self.n_potion_drunk}次"
                    f"{'' if self.potion_enabled else '（喝药已关）'}"
                    f" | {self.potion_drinker.last_decision.reason}")
        except Exception as e:                                       # noqa: BLE001
            # 出错就把功能关掉，别每帧刷屏 —— 但要在日志里留下"为什么没了"
            self.bars_detector = None
            logger.error(
                f"[血蓝监控] 识别过程出错，已停用自动喝药（挂机不受影响）：{e}",
                exc_info=True)

    def set_potion_enabled(self, enabled):
        '''界面/热键切换喝药总开关（2026-09-27 加）。

        为什么需要：用户可能想临时手动喝（比如打 BOSS 要自己控药），
        或者发现读数不对想先关掉 —— 不必为此停掉整个挂机。
        '''
        self.potion_enabled = bool(enabled)
        if self.potion_drinker is not None:
            self.potion_drinker.enabled = self.potion_enabled
        logger.info(f"[血蓝监控] 自动喝药已{'开启' if self.potion_enabled else '关闭'}。")

    def _route_pixels(self, img):
        '''收集路线图上的色码像素坐标（validate_route_image / 闭环检查共用）。


        ⚠️ 第二版只有**两张**色码表（color_code / color_code_up_down）：
           第一版的「落点锚」第三张表已删除，这里不再需要 include_anchor 形参。
        '''
        codes = set(self.color_code) | set(self.color_code_up_down)
        hits = []
        for y in range(img.shape[0]):
            row = img[y]
            for x in range(img.shape[1]):
                if tuple(row[x][:3]) in codes:
                    hits.append((x, y))
        return hits

    def check_route_loop_closure(self):
        '''
        巡逻闭环检查（2026-09-12 加）：最后一段的终点，能不能接回第一段？

        段与段之间本来不需要容差 —— 录制器存完一段会 `_reset_route_canvas()`
        清掉 `loc_player_global_last`，下一段的第一笔就从**按 F3 那一刻站的位置**
        开始画，天然重合。

        **唯独闭环那一步没人对齐**：最后一段的终点 A′ 走完 goal 后循环回第一段，
        而第一段的起点 A 是**录制时**你站的那个位置，A′ 是你走回来的位置，
        两者不可能完全重合。上游对这一步**完全没有设计**，全靠引擎通用的像素搜索兜：

            ≤ search_range(10px)  正常接上，无感
            ≤ rescue_range(40px)  靠失联救援接上，日志打「路线失联」
            > 40px                接不上 → 指令真空 → 干站 → watchdog 抽随机动作乱走

        所以这里在**加载时**把这个距离量出来告诉用户（同样是"加载时报错"而不是
        "运行时瞎猜"的套路），省得跑起来卡在终点才发现。

        Returns:
            int or None: 最后一段终点到第一段最近像素的曼哈顿距离；
                         段数不足 / 缺 goal / 第一段无像素时返回 None（不做检查）。
        '''
        if len(self.img_routes) < 2:
            # 单段路线不构成"闭环"问题（但会在终点停住，见 check_reach_goal）
            return None
        goal_codes = {c for c, v in self.color_code.items()
                      if v.strip().split()[-1] == "goal"}
        if not goal_codes:
            return None
        # 最后一段的 goal 像素 → 取质心（stamp 是个圆点，质心足够代表"终点"）
        last = self.img_routes[-1]
        gx = gy = 0
        n_goal = 0
        for y in range(last.shape[0]):
            row = last[y]
            for x in range(last.shape[1]):
                if tuple(row[x][:3]) in goal_codes:
                    gx += x
                    gy += y
                    n_goal += 1
        if n_goal == 0:
            return None   # 没有 goal：validate_route_image 已经把它判废剔除了
        gx, gy = gx // n_goal, gy // n_goal

        first_pts = self._route_pixels(self.img_routes[0])
        if not first_pts:
            return None
        best = min(abs(x - gx) + abs(y - gy) for x, y in first_pts)   # 曼哈顿距离

        search_range = self.cfg["route"]["search_range"]
        rescue = self.cfg["route"].get("rescue_range", 40)
        if best > rescue:
            logger.error(
                f"[巡逻闭环] 这条巡逻线**闭不上环**：最后一段终点 ({gx},{gy}) 到第一段"
                f"最近像素 {best}px，超过失联救援范围 {rescue}px。\n"
                f"        后果：走完最后一段会循环回第一段但接不上 → 指令真空 → "
                f"干站不动 → 看门狗 {self.cfg['watchdog']['timeout']}s 后抽随机动作乱走。\n"
                f"        修法：重录最后一段，走回起点**10px 以内**再按 F3（最多别超 40px）。")
        elif best > search_range:
            logger.warning(
                f"[巡逻闭环] 最后一段终点 ({gx},{gy}) 离第一段 {best}px —— "
                f"超过正常搜索范围 {search_range}px，只能靠失联救援（{rescue}px）接上，"
                f"每次循环都会打一条「路线失联」警告。建议重录时走回起点 10px 内。")
        else:
            logger.info(f"[巡逻闭环] 最后一段终点离第一段 {best}px，闭环正常 ✓")
        return best

    def validate_route_image(self, img, name):
        '''
        路线有效性校验（2026-09-12 加）——**用「加载时校验一次」换掉「运行时兜底一堆」**。

        为什么要有它（用户 2026-09-12 的质疑命中了要害：「你加的条件会不会越来越多，
        导致索敌逻辑越来越复杂、牵一发而动全身？」）：会的，而且这次「又不攻击了」
        的根因**根本不在逻辑里，而在数据里** ——

            minimaps/废都南方工地/route_home.png 上只有 **17 个色码像素**，
            全挤在安全点 (98,56) 周围 6×5 的一小块里，**而且没有 goal 像素**。
            → 角色掉到 (98,122)，最近的回正线像素在 **64px** 外
              （search_range=10 / rescue_range=40 都够不着）→ 一帧都找不到
            → 指令真空，而「回正期间不打怪」又把攻击一起掐掉 → 既不走也不打 = 死局。

        这种"录废了的线"在**运行时是查不出来的**：它跟「暂时没接上」长得一模一样，
        只能靠不断堆超时去猜，也就是用户担心的"条件越来越多"。
        所以改在**加载时**一次性验完：废线直接**不启用**，引擎连回正状态都进不去。

        判据（任一不满足即判废）：
          1. 色码像素太少（< 20）—— 真实路线一条至少几十上百个像素；
          2. 没有 goal 标记 —— 录制器收尾一定会盖 goal（_stamp_goal），没有 = 没录完；
          3. 像素全挤在一小块（bbox 跨度 < 10px）—— 典型症状就是"原地起头就收尾了"。

        Args:
            img: RGB 路线图（已经过 mask_route_colors 处理）
            name: 只用于日志显示的文件名

        Returns:
            (bool, str): (是否有效, 无效原因；有效时原因为 "")
        '''
        codes = set(self.color_code) | set(self.color_code_up_down)
        goal_codes = {c for c, v in self.color_code.items()
                      if v.strip().split()[-1] == "goal"}
        if not goal_codes:
            # 配置里连 goal 色码都没有 → 没法判断，跳过校验不误伤
            return True, ""

        hits = self._route_pixels(img)
        has_goal = any(tuple(img[y, x][:3]) in goal_codes for x, y in hits)

        n = len(hits)
        if n == 0:
            return False, "上面一个路线像素都没有（等于空图）"
        if n < 20:
            return False, f"只有 {n} 个路线像素（真实路线一条至少几十个）"
        if not has_goal:
            return False, "没有 goal 标记（= 录制时没走到收尾那一步）"
        xs = [p[0] for p in hits]
        ys = [p[1] for p in hits]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span < 10:
            return False, (f"路线像素全挤在 {span}px 的小块里"
                           f"（bbox x[{min(xs)},{max(xs)}] y[{min(ys)},{max(ys)}]）"
                           f"—— 像是原地起头、原地收尾")
        logger.debug(f"[路线校验] {name}：{n} 个路线像素，跨度 {span}px，有 goal ✓")
        return True, ""

    def _clamp_route_index(self):
        '''
        把「当前巡逻段号」钳回合法范围（2026-10-12 加、2026-10-14 重新接回）。

        ## 为什么需要它（真机**两次**停摆，症状都是"角色不动 + 一屏 IndexError"）

        界面按【开始】会在**同一个进程里**重跑 `load_config(cfg)`
        （`src/ui/AutoBotController.py:109`），那次调用会重建 `img_routes`：

        * 用户把一张图**从 10 段重录成 2 段** ⇒ 列表从 10 条变 2 条，
          而 `idx_routes` 还停在上一轮的值（比如 8）⇒
          `self.img_routes[self.idx_routes]` → IndexError；
        * 崩溃点排在「路线推进」之前 ⇒ 段号永远回不来 ⇒ 主循环**每帧都炸**
          （实测日志：「累计失败 149 帧」与「累计失败 296 帧」两次）；
        * 列表非空 ⇒ `run_once` 里那道 `and self.img_routes` 守卫会放它过去 ——
          所以"列表空不空"这个守卫**挡不住**这个坑。

        所以每次重建列表之后都要钳一次。
        **合法的段号不许被动**：否则每次重载都会把巡逻打断回第一段
        （`tools/verify_route_index_bounds.py` 钉的就是这两面）。
        '''
        if not isinstance(getattr(self, "idx_routes", None), int):
            self.idx_routes = 0
            return self.idx_routes
        n = len(getattr(self, "img_routes", None) or [])
        if self.idx_routes < 0 or self.idx_routes >= n:
            _old = self.idx_routes
            self.idx_routes = 0
            # ⚠️ 只在**真的改了值**时出声：`n == 0 且 idx == 0`（这张图还没录路线）
            #    本来就没问题，报一条"段号 0 超出范围（当前 0 段）"只会制造噪音。
            if _old != 0:
                logger.warning(
                    f"[路线] 段号 {_old} 超出范围（当前 {n} 段），已回到第 1 段。\n"
                    f"        换图 / 重录成更少段 / 有段被判废时都会发生；不钳位则主循环每帧 "
                    f"IndexError（角色彻底不动）。")
        return self.idx_routes

    def load_config(self, cfg):
        '''
        load_config
        '''
        # ⚠️ 必须在所有"读 self.cfg"的方法之前赋值（2026-09-12 踩坑）：
        # 之前在末尾才设 self.cfg=cfg，但中段要调用的 check_route_loop_closure
        # 会读 self.cfg["route"]["search_range"]，那时 self.cfg 还是 None → TypeError
        # 直接抛掉整条 load_config（verify_load_config 当时挂在这）。
        # 提前到第一行后下面所有 self.cfg[...] 都安全；末尾再赋一次是为了向
        # verify_load_config 的【4】`self.cfg 就是传入的 cfg` 断言交代（虽然
        # 赋了同一个对象结果一样，但显式赋值更稳）。
        self.cfg = cfg
        # 先校验配置完整性（缺键会让主循环在运行中静默崩溃，见 _check_cfg_completeness）
        self._check_cfg_completeness(cfg)

        # ── ⚠️★ 2026-09-28 存量配置自动纠正：white_mask → grayscale ──────────
        #   v1.0.11 曾把默认（及标定工具写入的）mode 改成 white_mask，那是一次
        #   回归：绿底 (0,255,0) 灰度≈150 恰好过 inRange(150,255) 下限 ⇒ 模板
        #   二值化后全 255 常数 ⇒ 任何纯白区域都能拿 0 分 ⇒ 匹配锁死填充边、
        #   分数完美无告警（用户报"定位漂移"，issue #4）。grayscale 14/14 全对。
        #   存量配置里写着 white_mask —— 这里在加载时自动纠正，用户不用手改。
        #   ⚠️ 位置有意放在 _check_cfg_completeness 之后、地图分支之前：
        #      下面 `if mode == "normal"` 里有「地图未选择就提前 return」，
        #      放它后面会导致没选图时纠正块根本执行不到（verify_nametag_mode_fix 用例1 踩坑）。
        if str(cfg["nametag"].get("mode", "")).strip().lower() == "white_mask":
            cfg["nametag"]["mode"] = "grayscale"
            logger.warning(
                "[名字定位] 检测到 nametag.mode = white_mask（v1.0.11 期间的错误默认值，"
                "会导致定位锁死在画面边缘）—— 已自动改回 grayscale，无需手动处理。")

        # Parse color code in config
        self.color_code = {
            tuple(map(int, k.split(','))): v
            for k, v in cfg["route"]["color_code"].items()
        }
        self.color_code_up_down = {
            tuple(map(int, k.split(','))): v
            for k, v in cfg["route"]["color_code_up_down"].items()
        }
        # ⚠️ 第二版没有落点锚，也就没有第三张色码表（color_code_home_anchor 已删除）。
        # ⚠️ 引擎实例是**跨多次「开始/暂停」复用**的：换图后上一张图的回正线必须清干净，
        #    否则会用旧图的线在新图里触发回正（静默的错误行为，和 monsters_info 同批坑）。
        self.home_lines = []
        self.home_line = None
        self.home_d_home = None
        self.home_d_main = None
        self._home_line_near = None
        self._main_seg_i = 0
        self._main_pts_xy = None
        self._main_pts_seg = None
        self.home_enter_pos = None
        self.home_enter_t = 0.0
        self.home_frame = 0
        self.home_reenter_lock_t = 0.0

        # 2026-09-12：定点模式（stand）已按用户要求删除，只剩巡逻（normal）。
        # ⚠️ 旧配置里可能还残留 mode: stand —— 那样下面的地图/路线**一行都不加载**，
        #    角色站着不动又不报错，属于最难查的静默失败。这里明确喊出来。
        mode = cfg["bot"].get("mode")
        if mode != "normal":
            logger.error(
                f"[配置] bot.mode = {mode!r} 不是合法值 —— 定点模式（stand）已于 "
                f"2026-09-12 删除，现在只有巡逻（normal）。\n"
                f"        当前不会加载任何地图 / 路线 / 回正线，角色**不会动也打不到怪**。\n"
                f"        修法：主界面「挂机方式」选回「巡逻绕圈」，或直接把配置里的 "
                f"bot.mode 改成 normal。")
        if mode == "normal":
            map_name = cfg['bot']['map']
            # ⚠️ 登记表必须在**每次开跑时重读**（2026-09-12 用户实测踩坑）。
            #    self.data 是界面启动那一刻的快照（见 __init__），而录制器是**独立子进程**，
            #    录完路线会自动把新图写进 config/config_data.yaml。界面那边的地图列表每 5 秒
            #    重读一次（所以新图**立刻能在下拉里选到**），引擎这边不重读就出现诡异现象：
            #    「列表里能选、一开跑就 Invalid map name: X. Not supported in config_data.yaml」。
            #    修法：在这个唯一的消费点重读，不管从哪个入口调用都不会再拿到过期登记表。
            self.data = load_yaml("config/config_data.yaml")
            # Check if the map is supported in config_data.yaml
            if not map_name:
                logger.info("地图未选择，跳过地图加载")
                return
            # ⚠️ 2026-10-13：登记表是**裸头部**时 yaml 解析成 None（见 utils/registry.py
            #    的 _drop_line_in_block 事故），原来直接下标会抛 KeyError；
            #    `or {}` 兜底，只影响这一处的报错文案，不改变正常路径。
            if map_name not in (self.data.get("map_mobs_mapping") or {}):
                text = f"Invalid map name: {map_name}. "\
                        "Not supported in config/config_data.yaml."
                logger.error(text)
                return -1
                # raise RuntimeError(text)

            # Load map.png from minimaps/
            self.img_map = load_image(f"minimaps/{map_name}/map.png",
                                      cv2.IMREAD_COLOR)
            # Load route*.png from minimaps/
            route_files = sorted(glob.glob(resource_path(f"minimaps/{map_name}/route*.png")))
            # route_rest.png 是上游遗留的「休息路线」；route_home.png 是本分支的回正线
            # ——两者都不属于主线巡逻列表，必须排除，否则回正线会被当成普通路线来回走。
            route_files = [p for p in route_files
                           if not p.endswith("route_rest.png")
                           and not p.endswith("route_home.png")]
            self.img_routes = []
            _map_hw = self.img_map.shape[:2]
            for route_file in route_files:
                img = cv2.cvtColor(load_image(route_file), cv2.COLOR_BGR2RGB)
                # ── 尺寸一致性闸（2026-10-14 加，与废线一样跳过）────────────────
                # 路线图的坐标系 = map.png 的坐标系（录制器拿 map.png 当全景图，
                # 由 find_pattern_sqdiff 定出小地图位置后把角色点画上去的）。
                # 尺寸不一样 ⇒ **刻度不一样**：引擎按它走会走到错的地方、或者
                # 在 get_nearest_color_code 里扫不到任何色码（站着不动），两者都不报错。
                # 真机现场：minimaps/废弃地铁2 里 map.png 137x344、route3..7 也是
                # 137x344，但 route1 137x268、route2 137x319（另一次录制的遗留）——
                # 手绘回正线面板就是被这两个撞崩的（boolean index 344 vs 268）。
                if route_size_mismatch(img.shape, _map_hw):
                    logger.error(
                        f"[路线校验] minimaps/{map_name}/{os.path.basename(route_file)} "
                        f"尺寸 {img.shape[:2]} 与 map.png {_map_hw} 不一致，"
                        f"已跳过不加载。\n"
                        f"        它是**另一次录制**留下的（那时小地图窗口大小不同 ⇒ "
                        f"坐标系不一样），按它走会走到错的地方。\n"
                        f"        修法：删掉这一张，或按现在的小地图尺寸重录这一段。")
                    continue
                # Remove pixel in map that is color code
                img = mask_route_colors(self.img_map, img, cfg["route"]["color_code"])
                img = mask_route_colors(self.img_map, img, cfg["route"]["color_code_up_down"])
                # 废线不进列表（见 validate_route_image 的说明）：与其让它在运行时
                # 把角色卡死、再去猜，不如加载时就说清楚。
                ok, why = self.validate_route_image(img, os.path.basename(route_file))
                if not ok:
                    logger.error(
                        f"[路线校验] minimaps/{map_name}/{os.path.basename(route_file)} "
                        f"判为废线，已跳过不加载：{why}\n"
                        f"        请在录制器里重录这一段：走到起点按 F3 开始，"
                        f"一路走到终点再按 F3 保存（收尾会自动盖上 goal 标记）。")
                    continue
                self.img_routes.append(img)

            # ── 段号钳位（2026-10-14 重新接回）───────────────────────────────
            # 列表刚重建完，段数可能与上一轮不同（换图 / 重录成更少段 / 有段被判废）：
            # 不钳位就会在 run_once 里按下标取 route 时 IndexError，**且每帧都炸**。
            self._clamp_route_index()

            # ── 移动指令自检（2026-09-14 加）──────────────────────────────────
            # 录制器定位失败（坐标卡 0,0）时会录出「整段全是 goal、一个 left/right
            # 都没有」的废路线：2026-09-13 用户重录的 route1 就是 13 个像素全是 goal。
            # 这种线能过 validate_route_image（像素数够、跨度够），但 bot 拿到之后
            # **根本不知道该往哪走** —— 表现为站着不动或乱走，而且**不报错**，极难排查。
            for _i, _img in enumerate(self.img_routes):
                _mv = 0
                for _c, _cmd in (list(self.color_code.items())
                                 + list(self.color_code_up_down.items())):
                    _parts = str(_cmd).strip().split()
                    if _parts and _parts[0] in ("left", "right"):
                        _mv += int(np.count_nonzero(
                            np.all(_img == np.asarray(_c, dtype=np.uint8), axis=-1)))
                if _mv == 0:
                    logger.error(
                        f"[路线校验] 第 {_i + 1} 段主线上**一个移动指令（left/right）都没有**，"
                        f"全是 goal/none —— 这是**废路线**，bot 拿它不知道该往哪走。\n"
                        f"        几乎可以肯定是**录的时候定位就是坏的**（界面上坐标显示 (0,0)）。\n"
                        f"        请重录这一段，并且录之前先确认界面坐标**不是** (0,0)。")

            # 算出每一段的终点（goal 质心），供"这段走完了"判定 —— 见 _calc_seg_goals
            self._calc_seg_goals()

            # 多段巡逻线：量一下最后一段能不能接回第一段（见 check_route_loop_closure）
            self.check_route_loop_closure()
            # 单段路线：算出"另一端"，供往返走时判断该掉头了
            self._calc_pingpong_start()
            # 主线像素缓存（运行期算 d_main 用，**全图最近像素**，向量化，不是每帧全图扫）
            self._build_mainline_cache()

            # 回正线（可选资源）：有就加载，没有则 None（此时掉出去只能站住或沿主线走）
            self.img_route_home = None
            home_path = resource_path(f"minimaps/{map_name}/route_home.png")
            if os.path.exists(home_path):
                # ── v0.9 第二版：回正线不再复用主线的三条判废（20 像素 / 跨度 10px）──
                #    合法短回正线只有 8~15px，那三条判据会把它全部误杀（坑2）。
                #    改成"分连通域 → 逐条现算 → 逐条判废 → 抹掉废的那条"。
                img = cv2.cvtColor(load_image(home_path), cv2.COLOR_BGR2RGB)
                # Q6 旧图迁移：第一版画的 127,0,127 落点锚，这里直接涂黑。
                # ⚠️ 为什么必须涂黑：该色不在 color_code 里 → 角色站上去时
                #    get_nearest_color_code 取不到 → 那一帧没指令 → 站着不动（不报错）。
                legacy = wipe_legacy_anchor(img)
                if legacy:
                    logger.info(
                        f"[回正线] 发现 {len(legacy)} 个**旧版落点标记**（127,0,127），已自动忽略"
                        f"（v0.9 第二版不再需要落点锚）：{legacy[:8]}"
                        f"{' …' if len(legacy) > 8 else ''}\n"
                        f"        不用管它；如果这条线跑起来接不住人，用【🖊 手绘回正线】重画一条"
                        f"（先沿坑底横着画一段，再竖着跳回主线）。")
                route_codes = set(self.color_code) | set(self.color_code_up_down)
                # ⚠️ mask 前**先数一次**回正线像素。mask_route_colors 是**就地改**数组的
                #    （img_route[mask] = (0,0,0)），所以"mask 前那份"必须在这里抓快照，
                #    不能事后用同一个数组比 —— 那永远是 0 个被吃掉（自检形同虚设）。
                raw_home = img.copy()
                img = mask_route_colors(self.img_map, img, cfg["route"]["color_code"])
                img = mask_route_colors(self.img_map, img, cfg["route"]["color_code_up_down"])
                # 撞色自检：回正线像素被地图底色吃掉 → 静默失效（这条线再也接不住人）
                eaten = home_pixel_clash(raw_home, img, route_codes)
                if eaten:
                    logger.error(
                        f"[回正线] 有 {len(eaten)} 个回正线像素被**地图底色吃掉了**：{eaten[:8]}"
                        f"{' …' if len(eaten) > 8 else ''}\n"
                        f"        原因：mask_route_colors 会把「地图底图里出现同色」的位置涂黑，"
                        f"而这张图的 map.png 里恰好有这些颜色。\n"
                        f"        后果：这些位置**永远接不住角色**（不报错、只是回不来）。\n"
                        f"        修法：把这几段线往旁边挪 1px 重画，"
                        f"或换一个底图里没出现过的色码后重画。")
                self.home_lines = self._scan_home_lines(map_name, img, self.img_routes)
                if self.home_lines:
                    self.img_route_home = img
                    self._log_home_health()
                else:
                    # ⚠️ 废回正线**必须当成没有回正线**，不能"加载了但用不了"：
                    #    后者会让引擎进入回正状态、又一帧像素都找不到 → 指令真空，
                    #    再加上「回正期间不打怪」= 既不走也不打的死局（2026-09-12 实测）。
                    self.img_route_home = None
                    logger.error(
                        f"[回正线] minimaps/{map_name}/route_home.png 上**没有一条可用的回正线**，"
                        f"本次**不启用回正**（掉出去后只站桩打怪、不会尝试走回）。\n"
                        f"        逐条判废的原因见上方 ERROR。\n"
                        f"        补一条：主界面选中这张图 →【🖊 手绘回正线（推荐）】"
                        f"（不用开游戏、不用真的掉下去）；或【补录回正线】"
                        f"（先让角色真的掉下去一次，站落点上按 F6 起头，走回主线再按 F6 保存）。")
            else:
                # ⚠️★ 2026-09-14 加：文件不存在时原来**静默跳过**，没有任何提示。
                #    后果很严重且极难排查：2026-09-13 用户重录路线时录制器把
                #    route_home.png 删了，bot 照常启动、照常巡逻，就是**掉下去再也回不来**
                #    —— 用户连测三轮都以为是我改的逻辑没生效，其实是回正线文件根本不存在。
                #    所以这里必须**响亮地**报出来。
                self.img_route_home = None
                self.home_lines = []
                logger.error(
                    f"[回正线] ⚠️ minimaps/{map_name}/route_home.png **文件不存在** ——"
                    f"**本次不启用回正**，角色掉下平台后**不会自己走回来**。\n"
                    f"        如果你记得以前画过一条，那是被删掉了（重录主线 / 手绘回正线"
                    f"都会重建这个目录）。\n"
                    f"        补一条：主界面选中这张图 →【🖊 手绘回正线（推荐）】"
                    f"（不用开游戏）；或【补录回正线】（真掉一次，落点按 F6 起头，"
                    f"走回主线再按 F6 保存）。")

            # Load monsters images from monster/<monster_name>
            # ⚠️ 必须先清空：self.monsters_info 只在 __init__ 里初始化过一次，引擎是
            #    **跨多次「开始/暂停」复用的同一个实例**。不清空的话换图后上一张图的怪
            #    会留在表里 —— 例：先在 henesys_west 跑（绿蘑菇），再换到怪物列表为空的
            #    地图，引擎仍会拿绿蘑菇模板去匹配，属于静默的错误行为。
            #    （2026-09-12 与"登记表快照"同批发现，都是引擎状态跨次启动没重置。）
            self.monsters_info = {}
            # ── 怪物模板的加载范围（2026-09-12 改）────────────────────────────
            # 以前只加载「这张图登记的」怪（map_mobs_mapping[图]），于是同一种怪
            # （比如三眼章鱼）每换一张新图都要重新登记一次 —— 用户实测质疑得对：
            #   「怪物不是通用的吗，几张图都有三眼章鱼」。
            # 现在**默认加载 monster/ 下的全部模板**，真正做到「截一次，所有图通用」。
            # 代价：每帧要多匹配几种怪。只有当你截了很多种（>8 种）、帧率被拖下来时，
            #      才把 monster_detect.match_all_mobs 关掉，退回按图过滤。
            # ⚠️ 必须用**参数 cfg**，不能写 self.cfg（2026-09-12 血的教训）：
            #    self.cfg 是在本函数**末尾**（约 402 行）才赋值的，这里还是 None，
            #    self.cfg.get(...) 会抛 AttributeError。而 load_config 里没有
            #    try/except，异常不进日志 → 界面上就是「点开始没反应」，无从查起。
            match_all = cfg.get("monster_detect", {}).get("match_all_mobs", True)
            if match_all:
                monster_root = resource_path("monster")
                mob_names = sorted(
                    d for d in os.listdir(monster_root)
                    if os.path.isdir(os.path.join(monster_root, d)))
            else:
                # ⚠️ 2026-10-13：同上，`or {}` + `.get(...) or []` 兜底；缺登记者退化成
                #    空列表，随后会被下面「没有可用怪模板」的报错分支接住，不会静默。
                mob_names = list(
                    (self.data.get("map_mobs_mapping") or {}).get(map_name) or [])

            # 🔍 多尺度模板（2026-10-13 借参考项目 zhoufanglu/maplestory-auto-ds）：
            #    scales 默认 [1.0] ⇒ 与"没有这个键"逐像素一致（**不做任何 resize**）。
            #    官方素材/换分辨率导致模板尺寸对不上时，配 [0.7, 0.85, 1.0, 1.15] 之类。
            #    代价 ≈ 匹配次数 × 尺度数（每张模板本来就有"原图 + 左右镜像" = ×2）。
            _mob_scales_list = self._mob_scales(cfg.get("monster_detect"))
            _mob_tpl_total = 0
            for monster_name in mob_names:
                imgs = []
                _no_bg = []      # 没有绿幕也没有透明底的模板（整张矩形都会参与匹配）
                for file in glob.glob(resource_path(f"monster/{monster_name}/{monster_name}*.png")):
                    try:
                        # ⚠️ IMREAD_UNCHANGED（2026-10-13）：官方素材常是 RGBA 透明底，
                        #    用默认的 IMREAD_COLOR 读会**直接丢掉 alpha**、透明区变成黑块
                        #    —— contour_only 模式下整块黑背景会被当成轮廓（假轮廓）。
                        #    flatten_alpha_to_green 把透明区填成绿幕，与 get_mask 的口径一致。
                        raw = load_image(file, cv2.IMREAD_UNCHANGED)
                        img = flatten_alpha_to_green(raw)
                    except Exception as e:                       # noqa: BLE001
                        # 单张读不了就跳过这一张（不静默：ERROR + 文件名 + 原因）
                        logger.error(f"[怪物] 模板 {file} 读取失败，已跳过这一张：{e!r}")
                        continue
                    if bool(get_mask(img, (0, 255, 0)).all()):
                        _no_bg.append(os.path.basename(file))
                    if min(img.shape[0], img.shape[1]) < 10:
                        # 匹配阶段对 <10px 的模板是**跳过**（判据借参考项目，见
                        # get_monsters_in_range）。这里在**加载时**吼一次，
                        # 而不是每帧每张都打一条 —— 否则日志会被刷屏。
                        logger.warning(
                            f"[怪物] 模板 {os.path.basename(file)} 只有 "
                            f"{img.shape[1]}x{img.shape[0]}（边长 <10px），"
                            f"匹配时会跳过这一张。\n"
                            f"        怎么修：重截一张更大的（怪在画面里多大就截多大）；"
                            f"或用官方的透明底素材（现已支持 RGBA）。")
                    imgs.extend(self._mob_template_variants(img, _mob_scales_list))
                if _no_bg:
                    logger.warning(
                        f"[怪物] monster/{monster_name}/ 里这些模板没有绿幕、也没有透明底："
                        f"{', '.join(_no_bg)}\n"
                        f"        它们会整张矩形参与匹配 —— contour_only 模式下模板里的"
                        f"黑色背景会被当成轮廓，容易误报。\n"
                        f"        怎么修：用 RGBA 透明底 PNG（现已支持），"
                        f"或把背景涂成纯绿 RGB(0,255,0) 再截图。")
                if imgs:
                    self.monsters_info[monster_name] = imgs
                    _mob_tpl_total += len(imgs)
                elif match_all:
                    # 全量扫描会扫到空目录之类的东西 —— 跳过就行，不能因此拒绝启动
                    logger.debug(f"[怪物] monster/{monster_name}/ 下没有 "
                                 f"{monster_name}*.png，已跳过")
                else:
                    logger.error(f"No images found in monster/{monster_name}/{monster_name}*")
                    return -1
                    # raise RuntimeError(f"No images found in monster/{monster_name}/{monster_name}*")
            if not self.monsters_info:
                # ⚠️ 必须报错（2026-09-12）：一个模板都加载不到 → 画面里一只怪都
                #    认不出来，症状是「站桩/走路、一次都不打怪」，而原实现只打一行
                #    INFO 的 Loaded monsters: []，用户完全无从查起（本次卡住的元凶）。
                if match_all:
                    logger.error(
                        f"[怪物] monster/ 目录里**没有任何怪物模板** —— "
                        f"一只怪都认不出来，角色会一直站桩/走路、一次都不打怪，而且不报错。\n"
                        f"        怎么补：界面上选中地图 → 点【截取怪物模板】→ 输入怪名 → "
                        f"框住一只怪存下来。\n"
                        f"        （模板是全局通用的，截一次之后所有图都能用。）")
                else:
                    logger.error(
                        f"[怪物] 地图「{map_name}」在 config/config_data.yaml 里"
                        f"**没有登记任何怪物**（map_mobs_mapping.{map_name} 是空列表），"
                        f"而当前是按图过滤模式（monster_detect.match_all_mobs: false）。\n"
                        f"        → 引擎没有可用的怪模板：角色会一直站桩/走路、"
                        f"一次都不打怪，**而且不报错**。\n"
                        f"        怎么补：界面上选中这张图 → 点【🐾 打哪几种怪…】勾选；\n"
                        f"              或者把 monster_detect.match_all_mobs 改回 true"
                        f"（加载全部模板，不用逐图登记）。")
            else:
                logger.info(f"Loaded monsters: {list(self.monsters_info.keys())}"
                            f"（{'全部模板' if match_all else f'按图过滤：{map_name}'}；"
                            f"{_mob_tpl_total} 个模板 = 每张 PNG × 左右镜像 × "
                            f"scales{_mob_scales_list}）")
                if len(self.monsters_info) > 8:
                    logger.warning(
                        f"[怪物] 当前每帧要匹配 {len(self.monsters_info)} 种怪的模板 —— "
                        f"如果帧率被拖下来，把 monster_detect.match_all_mobs 设为 false，"
                        f"改用「按图登记」只匹配这张图会出现的怪。")

            # 没有回正线时明确提示（回正线是可选资源，但掉了没有它就回不来）。
            # ⚠️ 措辞要分开「文件不存在」和「文件在但判废」—— 后者上面已经打过
            #    ERROR 了，这里再说"还没有回正线"会让人以为文件没生成，
            #    对着一个存在的文件反复排查（2026-09-12）。
            if self.img_route_home is None:
                home_file_exists = os.path.exists(
                    resource_path(f"minimaps/{map_name}/route_home.png"))
                if home_file_exists:
                    logger.warning(
                        f"[回正线] 文件**存在但已被判为废线**（见上方 ERROR），本次不启用"
                        f" —— 掉出平台后会原地打怪、走不回主线。")
                else:
                    logger.warning(
                        f"[回正线] 这张图还没有回正线（minimaps/{map_name}/route_home.png）——"
                        f"掉出平台后可能走不回主线。请在录制器里按 F6 录一条："
                        f"先让角色真的掉下去、站在**落点**上按 F6 起头，"
                        f"再一路走回主线按 F6 保存。")

        # Load player's name tag
        # 国服没有组队血条，角色名标签是画面内唯一的角色定位依据，属于必要资源
        try:
            self.img_nametag = load_image(f"nametag/{cfg['nametag']['name']}.png")
            self.img_nametag_gray = load_image(f"nametag/{cfg['nametag']['name']}.png",
                                               cv2.IMREAD_GRAYSCALE)
        except (FileNotFoundError, ValueError):
            logger.error(
                f"[初始化失败] 找不到角色名标签模板：nametag/{cfg['nametag']['name']}.png。"
                f"请先进入游戏截取自己角色头顶的名字标签存成该文件，"
                f"或在 config 里把 nametag.name 改成实际文件名（不含 .png 后缀）。"
            )
            raise

        # 开局先用「上次保存的名字锚点」兜底（2026-10-14 加）——
        # 名字偶尔认不到是常态，而**开局那几帧**没有"上一帧"可用，
        # 位置会停在 (0,0)（攻击框跑到画面左上角，只走不打且不报错）。
        # 详见 _load_nametag_pos()；只读 json，不改识别逻辑。
        self._load_nametag_pos(w_match=self.img_nametag.shape[1])

        # Load misc image
        # 国服适配（2026-09-12）：登录按钮图只用于「掉线自动重登」兜底（小地图
        # 30 秒找不到时的自动点登录），手动登录的挂机流程完全用不到 ——
        # 缺文件时降级为警告，不再让 load_config 整个崩掉（用户实测被迫去截登录界面）。
        lang = cfg["system"]["language"]
        login_button_path = resource_path(f"misc/login_button_{lang}.png")
        if os.path.exists(login_button_path):
            self.img_login_button = load_image(f"misc/login_button_{lang}.png")
        else:
            self.img_login_button = None
            logger.warning(
                f"[load_config] misc/login_button_{lang}.png 不存在，"
                f"「掉线自动重登」功能不可用（手动登录的挂机不受影响）。"
                f"需要时用界面 F3 截取模板（选「界面按钮」），名称填 login_button_{lang}。")

        # UI 坐标换算：把「录制时尺寸」的坐标换算到「当前画面尺寸」。
        # coord_base_size 不填（None）时 = game_window.size，即恒等变换。
        # 国服这套 ui_coords 需要按真实画面尺寸重新录制（见 tools/measure_window.py）。
        base_size = cfg["game_window"].get("coord_base_size") or cfg["game_window"]["size"]
        target_size = cfg["game_window"]["size"]
        if base_size != target_size:
            logger.info(f"[load_config] ui_coords 由 {base_size} 换算到 {target_size}")
        cfg['ui_coords']['login_button_top_left'] = normalize_pixel_coordinate(
            cfg['ui_coords']['login_button_top_left'], base_size, target_size)
        cfg['ui_coords']['login_button_bottom_right'] = normalize_pixel_coordinate(
            cfg['ui_coords']['login_button_bottom_right'], base_size, target_size)

        # Print mode on log
        logger.info(f"[load_config] Config AutoBot as {cfg['bot']['mode']} mode")

        # ── 按键校验：驱动认不认这些键名（2026-09-28 加，issue #6）────────
        # ⚠️ 放在血蓝装配**之前**：它只打日志、不改任何配置，越早报越好。
        #    必须 try 包住 —— 这是"提示性"检查，绝不能因为它出错而堵住挂机。
        try:
            self._check_key_names(cfg)
        except Exception as e:                                       # noqa: BLE001
            logger.warning(f"[按键校验] 校验本身出错（不影响挂机）：{e}")

        # ── 血蓝监控 + 自动喝药：装配（2026-09-27 加）─────────────────────
        self._setup_bars_and_potions(cfg)

        # Update cfg
        self.cfg = cfg
        # 性能埋点开关只在这里读**一次**（此时 cfg 已就位）。
        # 别放到每帧去 try —— 白白烧 CPU，还容易在 cfg 半初始化时读到脏值。
        self.perf_on = perf_trace_on(cfg)

        return 0 # load successfully

    def start(self):
        '''
        Start all threads
        '''
        # Start keyboard controller thread
        self.kb = KeyBoardController(self.cfg)
        if self.is_disable_control:
            self.kb.disable() # Disable keyboard controller for debugging

        # Start game window capturing thread
        if self.args.test_image == '':
            self.capture = GameWindowCapturor(self.cfg)
        else:
            self.capture = GameWindowCapturor(self.cfg, self.args.test_image)

        # 把抓帧器认定的游戏窗口句柄交给键盘控制器。
        # ⚠️ 判「游戏是否在前台」必须按 hwnd —— 标题判据会被**本界面自己的标题**骗到
        #    （界面标题 '冒险岛怀旧服 - 自动练级' 含游戏标题 '冒险岛怀旧服' 这个子串），
        #    详见 KeyBoardController.is_game_window_active 的说明。
        self.kb.game_hwnd = getattr(self.capture, "window_hwnd", None)
        logger.info(f"[前台守卫] 游戏窗口句柄 = {self.kb.game_hwnd}")

        # ── 手动示范录制（2026-10-14 加）────────────────────────────────
        # `demo_record.enabled: true` ⇒ 逐帧落盘（感知 + 影子决策 + 真人按键）；
        # `record_only: true` ⇒ 顺便把键盘**静音**（KeyBoardController 的发送循环
        # 见 is_enable 就跳过，感知/决策照跑）⇒「你自己操作、引擎只算不按」。
        # 见 src/utils/demo_recorder.py 文件头与 tools/analyze_demo.py。
        self.demo = None
        try:
            _demo = DemoRecorder(self.cfg, logger=logger)
            if _demo.settings.get("enabled"):
                if _demo.settings.get("record_only") and not self.is_disable_control:
                    self.kb.disable()
                    logger.warning(
                        "[示范录制] record_only=true ⇒ **键盘已静音**：引擎一个键都不发，"
                        "请你自己操作（想边看边打就把 demo_record.record_only 改成 false）")
                if _demo.start():
                    self.demo = _demo
        except Exception as _e:                               # noqa: BLE001
            self.demo = None
            logger.warning(f"[示范录制] 初始化失败：{_e!r} —— 不影响挂机")

        # Reset all timers
        self.t_last_frame = time.time()
        self.t_watch_dog = time.time()
        self.t_last_attack = time.time()
        self.t_last_minimap_update = time.time()

        # Set init state
        if self.args.init_state != "":
            self.fsm.set_init_state(self.args.init_state) # For debugging
        else:
            self.fsm.set_init_state("hunting")

        # Start Auto Bot main thread
        self.thread_auto_bot = threading.Thread(target=self.loop)
        self.thread_auto_bot.start()
        self.is_first_frame = True

        logger.info("[MapleStoryAutoBot] Started")

    def pause(self):
        '''
        Terminate thread except main thread
        '''
        self.terminate_threads()

    def enable_viz(self):
        '''
        Enable AutoBot to generate debug image
        '''
        self.is_need_show_debug_window = True
        logger.debug("[enable_viz] is_show_debug_window = True")

    def disable_viz(self):
        '''
        Disable AutoBot to generate debug image
        '''
        self.is_need_show_debug_window = False
        logger.debug("[disable_viz] is_show_debug_window = False")

    def start_record(self):
        '''
        Start record
        '''
        # Prepare video writer if need to record
        if not self.is_show_debug_window:
            self.enable_viz()

        # Make sure video/ exist
        video_dir = ensure_dir("video")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(video_dir, f"{timestamp}.mp4")

        # cv2.VideoWriter 与 imread/imwrite 一样走 ANSI 路径：项目路径含中文时，
        # 它会「不报错、不生成文件」地静默失败。折中办法是先写到系统临时目录
        # （纯 ASCII），停止录制时再搬到 video/ 下。
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # mp4 codec
        self.video_final_path = path
        self.video_tmp_path = os.path.join(tempfile.gettempdir(),
                                           f"msbot_record_{timestamp}.mp4")
        # VideoWriter 的尺寸参数是 (width, height)，配置里 size 是 [height, width]
        _wh = (self.cfg["game_window"]["size"][1],
               self.cfg["game_window"]["size"][0])
        self.video_writer = cv2.VideoWriter(self.video_tmp_path, fourcc, 10, _wh)

        logger.info(f"[start_record] Record video to {path}")

    def stop_record(self):
        '''
        Stop Record
        '''
        if self.video_writer is not None:
            # 原来只把引用置 None，没 release —— 视频会缺尾部数据甚至根本没写完
            self.video_writer.release()
            self.video_writer = None
            # 把临时文件搬到项目 video/ 下（中文路径写不进去，只能这样绕）
            try:
                if self.video_tmp_path and os.path.exists(self.video_tmp_path):
                    shutil.move(self.video_tmp_path, self.video_final_path)
                    logger.info(f"[stop_record] 视频已保存: {self.video_final_path}")
            except Exception as e:
                logger.error(f"[stop_record] 搬移视频失败: {e}")
                logger.error(f"           临时文件仍在: {self.video_tmp_path}")
        logger.info("[stop_record] Stop recording")

    def _nametag_pos_path(self):
        '''名字锚点存盘路径：minimaps/<当前地图>/nametag_pos.json（拿不到地图名 → None）。

        放地图目录里（与 route*.png / route_home.png 同级）的理由：
          · 换地图天然分开，不用维护一张"图名 → 坐标"的对照表；
          · 出问题只删这一个文件就回到旧行为，回滚点明确、可指认。
        '''
        try:
            _map = self.cfg["bot"]["map"]
        except (KeyError, TypeError):
            return None
        if not _map:
            return None
        return resource_path(f"minimaps/{_map}/nametag_pos.json")

    def _load_nametag_pos(self, w_match=None):
        '''开局读回「上次名字定位命中」的锚点；用上了返回 True（2026-10-14 加）。

        为什么需要：名字认不到时引擎沿用**上一帧**的 loc_nametag（位置被冻住），
        这本来无害 —— 同一张图里角色屏幕位置几乎是死的。但**开局那几帧**没有
        "上一帧"可用 ⇒ 位置停在初始值 (0,0)，攻击框落到画面左上角，
        症状是"只走路、不打怪"而且**不报错**。人堆遮挡导致名字偶尔丢是常态，
        所以把每张图最后一次命中的锚点存下来，开局先用着。

        存的是 `loc_nametag`（匹配到的锚点原始坐标），**不是** `loc_player`：
        引擎每帧都用 `loc_nametag + 当前模板宽度/offset` 反算 loc_player，
        存原始锚点才能与当前模板/offset 自洽（换模板后不会错位一个模板宽）。

        fail-closed：文件缺失 / 读不动 / 字段不全 / 坐标不合理 ⇒ 一律当"没存过"，
        不猜、不放行；合理性判据直接复用画面外假命中护栏 `_nametag_pos_plausible`。
        '''
        if not bool(self.cfg["nametag"].get("pos_cache", True)):
            return False
        path = self._nametag_pos_path()
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            x = int(data["loc_nametag"][0])
            y = int(data["loc_nametag"][1])
        except (OSError, ValueError, TypeError, KeyError, IndexError) as e:
            logger.warning(
                f"[名字定位] {path} 读不出来（{e!r}）⇒ 按「没存过位置」处理"
                f"（删掉这个文件即可清掉这条记录）。")
            return False
        ok, cand = self._nametag_pos_plausible((x, y), w_match)
        if not ok:
            logger.warning(
                f"[名字定位] 存盘的位置 ({x}, {y}) 算出来是 {cand}（在画面外）⇒ 不采用。"
                f"删掉 {path} 可清掉这条记录，下次命中会重新写一份。")
            return False
        self.loc_nametag = (x, y)
        self.loc_nametag_from_cache = True
        # 存盘锚点是**严格命中**时写下的 ⇒ 可以直接当"抗闪"判据的锚点：这样开局
        # 头几帧的半峰候选也能采信（那几帧正好最需要，见 nametag_relax_decision）。
        self._nt_strict_pos = (int(cand[0]), int(cand[1]))
        logger.info(
            f"[名字定位] 开局先用**上次保存的位置**：锚点 ({x}, {y}) → 角色 {cand}"
            f"（来自 {path}）。\n"
            f"        名字认不到时先按这个位置出招；一旦认出真名字会立刻改用它。"
            f"换过角色 / 改过名字 / 搬过游戏窗口的话，请删掉该文件。")
        return True

    def _save_nametag_pos(self, w_match, score=None, force=False, min_interval=5.0):
        '''把这次命中的锚点按地图存盘（节流：默认 5 秒最多写一次；force 立刻写）。

        fail-open：写盘只是"下次开局更好看"的加速器，**不是功能本身** ——
        失败绝不许影响挂机，留一条 WARNING（只吼一次、不刷屏）即可。
        写盘走"临时文件 + os.replace"，避免写一半崩掉留下半截 json。
        '''
        if not bool(self.cfg["nametag"].get("pos_cache", True)):
            return False
        path = self._nametag_pos_path()
        if not path:
            return False
        now = time.time()
        if not force and (now - float(getattr(self, "t_nametag_save", 0.0) or 0.0)) < min_interval:
            return False
        try:
            _off = int(self.cfg["nametag"]["offset"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            _off = 0
        try:
            _w = int(w_match)
        except (TypeError, ValueError):
            _w = 0
        data = {
            "note": "角色名标签最后一次命中的屏幕锚点。开局若认不到名字，引擎先用它出招；"
                    "删掉本文件即可回到旧行为（位置停在初始值）。",
            "map": self.cfg["bot"].get("map"),
            "loc_nametag": [int(self.loc_nametag[0]), int(self.loc_nametag[1])],
            "loc_player": [int(self.loc_nametag[0]) + _w // 2, int(self.loc_nametag[1]) - _off],
            "score": None if score is None else round(float(score), 4),
            "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = path + ".tmp"
        try:
            ensure_dir(os.path.dirname(path))
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            if not getattr(self, "nametag_pos_save_failed", False):
                self.nametag_pos_save_failed = True
                logger.warning(
                    f"[名字定位] 位置存盘失败（{path}）：{e!r} —— 不影响挂机，"
                    f"只是下次开局不能先用上次的位置。")
            return False
        self.nametag_pos_save_failed = False
        self.t_nametag_save = now
        # 已经有真命中了 ⇒ 不再是"开局用存盘位置"状态（告警文案据此分流）
        self.loc_nametag_from_cache = False
        return True

    # ── 手动示范录制（2026-10-14 加）────────────────────────────────────
    def _demo_frame_facts(self):
        '''拼出本帧要落盘的**事实**（引擎感知 + 影子决策；按键由录制器补）。

        ⚠️ 纯观测：只读引擎状态，一个字段都不改。
        ⚠️ 怪的中心按 `(position, size)` 现有约定算，且 `size` 是 **(宽, 高)**
           （2026-10-14 修掉了构造点的 `(h, w)` 对调）。
        '''
        try:
            _eng_ids = {id(_m) for _m in self._mobs_in_attack_box()}
        except Exception:                                     # noqa: BLE001
            _eng_ids = set()
        _mobs = []
        for _m in (getattr(self, "monsters", None) or []):
            try:
                _px, _py = _m["position"]
                _mw, _mh = _m["size"]
                _mobs.append({
                    "cx": int(_px + _mw // 2), "cy": int(_py + _mh // 2),
                    "w": int(_mw), "h": int(_mh),
                    "s": float(_m.get("score") or 0.0),
                    "in": 1 if id(_m) in _eng_ids else 0,
                })
            except Exception:                                 # noqa: BLE001
                continue
        _box = _sb = None
        try:
            _box = [int(_v) for _v in self.get_attack_range()]
        except Exception:                                     # noqa: BLE001
            pass
        try:
            _sb = [int(_v) for _v in self.get_search_box()]
        except Exception:                                     # noqa: BLE001
            pass
        return {
            "t": time.time(),
            "frame": int(getattr(self, "n_frame", 0) or 0),
            "player": getattr(self, "loc_player", None),
            "nametag": getattr(self, "loc_nametag", None),
            "miss": int(getattr(self, "nametag_miss_streak", 0) or 0),
            "ladder": bool(getattr(self, "is_on_ladder", False)),
            "seg": getattr(self, "idx_routes", None),
            "minimap": getattr(self, "loc_player_minimap", None),
            "global": getattr(self, "loc_player_global", None),
            "attack_box": _box,
            "search_box": _sb,
            "mobs": _mobs,
            "bot": {
                "mx": getattr(self, "cmd_move_x", "none"),
                "my": getattr(self, "cmd_move_y", "none"),
                "act": getattr(self, "cmd_action", "none"),
                "t_atk": float(getattr(self, "t_last_attack", 0.0) or 0.0),
            },
        }

    def record_demo_frame(self):
        '''手动示范录制：本帧落一行（`hunting.on_frame` 末尾调，纯观测）。

        位置讲究：必须在 `kb.set_command(...)` **之后** —— 那时 `cmd_*` 已是
        本帧最终决策（路线 → 打怪 → 卡死处理都跑完了），录到的影子决策才是
        "引擎本来会怎么做"。没开录制（`self.demo is None`）时这里几乎零成本。
        '''
        _d = getattr(self, "demo", None)
        if _d is None:
            return
        try:
            _d.add_frame(self._demo_frame_facts())
        except Exception as _e:                               # noqa: BLE001
            logger.warning(f"[示范录制] 落帧失败：{_e!r}")

    def _nt_miss_dump_dir(self):
        '''丢名字存图的目录（默认 <应用根>/debug；自检可用 self.debug_dir 覆盖）。'''
        _d = getattr(self, "debug_dir", None)
        if _d:
            return _d
        try:
            return ensure_dir("debug")
        except OSError:
            return None

    def _dump_nametag_miss(self, streak):
        '''
        丢名字时把"当时的画面"存两张图到 debug/（**纯观测**，诊断用）。

        为什么需要（2026-10-14 真机）：12:43 那轮只跑了 4.6 分钟，就有
        `[名字定位] 已连续 N 帧` 告警 33 条（miss 最高 71），可日志里**没有画面**
        ⇒ 分不清到底是哪一种：
          · 名牌被画面边缘裁掉（角色太靠上）   · 名牌被地形/其它玩家挡住
          · 角色根本不在画面里                 · 分数判错（模板不贴合）
        存图之后，下一轮跑一段就能直接看图定性，不用再猜。

        存的东西：
          · `..._miss<N>_full.png`：整帧，绿框 = 上次命中的名牌位置、
            黄十字 = 由此推出的角色位置、红框 = 本帧索敌框；
          · `..._miss<N>_crop.png`：名牌本该在的位置 ±(模板 + 60px)，最近邻放大 3 倍。

        ⚠️ 与判定完全无关：**不改** loc_*、不写存盘、不碰指令；任何异常都只吼一次
           （`nt_miss_dump_failed`），绝不让主循环挂掉。节流 5 秒；调用点本身只在
           miss 跨过 30 的整倍数那一帧（见 update_info_on_img_frame 的 `_streak`）。
        ⚠️ 开关是 `nametag.miss_dump`（默认 true，见 config_default.yaml）。
        '''
        if not bool((self.cfg.get("nametag") or {}).get("miss_dump", True)):
            return
        now = time.time()
        if now - float(getattr(self, "t_nt_miss_dump", 0.0) or 0.0) < 5.0:
            return
        try:
            frame = getattr(self, "img_frame", None)
            if frame is None or int(getattr(frame, "size", 0) or 0) == 0:
                return
            self.t_nt_miss_dump = now   # 先记账：下面失败也不要每帧重试刷屏
            _tpl = getattr(self, "img_nametag", None)
            if _tpl is None:
                _th, _tw = 20, 60
            else:
                _th, _tw = int(_tpl.shape[0]), int(_tpl.shape[1])
            if _tw <= 0 or _th <= 0:
                _tw, _th = 60, 20
            x, y = int(self.loc_nametag[0]), int(self.loc_nametag[1])
            _off = int((self.cfg.get("nametag") or {}).get("offset", [0, 30])[1])
            _h_f, _w_f = frame.shape[:2]
            _score = float(getattr(self, "nametag_score", -1.0))
            _dir = self._nt_miss_dump_dir()
            if not _dir:
                return
            ensure_dir(_dir)
            _base = f"nt_miss_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_miss{int(streak)}"
            _full_p = os.path.join(_dir, f"{_base}_full.png")
            _crop_p = os.path.join(_dir, f"{_base}_crop.png")
            # ── 整帧：三个标记（绿=上次命中名牌、黄十字=角色、红=本帧索敌框）──
            _vis = frame.copy()
            cv2.rectangle(_vis, (x, y), (x + _tw, y + _th), (0, 255, 0), 2)
            _px, _py = x + _tw // 2, y - _off
            cv2.line(_vis, (_px - 12, _py), (_px + 12, _py), (0, 255, 255), 2)
            cv2.line(_vis, (_px, _py - 12), (_px, _py + 12), (0, 255, 255), 2)
            try:
                _bx0, _by0, _bx1, _by1 = self.get_search_box()
                cv2.rectangle(_vis, (int(_bx0), int(_by0)),
                              (int(_bx1), int(_by1)), (0, 0, 255), 2)
            except Exception:
                pass
            # ── 裁图：名牌本该在的位置 ±(模板 + 60px)，放大 3 倍 ────────────
            _m = max(int(_tw), int(_th)) + 60
            _cx0, _cy0 = max(0, x - _m), max(0, y - _m)
            _cx1 = min(int(_w_f), x + _tw + _m)
            _cy1 = min(int(_h_f), y + _th + _m)
            _crop = frame[_cy0:_cy1, _cx0:_cx1]
            if int(getattr(_crop, "size", 0) or 0) == 0:
                _crop, _cx0, _cy0 = frame, 0, 0
            _crop = cv2.resize(_crop, None, fx=3.0, fy=3.0,
                               interpolation=cv2.INTER_NEAREST)
            cv2.rectangle(_crop,
                          (int((x - _cx0) * 3), int((y - _cy0) * 3)),
                          (int((x - _cx0 + _tw) * 3), int((y - _cy0 + _th) * 3)),
                          (0, 255, 0), 2)
            _ok1 = imwrite_unicode(_full_p, _vis)
            _ok2 = imwrite_unicode(_crop_p, _crop)
            if not (_ok1 and _ok2):
                raise OSError(f"imwrite 失败（full={_ok1} crop={_ok2}）")
            self.nt_miss_dump_failed = False
            logger.info(
                f"[名字定位·丢画面] miss={int(streak)} 分数={_score:.4f} ⇒ 已存 "
                f"{os.path.join('debug', os.path.basename(_full_p))} 与 "
                f"{os.path.join('debug', os.path.basename(_crop_p))}\n"
                f"        绿框 = 上次命中的名牌位置 / 黄十字 = 由此推的角色位置 / "
                f"红框 = 本帧索敌框；裁图 = 名牌本该在的位置 ±{_m}px 放大 3 倍。")
        except Exception as e:
            if not getattr(self, "nt_miss_dump_failed", False):
                self.nt_miss_dump_failed = True
                logger.warning(
                    f"[名字定位·丢画面] 存图失败：{e!r} —— 不影响挂机（只吼这一次）。")

    def _nametag_pos_plausible(self, loc_nametag, w_match):
        '''
        名字匹配算出来的**角色位置**是否可能（画面外假命中护栏，2026-10-13 加）。

        返回 `(ok, cand)`：
          · `cand` = 由这次匹配算出的角色位置（客户区坐标）；
          · `ok=False` ⇒ 这个匹配是假的，调用方要按"未命中"处理
            （loc_nametag 保持上一帧的值、nametag_miss_streak += 1）。
        为什么需要（真机实录）：用户 12 帧里 **4 帧**算出 loc=(21,-30)（负 y、贴左上角）
        —— 名字模板匹配到了左上角小地图面板的文字。位置**错了**比位置偏了更糟：
        索敌框、攻击框、回正判据全以它为中心 ⇒ 会"框在错误的地方"。

        判据（都不依赖跨帧状态）：
          ① 角色位置不可能是负数（客户区坐标；人在画面里才会有名牌）；
          ② 拿得到 `img_frame.shape` 时，位置必须落在客户区内（留 4px 贴边余量）。
        ⚠️ 取不到帧尺寸 ⇒ **只做①**：那是"无法判定"，不是"放行"——
           新自检 tools/verify_mob_sight.py 的 [9] 把这条口径钉住。
        '''
        try:
            _off = int(self.cfg["nametag"]["offset"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            _off = 0
        try:
            cand = (int(loc_nametag[0]) + int(w_match) // 2,
                    int(loc_nametag[1]) - _off)
        except (TypeError, ValueError, IndexError):
            return True, (0, 0)
        if cand[0] < 0 or cand[1] < 0:
            return False, cand
        _f = getattr(self, "img_frame", None)
        if _f is None:
            return True, cand
        try:
            _fh, _fw = _f.shape[:2]
        except Exception:                                    # noqa: BLE001
            return True, cand
        _mg = 4
        if -_mg <= cand[0] <= _fw + _mg and -_mg <= cand[1] <= _fh + _mg:
            return True, cand
        return False, cand

    def get_player_location_by_nametag(self):
        '''
        Detects the player's location based on the nametag position in the game window.

        This function works by:
        - Extracting a vertical region of interest (ROI) where the nametag is expected.
        - Padding the ROI to avoid template matching edge issues.
        - Using template matching to locate the nametag, split into left and right halves
        to improve robustness against partial occlusion.
        - Selecting the best match (left or right) based on score and cache status.
        - Computing the player's center position by applying a fixed offset to the nametag.

        Returns:
            loc_player (tuple): The (x, y) coordinates of the player's estimated location.
        '''
        # Get camera region in the game window
        img_camera = self.img_frame_gray[
            :self.cfg["ui_coords"]["ui_y_start"], :]

        # Get nametag image and search image
        if self.cfg["nametag"]["mode"] == "white_mask":
            # Apply Gaussian blur for smoother white detection
            img_camera = cv2.GaussianBlur(img_camera, (3, 3), 0)
            img_nametag = cv2.GaussianBlur(self.img_nametag_gray, (3, 3), 0)
            lower_white, upper_white = (150, 255)
            img_roi = cv2.inRange(img_camera, lower_white, upper_white)
            img_nametag  = cv2.inRange(img_nametag, lower_white, upper_white)
        elif self.cfg["nametag"]["mode"] == "grayscale":
            img_roi = img_camera
            img_nametag = self.img_nametag_gray
        elif self.cfg["nametag"]["mode"] == "histogram_eq":
            # Apply histogram equalization
            img_nametag_eq = cv2.equalizeHist(self.img_nametag_gray)
            img_camera_eq = cv2.equalizeHist(img_camera)

            # Apply global (fixed) threshold
            _, img_nametag = cv2.threshold(img_nametag_eq, 150, 255, cv2.THRESH_BINARY)
            _, img_roi = cv2.threshold(img_camera_eq, 150, 255, cv2.THRESH_BINARY)
        else:
            logger.error(f"Unsupported nametag detection mode: {self.cfg['nametag']['mode']}")
            return
        # cv2.imshow("img_roi", img_roi)
        # cv2.imshow("img_nametag", img_nametag)

        # Pad search region to deal with fail detection when player is at map edge
        (pad_y, pad_x) = self.img_nametag.shape[:2]
        img_roi = cv2.copyMakeBorder(
            img_roi,
            pad_y, pad_y, pad_x, pad_x,
            borderType=cv2.BORDER_REPLICATE  # replicate border for safe matching
        )

        # Get last frame name tag location
        if self.is_first_frame:
            last_result = None
        else:
            last_result = (
                self.loc_nametag[0] + pad_x,
                self.loc_nametag[1] + pad_y
            )

        # Get number of splits
        h, w = img_nametag.shape

        # Get nametag's background mask
        mask = get_mask(self.img_nametag, (0, 255, 0))

        # ---- 两遍匹配（2026-09-10 加）：先用**整块**，整块认不出再退到**切块**
        #
        # 为什么要这样：名字底板会被**别的玩家的名牌压住**。实测（20 帧人堆走位帧）：
        #   整块匹配只有 7/20 帧认得出；切成半块后变成 11/20 帧，而且救回来的 4 帧
        #   落点全在合理轨迹上（整块 0.0174 刚过阈值被拒的那帧，半块拿到 0.0059）。
        #
        # 为什么不干脆把 split_width 调小（等于永远用切块）：那样判别力反而变薄 ——
        #   实测半块左半的真位置分 0.0092，离阈值 0.01 只剩 1.09 倍。
        #   而上游原本切块就是为了抗遮挡（其注释原话 "To avoid occlusion from ladders
        #   or background objects"），只是一直被当成默认路径在用。
        #   所以正确形态是：**整块优先（判别力最强）+ 失败才退切块（抗遮挡）**。
        #
        # 两遍用**同一个阈值**：实测救回来的那 4 帧分数是 0.0002~0.0059，都在 0.01 以内；
        #   而认不出的帧最低也有 0.0174 —— 一个阈值就能切开，不需要给兜底单独放宽。
        passes = [(1, False)]
        _fb_split = int(self.cfg["nametag"].get("split_width_fallback", 0) or 0)
        if _fb_split > 0:
            _fb_n = max(1, w // max(1, _fb_split))
            if _fb_n > 1:
                passes.append((_fb_n, True))

        _thres = self.cfg["nametag"]["diff_thres"]
        _accepted = None      # 这一遍就达标了
        _best_try = None      # 都没达标时留一个最好的，供调试图显示

        # ── ★ 2026-09-27：全图回退**限流**（性能修复，见 docs/待优化-名字定位耗时.md）──
        # 为什么（真机实测数据）：
        #   · 全图搜索 ≈ 20ms，局部搜索 ≈ 0.4ms（**差 50 倍**）
        #   · 但一次名字定位会跑 2~4 次匹配（主匹配整块 → 兜底切块各一块），
        #     而**每一次失败都各自回退全图** ⇒ 失败时单帧 ≈ 82ms
        #     实测复现：用户报「名字定位 80ms、整帧 95ms ≈ 10fps」→ 本机 82.3ms ✅
        #   · 而"失败"通常只是**那一小块名牌被别的玩家盖住**，并不代表位置变了 ——
        #     让同一次定位里**只在第一次允许回退全图**，其余走局部就够。
        # 预期收益（实测）：82ms → 22ms，整帧 97ms → 37ms，约 10fps → 27fps。
        # ⚠️ 正确性保障：**第一次匹配仍允许全图**（那是唯一可能"位置真的大幅变化"
        #    的时刻，例如刚切地图、刚复活）。只有后续切块才禁用 —— 它们搜的是
        #    同一个名字的不同片段，位置必然一致，没必要各自再做一次全图。
        _global_used = False

        for num_splits, is_fallback in passes:
            w_split = w // num_splits

            # Vertically split the nametag image
            nametag_splits = {}
            for i in range(num_splits):
                x_s = i * w_split
                x_e = (i + 1) * w_split if i < num_splits - 1 else w
                nametag_splits[f"{i+1}/{num_splits}"] = {
                    "img": img_nametag[:, x_s:x_e],
                    "mask": mask[:, x_s:x_e],
                    "last_result": (
                        (last_result[0] + x_s, last_result[1]) if last_result else None
                    ),
                    "score_penalty": 0.0,
                    "offset_x": x_s
                }

            # Match tempalte
            matches = []
            for tag_type, split in nametag_splits.items():
                # ★ 限流：只在**还没用过全图**时允许回退（见上方 _global_used 的说明）。
                #   第一次之后一律禁用 —— 局部搜不到就返回局部结果，
                #   不再为同一帧的其它切块付 20ms 的全图代价。
                loc, score, is_cached = find_pattern_sqdiff(
                    img_roi,
                    split["img"],
                    last_result=split["last_result"],
                    mask=split["mask"],
                    global_threshold=self.cfg["nametag"]["global_diff_thres"],
                    allow_global_fallback=(not _global_used)
                )
                # 这次实际走了全图 → 后续切块不再允许（is_cached=False 表示未命中局部缓存，
                # 即走了全图那条路）
                if not is_cached:
                    _global_used = True
                w_match = split["img"].shape[1]
                h_match = split["img"].shape[0]
                score += split["score_penalty"]
                matches.append((tag_type, loc, score, w_match, h_match,
                                is_cached, split["offset_x"]))

            # Select best match and fix offset:
            matches.sort(key=lambda x: (not x[5], x[2]))  # prefer cached, then low score
            tag_type, loc_nt, score, w_match, h_match, is_cached, offset_x = matches[0]

            # Adjust match location back to full nametag coordinates
            loc_nt = (loc_nt[0] - offset_x, loc_nt[1])
            loc_nt = (loc_nt[0] - pad_x, loc_nt[1] - pad_y)

            cand = (tag_type, loc_nt, score, w_match, h_match, is_cached, is_fallback)
            if _best_try is None or score < _best_try[2]:
                _best_try = cand
            if score < _thres:
                _accepted = cand
                break                        # 整块够了就别用更弱的切块

        tag_type, loc_nametag, score, w_match, h_match, is_cached, used_fallback = \
            _accepted or _best_try

        # ── 画面外假命中护栏（2026-10-13 加）────────────────────────────────
        # 用户真机 12 帧里 **4 帧**算出 loc=(21,-30)（负 y、贴左上角）——
        # 名字模板匹配到了小地图面板/其它 UI 文字。位置**错了**比位置偏了更糟：
        # 索敌框、攻击框、回正判据全以它为中心 ⇒ 框在错误的地方（正是本轮要治的病）。
        # 判据与"无法判定"的处置都写在 _nametag_pos_plausible 里（自检直接测它）。
        _plausible, _cand = self._nametag_pos_plausible(loc_nametag, w)
        _thres = float(self.cfg["nametag"]["diff_thres"])
        _score_ok = bool(score < _thres)
        _accept = bool(_score_ok and _plausible)
        # ── 抗闪（2026-10-14 加）────────────────────────────────────────────
        # 分数卡在"半峰"（认不出、又远没到完全失配）时也采信这一帧，只要位置不跳。
        # 目的不是"多认出来几次"，而是**别让 miss 计数涨过 stale_pos_full_frame_after**
        # （涨过之后索敌框放大到整帧 ⇒ 怪数虚高 + 去追画面外的目标，
        #  见 log/MSBot_2026-10-14_11-42-16.log）。
        # ⚠️ 判据只有 nametag_relax_decision 一处；这里只负责"用它 + 打日志"。
        # ⚠️ 放宽采信**不进锚点、不写存盘**（见 accept 分支）。
        _relax = False
        _why = ""
        self.nametag_relaxed = False
        if not _accept:
            _relax, _why, _relax_d = nametag_relax_decision(
                score, _thres, self.cfg["nametag"].get("relax_ratio", 1.0),
                self.cfg["nametag"].get("relax_max_jump", 12), _cand, self._nt_strict_pos)
            if _relax and not _plausible:
                _relax = False
                _why = "位置落在画面外（假命中护栏先把它拦下了）"
            _nrel = time.time()
            if _relax:
                _accept = True
                self.nametag_relaxed = True
                self.nametag_relax_hits += 1
                if _nrel - float(getattr(self, "t_nametag_relax_log", 0.0)) > 5.0:
                    self.t_nametag_relax_log = _nrel
                    logger.info(
                        f"[名字定位·抗闪] 第 {self.nametag_relax_hits} 次放宽采信：{_why}"
                        f"（位置先按这一帧的候选，免得 miss 计数涨到触发"
                        f"「索敌框放大到整帧」）")
            elif _why and _nrel - float(getattr(self, "t_nametag_relax_log", 0.0)) > 30.0:
                self.t_nametag_relax_log = _nrel
                logger.warning(f"[名字定位·抗闪] 没敢采信这一帧：{_why}")
        if _score_ok and not _plausible:
            _now_g = time.time()
            if _now_g - float(getattr(self, "t_nametag_guard_log", 0.0)) > 5:
                self.t_nametag_guard_log = _now_g
                logger.warning(
                    f"[名字定位] 分数 {score:.4f} 过了阈值"
                    f"（{self.cfg['nametag']['diff_thres']}），但算出来的角色位置 "
                    f"{_cand} 在画面外 → 判为**假命中**，本帧按未命中处理"
                    f"（位置沿用上一帧）。多半是匹配到了小地图/UI 文字："
                    f"换一张更有特征的 nametag 模板，或把 nametag.diff_thres 收严")
        # Only update nametag location when score is good enough
        if _accept:
            self.loc_nametag = loc_nametag
            self.nametag_miss_streak = 0
            self.nametag_hit = True
            # ★ 只有**严格命中**才算"可信锚点"：更新抗闪锚点 + 把位置存盘
            #   （节流 5 秒一次，见 _save_nametag_pos）—— 下次开局若名字认不到，
            #   就能先按这个位置出招，而不是停在画面左上角。
            #   ⚠️ 放宽采信的帧只用来"保住位置、避免整帧索敌"：不进锚点、不落盘，
            #      否则位置会被一串放宽帧链式带跑（理由见 common.nametag_relax_decision）。
            #   ⚠️ 识别逻辑本身没有被本改动动过：存盘只是"顺带记一笔"。
            if not self.nametag_relaxed:
                if _cand is not None:
                    self._nt_strict_pos = (int(_cand[0]), int(_cand[1]))
                self._save_nametag_pos(w_match=w, score=score)
            if not self.nametag_ever_hit:
                self.nametag_ever_hit = True
                logger.info(
                    f"[名字定位] 首次命中，位置锚点 = "
                    f"({self.loc_nametag[0] + w // 2}, "
                    f"{self.loc_nametag[1] - self.cfg['nametag']['offset'][1]})"
                    f"，score={score:.4f}")
        else:
            # 匹配失败：loc_nametag 保持上一帧的值 —— 位置会被「冻住」。
            # 这里只累加计数，告警交给主循环（那边才知道要不要按帧刷屏）。
            self.nametag_miss_streak += 1
            self.nametag_hit = False

        loc_player = (
            self.loc_nametag[0] + w // 2,
            self.loc_nametag[1] - self.cfg["nametag"]["offset"][1]
        )

        # 2026-09-12：原来这里会画一个绿色名牌框，并在下面写一行
        #   "NameTag,<score>,cached/missed,<type>[,fb]"
        # 按用户要求去掉（画面上只留 Attack Range + Mob Detection Box 两个框）。
        # 名字有没有匹配上、score 多少，看日志的 [运行状态]（里面带"名字命中/miss"计数）。
        #
        # ⚠️★ 2026-09-28 订正上面那句话：**[运行状态] 里其实没有 score**。
        #    它只有二值的"名字命中True/False"和 miss 计数 —— 于是排查 issue #6 时
        #    只能看出"没命中"，**看不出"离阈值差多远"**，而这两者的病因完全不同：
        #       steadily 0.011（阈值 0.01）→ 模板/阈值问题，微调就好
        #       0.3~0.8                      → 名牌被挡 / 位置根本不对
        #    对比小地图定位：它的 score 是有存下来并显示的（self.minimap_score）。
        #    ⇒ 现在把名字匹配的 score 也记下来（**纯观测，不参与任何判定**）。
        self.nametag_score = score
        self.nametag_used_fallback = bool(used_fallback)
        # 记最近一段窗口的"最低分"：命中不了时，最低分最能说明"差多少"。
        # （阈值比较用的是**这一次**的 score，窗口最低分只用于日志。）
        _hist = getattr(self, "_nt_score_hist", None)
        if _hist is None:
            _hist = []
            self._nt_score_hist = _hist
        _hist.append(float(score))
        if len(_hist) > 150:
            del _hist[:-150]
        _ = (score, is_cached, tag_type, used_fallback)

        return loc_player

    def get_player_location_on_global_map(self):
        '''
        get_player_location_on_global_map
        '''
        self.loc_minimap_global, score, _ = find_pattern_sqdiff(
                                        self.img_map,
                                        self.img_minimap)
        # ── 埋点：记下小地图模板匹配的置信度 ────────────────────────────
        # 原来这个 score 被直接扔掉。留着它的用处：怀疑「坐标是不是定位错了」
        # 时能有一句客观依据 —— 回正小结那行会带上「小地图匹配度」。
        # ⚠️ 纯赋值，不参与任何判定，不影响行为
        #   （find_pattern_sqdiff 永远返回 min_loc，哪怕错配 —— 所以没有 score
        #    就完全看不出"这次定位到底准不准"）。
        self.minimap_score = score

        x_offset, y_offset = self.cfg["minimap"]["offset"]
        loc_player_global = (
            self.loc_minimap_global[0] + self.loc_player_minimap[0] + x_offset,
            self.loc_minimap_global[1] + self.loc_player_minimap[1] + y_offset
        )

        # Draw local minimap rectangle + player dot —— 只在调试可视化打开时画
        # ⚠️ 2026-09-12 崩过：原来**没判** is_show_debug_window，viz 关掉且
        # img_route_debug 没就绪（load_config 里置过 None）→ cv2.rectangle(None,...)
        # 抛 AttributeError，把主循环带崩。draw_rectangle / put_text_cn 现在也加了
        # None 兜底（防御性），但这里直接判 viz 关掉就不画，省 CPU 也避免出现「
        # 画了但没人看」的浪费。
        if self.is_show_debug_window and self.img_route_debug is not None:
            camera_bottom_right = (
                self.loc_minimap_global[0] + self.img_minimap.shape[1],
                self.loc_minimap_global[1] + self.img_minimap.shape[0]
            )
            cv2.rectangle(self.img_route_debug, self.loc_minimap_global,
                          camera_bottom_right, (0, 255, 255), 1)
            put_text_cn(
                self.img_route_debug,
                f"小地图 匹配度({round(score, 2)})",
                (self.loc_minimap_global[0], self.loc_minimap_global[1] + 15),
                (0, 255, 255), font_size=14, thickness=1
            )
            cv2.circle(self.img_route_debug,
                       loc_player_global, radius=2,
                       color=(0, 255, 255), thickness=-1)

        return loc_player_global

    def _goal_pixel_skip_params(self):
        '''「goal 像素不再吃掉整帧」的参数：返回 (enabled, y_tol, goal_y)。

        ★ 2026-10-14 用户 m08867 拍板（回应 m08737「读取日志，查看导航问题」）。

        真机现场（`log/MSBot_2026-10-14_15-47-36.log`）：
          15:59:34–16:05:07（**5.5 分钟**）角色站在本段终点**正下方 8px**
          （全局 (48,86)、段3/4），每帧指令都是 `none none goal` —— 因为它离
          本段的 goal 圆点最近，而 goal 的命令是"什么都不做"；同时
          `check_reach_goal()` 又因为 `|86-78| = 8 > route.goal_reach_y_tol(3)`
          拒绝切段 ⇒ **「命令不让动 + 段不让切」= 原地冻死**。
          更要命的是**回正线进不来**：回正的门槛是"方圆内一个路线像素都找不到"，
          而这里"找到了"（goal 像素）⇒ 用户 16:05 手绘的那条回正线在最该用它的时候
          用不上。最后是用户手动停机才结束的。
          （对照：16:21:06 `[回正·小结] 成功回主线 | 1.9s/56帧 | (76,86)→(68,78)`
           —— 只要回正进得去，1.9 秒就能从 y=86 回到 y=78 的主线。）

        返回：
            enabled：本层是否生效。任何一种"判不了"的情况（关掉 / 配置畸形 /
                     `goal_reach_y_tol <= 0`（用户把高度闸关了，此时 goal 本来就
                     该切段）/ 拿不到本段终点的 y）都 ⇒ **False**，即完全退回老行为。
            y_tol：与 `check_reach_goal()` **共用同一个** `route.goal_reach_y_tol`
                   （默认 3）—— 两条路径必须用同一个高度判据，否则又是一次
                   "改一处漏一处"（见 check_reach_goal 里 2026-09-27 那段注释）。
            goal_y：本段终点的 y（够不到就是 None）。
        '''
        _raw_on = self.cfg.get("route", {}).get("goal_pixel_skip", 1)
        if _raw_on is None:      # yaml 里写成空的 `goal_pixel_skip:` ⇒ 按出厂值
            _raw_on = 1          # （只有显式写 0 才是"关掉本层"）
        try:
            _on = int(_raw_on)
        except (TypeError, ValueError):
            _on = 1
        try:
            _tol = int(self.cfg.get("route", {}).get("goal_reach_y_tol", 3) or 0)
        except (TypeError, ValueError):
            _tol = 3
        _goal_y = None
        if _on > 0 and _tol > 0 and getattr(self, "_seg_goals", None):
            _i = int(getattr(self, "idx_routes", 0) or 0)
            if 0 <= _i < len(self._seg_goals):
                try:
                    _goal_y = int(self._seg_goals[_i][1])
                except (TypeError, ValueError, IndexError):
                    _goal_y = None
        return (_on > 0 and _tol > 0 and _goal_y is not None), _tol, _goal_y

    def _route_hit_unusable(self, hit, goal_y, y_tol):
        '''这个路线像素**本帧**能不能用来驱动移动？不能就返回 (True, 为什么)。

        ⚠️ 刻意只判两类（收窄改动面，"宁可少管，也不要动到正常跟随"）：
          ① **goal 像素，但高度差 > 容差** —— 它的命令是 `none none goal`（不动），
             而 `check_reach_goal()` 正因同一个高度判据拒绝切段 ⇒ 用它就是干站。
          ② **纯横向像素**（`left/right none none`）却在**另一层**（|Δy| > 容差）——
             朝它走也走不过去；真机表现是"在另一层左右来回蹭"
             （`log/MSBot_2026-10-14_15-47-36.log` 16:18:43–16:19:19 的 40 秒：
              指令在 `right none none` / `left none none` 之间翻，角色不挪窝）。
        其余一律放行（含 `up/down`、`jump`、`teleport` 任何像素）。
        '''
        if hit is None:
            return False, ""
        if not self.loc_player_global:
            return False, ""
        _cmd = str(hit.get("command", "")).split()
        if len(_cmd) != 3:
            return False, ""
        _py = int(hit["pixel"][1])
        _y = int(self.loc_player_global[1])
        if "goal" in _cmd:
            if goal_y is None:
                return False, ""
            _dg = abs(_y - int(goal_y))
            if _dg > y_tol:
                return True, (f"它是本段 goal 像素、而高度差 {_dg}px > 容差 {y_tol}px"
                              f"（人 y={_y} / 终点 y={int(goal_y)}）—— 还在半路，"
                              f"命令 `none none goal` 不会让角色动")
            return False, ""
        if _cmd[0] in ("left", "right") and _cmd[1] == "none" and _cmd[2] == "none" \
                and abs(_y - _py) > y_tol:
            return True, (f"它在另一层（像素 y={_py} / 人 y={_y}，差 {abs(_y - _py)}px "
                          f"> 容差 {y_tol}px）—— 朝它走也走不过去")
        return False, ""

    def get_nearest_color_code(self):
        '''
        Searches for the nearest color-coded action marker
        around the player on the route map.

        This function:
        - Scans each pixel in the search box to find nearest color code
        - Tracks the closest matching pixel using Manhattan distance (|dx| + |dy|).
        - Returns a dictionary containing the nearest matching
          pixel's position, color, action label, and distance.

        Returns:
            dict or None: Dictionary containing:
                - "pixel": (x, y) coordinate of the matched pixel
                - "color": matched RGB color tuple
                - "action": corresponding action string from config
                - "distance": Manhattan distance from player
            Returns None if no matching color is found within the region.
        '''
        x0, y0 = self.loc_player_global
        h, w = self.img_route.shape[:2]

        # ── E1：goal 像素不再吃掉整帧（2026-10-14，用户 m08867 拍板）────────────
        # 详见 `_goal_pixel_skip_params()` / `_route_hit_unusable()` 的注释。
        # 这里做三件事：
        #   ① 取参数（判据不可用 ⇒ 整层不启用，退回老行为）；
        #   ② 每次扫描之后过滤掉"本帧用不了"的像素，并在**同一个搜索框**里换一个
        #      真能用的（两类都换，见 `_filter_hit`）：
        #        · goal 像素 ⇒ 排除 goal 后重扫；换不到 ⇒ 本帧确实没有可用像素；
        #        · 侧向"另一层"像素 ⇒ 换一个可用像素，换不到就**退回用它**
        #          （2026-10-14 加，用户 m09666 拍板；改前这里是"直接放弃"）。
        #   ③ 过滤发生时（5s 节流）打一行 `[路线·goal过滤]` 说明"为什么没用它"。
        # ⚠️ 只有 goal 分支会出现"过滤后一个像素都不剩" ⇒ 本函数返回 (None, None)
        #    ⇒ `update_cmd_by_route` 走"找不到路线像素"那条路
        #    ⇒ **回正线 / 救援**接管（这正是本层的目的）。
        _skip_on, _y_tol, _goal_y = self._goal_pixel_skip_params()

        def _scan(radius, center, skip_goal=False, usable_only=False):
            """在 center 周围给定半径内找最近的路线色码，返回 (nearest, nearest_up_down, 扫描框)。

            skip_goal=True：重扫时**排除 goal 像素**（E1 换像素用）。
            usable_only=True：只接受"本帧真能用"的像素（判据 = `_route_hit_unusable`，
                侧向换像素用，见 `_filter_hit` 里 2026-10-14 的那段注释）。
                ⚠️ 上下码（`color_code_up_down`）**不受两个开关影响** —— E1 只管
                   `color_code`（爬梯指令本来就能动，没有"用它=干站"的问题）。
            """
            cx, cy = center
            bx_min = max(0, cx - radius)
            bx_max = min(w, cx + radius)
            by_min = max(0, cy - radius)
            by_max = min(h, cy + radius)
            n, n_ud, md, md_ud = None, None, float('inf'), float('inf')
            # ⚠️★ 2026-09-27 **撤回一次错误改动，别再加回来**：
            #   曾在此处加过「上下码里 up 优先于 down」（理由是那两段梯子像素
            #   x=178(up) / x=180(down) 差 2px、y 重叠）。**那个理由是错的** ——
            #   引擎每帧只加载**当前那一段**（`self.img_route = self.img_routes[idx]`，
            #   见 run_once），两段的像素**永远不会同时进入本扫描**，
            #   所以"两个上下码打架"这个前提根本不存在。
            #   真机症状「爬到一半开始往下爬」的真因是：**爬到一半就切段了**，
            #   切到回程段(route2) 后那段在梯子上写的是 down ⇒ 于是往下爬。
            #   ⇒ 要治的是**切段时机**（见 _near_seg_goal 的 goal_reach_y_tol），
            #     不是这里的取舍。别再往这里加优先规则。
            for y in range(by_min, by_max):
                for x in range(bx_min, bx_max):
                    pixel = tuple(self.img_route[y, x])  # (R, G, B)
                    dist = abs(x - cx) + abs(y - cy)
                    # usable_only（2026-10-14）：这个像素本帧能不能用？
                    # ⚠️ 只在真的命中色码时才问一次判据（否则每像素都建 dict，白花时间）。
                    _ok_cc = True
                    if usable_only and _skip_on and pixel in self.color_code:
                        _ok_cc = not self._route_hit_unusable(
                            {"pixel": (x, y), "color": pixel,
                             "command": self.color_code[pixel],
                             "distance": dist},
                            _goal_y, _y_tol)[0]
                    # Get nearest color
                    if _ok_cc and pixel in self.color_code and dist < md \
                            and not (skip_goal
                                     and "goal" in str(self.color_code[pixel])):
                        n = {
                            "pixel": (x, y),
                            "color": pixel,
                            "command": self.color_code[pixel],
                            "distance": dist
                        }
                        md = dist
                    # Get nearest color (up, dowm)
                    if pixel in self.color_code_up_down and dist < md_ud:
                        n_ud = {
                            "pixel": (x, y),
                            "color": pixel,
                            "command": self.color_code_up_down[pixel],
                            "distance": dist
                        }
                        md_ud = dist
            return n, n_ud, (bx_min, by_min, bx_max, by_max)

        def _filter_hit(n, radius, center):
            """E1：返回 (本帧可用的像素 或 None, 丢弃说明, 处置种类)。

            处置种类（`_scan_filtered` 打日志用，也是本层**唯一的对外语义**）：
              ""         本次没触发过滤（像素照旧）
              "alt"      换到了**另一个**本帧真能用的像素
              "fallback" 侧向像素换不到别的 ⇒ **仍按它走**（老行为，2026-10-14）
              "none"     goal 像素且换不到 ⇒ 本帧没有可用像素（交给回正 / 救援）
            """
            if not _skip_on or n is None:
                return n, "", ""
            _bad, _why = self._route_hit_unusable(n, _goal_y, _y_tol)
            if not _bad:
                return n, "", ""
            _detail = _why
            if "goal" in str(n.get("command", "")):
                _alt, _alt_ud, _box = _scan(radius, center, skip_goal=True)
                if _alt is None:
                    _detail += "；排除 goal 像素后这个搜索框里一个路线像素都没有"
                else:
                    _bad2, _why2 = self._route_hit_unusable(_alt, _goal_y, _y_tol)
                    if not _bad2:
                        return _alt, "", "alt"
                    _detail += (f"；排除 goal 后最近的像素 "
                                f"{tuple(int(v) for v in _alt['pixel'])} 也用不了"
                                f"（{_why2}）")
                return None, _detail, "none"
            # ── ② 侧向（"另一层"）分支：2026-10-14 修（用户 m09666 拍板）──────────
            # E1 第一版这里是"直接返回 None ⇒ 整帧交给回正线 / 失联救援"。
            # 真机代价（`log/MSBot_2026-10-14_16-51-02.log`，465s，第一份带新配置的
            # 会话）：`[路线·goal过滤]` 46 次，**46/46 全部以「本帧没有可用的路线
            # 像素」收场**，"改用别的像素" 0 次；理由 45/46 是"另一层"，
            # Δy 分布 4px=12 / 8px=17 / 9px=1 / 12px=11 / 24px=5，触发间隔 p50=5s
            # （= 日志节流上限 ⇒ 几乎每有机会就触发）。后果（次/分钟，对比 55 分钟
            # 的旧会话 `MSBot_2026-10-14_15-47-36.log`）：
            #   `[回正]` 2.2 → 14.2（6.5×）、`[路线失联]` 0.9 → 3.4（3.8×）、
            #   `[回正·小结]` 0.22 → 1.03（4.7×）；8 条小结里 6 条是
            #   「一次都没走到起跳点 | 离回正线 22~80px、离主线 0px」
            #   ⇒ 进去就被判"接不上"退出，并把那条回正线**去武装**。
            # 为什么 4px 也会被判"另一层"：本图相邻路线行距只有 4~8px
            #   （y=86/78/74/70/66/58/54），而这里的容差复用的是
            #   `goal_reach_y_tol`=3 ⇒ 正常跟随时的坐标抖动就够触发；
            #   真正的换层是 24~48px 量级。
            # ⇒ 新行为（与 goal 分支对称，但**不放弃**）：
            #   ① 先在同一个搜索框里换一个"本帧真能用"的像素；
            #   ② 换不到 ⇒ **退回用它**（老行为 `left/right none none`）。
            #      绝不因为"它可能在另一层"就把整帧交给回正 / 救援 ——
            #      「站着不动 / 被回正拽走并去武装一条线」比「朝同排像素走一步」差得多。
            _alt, _alt_ud, _box = _scan(radius, center, usable_only=True)
            if _alt is not None:
                return _alt, _why, "alt"
            return (n, _why + "；同一个搜索框里换不到别的可用像素 ⇒ 本帧仍按它走"
                              "（退回老行为）", "fallback")

        def _scan_filtered(radius, center):
            """扫描 + E1 过滤（过滤发生时 5s 节流打一行日志）。"""
            n, n_ud, box = _scan(radius, center)
            n, _why, _kind = _filter_hit(n, radius, center)
            if _why and time.time() - getattr(self, "t_goal_skip_log", 0.0) > 5:
                self.t_goal_skip_log = time.time()
                if _kind == "alt":
                    _use = f"改用 {tuple(int(v) for v in n['pixel'])}（{n['command']}）"
                elif _kind == "fallback":
                    _use = (f"仍按它走（{n['command']}）—— 换不到别的可用像素，"
                            f"本帧不再交给回正 / 救援")
                else:
                    _use = "本帧没有可用的路线像素 ⇒ 交给回正线 / 救援接管"
                logger.info(
                    f"[路线·goal过滤] 最近的路线像素没用：{_why}\n"
                    f"        {_use}；`route.goal_pixel_skip` 填 0 = 关掉本层")
            return n, n_ud, box

        nearest, nearest_up_down, scan_box = _scan_filtered(
            self.cfg["route"]["search_range"], self.loc_player_global)

        # 失联救援（2026-09-12）：被怪碰撞/打怪位移很容易偏离路线超过正常搜索半径
        # （默认仅 10px）→ 指令真空、干站发呆，只能等 watchdog 抽随机动作（实测）。
        # 两级救援（用户设计：平时优先接回所在段，严重偏移才接受其它段）——
        #   Level 2：以「失联前最后跟随的路线像素」为锚点搜索。折返段即使空间上
        #            离角色更近，只要锚点附近还有当前段的像素，就接回当前段；
        #   Level 3：锚点附近确实没有像素（严重偏移，如掉下台子）才以角色为
        #            中心接受任何段。
        # 正常态坚持小半径，是因为搜得太宽会抓到隔壁折返段的像素、被引去错误分支。
        if nearest is None and nearest_up_down is None:
            rescue = self.cfg["route"].get("rescue_range", 40)
            if rescue > self.cfg["route"]["search_range"]:
                level = None
                anchor = getattr(self, "loc_last_route_pixel", None)
                if anchor is not None:
                    nearest, nearest_up_down, scan_box = _scan_filtered(rescue, anchor)
                    if nearest is not None or nearest_up_down is not None:
                        level = f"接回当前段（锚点 {anchor}）"
                if nearest is None and nearest_up_down is None:
                    nearest, nearest_up_down, scan_box = _scan_filtered(
                        rescue, self.loc_player_global)
                    if nearest is not None or nearest_up_down is not None:
                        level = "严重偏移，接入最近段"
                if level is not None and \
                        time.time() - getattr(self, "t_route_rescue_log", 0) > 5:
                    self.t_route_rescue_log = time.time()
                    hit = nearest if nearest is not None else nearest_up_down
                    # ⚠️ 2026-10-14 修文案（用户 m08454 报「偏离路线后站着不动」）：
                    #   原来打的是 `hit["distance"]` —— 那是**扫描中心到该像素**的距离；
                    #   而 Level 2 的扫描中心正是"锚点"本身（锚点必然是个路线像素）
                    #   ⇒ 那个数字恒为 0。用户看到「距离 0px」会以为"角色就在路线上"，
                    #   实际日志里角色离它 16~22px（`log/MSBot_2026-10-14_15-07-02.log`
                    #   的 `(74,54)` vs 锚点 `(58,76)`）。
                    #   ⇒ 改成打**到角色的实际距离** + 路线像素坐标。
                    #   顺带点明"锚点是 goal 像素"这种接不动的接引（命令是
                    #   `none none goal`，一个不会让角色移动的命令）。
                    _hp = tuple(int(v) for v in hit["pixel"])
                    _d_play = abs(_hp[0] - int(self.loc_player_global[0])) + \
                        abs(_hp[1] - int(self.loc_player_global[1]))
                    _goal_hint = ""
                    if "goal" in str(hit["command"]):
                        _goal_hint = ("；⚠️ 这个像素是本段的 goal 像素 ⇒ 接引命令"
                                      "（none none goal）**不会让角色移动**，"
                                      "若接下来一直干站，就是这里接错了段")
                    logger.warning(
                        f"[路线失联] 角色已偏离路线（小地图位置 {self.loc_player_global}），"
                        f"{level}，重新接引：{hit['command']}"
                        f"（路线像素 {_hp}，到角色 {_d_play}px）{_goal_hint}")

        # Debug 绘制：搜到路线像素时画搜索框 + 角色到路线的连接线。
        # ⚠️ 同 get_player_location_on_global_map：原来没判 viz 状态，img_route_debug
        # 为 None 时 cv2.line(None,...) 会抛 AttributeError 把主循环带崩。现在统一判。
        if self.is_show_debug_window and self.img_route_debug is not None:
            draw_rectangle(
                self.img_route_debug,
                (scan_box[0], scan_box[1]),
                (scan_box[2] - scan_box[0],
                 scan_box[3] - scan_box[1]),
                (0, 0, 255), "", text_height=0.4, thickness=1,
            )
            # Draw a straigt line from map_loc_player to color_code["pixel"]
            if nearest is not None:
                cv2.line(
                    self.img_route_debug,
                    self.loc_player_global, # start point
                    nearest["pixel"],       # end point
                    (0, 255, 0),            # green line
                    1                       # thickness
                )
                # 2026-09-12：原来这里会在游戏画面上写 "Route Action: ..." 和
                # "Route Index: ..." 两行，按用户要求去掉（画面上只留两个框）。
                # 当前指令 / 段号看日志的 [运行状态]（每 5 秒一条）。

            if nearest_up_down is not None:
                # 2026-09-12：原来这里会再写一行 "Route Action: ..."（上下方向），已去掉。
                cv2.line(
                    self.img_route_debug,
                    self.loc_player_global,  # start point
                    nearest_up_down["pixel"],# end point
                    (0, 0, 255),             # green line
                    1                        # thickness
                )

        # 记录最近跟随的路线像素，作为下次失联救援的锚点（见上面的两级救援说明）
        if nearest is not None:
            self.loc_last_route_pixel = nearest["pixel"]
        elif nearest_up_down is not None:
            self.loc_last_route_pixel = nearest_up_down["pixel"]

        return nearest, nearest_up_down  # if not found return none

    def get_attack_range(self, is_left=True):
        '''
        get_attack_range
        '''
        if self.cfg["bot"]["attack"] == "aoe_skill":
            dx = self.cfg["aoe_skill"]["range_x"] // 2
            dy = self.cfg["aoe_skill"]["range_y"] // 2
            x0 = max(0, self.loc_player[0] - dx)
            x1 = min(self.img_frame.shape[1], self.loc_player[0] + dx)
            y0 = max(0, self.loc_player[1] - dy)
            y1 = min(self.img_frame.shape[0], self.loc_player[1] + dy)

        elif self.cfg["bot"]["attack"] == "directional":
            if is_left:
                x0 = self.loc_player[0] - self.cfg["directional_attack"]["range_x"]
                x1 = self.loc_player[0]
            else:
                x0 = self.loc_player[0]
                x1 = x0 + self.cfg["directional_attack"]["range_x"]
            y0 = self.loc_player[1] - self.cfg["directional_attack"]["range_y"] // 2
            y1 = y0 + self.cfg["directional_attack"]["range_y"]
        else:
            raise RuntimeError(f"Unsupported attack mode: {self.cfg['bot']['attack']}")

        return (x0, y0, x1, y1)

    def get_detect_extent(self):
        '''
        「感知框」的半宽/半高 (dx, dy) —— 只决定**看得见多远**，不决定打不打。

        与攻击框（get_attack_range）的区别（2026-10-13 用户选的方案 A）：
          · 攻击框 = 技能真打得到的范围（aoe: range/2；directional: range）；
          · 感知框 = 攻击框口径 + `search_box_margin`，再乘
            `monster_detect.detect_range_scale`。
        为什么要分开：旧实现两者是同一个框（±(range/2+margin) = 500×270，只占
        1366×768 客户区的 13%）。用户真机实测 12 帧：整帧 53 只怪里 **46 只（87%）
        落在框外** ⇒ 现象是"身边明摆着有怪，机器人不认"。
        放大感知范围**不改变攻击范围**：够不着的怪交给 approach_mob 走过去打。

        ⚠️ 默认 scale = 1.0（= 老行为）；出厂配置 `config/config_default.yaml`
           显式写 2.0。自检夹具（自己拼 cfg 的那些）不写这个键 ⇒ 框与老口径一致，
           不会因为这次改动出现"夹具期望值全部要改"的连带。
        '''
        _md = self.cfg.get("monster_detect") or {}
        try:
            _margin = int(_md.get("search_box_margin", 50) or 0)
        except (TypeError, ValueError):
            _margin = 50
        if self.cfg["bot"]["attack"] == "aoe_skill":
            dx = self.cfg["aoe_skill"]["range_x"] // 2 + _margin
            dy = self.cfg["aoe_skill"]["range_y"] // 2 + _margin
        elif self.cfg["bot"]["attack"] == "directional":
            dx = self.cfg["directional_attack"]["range_x"] + _margin
            dy = self.cfg["directional_attack"]["range_y"] + _margin
        else:
            dx = dy = _margin
        try:
            _scale = float(_md.get("detect_range_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            _scale = 1.0
        if _scale < 1.0:      # 负/0/0.x：夹回 1.0（"看得更少"没有意义，只会白白漏怪）
            _scale = 1.0
        # ★ 2026-10-13 加：**竖直方向单独一个倍数**（`detect_range_scale_y`）。
        #   为什么需要：感知框唯一的下游用途是 approach_mob「走过去打」，
        #   而它只在**同一层**才敢走（|dy| <= approach_dy_tol = 60）。
        #   ⇒ 竖直方向多看到 ±270px 是纯浪费：真机 12 帧里 60% 的怪在
        #     角色上方 280~320px（上层平台），既够不着也走不过去。
        #   实测（tools/verify_monster 的 12 帧，模板 10 张含翻转）：
        #     1000×540 = 141ms；1000×296（本键 1.1）= 77ms ⇒ **省 45% 的检测耗时**，
        #     而 x 方向的可达范围一点没少。缺键 ⇒ 沿用 detect_range_scale（老口径）。
        try:
            _scale_y = float(_md.get("detect_range_scale_y", _scale) or _scale)
        except (TypeError, ValueError):
            _scale_y = _scale
        if _scale_y < 1.0:
            _scale_y = 1.0
        return int(round(dx * _scale)), int(round(dy * _scale_y))

    def get_search_box(self):
        '''
        本帧怪检测的**索敌框** (x0, y0, x1, y1)（客户区坐标，已 clamp 到帧内）。

        ⚠️ 这个函数是"画出来的框"与"怪检测真正用的框"的**唯一出处**：
           `update_cmd_by_mob_detection` 与帧尾画框（update_info_on_img_frame_debug）
           都必须调它。历史上这两处各算一遍，注释里靠"纯函数所以必然一致"来保证 ——
           本轮加了 detect_range_scale / stale 放大之后，这个保证不再成立，
           所以改成同一个函数（tools/verify_mob_boxes.py 的 [6b] 钉这条）。

        位置连续丢失（nametag_miss_streak >= stale_pos_full_frame_after）时，
        （⚠️ 该键 **2026-10-14 起默认 0 = 关掉下面这条放大**，见 config_default.yaml）
        把索敌框**临时放大到整帧**：位置都已经不可信了，还拿它当中心只会把怪框在
        错误的位置外（"框错了"比"框大了"更糟 —— 大了只是多算几次模板匹配）。

        ★ 2026-10-14 加第三条（低频识别优化 P2，用户 m05009「落地」）：
           **交战期间只扫「攻击框 + search_box_margin」**（`engaged_scan`，默认开）。
           为什么：一次怪检测 17.1ms 里 ~99% 是模板匹配，而匹配耗时 ∝ 扫描面积。
           实测（`_probe_detect_cost.py`，帧 800×1368、感知框 660×264、攻击框 340×140、
           4 张模板×2 镜像 = 8 次匹配）：**全框 17.1ms → 只扫攻击框 7.2ms（-58%）**，
           单张模板 2.11ms → 0.51ms。交战期间要回答的问题只有"目标还在不在、
           有没有新怪靠过来"，这两件事都发生在攻击框附近；远处的怪等**松开那一帧**
           重新扫全框完全来得及（松开后立刻回到全感知框）。
           ⚠️ 顺序：位置失效（上面那条）**优先** —— 位置都不可信时必须扫全帧。
           ⚠️ `engaged_scan: false` = 逐字退回老行为（交战期间照样扫全感知框）。
        '''
        _md = self.cfg.get("monster_detect") or {}
        _h_f, _w_f = self.img_frame.shape[:2]
        try:
            _after = int(_md.get("stale_pos_full_frame_after", 0) or 0)
        except (TypeError, ValueError):
            _after = 0
        if _after > 0 and int(getattr(self, "nametag_miss_streak", 0) or 0) >= _after:
            return (0, 0, _w_f, _h_f)
        if bool(getattr(self, "_stop_hold", False)) and bool(_md.get("engaged_scan", True)):
            try:
                _mgn = int(_md.get("search_box_margin", 50) or 0)
            except (TypeError, ValueError):
                _mgn = 50
            _ab = []
            try:
                _ab = [self.get_attack_range(is_left=True)]
                if self.cfg["bot"]["attack"] == "directional":
                    _ab.append(self.get_attack_range(is_left=False))
            except Exception as _e:                          # noqa: BLE001
                # 算不出来就退回**全感知框**：看少了比看多了危险（会漏怪）。
                # 落一条可查的日志 —— 这条出现就说明攻击框口径本身有问题。
                logger.warning(f"[索敌框] 交战收紧算不出攻击框（{_e}）→ 本帧仍扫全感知框")
            if _ab:
                return (max(0, min(_b[0] for _b in _ab) - _mgn),
                        max(0, min(_b[1] for _b in _ab) - _mgn),
                        min(_w_f, max(_b[2] for _b in _ab) + _mgn),
                        min(_h_f, max(_b[3] for _b in _ab) + _mgn))
        dx, dy = self.get_detect_extent()
        return (max(0, self.loc_player[0] - dx),
                max(0, self.loc_player[1] - dy),
                min(_w_f, self.loc_player[0] + dx),
                min(_h_f, self.loc_player[1] + dy))

    def _min_mob_area(self):
        '''
        怪与框「算交叠」的面积下限（像素²）。

        与 `get_nearest_monster` 原实现同一个口径：所有模板里最小的那张的面积
        （一张模板都没有时退回 0），再被 `monster_detect.max_mob_area_trigger` 封顶。
        抽成 helper 是因为 `_mobs_in_attack_box` 也要用同一个下限 —— 两处各写一遍
        就是"改一处漏一处"的经典形态。
        ⚠️ 全用 .get + try：自检夹具常只拼几个键，直接下标会 KeyError 带崩既有自检。
        '''
        try:
            _areas = [im.shape[0] * im.shape[1]
                      for _, _imgs in (self.monsters_info or {}).items()
                      for im, _ in _imgs]
        except Exception:                                    # noqa: BLE001
            _areas = []
        _min_area = min(_areas) if _areas else 0
        try:
            _cap = int((self.cfg.get("monster_detect") or {})
                       .get("max_mob_area_trigger", 1500) or 0)
        except (TypeError, ValueError):
            _cap = 1500
        return min(_min_area, _cap)

    def _engage_mode(self):
        '''
        「够得着」的判据名（`monster_detect.mob_engage_mode`）—— **只有这一处读**。

        返回 `"center"` 或 `"min_area"`：
          · `center`（**出厂默认**）：怪框中心落在攻击框内。
          · `min_area`（老口径，保留可回退）：交叠面积 >= `_min_mob_area()`。
        畸形值 / 缺键 / 读不到 ⇒ `"min_area"`（保守 = 老口径，绝不静默放宽）。
        ⚠️ 判据名只在这里归一，`_mobs_in_attack_box()` 与 `[怪检测]` 明细行都调它 ——
           同一判据写两处就会出现"日志说 center、判定却在用 min_area"这种静默失准。
        '''
        try:
            _mode = str((self.cfg.get("monster_detect") or {})
                        .get("mob_engage_mode", "min_area") or "min_area").strip().lower()
        except Exception:                                     # noqa: BLE001
            _mode = "min_area"
        return _mode if _mode in ("min_area", "center") else "min_area"

    def _engage_margin(self):
        '''
        「宽判定」的像素余量（`monster_detect.mob_engage_margin`）—— **只有这一处读**。

        由来（用户 m08016 拍板「B 够得着判据加余量」，依据 = 2026-10-14 示范录制的读数）：
          389 帧「引擎**看见了**怪、却判够不着」里，最近那只怪 **69% 只差在框外
          ≤ 45px**（dx p50=6px / p90=45px；dy p50=0 / p90=42px）—— 就是"差一点点
          就不算"。人眼是**预估**的（用户 m06810 原话：「人不能准确计算，只能预估
          大概在范围内就会攻击，属于宽判定」），所以这里把攻击框向外扩 margin
          像素，再拿扩过的框做够得着判定。

        取值：`0` = 关掉（严格按技能框）；**缺键 ⇒ 40**（出厂默认 —— 老配置
          文件里没有这个键时也照 40 走，否则会出现"改了却看不出变化"）；
          畸形 / 负数 / > 200 ⇒ 0（保守：绝不因为配置写坏而静默放宽）。
        ⚠️ 只放大**判定**用的框。画在屏幕上的攻击框仍是技能真实范围
          （`get_attack_range()` 一个像素都不动）—— 免得屏幕上的框越画越大、
          和技能真实范围对不上。
        '''
        try:
            _raw = (self.cfg.get("monster_detect") or {}).get(
                "mob_engage_margin", 40)
        except Exception:                                     # noqa: BLE001
            return 0
        if _raw is None:
            return 40
        try:
            _m = int(float(_raw))
        except (TypeError, ValueError):
            return 0
        return _m if 0 <= _m <= 200 else 0

    def _attack_boxes(self):
        '''
        「够得着」判定用的攻击框（**已加 `mob_engage_margin` 余量**）—— 只有这一处拼。

        directional 模式合并左右两个框（与 `get_nearest_monster` 的两次调用一致）。
        ⚠️ 三个消费点（`_mobs_in_attack_box` / `_engage_gap` / `[怪检测]` 明细行）
          必须都走这里：各拼一遍就会出现"日志说差 6px、判定却在拿加过余量的框算"
          这种静默失准（`03` §6 教训：改一处漏一处）。
        '''
        if self.cfg["bot"]["attack"] == "aoe_skill":
            _bs = [self.get_attack_range(is_left=True)]
        else:
            _bs = [self.get_attack_range(is_left=True),
                   self.get_attack_range(is_left=False)]
        _m = self._engage_margin()
        if _m:
            _bs = [(_b[0] - _m, _b[1] - _m, _b[2] + _m, _b[3] + _m)
                   for _b in _bs]
        return _bs

    def _mobs_in_attack_box(self):
        '''
        **攻击框内**的怪（"够得着、打得到"的那一批）。

        ⚠️ 与索敌框（get_search_box）不是一回事：索敌框可能被 detect_range_scale
           放大到只看不打的远距离。攻击判定、停下来打怪都以本函数为准 ——
           否则会对着够不着的怪一直出招/一直站着。
        directional 模式合并左右两个框（与 get_nearest_monster 的两次调用一致）。
        交叠判据与 `get_nearest_monster` 同款（交叠面积 >= _min_mob_area()）。
        ★ 2026-10-14 加 `monster_detect.mob_engage_mode` 开关（用户 m06668
          「红色攻击框里有绿色的检测框，但是就是不攻击」）：
            · `center`（**出厂默认**，2026-10-14 用户拍板"宽判定"）：怪框**中心**
              落在攻击框内 ⇒ 算够得着。判据与眼睛一致、人眼可核对。
            · `min_area`（老口径，保留可回退）：交叠面积 >= `_min_mob_area()`。
              注意这条对**小怪**很严：`_min_mob_area()` 取的是本图**最小**那张模板的
              面积（废铁2 = 青蛇 28×31 = 868px²），等于要求小怪**整个**框都落在攻击框
              里 —— 差 1px 就不算"够得着"（人眼分辨不出 1px ⇒ 看着"在框里却不打"）。
            非法/缺失 ⇒ 按 `min_area`（保守 = 老口径，绝不静默放宽）。
        '''
        # ★ 2026-10-14：框只在这里拼一次（`_attack_boxes()` 已含 `mob_engage_margin`）
        _boxes = self._attack_boxes()
        _thres = self._min_mob_area()
        _mode = self._engage_mode()
        out = []
        for monster in self.monsters:
            mx, my = monster["position"]
            mw, mh = monster["size"]
            for (bx0, by0, bx1, by1) in _boxes:
                iw = max(0, min(bx1, mx + mw) - max(bx0, mx))
                ih = max(0, min(by1, my + mh) - max(by0, my))
                if _mode == "center":
                    # 「怪框中心落在攻击框内」—— 与人的直觉一致（"它就在框里"）。
                    # ⚠️ 不写"或交叠 >= 自身面积一半"：对轴对齐矩形，交叠 >= 自身面积
                    #    的一半 ⇒ 两个方向都至少有一半在框内 ⇒ **必然**包含中心，
                    #    那条分支是恒假的废话（写上去只会让人以为有额外语义）。
                    cx, cy = mx + mw / 2.0, my + mh / 2.0
                    if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                        out.append(monster)
                        break
                elif iw * ih >= _thres:
                    out.append(monster)
                    break
        return out

    def _engage_gap(self, mobs=None):
        '''
        「离**算够得着**最近的那一只还差多少」—— **只给日志文案用**（纯观测，不参与判定）。

        由来（用户 m06668「红色攻击框里有绿色的检测框，但是就是不攻击」）：
          只说"框外有 N 个目标"分不清三种完全不同的处境 ——
            ① 差一点点 ⇒ 交叠判据太严（最小模板的**整个**面积才算够得着）；
            ② **中心已经在攻击框里**却仍不算 ⇒ 判据与人的直觉不一致；
            ③ 差得远 ⇒ 画出来的是索敌框，本来就不该打（预期行为）。

        Returns:
            (area, need, dx, dy, center_in) 或 None（算不出来 / 没怪）：
              · area   离得最近的那只与攻击框的最大交叠面积（px²）
              · need   "算够得着"需要的面积（`_min_mob_area()`）
              · dx, dy 它中心离攻击框的水平/垂直距离（已在框内 = 0）
              · center_in 怪框**中心**已在攻击框内的目标数
        '''
        _list = list(getattr(self, "monsters", None) or []) if mobs is None else list(mobs)
        if not _list:
            return None
        try:
            _need = int(self._min_mob_area())
            # ★ 2026-10-14：与 `_mobs_in_attack_box()` 用**同一个**框（含
            #   `mob_engage_margin`）—— 否则日志里的"差 N px"和实际判定用的框
            #   不是一个东西，就会出现"日志说差 6px 却没算够得着"的怪相。
            _boxes = self._attack_boxes()
        except Exception:                                     # noqa: BLE001
            return None
        _best = None
        _center_in = 0
        for _m in _list:
            try:
                _mx, _my = _m["position"]
                _mw, _mh = _m["size"]
            except Exception:                                 # noqa: BLE001
                continue
            _cx, _cy = _mx + _mw / 2.0, _my + _mh / 2.0
            _in_any = False
            for (_bx0, _by0, _bx1, _by1) in _boxes:
                _iw = max(0, min(_bx1, _mx + _mw) - max(_bx0, _mx))
                _ih = max(0, min(_by1, _my + _mh) - max(_by0, _my))
                _area = _iw * _ih
                if _bx0 <= _cx <= _bx1 and _by0 <= _cy <= _by1:
                    _in_any = True
                    _dx = _dy = 0
                else:
                    _dx = int(max(_bx0 - _cx, 0.0, _cx - _bx1))
                    _dy = int(max(_by0 - _cy, 0.0, _cy - _by1))
                if _best is None or _area > _best[0]:
                    _best = (_area, _dx, _dy)
            if _in_any:
                _center_in += 1
        if _best is None:
            return None
        return (_best[0], _need, _best[1], _best[2], _center_in)

    def _engage_gap_facts(self, mobs=None):
        '''`_engage_gap()` 的 kwargs 版，供 `_mob_no_attack_log(..., **facts)` 直接用。

        ⚠️ 纯观测：拿不到就返回**空 dict**（一个字都不加），绝不抛异常。
        '''
        _g = self._engage_gap(mobs)
        if not _g:
            return {}
        return dict(gap_area=_g[0], gap_need=_g[1], gap_dx=_g[2], gap_dy=_g[3],
                    center_in=_g[4])

    def _attack_box_half(self):
        '''
        攻击框的半宽/半高（像素）—— **只给日志文案用**（纯观测，不参与任何判定）。

        ⚠️ 口径必须与 get_attack_range() 一致（那里才是权威）：
           aoe         → range_x//2 / range_y//2（以角色为中心，左右上下都有）
           directional → range_x（**单侧**）/ range_y//2
        '''
        try:
            _bot = self.cfg.get("bot") or {}
            if _bot.get("attack") == "aoe_skill":
                _a = self.cfg.get("aoe_skill") or {}
                return int(_a.get("range_x", 0)) // 2, int(_a.get("range_y", 0)) // 2
            _d = self.cfg.get("directional_attack") or {}
            return int(_d.get("range_x", 0)), int(_d.get("range_y", 0)) // 2
        except (TypeError, ValueError):
            return 0, 0

    def _game_window_front(self):
        '''
        游戏窗口是否在前台（True/False）；**拿不到就返回 None（未知）**。

        ⚠️ 必须是三态：`None`（查不到：键盘控制器还没起、或查询抛了）与 `False`
           （确认不在前台）是两回事 —— 文案里分开写，别把"不知道"写成"正常"。
        '''
        try:
            _kb = getattr(self, "kb", None)
            if _kb is None:
                return None
            return bool(_kb.is_game_window_active())
        except Exception:                                     # noqa: BLE001
            return None

    def _note_attack(self, engage_n=0):
        '''
        纯观测：记一次「**真的**发出了 attack 指令」（2026-10-14 加）。

        由来（用户真机反馈 m06529「攻击就是很慢」）：日志里 attack 指令只存在
        **一帧**（下一帧就被清掉，见 update_cmd_by_mob_detection 里的残留清理），
        所以按指令采样永远抓不到 —— 必须在这儿**数**，否则"出招频率"根本无从考证。

        ⚠️ 不参与任何判定；任何异常都不许影响出招（调用点就在出招那一行上）。
        '''
        try:
            _now = time.time()
            _prev = float(getattr(self, "t_last_attack", 0.0) or 0.0)
            self._atk_n = int(getattr(self, "_atk_n", 0)) + 1
            # ★ P4 纯观测：把出招时刻留给 [开火预算] 算"近 60s 覆盖率"
            try:
                self._budget_atk_ts = list(
                    getattr(self, "_budget_atk_ts", None) or [])[-600:] + [_now]
            except Exception:                                 # noqa: BLE001
                pass
            if _prev > 0:
                _gap = _now - _prev
                # 只收合理间隔：跨"停下打怪/回正/爬梯"的巨大间隔会把平均值带偏
                if 0 < _gap < 30.0:
                    self._atk_gap_n = int(getattr(self, "_atk_gap_n", 0)) + 1
                    self._atk_gap_sum = float(getattr(self, "_atk_gap_sum", 0.0)) + _gap
                    self._atk_gap_max = max(
                        float(getattr(self, "_atk_gap_max", 0.0) or 0.0), _gap)
                    _mn = getattr(self, "_atk_gap_min", None)
                    if _mn is None or _gap < float(_mn):
                        self._atk_gap_min = _gap
            _e = int(engage_n or 0)
            self._atk_eng_max = max(int(getattr(self, "_atk_eng_max", 0)), _e)
            _em = getattr(self, "_atk_eng_min", None)
            if _em is None or _e < int(_em):
                self._atk_eng_min = _e
            # ★ 2026-10-14 加：**灵敏度的直接量** —— 第一只怪进攻击框到真正出刀
            #   中间隔了多久（用户 m06852「攻击还是不够灵敏」）。只在"框内确实有怪"
            #   的那一刀上算；锁定补招那一刀（engage_n=0）不算，否则会把"补一刀"
            #   混进灵敏度里。`_eng_first_t` 由调用点在框内空了时清掉 ⇒ 这里的值
            #   恒等于"这一批怪连续在框内的时长"。
            if _e > 0:
                _t_in = getattr(self, "_eng_first_t", None)
                if _t_in:
                    self._eng_first_t = None
                    _ms = max(0.0, (_now - float(_t_in)) * 1000.0)
                    if self.cfg["bot"]["attack"] == "aoe_skill":
                        _cd = float((self.cfg.get("aoe_skill") or {}).get("cooldown", 0.0) or 0.0)
                    else:
                        _cd = float((self.cfg.get("directional_attack") or {})
                                    .get("cooldown", 0.0) or 0.0)
                    logger.info(
                        f"[出招·延迟] 第一只怪进攻击框 → 出刀 用了 {_ms:.0f}ms"
                        f"（检测每 {monster_detect_interval(self.cfg)} 帧一次；"
                        f"设定冷却 {_cd:.2f}s）")
                    # ★ P4 纯观测（2026-10-14）：进框→出刀 + 确认→进框 两段的样本
                    try:
                        self._budget_box2hit = list(
                            getattr(self, "_budget_box2hit", None) or []) + [_ms / 1000.0]
                        _ct = getattr(self, "_budget_confirm_t", None)
                        if _ct:
                            self._budget_ok2box = list(
                                getattr(self, "_budget_ok2box", None) or []) + [_now - float(_ct)]
                            self._budget_confirm_t = None
                    except Exception:                             # noqa: BLE001
                        pass
        except Exception:                                     # noqa: BLE001
            pass

    def _log_attack_heartbeat(self):
        '''
        纯观测：`[出招] 近5秒出招 N 次 …`（2026-10-14 加，紧跟 [运行状态] 的 5 秒节拍）。

        看什么：
          · 出招 0 次 而画面有怪 ⇒ 去翻同刻的 `[没出招·归因]`（冷却？框外？绳梯让路？）
          · 出招次数 ≈ 5 / 设定冷却 ⇒ 摸到了冷却上限，**冷却本身就是限速器**
            （aoe_skill.cooldown 默认 0.05 = 只防重复按键；真机被设成 0.77 ⇒ 1.3 次/秒）
          · 间隔远大于设定冷却 ⇒ 时间花在"没怪 / 走过去 / 爬梯"上，不是出招被卡
        '''
        try:
            _n = int(getattr(self, "_atk_n", 0))
            _gn = int(getattr(self, "_atk_gap_n", 0))
            _avg = (float(getattr(self, "_atk_gap_sum", 0.0)) / _gn) if _gn else 0.0
            _mn = getattr(self, "_atk_gap_min", None)
            _mx = float(getattr(self, "_atk_gap_max", 0.0) or 0.0)
            if _gn:
                _gap = (f"间隔 平均{_avg:.2f}s/最小{float(_mn):.2f}s/最大{_mx:.2f}s")
            else:
                _gap = "间隔 -（本窗口没有连续两次出招）"
            try:
                if self.cfg["bot"]["attack"] == "aoe_skill":
                    _cd = float(self.cfg["aoe_skill"].get("cooldown", 0.0))
                else:
                    _cd = float(self.cfg["directional_attack"].get("cooldown", 0.0))
            except Exception:                                 # noqa: BLE001
                _cd = 0.0
            _em = getattr(self, "_atk_eng_min", None)
            _eng = f"{int(_em)}~{int(getattr(self, '_atk_eng_max', 0))}" if _n else "-"
            _tail = ""
            if _n and _cd >= 0.3:
                _tail = f"  ⚠️ 设定冷却 {_cd:.2f}s 就是限速器（上限约 {5.0 / _cd:.1f} 次）"
            elif _n and _gn and _avg > _cd * 1.5 and _cd > 0:
                _tail = "  ⇒ 没摸到冷却上限：时间花在没怪/走过去/爬梯上"
            logger.info(
                f"[出招] 近5秒出招 {_n} 次（{_gap}；设定冷却 {_cd:.2f}s；"
                f"出招时攻击框内 {_eng} 个目标）{_tail}")
        except Exception as _e:                               # noqa: BLE001
            logger.info(f"[出招] 心跳统计失败：{_e!r}")
        finally:
            # 失败了也要清账：否则失败一次就永远重复报同一批数字
            self._atk_n = 0
            self._atk_gap_n = 0
            self._atk_gap_sum = 0.0
            self._atk_gap_min = None
            self._atk_gap_max = 0.0
            self._atk_eng_min = None
            self._atk_eng_max = 0

    def _budget_note_lost(self, why):
        '''[开火预算] 记一次"没打成"的原因（纯观测；失败绝不影响判定/挂机）。'''
        try:
            _d = getattr(self, "_budget_lost", None)
            if not isinstance(_d, dict):
                _d = {}
                self._budget_lost = _d
            _d[str(why)] = int(_d.get(str(why), 0)) + 1
        except Exception:                                     # noqa: BLE001
            pass

    def _log_fire_budget(self):
        '''
        纯观测：`[开火预算] …`（2026-10-14 加，P4；紧跟 [出招] 的 5 秒节拍）。

        由来（用户 m10830「把'人眼的实时反应'拆解成'适合低频检测的确认流程'」）：
          审计 `log/MSBot_2026-10-14_17-40-51.log` 段11（152 分钟）显示「出招」本身
          不慢（`[出招·延迟]` p50 33ms＝怪已在框里到出刀约一帧），慢的是"该不该打、
          走不走得过去"：>10s 的打怪空档 105 次合计 2726s（占 30%），其中 104 次
          含 `[绳/梯]` 事件、87 次（1540s = 17%）角色几乎没动却在按着键。
          ⇒ 把"从看见一只怪到真正出刀"拆成三段计时 + 没打成的账，才能在下一次
          真机会话里直接读出**是哪一段**慢，而不是靠猜。

        三段的定义（都能在 `[出招·延迟]` / `[没出招·归因]` 里对上）：
          · 可见→确认：第一次看见"框外够得着的怪" → 凑够 2 次证据（P1 出现确认）；
          · 确认→进框：确认可追 → 第一只怪进攻击框（走位/追怪这一段）；
          · 进框→出刀：第一只怪进攻击框 → 真的发刀（与 `[出招·延迟]` 同源）。
        覆盖率的定义（与审计脚本 `_c5.py` 一致）：最近 60s 里"距上一次出招 ≤1s"
          的时间占比 —— 它把"站桩/爬绳/走位"与"在打"直接分开。

        跟 `monster_detect.detect_log` 同一个开关（关掉明细日志就一起关掉）。
        ⚠️ 纯观测：全部 getattr 兜底、异常只吼一次，绝不影响判定/挂机。
        '''
        try:
            if not bool((self.cfg.get("monster_detect") or {}).get("detect_log", True)):
                return
            _now = time.time()
            # ① 覆盖率（近 60s）
            _ts = [float(_t) for _t in (getattr(self, "_budget_atk_ts", None) or [])
                   if _now - float(_t) <= 60.0][-600:]
            self._budget_atk_ts = _ts
            _cov = 0.0
            for _i in range(1, len(_ts)):
                _g = _ts[_i] - _ts[_i - 1]
                if _g <= 1.0:
                    _cov += _g
            if _ts:
                _cov += min(_now - _ts[-1], 1.0)
            _cov_pct = 100.0 * _cov / 60.0
            _a = list(getattr(self, "_budget_see2ok", None) or [])
            _b = list(getattr(self, "_budget_ok2box", None) or [])
            _c = list(getattr(self, "_budget_box2hit", None) or [])
            _segs = []
            for _xs, _lab in ((_a, "可见→确认"), (_b, "确认→进框"), (_c, "进框→出刀")):
                _v = sorted(float(_x) for _x in _xs)
                _p = _v[len(_v) // 2] if _v else None
                _segs.append(f"{_lab} " + ("-" if _p is None else f"{_p:.2f}s")
                             + f"(n={len(_v)})")
            _lost = getattr(self, "_budget_lost", None) or {}
            _lost_txt = " / ".join(f"{_k} {int(_v)}"
                                   for _k, _v in sorted(_lost.items())) or "-"
            logger.info(
                f"[开火预算] 近60s 覆盖率 {_cov_pct:.0f}%" + "；"
                + "；".join(_segs) + f"；没打成的账：{_lost_txt}")
        except Exception as _e:                               # noqa: BLE001
            if not getattr(self, "_budget_warned", False):
                self._budget_warned = True
                logger.info(f"[开火预算] 统计失败：{_e!r}")
        finally:
            # 与 [出招] 心跳同一个约定：失败了也要清账，否则永远重复同一批数字
            self._budget_see2ok = []
            self._budget_ok2box = []
            self._budget_box2hit = []
            self._budget_lost = {}

    def _log_monster_detect(self, engage, far):
        '''
        纯观测：把**这一帧的怪检测结果**写成一行 `[怪检测] …`（2026-10-14 加）。

        由来（用户 m06852「记录下怪物的检测信息，现在的攻击还是不够灵敏」）：
            `[运行状态]` 里只有「怪N」—— 看不出怪在**哪**、离攻击框差**多少**、
            引擎认为角色在哪，于是"够不够灵敏"既没法证、也没法调。这一行补上：
            攻击框绝对坐标 + 角色位置 + 每只怪的中心/尺寸/交叠/中心离框多远。

        节流：① 攻击框内那批怪的**指纹变了**且距上次 >= 1s ⇒ 立刻打（"刚进框"
                 正是最需要看清的时刻）；② 否则 >= 5s 打一次（连续但低调）。
        开关：`monster_detect.detect_log`（默认开；false 关掉这一行）。
        ⚠️ 纯观测：异常只吼一次，绝不影响判定/挂机。
        '''
        try:
            _now = time.time()
            if not bool((self.cfg.get("monster_detect") or {}).get("detect_log", True)):
                return
            _eng = list(engage or [])
            _far = list(far or [])
            _sig = tuple(sorted((int(_m["position"][0]), int(_m["position"][1]))
                                for _m in _eng if _m.get("position")))
            _t0 = float(getattr(self, "t_detect_log", 0.0) or 0.0)
            _changed = _sig != getattr(self, "_det_log_sig", None)
            if not ((_changed and _now - _t0 >= 1.0) or (_now - _t0 >= 5.0)):
                return
            self.t_detect_log = _now
            self._det_log_sig = _sig
            try:
                # ★ 与判定同一个框（含 mob_engage_margin）—— 这一行的"中心在框内"
                #   必须和引擎真的怎么判的一致，否则日志会撒谎。
                _r = self._attack_boxes()[0]
                _box = (int(_r[0]), int(_r[1]), int(_r[2]), int(_r[3]))
            except Exception:                                  # noqa: BLE001
                _box = None
            _text = monster_detect_snapshot(
                _eng, _far, box=_box, mode=self._engage_mode(),
                need=self._min_mob_area(), detail_n=4,
                margin=self._engage_margin(),
                player=getattr(self, "loc_player", None))
            logger.info(f"[怪检测] {_text}")
        except Exception as _e:                                # noqa: BLE001
            if not getattr(self, "_det_log_failed", False):
                self._det_log_failed = True
                logger.warning(
                    f"[怪检测] 明细行生成失败：{_e!r} —— 不影响挂机（只吼这一次）。")

    def _mob_no_attack_log(self, kind, **facts):
        '''
        `[没出招·归因]` 一行说清"**画面上有怪、这一帧却没出招**"的原因（2026-10-14 加）。

        由来（用户真机反馈 m05740「有检测框、也在攻击范围内，但是没有立马攻击」）：
          引擎里"没出招"有整整 6 条 `return` 路径（回正线 / 绳梯让路 / 攻击框外 /
          锁定补招冷却 / directional 方向判不出 / 窗口不在前台），每条都合理、也各有
          各的日志，但**没有任何一处把它们和"画面上的框"对上** —— 用户只能看到框，
          看不到"引擎认为够不着"。本函数只做一件事：把本帧的事实汇总成一行。

        ⚠️ 纯观测：**不参与任何判定**，也不改任何 cmd_*。删掉它行为完全不变。
        ⚠️ 只在 `self.monsters` 非空时说话（画面上一个怪都没有 ⇒ 不出招是**正常**的，
           不刷屏）。
        ⚠️ 5 秒节流（与 `[绳/梯]` 同款）：避免把日志刷爆（`verify_log_no_flood` 盯着）。
        ⚠️ 前台判据放在**节流之后**：`is_game_window_active()` 每调一次都要打 win32 API，
           不能每帧调。
        ⚠️ `kind='front'` 是"只在真的不在前台时才说"的：前台正常 ⇒ 直接返回，
           **不消费节流**（否则真·不出的原因会被这一行挤掉）。

        Args:
            kind (str): 交给 mob_no_attack_reason 的归因类别。
            **facts: 覆盖项（engage_n / far_n / front / cooldown_left 等），
                缺的由这里补齐（攻击框半宽半高、两个开关、前台）。
        '''
        if not getattr(self, "monsters", None):
            return
        _now = time.time()
        if _now - float(getattr(self, "t_mob_noatk_log", 0.0)) < 5.0:
            return
        if "front" not in facts:
            facts["front"] = self._game_window_front()
        if kind == "front" and facts.get("front") is not False:
            return                                            # 前台正常/未知 ⇒ 不消费节流
        # ⚠️ **先记账、再凑事实**：下面任何一步出意外，也不许变成"每帧刷一行"
        #    （纯观测代码的底线：既不能带崩主循环，也不能把日志刷爆）
        self.t_mob_noatk_log = _now
        try:
            _hx, _hy = self._attack_box_half()
            facts.setdefault("half_x", _hx)
            facts.setdefault("half_y", _hy)
            if "engage_n" not in facts:
                try:
                    facts["engage_n"] = len(self._mobs_in_attack_box())
                except Exception:                             # noqa: BLE001
                    facts["engage_n"] = 0
            facts.setdefault("far_n",
                             max(0, len(self.monsters) - int(facts["engage_n"])))
            _md = (self.cfg or {}).get("monster_detect") or {}
            facts.setdefault("approach", bool(_md.get("approach_mob", True)))
            facts.setdefault("stop_to_attack", bool(_md.get("stop_to_attack", True)))
            _code, _text = mob_no_attack_reason(kind, **facts)
        except Exception as e:                                # noqa: BLE001
            # 失败绝不静默（`03 §6` 教训 4）：出错就直说，并且只吼一次/5s
            logger.info(f"[没出招·归因] 归因文案生成失败：{e!r}")
            return
        logger.info(f"[没出招·归因] {_text}")

    def _pick_approach_target(self, far_mobs, now=None):
        '''从"看得见但够不着"的怪里挑一个来追 —— 带**出现确认**与**方向最小保持**。

        2026-10-14（低频识别优化 P1，用户 m05009「落地」）加，治真机日志
        （MSBot_2026-10-14_10-03-15.log 的 11:02 段）里看得见的两件事：
          ① **幻影目标**：单帧匹配偶尔会在别处认出一个假目标，老口径"看见一帧就追"
             会让角色为一个不存在的目标横着走几百像素。现在要求"看见够得着的远处怪"
             至少 **2 次采样**才算真看见；★ 2026-10-14（用户 m10830）把"两次紧挨着"
             放宽成"`approach_confirm_s` 秒**窗口内累计**两次"（见 ① 处的长注释：
             老口径在闪烁时会永远凑不齐）。
          ② **左右抖动**：日志里 `[靠近] …left 侧 323px` / `right 侧 282px` 每隔
             几秒换一次边 —— 两只怪离得差不多远时，"最近的那只"每帧都可能换人，
             角色就来回抽方向。现在方向至少保持 `approach_dir_min_s` 秒；
             只有老方向那侧**一只怪都没有**时才允许提前掉头。

        ⚠️ 只约束"追"，不约束"停下打"：停下的代价小（原地站一下）而且必须敏捷，
           所以那一侧仍旧看一帧就算（见 `update_cmd_by_mob_detection`）。
        ⚠️ 跨层的怪（|dy| > approach_dy_tol）照旧不追 —— 与老口径一致。
        ⚠️ 同一帧内可能被调用两次（`_holding` 的追怪分支 + `if not _engage` 的
           靠近分支，directional 下两者都会走到）⇒ 计数用 `_approach_count_t`
           防重复，否则"连续两次采样"会被同一帧凑齐。

        Args:
            far_mobs (list): 看得见但不在攻击框里的那批怪（调用方算好）
            now (float | None): 本帧的时刻（调用方传 `update_cmd_by_mob_detection`
                里的 `_now`）。⚠️ 不能在这里各自 `time.time()` —— 同一帧内本函数会被
                调用两次（追怪 + 靠近），两次的时间戳差几微秒，就会绕过"同一帧只数一次
                采样"的防重复，让**一帧**凑齐"连续两次确认"（幻影目标立刻被追）。

        Returns:
            (str | None, int | None, int | None):
                (方向 'left'/'right', 该侧最近目标的曼哈顿距离, 它的高差 dy)
                没得追 / 还没确认 ⇒ (None, None, None)
        '''
        _md = self.cfg.get("monster_detect") or {}
        try:
            _dy_tol = int(_md.get("approach_dy_tol", 60) or 0)
        except (TypeError, ValueError):
            _dy_tol = 60
        try:
            _confirm_s = float(_md.get("approach_confirm_s", 0.5) or 0.0)
        except (TypeError, ValueError):
            _confirm_s = 0.5
        try:
            _dir_min_s = float(_md.get("approach_dir_min_s", 0.4) or 0.0)
        except (TypeError, ValueError):
            _dir_min_s = 0.4
        _now = float(now) if now is not None else time.time()
        _px, _py = self.loc_player[0], self.loc_player[1]
        _cands = []
        _n_dy_skip = 0
        for _m in far_mobs or []:
            try:
                _mx, _my = _m["position"]
                _mw, _mh = _m["size"]
            except (KeyError, TypeError):
                continue
            _cx, _cy = _mx + _mw // 2, _my + _mh // 2
            if abs(_cy - _py) > _dy_tol:
                _n_dy_skip += 1
                continue                       # 跨层：交给路线，不横着硬走
            _cands.append((_cx, _cy, abs(_cx - _px) + abs(_cy - _py)))
        if _n_dy_skip and not _cands:
            # ★ P4 纯观测：这一帧"看见了但够不着"的账记在跨层上（不是没看见）
            self._budget_note_lost("高差(帧)")
        if not _cands:
            return None, None, None
        # ── ① 出现确认：**证据累积（N-of-M）** —— `approach_confirm_s` 秒窗口内
        #    **累计**看见 ≥2 次采样才算数（不再要求"两次紧挨着"）──────────────
        #    ⚠️ 2026-10-14（用户 m10830「把'人眼的实时反应'拆解成'适合低频检测的
        #       确认流程'」）把老口径"连续两次**紧邻**采样"升级成"窗口内两次"：
        #       老口径在**闪烁**（看见 / 丢一帧 / 又看见）时会永远凑不齐 —— 真机
        #       `MSBot_2026-10-14_17-40-51.log` 段11 里 `[没出招·归因]` 有 113 条
        #       （15%）就是"框外有目标但确认没凑齐"，其中绝大多数是"每一次新出现
        #       的首帧都算 pending"和"丢一帧就把计数清零"。人眼不会因为眨了一下眼
        #       就把刚看见的怪从记忆里删掉 —— 但也不能只凭一帧就为幻影目标横着走
        #       （那正是老口径要治的病），所以保留"至少两次证据"这条底线。
        #    ⚠️ `approach_confirm_s: 0`（或负）= **关掉**这道防抖，逐字退回老口径
        #       「看见一帧就追」—— 与 `target_hold_s: 0` 的约定一致（0 = 关）。
        #       所以 ① 整段必须包在 `if _confirm_s > 0` 里，不能只靠"窗口为 0"
        #       去凑：第一帧的证据天生只有 1 条，那样"关掉"也会少追一帧。
        if _confirm_s > 0.0:
            if _now - float(getattr(self, "_approach_count_t", 0.0) or 0.0) > 0.001:
                self._approach_count_t = _now
                _prev_n = int(getattr(self, "_approach_n", 0))
                _ts = [float(_t) for _t in (getattr(self, "_approach_seen_ts", None) or [])
                       if _now - float(_t) <= _confirm_s]
                _ts.append(_now)
                self._approach_seen_ts = _ts
                self._approach_n = len(_ts)          # 只读用途：日志/自检能看出凑了几条
                self._approach_seen_t = _now
                # ★ P4 纯观测（2026-10-14，用户 m10830）：量"看见 → 确认"这一段。
                try:
                    if _prev_n == 0:
                        self._budget_vis_t0 = _now        # 新的一段"看见可追目标"
                    if _prev_n < 2 <= len(_ts):
                        _v0 = getattr(self, "_budget_vis_t0", None)
                        if _v0:
                            self._budget_see2ok = list(
                                getattr(self, "_budget_see2ok", None) or []) + [_now - float(_v0)]
                        self._budget_vis_t0 = None
                        self._budget_confirm_t = _now     # 下一段"确认 → 进框"的起点
                except Exception:                             # noqa: BLE001
                    pass
            if int(getattr(self, "_approach_n", 0)) < 2:
                return None, None, None        # 只认到一帧 —— 可能是幻影，等下次采样
        _cx, _cy, _dist = min(_cands, key=lambda _c: _c[2])
        _dir = "right" if _cx >= _px else "left"
        # ── ② 方向最小保持：老方向那侧还有怪就不许掉头（治左右抖动）──────────
        _old = getattr(self, "_approach_dir", None)
        _old_t = float(getattr(self, "_approach_dir_t", 0.0) or 0.0)
        if _old and _dir != _old and _now - _old_t < _dir_min_s:
            _same = [_c for _c in _cands if ((_c[0] >= _px) == (_old == "right"))]
            if _same:
                _dir = _old
                _cx, _cy, _dist = min(_same, key=lambda _c: _c[2])
        if _old != _dir:
            self._approach_dir = _dir
            self._approach_dir_t = _now
        return _dir, int(_dist), int(_cy - _py)

    def get_nearest_monster(self, is_left=True):
        '''
        Finds the nearest monster within the player's attack range.

        This function:
        - Defines an attack box relative to the player position,
            depending on the facing direction (`is_left`).
        - Iterates through all detected monsters and checks which ones overlap
          with the attack box.
        - Returns the closest valid monster that meets the overlap criteria.

        Args:
            is_left (bool): If True, assume the player is facing left;
                            adjusts attack box accordingly.
        Returns:
            dict or None: The nearest monster's info dict, or None if no valid match.
        '''

        x0, y0, x1, y1 = self.get_attack_range(is_left=is_left)

        nearest_monster = None
        min_distance = float('inf')
        for monster in self.monsters:
            mx1, my1 = monster["position"]
            mw, mh = monster["size"]
            mx2 = mx1 + mw
            my2 = my1 + mh

            # Calculate intersection
            ix1 = max(x0, mx1)
            iy1 = max(y0, my1)
            ix2 = min(x1, mx2)
            iy2 = min(y1, my2)

            iw = max(0, ix2 - ix1)
            ih = max(0, iy2 - iy1)
            inter_area = iw * ih

            # 交叠面积下限：与 `_mobs_in_attack_box` 共用同一个 helper
            # （原来是就地算一遍：空模板退回 0，避免对空序列 min() 抛 ValueError。
            #   抽出去是为了两处口径永不漂移 —— "改一处漏一处"是本项目的常见故障形态）
            inter_area_thres = self._min_mob_area()
            if inter_area >= inter_area_thres:
                # Compute distance to player center
                monster_center = (mx1 + mw // 2, my1 + mh // 2)
                dx = monster_center[0] - self.loc_player[0]
                dy = monster_center[1] - self.loc_player[1]
                distance = abs(dx) + abs(dy)  # Manhattan distance

                if distance < min_distance:
                    min_distance = distance
                    nearest_monster = monster

        return nearest_monster

    # ── 怪物模板的「前景色校验闸」（2026-10-13 加）─────────────────────────────
    # 为什么要这一闸：
    #   contour_only 模式**只看黑色轮廓**（`np.all(img == [0,0,0])`），把怪的颜色
    #   信息整个丢掉了。实测（2026-10-13，沼泽地2 真帧，画面里**没有**鳄鱼）：
    #   diff_thres=0.4 下检出 **17 个框，全部落在平台植被上**（分数 0.359~0.3997），
    #   而正确结果应当是 0 个 —— 植被的黑色轮廓密度/走向和怪足够像，轮廓分数分不开。
    # 判据：
    #   拿模板里**真正的怪像素**（前景 = 不是绿幕 (0,255,0) 的像素；绿幕是本项目
    #   截模板工具画的背景色）逐点比对画面同位置的像素：
    #   逐通道最大差 <= verify_color_tol 的前景像素占比 >= verify_min_ratio 才算命中。
    # 标定（同一批真帧 + 把真模板贴到平台上的合成"真怪"）：
    #   假框通过率 0.33~0.63（前景色差中位数 34）；合成真怪通过率 1.00（色差 0）
    #   ⇒ 默认 tol=30 / ratio=0.75 挡掉全部 17 个假框，真怪一个不少。
    # ⚠️ 前景 = 不是纯绿 (0,255,0) 的像素。本工具截的模板用绿幕当透明底，
    #    所以前景正好是"怪自己的像素"；没有绿幕的合成/旧模板，前景就退化成
    #    "全部像素"，等价于逐像素色校验 —— 只要画面里原样贴着这张模板就照样通过
    #    （既有自检的合成夹具就是这么造的，行为不变）。
    @staticmethod
    def _mob_scales(md_cfg):
        '''解析 `monster_detect.scales`（多尺度模板匹配）。

        · 默认 `[1.0]` ⇒ 与"没有这个键"完全一致（调用方对 1.0 不做 resize）。
        · 官方素材与游戏内渲染尺寸对不上时配多个比例
          （参考项目 zhoufanglu/maplestory-auto-ds 的推荐值 `[0.7, 0.85, 1.0, 1.15]`）。
        · ⚠️ 代价 ≈ 匹配耗时 × 尺度数（2026-10-13 真机标定：单尺度 66.7~69.6 ms/帧）。
        · fail-closed：非法值逐条丢掉 + warning；**全丢光回退 `[1.0]`**
          （宁可退化成老行为，也不让整套模板因为一个键写错而加载不出来）。
        '''
        raw = (md_cfg or {}).get("scales") or [1.0]
        if isinstance(raw, (int, float)) or not isinstance(raw, (list, tuple)):
            logger.warning(f"[怪物] monster_detect.scales 必须是列表，收到 {raw!r} ⇒ 回退 [1.0]")
            return [1.0]
        out = []
        for s in raw:
            try:
                s = float(s)
            except (TypeError, ValueError):
                logger.warning(f"[怪物] scales 里的 {s!r} 不是数字，已忽略")
                continue
            if not (0.0 < s <= 10.0):
                logger.warning(f"[怪物] scales 里的 {s} 超出 (0, 10] 范围，已忽略")
                continue
            if s not in out:
                out.append(s)
        if not out:
            logger.warning("[怪物] scales 里没有一个可用值 ⇒ 回退 [1.0]（单尺度）")
            return [1.0]
        return sorted(out)

    @staticmethod
    def _mob_template_variants(img, scales):
        '''一张模板 → 每个尺度 + 左右镜像的 `(图, 前景掩码)` 列表。

        · `scale == 1.0` **不做 resize**（保证默认配置下与"没有多尺度"逐像素一致）。
        · 缩放用 `INTER_NEAREST`：contour_only 的输入是硬边黑白轮廓，
          线性插值会糊掉边界（参考项目同款做法）。
        · 掩码口径只有一处：`get_mask(img, (0, 255, 0))`（= 绿幕以外都是前景）。
        '''
        out = []
        h, w = img.shape[:2]
        for s in scales:
            if s == 1.0:
                v = img
            else:
                v = cv2.resize(img, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                               interpolation=cv2.INTER_NEAREST)
            out.append((v, get_mask(v, (0, 255, 0))))
            v_flip = cv2.flip(v, 1)
            out.append((v_flip, get_mask(v_flip, (0, 255, 0))))
        return out

    @staticmethod
    def _mob_hit_points(res, diff_thres, cap=50):
        '''模板匹配结果里 `<= diff_thres` 的命中点 → 至多 `cap` 个，按分数从好到坏。

        ⚠️ 为什么要封顶（2026-10-13 借参考项目 maplestory-auto-ds 的 50 上限）：
            `TM_SQDIFF_NORMED` 在**平坦画面**（全黑帧、大片同色地面）上会让成千上万个
            位置都满足阈值。原实现把它们全部收进列表，再逐点过颜色校验闸、最后做
            O(n²) 的 NMS —— 一帧就能把界面卡死，而症状是"卡住了"、不是报错。
        返回：(x, y) 的迭代器（顺序与原来的 `zip(*np.where(...)[::-1])` 一致）。
        '''
        ys, xs = np.where(res <= diff_thres)
        if len(xs) <= cap:
            return zip(xs.tolist(), ys.tolist())
        best = np.argsort(res[ys, xs])[:cap]      # SQDIFF：越小越像
        return zip(xs[best].tolist(), ys[best].tolist())

    # ⚠️ 形状对不上、前景为空一律**放行**：这里宁可能多认，也不静默漏怪。
    @staticmethod
    def _mob_foreground_mask(templ):
        '''模板的「前景」掩码 = 不是纯绿 (0,255,0) 的像素（见上面的说明）。

        返回 None 只在"整张模板都是绿幕"（空模板）时发生 ⇒ 调用方不做色校验。
        '''
        # 与模板加载同口径：`tools/verify_monster.py:67` 也是 `get_mask(img, (0,255,0))`
        # ⇒ "什么算透明"只有一处定义（src/utils/common.py:575），不再各写一份。
        fg = get_mask(templ, (0, 255, 0)).astype(bool)
        return fg if bool(fg.any()) else None

    @staticmethod
    def _mob_color_ok(patch, templ, fg, tol, min_ratio):
        '''画面候选框 patch 与模板 templ 的前景色是否足够一致（见上面的说明）。'''
        if patch.size == 0 or patch.shape[:2] != templ.shape[:2]:
            return True
        n = int(fg.sum())
        if n == 0:
            return True
        diff = np.abs(patch.astype(np.int16) - templ.astype(np.int16)).max(axis=2)
        return float((diff[fg] <= tol).mean()) >= min_ratio

    def get_monsters_in_range(self, top_left, bottom_right):
        '''
        get_monsters_in_range
        '''
        x0, y0 = top_left
        x1, y1 = bottom_right

        img_roi = self.img_frame[y0:y1, x0:x1]

        # Shift player's location into ROI coordinate system
        px, py = self.loc_player
        px_in_roi = px - x0
        py_in_roi = py - y0

        # Define rectangle range around player (in ROI coordinate)
        char_x_min = max(0, px_in_roi - self.cfg["character"]["width"] // 2)
        char_x_max = min(img_roi.shape[1], px_in_roi + self.cfg["character"]["width"] // 2)
        char_y_min = max(0, py_in_roi - self.cfg["character"]["height"] // 2)
        char_y_max = min(img_roi.shape[0], py_in_roi + self.cfg["character"]["height"] // 2)

        monsters = []
        # 前景色校验闸的参数（2026-10-13 加，判据与标定见 _mob_color_ok 的说明）
        # ⚠️ 一律 .get(...) 取：自检脚本里的桩 cfg 往往只写少数几个键（见
        #    tools/verify_mob_boxes.py / verify_two_side_mob_attack.py），
        #    直接下标会 KeyError 把既有自检全带崩。
        _md_cfg = self.cfg.get("monster_detect") or {}
        _verify_color = bool(_md_cfg.get("verify_color", True))
        _color_tol = int(_md_cfg.get("verify_color_tol", 30))
        _min_ratio = float(_md_cfg.get("verify_min_ratio", 0.75))
        # ── contour_only 模式：ROI 侧的预处理**整帧只做一次**（2026-09-14 优化）──
        # ⚠️ 原来这几行放在下面的模板循环里 → 同一张 ROI 被重复处理 N 遍
        #    （N = 模板数量，本图 4 种怪共约 20 张）。实测怪检测一帧要 ~490ms，
        #    这部分重复计算占了不小一块。
        #    而它只跟「当前画面 + 角色位置」有关、跟具体用哪个模板**完全无关**
        #    → 提出来是**纯赚**：结果一模一样，只是少算十几遍。
        _mode = self.cfg["monster_detect"]["mode"]
        _blur = self.cfg["monster_detect"]["contour_blur"]
        _roi_blur = None
        if _mode == "contour_only":
            _mask_roi = np.all(img_roi == [0, 0, 0], axis=2).astype(np.uint8) * 255
            # Zero out mask inside this region (ignore player's own character)
            _mask_roi[char_y_min:char_y_max, char_x_min:char_x_max] = 0
            _roi_blur = cv2.GaussianBlur(_mask_roi, (_blur, _blur), 0)

        for monster_name, monster_imgs in self.monsters_info.items():
            for img_monster, mask_monster in monster_imgs:
                if _mode == "contour_only":
                    # Use only black lines contour to detect monsters
                    # Create masks (already grayscale)
                    mask_pattern = np.all(img_monster == [0, 0, 0], axis=2).astype(np.uint8) * 255

                    # Apply Gaussian blur (soften the masks)
                    img_monster_blur = cv2.GaussianBlur(mask_pattern, (_blur, _blur), 0)
                    # ROI 侧**不再重算** —— 直接复用上面那一份（见上面的说明）
                    img_roi_blur = _roi_blur

                    # Check template vs ROI size before matching
                    h_roi, w_roi = img_roi_blur.shape[:2]
                    h_temp, w_temp = img_monster_blur.shape[:2]

                    if h_temp > h_roi or w_temp > w_roi:
                        # ⚠️★ 2026-10-13 修：原来这里是 `return []` —— 一张比搜索框大的
                        #    模板会让**整帧**的怪检测当场作废：已经找到的怪全被丢掉，
                        #    后面还没跑的模板也一个都不跑。而"模板比搜索框大"只是这一张
                        #    不能用而已（攻击范围小、或怪贴着搜索框边界时很常见）。
                        #    这又是一处**静默**失效：日志只字不提，用户只看到"怪认不出来"。
                        logger.debug(
                            f"[怪物] 模板 {monster_name} {w_temp}x{h_temp} 比搜索框 "
                            f"{w_roi}x{h_roi} 大，跳过这一张（不影响其它模板）")
                        continue

                    # 太小的模板（<10px）直接跳过：匹配没意义，多半是坏素材
                    # （判据借参考项目 maplestory-auto-ds 的 `tw<10 or th<10`）
                    if h_temp < 10 or w_temp < 10:
                        logger.debug(f"[怪物] 模板 {monster_name} {w_temp}x{h_temp} 太小"
                                     f"（<10px），跳过这一张")
                        continue

                    # Perform template matching
                    try:
                        res = cv2.matchTemplate(img_roi_blur, img_monster_blur,
                                                cv2.TM_SQDIFF_NORMED)
                    except cv2.error as e:
                        # 单张模板匹配失败不该作废整帧（不静默：debug 日志带原因）
                        logger.debug(f"[怪物] 模板 {monster_name} 匹配失败，跳过这一张：{e}")
                        continue

                    # Apply soft threshold
                    match_locations = self._mob_hit_points(
                        res, self.cfg["monster_detect"]["diff_thres"])

                    h, w = img_monster.shape[:2]
                    _fg = self._mob_foreground_mask(img_monster) if _verify_color else None
                    for pt in match_locations:
                        if _fg is not None and not self._mob_color_ok(
                                img_roi[pt[1]:pt[1] + h, pt[0]:pt[0] + w],
                                img_monster, _fg, _color_tol, _min_ratio):
                            continue
                        monsters.append({
                            "name": monster_name,
                            "position": (pt[0] + x0, pt[1] + y0),
                            # ⚠️ (宽, 高)——**不是** (高, 宽)：全仓库 11 处消费点
                            #    （`mw, mh = monster["size"]`、绿框绘制、锁定中心
                            #    `position[0] + size[0]//2`）都按 (宽, 高) 读。
                            #    2026-10-14 修：这里原来写 `(h, w)`，等于把每个怪的
                            #    判定框宽高**对调**（青蛇 31h×28w ⇒ 横向多 3px、
                            #    纵向少 3px），"够得着"的判定与画出来的绿框都跟着偏。
                            "size": (w, h),
                            "score": res[pt[1], pt[0]],
                        })
                elif self.cfg["monster_detect"]["mode"] == "grayscale":
                    # 尺寸闸（2026-10-13 补）：模板比 ROI 大时 cv2.matchTemplate 会**抛异常**，
                    #   而 grayscale/color 这两个分支原来**没有**这道闸（只有 contour_only 有）
                    #   ⇒ 一张大模板足以让整帧怪检测崩掉。
                    _h_roi, _w_roi = img_roi.shape[:2]
                    _h_temp, _w_temp = img_monster.shape[:2]
                    if _h_temp > _h_roi or _w_temp > _w_roi:
                        logger.debug(
                            f"[怪物] 模板 {monster_name} {_w_temp}x{_h_temp} 比搜索框 "
                            f"{_w_roi}x{_h_roi} 大，跳过这一张（不影响其它模板）")
                        continue
                    if _h_temp < 10 or _w_temp < 10:
                        logger.debug(f"[怪物] 模板 {monster_name} {_w_temp}x{_h_temp} 太小"
                                     f"（<10px），跳过这一张")
                        continue
                    img_monster_gray = cv2.cvtColor(img_monster, cv2.COLOR_BGR2GRAY)
                    img_roi_gray = cv2.cvtColor(img_roi, cv2.COLOR_BGR2GRAY)
                    try:
                        res = cv2.matchTemplate(
                                img_roi_gray,
                                img_monster_gray,
                                cv2.TM_SQDIFF_NORMED,
                                mask=mask_monster)
                    except cv2.error as e:
                        logger.debug(f"[怪物] 模板 {monster_name} 匹配失败，跳过这一张：{e}")
                        continue
                    match_locations = self._mob_hit_points(
                        res, self.cfg["monster_detect"]["diff_thres"])
                    h, w = img_monster.shape[:2]
                    _fg = self._mob_foreground_mask(img_monster) if _verify_color else None
                    for pt in match_locations:
                        if _fg is not None and not self._mob_color_ok(
                                img_roi[pt[1]:pt[1] + h, pt[0]:pt[0] + w],
                                img_monster, _fg, _color_tol, _min_ratio):
                            continue
                        monsters.append({
                            "name": monster_name,
                            "position": (pt[0] + x0, pt[1] + y0),
                            # ⚠️ (宽, 高)——**不是** (高, 宽)：全仓库 11 处消费点
                            #    （`mw, mh = monster["size"]`、绿框绘制、锁定中心
                            #    `position[0] + size[0]//2`）都按 (宽, 高) 读。
                            #    2026-10-14 修：这里原来写 `(h, w)`，等于把每个怪的
                            #    判定框宽高**对调**（青蛇 31h×28w ⇒ 横向多 3px、
                            #    纵向少 3px），"够得着"的判定与画出来的绿框都跟着偏。
                            "size": (w, h),
                            "score": res[pt[1], pt[0]],
                    })
                elif self.cfg["monster_detect"]["mode"] == "color":
                    # 尺寸闸同上（color 分支原来也没有）
                    _h_roi, _w_roi = img_roi.shape[:2]
                    _h_temp, _w_temp = img_monster.shape[:2]
                    if _h_temp > _h_roi or _w_temp > _w_roi:
                        logger.debug(
                            f"[怪物] 模板 {monster_name} {_w_temp}x{_h_temp} 比搜索框 "
                            f"{_w_roi}x{_h_roi} 大，跳过这一张（不影响其它模板）")
                        continue
                    if _h_temp < 10 or _w_temp < 10:
                        logger.debug(f"[怪物] 模板 {monster_name} {_w_temp}x{_h_temp} 太小"
                                     f"（<10px），跳过这一张")
                        continue
                    try:
                        res = cv2.matchTemplate(
                                img_roi,
                                img_monster,
                                cv2.TM_SQDIFF_NORMED,
                                mask=mask_monster)
                    except cv2.error as e:
                        logger.debug(f"[怪物] 模板 {monster_name} 匹配失败，跳过这一张：{e}")
                        continue
                    match_locations = self._mob_hit_points(
                        res, self.cfg["monster_detect"]["diff_thres"])
                    h, w = img_monster.shape[:2]
                    _fg = self._mob_foreground_mask(img_monster) if _verify_color else None
                    for pt in match_locations:
                        if _fg is not None and not self._mob_color_ok(
                                img_roi[pt[1]:pt[1] + h, pt[0]:pt[0] + w],
                                img_monster, _fg, _color_tol, _min_ratio):
                            continue
                        monsters.append({
                            "name": monster_name,
                            "position": (pt[0] + x0, pt[1] + y0),
                            # ⚠️ (宽, 高)——**不是** (高, 宽)：全仓库 11 处消费点
                            #    （`mw, mh = monster["size"]`、绿框绘制、锁定中心
                            #    `position[0] + size[0]//2`）都按 (宽, 高) 读。
                            #    2026-10-14 修：这里原来写 `(h, w)`，等于把每个怪的
                            #    判定框宽高**对调**（青蛇 31h×28w ⇒ 横向多 3px、
                            #    纵向少 3px），"够得着"的判定与画出来的绿框都跟着偏。
                            "size": (w, h),
                            "score": res[pt[1], pt[0]],
                    })
                else:
                    logger.error(f"Unexpected camera localization mode: {self.cfg['monster_detect']['mode']}")
                    return []

        # Apply Non-Maximum Suppression to monster detection
        monsters = nms(monsters, iou_threshold=0.4)

        # Detect monster via health bar
        if self.cfg["monster_detect"]["with_enemy_hp_bar"]:
            # Create color mask for Monsters' HP bar
            mask = cv2.inRange(img_roi,
                               np.array(self.cfg["monster_detect"]["hp_bar_color"]),
                               np.array(self.cfg["monster_detect"]["hp_bar_color"]))

            # Find connected components (each cluster of green pixels)
            num_labels, labels, stats, centroids = \
                cv2.connectedComponentsWithStats(mask, connectivity=8)

            for i in range(1, num_labels):  # skip background (label 0)
                x, y, w, h, area = stats[i]
                if area < 3:  # small noise filter
                    continue

                # Guess a monster bounding box
                y += 10
                x = max(0, x)
                y = max(0, y)
                w = 70
                # 原来这里直接 min() 所有模板的高度 —— 一个模板都没有时会抛 ValueError。
                # 改成：有模板就取最矮的那个当高度，没有就退回到经验值，
                # 这样「只用血条、不做模板」的用法也能跑起来。
                all_h = [img.shape[0] for _, imgs in self.monsters_info.items()
                         for img, _ in imgs]
                h = min(all_h) if all_h else 100

                monsters.append({
                    "name": "Health Bar",
                    "position": (x0 + x, y0 + y),
                    "size": (h, w),
                    "score": 1.0,
                })

        # ── 索敌范围框 + 逐怪框都**不在**这里画了（2026-09-29）────────────
        # 两个框都已挪到帧尾的 update_info_on_img_frame_debug，**每帧重画**。
        # 原因：本函数被**降频**调用（every_n_frames 默认 2，见 monster_detect_interval），
        #      而 img_frame_debug 在 run_once 开头**每帧都被清空重建**
        #      ⇒ 不画的那帧框整个消失 = 用户看到的「疯狂闪烁」。
        # ⚠️ 别挪回来！自检 tools/verify_mob_boxes.py 的 [4b]/[6] 会立刻报错。
        return monsters

    def get_img_frame(self):
        '''
        get_img_frame

        把抓到的原始帧裁成「游戏客户区」，并**保证帧的像素坐标系 == 客户区坐标系**。
        这一条是全部坐标（模板、ui_coords、路线图、点击）能对上的前提。

        ── 抓帧口径（2026-09-10 实测确认，见 tools/measure_window.py）────────────
        `windows_capture` 抓**窗口**时，帧里是**含标题栏和 1px 窗口边框**的：
          帧宽 = 客户区宽 + 左右各 1px
          帧高 = 客户区高 + 标题栏高 + 底部 1px
        实测（800x600 客户区的对照窗口）：帧 802x632，标题栏 31px，内容从 (1, 31) 开始。

        ── 裁剪规则 ──────────────────────────────────────────────────────────
        1. 切掉顶部 `title_bar_height` 行 → 帧内容左上角 == 客户区左上角
        2. 水平方向按「左右边框对称」取左偏移 `(帧宽 - 目标宽) // 2`，锚在左上角裁到目标尺寸

        为什么不用原来的「居中裁」：居中裁在多余像素左右/上下不对称时会整体偏移
        （底部多余 1px 时 y 偏移 0 是巧合，多余 2px 就会偏 1px）。锚定左上角则
        天然只丢弃底部/右侧的边框像素，不会动到内容。
        config: game_window.size = [height, width]
        '''
        # Get window game raw frame
        self.frame = self.capture.get_frame()
        if self.frame is None:
            # ★ 2026-09-27 修（issue #2 排查中发现）：
            #   原来只有一句英文 "Failed to capture game frame."，且**每帧都打**
            #   （主循环 30fps ⇒ 每秒 30 行），既看不懂又把日志冲爆。
            #   现在：① 说人话 + 给出最可能的原因与怎么办；
            #         ② 按时间节流（每 10 秒最多一条），把日志留给有用信息。
            now = time.time()
            if now - getattr(self, "_t_last_noframe_warn", 0.0) > 10:
                self._t_last_noframe_warn = now
                logger.error(
                    "[抓帧] 拿不到游戏画面（这一帧没有画面数据）——\n"
                    "        角色不会动、也不会打怪，因为工具「看不见」游戏。\n"
                    "        最常见原因（按顺序查）：\n"
                    "          ① 游戏窗口被**最小化**了（挡住没事，最小化不行）；\n"
                    "          ② 游戏窗口被切到了**另一个虚拟桌面**（Win+Tab 看看）；\n"
                    "          ③ 游戏刚被关闭 / 还在加载。\n"
                    "        处理：把游戏窗口还原到当前桌面，工具会自动恢复。\n"
                    "        （本条提示 10 秒内最多出现一次，不刷屏）")
            return

        raw_h, raw_w = self.frame.shape[:2]
        # ★ 2026-09-29（issue #14）：标题栏高度改为**自适应取值**。
        #   配置里的 31 只是开发机实测常数，标题栏高度是系统按 主题/DPI/build 算的
        #   （实测有用户机器为 27）。切多了会让帧高不足 → 返回 None → 完全抓不到画面。
        #   取值规则与安全闸见 common.resolve_title_bar_height 的文档。
        target_h, target_w = self.cfg["game_window"]["size"]   # [height, width]
        title_bar = resolve_title_bar_height(
            self.cfg, window_title=getattr(self.capture, "window_title", None),
            tag="帧几何")

        frame_cropped, msg = crop_frame_to_client(
            self.frame, (target_h, target_w), title_bar, tag="引擎")

        self._verify_frame_geometry_once(raw_w, raw_h, title_bar, msg)

        if frame_cropped is None:
            logger.error(f"[get_img_frame] {msg}")
            return None

        return frame_cropped

    def _verify_frame_geometry_once(self, raw_w, raw_h, title_bar, crop_msg):
        '''
        第一帧到手后，把「抓帧几何」和「窗口实测客户区」交叉核对一次并留日志。

        背景：config 的 game_window.size 一旦和真实窗口不一致，所有坐标都会失准，
        而且症状是「看不出来」（画面正常，只是打得偏）。所以在第一帧就把尺寸摊开对照，
        让问题一眼可见，而不是等跑起来打不着怪再回头查。
        '''
        if getattr(self, "_frame_geom_logged", False):
            return
        self._frame_geom_logged = True

        target_h, target_w = self.cfg["game_window"]["size"]
        logger.info(f"[帧几何] {crop_msg}")

        # 与窗口实测客户区对照
        client = None
        try:
            client = get_window_client_size(self.capture.window_title)
        except Exception as e:
            logger.debug(f"[帧几何] 取窗口客户区失败：{e}")

        if client is None:
            logger.warning("[帧几何] 取不到窗口客户区尺寸，无法交叉核对。"
                           "请确认游戏窗口还在（没被最小化/关闭）。")
            return

        client_w, client_h = client
        if (client_w, client_h) == (target_w, target_h):
            logger.info(f"[帧几何] 【OK】窗口实测客户区 {client_w}x{client_h} 与配置一致，"
                        f"帧坐标 == 客户区坐标。")
        else:
            logger.error(
                f"[帧几何] 【失败】 窗口实测客户区 {client_w}x{client_h} 与配置的 "
                f"{target_w}x{target_h} 不一致 —— 所有坐标都会失准！\n"
                f"        修法：把游戏窗口调成 {target_w}x{target_h}（不含标题栏），"
                f"或运行 `python -m tools.measure_window --write-config` 按实测值改配置。")

    def is_player_stuck(self):
        """
        Checks whether the player is stuck (not moving)
        based on their global position on map.

        This function:
        - Compares the player's current position with their last known position
          tracked by the watchdog.
        - If the player has moved beyond a threshold (`watch_dog_range`),
          it resets the watchdog timer.
        - If the player hasn't moved and the elapsed time exceeds (`watch_dog_timeout`),
          it flags the player as stuck and resets the watchdog.

        Returns:
            bool: True if the player is stuck, False otherwise.
        """
        dx = abs(self.loc_player_global[0] - self.loc_watch_dog[0])
        dy = abs(self.loc_player_global[1] - self.loc_watch_dog[1])

        # ── 正在打怪 → 不算卡死（2026-09-12 加）──────────────────────────────
        # 打怪时角色本来就该站着不动（技能前摇 + 冷却），连续打同一只怪的位移
        # 经常 < watchdog.range(10px) → 10 秒后被误判卡死、抽随机动作乱走，
        # 把正在进行的战斗打断（表现：打得好好的突然乱跑）。
        #
        # ⚠️ 原来靠「站在安全点内豁免」兜住站桩打怪，但那是 stand 模式专属的；
        #    2026-09-12 删掉定点模式后，换成这条与模式无关的豁免。
        grace = self.cfg["watchdog"].get("attack_grace", 5)
        if time.time() - self.t_last_attack < grace:
            self.loc_watch_dog = self.loc_player_global
            self.t_watch_dog = time.time()
            return False

        # ── 窄平台降级：站着不动是**预期行为**，不是卡死（2026-09-15 加）──────
        # 见 route_too_narrow_for_patrol：这条路窄到没法巡逻，引擎主动不巡逻了。
        # ⚠️ 只豁免「**画面里没有认出来的怪**」的情况（用户 2026-09-15 明确：
        #    「附近有模板怪但是不打」= 真的卡死）。有怪却不打时**不豁免**，
        #    照样往下走 → 看门狗触发 → 脱困随机指令去救。
        if getattr(self, "_patrol_degraded", False) and not getattr(self, "monsters", None):
            self.loc_watch_dog = self.loc_player_global
            self.t_watch_dog = time.time()
            return False

        # ── 路线卡在够不着的**终点**上 → 不算「角色卡死」，算「路线/录制问题」（2026-09-28 加）
        # 背景（issue #6 真机现场，废都南方工地）：route1 的终点 goal 在 **y=90~94 的高台**上，
        #   角色在地面 y=116 来回走，**永远上不去**。此时：
        #     · 位置每帧都在变 → `dx+dy > range` 频繁成立 → 看门狗**不会**报卡死；
        #     · 但它其实已经空转很久（用户日志：3 分钟只在同一条地面线上往返 4 趟）。
        #   ⇒ 用户看到的是「一直在走、但不跳也不打怪」，而系统一声不吭。
        #   这里补一条**只告警、不改行为**的判据：同一段内连续 N 帧没能切段、
        #   且角色 y 始终没接近本段终点 y ⇒ 打一条说明"终点够不着"的日志。
        #   ⚠️ 为什么只告警不脱困：脱困会给随机动作，而这类地形的真因是
        #      **路线画错了**（终点画在跳不上去的高度 / 缺跳跃像素），
        #      乱动只会把角色踹下平台 —— 与 `on_stuck: alert` 的既定取舍一致。
        self._warn_unreachable_goal()

        current_time = time.time()

        current_time = time.time()
        if dx + dy > self.cfg["watchdog"]["range"]:
            # Player moved, reset watchdog timer
            self.loc_watch_dog = self.loc_player_global
            self.t_watch_dog = current_time
            return False

        dt = current_time - self.t_watch_dog
        if dt > self.cfg["watchdog"]["timeout"]:
            # watch dog idle for too long, player stuck
            self.loc_watch_dog = self.loc_player_global
            self.t_watch_dog = current_time
            # 记下已卡多少秒：`handle_stuck` 要把它写进提示窗（2026-09-15 加）
            self.stuck_seconds = round(dt, 2)
            logger.warning(f"[is_player_stuck] Player stuck for {self.stuck_seconds} seconds.")
            return True
        return False

    def _warn_unreachable_goal(self):
        '''本段终点**高度上够不着**时，打一条"路线画错了"的告警（**纯观测**）。

        治的是什么（issue #6，2026-09-28 真机现场）：
            用户在「废都南方工地」录的两段路线，段0 的 goal 画在 **y=90~94 的高台**上，
            而角色一直在地面 y=116 上往返 —— 它**从来没有爬上去过**。
            表现就是用户报的「不跳、不打怪、只在一条线上来回走」。

        为什么现有的看门狗抓不到：
            `is_player_stuck` 看的是"位置有没有变"。这里角色**一直在动**
            （每 3~5 秒就往返一趟），`dx+dy > range` 频繁成立 ⇒ 永不判卡死 ⇒ 静默空转。

        判据（三条同时满足才报）：
            ① 不在回正状态（回正有自己的日志）；
            ② 有本段终点，且**高度差始终没收敛**（连续帧数超过阈值）；
            ③ 期间发生过切段（说明确实在来回走，而不是真的站着不动）。
        ⇒ 报一条 error 级日志，**明确告诉用户是路线的问题、怎么修**，不做任何脱困动作。

        ⚠️ 纯观测：只改计数器与日志，不碰 cmd_* / idx_routes / 任何判定。
        ⚠️ 所有状态一律 getattr 兜底、复位也**内联**不调其它方法：
           自检的 Stub **不走 __init__**（既有纪律，踩过坑 —— 调 self._xxx() 会
           因为 Stub 上没绑该方法而 AttributeError，把整条自检带崩）。
        '''
        def _reset():
            self._unreach_frames = 0
            self._unreach_seg = getattr(self, "idx_routes", None)

        if getattr(self, "is_using_home_route", False):
            _reset()
            return
        goals = getattr(self, "_seg_goals", None) or []
        i = getattr(self, "idx_routes", 0)
        if not goals or not (0 <= i < len(goals)) or goals[i] is None:
            _reset()
            return
        gx, gy = goals[i]
        pos = getattr(self, "loc_player_global", None)
        if not pos:
            return
        # ⚠️ 读法与 route 其它处一致：**缺键 → 默认 3**；但显式填 0 是
        #    "关掉高度判据"（见 config_default.yaml 的 goal_reach_y_tol 说明），
        #    必须真的不告警 —— 所以**不能**写成 `... or 3`：
        #    那样 0 会被当成假值吞掉、回落 3，等于"想关关不掉"。
        #    （本自检的用例 5 正是钉这条，第一次就抓到了这个错。）
        _raw = self.cfg.get("route", {}).get("goal_reach_y_tol", 3)
        try:
            tol = 3 if _raw is None else int(_raw)
        except (TypeError, ValueError, AttributeError):
            tol = 3
        if tol <= 0:
            _reset()
            return                       # 关掉高度判据 = 这条告警也无从判起

        # 进入"够不着"的观察：|人y - 终点y| 一直大于容差
        if abs(int(pos[1]) - int(gy)) > tol:
            n = int(getattr(self, "_unreach_frames", 0) or 0) + 1
            self._unreach_frames = n
            seg = getattr(self, "_unreach_seg", None)
            if seg != i:
                # 换了段 → 重新开始观察（不同段的终点高度本来就不同）
                self._unreach_seg = i
                self._unreach_frames = 1
                n = 1
            # 阈值：按 30fps 算，约 60 秒还没够到就报（比 watchog.timeout 宽得多，
            # 因为爬梯 + 打怪 + 被撞飞本来就要花时间）
            if n == 180:
                logger.error(
                    f"[路线够不着] 本段(第 {i + 1}/{len(goals)} 段)的终点在 "
                    f"y={gy}，而角色一直在 y={int(pos[1])}（高度差 "
                    f"{abs(int(pos[1]) - int(gy))}px > 容差 {tol}px），"
                    f"已经连续 {n} 帧（约 {n // 30} 秒）没能靠近。\n"
                    f"        角色会一直在这条路上来回走、既不跳也不打怪 —— "
                    f"**这不是卡死，是这段路线画得够不着**。\n"
                    f"        怎么修（任选其一）：\n"
                    f"          ① 重录这一条路线：走到那个高台上再按 F6 收尾，"
                    f"让终点落在**人真站得到**的位置；\n"
                    f"          ② 检查从地面到高台的**跳跃/爬梯像素有没有录进去**"
                    f"（录制时跳一下才会记下跳跃点）；\n"
                    f"          ③ 该高台本来就不用去 → 重录时把终点画在地面这一层。")
        else:
            _reset()

    def handle_stuck(self):
        '''判定卡死后**做什么** —— 唯一入口（2026-09-15 加）。

        配置 `watchdog.on_stuck`（完整理由见 config_default.yaml）：
            random = 老的"抽随机动作"。实测两条坏处：
                     ① 窄平台上乱抽 = 直接掉下去（抽到 `right none jump`，右边正好是悬崖）；
                     ② 把真因**盖住** —— 随机跳把角色从 (100,122) 踹到 (100,118)，
                        症状从"在原地蹭"变成"跳到另一层"，日志上看"它动了"，更难查。
            alert  = ★默认：**不乱动**（保持本帧逻辑给的指令），
                     改为**弹置顶提示窗** + 醒目日志，让人来处理。
            none   = 什么都不做（只留 is_player_stuck 那条 WARNING）。

        ⚠️ 取值非法 / 缺键 / 读配置抛异常 → 一律回落 **alert**（宁可不动也别乱动）。
        ⚠️ alert 模式**故意不清指令**：清成 none 反而会让角色停下（本来可能只是在
           "朝一个方向顶着走不动"），而"顶着走"至少方向是逻辑算出来的、是安全的。
        '''
        try:
            mode = str(self.cfg.get("watchdog", {}).get("on_stuck", "alert") or "alert")
        except Exception:                                    # noqa: BLE001
            mode = "alert"
        mode = mode.strip().lower()
        if mode not in ("random", "alert", "none"):
            logger.warning(f"[卡死处理] watchdog.on_stuck 取值非法（{mode!r}）"
                           f"→ 按 alert 处理（不乱动）")
            mode = "alert"

        if mode == "random":
            self.update_cmd_by_random()
            return
        if mode == "none":
            return

        # ── alert：不乱动，只告警 ──────────────────────────────────────────
        try:
            cooldown = float(self.cfg.get("watchdog", {}).get("alert_cooldown", 60))
        except (TypeError, ValueError):
            cooldown = 60.0
        stuck_alert.notify_stuck(
            pos=getattr(self, "loc_player_global", None),
            seconds=getattr(self, "stuck_seconds", None),
            cmd=f"{self.cmd_move_x} {self.cmd_move_y} {self.cmd_action}",
            extra=(f"回正状态={bool(getattr(self, 'is_using_home_route', False))}"
                   f" / 窄平台降级={bool(getattr(self, '_patrol_degraded', False))}"
                   f" / 附近怪={len(getattr(self, 'monsters', None) or [])}"),
            cooldown=cooldown)

    def screenshot_img_frame(self):
        '''
        Save self.img_frame
        '''
        if self.img_frame is None:
            logger.error("[screenshot_img_frame] Failed, game window is not available")
        else:
            screenshot(self.img_frame, "img_frame")

        if self.img_frame_debug is None:
            pass
        else:
            screenshot(self.img_frame_debug, "img_frame_debug")

        if self.frame is None:
            pass
        else:
            screenshot(self.frame, "frame")

    def update_info_on_img_frame_debug(self):
        '''
        update_info_on_img_frame_debug
        '''
        # ── 2026-09-12 精简画面 ──────────────────────────────────────────
        # 这里原本有一组左下角文字（FPS / State / Resolution / 按键提示），现已去掉。
        # 现在画面上有**三种框**（2026-09-29 起）：
        #   红框 "攻击范围"   = 攻击范围（技能能打到的矩形）
        #   红框 "索敌范围"   = 怪物检测搜索区（引擎在这个矩形内找怪）★09-29 挪到这里
        #   绿/黄 逐怪小框     = 每只识别到的怪（绿=模板检出、黄=血条检出）★09-29 加回
        # 其余标注（Route Action / Route Index / Cmd / minimap 红框）已去掉。
        #
        # ★ 这三种框**都必须在本方法里画（帧尾、每帧都到）**，不能放进按帧降频的
        #   get_monsters_in_range —— 否则会「不画的那帧框消失」= 疯狂闪烁。
        #   详见下面各自的注释。
        #
        # ★ 2026-09-29 更正：这段注释原来写的是「怪物小框也一并去掉，保持画面干净」，
        #   但**用户从没要求删掉逐怪小框** —— 他当时只要求把两个框的英文标签改成中文。
        #   逐怪小框在初始提交 68118f7 里就已是注释状态，git 查不到任何删除记录。
        #   ⇒ 现已按 `monster_detect.draw_mob_boxes` 开关加回（**默认 True**，见
        #     get_monsters_in_range 里的实现）。不想要框的把它设成 false 即可。
        #
        # ⚠️ 这些运行信息并没有丢：日志里主循环每 5 秒一条 [运行状态]
        #    （帧号/小地图/全局坐标/段号/指令/怪数/名字命中），排查照旧。
        # 注：`self.fps` 仍在这里更新，其它地方/日志可能会读。
        self.fps = round(1.0 / (time.time() - self.t_last_frame))

        # Draw attack box on debug window
        if self.cfg["bot"]["attack"] == "aoe_skill":
            x0, y0, x1, y1 = self.get_attack_range()
            draw_rectangle(
                self.img_frame_debug, (x0, y0),
                (y1-y0, x1-x0),
                (0, 0, 255), "攻击框（打得到）蓝框=索敌框（只看不打）"
            )
        elif self.cfg["bot"]["attack"] == "directional":
            x0, y0, x1, y1 = self.get_attack_range(is_left=True)
            draw_rectangle(
                self.img_frame_debug, (x0, y0),
                (y1-y0, x1-x0),
                (0, 0, 255), "攻击框(左)蓝框=索敌框(只看不打)"
            )
            x0, y0, x1, y1 = self.get_attack_range(is_left=False)
            draw_rectangle(
                self.img_frame_debug, (x0, y0),
                (y1-y0, x1-x0),
                (0, 0, 255), "攻击框(右)"
            )

        # ── 逐怪画框（2026-09-29 加回，**默认开**）─────────────────────────────
        # 历史澄清：以前 get_monsters_in_range 里有句注释说「原来给每只怪画小框，
        # 按用户要求去掉」。用户 2026-09-29 明确澄清：**他当时只要求把两个框的
        # 英文标签改成中文，从没要求删掉逐怪小框**。那句注释是记错的
        # （git 里也查不到删除记录 —— 初始提交 68118f7 起就已是注释状态）。
        #
        # 两个作用：
        #   ① 用户肉眼判断「引擎到底看见没看见」，这是排查漏检的唯一抓手；
        #   ② 出问题时能一眼区分**漏检**（怪没框）还是**下游不打**（有框不出招）。
        #
        # ⚠️⚠️ 为什么画在**这里**（帧尾、每帧都到）而不是怪检测内部 ——
        #    这是治「框疯狂闪烁」的关键，别挪回去！
        #
        #    第一版把它放在 get_monsters_in_range 里（= 怪检测刚跑完那帧）。
        #    理由听着对（框和 self.monsters 同一批数据），但**结果是闪烁**：
        #    怪检测**降频**（every_n_frames 默认 2，见 monster_detect_interval），
        #    所以它每 2 帧才画一次；而 img_frame_debug 在 run_once 开头**每帧都被
        #    丢弃重建**（self.img_frame.copy()）⇒ 不画的那帧框整个消失。
        #
        #    ★ 实证反例（用户 2026-09-29 提问点破）：**「攻击范围框」从不闪烁**,
        #      它同样画在这张每帧重建的图上 —— 差别是它**每帧都重画**。
        #      ⇒ 结论：**「每帧清空」本身不导致闪烁**（攻击范围框就是活反例），
        #        真因是「**画框频率(每2帧) < 清空频率(每帧)**」。
        #        所以修法不是去改 img_frame_debug 的生命周期，而是**对齐画框频率**。
        #
        #    ⚠️ 副作用（已知并接受，用户 2026-09-29 确认"拖后一帧没事"）：
        #      降频下中间帧用的 self.monsters 是**上一次检测**的结果，所以框的
        #      位置会比怪**慢半拍**。滞后量级（真机实测帧率 1.8~2fps 反推）：
        #        最快 ~50ms（中间帧）／平均 ~300ms／最坏 ~600ms（两次检测之间）。
        #      排查漏检时看的是「怪身上**有没有**框」，不是「框跟得多准」，
        #      慢半拍不影响判断 —— 比整个框一闪一闪没法看强得多。
        #
        # ⚠️ 画在 self.img_frame_debug 上，**不碰 self.img_frame** —— 识别用的是
        #    后者，画框不会污染任何颜色/模板判据（见 run_once 里的同款注释）。
        if self.img_frame_debug is not None and \
                (self.cfg.get("monster_detect", {}) or {}).get("draw_mob_boxes", False):
            # getattr 兜底：离线用例里的 Stub 不走 __init__，可能没有这个属性
            for _mk in (getattr(self, "monsters", None) or []):
                try:
                    _mx, _my = _mk["position"]
                    _mw, _mh = _mk["size"]
                    # 尺寸防御：模板尺寸异常时不画（画出来是个巨型框，更迷惑）
                    if not (0 < _mw < self.img_frame_debug.shape[1] and
                            0 < _mh < self.img_frame_debug.shape[0]):
                        continue
                    _is_hp = (_mk.get("name") == "Health Bar")
                    # 黄 = 血条检出，绿 = 模板检出（沿用上游原版的配色约定）
                    _color = (0, 255, 255) if _is_hp else (0, 255, 0)
                    # 不写文字：中文标签要过 PIL（实测 ≈0.1ms/次），
                    # 误报多时一帧几十个框会明显拖帧。框本身（cv2.rectangle）
                    # 只改几十个像素，开销可忽略 —— 所以只画框、不写字。
                    cv2.rectangle(
                        self.img_frame_debug,
                        (max(0, _mx), max(0, _my)),
                        (max(0, _mx + _mw), max(0, _my + _mh)),
                        _color, 2)
                except Exception:                            # noqa: BLE001
                    # 画框是纯观测，绝不允许把主循环带崩（同 _perf 的纪律）
                    continue

        # ── 索敌范围框（2026-09-29 从怪检测内部挪到这里，治闪烁）──────────────
        # 治的是用户「识别框疯狂闪烁」——原来它画在 get_monsters_in_range 里，
        # 而那个函数**被降频调用**（every_n_frames 默认 2）⇒ 每 2 帧才画一次；
        # 但 img_frame_debug 每帧都被清空重建 ⇒ 不画的那帧框整个消失 = 闪。
        # ⇒ 挪到帧尾（每帧都到），与「攻击范围框」「逐怪框」同处，**每帧重画**。
        #
        # ⚠️ 这里**重算**一遍框，不从怪检测那边"传出来"：
        #    搜索框纯粹由 loc_player + 配置（range/margin）算出，是**纯函数**，
        #    没有随机性也没有跨帧状态（见 update_cmd_by_mob_detection 的算法）。
        #    所以重算的结果与怪检测那帧用的框**完全一致**，不必额外存字段
        #    （存字段反而多一处要维护的跨帧状态，容易忘清）。
        # ⚠️ 已知副作用（与逐怪框同性质、同样接受）：降频下中间帧 loc_player 可能
        #    滞后一帧，框会慢半拍（~50ms 最快／~300ms 平均／~600ms 最坏，见上）。
        #    但**不闪**远比"绝对实时"重要 —— 闪的框根本没法看。
        if self.img_frame_debug is not None and \
                (self.cfg.get("monster_detect", {}) or {}).get("draw_mob_boxes", False):
            try:
                # ⚠️★ 2026-10-13：改成调**怪检测用的同一个函数** get_search_box()。
                #    原因：本轮给索敌框加了 detect_range_scale（放大）与"位置丢了
                #    就放到整帧"，画框那边如果继续就地重算，就会画出一个**跟检测
                #    实际用的框不一样**的框 —— 用户按画面上那个框去判断"为什么没
                #    认出怪"，会被彻底带偏（比不画还糟）。纯函数性质从此由
                #    "同一份代码"保证，不再靠注释里的论证。
                _h_f, _w_f = self.img_frame_debug.shape[:2]
                _sx0, _sy0, _sx1, _sy1 = self.get_search_box()
                # 2026-10-14：**不给索敌框加中文标签** —— 用户分不清"哪个框才打得
                #   到"的问题改由**攻击框的标签**解决（见上面 "攻击框（打得到）
                #   蓝框=索敌框（只看不打）"）。理由：中文标签要走 PIL，每帧多一次
                #   图像往返会拖帧，这条由 tools/verify_mob_boxes.py 钉着。
                cv2.rectangle(self.img_frame_debug,
                              (_sx0, _sy0), (_sx1, _sy1), (255, 0, 0), 2)
            except Exception:                                # noqa: BLE001
                pass        # 纯观测，绝不允许把主循环带崩

        # 2026-09-12：原来这里会在游戏画面上画一个 "minimap" 红框标出小地图位置，
        # 按用户要求去掉（画面上只留 Attack Range + Mob Detection Box 两个框）。
        # self.loc_minimap / self.img_minimap 本身仍在用（坐标换算需要），只是不画框了。

        # ⚠️ 没有路线图时 img_route_debug 是 None（这张图还没录路线就会这样），
        #    下面直接 .shape 会 AttributeError，而 viz 一崩主循环就跟着崩 ——
        #    打开「游戏画面」页签反而把挂机搞停。这里提前收手。
        if self.img_route_debug is None:
            return

        # Compute crop region with boundary check
        crop_w, crop_h = 80, 80
        x0 = max(0, self.loc_player_global[0] - crop_w // 2)
        y0 = max(0, self.loc_player_global[1] - crop_h // 2)
        x1 = min(self.img_route_debug.shape[1], x0 + crop_w)
        y1 = min(self.img_route_debug.shape[0], y0 + crop_h)

        # Check if valid crop region
        if x1 <= x0 or y1 <= y0:
            return

        # Crop region
        mini_map_crop = self.img_route_debug[y0:y1, x0:x1]
        mini_map_crop = cv2.resize(mini_map_crop,
                                (int(mini_map_crop.shape[1] * 3),
                                 int(mini_map_crop.shape[0] * 3)),
                                interpolation=cv2.INTER_NEAREST)
        # Paste into top-right corner of self.img_frame_debug
        h_crop, w_crop = mini_map_crop.shape[:2]
        h_frame, w_frame = self.img_frame_debug.shape[:2]
        x_paste = w_frame - w_crop - 10  # 10px margin from right
        y_paste = 10
        self.img_frame_debug[y_paste:y_paste + h_crop, x_paste:x_paste + w_crop] = mini_map_crop

        # Draw border around minimap
        cv2.rectangle(
            self.img_frame_debug,
            (x_paste, y_paste),
            (x_paste + w_crop, y_paste + h_crop),
            color=(255, 255, 255),   # White border
            thickness=2
        )

        # 2026-09-12：原来这里会画一行 `Cmd: <指令>`，按用户要求去掉。
        # 想看当前指令时看日志的 [运行状态]（每 5 秒一条）即可。

    def update_img_frame_debug(self):
        '''
        update_img_frame_debug
        '''
        cv2.imshow("Game Window Debug",
            self.img_frame_debug[:self.cfg["ui_coords"]["ui_y_start"], :])
        # Update FPS timer
        self.t_last_frame = time.time()

    def terminate_threads(self):
        '''
        terminate all threads
        '''
        # Terminate keyboard controller
        if self.kb is not None:
            self.kb.is_terminated = True
        # Terminate game window capturor
        if self.capture is not None:
            self.capture.stop()
        # 示范录制收尾（2026-10-14 加）：关文件 + 停按键监听（纯观测，出错不影响退出）
        if getattr(self, "demo", None) is not None:
            try:
                self.demo.stop()
            except Exception as _e:                           # noqa: BLE001
                logger.warning(f"[示范录制] 停止时出错：{_e!r}")
        self.is_terminated = True
        logger.info(f"[terminate_threads] Terminated all threads")

    def get_attack_direction(self, monster_left, monster_right):
        '''
        get_attack_direction
        '''
        # Compute distance for left
        distance_left = float('inf')
        if monster_left is not None:
            mx, my = monster_left["position"]
            mw, mh = monster_left["size"]
            center_left = (mx + mw // 2, my + mh // 2)
            distance_left = abs(center_left[0] - self.loc_player[0]) + \
                            abs(center_left[1] - self.loc_player[1])
        # Compute distance for right
        distance_right = float('inf')
        if monster_right is not None:
            mx, my = monster_right["position"]
            mw, mh = monster_right["size"]
            center_right = (mx + mw // 2, my + mh // 2)
            distance_right = abs(center_right[0] - self.loc_player[0]) + \
                            abs(center_right[1] - self.loc_player[1])
        # Choose attack direction and nearest monster
        attack_direction = None
        # nearest_monster = None

        # Additional validation: check if monster is actually on the correct side
        def is_monster_on_correct_side(monster, direction):
            if monster is None:
                return False
            mx, my = monster["position"]
            mw, mh = monster["size"]
            monster_center_x = mx + mw // 2
            player_x = self.loc_player[0]

            if direction == "left":
                return monster_center_x < player_x  # Monster should be left of player
            else:  # direction == "right"
                return monster_center_x > player_x  # Monster should be right of player

        # Only choose direction if there's a clear winner and monster is on correct side
        if monster_left is not None and monster_right is None and \
            is_monster_on_correct_side(monster_left, "left"):
            attack_direction = "left"
            # nearest_monster = monster_left
        elif monster_right is not None and monster_left is None and \
            is_monster_on_correct_side(monster_right, "right"):
            attack_direction = "right"
            # nearest_monster = monster_right
        elif monster_left is not None and monster_right is not None:
            # Both sides have monsters, check distance and side validation
            left_valid = is_monster_on_correct_side(monster_left, "left")
            right_valid = is_monster_on_correct_side(monster_right, "right")

            if left_valid and not right_valid:
                attack_direction = "left"
                # nearest_monster = monster_left
            elif right_valid and not left_valid:
                attack_direction = "right"
                # nearest_monster = monster_right
            elif left_valid and right_valid:
                # ── 两侧都有**合法**目标 → 选近的，不再"两边都不打"（2026-09-29 修）──
                #
                # 【改的是什么】原实现在这里要求「距离差 > 50px」才动手，
                #   差距不到 50px 就返回 None（原注释：to avoid confusion）。
                #   结果是**两侧都有怪反而比只有一侧更差**：一侧有怪必打，
                #   两侧都有怪却一次都不出招。
                #
                # 【为什么是 bug 不是取舍】issue #12 用户原话实录：
                #   「左右同时存在怪物时，角色在原地左右抽搐，但是不攻击」
                #   —— 它不是"偶尔犹豫"，而是**持续不出招**；而"原地抽搐"
                #   正是本函数返回 None 之后、由路线指令来回拉扯造成的观感。
                #   更关键的是：这个函数存在的意义就是"该打哪边"，
                #   返回 None 等于把决策权交给下游，而下游拿到 None 就 return
                #   （见 update_cmd_by_mob_detection）⇒ 没有任何一方兜底。
                #
                # 【为什么改成"选近的"是安全的】两侧的怪**都已经过
                #   get_nearest_monster 的攻击框重叠判定**（重叠面积 ≥ 阈值），
                #   也就是说：无论朝哪边打，都不会是"打空气"。
                #   距离差几十像素在这个前提下不影响命中，只影响先后 ——
                #   打近的那只，下一帧冷却好了再打远的，比两边都停手强。
                #
                # ⚠️ 距离并列时**必须给出确定性结果**（用 <= 让 left 胜出），
                #    不能靠浮点比较"撞运气"：并列时若两边都不满足严格小于，
                #    又会退化成 None = 回到旧 bug。这是本改动最容易写错的一行。
                attack_direction = "left" if distance_left <= distance_right else "right"

                # ── 迟滞（hysteresis）：已选过的方向不许被"微弱优势"翻面 ────────
                #
                # 【为什么必须加这一步 —— 光"选近的"治不住抽搐】
                #   上面那句只看**本帧**谁更近。但真机上两只怪都在动，
                #   "谁更近"每帧都可能翻面 ⇒ 目标方向 left/right 逐帧横跳
                #   ⇒ 下游转身状态机每帧都被要求转向 ⇒ 角色原地左右抽搐。
                #
                #   实测复现（本仓库 tools/verify_two_side_mob_attack 第 6 节）：
                #   目标每帧翻面时，20 帧里**转向 20 次、出招 0 次** ——
                #   因为转身永远追不上不断翻面的目标，每帧都走"只转向不出招"分支。
                #   ⇒ 「抽搐」和「不出招」是**同一个病**的两个面。
                #
                # 【迟滞怎么起作用】记住上一帧的判定方向；只有当**另一侧明显更近**
                #   （近出 `attack_hysteresis` 像素以上）时才允许翻面，
                #   否则维持原方向。两只怪距离相当来回摆动时，方向就稳住了：
                #   角色朝着一边打完，冷却好了再打另一边。
                #
                # ⚠️ 迟滞**只在"两侧都合法"时生效**：单侧有怪、或另一侧消失时，
                #    上面两个分支已经直接定了方向，不受此影响（否则会打不到眼前的怪）。
                # ⚠️ 迟滞窗口默认 30px（见 config 的 attack_hysteresis）；
                #    填 0 = 关掉迟滞、退回"每帧选近的"（会重新出现抽搐，仅作对照用）。
                try:
                    _hyst = float(self.cfg.get("directional_attack", {})
                                  .get("attack_hysteresis", 30) or 0)
                except (TypeError, ValueError):
                    _hyst = 30.0
                _last_dir = getattr(self, "_attack_dir_last", None)
                if _hyst > 0 and _last_dir in ("left", "right") and attack_direction != _last_dir:
                    # 想翻面：只有当"新方向明显更近"时才准许
                    _d_old = distance_left if _last_dir == "left" else distance_right
                    _d_new = distance_left if attack_direction == "left" else distance_right
                    if _d_new > _d_old - _hyst:
                        attack_direction = _last_dir      # 优势不足 → 维持原方向
                self._attack_dir_last = attack_direction

        # 单侧分支也要记住方向，否则「另一侧怪消失」后迟滞会拿着过期方向乱判
        if attack_direction is not None:
            self._attack_dir_last = attack_direction
        else:
            # 两侧都没合法目标 → 清掉记忆，免得下次拿一个早就没怪的方向做迟滞基准
            self._attack_dir_last = None

        # 2026-09-12：原来这里会在画面上写一行
        #   "L:<dist>(ok) R:<dist>(ok) Dir:<方向>" 的调试文字，按用户要求去掉
        # （画面上只留 Attack Range 红框 + Mob Detection Box 蓝框两个框）。
        # 攻击方向的选择结果会体现在日志 [运行状态] 的指令里。
        return attack_direction

    def get_face_direction(self, monsters=None):
        '''
        离玩家最近的怪在玩家的哪一侧（"left" / "right" / None）。

        只用于让角色**面向**目标（AOE 模式）—— 与 get_attack_direction 不同：
        后者在两边怪距离接近时会返回 None（避免乱打），但**朝向**总得选一个，
        不能因为取舍不定就让角色一直保持初始朝向。

        Args:
            monsters: 参与判定的怪列表；默认 `self.monsters`（全部检测结果）。
                2026-10-13 加：感知框放大后 self.monsters 里会混进**够不着**的远处怪，
                AOE 分支改成传 `_mobs_in_attack_box()` 的结果 —— 否则角色会对着一只
                够不着的远端怪转身，把近处这只打偏。
        '''
        monsters = self.monsters if monsters is None else monsters
        if not monsters:
            return None
        best_x, best_d = None, float('inf')
        for monster in monsters:
            mx, my = monster["position"]
            mw, mh = monster["size"]
            cx, cy = mx + mw // 2, my + mh // 2
            d = abs(cx - self.loc_player[0]) + abs(cy - self.loc_player[1])
            if d < best_d:
                best_x, best_d = cx, d
        if best_x is None:
            return None
        if abs(best_x - self.loc_player[0]) <= 3:
            return None     # 正上方/正下方，不强行转身
        return "left" if best_x < self.loc_player[0] else "right"

    def get_login_button_location(self):
        '''
        get_login_button_location

        返回登录按钮中心点在**客户区坐标系**里的位置（找不到返回 None）。

        【注意】 这里不再加 title_bar_height（2026-09-10 修正）：
          img_frame 已被 get_img_frame() 裁成客户区，所以算出来的 loc 本来就是
          客户区坐标；而调用方是把它交给 click_in_game_window() 的，后者会自己
          加上客户区左上角的屏幕原点。原先再补一个标题栏高度属于重复补偿，
          会让点击整体下移一个标题栏的高度（本机实测 31px）。
        '''
        # 登录按钮模板未提供时（国服手动登录，见 load_config 的降级说明），直接放弃
        if self.img_login_button is None:
            return None

        # Extract the region where the login button should appear
        x0, y0 = self.cfg["ui_coords"]["login_button_top_left"]
        x1, y1 = self.cfg["ui_coords"]["login_button_bottom_right"]
        img_roi = self.img_frame[y0:y1, x0:x1]

        # 2026-09-12：原来这里会在画面上画一个 "login_button box" 绿框，
        # 按用户要求去掉（只有登录画面才会走到这里，平时不出现）。

        # Find the 'login' button
        loc, score, _ = find_pattern_sqdiff(
                        img_roi, self.img_login_button)
        if score < self.cfg["ui_coords"]["login_button_thres"]:
            h, w = self.img_login_button.shape[:2]
            logger.info(f"[get_login_button_location] Found login button with score({score})")
            # 客户区坐标，交给 click_in_game_window() 时会再加上客户区屏幕原点
            return (int(x0 + loc[0] + w // 2),
                    int(y0 + loc[1] + h // 2))
        else:
            return None

    def try_return_to_mainline(self):
        '''
        回正期间的退出判定：**主线优先**（2026-09-12 用户要求 + v0.9 第二版新判据）。

        第二版内核（设计 4.2）：
            `d_home >= d_main - home_dist_tol` → 角色离主线更近（或一样近）→
            已经回到主线上了 → 立刻切回主线，停止走回正线。

        ⚠️ 与**进入判据是同一个表达式**（home_should_return），口径唯一：
           第一版"警告口径和退出口径不一致"的 bug 就是这么来的。

        ⚠️ 不再用任何"半径"找主线（第一版的 home_return_range / D-3 全部删除）：
           落点离主线只有 6px 时，任何固定半径在落点处就成立 → 一进回正就被掐掉
           （第一版坑 3「抢跑」）。改成比两条线的距离后天然免疫。

        Returns:
            bool: 是否成功切回主线
        '''
        if not self.img_routes:
            return False
        # 最小驻留门（保险丝，默认关）未开 → 屏蔽一切"已回主线"判定（防抢跑）
        if not self._home_dwell_ok():
            return False
        # ⚠️ d_main 量不出（这张图没有主线）时**绝不**判"已回主线" ——
        #    否则角色会被扔回一条不存在的路线，等于站着不动。
        if self.home_d_main is None:
            return False

        # ── 硬闸：必须**真的站到主线上**才准退出回正（2026-09-13 实测修）────────
        # 为什么必须加（这段是踩坑实录，别删）：
        #   主线的 goal 圆点半径 2，会朝上下各铺 2 行 → 本项目的主线像素覆盖
        #   y=120~124。角色掉到坑底 y=126 时，到 goal 下沿 y=124 只有 **2px**，
        #   而 home_dist_tol=2 → `d_home(0) >= d_main(2) - 2 = 0` **恒成立**
        #   → 角色**刚进回正，下一帧就被踢出来**，回正线一步都走不完。
        #   实测日志：22:45:19 进 / 22:45:20 出 / 22:45:21 进 / 22:45:22 出 ……
        #   每秒进出 3~4 次，表现为「掉下去就再也回不来」。
        #
        #   更致命的是**跳跃途中**：人跳到 y=124（还在半空）就满足上面的退出式，
        #   主线立刻接管、给 left/right → 空中被横向带偏 → 摔回坑底。
        #   实测日志 22:45:21 帧10 在 (98,126) 正确拿到 `none none jump`（起跳点 98
        #   确实落在 97~101 的跳跃区里），但 22:45:22 人还在 y=124 就被踢出回正
        #   → 起跳修好了也白搭，这就是「跳了但永远上不去」。
        #
        # 所以退出回正必须**额外**要求：角色的 y 落在主线站立行 ±1 内。
        #   坑底 y=126 → |126-122|=4 > 1 → 不退出 ✓
        #   跳跃中 y=124 → |124-122|=2 > 1 → 不退出 ✓
        #   真站上平台 y=122 → 0 ≤ 1 → 退出 ✓
        _sy = getattr(self, "_main_stand_y", None)
        if _sy is not None and self.loc_player_global:
            _py = int(self.loc_player_global[1])
            if abs(_py - int(_sy)) > 1:
                self._home_stand_last_py = None
                return False      # 还在坑底 / 还在空中 → 继续沿回正线走
            # ★ 光"y 落在站立行 ±1"还不够 —— 必须**站定了**才算数。
            #   起跳过程中 y 会从 126 一路经过 124 / 123 / 122（甚至冲到 121），
            #   其中 123、122、121 都满足 ±1 —— 只判这一条的话，**人还在半空**
            #   就会被判「已回主线」踢出回正，主线立刻接管给 left/right →
            #   空中被横向带偏 → 摔回坑底。这正是「起跳了却永远上不去」的成因。
            #   所以再要求：**连续两帧 y 相同**（落地后 y 才会连续不变）。
            if getattr(self, "_home_stand_last_py", None) != _py:
                self._home_stand_last_py = _py
                return False      # y 还在变 = 还在空中 → 不许退出

        if self._home_line_keep(self.home_d_home, self.home_d_main):
            return False     # 还是回正线更近 → 保持回正，继续沿回正线走
        p = home_params(self.cfg)
        tol = int(p["home_dist_tol"])
        # 切回**最近的那一段**主线（多段巡逻时不能切回上一段，会往回走）
        seg = self._main_seg_i
        if seg is not None and 0 <= int(seg) < len(self.img_routes):
            # ★ 2026-10-14：这里是**唯一会跨段跳**的地方 —— 退出回正线时直接切到
            #   「离角色最近的那一段」（可能一次跳好几段，用户看到的就是"段号跳"）。
            #   原来只在 `_exit_home_route` 的 reason 里带一句"（第 N/M 段）"，
            #   找起来费劲；现在只要段号真的变了就单打一行 `[切段]`，
            #   与新加的推进切段日志同前缀，grep `[切段]` 就能看全部段号变化。
            if int(seg) != self.idx_routes:
                logger.info(
                    f"[切段] 退出回正线 → 切到**离角色最近的那一段**："
                    f"第 {self.idx_routes + 1}/{len(self.img_routes)} 段 → "
                    f"第 {int(seg) + 1}/{len(self.img_routes)} 段"
                    f"（全局 {tuple(self.loc_player_global) if self.loc_player_global else None}）")
            self.idx_routes = int(seg)
            self.img_route = self.img_routes[int(seg)]
        # 走统一出口（它会清掉 home_* 那一组跨帧状态 + 重新武装这条线）
        self._exit_home_route(
            f"离回正线 {self.home_d_home}px ≥ 离主线 {self.home_d_main}px - {tol} "
            f"→ 主线优先（第 {self.idx_routes + 1}/{len(self.img_routes)} 段）")
        logger.info("[回正] 已回到主线，恢复主线巡逻")
        return True

    # ══════════════════════════════════════════════════════════════════
    # v0.9 第二版回正状态机（设计 3.6 签名）
    # ══════════════════════════════════════════════════════════════════

    def _build_mainline_cache(self) -> None:
        '''把全部主线（route{N}.png）的色码像素抽成 numpy 数组，供每帧算 d_main。

        ⚠️ 为什么必须预抽：d_main 要**每帧**算一次，而 268×187 全图 Python 双循环
           约 5 万次/帧，30fps 下吃不消。主线实际只有几十个像素，向量化后 < 20µs。

        ⚠️ 白色中转点 (255,255,255) 不在 color_code 里 → 天然不算主线像素
           （沿用第一版用例【10】：角色站在中转点上不会被误判成"已回主线"）。

        ⚠️ img_routes 运行期不变 → 只在 load_config 里建一次。
        '''
        codes = set(self.color_code) | set(self.color_code_up_down)
        xs, ys, segs = [], [], []
        for i, img in enumerate(self.img_routes or []):
            if img is None:
                continue
            for (x, y) in collect_pixels(img, codes):
                xs.append(int(x))
                ys.append(int(y))
                segs.append(int(i))
        if xs:
            self._main_pts_xy = (np.asarray(xs, dtype=np.int32),
                                 np.asarray(ys, dtype=np.int32))
            self._main_pts_seg = np.asarray(segs, dtype=np.int32)
            # ── 主线的「站立行」= 主线像素里 y 出现次数最多的那一排 ──────────
            # ⚠️ 为什么不能直接用「最近主线像素的 y」：goal 圆点半径 2，会朝上下
            #    各铺 2 行（本项目 route1 的像素覆盖 y=120~124，但真正走的只有
            #    y=122 那 13 个像素）。取众数才是角色真正站的那一排。
            #    它用来给「退出回正」加硬闸 —— 见 try_return_to_mainline。
            self._main_stand_y = int(np.argmax(np.bincount(
                np.asarray(ys, dtype=np.int32))))
        else:
            self._main_pts_xy = None
            self._main_pts_seg = None
            self._main_stand_y = None

    def _scan_home_lines(self, map_name, img_home_rgb, img_routes_rgb):
        '''加载期：把 route_home.png 解析成一组「回正线」。

        流程：split_components（一个 8-连通域 = 一条线）
              → 每条 calc_line_geom + far_segment_stats
              → validate_home_line 逐条判废 → **就地抹掉废线像素**（只涂黑那一个组件）
              → 存活的线填 xs/ys（numpy，运行期算 d_home 用）

        ⚠️ 判废粒度是**组件**，不是整张图：一张图可以有好几条回正线（左边一个坑、
           右边一个坑），一条画废了不能连坐其它几条。

        Args:
            map_name: 只用于日志
            img_home_rgb: 回正线 RGB 图（**会被就地修改**：废线像素被涂黑）
            img_routes_rgb: 主线 RGB 图列表（用来量 d_main / 覆盖跨度 / 终点贴主线）

        Returns:
            list[dict]: 可用回正线（已按"最上、最左"排序），元素结构见设计 3.3
        '''
        route_codes = set(self.color_code) | set(self.color_code_up_down)
        comps = split_components(img_home_rgb, route_codes)
        if not comps:
            logger.error(
                f"[回正线] minimaps/{map_name}/route_home.png 上一个路线像素都没有"
                f"（等于空图）—— 本次不启用回正。")
            return []

        lines = []
        n_bad = 0
        for comp in comps:
            geom = calc_line_geom(comp, img_routes_rgb, route_codes, self.cfg,
                                  img_rgb=img_home_rgb)
            far = cover_stats(comp, img_routes_rgb, route_codes, self.cfg)
            ok, why = validate_home_line(comp, img_home_rgb, img_routes_rgb,
                                         route_codes, self.cfg)
            if ok:
                item = {
                    "id": len(lines) + 1,
                    "start": geom["start"],
                    "goal": geom["goal"],
                    "span": geom["span"],
                    "has_jump": geom["has_jump"],
                    "n_pts": geom["n_pts"],
                    "far_pts": far["far_pts"],
                    "cover_xs": far["cover_xs"],
                    "cover_span": far["cover_span"],
                    "bbox": geom["bbox"],
                    # 运行期算 d_home 用的坐标数组（向量化）
                    "xs": np.asarray([p[0] for p in comp], dtype=np.int32),
                    "ys": np.asarray([p[1] for p in comp], dtype=np.int32),
                    "armed": True,       # 运行期：这条线还能不能被选中
                    "disarm_t": 0.0,     # 运行期：去武装时刻（超时兜底用）
                }
                lines.append(item)
                continue

            # ── 废线：只抹掉这一个组件（其它线照常生效）─────────────────────
            n_bad += 1
            for x, y in comp:
                img_home_rgb[y, x] = (0, 0, 0)
            where = (f"起点 {geom['start']}" if geom["start"] else
                     f"这一坨像素（{len(comp)} 个，左上角 {min(comp, key=lambda q: (q[1], q[0]))}）")
            logger.error(
                f"[回正线] minimaps/{map_name}/route_home.png 第 {len(lines) + n_bad} 条"
                f"（{where}）判为废线，已**只禁用这一条**：{why}\n"
                f"        同图其它回正线不受影响；修好这条之前，从这儿掉下去只能站桩打怪。")

        # P2-1：两条线的落点区间（起始段 x 范围）重叠 → 只是警告，不阻断
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                ci, cj = lines[i]["cover_xs"], lines[j]["cover_xs"]
                if not ci or not cj:
                    continue
                if ci[0] <= cj[1] and cj[0] <= ci[1]:
                    logger.warning(
                        f"[回正线] 第 {lines[i]['id']} 条与第 {lines[j]['id']} 条的落点区间"
                        f"（x {ci[0]}~{ci[1]} / x {cj[0]}~{cj[1]}）**重叠了** —— "
                        f"角色掉在重叠区可能走错线。不阻断，但建议两条线画在不同的坑上。")
        return lines

    def _log_home_health(self):
        '''P1-2：加载期逐条打一行中文体检（非程序员只看日志就该知道画得对不对）。

        一行包含：起点 / 终点 / 点数 / 是否含跳跃 / 落点覆盖 x 范围。
        （PRD 6.4 的文案样式：`#1 起点(94,128) → 终点(99,122)，21 px，含 1 次跳跃，
          落点覆盖 x94~108 ✓`）
        '''
        if not self.home_lines:
            return
        for a in self.home_lines:
            cov = a.get("cover_xs")
            cov_txt = f"x{cov[0]}~{cov[1]} ✓" if cov else "未知"
            logger.info(
                f"[回正线] #{a['id']} 起点 {a['start']} → 终点 {a['goal']}，"
                f"{a['n_pts']} px，{'含跳跃 ✓' if a['has_jump'] else '无跳跃'}，"
                f"落点覆盖 {cov_txt}")
        logger.info(f"[回正线] 共 {len(self.home_lines)} 条可用；"
                    f"角色离回正线比离主线近 "
                    f"{int(home_params(self.cfg)['home_dist_tol'])}px 以上就自动回正"
                    f"（不需要等到「看不见主线」）")

    def _home_line_armed(self, line) -> bool:
        '''这条线现在**能不能被选中**（去武装后的重新武装逻辑）。

        去武装后两种方式恢复（取或）：
          · 角色离开这条线的 **bbox** ≥ home_rearm_margin（默认 4px）
          · 或去武装满 home_rearm_timeout（默认 30s）无条件恢复

        ⚠️ 兜底那条必须有：否则角色一直在坑底晃悠时这条线会**永远**武装不回来
           （等于这条线被静默删除）。

        ⚠️ 本函数有副作用（恢复时会写回 line["armed"]）—— 它是**唯一的**重新武装点，
           由 `_home_dist()` 在过滤前逐条调用，保证每帧都刷新。
        '''
        if not line:
            return False
        if line.get("armed", True):
            return True
        p = home_params(self.cfg)
        disarm_t = line.get("disarm_t", 0.0)
        if disarm_t and time.time() - disarm_t > float(p["home_rearm_timeout"]):
            line["armed"] = True
            line["disarm_t"] = 0.0
            return True
        if self.loc_player_global and line.get("bbox"):
            x0, y0, x1, y1 = line["bbox"]
            px, py = int(self.loc_player_global[0]), int(self.loc_player_global[1])
            # 点到包围盒的曼哈顿距离（在盒内 = 0）
            d = max(int(x0) - px, 0, px - int(x1)) + max(int(y0) - py, 0, py - int(y1))
            if d >= int(p["home_rearm_margin"]):
                line["armed"] = True
                line["disarm_t"] = 0.0
                return True
        return False

    def _home_dist(self):
        '''本帧：角色到**已武装**回正线的最近距离 → (d, line)。

        ★ 防御 D2：必须**跳过 armed=False 的线**（在本函数内部过滤，不是在外面）。
          否则去武装形同虚设 → 「8s 超时 → 下一帧立刻重进」的死锁必然复活：
          新判定天然不带 armed 概念，超时退出后角色仍在坑底、d_home 还是 0。

        ⚠️ 为什么"内部过滤"而不是"外层再判一次 armed"：
          只要有一处漏判 armed，死锁就复活。口径统一为 ——
          `_home_dist()` 的返回值语义 = 「**当前可用的**回正线里最近的距离」，
          `None` = 没有可用回正线（进不了回正）。

        ⚠️ 本函数会顺带调 `_home_line_armed()` 刷新每条线的武装状态：
          没有这个刷新，去武装的线永远武装不回来（等于被静默删除）。

        Returns:
            (int | None, dict | None): (最近距离, 那条线)；没有可用线 → (None, None)
        '''
        if not self.loc_player_global:
            return None, None
        best_d, best_line = None, None
        for ln in self.home_lines or []:
            self._home_line_armed(ln)          # 先刷新武装状态（内部只在需要时改）
            if not ln.get("armed", True):
                continue                       # ★ D2：未武装的线**不算** d_home
            d = dist_to_pixels(ln.get("xs"), ln.get("ys"), self.loc_player_global)
            if d is None:
                continue
            if best_d is None or d < best_d:   # 并列时取靠前那条（id 小者）
                best_d, best_line = int(d), ln
        return best_d, best_line

    def _main_dist(self):
        '''本帧：角色到最近主线像素的曼哈顿距离 + 那个像素属于第几段 → (d, seg_i)。

        ⚠️ 走 `_main_pts_xy` 缓存（O(像素数) 向量化），不是每帧全图扫描。
        ⚠️ **全图**最近像素，不受 search_range / rescue_range 限制 ——
           套搜索框会复活第一版坑 1（坑底离主线 6px < 10 → 判定在主线上 → 永不回正）。

        Returns:
            (int | None, int | None): 没有主线 / 还没定位 → (None, None)
        '''
        if self._main_pts_xy is None or not self.loc_player_global:
            return None, None
        xs, ys = self._main_pts_xy
        px, py = int(self.loc_player_global[0]), int(self.loc_player_global[1])
        d_arr = np.abs(xs - px) + np.abs(ys - py)
        i = int(np.argmin(d_arr))
        return int(d_arr[i]), int(self._main_pts_seg[i])

    def _home_line_keep(self, d_home, d_main) -> bool:
        '''= home_should_return(d_home, d_main, tol)（tol 从配置读，<0 回落默认 2）。

        ⚠️ 进入 / 保持 / 退出**共用这一处**，禁止在别处重写这个比较 ——
           第一版"警告口径和退出口径不一致"就是这么来的。

        ★ 坑底例外（2026-09-15 真机实测加）：角色在**主线站立行下方 3px 以上**
          （= 真掉在平台下面，见 `_home_below_mainline`）时，**容差收成 0**。

          为什么必须收：`d_main` 在坑底是"**隔着地板看见的**"，会虚小 ——
          主线在平台两端 x=94 / x=104 有斜坡像素落在 **y=124**，离坑底 y=126
          只有 **2px**，而 `tol=2` 的预算正好被这 2px 吃光：
              `d_home(0) < d_main(2) - 2` 即 `0 < 0` → 判"你在主线上" → **不进回正**
          → 角色**最可能掉下去的两个边缘位置**，恰好是**唯一进不了回正**的两个位置
          → 掉下去就永远回不来（再叠加窄平台降级吞掉左右移动 = 站着不动、不报卡死）。

          收成 0 之后判据**仍然生效**（依旧要求"离回正线比离主线更近"），只是不再
          要求"近 2px 以上" —— 所以"接不住的回正线"照样进不去
          （见 verify_home_route【15】：只画竖线时 d_home=8 / d_main=8 → `8 < 8` 不成立）。

          实测（落点 y=126 逐像素，调 `_home_line_pick`）：修前 **x≤94 与 x=104
          共 12 个落点进不了回正**；修后 **x=84~116 全部能进**，而主线上 y=122~124
          的 69 个点**一个都不会被误拉进回正**（无"抢跑"）。
        '''
        return home_should_return(d_home, d_main, self._home_tol_now())

    def _home_tol_now(self) -> int:
        '''本帧**实际生效**的容差 —— 判定与日志**共用这一处**（2026-09-15 加）。

        ⚠️ 为什么必须有它：坑底会把容差收成 0（见 `_home_line_keep`），
           而进入回正那条日志原来是**自己**读 `home_dist_tol` 拼字符串的 →
           坑底明明按 0 算，日志却打「离回正线 2px < 离主线 4px - **2**」，
           看起来像 `2 < 2` 不成立却进了回正 —— **日志说谎比没日志更坏**。
           现在判定和日志都从这里取，不可能再对不上。
        '''
        tol = int(home_params(self.cfg)["home_dist_tol"])
        if self._home_below_mainline():
            tol = 0
        return tol

    def _home_below_mainline(self) -> bool:
        '''角色是否在主线**站立行下方**（= 掉在平台下面 / 坑里）。

        ⚠️ 为什么要它（2026-09-15 真机实测踩出来，用户原话
           「中间和右边边缘掉下去都能立马回来，**左边**不知道是不是超出当初录制的
             回正线了，所以没回来也没反应，**原地不动**」）：

        进入回正的判据是 `d_home < d_main - tol`（比两条线谁更近）。它有个**前提**：
        `d_main` 必须真的代表"角色离主线的距离"。而在坑底这个前提**不成立** ——
        主线在平台两端（x=94 / x=104）有斜坡像素落在 **y=124**，而坑底是 **y=126**
        → `d_main` 只有 **2px**（隔着地板"看见"的），而 `d_home` 也是 0~2px
        → `d_home(0) < d_main(2) - 2` 即 `0 < 0` **不成立**
        → 判"你在主线上" → **不进回正**。

        结果极其讽刺：**角色最可能掉下去的两个边缘位置，恰好是唯一进不了回正的位置**。
        而掉下去之后又因为窄平台降级吞掉左右移动 → 站着不动、也不报卡死
        → 永远出不来（用户看到的"没反应，原地不动"）。

        实测（落点 y=126，逐像素）：**x≤94 与 x=104 共 12 个落点进不了回正**，
        x=95~103 / 105~116 都能进。

        ⇒ 判据本身没写错，是**坑底离主线比设计假设的近**：设计按"坑底离主线 6px"
          验算（config 注释），实测只有 4px、两端更只有 2px —— tol=2 的预算被吃光。

        这道闸与**退出侧**那道硬闸（`try_return_to_mainline` 里"必须站在站立行 ±1
        才算回到主线"）正好对称：一个防"在坑底被误判成**已回**主线"，
        一个防"在坑底被误判成**在主线上**"。

        Returns:
            bool: True = 人在站立行**下方 3px 以上**（`py > stand_y + 2`）

        ⚠️ 为什么门槛是 `+2`（下方 3px）而不是退出侧那个 `±1`：
           主线像素**不止站立行一行** —— 本项目 route1/route2 的 y 覆盖 120~124
           （goal 圆点上下各铺 2 行 + 两端斜坡）。所以 y=124 是**主线自己的斜坡行**，
           角色正常巡逻踩上去时 y 也会到 124 —— 那是"在主线上"，不是"掉下去了"。
           拿 ±1 当门槛会把"踩斜坡"误判成"掉坑" → 一到平台边缘就抢跑进回正。
           实测：坑底 y=126、斜坡 y=124 ⇒ `+2`（=125 起算）正好把两者分开，
           且对 ±1px 的坐标抖动留了 1px 余量（斜坡读到 125 也不会误触发）。
        '''
        _sy = getattr(self, "_main_stand_y", None)
        if _sy is None or not self.loc_player_global:
            return False
        try:
            return int(self.loc_player_global[1]) > int(_sy) + 2
        except (TypeError, ValueError):
            return False

    def _home_line_pick(self):
        '''运行期：这一帧该不该进回正、进的是哪条线。

        三条闸（**缺一不可**，设计 11-⑪）：
          ① `_home_dist()` 拿到最近的**已武装**回正线（内部已过滤未武装的线 = D2）
          ② `_home_line_keep()` 成立（d_home < d_main - tol）
          ③ `_home_reenter_allowed()`（冷静期必须卡在**新入口**上 = D1）

        ⚠️ D1 为什么必须在这：第一版只在「主线一帧像素都找不到」的老路径上调冷静期，
           新入口根本没调 —— 于是超时退出后下一帧从新入口直接重进，
           `home_enter_t` 每帧归零，8s 超时退化成"每帧重置"（实测 120/120 帧卡死）。

        ★ 缓存失效时 **d_home 和 d_main 一并作废、一并重算**（不各自为政）：
          调用方（插入点①）本来就会先算好 `home_d_main` / `home_d_home`，
          正常路径**一次都不用重算**（每帧计算量不变）。
          但"缓存里的线已被去武装 / 本方法被直接调用（没走插入点①）"时，
          如果只重算 d_home 而复用 `home_d_main`，就会拿**上一帧、甚至上一个位置**
          的主线距离去比 —— 本函数不再自洽，悄悄依赖"调用方一定先算过 d_main"这个
          没有写下来的约定（换个调用点就静默出错）。一并重算才自洽。

        Returns:
            dict | None: 该进的那条回正线；不该进返回 None
        '''
        if not self.home_lines or not self.loc_player_global:
            return None
        d_home, line = (self.home_d_home, self._home_line_near)
        d_main = self.home_d_main
        # ⚠️ 缓存可能过期（这一帧内刚被去武装 / 直接调用本方法没走插入点①）：
        #    只要缓存里的线已经不是"已武装"、或 d_main 还没算出来，就**两个距离一起重算**。
        #    宁可多算一次，也不能拿着一条刚被去武装的线把人拉回回正（那正是 D2 要防的死锁），
        #    也不能拿一个过期的 d_main 去比（那会让进入判定反向）。
        if line is None or not line.get("armed", True) or d_main is None:
            d_home, line = self._home_dist()
            d_main, _seg = self._main_dist()
        if line is None:
            return None
        # 坑底把容差收成 0 的处理在 `_home_line_keep` 里（口径唯一，别在这重写比较）
        if not self._home_line_keep(d_home, d_main):
            return None
        if not self._home_reenter_allowed():      # ★ D1：冷静期卡新入口
            return None
        return line

    def _enter_home_route(self, reason: str, line=None):
        '''**进入回正状态的唯一入口**（禁止在别处直接写 is_using_home_route）。

        ⚠️ 没有回正线（img_route_home 为 None）时**什么都不做** —— 这是最重要的保护：
           进去了又没有线可走 = 指令真空 + 不打怪 = 死局。
        '''
        if self.is_using_home_route:
            return
        if self.img_route_home is None:
            return
        self.is_using_home_route = True
        self.img_route = self.img_route_home
        self.home_line = line
        self.home_enter_pos = (tuple(self.loc_player_global)
                               if self.loc_player_global else None)
        self.home_enter_t = time.time()
        self.home_frame = 0
        # 重置「站稳判定」的上一帧 y：不清掉的话，进入回正那一刻可能碰巧
        # 和上一帧 y 相同，被误判成「已经站稳」，回正线一步都走不出去。
        self._home_stand_last_py = None
        # ── 回正起跳埋点：计数**必须在这里清零** ──────────────────────────
        # 不清零就会跨轮累计 —— 第二轮看到的「走到起跳点 5 次」其实是好几轮
        # 加起来的，数字是假的，比没有还糟（会直接把排查带沟里）。
        # 起点不用新建变量：上面 `home_enter_pos` 已经记好了。
        self.home_jump_n = 0
        self._home_jump_on = False
        self.home_jump_ts = []
        self.home_y_min = None
        self.home_y_max = None
        self.home_d_home0 = getattr(self, "home_d_home", None)
        self.home_d_main0 = getattr(self, "home_d_main", None)
        # 一进回正就取消归位：归位是"回到主线之后"才做的事，
        # 人又掉坑了还在归位，两套指令会打架。
        self._recenter_active = False
        self._recenter_n = 0
        logger.info(f"[回正] {reason} → 进入回正状态，沿回正线走回主线"
                    f"（起点 "
                    f"{tuple(self.loc_player_global) if self.loc_player_global else '?'}，"
                    f"主线站立行 y={getattr(self, '_main_stand_y', '?')}，"
                    f"离回正线 {getattr(self, 'home_d_home', '?')}px"
                    f" / 离主线 {getattr(self, 'home_d_main', '?')}px）")

    def _exit_home_route(self, reason: str, disarm: bool = False):
        '''**退出回正状态的唯一出口**（三条路：回主线 / 接不上 / 8s 超时）。

        Args:
            reason: 中文原因（进日志）
            disarm: True = 这次回正**以失败告终**（超时 / 接不上）→ **这条线**去武装，
                    并且给**所有**入口上一段冷静期（home_reenter_lock）；
                    False = 成功回到主线 → 立刻重新武装 + 解除冷静期
                    （下次真的掉下去要能马上接住）

        ⚠️ 为什么失败退出必须去武装 + 上冷静期（QA 实测必修的 Bug，第二版更严重）：
            超时退出这一帧，角色**仍站在坑底**（d_home 还是 0）→ 新判定
            `d_home < d_main - tol` **下一帧仍然成立** → 立刻重进 →
            home_enter_t / home_frame 全部归零 → 8s 超时变成"每帧重置"，
            而回正期间不打怪 = 「既不走也不打」的死循环。
            去武装（D2）+ 冷静期（D1）一起把这个环断开。
        '''
        if not self.is_using_home_route:
            return
        self.is_using_home_route = False
        # ── 埋点：抓快照（**必须在下面清空 home_* 之前**，否则抓到的全是 None）──
        # 只读、不写任何状态，纯观测。所有字段一律 getattr 兜底：
        # tools/verify_navigation.py 的 Stub 不走 __init__，直接 self.xxx 会崩。
        _ent = float(getattr(self, "home_enter_t", 0) or 0)
        _snap = {
            "result": "超时/接不上退出" if disarm else "成功回主线",
            "reason": reason,
            "dur_s": (time.time() - _ent) if _ent else 0.0,
            "frames": int(getattr(self, "home_frame", 0) or 0),
            "start": getattr(self, "home_enter_pos", None),
            "end": (tuple(self.loc_player_global)
                    if getattr(self, "loc_player_global", None) else None),
            "jump_n": int(getattr(self, "home_jump_n", 0) or 0),
            "y_min": getattr(self, "home_y_min", None),
            "y_max": getattr(self, "home_y_max", None),
            "d_home": getattr(self, "home_d_home", None),
            "d_main": getattr(self, "home_d_main", None),
            "mm_score": getattr(self, "minimap_score", None),
        }
        try:
            _ts = list(getattr(self, "home_jump_ts", None) or [])
            _snap["jump_gap"] = (_ts[-1] - _ts[-2]) if len(_ts) >= 2 else None
        except (TypeError, IndexError):
            _snap["jump_gap"] = None
        ln = self.home_line
        p = home_params(self.cfg)
        if ln is None:
            tail = "（这次回正不是某条回正线触发的，无需改动线的状态）"
        elif disarm:
            ln["armed"] = False
            ln["disarm_t"] = time.time()
            tail = (f"回正线 #{ln.get('id')} **暂时去武装** —— 需离开它的范围 "
                    f"{int(p['home_rearm_margin'])}px 或 {int(p['home_rearm_timeout'])}s 后"
                    f"才会再次接人（防「8s 回正 → 立刻重进」的死循环）")
        else:
            ln["armed"] = True
            ln["disarm_t"] = 0.0
            tail = f"回正线 #{ln.get('id')} 已重新武装（下次掉下去还会接住）"
        # 冷静期：失败退出 → 上锁；成功回主线 → 解锁（不然下次真掉下去要白等 10s）
        if disarm:
            self.home_reenter_lock_t = time.time()
        else:
            self.home_reenter_lock_t = 0.0
        self.home_line = None
        self.home_enter_pos = None
        self.home_enter_t = 0.0
        self.home_frame = 0
        # 把导航图交回主线（没主线就保持现状，由打怪逻辑接管移动）
        if self.img_routes:
            i = max(0, min(self.idx_routes, len(self.img_routes) - 1))
            self.idx_routes = i
            self.img_route = self.img_routes[i]
        logger.info(f"[回正] {reason} → 退出回正状态，恢复主线巡逻；{tail}")
        # ── 回正**成功** → 先走回安全区中心再正常巡逻（见 _apply_recenter）──
        # 失败（超时 / 接不上）时**不**归位：人还在坑底，归位只会跟回正打架。
        self._recenter_active = (not disarm)
        self._recenter_n = 0
        # ★ 归位新开一轮 → 清掉上一轮的"上一帧 x"（2026-09-15 加，见 _apply_recenter）
        self._recenter_last_px = None
        # ── 埋点：一次回正压成一行小结（区分 A / B / C 全靠它）────────────
        #   A 没走到起跳点 → 小结里「**一次都没走到起跳点**」
        #   B 被冷却吞了   → 「走到起跳点 N 次」但键盘侧只有 1 条「真的按下了跳跃键」
        #   C 跳了没上去   → 按了 ≥2 次，但「期间 y」仍然贴着坑底
        # ⚠️ 打日志失败绝不能影响退出回正 —— 这儿抛异常 = 回正退不出去 = 卡死。
        if jump_trace_on(self.cfg):
            try:
                logger.info(home_summary_line(_snap))
            except Exception:
                pass

    def _home_dwell_ok(self) -> bool:
        '''最小驻留门（**保险丝，默认关闭**）：进入回正后，得先"真的动起来了 /
        过了一会儿"才允许判定退出。

        条件（取**或**）：
          ① 已离开进入点 ≥ home_min_leave（默认 0 = 关闭）
          ② 已处于回正态 ≥ home_min_frames（默认 0 = 关闭）

        ⚠️ 为什么默认关：第二版靠「走到终点时 d_home = d_main = 0 → 相等时主线优先」
           自动处理抢跑，理论上不需要。实测若发现"一进就出"的抖动，把这两个值
           调回 6 / 15 即可（这就是保留保险丝的意义，不写死删）。

        ⚠️ home_enter_pos 为 None（没走进入口 / 还没定位）时返回 True：
           量不出位移就不屏蔽，交给距离判据，避免"门永远打不开"。
        '''
        if not self.is_using_home_route:
            return True
        p = home_params(self.cfg)
        if self.home_frame >= int(p["home_min_frames"]):
            return True
        if self.home_enter_pos is None or not self.loc_player_global:
            return True
        return manhattan(self.loc_player_global, self.home_enter_pos) >= int(p["home_min_leave"])

    def _home_reenter_allowed(self) -> bool:
        '''现在**能不能进回正**（冷静期）。

        ⚠️ 第二版改：这条**卡所有入口**（新判定 `_home_line_pick` + 老兜底
           「主线一帧像素都找不到」），不再只卡老路径 —— 见 D1。

        回正失败（超时 / 接不上）退出后的 home_reenter_lock 秒内一律不准再进 ——
        理由见 _exit_home_route 的注释：否则下一帧被拉回来，超时和驻留门全部失效。
        '''
        # ⚠️ home_params 已经把 0 / 负数回落成默认 10（D4），这里只管读
        lock = float(home_params(self.cfg)["home_reenter_lock"])
        if lock <= 0:
            lock = float(DEFAULT_HOME_PARAMS["home_reenter_lock"])
        if not self.home_reenter_lock_t:
            return True
        if time.time() - self.home_reenter_lock_t >= lock:
            self.home_reenter_lock_t = 0.0   # 冷静期自然到期 → 解锁
            return True
        if time.time() - self.t_last_home_lock_log > 10:
            self.t_last_home_lock_log = time.time()
            logger.info(
                f"[回正] 角色 {self.loc_player_global} 现在应该回正，但上一次回正刚失败、"
                f"还在 {lock:.0f}s 冷静期内 —— 这一轮先打怪，不重复往回正线里钻。\n"
                f"        想让它早点再试：重画这条回正线（终点画到主线上），"
                f"或把 route.home_reenter_lock 调小（秒）。")
        return False

    def _calc_seg_goals(self):
        '''对外入口：按 endpoint_margin 内缩终点，并保证两端终点**不至于靠太近**。

        ⚠️★ 2026-09-13 用户实测修：内缩到"两端终点只差 2px"时，`_near_seg_goal`
        的判定容差 reach 会让两段的"已到达"区间**完全重叠** —— 切到其中一段后
        条件恒为真、永远等不到"离开"，就再也切不回来，角色一路走出主线掉下平台
        （日志表现：段1 的判定是 x ≤ 104，而角色一路往左走根本到不了 104）。
        所以这里**从配置值往下试**，直到内缩后两端至少隔 4px。
        单段路线不存在"切段"的问题，直接用配置值。
        '''
        try:
            _m = int(self.cfg.get("route", {}).get("endpoint_margin", 3) or 0)
        except (TypeError, ValueError):
            _m = 3
        _m = max(0, _m)
        for m in range(_m, -1, -1):
            self._calc_seg_goals_with(m)
            if len(self._seg_goals) < 2 or self._seg_goal_gap >= 4:
                break

    def _calc_seg_goals_with(self, _margin):
        '''每一段的终点（goal 圆点质心）+ 主方向（这段主要是往左还是往右走）。

        平台和路线可能很短（用户实测只有 14px），光靠"踩到 goal 像素"判定到段尾
        太脆弱 —— 见 _near_seg_goal 的说明。

        Args:
            _margin: 终点朝段内收缩多少像素（0 = 不缩）
        '''
        self._seg_goals = []
        self._seg_dir = []
        self._seg_span = []
        # ── 端点安全余量（2026-09-13 加）────────────────────────────────
        # 主线是"录到哪走到哪"，很多人会一路录到平台边缘，于是终点就贴在悬崖上。
        # 走到终点才掉头 + 判定有一帧延迟 + 移动惯性 = 踩空掉下平台
        # （用户实测：录制时没事，挂机久了却会走出主线掉下去）。
        # 所以把终点朝**段内**收缩 margin 像素，让角色提前掉头。
        goal_codes = {c for c, v in self.color_code.items()
                      if v.strip().split()[-1] == "goal"}
        for img in self.img_routes:
            pts = self._route_pixels(img)
            if pts:
                _xs = [q[0] for q in pts]
                _ys = [q[1] for q in pts]
                self._seg_span.append(max(max(_xs) - min(_xs),
                                          max(_ys) - min(_ys)))
            else:
                self._seg_span.append(0)
            goals = [(x, y) for x, y in pts if tuple(img[y, x][:3]) in goal_codes]
            if goals:
                gx = sum(p[0] for p in goals) // len(goals)
                gy = sum(p[1] for p in goals) // len(goals)
                if _margin > 0 and pts:
                    # 只沿**主导轴**（跨度大的那根轴）收，免得斜着把终点推离路线
                    _sx = max(q[0] for q in pts) - min(q[0] for q in pts)
                    _sy = max(q[1] for q in pts) - min(q[1] for q in pts)
                    # ⚠️ 上限保护：**原来**是「不得超过跨度的 20%」，实测证明是错的。
                    #    2026-09-13 用户实测：本图跨度 10px → 20% 只让收 2px，
                    #    而角色冲过终点的**过冲量正好也是 2px** —— 内缩被过冲完全吃掉，
                    #    等于白缩，照样踩到 x=94/104 的斜坡掉进坑。
                    #    教训：**内缩量必须明显大于过冲量才有效**，按"跨度百分比"封顶
                    #    在短路线上必然把内缩压到和过冲同一量级。
                    #    改成"至少保留 3px 巡逻宽度"的硬下限：(_span - 4) // 2。
                    #    （留 3px 而不是 1px：两端塌到同一个点时角色会原地抖动，
                    #      而且彻底失去巡逻意义 —— 用户虽说不介意范围小，但定点模式
                    #      他早就因为识别问题放弃了。）
                    #    用户已明确：巡逻范围无所谓，一切以不掉落为优先。
                    _span = max(_sx, _sy)
                    _margin_eff = min(_margin, max(1, (_span - 4) // 2))
                    if _sx >= _sy:
                        _cx = sum(q[0] for q in pts) / len(pts)
                        _lo, _hi = min(q[0] for q in pts), max(q[0] for q in pts)
                        if gx > _cx:
                            gx = max(_lo, gx - _margin_eff)   # 终点在右端 → 往左收
                        elif gx < _cx:
                            gx = min(_hi, gx + _margin_eff)   # 终点在左端 → 往右收
                    else:
                        _cy = sum(q[1] for q in pts) / len(pts)
                        _lo, _hi = min(q[1] for q in pts), max(q[1] for q in pts)
                        if gy > _cy:
                            gy = max(_lo, gy - _margin_eff)
                        elif gy < _cy:
                            gy = min(_hi, gy + _margin_eff)
                    logger.debug(f"[巡逻] 终点内缩 {_margin_eff}px"
                                 f"（上限 {_margin}，防走出平台）→ ({gx},{gy})")
                self._seg_goals.append((gx, gy))
            else:
                self._seg_goals.append(None)
            # 主方向：数一下这段里 left / right 像素哪个多
            # ── 主方向：数一下这段里 left / right 像素哪个多 ──────────────
            #
            # ⚠️★ 2026-09-28 改口径：**只看 goal 附近的像素**，不再统计全段
            #
            #   为什么必须改（真机现场，用户 minimaps/废都南方工地，2026-09-28 00:26）：
            #     用户的路线是 **L 形**（不是直线）：
            #       地面：从右往左走（left，x=183~202）
            #       → 跳到梯子(x=178) → 爬上去
            #       → 高台：往右走（right，x=178~195）
            #       → 折返点 goal=(198,93)
            #     **全段统计**：left=20、right=18 ⇒ 主方向被算成 **left**
            #       ⇒ `_near_seg_goal` 的判据变成 `px <= goal_x + reach = 199`
            #       ⇒ 而**梯子在 x=178，178 ≤ 199 恒成立** —— 横向条件**永远满足**，
            #         卡住切段的只剩"高度"那一条 ⇒ 爬到 y=96 就切段 ⇒ 角色还在
            #         梯子半中腰就被判"到达" ⇒ 切到回程段后立刻被 `down` 赶下去。
            #       （用户症状："爬不到顶"、"差一点点就要碰到梯子就回头"。）
            #
            #   ⇒ 正确语义：切段判据要回答的是「**我是不是朝着终点走过去的**」，
            #     所以该看**终点附近**的行进方向，而不是整段的方向。
            #     L 形路线的"接近段方向"与"整段方向"本来就相反。
            #
            #   实测（半径 10 / 15 / 20 三种取值结果完全一致：left=0、right=8/13/18）
            #     ⇒ 本判据**对半径不敏感**，取 10 即可，也不需要新增配置项。
            #     改后判据 = `px >= goal_x - reach = 197` ⇒ 只有真走到折返点附近
            #     才切段，梯子底下(x=178)不再误触发 ✓
            #
            #   ⚠️ 对**直线路线完全无影响**：直线时"goal 附近方向"与"全段方向"
            #      必然一致，判据与改动前逐字相同。
            _DIR_NEAR_R = 10
            _near = []
            # ⚠️ 每段**重新取一次** goal 中心：`gx/gy` 是上一段 if goals 块里赋的，
            #    本段没有 goal 时它会**残留上一段的值**（变量在循环外仍是旧值），
            #    那样算出来的方向就是别段的 —— 所以这里显式重算一次，不靠残留。
            _seg_goals_here = [(x, y) for x, y in pts
                               if tuple(img[y, x][:3]) in goal_codes]
            if _seg_goals_here:
                _gx = sum(q[0] for q in _seg_goals_here) // len(_seg_goals_here)
                _gy = sum(q[1] for q in _seg_goals_here) // len(_seg_goals_here)
                _near = [(x, y) for x, y in pts
                         if abs(x - _gx) + abs(y - _gy) <= _DIR_NEAR_R]
            _scan_pts = _near if _near else pts   # goal 附近没像素时退回全段（保守）
            nl = nr = 0
            for x, y in _scan_pts:
                cmd = (self.color_code.get(tuple(img[y, x][:3]))
                       or self.color_code_up_down.get(tuple(img[y, x][:3])))
                if not cmd:
                    continue
                parts = cmd.split()
                if parts and parts[0] == "left":
                    nl += 1
                elif parts and parts[0] == "right":
                    nr += 1
            self._seg_dir.append("left" if nl > nr else ("right" if nr > nl else None))

        # ── 内缩后相邻终点的实际间距（_near_seg_goal 用它给 reach 封顶）──
        self._seg_goal_gap = 0
        _fg = [g for g in self._seg_goals if g]
        if len(_fg) >= 2:
            self._seg_goal_gap = min(
                max(abs(_fg[i][0] - _fg[i - 1][0]),
                    abs(_fg[i][1] - _fg[i - 1][1]))
                for i in range(len(_fg)))

    def _near_seg_goal(self):
        '''角色是否**走过了**当前段的终点（不要求踩到 goal 像素）。

        为什么需要它：goal 圆点半径只有 2px，而玩家坐标是**质心**、踩不中就判不到 ——
        平台本来就不长（用户实测路线跨度只有 14px），角色走过去却没触发切段，
        冲出去后再也找不到路线像素。表现就是「到了终点停不下来一直往前走」。
        ⚠️★ 2026-09-16 订正：这里原来写的是"角色一帧能移动十几像素"，**那是推断、
           从没实测**；实测只有 ~0.3 px/帧（≈4~5 px/秒，依据见下面"上限三"那段），
           差了 20~50 倍。症状是真的，数字是猜的，别再引用它。

        ⚠️ 必须**方向感知 + 边沿触发**（2026-09-12 模拟发现的坑）：
          光用"离终点够近"会出大事 —— 用户这两段终点只相距 10px（104 vs 94），
          而 goal_reach_range 默认 15 > 10，两段终点的判定范围**重叠**，
          角色走到中间时两段都算"已到达" → 每帧来回切段（模拟实测 60 帧切了 60 次）。
          所以：① 按本段主方向判断"是否越过终点"；② 切过一次后要先**离开**终点
          才重新武装（边沿触发），避免重复触发。

        Returns:
            bool: 是否应该切到下一段（每进入一次终点范围只返回一次 True）
        '''
        if not self.img_routes or self.is_using_home_route:
            return False
        i = self.idx_routes
        if i >= len(self._seg_goals):
            return False
        g = self._seg_goals[i]
        if g is None or not self.loc_player_global:
            return False
        reach = self.cfg.get("route", {}).get("goal_reach_range", 15)
        # ⚠️ 上限一：不能超过本段跨度的一半：平台本来就不长时（用户路线跨度 14px），
        #    15px 的判定范围会导致"一起步就算到达终点"，这一段根本走不完。
        span = self._seg_span[i] if i < len(self._seg_span) else 0
        if span:
            reach = min(reach, max(3, int(span * 0.5)))
        # ⚠️★ 上限二（2026-09-13 实测修，**比上限一关键**）：
        #    reach 还不能超过「相邻两个终点间距」的一半减 1。
        #    为什么：两段是**交替**的 —— 段0(向右) 在 x ≥ g0-reach 触发，
        #    段1(向左) 在 x ≤ g1+reach 触发。若 reach 大到让 g0-reach ≤ g1+reach，
        #    两个判定区就重叠了：切到段1 后条件**恒为真**，而"离开"要求
        #    x > g1+reach 才重新武装 —— 角色正往左走，永远到不了 →
        #    **再也切不回段0，一路走出主线掉下平台**。
        #    实测就是这么坏的：g0=100 / g1=98 / reach=6 → 阈值变成
        #    "x ≥ 94" 和 "x ≤ 104"，把整个巡逻区都罩住了，段1 永远出不来。
        gap = getattr(self, "_seg_goal_gap", 0) or 0
        if gap:
            reach = min(reach, max(1, (gap - 2) // 2))
        # ⚠️★ 上限三（2026-09-16 真机实测修，**这条才是关键**）：
        #    上限二只保证"两个判定区不重叠"，但允许它们**贴在一起** ——
        #    gap=24 时 reach=11：段0 在 x ≥ 76-11 = 65 触发、段1 在 x ≤ 52+11 = 63 触发
        #    ⇒ 角色只在 **63~65（2px）** 里来回蹭。
        #    真机实测（2026-09-16 23:22 那次）：录了 24px 的路，日志里全局 x 只在
        #    62~66 之间动，看起来就像"卡在一个很小的范围里"。
        #
        #    ⚠️ 为什么上限一/二都没拦住：它们都建立在同一个**从没实测过的前提**上 ——
        #       "角色一帧能走十几像素"（见 __init__ 里那段注释，2026-09-12 写的）。
        #       2026-09-16 用真机日志实测：**~0.3 px/帧（≈4~5 px/秒）**，差了 20~50 倍。
        #       前提错 → 容差被设成 15 → 短路线被整条吞掉。
        #       实测依据（三处互相印证）：录制 7 秒走 ~32px；挂机 5 秒走 18px；
        #       用户自述"一个来回约 10 秒"（来回 48px）⇒ 都是 4~5 px/秒。
        #
        #    ⇒ 按 gap 的比例封顶，取 1/16、下限 1px：
        #      · gap=24（本图）→ reach=1 ⇒ 可用行程 22px（原来只有 2px）
        #      · gap=240 以上时 1/16 已 ≥ 配置值 15 ⇒ **长路线完全不受影响**
        #    用户 2026-09-16 定："不太需要担心角色掉坑的问题了，容差甚至可以缩到 1px"。
        #    ⚠️ 这条推翻了 2026-09-13 记下的旧前提（"巡逻范围无所谓、一切以不掉落为优先"），
        #       见下方 `_goal_armed` 那段注释里的说明。
        if gap:
            reach = min(reach, max(1, gap // 16))
        px, py = self.loc_player_global
        direction = self._seg_dir[i] if i < len(self._seg_dir) else None
        # ⚠️ 没有主方向时（这段 left/right 像素一样多，`_seg_dir` 会是 None）**不能**给 1px：
        #    那条分支只能靠"离终点够近"判定（没有方向感知的"越过"兜底），
        #    1px 等于要求踩点，角色一帧 0.3px 也可能判不到 → 永远不切段。
        #    那种情况至少留 3px。
        if direction is None:
            reach = max(reach, 3)

        # ★★ 2026-09-27 加：方向判定还要**满足"高度接近"**，否则会在
        #    「终点在高处（梯子顶 / 上层平台）」时只凭 x 接近就误判到达。
        #
        #   现场（用户 minimaps/废都南方工地，2026-09-27 23:45 真机）：
        #     段0 终点（内缩后）= **(180, 93)** —— 那是**梯子顶部**；
        #     而人行进在 **y=116 的地面**上，梯子竖线在 x=178。
        #     算得 reach=1 ⇒ 判据变成 `px <= 181` ⇒ **角色还在地面、离梯子还差约 3px
        #     就被判"段0走完"→ 切段 → 立刻掉头**。
        #     用户截图原话："差一点点就要碰到梯子了结果马上回头"，
        #     而它**根本没机会爬梯子**（终点在 y=93，却在地面 y=116 就被判到达）。
        #
        #   ⇒ 规则：方向判定额外要求 `|py - g_y| <= goal_reach_y_tol`。
        #     · 平坦路线（终点与人同高）Δy≈0 → **行为完全不变**；
        #     · 终点在高处（本图 Δy=23）→ 必须先真的爬上去（Δy 收进容差）才切段。
        #
        #   ⚠️★ 2026-09-27 由 12 下调到 3（用户真机实测：12 会在**半路**就切段）
        #     现场（minimaps/废都南方工地 的 ASCII 逐像素核对）：
        #       route1 终点 goal 在 **y=93**（高台上），梯子竖线从 y=109 到 y=93，
        #       地面在 y=116 ⇒ 全程爬升 **23px**。
        #       容差=12 时：角色爬到 **y=105（只爬了 11px、约一半）** 就满足
        #         `|105-93| = 12 <= 12` ⇒ 判"段0完成" ⇒ **切到 route2**；
        #       而 route2 在同一根梯子上标的是 **down**（y=95~103）
        #         ⇒ 于是角色**爬到一半掉头往下爬**（用户原话）。
        #     ⇒ 3 能覆盖正常路线的高度抖动（搜索框/质心 ±1~2px），
        #       又远小于本图 23px 的爬升 ⇒ 只有真爬到顶（y=96 以内）才切段。
        #
        #     ⚠️ 填 0 = 关掉本层（退回"只看 x"的老行为）。
        #     ⚠️ 已知代价：若某张图的终点确实在够不着的高度（跳不上去 / 爬不上去），
        #        角色会一直在该段打转而不切段 —— 那是**录制/地形问题**，
        #        比"到不了就掉头"更容易被发现和修。
        #        兜底：watchdog 的卡死检测（10s）会触发脱困，不会真的死住；
        #        若某张图确实切不了段，把 `route.goal_reach_y_tol` 往上调即可。
        #     可调：route.goal_reach_y_tol（默认 3）。
        try:
            _y_tol = int(self.cfg.get("route", {}).get("goal_reach_y_tol", 3) or 0)
        except (TypeError, ValueError):
            _y_tol = 3
        _same_height = (_y_tol <= 0) or (abs(py - g[1]) <= _y_tol)

        if direction == "right":
            passed = _same_height and px >= g[0] - reach
        elif direction == "left":
            passed = _same_height and px <= g[0] + reach
        else:
            passed = (abs(px - g[0]) + abs(py - g[1])) <= reach

        # ★★ 换段 → **无条件重新武装**（2026-09-13 逐帧实测修，别改回 `not passed`）
        #
        #   ❌ 老写法 `self._goal_armed = not passed` 的致命问题：
        #      角色一帧能走 1.5px（实测值），**切到新段的那一帧**角色往往已经站在
        #      新段的终点区里了（本图段0 终点 101、段1 终点 97，gap 只有 4）。
        #      于是 `not passed` = False = "这一段的终点已经消费过了" → 此后
        #      `passed` 恒为真、`if not passed: _goal_armed = True` 这条**永远进不去**
        #      → 再也切不回段0 → 一路走出平台掉下去。
        #
        #   逐帧实测（废都南方工地，两段，段0 向右 终点 (101,122) / 段1 向左 终点
        #   (97,122)，gap=4 → reach=1，平台 92~106）：
        #        f0  seg=0 x= 98.0 px= 98 dir=right passed=False armed=True  → FIRE=False
        #        f1  seg=0 x= 99.5 px=100 dir=right passed=True  armed=True  → FIRE=True（切到段1）
        #        f2  seg=1 x= 98.0 px= 98 dir=left  passed=True  armed=False → FIRE=False ← 死锁开始
        #        f3  seg=1 x= 96.5 px= 96 dir=left  passed=True  armed=False → FIRE=False
        #        …… f13 已经跑到 x=81.5，平台在 92~106，早就掉下去了
        #
        #   ✅ 正确语义：**进入新段后的第一个 passed=True 的帧就触发**，
        #      而不是"进入新段时若已在终点区内就当已经触发过"。
        #      改完后 f2 立刻 FIRE 切回段0，角色在 98~100 之间来回蹭 ——
        #      巡逻范围变小了，但用户明确：「一切以不掉落为优先」，宁可蹭 3px。
        #      ⚠️★ 2026-09-16 更新：这条前提**已被用户推翻** —— 他当天说
        #         "目前不太需要担心角色掉坑的问题了，容差甚至可以缩到 1px"。
        #         所以 `_near_seg_goal` 新增了上限三（reach ≤ gap/16，下限 1px），
        #         本图（gap=24）reach 从 11 降到 1 ⇒ 可用行程从 2px 变成 22px。
        #         「宁可蹭 3px」不再成立，改成"在保证不掉落的前提下尽量走满"。
        #         掉落的防线仍在：`endpoint_margin`（终点朝内缩，见 _calc_seg_goals_with）。
        _left = not passed
        if _left or getattr(self, "_goal_armed_seg", -1) != i:
            self._goal_armed_seg = i
            self._goal_armed = True
        if _left:
            return False                # 还没走到终点区 → 不切段（顺带重新武装）
        if not self._goal_armed:
            return False                # 刚切过、还没离开过 → 不重复触发
        self._goal_armed = False
        return True

    def _calc_pingpong_start(self):
        '''单段路线的"另一端"—— 往返走时用它判断"该掉头了"。

        路线是**单向**编码的（从 A 走到 B），只有终点 B 有 goal 标记，起点 A 什么都没有。
        所以 A 只能反推：取**离终点最远**的那个路线像素（对一条不折返的线成立）。
        '''
        self._pingpong_start = None
        if len(self.img_routes) != 1:
            return   # 多段靠 goal 切段循环，不需要这个
        img = self.img_routes[0]
        goal_codes = {c for c, v in self.color_code.items()
                      if v.strip().split()[-1] == "goal"}
        pts = self._route_pixels(img)
        goals = [(x, y) for x, y in pts if tuple(img[y, x][:3]) in goal_codes]
        if not pts or not goals:
            return
        gx = sum(p[0] for p in goals) // len(goals)
        gy = sum(p[1] for p in goals) // len(goals)
        self._pingpong_start = max(
            pts, key=lambda p: abs(p[0] - gx) + abs(p[1] - gy))
        logger.info(f"[巡逻] 只录了 1 段主线 → 启用自动往返"
                    f"（终点 ({gx},{gy}) ↔ 起点 {self._pingpong_start}）")

    def _pingpong_enabled(self):
        return (self.cfg.get("route", {}).get("pingpong_single_route", True)
                and len(self.img_routes) == 1
                and not self.is_using_home_route
                and self._pingpong_start is not None)

    def _apply_pingpong(self):
        '''单段路线的自动往返（2026-09-12 加）。

        为什么要它：只录了**一段**（A→B）时，`check_reach_goal` 的取模
        `idx_routes = (idx+1) % 1` 恒等于 0 —— 角色走到 B 之后仍然跟着同一段走，
        而 B 附近像素的指令还是"继续往 B"，于是**一路冲出平台**。
        （用户实测：启动后直奔右边不停。这比"卡在终点"更糟。）

        与其要求每个人都录"去 + 回"两段，不如单段时自动往返：
          · 正向走到 goal（终点 B）→ 掉头
          · 反向时把**左右指令取反**（路线是单向编码的，往回走必须反着读）
          · 走回起点 A 附近 → 再掉头

        ⚠️ 只反转 left/right：跳跃/上下/传送这类动作反向后语义不变，
           动它们会在台阶和梯子处出错。
        '''
        if not self.route_reverse:
            # 正向：走到终点 → 掉头
            if self.cmd_action == "goal":
                self.route_reverse = True
                self.cmd_action = "none"
                logger.info("[巡逻] 单段路线已到终点 → 掉头往回走")
            return

        # 反向：goal 对回程没意义（那意味着"又到终点了"），清掉免得站着不动
        if self.cmd_action == "goal":
            self.cmd_action = "none"
        if self.cmd_move_x == "left":
            self.cmd_move_x = "right"
        elif self.cmd_move_x == "right":
            self.cmd_move_x = "left"

        # 走回起点附近 → 掉头
        turn_range = self.cfg.get("route", {}).get("pingpong_turn_range", 15)
        d = (abs(self.loc_player_global[0] - self._pingpong_start[0])
             + abs(self.loc_player_global[1] - self._pingpong_start[1]))
        if d <= turn_range:
            self.route_reverse = False
            logger.info(f"[巡逻] 单段路线已回到起点（{d}px）→ 掉头继续正走")

    # ── 平台边缘**硬**保护（2026-09-13 用户点名要的功能）────────────────────
    # 用户原话：「没有平台边缘保护，挂机却会走出主线，掉到平台下面」。
    # 前面那些「终点内缩 / reach 封顶 / 切段重新武装」都是**软**保护 —— 它们靠
    # 状态机算得对不对。掉帧、坐标识别抖动、打怪位移都能让角色一帧冲出好几像素，
    # 软保护算得再准也追不上。所以再加一道**绝对地理围栏**：不管状态机怎么想，
    # 只要这一步会把角色带出「主线路线像素的 x 范围」，就把方向掰回来。

    def _edge_guard_rows(self):
        '''把全部主线路线图的**非 goal** 像素按行汇总成 {y: (xmin, xmax)}。

        为什么要按行存：围栏只看角色**当前这一行**附近的像素（见 _edge_guard_range）。
        地图常有上下两层平台，全局 x 范围会把下层平台的 x 也算进来 —— 那样角色
        走到上层边缘时围栏根本不生效，等于没保护。

        goal 圆点（半径 2 的黄色色块）**不参与**：它是画在终点上的，可能画到平台
        外面去，把它算进来等于把围栏放宽到平台外。

        结果按 img_routes 的 id 签名缓存 —— 每帧全图扫一遍太浪费（小地图 200×150
        ×2 张 ×10fps）。路线图换了才重算。
        '''
        # ⚠️ 一律用 getattr 兜底：本方法在**每帧**的指令计算链路上，
        #    离线自检的 Stub 未必有 img_routes / color_code 这些字段 ——
        #    少一个就抛 AttributeError 会把整帧指令搞崩（比不保护更糟）。
        imgs = [im for im in (getattr(self, "img_routes", None) or [])
                if im is not None]
        sig = tuple(id(im) for im in imgs)
        cached = getattr(self, "_edge_guard_rows_cache", None)
        if cached is not None and cached[0] == sig:
            return cached[1]

        goal_codes = {c for c, v in (getattr(self, "color_code", None) or {}).items()
                      if v.strip().split()[-1] == "goal"}
        rows = {}
        for img in imgs:
            for x, y in self._route_pixels(img):
                if tuple(img[y, x][:3]) in goal_codes:
                    continue            # goal 圆点不算，见上
                lo, hi, n = rows.get(y, (x, x, 0))
                rows[y] = (min(lo, x), max(hi, x), n + 1)
        self._edge_guard_rows_cache = (sig, rows)
        return rows

    def _edge_guard_range(self, py):
        '''角色所在这一行的围栏左右边界 (xmin, xmax)；该行像素太少 → None（不启用）。

        Args:
            py (int): 角色当前 y（小地图全局坐标）。

        Returns:
            tuple | None: (xmin, xmax)，或 None 表示这一帧不启用围栏。
        '''
        try:
            tol = int(self.cfg.get("route", {}).get("edge_guard_row_tol", 2))
        except (TypeError, ValueError):
            tol = 2
        tol = max(0, tol)
        lo = hi = None
        n = 0
        for y, (xl, xr, cnt) in self._edge_guard_rows().items():
            if abs(y - py) > tol:
                continue                # 只认角色**同一层**的像素（上下层平台不算）
            lo = xl if lo is None else min(lo, xl)
            hi = xr if hi is None else max(hi, xr)
            n += cnt
        # 这一行非 goal 像素 < 2 个：多半是角色在梯子上 / 空中 / 竖线楼梯上，
        # 此时"横向围栏"没有意义，开了只会误伤（把角色按在原地）。
        if lo is None or n < 2:
            return None

        # ── 安全余量：把围栏往**里**收 N 像素（2026-09-14 加）──────────────
        # 为什么需要：角色跳回主线时落点会偏 ±4~6px（跳跃保留水平速度，实测），
        #   窄平台上这点偏移足以把它送到安全区外、再走一步就掉下去。
        #   往里收一圈后，落点偏了也还在安全区内，且会被强制走回中间。
        #
        # ⚠️ 这是**围栏边界**的内缩，跟 `endpoint_margin`（**段终点**内缩、
        #   见 _calc_seg_goals_with）**不是一回事**，两者可以叠加、不冲突。
        #
        # ⚠️ 值必须按地形调：窄平台调大（更安全、巡逻范围变小），
        #   宽平台调 0（= 不收，老行为）—— 所以界面上要能直接改。
        try:
            inset = int(self.cfg.get("route", {}).get("edge_guard_inset", 0))
        except (TypeError, ValueError):
            inset = 0
        if inset > 0:
            lo2, hi2 = lo + inset, hi - inset
            if lo2 < hi2:          # 收完还站得住才收，别把角色锁死在一个点上
                lo, hi = lo2, hi2
        return lo, hi

    def _edge_guard_step(self, px):
        '''围栏要**往前看多少像素**才准（= 单帧步长 × 反应延迟帧数，至少 1）。

        为什么需要它：只看"当前 px 有没有出界"是不够的 —— 角色一帧能跨好几像素，
        等 px 真的贴到边界再掰方向，这一步已经跨出去了（离线模拟实测：速度 5.0
        时角色能一帧冲到 x=93，而平台边界是 94）。所以要用**实测步长往前看**。

        取近 5 帧最大值而不是"上一帧"：坐标识别本身有 ±1px 抖动，单帧位移会忽大
        忽小（真值 5 时可能量到 4 或 7），只看上一帧会低估、等于没往前看。
        只取 5 帧而不是永久最大值：被撞飞 / 传送那一下的巨型位移不能被永久记住，
        否则围栏会一直把角色按在平台正中间（巡逻范围被白白吃光）。
        下限 1：哪怕角色站着没动，也要留 1px 余量（站在 xmin 上不许再往左踩）。

        ── 反应延迟补偿（2026-09-13 加，这条很关键）────────────────────────
        原来只往前看**一帧**，但指令从「看到位置」到「按键生效」要穿过
        截图 → 主循环 → 键盘 三个线程，每个各占一帧。也就是说，围栏发出
        "掉头"指令时，角色还会继续沿**旧方向**走好几帧才真的转向。
        只看一帧 = 严重低估过冲 → 围栏形同虚设（10 FPS 时实测过冲 ~8.8px，
        而平台只有 14px 宽，必然冲出边缘）。
        所以再乘 `route.edge_guard_react_frames`（默认 3 = 三个线程各 1 帧）。
        '''
        last = getattr(self, "_edge_guard_last_px", None)
        hist = list(getattr(self, "_edge_guard_step_hist", []))
        self._edge_guard_last_px = px
        if last is not None:
            hist.append(abs(int(px) - int(last)))
        hist = hist[-5:]
        self._edge_guard_step_hist = hist
        step = max([1] + hist)
        # 反应延迟补偿：读不到配置就按 3 帧算（宁可多留余量，也不能漏保护）
        try:
            react = int(self.cfg.get("route", {}).get(
                "edge_guard_react_frames", 3))
        except (TypeError, ValueError):
            react = 3
        react = max(1, min(react, 10))
        return step * react

    def _apply_jump_brake(self):
        '''回正起跳前先**停稳**，让跳跃真的变垂直（2026-09-14 实测修）。

        治的是什么（用户观察 + 实测数据完全吻合）：
            回正线的设计假设是"**垂直起跳**" —— 角色走到 x=97~101（主线的正下方）
            原地一跳，正好落到 goal(98,122)。
            但实测角色是**跑着进来**的，跳跃会保留水平速度：
                从右边(110)过来 → 一路往左 → 在 x=100 起跳 → 带着向左速度 → 落到 96/92
                从左边(96)过来  → 一路往右 → 在 x=100 起跳 → 带着向右速度 → 落到 106/104
            **落点 = 起跳点 ± 4~6px**，一偏就贴到平台边缘，再走两步又掉下去
            （用户原话：「边移动边跳，所以落点本身就很靠近平台的边缘了」）。

        所以：拿到起跳指令时，如果角色**还在移动**，本帧先松开方向键让它停稳；
        等它停住了（或最多等 `home_jump_brake_frames` 帧，防卡死）再真正起跳。

        ⚠️ 为什么自己记位移，而不用围栏那套 `_edge_guard_step_hist`：
            回正期间 `_apply_edge_guard` 是**直接 return** 的（回正线本来就要往
            平台外走），那套历史根本不更新，拿来用永远是空的。
        '''
        if not getattr(self, "is_using_home_route", False):
            return                                  # 只在回正期间生效
        pg = getattr(self, "loc_player_global", None)
        if not pg:
            return
        try:
            px = int(pg[0])
        except (TypeError, ValueError, IndexError):
            return

        last_px = getattr(self, "_jump_brake_last_px", None)
        self._jump_brake_last_px = px

        if self.cmd_action != "jump":
            self._jump_brake_n = 0                  # 不是起跳帧 → 重置计数
            return
        if last_px is None:
            return                                  # 第一帧没有参照，先放行
        if abs(px - last_px) <= 0:
            return                                  # 已经停稳 → 正常起跳

        try:
            on = bool(self.cfg.get("route", {}).get("home_jump_brake", True))
        except Exception:                           # noqa: BLE001
            on = True
        if not on:
            return
        try:
            limit = int(self.cfg.get("route", {}).get("home_jump_brake_frames", 3))
        except (TypeError, ValueError):
            limit = 3
        n = int(getattr(self, "_jump_brake_n", 0) or 0) + 1
        self._jump_brake_n = n
        if n > max(1, limit):
            return                                  # 等太久了，别卡住，照跳
        # ★ 本帧不起跳：先松开方向键让角色停下，下一帧速度归零再跳
        self.cmd_move_x = "none"
        self.cmd_move_y = "none"
        self.cmd_action = "none"
        logger.info(f"[回正·起跳] 还在移动（上一帧 x={last_px} → 现在 {px}）"
                    f"→ 本帧先松开方向键停稳（第 {n} 次等待）")

    def _apply_recenter(self):
        '''回正成功后，先走回安全区**中心**再恢复正常巡逻（2026-09-14 实测加）。

        治的是什么（真机实测，用户原话「这一走就又掉下去了」）：
            角色跳回主线时会**横移约 4px**（`(100,126)→(96,122)`），
            而回正期间边缘保护是**故意关掉**的（回正线本来就要往平台外走）
            → 落点完全没人管。落到 106（安全区 94~104 之外）那次，
            **同一秒就又掉下去了**（日志 18:48:19 完整复现）。
            先走回中心，落地位置永远安全，也给后续巡逻留出到边界的距离。

        ⚠️ 为什么必须有**三条**退出条件（别把角色锁死）：
            ① 到了中心（±1px）→ 立刻交还正常巡逻；
            ② 量不出安全区（在梯子 / 空中 / 该行像素太少）→ 放弃归位；
            ③ 超过 `home_recenter_frames` 帧还没到 → 放弃归位。
            缺任何一条，角色都可能被按在原地 —— 那就是之前「围栏死区」
            把角色按死 10 秒、又被脱困指令踹下平台的同一个教训。

        Returns:
            bool: True = 本帧的左右指令已被归位接管。
        '''
        try:
            on = bool(self.cfg.get("route", {}).get("home_recenter", True))
        except Exception:                                    # noqa: BLE001
            on = True
        if not on or not getattr(self, "_recenter_active", False):
            return False
        pg = getattr(self, "loc_player_global", None)
        if not pg:
            return False
        try:
            px, py = int(pg[0]), int(pg[1])
        except (TypeError, ValueError, IndexError):
            return False

        # ③ 帧数上限（防被怪挡住 / 一直走不到 → 把角色锁死）
        try:
            limit = int(self.cfg.get("route", {}).get("home_recenter_frames", 30))
        except (TypeError, ValueError):
            limit = 30
        n = int(getattr(self, "_recenter_n", 0) or 0) + 1
        self._recenter_n = n
        if limit > 0 and n > limit:
            self._recenter_active = False
            self._recenter_n = 0
            logger.info(f"[回正·归位] 走了 {n} 帧还没到中心 → 放弃归位，交还正常巡逻")
            return False

        rng = self._edge_guard_range(py)
        if rng is None:
            # ② 量不出安全区：多半在梯子 / 空中，此时硬归位只会误伤
            self._recenter_active = False
            self._recenter_n = 0
            return False
        xmin, xmax = rng
        center = (int(xmin) + int(xmax)) // 2
        if abs(px - center) <= 1:
            # ★ 光"到中心"**不够**，还必须**真的停住**（连续两帧 x 相同）才交还控制权。
            #   （2026-09-15 真机实测加；与 try_return_to_mainline 的"连续两帧 y 相同"对称）
            #
            #   为什么必须加（真机实测 19:15 那次，27 秒里循环了 5 轮）：
            #     回正是**跑着跳**上来的，跳跃保留水平速度 → 落回平台后还会**滑 4~6px**。
            #     旧逻辑一到 ±1px 就交还控制权，可人还在滑 →
            #       从左边回正（往右跑着跳）→ 落点 100 → 滑到 **104** → 从右边缘掉下去
            #       从右边回正（往左跑着跳）→ 落点 98  → 滑到 **94**  → 从左边缘掉下去
            #     → 再从那边掉进坑、反向回正 → **来回死循环**。
            #     平台安全区只有 94~104（10px），而滑行量有 4~6px —— 交还得太早必然滑出去。
            #
            #   ⚠️ 加这条之后归位会一直生效到角色停下；一旦越过中心，下面那行
            #      `right if px < center else left` 会自动反向 → **正好起刹车作用**，
            #      把滑行吃掉。所以"停稳"是可达的，不会锁死（还有 30 帧上限兜底）。
            if getattr(self, "_recenter_last_px", None) == px:
                self._recenter_active = False
                self._recenter_n = 0
                self._recenter_last_px = None
                logger.info(f"[回正·归位] 已回到安全区中心 x≈{center}（当前 x={px}）"
                            f"且已停稳，恢复正常巡逻")
                return False
            self._recenter_last_px = px
            # ⚠️ 这里**故意不 return**：人还在动 → 保持归位生效、继续朝中心给方向
        self.cmd_move_x = "right" if px < center else "left"
        return True

    def _apply_edge_guard(self):
        '''最后一道防线：不许角色走出主线路线像素的 x 范围（2026-09-13 加）。

        在 update_cmd_by_route 里、`_apply_pingpong()` 之后调用（要等前面的
        往返 / 左右取反都改完，围栏才盖得住）。

        判定不是"贴到边界就掰"，而是"**再走一步会不会出界**"：
            can_left  = px - step >= xmin   （还能往左）
            can_right = px + step <= xmax   （还能往右）
        不能往左就往右，反之亦然；两边都不能（一步比平台还宽）时朝**中心**走 ——
        这时没有"安全方向"，只能选离边界更远的那侧，先把角色拉回平台中段。

        ⚠️ 必须跳过回正状态：回正线是**故意**要往坑外 / 平台外走的，围栏一插手
           就会和回正线的方向打架，角色被按在坑底上不来。
        '''
        # 开关一律 try/except 兜底：配坏了宁可**按默认开启**，
        # 也不能因为读配置抛异常把整帧指令搞崩（用户要的是"绝不掉落"）。
        try:
            enabled = bool(self.cfg.get("route", {}).get("edge_guard", True))
        except Exception:
            enabled = True
        if not enabled:
            return
        if getattr(self, "is_using_home_route", False):
            return                      # 走回正线时不干预，见上
        if not getattr(self, "loc_player_global", None):
            return
        if not (getattr(self, "img_routes", None) or []):
            return                      # 没有主线路线图 → 无从算围栏
        px, py = self.loc_player_global
        rng = self._edge_guard_range(py)
        if rng is None:
            return
        xmin, xmax = rng
        step = self._edge_guard_step(px)
        # ── ★ 封顶（2026-09-14 实测修，见 edge_guard_step_cap）────────────
        # 平台只有 10px 宽时，6px 的补偿量会让角色在中间**两边都不许走**：
        #   往左会到 93（出界）、往右会到 105（出界）→ 被按死在原地
        #   → 卡住 10s → 脱困随机指令（right down jump）把角色踹下平台。
        #   实测日志 18:28:34~53 完整复现了这条链。
        # 所以上限必须 **小于半宽**，保证任意位置至少有一个方向能走。
        # 顺带压住异常值（被怪撞飞量到 14px、×3 变 42px，接下来几帧会彻底锁死）。
        step = edge_guard_step_cap(step, xmax - xmin)
        # ⚠️ 用**严格**不等式（`>` / `<`，不是 `>=` / `<=`）：
        #    坐标识别有 ±1px 抖动，px 本身是"约数"。用 >= 等于默认 px 绝对准确，
        #    实测速度 5.0 + 抖动时角色正好从 px=99 一路左移到真实 x=93（边界 94）
        #    —— 踩空的正是这一像素的乐观。宁可把围栏收严 1px（用户：范围无所谓）。
        can_left = (px - step) > xmin
        can_right = (px + step) < xmax
        if self.cmd_move_x == "left" and not can_left:
            # 往左会出界：能往右就往右；两边都出界 → 朝中心走（px ≤ 中心就往右）。
            #
            # ⚠️ 正中线上的平局**必须有个确定选择**（`<=` 而不是 `<`）：
            #    实测速度 5.0 + 抖动时，角色真实 x=98、识别成 px=99（平台正中），
            #    一步 5px 时左右两边"看起来"一样危险 → 若走 `<` 分支会选中往左，
            #    角色一帧冲到 x=93（边界 94）掉坑；往右则是 x=103（在界内）。
            #    噪声下平局必须**钉死一个方向**，让它随机踩空等于没保护。
            if can_right or px * 2 <= xmin + xmax:
                self.cmd_move_x = "right"
                self._log_edge_guard(f"[边缘保护] 角色 x={px} 已到路线左边界({xmin})"
                                     f"（一帧约走 {step}px）→ 强制改向右")
        elif self.cmd_move_x == "right" and not can_right:
            if can_left or px * 2 >= xmin + xmax:
                self.cmd_move_x = "left"
                self._log_edge_guard(f"[边缘保护] 角色 x={px} 已到路线右边界({xmax})"
                                     f"（一帧约走 {step}px）→ 强制改向左")

    def _log_edge_guard(self, msg):
        '''围栏命中日志（1 条/秒节流）。

        为什么要节流：角色被按在边界上时会**每帧**命中 —— 主循环 10fps 就是
        每秒 10 条，真出问题时日志会被它刷屏，反而看不见别的信息（用户之前抱怨过
        日志看不清）。1 条/秒足够自证「围栏真的生效了」，又不刷屏。
        '''
        now = time.time()
        if now - getattr(self, "_t_last_edge_log", 0.0) < 1.0:
            return
        self._t_last_edge_log = now
        logger.info(msg)

    # ── 窄平台降级（2026-09-15，判据见 common.route_too_narrow_for_patrol）────
    def _patrol_degraded_now(self):
        '''本图是否"窄到不值得来回巡逻"。每帧算一次，存成 self._patrol_degraded。

        两处用它：
          · `update_cmd_by_route` —— 降级时不发左右移动（不来回蹭）
          · `is_player_stuck`     —— 降级且**画面里没有怪**时不判卡死
        '''
        try:
            min_gap = self.cfg.get("route", {}).get("patrol_min_gap", 24)
        except Exception:                                    # noqa: BLE001
            min_gap = 24
        return route_too_narrow_for_patrol(
            getattr(self, "_seg_goal_gap", 0) or 0, min_gap)

    def _log_patrol_degraded(self):
        '''降级生效 / 解除时各打一行（**只在状态变化时**打，不刷屏）。'''
        want = bool(getattr(self, "_patrol_degraded", False))
        if getattr(self, "_degraded_logged", None) == want:
            return
        self._degraded_logged = want
        if want:
            try:
                _th = self.cfg.get("route", {}).get("patrol_min_gap", 24)
            except Exception:                                # noqa: BLE001
                _th = 24
            logger.info(
                f"[窄平台降级] 本图相邻两段终点只差 "
                f"{getattr(self, '_seg_goal_gap', 0)}px（阈值 {_th}px）"
                f"→ 判定「这条路不值得来回巡逻」：不再左右蹭，站着面向怪打。\n"
                f"        附近有怪却不打时，仍然按「卡死」处理（会触发脱困指令）。\n"
                f"        想改判据：route.patrol_min_gap（填 0 = 关掉降级）。")
        else:
            logger.info("[窄平台降级] 已解除，恢复正常巡逻")

    def update_cmd_by_route(self):
        # ── 插入点①：**两线距离比较**（v0.9 第二版，必须放在函数最前面）─────────
        # 新判定是**坐标比较**，与"看不看得见主线"无关，所以必须放在最前面：
        # 哪怕这一帧主线像素找得到，只要人离回正线明显更近就要进回正。
        #
        # ★ 铁律：d_home / d_main **每帧只在这里算一次**，存实例字段；
        #   插入点②（超时 / 驻留 / try_return）**只读不算** ——
        #   一帧内出现"①说保持、②说退出"的口径分裂是最难查的 bug。
        _perf_t0 = time.perf_counter()
        self.home_d_main, self._main_seg_i = self._main_dist()
        self.home_d_home, self._home_line_near = self._home_dist()
        self._perf("回正距离", _perf_t0)

        # ── 窄平台降级：每帧先算一次（判据见 common.route_too_narrow_for_patrol）──
        # 放在这里 = 在任何 return 之前，保证同帧后面跑的 is_player_stuck
        # 读到的一定是本帧的值（它靠这个标志决定豁不豁免卡死判定）。
        self._patrol_degraded = self._patrol_degraded_now()
        self._log_patrol_degraded()

        if not self.is_using_home_route and self.img_route_home is not None:
            line = self._home_line_pick()
            if line is not None:
                # ⚠️ 用 _home_tol_now()（本帧实际生效的容差），**不要**自己读
                #    home_dist_tol —— 坑底会把它收成 0，自己读就会打出对不上的日志
                #    （2026-09-15 踩过：日志写"- 2"，实际按 0 算，看起来像 bug）。
                tol = self._home_tol_now()
                self._enter_home_route(
                    f"离回正线 {self.home_d_home}px < 离主线 {self.home_d_main}px - {tol}"
                    f"（回正线 #{line.get('id')}）", line)

        # ── 插入点②：回正状态下的帧计数 + 8s 超时出口 ─────────────────────────
        if self.is_using_home_route:
            self.home_frame += 1
            timeout = float(home_params(self.cfg)["home_timeout"])
            # ⚠️ 0 / 负数 → 视为"没配/配错"，**回落到默认 8s**（home_params 已兜住，
            #    这里再兜一层），而不是关掉超时出口。
            #    关掉出口的代价是：回正线接不上主线时，角色会**无限期**停在回正状态
            #    —— 回正期间不打怪，踩到终点后 goal 在键盘层又是 pass，等于
            #    「既不走也不打」且没有任何日志（QA-14）。这个"逃生口"弊大于利，砍掉。
            if timeout <= 0:
                timeout = float(DEFAULT_HOME_PARAMS["home_timeout"])
            if self.home_enter_t > 0 \
                    and time.time() - self.home_enter_t > timeout:
                # ⚠️ 加开关必须加出口：进了回正又不打怪，接不上时就是"既不走也不打"。
                #    超时后**该锚点去武装**，否则会变成「8s 回正 → 立刻重进」的死循环。
                # ── ★ 2026-09-14 实测修：超时那一刻，先补判「是不是其实已经回去了」──
                # 现象（真机日志 18:02:08 / 18:02:33 各一次）：小结写着
                #   「(96,126)→(92,122) | 离回正线 5px 离主线 **0px**」
                #   —— 角色**就站在主线上**（d_main=0，y=122 正是站立行），
                #   却照样走 8s 超时出口 → 判失败 → 回正线被去武装 30s，
                #   等于"明明回去了还要罚站 30 秒"，这期间再掉下去没人接。
                #
                # 成因：正常退出要求「连续两帧 y 相同」（防空中误判的 E3 硬闸，
                #   见 try_return_to_mainline），而角色**跳上去又掉下来**，
                #   y 在 122/126 之间反复（小结「期间 y 122~126」），
                #   2fps 下根本凑不齐连续两帧相同 → 8s 超时抢先触发。
                #
                # ⚠️ 判据为什么用 `d_main <= 1`：坑底 d_main=4（实测值）、
                #   站上主线 d_main=0，两者差得远，1px 足以区分，
                #   不会把"还在坑底"误判成"已经回去了"。
                _back_on_main = False
                try:
                    _dm = getattr(self, "home_d_main", None)
                    _back_on_main = (_dm is not None and float(_dm) <= 1)
                except (TypeError, ValueError):
                    _back_on_main = False

                if _back_on_main:
                    # 已经回去了 → **按成功处理**：不去武装、不上冷静期
                    self._exit_home_route(
                        f"回正超过 {timeout}s，但角色已经站在主线上"
                        f"（离主线 {self.home_d_main}px）→ 按成功处理，"
                        f"回正线保持可用")
                else:
                    self._exit_home_route(f"回正超过 {timeout}s 还没回到主线，"
                                          f"先交还给打怪逻辑", disarm=True)
                # ── 超时退出后**本帧直接交还给打怪逻辑**（QA-20 的另一半）────────
                # 光靠 _home_reenter_allowed 的冷静期只能保证"不会再被拉回回正"；
                # 但函数还会继续往下走到 get_nearest_color_code()，而此刻 img_route
                # 已被交回主线、角色离主线 > rescue_range → 取不到像素 → 命中
                # 「完全找不到路线像素」那条分支，把这一帧的指令清成 none。
                # 那跟这里 return 的结果一样，但**依赖了外面的分支**——脆弱。
                # 显式清成 none 再 return，语义才是"这一帧我就是不打不走了，交给打怪"：
                #   · 必须清成 none，不能留 jump / goal —— jump 在键盘层是真按键，
                #     goal 是 pass（空转），两者都会让角色原地做多余动作；
                #   · 必须 return，否则本帧后面的路线/往返逻辑会用**回正线的**
                #     旧 img_route 再算一次指令，把刚退出的状态又搅一遍。
                self.cmd_move_x = "none"
                self.cmd_move_y = "none"
                self.cmd_action = "none"
                # ⚠️ 只有**真没回去**才打这条告警 —— 人已经站在主线上就别再吓唬了
                #   （原来无条件打，日志里满屏"回正失败 + 建议重画"，其实人早回去了，
                #    差点误导到"重画回正线"上去）。
                if not _back_on_main and time.time() - self.t_last_home_fail_log > 10:
                    self.t_last_home_fail_log = time.time()
                    logger.warning(
                        f"[回正] 角色 {self.loc_player_global} 回正超过 {timeout}s 仍没接上主线 "
                        f"（离回正线 {self.home_d_home}px / 离主线 {self.home_d_main}px）"
                        f"—— 多半是这条回正线画短了/画偏了（终点没画到主线上，"
                        f"角色在下面够不着主线）。\n"
                            f"        本次先回去打怪；{home_params(self.cfg)['home_reenter_lock']:.0f}s 内"
                            f"不会再尝试回正、{home_params(self.cfg)['home_rearm_timeout']:.0f}s 内"
                            f"不会再被这条线接住。**建议重画这条回正线**：【🖊 手绘回正线】"
                            f"先沿坑底横着画一段，再竖着跳回主线上。")
                # 本帧到此为止（理由见上面那段注释）：打怪逻辑立刻接管这一帧。
                return
            elif not self._home_dwell_ok():
                # 最小驻留门没开：屏蔽一切"已回主线/已到终点"判定，防一进就出
                pass
            else:
                # 回正期间：优先尝试接回主线（理由见 try_return_to_mainline 注释）
                self.try_return_to_mainline()

        # get color code from img_route
        _perf_t0 = time.perf_counter()
        color_code, color_code_up_down = self.get_nearest_color_code()
        self._perf("路线搜索", _perf_t0)

        # ── 回正线：主线附近找不到路线像素 = 走丢了（掉落平台 / 被撞飞）──────────
        # 这时改用 route_home.png 走回主线；走到回正线末端的 goal 后，
        # 由 check_reach_goal() 把 is_using_home_route 置回 False，恢复主线巡逻。
        if color_code is None and color_code_up_down is None:
            # ⚠️ 冷静期判据（QA 实测必修）：上一次回正刚失败（超时 / 接不上）时，
            #    这里**必须**挡住 —— 超时退出后角色离主线还很远，本帧必定再次
            #    "找不到主线像素"，于是同一帧退出又进入，home_enter_t 归零，
            #    8s 超时与 15 帧驻留门全部失效 → 卡在回正里既不走也不打。
            if self.img_route_home is not None and not self.is_using_home_route \
                    and self._home_reenter_allowed():
                # 老兜底（P1-3，保留）：主线附近一个像素都找不到 = 走丢了 → 试回正线
                #
                # ★ 第二版新增的这道闸（QA-20 死锁的**新伪装**，必须加）：
                #   必须**先拿到一条已武装的回正线**才准进。
                #   为什么：这一版把「去武装」绑到了"线"上（D2）。角色卡在坑底、
                #   那条线刚被超时去武装时，`d_home` = None（没有可用线）→ 新判定
                #   `_home_line_pick()` 会正确地**不进**；但老兜底看的是"主线像素
                #   够不着"，跟 armed 完全无关 → 于是每帧都从这儿钻回回正状态：
                #     · `_enter_home_route` 每帧把 `home_enter_t` 重置成本帧 →
                #       8s 超时**永远**走不到（实测：最大连续回正 748/1290 帧）；
                #     · 回正期间不打怪 → 「既不走也不打」的 QA-20 死局原样复活。
                #   加了这道闸后：没有可用回正线 = 站着不动、由打怪逻辑接管（有出口）。
                _d_far, _ln = self._home_dist()
                # ★ ③（2026-10-14，用户 m09666 拍板）：**可达性闸** —— 只看"有没有
                #  这条线"是不够的：`_home_dist()` 量的是**全图**最近距离（跟
                #  `_main_dist()` 一样刻意不受 search_range / rescue_range 限制），
                #  而进来之后的路线搜索只有 search_range(10) / rescue_range(40) 的框
                #  ⇒ 线在 48~80px 外时，进来必然"一个像素都搜不到"⇒ 同一帧走
                #  `回正线接不上` 出口，还会**把这条线去武装**（要离开它的范围才重装）。
                #  真机证据（`log/MSBot_2026-10-14_16-51-02.log`，465s）：`[回正·小结]`
                #  8 条里 **6 条**是「0.0s/0帧 | 一次都没走到起跳点 | 离回正线
                #  22/48/78/78/80/48px、离主线 0px」——就是这种"进得去、走不了"的空转，
                #  它喂出了 14.2 次/分钟的 `[回正]`、3.4 次/分钟的 `[路线失联]`。
                #  ⇒ 要求"最近的回正线像素落在搜索框够得着的范围内"才准进。
                #  ⚠️ 半径口径必须与 `get_nearest_color_code()` 的救援半径**一致**
                #     （那边 rescue 只在 > search_range 时才生效 ⇒ 这里照抄同一条判据），
                #     否则又是一处"改一处漏一处"。
                _reach = self.cfg["route"].get("rescue_range", 40)
                try:
                    _reach = int(_reach)
                except (TypeError, ValueError):
                    _reach = 40
                if _reach <= int(self.cfg["route"]["search_range"]):
                    _reach = int(self.cfg["route"]["search_range"])
                if _ln is not None and _d_far is not None and int(_d_far) <= _reach:
                    self._enter_home_route(
                        f"主线附近找不到路线（走丢了），离回正线 {_d_far}px", _ln)
                    if self.is_using_home_route:
                        _perf_t0 = time.perf_counter()
                        color_code, color_code_up_down = self.get_nearest_color_code()
                        self._perf("路线搜索", _perf_t0)
                elif _ln is not None and time.time() - self.t_last_home_fail_log > 10:
                    self.t_last_home_fail_log = time.time()
                    logger.info(
                        f"[回正] 有可用回正线，但最近像素在 {_d_far}px 外"
                        f"（搜索 {self.cfg['route']['search_range']}px / "
                        f"救援半径 {_reach}px 够不着）⇒ **不进去**：进去也搜不到像素、"
                        f"当帧就会退出并把这条线去武装。\n"
                        f"        先站着让打怪逻辑接管（有出口）；人走近这条线附近"
                        f"会自动进去。")
            if color_code is None and color_code_up_down is None:
                # ── 出口：回正线接不上 → 立刻退出回正状态（2026-09-12 加）────────────
                # 「加开关必须加出口」。「回正期间不打怪」这个开关的前提是回正线能接上；
                # 接不上时它就从"保护回正"变成"锁死角色"：
                #     找不到像素 → 指令真空 → 又因为还在回正状态而不打怪
                #     → 既不走也不打，日志一切正常 = 极难查的死局（用户实测）。
                # 接不上说明**拿着这条线也走不回去**（掉进了没录过的坑 / 落点录歪了），
                # 那就不如退出回正状态，让 update_cmd_by_mob_detection 接管 —— 至少还能
                # 打怪。下一帧如果还在安全点外，会再试一次回正；一旦角色飘回回正线
                # 附近（能找到像素了），就正常走回去。
                # ⚠️ 这里**故意不加计时器/冷却**：找得到像素 = 能走 = 保持回正优先
                #    （用户要的"掉下去先回来再打怪"）；找不到 = 走不了 = 让位给打怪。
                #    一个 if 就说清了，不需要超时阈值那堆状态。
                if self.is_using_home_route:
                    # disarm=True：接不上时也让**这条线**去武装，避免"退出 → 下一帧又立刻进"
                    # 的反复横跳（回正期间是不打怪的，横跳 = 既不走也不打）。
                    self._exit_home_route("回正线接不上（附近一个回正线像素都没有）",
                                          disarm=True)
                    if time.time() - self.t_last_home_fail_log > 10:
                        self.t_last_home_fail_log = time.time()
                        logger.warning(
                            f"[回正] 角色位置 {self.loc_player_global} 附近找不到回正线像素"
                            f"（搜索 {self.cfg['route']['search_range']}px / "
                            f"救援 {self.cfg['route'].get('rescue_range', 40)}px 都够不着）"
                            f"—— 这条回正线覆盖不到这里，暂时放弃回正、先打怪。\n"
                            f"        多半是掉进了一个**没录过回正线**的坑。补录：站到这个落点上"
                            f"按 F6 起头，再走回主线按 F6 保存（可以叠加多条）；\n"
                            f"        或者用【🖊 手绘回正线】直接画一条（不用真的掉下去）。")
                # ⚠️ 完全找不到路线像素时**不能保持上一帧的方向键**（2026-09-12 实测）：
                #    那等于让角色一直朝最后一次的方向走 —— 短路线（平台就那么长）
                #    一冲出去就再也回不来，表现是「到终点停不下来一直往前走」。
                #    停下比冲出去安全：站着还能打怪，看门狗也会来救。
                self.cmd_move_x = "none"
                self.cmd_move_y = "none"
                if self.cmd_action not in ("goal", "attack"):
                    self.cmd_action = "none"
                return

        # Use color_code and color_code_up_down to complement each other
        # To prevent character stuck at the end of ladder, we use two color color pixels
        # and let them complement with each other, to ensure smoothy ladder climbing
        #
        # ★ 2026-09-27 加：**贴身跳跃优先**（issue 现场：用户「跳上梯子」的路线没反应）
        #   症状与根因（用用户的 route1.png 复刻引擎逻辑实测）：
        #     跳跃圆点画在梯子竖线的**右边 2~7px**（跳跃 (87~91,106~110)，
        #     梯子灰线 x=80 从 y=108 往下）。角色从左边走过来时：
        #       x=79~86 → 梯子 d=3→0，跳跃 d=9→2 ⇒ 判据选**梯子** → 一直 `none up none`
        #       x=87    → 跳跃 d=1，梯子 d=3      ⇒ 才轮到跳跃，但指令是 `right none jump`
        #     而梯子在**左边** —— 向右跳只会离梯子越来越远。
        #     ⇒ 结果：「走到梯子下一直按上（爬不上，入口偏高），等到发跳时方向已经错了」。
        #   为什么不能只靠"重录得远一点"：跳跃点必须贴着梯子才好用，
        #     画远了角色走过去会先被别的色码接走 —— 这是地形本身决定的。
        #   ⇒ 本层只做一件事：**当两者都近到"人正踩在上面"时，让跳跃赢**。
        #     判据用 `<=` 而非改全局距离口径，保证：
        #       · 人还在路上（梯子 d 明显更小）→ 行为完全不变，仍按梯子走；
        #       · 人已到跳跃点跟前（两者都 ≤ 2px）→ 才切到跳跃。
        #     `ladder_adjacent` 阈值 2 的依据：跳跃圆点半径 2px + 一格像素，
        #     人在这个范围内本来就同时"站在"两团像素上，此时方向由跳跃决定更符合录制意图。
        _PREFER_JUMP_RADIUS = 2
        if color_code and color_code_up_down:
            _cc_cmd = str(color_code.get("command", ""))
            _is_jump = "jump" in _cc_cmd
            _both_tight = (color_code["distance"] <= _PREFER_JUMP_RADIUS
                           and color_code_up_down["distance"] <= _PREFER_JUMP_RADIUS)
            # ★ 2026-09-27 加：**方向性跳跃要等走到梯子正下方再发**（治「跳太早摸不到梯子」）
            #   现场数据（用户 minimaps/废都南方工地/route1.png，角色沿 y=126 右行）：
            #     跳跃像素 x[78~84]、梯子(up)像素 x[81~84]
            #     x=78 时最近色码 = `right none jump`（d=0），梯子 d=4
            #     ⇒ 判据立刻发跳 ⇒ 人在 78、梯子在 81，**提前 3px 起跳**。
            #   为什么 3px 就够呛：跳跃**保留水平速度**（本文件 :3109 实测过），
            #     带着 right 起跳 ⇒ 落点比梯子更靠右 ⇒ 撞不到梯子；而且
            #     `key.jump_cooldown: 1.0` 会把随后"对准梯子那一跳"整段挡掉。
            #   ⇒ 规则：跳跃方向是 left/right（会横向漂移）+ 附近有梯子 + 尚未对齐
            #      → **先只走路**（保留方向、去掉 jump），对齐后交回原逻辑。
            #     ⚠️ **原地跳（move_x == "none"）不受影响** —— 那正是"站在梯子上跳"
            #        用的动作，动了反而会让爬梯失效。
            #     可调：route.jump_align_tol（默认 2，填 0 = 关闭本层）。
            try:
                _jtol = int(self.cfg.get("route", {}).get("jump_align_tol", 2) or 0)
            except (TypeError, ValueError):
                _jtol = 2
            _jparts = _cc_cmd.split()
            _jmx = _jparts[0] if _jparts else "none"
            _lad_px = color_code_up_down.get("pixel")
            # ★★ 2026-09-27 加：**跳跃时若附近有梯子，就同时按住"上"**
            #
            #   用户对爬梯机制的解释（2026-09-27 原话）：
            #     "能不能抓住梯子不看落点，而看角色的抛物线和梯子重合的那个点，
            #       角色是不是按住 up 了"
            #     "边走边跳肯定不光是跳+上，还得带个方向键"
            #   ⇒ 爬梯动作 = **方向 + 跳 + 上** 三键齐发。起跳后走抛物线，
            #     只要"上"一直按着，抛物线扫过梯子那一列时角色**自动扒住** ——
            #     不需要精确控制落点。
            #
            #   改前为什么抓不住：补 up 的条件是 `self.is_on_ladder`（见下面两处），
            #     而**起跳瞬间人在空中/地面，is_on_ladder 为假** ⇒ 跳跃全程不按上
            #     ⇒ 抛物线扫过梯子也抓不住。
            #     真机症状（用户描述）："边走边跳爬梯子时没扒住梯子，也就是跳过了，
            #     角色会继续往前走，而不会重试继续爬梯子"。
            #
            #   ⚠️ 只在"扫到了梯子像素"时才补 up（_lad_px is not None）：
            #     用户确认"没有梯子的地方按跳+上不会变高、也不影响普通跳跃（跨缺口）"，
            #     但仍按**最小影响**原则限定，避免动到普通跳跃。
            #   ⚠️ 填 0 = 关掉本层：route.jump_hold_up
            try:
                _jump_hold_up = int(self.cfg.get("route", {}).get("jump_hold_up", 1) or 0)
            except (TypeError, ValueError):
                _jump_hold_up = 1
            #   ⚠️★ 2026-09-28 修「沿高台走路时自动爬上路过的梯子」：
            #     本标志**必须**同时要求 `_is_jump`（当前动作是跳跃）。
            #     曾经漏了这个条件（只判 `_lad_px is not None`）⇒ 只要附近有梯子
            #     像素它就是 True ⇒ 沿高台往右走时（`right none none`）也被补上 up
            #     ⇒ 变成 `right up none` ⇒ **路过梯子就自动爬上去**
            #     （用户真机现场："从梯子终点往右走一两个像素有个往上的梯子，
            #       路过时按着 up 就自动爬上去了，爬到顶再从右边悬崖掉下来"）。
            #   ⇒ 语义收窄为：**只在"跳跃那一帧"才补 up**（爬梯靠抛物线扒住），
            #     纯走路时不补 —— 走路补 up 会误触发沿途的梯子。
            _hold_up_on_jump = (_jump_hold_up > 0 and _is_jump and _lad_px is not None)
            # ⚠️★ 2026-09-27 **撤回过一条规则**，别再加回来：
            #   曾在此处加过「人在梯子上（is_on_ladder）→ 一律不跳、改按梯子方向」，
            #   想治"爬上去又跳下来"。**真机实测证明它是错的**（用户 23:49）：
            #     is_on_ladder 的判据是「横向几乎不动 + 纵向在动」（:4125-4130），
            #     而**角色起跳时纵向就在动** ⇒ 跳跃途中被误判成"在梯子上"
            #     ⇒ 该规则把跳跃换成按上 ⇒ 角色**到起跳点就站着按上、再也不跳**。
            #     日志现场：`帧306 全局(182,116) 指令(none up none)`（在起跳点却不跳）。
            #   ⇒ 教训：`is_on_ladder` **不能**用来判断"真的在爬梯"，
            #     它在跳跃过程中同样为真。想区分"爬梯 vs 跳跃"必须另找依据。
            if _is_jump and _jtol > 0 and _jmx in ("left", "right") \
                    and _lad_px is not None \
                    and abs(int(_lad_px[0]) - int(self.loc_player_global[0])) > _jtol:
                # 还没和梯子对齐 → 先走过去（保留方向，去掉跳跃）
                self.cmd_move_x, self.cmd_move_y, self.cmd_action = _cc_cmd.split()
                self.cmd_action = "none"
                # ── 埋点：跳跃被"还没对齐"挡掉（2026-09-28 加，纯观测）──────────
                # 为什么必须有：这一层会把跳跃**静默**吞掉（cmd_action 直接变 none，
                # 键盘层根本走不到 jump 分支 ⇒ 连「跳跃冷却挡住」那条日志都不会有）。
                # 于是"角色踩在跳跃像素上却不跳"在日志里**完全没有痕迹** ——
                # issue #6 用户报的「不跳」就卡在这个盲区上。
                # 这里只累计 + 节流告警；累计到阈值说明"一直没对齐"，
                # 多半是跳跃点离梯子太远（录制问题），给出可操作提示。
                _n = int(getattr(self, "_jump_align_suppress", 0) or 0) + 1
                self._jump_align_suppress = _n
                if _n == 90 and jump_trace_on(self.cfg):
                    _dxg = abs(int(_lad_px[0]) - int(self.loc_player_global[0]))
                    logger.warning(
                        f"[跳跃被挡] 踩在跳跃像素上、但和梯子的横向还差 "
                        f"{_dxg}px（容差 {_jtol}px）—— 已经连续 {_n} 帧"
                        f"「只走不跳」。\n"
                        f"        若角色一直走不到梯子正下方，就会**永远不跳**。\n"
                        f"        怎么修：把跳跃点录得离梯子更近一点（重录时贴着梯子按跳），"
                        f"或放宽 route.jump_align_tol（当前 {_jtol}，填 0 = 关掉本层）。")
            elif _is_jump and _both_tight:
                # 贴身且最近的是跳跃 → 用跳跃。
                #
                # ⚠️★ 方向键要按**梯子在哪边**来定，不能照抄色码里的方向：
                #   现场数据（route1.png）：跳跃像素是 `right none jump`（录的时候
                #   人是边向右走边跳的），可梯子（灰像素竖线）在跳跃点的**左边**。
                #   照抄 `right` ⇒ 角色向右跳，离梯子越来越远，症状依旧是"跳不上去"。
                #   而跳跃像素的左右方向**没有语义**：色码表里 jump 只有
                #   left/right/down/none 四种组合、没有 up，录制器对"跳+上"
                #   只能退成 `left/right none jump` —— 这个方向纯属录制时的副产品。
                #   ⇒ 用梯子像素相对角色的方位来定：梯子在左就按左，在右就按右，
                #     同列（|dx| ≤ 1）才真的原地跳（none）。
                self.cmd_move_x, self.cmd_move_y, self.cmd_action = _cc_cmd.split()
                _up_px = color_code_up_down.get("pixel")
                if _up_px is not None:
                    # 同列（差值 ≤1）不给方向：人在梯子正下方，直接原地起跳最稳。
                    # 给方向会让角色在空中横移，落点偏出梯子 —— 这正是
                    # "跳上平台又掉下去"的成因之一。
                    _dx = int(_up_px[0]) - int(self.loc_player_global[0])
                    if _dx <= -2:
                        self.cmd_move_x = "left"
                    elif _dx >= 2:
                        self.cmd_move_x = "right"
                    else:
                        self.cmd_move_x = "none"
                _cmd_ud = str(color_code_up_down.get("command", "")).split()
                # ★ 跳跃 + 附近有梯子 → 同时按住"上"（见上方 _hold_up_on_jump 的说明）。
                #   这是"抛物线与梯子重合时能扒住"的关键 —— 不再依赖 is_on_ladder。
                if len(_cmd_ud) == 3 and self.cmd_move_y == "none" \
                        and (self.is_on_ladder or _hold_up_on_jump):
                    self.cmd_move_y = _cmd_ud[1]
            elif color_code["distance"] < color_code_up_down["distance"]:
                self.cmd_move_x, self.cmd_move_y, self.cmd_action = color_code["command"].split()
                _, cmd, _ = color_code_up_down["command"].split()
                # ★ 同上：跳跃时也补上（原来只在 is_on_ladder 时补，起跳时补不上）
                if self.cmd_move_y == "none" and (self.is_on_ladder or _hold_up_on_jump):
                    self.cmd_move_y = cmd # only complement cmd_move_y when player is on ladder
            else:
                # ★ 2026-09-17 防"地面伪灰"（用户报「路过管道一直往右走」的根因）。
                # up 像素和角色**同 y**（站在地面层）且**同列往上 3px 内没有第二个 up 像素**
                # → 它不是真管柱的起点（真管柱一定有连续 up 像素）。角色按 up 也爬不上去，
                # 改成走 color_code（多半是"right"，让角色先走到管柱下方再爬）。
                #
                # ⚠️★ 2026-09-27 修崩溃（issue #2 用户日志实证）：
                #    这层检测是**拿回正线当参照**判断的（真管柱在 route_home.png 上有连续像素），
                #    而 `img_route_home` 在**没录回正线时是 None** —— 旧代码直接
                #    `self.img_route_home[_up_y-d, _up_x]` 取下标 ⇒
                #    `TypeError: 'NoneType' object is not subscriptable`，
                #    **每帧抛一次、主循环停摆**（用户症状：「移动可以执行，不攻击」）。
                #    ⇒ 把「有回正线」作为前置条件：没有它就没法做这层验证，
                #      退回不做伪灰检测的老行为（直接按 up 走）——宁可偶尔误判，
                #      也好过整条挂机流程崩掉。
                _up_x = color_code_up_down["pixel"][0]
                _up_y = color_code_up_down["pixel"][1]
                _can_check_fake = self.img_route_home is not None
                # ★ 2026-10-14 追加（用户报「不上楼梯、从绳梯旁直接走过去」）：
                #   判"真管柱"时**同时看当前段路线图**（`self.img_route`）。
                #   旧版只看回正线 —— 废铁2 的 route_home.png 里竖向像素**只有
                #   x=70..71 / y=80..88 这 9 个**，于是除那一列以外，凡是"up 像素
                #   正好落在角色这一行"的真绳梯都被判成地面伪灰 ⇒ 引擎照抄横向
                #   命令（right）⇒ 角色从绳梯脚下走过去、全程不按 up
                #   （真机 17:22 场逐帧复刻：x=162..171 一路 right，直到 x=179
                #   才轮到 up；而真绳梯就在 x=163）。
                #   判据：同列 ±1px、上下 3px 内有"竖向色码"（up/down）= 真绳梯/管柱
                #   —— 与 09-17 的原始设计一致（真管柱一定有连续竖向像素）。
                #   取不到（桩不全 / 图不可用）就退回旧判断（fail-closed）。
                _route_vert = False
                try:
                    _img_r = self.img_route
                    if _can_check_fake and _up_y == self.loc_player_global[1] \
                            and _img_r is not None:
                        _vcodes = set(self.color_code_up_down.keys())
                        for _ck, _cmdv in self.color_code.items():
                            _p3v = str(_cmdv).split()
                            if len(_p3v) == 3 and _p3v[1] in ("up", "down"):
                                _vcodes.add(_ck)
                        _rh, _rw = _img_r.shape[:2]
                        _route_vert = any(
                            tuple(int(v) for v in _img_r[_up_y-d, _up_x+dx][:3]) in _vcodes
                            for dx in (-1, 0, 1) for d in (-3, -2, -1, 1, 2, 3)
                            if 0 <= _up_y-d < _rh and 0 <= _up_x+dx < _rw)
                except Exception:                                    # noqa: BLE001
                    _route_vert = False
                _fake = (_can_check_fake
                         and _up_y == self.loc_player_global[1]
                         and not _route_vert
                         and (_up_y - 3 < 0
                              or not any(tuple(int(v) for v in self.img_route_home[_up_y-d, _up_x][:3])
                                          in {(127, 127, 127), (255, 255, 127)}
                                          for d in (-3,-2,-1,1,2,3))))
                if _fake and color_code is not None:
                    self.cmd_move_x, self.cmd_move_y, self.cmd_action = (
                        color_code["command"].split())
                else:
                    self.cmd_move_x, self.cmd_move_y, self.cmd_action = (
                        color_code_up_down["command"].split())
                    cmd, _, _ = color_code["command"].split()
                    if self.cmd_move_x == "none":
                        if self.is_on_ladder:
                            # 原有：真在爬 ⇒ 用路线的横向命令把角色稳在绳子上
                            self.cmd_move_x = cmd  # only complement cmd_move_x when player is on ladder
                        else:
                            # ★ 2026-10-14 绳梯脚下"蹭一下"自救（用户拍板 D）——
                            #   判据/方向/取值全在 `_rope_nudge_dir()` 与
                            #   config_default.yaml 的 route.rope_nudge_* 长注释里；
                            #   本块只负责"问一次 + 蹭 + 留痕"。
                            _ud_cmd = color_code_up_down["command"].split()
                            _nd = self._rope_nudge_dir(
                                _up_x, _ud_cmd[1] if len(_ud_cmd) == 3 else "none")
                            if _nd:
                                self.cmd_move_x = _nd
                                self._rope_nudge_n = int(
                                    getattr(self, "_rope_nudge_n", 0)) + 1
                                if time.time() - getattr(self, "t_rope_nudge_log", 0.0) > 1.5:
                                    self.t_rope_nudge_log = time.time()
                                    _age_ms = int((time.time() - float(
                                        getattr(self, "t_ladder_move", 0.0) or 0.0)) * 1000)
                                    logger.warning(
                                        f"[绳梯自救] 路线要 {_ud_cmd[1]}、已经在绳梯脚下"
                                        f"按了 {_age_ms}ms 没往上走 → 往 {_nd} 蹭一下"
                                        f"（左右交替、up 一直按着；"
                                        f"`route.rope_nudge_s` 填 0 = 关掉本层）")
        elif color_code:
            self.cmd_move_x, self.cmd_move_y, self.cmd_action = color_code["command"].split()
        elif color_code_up_down:
            self.cmd_move_x, self.cmd_move_y, self.cmd_action = color_code_up_down["command"].split()

        # ── 记下"路线最近一次给的竖向指令"（爬梯补竖向时要用，见
        #    `_ladder_finish_tick()`）—— 只读一支，不改任何行为 ────────────────
        if str(self.cmd_move_y) in ("up", "down"):
            self._last_vert_cmd = str(self.cmd_move_y)

        # ── P0（2026-10-14，用户 m10830）：爬绳超时 ⇒ 本帧不再按 up/down ──────
        # 判据与状态机全在 `_rope_ladder_giveup_tick()`（那边唯一写状态），
        # 这里只做一件事：把竖向压成 none。**只压竖向** —— 横向/动作仍由路线与
        # 怪逻辑照旧决定（不许顺手把整条指令清掉，那会让角色在绳脚定住不动）。
        # 目的：那一秒不再死按方向键顶着绳（真机最长 211s、发键 90~96 次/5s）。
        if (str(self.cmd_move_y) in ("up", "down")
                and not self.is_on_ladder
                and self._rope_giveup_now()):
            self.cmd_move_y = "none"

        # ── 爬梯"走完"（2026-10-15 加，用户 m11669）──────────────────────────
        # 判据与状态机全在 `_ladder_finish_tick()`（那边唯一改状态），这里只做一件事：
        # **把竖向补上**。为什么：梯子上按左右是不动的（人还挂在绳上），而路线这帧
        # 可能只给横向像素 ⇒ 键一直在发、人不动（真机 26 分钟里 (210,60) 停 30s）。
        # 补上之后 `_ladder_yield()` 的"路线给 up/down ⇒ 始终让路"自然生效 ⇒
        # 怪逻辑全程静音（用户原话"不要检测怪了"）。
        _ladder_push = self._ladder_finish_tick()
        if _ladder_push:
            self.cmd_move_y = _ladder_push

        # ── 窄平台降级：把路线给的**左右移动**吞掉（见 _patrol_degraded_now）──
        # 只吞 cmd_move_x，保留 cmd_move_y / cmd_action —— 改动面最小。
        # ⚠️ 放在这里是安全的：update_cmd_by_mob_detection 在后面跑，它还会按
        #    「面向最近的怪」重新设 cmd_move_x，所以降级**不影响转身打怪**。
        # ⚠️★ 回正期间**绝不降级**（2026-09-15 补，跟围栏同一个道理）：
        #    回正线本来就要左右走，吞掉它角色就**走不回主线**。
        #    `_apply_edge_guard` 里也有同样的 `is_using_home_route` 短路。
        #    漏了这条会被 verify_home_route_qa / qa2 直接抓到
        #    （"起点 (94,128)：走回主线并退出" 失败）—— 本次就是这么发现的。
        if (getattr(self, "_patrol_degraded", False)
                and not self.is_using_home_route):
            self.cmd_move_x = "none"

        # replace teleport to jump if user doesn't set teleport key
        # （路线颜色编码里仍可能有 teleport 动作，所以这个降级判断保留）
        if self.cfg["key"]["teleport"] == "" and self.cmd_action == "teleport":
            self.cmd_action = "jump"

        # 本帧真的发出了跳跃 ⇒ 清掉"跳跃被挡"的累计（见 _jump_align_suppress）
        if self.cmd_action == "jump":
            self._jump_align_suppress = 0

        # ── 回正：起跳前先**停稳**，让跳跃真的变垂直（见 _apply_jump_brake）────
        self._apply_jump_brake()

        # 单段路线走到头自动掉头（否则会一路冲出平台，见 _apply_pingpong）
        if self._pingpong_enabled():
            self._apply_pingpong()

        # ── 回正成功后：先走回安全区中心，再正常巡逻（见 _apply_recenter）────
        # 放在 pingpong 之后、边缘围栏之前：归位给的是"朝中心走"，
        # 本身就在安全区里，围栏不会拦它，也不会被 pingpong 再翻回去。
        self._apply_recenter()

        # ── 最后一道防线：平台边缘硬围栏（见 _apply_edge_guard 的说明）────────
        # 必须放在**所有**方向改动之后（含 _apply_pingpong 的左右取反），
        # 否则 pingpong 会在围栏之后再把方向翻回去，围栏白干。
        self._apply_edge_guard()

        # ── 埋点：回正期间记"走到起跳点几次"（放最后 = 拿到的是最终指令）────
        if getattr(self, "is_using_home_route", False):
            self._trace_home_cmd()

    def _trace_home_cmd(self):
        '''回正期间每帧记一次状态（2026-09-14 排查「掉坑回不来」的埋点，纯观测）。

        做两件事，**不判定、不改任何指令**：
          ① `cmd_action` 变成 jump 的**上升沿** → 记一次「走到起跳点」（带坐标、距上次间隔）
          ② 累积回正期间角色 y 的区间 → 小结里用来看「跳没跳上去」

        ⚠️ 为什么只记**上升沿**而不是每帧：角色站在一片 jump 像素上时，
           每帧的 cmd_action 都是 jump —— 30fps 下站 1 秒就是 30 条一模一样的
           日志，会把「进入 / 退出回正」这些真正有用的行全淹掉。
           而"走到起跳点**几次**"这个计数才是区分病因的关键，上升沿天然就等于次数。

        ⚠️ 所有状态一律 `getattr` 兜底：tools/verify_navigation.py 的 Stub
           **不走 __init__**，直接 self.xxx 会让现有 19 项自检全崩（踩过的坑）。
        '''
        if not jump_trace_on(self.cfg):
            return
        # ── ② 期间 y 的区间 ────────────────────────────────────────────
        try:
            py = int(self.loc_player_global[1])
        except (TypeError, ValueError, IndexError):
            py = None
        if py is not None:
            y0 = getattr(self, "home_y_min", None)
            y1 = getattr(self, "home_y_max", None)
            self.home_y_min = py if y0 is None else min(y0, py)
            self.home_y_max = py if y1 is None else max(y1, py)

        # ── ① 走到起跳点的上升沿 ───────────────────────────────────────
        on = (self.cmd_action == "jump")
        was_on = bool(getattr(self, "_home_jump_on", False))
        self._home_jump_on = on
        if not (on and not was_on):
            return
        n = int(getattr(self, "home_jump_n", 0) or 0) + 1
        self.home_jump_n = n
        ts = list(getattr(self, "home_jump_ts", None) or [])
        now = time.time()
        gap = (now - ts[-1]) if ts else None
        ts.append(now)
        self.home_jump_ts = ts
        logger.info(
            f"[回正·起跳] 第 {n} 次走到起跳点 位置 "
            f"{tuple(self.loc_player_global) if self.loc_player_global else '?'} "
            f"指令({self.cmd_move_x} {self.cmd_move_y} {self.cmd_action})"
            + (f"，距上次 {gap:.2f}s" if gap is not None else ""))

    def _ladder_still_s(self):
        '''
        「真的在爬」的竖向进展窗口（秒，`route.ladder_still_s`）—— **只有这一处读**。

        返回 0.0 表示**这条判据不可用** ⇒ 调用方 fail-closed，退回老行为
        （旗标为真就一直让路）。两种情形：
          · 配置填 0 / 负数 / 读不到 / 畸形（用户明确要退回老行为）；
          · 小地图全局坐标读不到（`loc_player_global` 取不到 ⇒ 永远测不到
            竖向进展 ⇒ 若照常让路会一路不让，把"打几下"变成"吊在绳子上"，
            那是 2026-10-13 真机实测过的更坏的故障）。

        由来（用户 m08016 拍板 A「梯子让路收紧」，依据 = 2026-10-14 示范录制）：
          让路块 `_climbing = bool(self.is_on_ladder) or cmd_move_y in (up, down)` 里，
          前一项的旗标**只进不出** ⇒ 示范录制里梯上帧 2749（43.9%），其中
          44/105 段整段全局 y 零变化（粘滞误判，1048 帧），人在这些帧里出手 910 次
          全被让路吃掉；而路线真在爬的只占 674/2749。
        '''
        try:
            _raw = (self.cfg.get("route") or {}).get("ladder_still_s", 0.7)
        except Exception:                                     # noqa: BLE001
            _raw = 0.7
        if _raw is None:
            _raw = 0.7
        try:
            _v = float(_raw)
        except (TypeError, ValueError):
            _v = 0.7
        if _v <= 0 or _v > 30:        # 非法 / 明确关掉 ⇒ 本判据不可用
            return 0.0
        if getattr(self, "loc_player_global", None) is None:
            return 0.0                # 拿不到全局坐标 ⇒ 测不到竖向进展
        return _v

    def _rope_stuck_s(self):
        '''
        「爬了这么久还没上去，就不再死等」的窗口（秒，`route.rope_stuck_s`）——
        **只有这一处读**。

        返回 0.0 表示**本层关掉**（用户明确填 0/负数，或值明显写错 >120，或拿不到
        小地图全局坐标 ⇒ 测不到竖向进展、不该猜）。

        由来（用户 m10830「把'人眼的实时反应'拆解成'适合低频检测的确认流程'」）：
          真机取证（`log/MSBot_2026-10-14_17-40-51.log` 段11，152 分钟）——
          >10s 的打怪空档 105 次合计 2726s（占 30%），其中 **104 次含 `[绳/梯]`
          事件**；**87 次（1540s = 会话的 17%）角色"几乎没动"**（净位移 ≤8px），
          而这 87 次里"发键 0 次"的采样是 **0/317**（键一直在发、90~96 次/5s）——
          即**一直按着 up 顶着绳/墙，同时怪逻辑被 `_ladder_yield()` 整段静音**。
          最长一次 211 秒。人眼在这种场景里的做法是"看一眼、够不着就干别的去"，
          不是"盯着一根上不去的绳子按 211 秒"。
        '''
        try:
            _raw = (self.cfg.get("route") or {}).get("rope_stuck_s", 8.0)
        except Exception:                                     # noqa: BLE001
            _raw = 8.0
        if _raw is None:
            _raw = 8.0
        try:
            _v = float(_raw)
        except (TypeError, ValueError):
            _v = 8.0
        if _v <= 0 or _v > 120:       # 非法 / 明确关掉 ⇒ 本层不可用
            return 0.0
        if getattr(self, "loc_player_global", None) is None:
            return 0.0                # 拿不到全局坐标 ⇒ 测不到竖向进展
        return _v

    def _rope_giveup_now(self):
        '''本帧是否处在「这根绳判定上不去、暂停爬绳」的窗口里（**纯读**，不改状态）。'''
        return time.time() < float(getattr(self, "_rope_giveup_until", 0.0) or 0.0)

    def _rope_ladder_giveup_tick(self):
        '''
        爬绳 give-up 的状态机 —— **只有这一处写状态**；返回 True = 本帧在暂停爬绳。

        判据：路线要求 up/down 已持续 ≥ `rope_stuck_s` 秒，**且**同期小地图全局 y
        没有任何竖向进展（`t_ladder_move` 不新鲜）⇒ 判定"这根绳上不去"，进入暂停
        窗口 `rope_stuck_s` 秒。窗口里：
          · `_ladder_yield()` 不再让路 ⇒ **怪逻辑恢复**（这是用户 m10830 取证里
            占会话 17% 那一项的正解）；
          · `update_cmd_by_route` 把 up/down 压成 none ⇒ 不再死按着方向键。
        一旦真的出现竖向进展（`t_ladder_move` 新过窗口起点）⇒ 立刻取消暂停，
        把控制权立刻还给路线（不许"人已经在爬了、怪逻辑还回来抢"）。
        '''
        _now = time.time()
        _stuck = self._rope_stuck_s()
        if _stuck <= 0:                       # 本层关掉 ⇒ 清窗口，退回老行为
            self._rope_giveup_until = 0.0
            return False
        _last = float(getattr(self, "t_ladder_move", 0.0) or 0.0)
        _give = float(getattr(self, "_rope_giveup_until", 0.0) or 0.0)
        if _now < _give:
            if _last > float(getattr(self, "_rope_giveup_stamp", 0.0) or 0.0):
                self._rope_giveup_until = 0.0     # 有进展 ⇒ 立刻取消暂停
            else:
                return True
        _cmd_now = str(self.cmd_move_y)
        if _cmd_now not in ("up", "down"):        # 路线没在要求爬 ⇒ 重新起表
            self._rope_req_t0 = 0.0
            self._rope_req_cmd = ""
            return False
        _t0 = float(getattr(self, "_rope_req_t0", 0.0) or 0.0)
        if _t0 <= 0 or str(getattr(self, "_rope_req_cmd", "")) != _cmd_now:
            self._rope_req_t0 = _now              # 新的一次爬绳尝试，重新计时
            self._rope_req_cmd = _cmd_now
            return False
        _age_req = _now - _t0
        _age_move = (_now - _last) if _last > 0 else _age_req
        if _age_req < _stuck or _age_move < _stuck:
            return False
        self._rope_giveup_until = _now + _stuck
        self._rope_giveup_stamp = _last
        self._rope_req_t0 = _now                  # 下一次尝试重新计时
        if _now - float(getattr(self, "t_rope_giveup_log", 0.0) or 0.0) > 5.0:
            self.t_rope_giveup_log = _now
            logger.warning(
                f"[爬绳] 路线要求 {_cmd_now} 已 {int(_age_req)}s、期间没有竖向进展"
                f"（全局 {tuple(self.loc_player_global) if self.loc_player_global else '?'}）"
                f" ⇒ 判定这根绳上不去，暂停爬绳 {int(_stuck)}s："
                f"不再按 {_cmd_now}、怪逻辑恢复（用户 m10830；"
                f"`route.rope_stuck_s` 填 0 = 关掉本层）")
            self._budget_note_lost("爬绳放弃(次)")
        return True

    def _ladder_yield(self):
        '''
        「本帧要不要给绳/梯让路」—— 唯一判据点（让路块只调这一个）。

        三路，任一成立即让路：
          ① 路线本帧给的 `cmd_move_y` 是 up/down —— **始终让路**（拉绳只由路线负责，
             见让路块 `:5900` 那段真机现场；本路不受 `ladder_still_s` 影响）；
             ★ 2026-10-14（P0，用户 m10830）：但**爬了 `rope_stuck_s` 秒还没上去**
             就不再让路 —— 见 `_rope_ladder_giveup_tick()`（那 17% 的静音就是这里）；
          ② `is_on_ladder` 为真 **且** 最近 `ladder_still_s` 秒内有过竖向进展
             （小地图全局 y 变过）；
          ③ `_ladder_still_s()` 返回 0.0（判据不可用）**且** `is_on_ladder` 为真
             ⇒ fail-closed 退回老行为。
        返回 `(让路吗, 为什么)`，`为什么` 直接进日志，方便用户核对"这次为什么没打"。
        '''
        _giveup = self._rope_ladder_giveup_tick()
        if str(self.cmd_move_y) in ("up", "down"):
            _why = f"路线指令 y={self.cmd_move_y}"
            if self.is_on_ladder:
                _why += "、且在梯子上（is_on_ladder）"
            if _giveup:
                return False, ""     # 这根绳判定上不去 ⇒ 把控制权交回怪逻辑
            return True, _why
        if not self.is_on_ladder:
            return False, ""
        _still_s = self._ladder_still_s()
        if _still_s <= 0:
            return True, "在梯子上（is_on_ladder）、竖向进展判据不可用（fail-closed 老行为）"
        _age = time.time() - float(getattr(self, "t_ladder_move", 0.0))
        if _age <= _still_s:
            return True, (f"在梯子上（is_on_ladder）且 {int(_age * 1000)}ms 内"
                          f"有竖向移动（小地图全局 y 变过）")
        return False, f"旗标粘着：{int(_age * 1000)}ms 没有竖向移动"

    def _rope_nudge_cfg(self):
        '''
        绳梯脚下"蹭一下"自救的四个参数 —— **只有这一处读配置**。

        对应 `config/config_default.yaml` 的 `route.rope_nudge_s` /
        `rope_nudge_ms` / `rope_nudge_cycle_s` / `rope_nudge_max_s`。

        为什么要有这层自救（用户 m08454 报「卡楼梯」+ 真机日志取证）：
          · `log/MSBot_2026-10-14_15-07-02.log`：15:16:40→15:17:25 停在
            全局(78,78)、15:32:22→15:33:05 停在 (76,78)，**按着 up 45~50 秒**
            不动；同期 `[按键状态] 指令(none up none) 近5秒实际发键 94~97 次`
            ⇒ 键一直在发、不是按键失败。
          · 机制：路线那根绳子是 1px 竖列，角色停在列上 ⇒ 最近像素是 up 像素
            ⇒ 只发纯 up；能把角色"挪进绳子"的横向补位在
            `if self.cmd_move_x == "none" and self.is_on_ladder` 里，而旗标要
            "纵向真动过"才置真 ⇒ **自锁**。
          · 跨轮取证：同样的僵持在 A+B 之前就有（02:20 轮 ≈45s @(256,78) 等）
            ⇒ 不是本分支 A/B 引入的。

        Returns:
            (start_s, burst_ms, cycle_s, max_s)；`start_s <= 0` 表示**关掉本层**。
            每个值的越界/畸形一律退回出厂值 —— 只有 `rope_nudge_s` 明确填
            0/负数才是"用户要关掉"，其余一律按出厂值保守执行。
        '''
        _r = self.cfg.get("route") or {}

        def _num(key, default, lo, hi):
            try:
                _v = float(_r.get(key, default))
            except (TypeError, ValueError):
                return default
            if _v != _v or _v < lo or _v > hi:     # NaN / 越界
                return default
            return _v

        # rope_nudge_s：负数也算"关掉"，所以下界放得很松（数值本身由调用方判 ≤0）
        _start = _num("rope_nudge_s", 1.0, -1000.0, 30.0)
        _ms = _num("rope_nudge_ms", 120.0, 20.0, 1000.0)
        _cyc = _num("rope_nudge_cycle_s", 0.6, 0.1, 10.0)
        _max = _num("rope_nudge_max_s", 6.0, 0.0, 120.0)
        return _start, _ms, _cyc, _max

    def _rope_nudge_dir(self, up_x, ud):
        '''
        本帧要不要给"蹭一下"的横向方向 —— **唯一判据点**（路线跟随的 up 分支只调它）。

        触发条件（全部成立才蹭）：
          ① `route.rope_nudge_s` > 0（用户没关掉本层）；
          ② 路线本帧要的是 up/down（`ud`）—— 只治"站在绳梯脚下上不去"这一类；
          ③ **不在梯子上**（`is_on_ladder` 为假）—— 在绳子上按左右会松手掉下去；
          ④ 最近 `rope_nudge_s` 秒**没有竖向进展**（复用 A 那枚戳 `t_ladder_move`：
             只认小地图全局 y 变化）；
          ⑤ 从"开始该蹭"那一刻算起还没超过 `rope_nudge_max_s` 秒
             （超过 ⇒ 放弃，退回老行为，交给既有的 `[路线够不着]`/watchdog 报警）。

        方向：先朝绳梯那一列（`up_x`）走；已经在列上（|Δx| ≤ 1）就按时间片
        **左右交替** —— 小地图推算的全局 x 有 ±2~4px 抖动（同一轮日志里到处是
        `[梯子] 全局 x 变化 2px`），真实位置可能偏在列的一侧，左右交替才能
        "扫"过那一列（真人也是这么蹭的）。

        Args:
            up_x: 本帧扫到的 up/down 像素的 x（route 图坐标 == 全局坐标）。
            ud: 该像素的命令里的上下分量（"up" / "down" / "none"）。
        Returns:
            "left" / "right" / ""（"" = 本帧不蹭）
        '''
        _start, _ms, _cyc, _max = self._rope_nudge_cfg()
        if _start <= 0:                       # ① 用户关掉了本层
            return ""
        if str(ud) not in ("up", "down"):     # ② 不是"要上下"
            return ""
        if getattr(self, "is_on_ladder", False):   # ③ 真在爬 ⇒ 绝不能蹭
            return ""
        if getattr(self, "loc_player_global", None) is None:
            return ""                          # 拿不到全局坐标 ⇒ 判不了，保守不蹭
        _stamp = float(getattr(self, "t_ladder_move", 0.0) or 0.0)
        if _stamp <= 0.0:
            # 本次开跑还**从没记录过竖向进展**（戳是 __init__ 的 0.0）⇒ 从这一刻起算，
            # 否则 time.time() - 0.0 是个天文数字、会直接被 ⑤ 判成"早就该放弃"。
            self.t_ladder_move = time.time()
            return ""
        _age = time.time() - _stamp
        if _age < _start:                      # ④ 还没到"该蹭"的阈值
            return ""
        _over = _age - _start
        if _max > 0 and _over > _max:          # ⑤ 蹭够久了 ⇒ 放弃
            return ""
        _dx = int(up_x) - int(self.loc_player_global[0])
        if _dx <= -2:
            return "left"                      # 朝绳子那一列走
        if _dx >= 2:
            return "right"
        # 已经在列上 ⇒ 时间片左右交替：每 _cyc 秒里前 _ms 毫秒蹭一下、其余只按 up
        if _cyc <= 0:
            return ""
        _burst = min(max(_ms / 1000.0, 0.0), _cyc)
        _slot = int(_over / _cyc)
        if (_over - _slot * _cyc) > _burst:
            return ""                          # 这一档的"蹭"已经用完，只按 up 等判定
        return "right" if _slot % 2 == 0 else "left"

    def _ladder_finish_cfg(self):
        '''
        爬梯"走完"的两个参数 —— **只有这一处读配置**。

        对应 `config/config_default.yaml` 的 `route.ladder_finish`（总开关）与
        `route.ladder_finish_s`（补多久还没进展就冷却同样长的时间）。

        Returns:
            (on, max_s)；`on=False` 或 `max_s <= 0` ⇒ **关掉本层**（逐字退回老行为）。
        '''
        _r = self.cfg.get("route") or {}
        _on = bool(_r.get("ladder_finish", True))
        try:
            _s = float(_r.get("ladder_finish_s", 6.0))
        except (TypeError, ValueError):
            _s = 6.0
        if _s != _s or _s > 120.0:          # NaN / 越界 ⇒ 退回出厂值
            _s = 6.0
        return _on, _s

    def _ladder_finish_tick(self):
        '''
        本帧要不要"补一个竖向指令、把梯子走完" —— **唯一判据、也是唯一改状态的地方**。

        背景（用户 m11669 原文：「上梯子要走完，不要检测怪了，现在会走上梯子后，
        发现有怪就卡在梯子上」+ 真机取证 `log/MSBot_2026-10-15_00-36-31.log`）：
          · 角色挂在梯子上时**按左右键是不动的**（人还挂在绳上），只有 up/down 能走；
          · 而路线这帧未必给竖向 —— 最近的可走路线像素可能是横向的（
            `elif color_code:` 那条路直接 `cmd_move_x, cmd_move_y, cmd_action = …`），
            也可能是同列的 up 像素被"防地面伪灰"判掉 ⇒ 指令里 `y=none`；
          · 于是出现"键一直在发（90~96 次/5s）、全局坐标一动不动"的僵持：
            实测 26 分钟里 (210,60) 停了 30s（竖向指令 0/6 帧）、(78,74) 停 15s、
            (180,64) 停 15s；跨轮取证里这一类占会话 17%（87 段 / 1540s）。
          · 同时 `_ladder_yield()` 此时判"没在爬"⇒ 怪逻辑被放进来 ⇒ 站桩打怪。

        判据（全部成立才补）：
          ① `route.ladder_finish` 打开且 `ladder_finish_s` > 0；
          ② `is_on_ladder` 为真（引擎自己说"人在梯子上"—— 本层只信这面旗子）；
          ③ 本帧路线**没给**竖向（`cmd_move_y` 不是 up/down）；
          ④ 不在 P0 的"爬绳放弃"窗口里（那 8 秒归 P0，别抢）；
          ⑤ 没在冷却期（上一轮补够时间还没进展 ⇒ 让它歇同样长的时间）。
        方向：路线最近一次给的竖向指令（`_last_vert_cmd`，默认 up）。

        ⚠️ 退出靠既有的两条：① 引擎的"离开梯子"判据（全局 x 变化 ≥
        `route.ladder_leave_x`、或屏幕 dx > 3）⇒ 旗标转假 ⇒ 本层下一帧自动停；
        ② 本层的超时 —— 补了 `ladder_finish_s` 秒、全局 y 一直没动 ⇒ 判定"按上去
        也没用"，冷却同样长的时间（日志 `[爬梯] …`），避免在梯子顶端把横向走位
        一直压住。

        ⚠️ 补上竖向之后，`_ladder_yield()` 的判据①（路线给 up/down ⇒ 始终让路）会
        自然生效 ⇒ **怪逻辑全程静音**，这正是用户要的"不要检测怪了"。

        Returns:
            "up" / "down" / ""（"" = 本帧不补，路线指令照旧）
        '''
        _on, _maxs = self._ladder_finish_cfg()
        _now = time.time()
        if not _on or _maxs <= 0:
            self._ladder_push_t0 = 0.0
            return ""
        if not getattr(self, "is_on_ladder", False):
            self._ladder_push_t0 = 0.0
            return ""
        if str(self.cmd_move_y) in ("up", "down"):
            self._ladder_push_t0 = 0.0      # 路线自己给了竖向 ⇒ 不补
            return ""
        if self._rope_giveup_now():
            self._ladder_push_t0 = 0.0      # P0 的窗口里不抢
            return ""
        if _now < float(getattr(self, "_ladder_push_cool", 0.0) or 0.0):
            return ""                       # ⑤ 冷却中
        _t0 = float(getattr(self, "_ladder_push_t0", 0.0) or 0.0)
        if _t0 <= 0.0:
            _t0 = _now
        _move = float(getattr(self, "t_ladder_move", 0.0) or 0.0)
        _age_move = (_now - _move) if _move > 0.0 else (_now - _t0)
        if (_now - _t0) >= _maxs and _age_move >= _maxs:
            # 补够了、全局 y 一直没动 ⇒ 按上去也没用，冷却同样长的时间
            self._ladder_push_cool = _now + _maxs
            self._ladder_push_t0 = 0.0
            if _now - float(getattr(self, "t_ladder_finish_log", 0.0) or 0.0) > 1.5:
                self.t_ladder_finish_log = _now
                logger.warning(
                    f"[爬梯] 在梯子上补了 {int(_now - _t0)}s 竖向、全局 y 一直没动"
                    f" ⇒ 冷却 {int(_maxs)}s 不再补（交给路线/绳梯自救；"
                    f"`route.ladder_finish_s` 填 0 = 关掉本层）")
            return ""
        _dir = str(getattr(self, "_last_vert_cmd", "up") or "up")
        if _dir not in ("up", "down"):
            _dir = "up"
        self._ladder_push_t0 = _t0
        if _now - float(getattr(self, "t_ladder_finish_log", 0.0) or 0.0) > 1.5:
            self.t_ladder_finish_log = _now
            logger.info(
                f"[爬梯] 还在梯子上、路线这帧没给上下"
                f"（指令 {self.cmd_move_x} none {self.cmd_action}）"
                f" → 补 {_dir} 把梯子走完（已补 {int(_now - _t0)}s、"
                f"怪逻辑同时让路；`route.ladder_finish_s` 填 0 = 关掉本层）")
        return _dir

    def update_cmd_by_mob_detection(self):
        # Get monster search box
        # ── 感知框 / 攻击框分开（2026-10-13 加，用户选的方案 A）──────────────
        # 旧实现：索敌框 == 攻击框 == loc_player ± (range/2 + margin)，只有 500×270
        #   （1366×768 客户区的 13%）。用户真机 12 帧实测：整帧 53 只怪里 46 只
        #   （87%）落在这个框外 ⇒ 现象是"身边明摆着有怪、机器人不认"。
        # 新实现：**看得远、只打够得着的** ——
        #   · 索敌（感知）框 = 上面那个口径 × monster_detect.detect_range_scale
        #     （位置连续丢失时还会临时放大到整帧，都在 get_search_box 里）；
        #   · 攻击判定只认 get_attack_range()（打得到的范围，**不含 margin**）；
        #   · 看得见但够不着 → approach_mob 让它"走过去打"（见下面那段）。
        # ⚠️ 帧尾画的那个蓝框必须走**同一个** get_search_box()，别在这里手算 ——
        #    否则画的和用的不是一个框（tools/verify_mob_boxes.py 的 [6b] 钉这条）。
        if self.cfg["bot"]["attack"] == "aoe_skill":
            cooldown = self.cfg["aoe_skill"]["cooldown"]
        elif self.cfg["bot"]["attack"] == "directional":
            cooldown = self.cfg["directional_attack"]["cooldown"]
        else:
            raise RuntimeError(f"Unsupported attack mode: {self.cfg['bot']['attack']}")
        x0, y0, x1, y1 = self.get_search_box()
        # 两个框的尺寸（2026-10-14 加，**只给日志文案用**）：出招只看攻击框，
        # 而画在画面上的、用户看到的那个框是索敌框 —— 两者差一大截，日志里必须给数字，
        # 否则每次都要回去翻配置才知道"为什么 279px 还不打"。
        _ax, _ay = self._attack_box_half()
        _sbx = abs(int(x1) - int(x0)) // 2
        _sby = abs(int(y1) - int(y0)) // 2

        # Get monsters in the search box
        _perf_t0 = time.perf_counter()
        # ── 怪检测**降频**（2026-09-14 实测加，见 monster_detect_interval）────
        # 为什么：一帧 552ms 里怪检测占 **502ms（91%）**，是帧率卡在 1.8fps 的
        #   唯一元凶。怪不需要每帧重新找 —— 降频省下的时间会让帧率涨回去，
        #   实测"两次检测之间"只慢 50~100ms，却换来整体响应快一倍。
        # 中间帧沿用上一次的 self.monsters（下游只读它的大小和位置）。
        #
        # ⚠️ 跳过的帧**不调 _perf**：否则耗时被摊到更多帧上，日志里的
        #    "怪检测 XXXms" 会假性变小（502 → 251），让人误以为优化生效，
        #    把后续排查彻底带偏。它应该始终显示"跑一次要多久"。
        _iv = monster_detect_interval(self.cfg)
        _skip = int(getattr(self, "_mob_skip", 0)) + 1
        if _iv <= 1 or _skip >= _iv:
            self.monsters = self.get_monsters_in_range((x0, y0), (x1, y1))
            self._perf("怪检测", _perf_t0)
            self._mob_skip = 0
            # ★ 本帧**真的**跑了一次检测（下面的"打完了没"只按检测帧计数 ——
            #   降频时中间帧沿用的是上一次的 self.monsters，拿它计数会把
            #   "打完"的判定延后/提前 `every_n_frames` 倍）。
            _detected_now = True
        else:
            self._mob_skip = _skip
            _detected_now = False

        # ── 「有框不打」的第一大现场：窗口不在前台（2026-10-14 加）────────────
        # 键盘层（src/input/KeyBoardController.py 的前台守卫）在窗口不在前台时
        # **一个键都不发**（还会自动切回前台，节流 2s），而引擎照旧检测、照旧画框 ⇒
        # 从画面上看就是"有框、有目标、就是不出招"。这一行把"打怪逻辑的问题"和
        # "焦点/前台的问题"分开（纯观测，5 秒节流；内部先判 monsters/节流再查前台，
        # 所以**不会**每帧打 win32 API）。
        self._mob_no_attack_log("front")

        # ── 回正优先（最高优先级，2026-09-12）────────────────────────────────
        # 正在走回正线时，**完全不覆盖移动指令**，
        # 只更新 self.monsters（画面上的索敌框、状态日志的怪数还要用它）。
        #
        # 用户实测：「从平台上掉下去后，角色会主动跑去打怪而不是回到安全点」——
        # 就是这里把回正的方向覆盖成了怪的方向。回正期间先回来，回来再打。
        # （上一版只保护了"起跳那一帧"，回正线给 left/right 时照样被拽走，不够）
        #
        # ⚠️ 配套的出口在 update_cmd_by_route：回正线**一帧像素都找不到**时立刻
        #    退出回正状态（is_using_home_route=False），否则会「既不走也不打」。
        if self.is_using_home_route:
            # 回正期间怪逻辑完全不碰走位与动作（上面那段注释）—— 这正是"有框却不打"
            # 的另一个现场，所以在这里也留一行归因（纯观测，5s 节流）。
            self._mob_no_attack_log("home_route")
            return

        # ── 绳/梯让路（2026-10-13 修用户实测的「会卡在绳子上」）──────────────
        # 判据：不动 cmd_move_x/cmd_move_y/cmd_action，上面那份 self.monsters 已经
        # 更新过了（画面框/日志照旧）。
        #   ① 本帧**路线给的** `cmd_move_y` 是 up/down —— 角色正要/正在爬。**始终让路**。
        #   ② `self.is_on_ladder` —— 引擎自己的"在梯子上"状态。**2026-10-14 起收紧**
        #      （用户 m08016 拍板 A）：旗标为真**且**最近 `route.ladder_still_s` 秒内
        #      小地图全局 y 变过（真在上下走）才让路。理由 = 示范录制实测：
        #        梯上帧 2749（43.9%），其中整段全局 y 零变化的粘滞段 44/105（1048 帧），
        #        人在这些帧里出手 **910 次**，而引擎梯上出手率 **0.0%**（平台 16.5%）。
        #      判据不可用（配置 ≤0 / 全局坐标读不到）⇒ fail-closed 退回老行为（一直让路）。
        #      两个判据都在 `_ladder_yield()` 里（那才是唯一判据点，本块只问它）。
        # 为什么必须有①（真机日志实证，`log/MSBot_2026-10-13_23-14-52.log`）：
        #   梯子口的"离开梯子"判据是**小地图全局 x 变化 ≥ route.ladder_leave_x(2)**
        #   （本意：爬到顶开始沿平台横向移动），而**怪逻辑的横向移动（靠近/转身）
        #   恰好会让全局 x 变 2px** ⇒ 引擎把 is_on_ladder 判成 False（日志
        #   `[梯子] 全局 x 变化 2px（80 → 82）⇒ 判定已离开梯子，停止按 up`）⇒
        #   它不再补 up，怪逻辑又把 cmd_move_y 清零 / 把动作改成 attack ⇒ 角色
        #   吊在绳子上反复起跳（同段日志里 `[靠近] …朝它走一段` 与上面那条交替
        #   出现，还夹着 `[按键·跳跃] 想跳但被冷却挡住（已连挡 13 次）`）。
        # ⇒ **拉绳/爬梯这件事只由路线负责**：爬的时候怪逻辑一律不碰走位与动作。
        #   ⚠️ 别把这里放宽成"只让 x"：路线的 up 被清掉一样会吊在绳上。
        _climbing, _why = self._ladder_yield()
        if _climbing:
            # ★ P4 纯观测（2026-10-14）：这一帧的怪逻辑是被让路吃掉的 —— 记账，
            #   好在 [开火预算] 里看出"没打成"有多少来自绳/梯（真机占 17% 会话时间）。
            self._budget_note_lost("被让路(帧)")
            if getattr(self, "_stop_hold", False):
                # 已经站定了，现在路线在爬 → 立刻把走位交回路线（不许钉在绳上）
                self._stop_hold = False
                self._stop_hold_empty = 0
            if self.cmd_action == "attack":
                # cmd_action 是**跨帧保持**的：不清的话在绳上会一路按技能键，
                # 还挡掉路线在梯子顶那一下 jump。
                self.cmd_action = "none"
            _now_climb = time.time()
            if _now_climb - float(getattr(self, "t_climb_yield_log", 0.0)) > 5.0:
                self.t_climb_yield_log = _now_climb
                logger.info(
                    f"[绳/梯] 判定正在爬（{_why}）→ 怪逻辑让路："
                    f"不站定、不靠近、不抢方向/动作（拉绳只由路线负责）"
                    # ★ 2026-10-14：把"画面上的框"和"不打"对上 —— 让路期间检测照跑、
                    #   框照画（用户 m05740 反馈的"有框却不打"，有一大半就是这里）。
                    f"（画面上还有 {len(getattr(self, 'monsters', None) or [])} 个目标、"
                    f"框照旧画）")
            return

        # ★ 2026-10-14：旗标还在、但已判定"不是真在爬"⇒ 不再让路。这是用户
        #   m07230「攻击还是不够灵敏」→ m08016 拍板 A 之后新增的一行归因：
        #   不改行为的话，用户只会看到"以前不打、现在打了"，却不知道是哪条生效。
        if self.is_on_ladder and _why:
            _now_stale = time.time()
            if _now_stale - float(getattr(self, "t_ladder_stale_log", 0.0)) > 5.0:
                self.t_ladder_stale_log = _now_stale
                logger.info(
                    f"[绳/梯] {_why} ⇒ **不再让路**：怪逻辑照常靠近/出招"
                    f"（2026-10-14 用户拍板 A；路线给 up/down 时仍让路，"
                    f"`route.ladder_still_s` 填 0 可退回老行为）"
                    f"（画面上有 {len(getattr(self, 'monsters', None) or [])} 个目标）")

        # ── 巡逻中「停下来打怪」（2026-10-13 加，用户要求）──────────────────
        # 用户原话：「巡逻中，检测到怪，打完怪再移动，而不是在移动中穿插着攻击」。
        #
        # 旧行为：`update_cmd_by_route` 每帧先把移动指令写好，本函数只在上面**叠**
        #   一个 `cmd_action="attack"` ⇒ 角色边走边打：路过的怪只吃到擦身而过的
        #   几刀就被走远了；而且出招节奏被路线方向不断打断（directional 还要先
        #   转身，转完人已经走出攻击框）。
        #
        # 新行为（`monster_detect.stop_to_attack`，默认开）：
        #   ① 索敌框里**有怪** → 本帧移动清零（站定），只出招；
        #   ② 框里**连续 stop_attack_clear_frames 个检测帧**没怪 → 判定打完，
        #      松开、把走位交回路线（`update_cmd_by_route` 下一帧照常给方向）；
        #   ③ 两条安全阀（都不会把角色钉死）：
        #      · 停下后**一直出不了招**（idle_s ≈ max(2.5, 攻击冷却×2.5) 秒没有
        #        攻击指令）→ 立刻放行，并打一条 WARNING。这只说明"怪在框里但
        #        打不着"（正上方/正后方/被地形挡住），没必要傻站；
        #      · 停下总时长 > stop_attack_timeout_s（默认 20s）→ 强制放行 + WARNING
        #        （治"血厚打不死"或检测一直在闪的情形）；
        #      · 强制放行后要求**先清场一次**（一次检测帧框里为空）才允许再停下 ——
        #        否则在"满屏都是怪"的图上会变成：停 20s → 放行 1 帧 → 又停 20s，
        #        永远走不出这一段路线。
        #
        # ⚠️ 为什么清零必须写在**本函数**里：`cmd_move_x/cmd_move_y` 是**跨帧保持**
        #    的实例属性，而 `update_cmd_by_route()` 在 HuntingState 里**先于**本函数
        #    执行（`src/states/hunting.py` 的 on_frame：route → check_reach_goal →
        #    mob_detection）—— 只有在这里清零才能盖掉本帧路线刚写的方向。
        # ⚠️ 回正线期间不参与（上面那条 return 已经拦掉）：回正是"回家"，
        #    优先级高于打怪，这条规则从 2026-09-12 起就在，别挪。
        # ⚠️ 看门狗不会因此误报卡死：出招会刷新 `t_last_attack`，而
        #    `is_player_stuck` 有 `watchdog.attack_grace`（默认 5s）豁免；
        #    真打不到时它照旧弹 alert —— 那正是我们要的信号，别去屏蔽它。
        # ── "打得到"的那一批（攻击框内）──────────────────────────────────────
        # 索敌框（x0..y1）可能被 detect_range_scale 放大很多，但**攻击判定只认
        # 攻击框**：够不着的怪不该让角色停下来出招（打不到，白发）。
        # `_far` = 看得见、够不着的那些（交给 approach_mob 走过去打）。
        _engage = self._mobs_in_attack_box()
        _eng_ids = {id(_m) for _m in _engage}
        _far = [_m for _m in self.monsters if id(_m) not in _eng_ids]
        # ★ 2026-10-14 加（用户 m06852「记录下怪物的检测信息，现在的攻击还是不够
        #   灵敏」）：把**这一帧的检测结果**单开一行写下来 —— 只写"怪N"看不出
        #   它在哪、为什么不算够得着。纯观测（异常只吼一次，绝不影响判定）。
        self._log_monster_detect(_engage, _far)
        # 「第一只怪进攻击框」的时刻 —— 出招那一刻用它算"灵敏度"（进框→首刀 ms）。
        # 框内空了就作废：那一次交战已经结束，下一个目标要重新计时（否则会拿
        # "上一只怪的进框时刻"去算，把延迟算成几十秒）。
        if _engage:
            if getattr(self, "_eng_first_t", None) is None:
                self._eng_first_t = time.time()
        elif getattr(self, "_eng_first_t", None) is not None:
            self._eng_first_t = None
        _md_stop = self.cfg.get("monster_detect") or {}
        _stop_to_attack = bool(_md_stop.get("stop_to_attack", True))
        # ── 够不着就走过去打（approach_mob，2026-10-13 用户选的方案 A）────────
        # 只在**同一层**横着走：怪与角色高度差 <= approach_dy_tol（默认 60px）。
        # 跨层（上面/下面的平台）一律不硬走 —— 那要爬梯子/跳，交给路线；
        # 历史上"窄平台上乱抽方向直接掉下去"就是这么来的。
        _approach = bool(_md_stop.get("approach_mob", True))
        # ⚠️ 「同一层」的高度容差（`approach_dy_tol`）与两个防抖参数
        #    （`approach_confirm_s` 出现确认、`approach_dir_min_s` 方向最小保持）
        #    都读在 `_pick_approach_target()` 里 —— 挑目标的规则只有那一处，
        #    两个调用点（锁定期间追怪 / 松开后靠近）不再各写一遍。
        _clear_frames = max(1, int(_md_stop.get("stop_attack_clear_frames", 2) or 2))
        # ★ 2026-10-14 P1：把"框空多久算打完"从**帧数**改成**秒数**（帧数只当最小值）。
        #   帧数阈值是**采样率相关**的：同样"空 2 帧"，14.7Hz 是 136ms、3.3fps 是 600ms
        #   —— 用户一改 every_n_frames，行为就跟着变。秒数阈值与采样率无关。
        try:
            _clear_s = float(_md_stop.get("stop_attack_clear_s", 0.3) or 0.0)
        except (TypeError, ValueError):
            _clear_s = 0.3
        if _clear_s < 0:
            _clear_s = 0.0
        try:
            _stop_timeout = float(_md_stop.get("stop_attack_timeout_s", 20) or 0)
        except (TypeError, ValueError):
            _stop_timeout = 20.0
        # ⚠️ 用 max(2.5, 冷却×2.5)：冷却短的 AOE（0.05s）别 2 帧就误判"打不着"，
        #    冷却长的 directional（0.9s）也要给够一次转身+出招的时间。
        try:
            _cd = float(cooldown)
        except (TypeError, ValueError):
            _cd = 0.0
        _idle_s = max(2.5, _cd * 2.5)
        _now = time.time()
        _holding = bool(getattr(self, "_stop_hold", False))
        # ── 目标锁定（2026-10-14 加，用户要求「一旦发现目标就持续攻击，直到怪物消失」）──
        # 修之前的行为（真机日志 MSBot_2026-10-14_10-03-15.log 11:02 段实证）：
        #   放行判据是「**攻击框**里连续 stop_attack_clear_frames(2) 个**检测帧**没怪」
        #   —— `every_n_frames: 2` + 28fps ⇒ **约 0.15 秒**就判"打完"。而 AOE 攻击框
        #   只有 ±(range_x/2, range_y/2)，怪又在走动 ⇒ 日志里 [停下打怪] 与 [判定打完]
        #   每秒抖好几次，`运行状态` 几乎全是 `指令(left none none)`（一直在走、基本没打）。
        #   ⇒ 缺的是**目标记忆**：全项目没有任何"刚才在打哪只怪"的状态。
        #
        # 现在：攻击框里认出过怪就记住它 + 时间戳；在 `target_hold_s` 秒内**不判"打完"**，
        # 它不在攻击框里就追过去（同层）、追不到就守着最后位置继续出招；
        # 直到"连续 target_hold_s 秒没再看见它"才判定怪物已消失、恢复巡逻。
        # ⚠️ `target_hold_s: 0` = 关掉本功能（逐字退回老行为），默认 3.0s。
        # ⚠️ 两条安全阀（2.5s 出不了招 / 停留上限 20s）照旧生效 —— 不会把角色钉死。
        try:
            _hold_s = float(_md_stop.get("target_hold_s", 3.0) or 0.0)
        except (TypeError, ValueError):
            _hold_s = 3.0
        if _hold_s < 0:
            _hold_s = 0.0
        if _detected_now and _engage and _hold_s > 0:
            _lp = self.loc_player
            _near = min(_engage,
                        key=lambda _m: (abs(_m["position"][0] + _m["size"][0] // 2 - _lp[0])
                                        + abs(_m["position"][1] + _m["size"][1] // 2 - _lp[1])))
            # 上一把锁定已经过期 ⇒ 这是**新**目标（t0 供日志/诊断用）
            if _now - float(getattr(self, "_mob_lock_seen_t", 0.0)) > _hold_s:
                self._mob_lock_t0 = _now
            self._mob_lock_seen_t = _now
            self._mob_lock_pos = (_near["position"][0] + _near["size"][0] // 2,
                                  _near["position"][1] + _near["size"][1] // 2)
            self._mob_lock_n = len(_engage)
            # 又看见它了 ⇒ "框空"的计时**从头算**（P1 的时间窗必须以**连续空窗**为准；
            # 怪只是走出一帧又回来，不该攒够秒数）
            self._stop_hold_empty = 0
            self._stop_hold_empty_t0 = 0.0
        _lock_seen = float(getattr(self, "_mob_lock_seen_t", 0.0) or 0.0)
        _locked = bool(_hold_s > 0 and _lock_seen > 0 and _now - _lock_seen <= _hold_s)
        if not _stop_to_attack:
            # 关掉这个功能 = 退回"边走边打"；同时把停留状态清干净（别留僵尸态）
            self._stop_hold = False
            self._stop_hold_empty = 0
            self._stop_hold_need_clear = False
            _holding = False
        elif _detected_now and not _engage:
            # 这一帧真的跑了检测、**攻击框内**为空 → 判"这段打完了"（也解除封锁）
            # ⚠️ 判据用 _engage 而不是 self.monsters：远处看得见但够不着的怪
            #    不该让"打完"永远判不出来（那样会一直站着等 idle 安全阀）。
            self._stop_hold_need_clear = False
            if _holding:
                # ★★ 2026-10-14 第二次改（低频识别优化 P1，用户 m05009「落地」）：
                #   放行判据 = 「**连续空够 `stop_attack_clear_s` 秒**」+「空够
                #   `stop_attack_clear_frames` 个检测帧」+「**本次停留出过招**」。
                #   三个都要满足才判"打完了"：
                #     · 秒数：与采样率无关（帧数阈值在 14.7Hz 是 136ms、3.3fps 是 600ms）；
                #     · 检测帧数：兜底最小值，防"一帧就放行"（降频时也成立）；
                #     · 出过招：**秒杀怪**的正解 —— 怪一刀就死，之后框里一直空着，
                #       旧规则会白站 `target_hold_s`(3.0) 秒。反过来，一次都没出过招
                #       说明"不是打完了，是打不着"（在正上/正下方、被地形挡住），
                #       那种情况**不该**记成"打完"，交给锁定过期或 idle 安全阀放行。
                #
                # ⚠️★ 2026-10-14 第三次改（真机日志 MSBot_2026-10-14_11-42-16.log 实证）：
                #   **"出过招 + 空够秒数 ⇒ 收手"这条不受目标锁定阻拦**。
                #   病根：原来整段包在 `if _holding and not _locked:` 里，而
                #   `target_hold_s: 3.0` 决定了"最后看见之后 3 秒内"锁定恒为真
                #   ⇒ 这条判据**永远轮不到**：那次真机日志里只有
                #   `目标已消失（连续 3s 没再看见它）`，停留时长清一色 3.1~4.2s
                #   —— 一刀秒掉的怪同样白站 3 秒（P1 想修的正是它）。
                #   现在：出过招 + 框空够秒数 ⇒ 立刻收手（0.3s 量级）；
                #   一次都没出过招 ⇒ 仍然靠锁定/idle 撑着继续找（锁定期**不**判完）。
                _empty_t0 = float(getattr(self, "_stop_hold_empty_t0", 0.0) or 0.0)
                if _empty_t0 <= 0.0:
                    _empty_t0 = _now
                    self._stop_hold_empty_t0 = _now
                self._stop_hold_empty = int(getattr(self, "_stop_hold_empty", 0)) + 1
                _empty_for = _now - _empty_t0
                _hit_done = float(getattr(self, "t_last_attack", 0.0)) >= \
                    float(getattr(self, "_stop_hold_t0", 0.0))
                _empty_long = bool(self._stop_hold_empty >= _clear_frames
                                   and _empty_for >= _clear_s)
                # ★★ "画面上还看得见**追得到**的怪吗"（2026-10-14 第三次改的配套）：
                #   还看得见 ⇒ **不收手** —— 多半就是它本人挨了一刀走出攻击框，或者
                #   旁边还有别的活口；两种情况都该继续打/追（"走过去打"也是用户要的）。
                #   一只都看不见 ⇒ 判定打完（一刀秒掉的怪走这条，0.3s 就收手，
                #   不再白站 target_hold_s 秒）。
                #   三个条件缺一不可：
                #     · `_far` 非空 —— 画面上还有怪；
                #     · `_approach` —— 用户允许"走过去打"。关掉它就等于"框外的当没看见"，
                #       那就必须能判"打完"，否则远处一只够不着的怪会让人**永远走不掉**
                #       （`tools/verify_mob_sight.py`【7】钉的就是这条）；
                #     · `_locked` —— 这是**目标锁定**这套功能的一部分；`target_hold_s: 0`
                #       关掉锁定时逐字退回老口径。
                #   ⚠️ 跨层的怪（approach_dy_tol 之外，追不到）也算"看得见" ⇒ 极端情况下
                #      会对着上层平台干等到 idle 安全阀（2.5s 出不了招）放行。
                #      这比"把还活着的怪扔下就走"保守，可以接受。
                _still_there = bool(_far) and _approach and _locked
                if _empty_long and _hit_done and not _still_there:
                    # ⚠️ 先把"空了几个检测帧"留证**再**清零：原来清零在前、打印在后，
                    #    于是真机日志里这一行永远写着「（0 个检测帧）」——
                    #    看起来像"一个检测帧都没空就判完了"，其实放行要求 ≥2 个检测帧，
                    #    是**日志自己把证据抹掉了**（2026-10-14 从 log 12-05-06 的
                    #    76 条判定打完里 61 条印着 0 反查出来）。
                    _empty_frames = int(getattr(self, "_stop_hold_empty", 0))
                    self._stop_hold = False
                    self._stop_hold_empty = 0
                    self._stop_hold_empty_t0 = 0.0
                    self._mob_lock_seen_t = 0.0   # 收手 ⇒ 锁定一起清掉，别留着
                    _holding = False
                    logger.info(
                        f"[停下打怪] 出过招 + 连续 {_empty_for:.1f}s"
                        f"（{_empty_frames} 个检测帧）框内没怪 → 判定打完，"
                        f"恢复巡逻")
                elif _empty_long and not _locked \
                        and _now - float(getattr(self, "t_empty_nohit_log", 0.0)) > 5.0:
                    # 一次都没出过招：不是"打完了"，是"打不着" —— 不判完，但**不许静默**
                    self.t_empty_nohit_log = _now
                    logger.info(
                        f"[停下打怪] 框里已经空 {_empty_for:.1f}s，但本次停留"
                        f"**一次都没出过招** → 不判「打完」，继续找（锁定还剩 "
                        f"{max(0.0, _hold_s - (_now - _lock_seen)):.1f}s；"
                        f"到点或 idle 安全阀会放行）")
        elif _detected_now and not _holding \
                and not getattr(self, "_stop_hold_need_clear", False):
            self._stop_hold = True
            self._stop_hold_empty = 0
            self._stop_hold_empty_t0 = 0.0
            self._stop_hold_t0 = _now
            _holding = True
            logger.info(
                f"[停下打怪] 攻击框里认出 {len(_engage)} 个目标（框外还有 "
                f"{len(_far)} 个够不着的），停下打完再走"
                f"（停留上限 {_stop_timeout:g}s、{_idle_s:g}s 出不了招就放行）")
        if _holding:
            # 参考点：最近一次出招 或 本次停留开始（哪个更晚取哪个）——
            # 停下后一次都没出过招也算"闲着"，不能被旧的 t_last_attack 骗过去。
            _last_act = max(float(getattr(self, "t_last_attack", 0.0)),
                            float(getattr(self, "_stop_hold_t0", _now)))
            _held = _now - float(getattr(self, "_stop_hold_t0", _now))
            if _hold_s > 0 and _lock_seen > 0 and _now - _lock_seen > _hold_s:
                # ★ 目标锁定过期：连续 target_hold_s 秒没再看见它 ⇒ 判定"怪物已消失"。
                #   这才是用户要的"直到怪物消失"；不是"离开攻击框 0.15 秒就放行"。
                self._stop_hold = False
                self._stop_hold_empty = 0
                self._stop_hold_need_clear = False
                self._mob_lock_seen_t = 0.0
                _holding = False
                logger.info(
                    f"[停下打怪] 目标已消失（连续 {_hold_s:g}s 没再看见它）→ 恢复巡逻"
                    f"（本次停留 {_held:.1f}s）")
            elif _now - _last_act > _idle_s:
                self._stop_hold = False
                self._stop_hold_empty = 0
                self._stop_hold_need_clear = True
                _holding = False
                logger.warning(
                    f"[停下打怪] 已停下 {_now - _last_act:.1f}s 一直出不了招 → 放行，"
                    f"恢复巡逻（框里的目标多半打不着：正上/正下方或被地形挡住；"
                    f"要它继续站着打就查 monster_detect.stop_to_attack 相关配置）")
            elif _stop_timeout > 0 and _held > _stop_timeout:
                self._stop_hold = False
                self._stop_hold_empty = 0
                self._stop_hold_need_clear = True
                _holding = False
                logger.warning(
                    f"[停下打怪] 已停留 {_held:.1f}s（上限 {_stop_timeout:g}s）仍在出招 "
                    f"→ 强制恢复巡逻（可疑：怪血厚打不死 / 检测在闪）。"
                    f"清场一次之后才会再停下打怪")
        if _holding:
            # 站定：清掉路线本帧刚写的走位，把"出招/朝向"留给下面的攻击分支
            # （directional 的"先转身"会自己把 cmd_move_x 设成目标方向 —— 那一小步
            #   是出招必需的，不是"边走边打"）
            #
            # ★ 例外（2026-10-14 目标锁定）：锁定期间目标暂时不在攻击框里 ⇒ **追过去**，
            #   而不是站着等 2.5s 的 idle 安全阀。只追**同一层**的
            #   （|dy| <= approach_mob 的 approach_dy_tol，默认 60px），梯子上不追。
            _chase, _chase_d, _chase_dy = None, None, None
            if _locked and not _engage and _approach and _far and not self.is_on_ladder:
                # 2026-10-14 P1：挑目标统一走 `_pick_approach_target`
                # （含"出现确认"与"方向最小保持"），不再两处各抄一遍取最近的循环。
                _chase, _chase_d, _chase_dy = self._pick_approach_target(_far, _now)
            if _chase:
                self.cmd_move_x = _chase
                self.cmd_move_y = "none"
                if _now - float(getattr(self, "t_lock_chase_log", 0.0)) > 2.0:
                    self.t_lock_chase_log = _now
                    logger.info(
                        f"[锁定] 目标暂时不在攻击框（框外还有 {len(_far)} 个）→ 朝它所在的"
                        f"方向{_chase}追一段，追上继续打；连续 {_hold_s:g}s 没再看见才算消失")
            else:
                self.cmd_move_x = "none"
                self.cmd_move_y = "none"

        # Check if no mob to attack
        if not _engage:
            # ★ 目标锁定期间（2026-10-14）：目标只是**暂时**没被认出来（走出攻击框 /
            #   动画帧没匹配上），别当成"打完了" —— 朝最后看见它的方向继续出招，
            #   直到锁定过期或两条安全阀放行。用户原话：「一旦发现目标就持续攻击，
            #   直到怪物消失」。
            # ⚠️ 只有 AOE 能"没目标也出招"（技能不分方向）；directional 打的是**当前
            #   朝向**，没目标时补发等于打空气，所以那里不补 —— 交给 idle 安全阀。
            if _holding and _locked and self.cfg["bot"]["attack"] == "aoe_skill":
                _lpos = getattr(self, "_mob_lock_pos", None)
                if _lpos is not None and not self.is_on_ladder \
                        and self.cmd_move_x_last != ("left" if _lpos[0] < self.loc_player[0]
                                                     else "right"):
                    # 只发**一帧**转向（cmd_move_x 是跨帧保持的，连发会变成走路）
                    self.cmd_move_x = ("left" if _lpos[0] < self.loc_player[0] else "right")
                if time.time() - self.t_last_attack > cooldown:
                    self.cmd_action = "attack"
                    self._note_attack(0)      # 纯观测：目标暂时不在框里也算一次出招
                    self.t_last_attack = time.time()
                else:
                    # 锁定补招也被冷却挡住（engage_n=0：目标只是暂时不在框里）
                    self._mob_no_attack_log(
                        "cooldown", engage_n=0, far_n=len(_far),
                        half_x=_ax, half_y=_ay,
                        cooldown_left=cooldown - (time.time() - self.t_last_attack))
                return
            # ⚠️ 必须清掉上一帧残留的攻击指令（2026-09-12 修）
            # cmd_move_x / cmd_move_y / cmd_action 是**跨帧保持**的实例属性，原实现
            # （上游逐字如此）在这里直接 return，于是只要某一帧设过 "attack"，怪没了
            # 它也一直是 "attack"，键盘层每帧按一次技能键（30fps ≈ 每秒 30 次）
            # → 用户实测「索敌范围内没有怪还在疯狂打空气」。
            # 上游不发作是因为 update_cmd_by_route 每帧拿路线色码覆盖 cmd_action；
            # 但回正线接不上、或路线走到 goal 时它不会覆盖，这颗雷就会响 —— 所以必须清。
            if self.cmd_action == "attack":
                self.cmd_action = "none"
            # ── 走得过去就打（2026-10-13 加，用户选的方案 A）──────────────────
            # 索敌框里看得见、但**攻击框外**的怪 → 朝最近的走过去；
            # 一旦它进攻击框，上面的"停下打怪"就接管：站定出招，打完再走。
            # 保守限制：
            #   ① 只走**同一层**（高差 <= approach_dy_tol，默认 60px）；
            #   ② 梯子上不走（update_cmd_by_route 刚配好上下，横向一动就被判成离开梯子）；
            #   ③ `approach_mob: false` 退回老行为（框外的当没看见，只打框内的）。
            # ⚠️ 路线的 cmd_action 保持不动（不硬改成 none）：路线里的 jump 像素是
            #    用户亲手录的"这里要跳"，清掉它反而可能走到悬崖边不跳。
            if _approach and _far and not self.is_on_ladder:
                _dir, _tkey, _tdy = self._pick_approach_target(_far)
                if _dir:
                    self.cmd_move_x = _dir
                    self.cmd_move_y = "none"
                    if _now - float(getattr(self, "t_approach_log", 0.0)) > 2.0:
                        self.t_approach_log = _now
                        logger.info(
                            f"[靠近] 攻击框外还有 {len(_far)} 个目标"
                            f"（最近的在{_dir}侧 {_tkey:g}px、高差 "
                            f"{_tdy:+d}px）→ 朝它走一段，"
                            f"进攻击框后再停下打"
                            # ★ 2026-10-14：同一行里给出**两个框的尺寸** ——
                            #   用户 m05740「有检测框、也在攻击范围内，却没有立马攻击」，
                            #   根子就是画出来的框（索敌框）比出招判据（攻击框）大一圈。
                            #   不给数字的话，每次都得回去翻配置才知道差多少。
                            f"（攻击框 ±{_ax}×±{_ay}px；"
                            f"画出来的框是索敌框 约 ±{_sbx}×±{_sby}px）")
                else:
                    # 看得见却够不着，但本帧没追过去（还没凑齐"连续两次确认"，或都在别的层）
                    # ★ P4 纯观测（2026-10-14）：这一条就是"确认没凑齐"的账
                    #   （含"只认到一帧"与"候选全在别的层"两种情形，后者另有高差账）
                    self._budget_note_lost("未确认(次)")
                    self._mob_no_attack_log("approach_pending", far_n=len(_far),
                                            half_x=_ax, half_y=_ay,
                                            **self._engage_gap_facts(_far))
            else:
                # approach_mob 关着 ⇒ 框外的一律不管；或 _far 为空（异常兜底）
                self._mob_no_attack_log("approach_off" if not _approach else "no_engage",
                                        far_n=len(_far), half_x=_ax, half_y=_ay,
                                        **self._engage_gap_facts(_far))
            return

        # Update attack command
        if self.cfg["bot"]["attack"] == "aoe_skill":
            if time.time() - self.t_last_attack > cooldown:
                # AOE 技能本身不分方向，但仍然要**面向**最近的怪：
                # 上游在这里完全不设 cmd_move_x，角色永远保持初始朝向，
                # 用户实测「后方的怪打不到」。AOE 不需要等转身，同帧出招即可。
                # ⚠️ 梯子上不抢方向键（见下方 directional 分支的说明）。
                if not self.is_on_ladder:
                    # ⚠️ 只看**攻击框内**的怪来定朝向：远处够不着的怪不该把角色
                    #    转身背对近处这只（感知框放大后 this 才会踩到）
                    face = self.get_face_direction(_engage)
                    if face is not None and self.cmd_move_x_last != face:
                        self.cmd_move_x = face
                self.cmd_action = "attack"
                self._note_attack(len(_engage))   # 纯观测：出招时攻击框内有几只
                self.t_last_attack = time.time()
            else:
                # 冷却没到（AOE 默认 0.05s，正常几乎不会命中；命中说明上一帧刚出过招）
                self._mob_no_attack_log(
                    "cooldown", half_x=_ax, half_y=_ay,
                    cooldown_left=cooldown - (time.time() - self.t_last_attack))

        elif self.cfg["bot"]["attack"] == "directional":
            # Get nearest monster to player
            monster_left  = self.get_nearest_monster(is_left = True)
            monster_right = self.get_nearest_monster(is_left = False)
            # Determine attack direction
            attack_direction = self.get_attack_direction(monster_left, monster_right)
            if attack_direction is None:
                # 两侧都没明确目标（两边怪距离太接近、或怪在正上/正下方）
                # —— 同样要清残留，否则会继续打空气
                if self.cmd_action == "attack":
                    self.cmd_action = "none"
                self._mob_no_attack_log("direction", half_x=_ax, half_y=_ay)
                return
            # Attack Command
            if time.time() - self.t_last_attack > cooldown:
                # ⚠️ 先转身、再出招（2026-09-12 修）
                # 技能是朝**当前朝向**打出去的；同一帧按方向键+技能键，技能会朝旧朝向
                # 飞走 —— 后方的怪永远打不到。上游 config 里躺着 character_turn_delay,
                # 但整个 src/ 一处引用都没有（没做完的半成品），这里补上：朝向不对时
                # 本帧只发转向键、不出招，下一帧再打（延迟一帧 ≈ 33ms）。
                #
                # 判据用「上一帧实际发出的方向」而不是一个持久的 facing 字段：
                # 巡逻模式下路线每帧都会把 cmd_move_x 改回路线方向，用持久 facing
                # 会导致永远卡在"等转身"、一次都不出招。
                # ⚠️ 梯子上不抢方向键：update_cmd_by_route 刚把上下方向配好，
                #    这里一旦覆盖成 left/right，角色横向一动就会被判成「离开梯子」
                #    （run_once 里 dx > 3 → is_on_ladder = False）→ 直接从梯子上掉下去。
                #    梯子上照常打怪，只是不改朝向。
                if self.is_on_ladder:
                    self.cmd_action = "attack"
                    self._note_attack(len(_engage))   # 纯观测
                    self.t_last_attack = time.time()
                    return
                # ── 转身状态机（2026-09-17 重写）────────────────────────────
                # 旧实现只有两步：① 朝向不对 → 本帧只发方向键；② 下一帧就出招。
                # 它有两个致命问题（用户实测「后面有怪却一直在往前面砍空气」）：
                #   (a) 转向只维持 1 帧（≈33ms），游戏里朝向可能根本没切过来；
                #   (b) 出招那帧**仍然按着方向键** —— 技能按"当前朝向"飞出去，
                #       同帧按方向+技能，技能会朝**转身前**的朝向飞走（见上面
                #       2026-09-12 的注释），等于每次都朝路线前进方向甩。
                # 现在改成三段：
                #   ① 朝向不对 / 怪换边 → 起一个"按住方向键 N 帧"的转身过程
                #   ② 转身期间只发方向键、不出招
                #   ③ 转完**松开方向键**（cmd_move_x="none"，朝向保持不变）再出招
                #      ⇒ 技能朝当前朝向飞，后方的怪打得着。
                _turn_frames = int(self.cfg["directional_attack"].get("turn_frames", 3))
                # getattr 兜底：离线用例里的 Stub 不走 __init__，没有这两个属性
                _turn_left = int(getattr(self, "_turn_frames_left", 0) or 0)
                # ── 换边抑制：目标在左右之间"横跳"时不许打断正在进行的转身 ────────
                #
                # 【治什么】issue #12（2026-09-29）：「左右同时存在怪物时，
                #   角色在原地左右抽搐，但是不攻击」。
                #   两只怪离得差不多近时，`get_attack_direction` 返回的方向
                #   会随两怪距离的**帧间微小变化**来回翻面（左、右、左、右…）。
                #   而下面那个条件里 "怪换边 ⇒ 重置转身计时" 是**无条件**的：
                #   每翻一次面，`_turn_left` 就被重置回满帧数 ⇒ 转身**永远转不完**
                #   ⇒ 每一帧都走 `_turn_left > 0` 分支并 `return`
                #   ⇒ **一次招都出不了**，同时方向键每帧都被设成新方向
                #   ⇒ 屏幕上就是"原地左右抽搐"。
                #
                # 【改法】转身进行中（_turn_left > 0）时，**不因换边而重置**；
                #   只把"我要打哪边"记在新属性 `_turn_pending_dir` 上，
                #   等当前这次转完、出招之后，下一轮再处理新方向。
                #   效果：无论目标怎么横跳，**每 turn_frames 帧至少落地一次出招**，
                #   抽搐消失，攻击不再被"犹豫"吃掉。
                #
                # ⚠️ 关键前提：进入这个分支时 `attack_direction` 一定不是 None
                #    （上面 `if attack_direction is None: return` 已经拦掉），
                #    所以"换边"必然是"换成另一个合法方向"，记下来是安全的。
                # ⚠️ 保留原有的"朝向不对 ⇒ 起转"语义：_turn_left <= 0 时照旧。
                _turn_dir = getattr(self, "_turn_dir", None)
                if _turn_left <= 0:
                    if self.cmd_move_x_last != attack_direction:
                        _turn_left = max(1, _turn_frames)
                        self._turn_dir = attack_direction
                    elif _turn_dir != attack_direction:
                        # 已经朝着目标、只是记录的转身方向过期 → 同步一下即可
                        self._turn_dir = attack_direction
                elif _turn_dir != attack_direction:
                    # 转身进行中且目标换边：不改计时，只记下"转完打哪边"
                    self._turn_pending_dir = attack_direction
                if _turn_left > 0:
                    self.cmd_move_x = attack_direction
                    self._turn_frames_left = _turn_left - 1
                    logger.debug(
                        f"[转向] 目标在{attack_direction}侧，按住方向键转身"
                        f"（还剩 {self._turn_frames_left} 帧，本帧不出招）")
                    return
                self._turn_frames_left = 0
                # 朝向已就位 → 松开方向键再出招（用户要的「回头打一下就继续走」：
                # 出招后本函数不再改 cmd_move_x，下一帧由路线接管，角色继续巡逻）
                # ⚠️ 若转身期间目标换过边，把"最新方向"落到 _turn_dir，
                #    免得下一帧拿旧方向再判一次"换边"、白转一轮。
                if getattr(self, "_turn_pending_dir", None) is not None:
                    self._turn_dir = self._turn_pending_dir
                    self._turn_pending_dir = None
                self.cmd_action = "attack"
                self.cmd_move_x = "none"
                self._note_attack(len(_engage))       # 纯观测
                self.t_last_attack = time.time()

    def update_cmd_by_random(self):
        '''
        update_cmd_by_random - pick a random action except 'up' and teleport command
        '''
        self.cmd_move_x = random.choice(["left", "right", "none"])
        self.cmd_move_y = random.choice(["down", "none"])
        self.cmd_action = random.choice(["jump", "none"])
        logger.warning("[update_cmd_by_random]"\
                    f"{self.cmd_move_x} {self.cmd_move_y} {self.cmd_action}")

    def check_reach_goal(self):
        # ── 回正状态下：goal **不再是**退出条件（设计决策⑤）───────────────────
        # 为什么去掉：goal 圆点（半径 2 = 13px）比一整条短回正线的一半还大，
        # 而 search_range=10 > 整条线长 → 角色**站在落点时**最近的回正线像素
        # 可能就是 goal（黄） → 一进回正就被判"走完了"，原地退出（坑4）。
        # 退出改成「两条线谁更近」（见 try_return_to_mainline / _home_line_keep）。
        if self.is_using_home_route:
            if self.cmd_action == "goal" and \
                    time.time() - self.home_last_goal_log > 10:
                self.home_last_goal_log = time.time()
                tol = int(home_params(self.cfg)["home_dist_tol"])
                logger.info(
                    "[回正] 已踩到回正线的终点标记；是否真的回到主线由**两线距离**判定"
                    f"（当前 离回正线 {self.home_d_home}px / 离主线 {self.home_d_main}px，"
                    f"要满足「离回正线 < 离主线 - {tol}」才继续走回正线）"
                    f"—— 这样短回正线才不会一进就出。")
            return
        # ⚠️ 不能只认 cmd_action == "goal"：goal 圆点半径只有 2px，而玩家坐标是质心、
        #    踩不中就判不到 —— 平台本来就不长时会**走过去却不触发切段**，
        #    冲出去后再也找不到像素 → 一路往前（用户实测："到终点停不下来"）。
        #    所以再用「离本段终点够近」补一次判定（见 _near_seg_goal）。
        #    ⚠️ 原注释写的是"一帧能移动十几像素"，那是**推断、从没实测**（2026-09-16 订正：
        #       实测 ~0.3 px/帧，差 20~50 倍）。别再引用那个数字。
        #
        # ★★ 2026-09-27 加：**`goal` 这条路径也要遵守"高度接近"**（治「没爬到顶就切段」）
        #
        #   为什么必须补这一层（真机现场，用户 minimaps/废都南方工地）：
        #     route1 的 goal 圆点在 **y=90~94**（高台上），梯子从 y=109 爬到 93，
        #     地面在 y=116 ⇒ 全程爬升 23px。而 goal 的**探测半径是 search_range=10**
        #     ⇒ 角色爬到 **y=100** 时，到 goal 的距离就是 8 < 10 → `_scan` 命中了 goal
        #     → `self.cmd_action = "goal"` → **下面这行直接判"到达"→ 切段**。
        #     可此时 Δy = |100-93| = 7，**人还在梯子半中腰**。
        #     ⇒ 切到回程段(route2) 后，那段在同一根梯子上标的是 **down**
        #       ⇒ 角色**爬到一半掉头往下爬**（用户原话）。
        #
        #   ⚠️ 上一轮只给 `_near_seg_goal()` 加了高度容差（goal_reach_y_tol），
        #      但**这条 `cmd_action == "goal"` 是另一条路径、绕过了它** ⇒ 没治住。
        #      两条路径必须用**同一个高度判据**，否则又是一次"改一处漏一处"。
        #
        #   ⚠️ 容差与 `_near_seg_goal` 共用 route.goal_reach_y_tol（默认 3）；
        #      填 0 = 两边一起关掉（退回只看"有没有踩到 goal"的老行为）。
        #   ⚠️ 为什么这里可以用"绝对高度"判断：goal 是**录制时人真站过的位置**，
        #      它和"人该在的高度"天然一致 —— 不像虚拟坐标需要额外推算。
        try:
            _goal_y_tol = int(self.cfg.get("route", {}).get("goal_reach_y_tol", 3) or 0)
        except (TypeError, ValueError):
            _goal_y_tol = 3
        _goal_height_ok = True
        _g = None
        if _goal_y_tol > 0 and self._seg_goals:
            _i = self.idx_routes
            if 0 <= _i < len(self._seg_goals):
                _g = self._seg_goals[_i]
        if _g is not None and self.loc_player_global:
            _goal_height_ok = abs(self.loc_player_global[1] - _g[1]) <= _goal_y_tol
            if self.cmd_action == "goal" and not _goal_height_ok:
                # 够到 goal 像素但高度差太大（还在半路）→ **不切段**，
                # 保持本帧指令（继续爬），并节流打一行日志便于排查。
                _now = time.time()
                if _now - getattr(self, "t_goal_height_block_log", 0) > 5:
                    self.t_goal_height_block_log = _now
                    logger.info(
                        f"[切段] 探到 goal 像素，但高度差 "
                        f"{abs(self.loc_player_global[1] - _g[1])}px > 容差 {_goal_y_tol}px "
                        f"（人 y={self.loc_player_global[1]} / 终点 y={_g[1]}）"
                        f"—— 判定**还在半路**，暂不切段，继续按当前段走。\n"
                        f"        想放宽：route.goal_reach_y_tol（当前 {_goal_y_tol}）")
                return
        reached = (self.cmd_action == "goal") or self._near_seg_goal()
        if not reached:
            return
        # 没有主线路线时下面的取模会 ZeroDivisionError 把主循环带崩 —— 原地不动即可
        if not self.img_routes:
            return
        # Switch to next route map
        _old_route = self.idx_routes
        self.idx_routes = (self.idx_routes+1)%len(self.img_routes)
        self.route_reverse = False     # 换段了，往返方向要复位
        # ★ 2026-10-14：原来这里只有 `logger.debug("Change to new route:…")` ——
        #   **debug 不写进日志文件**，于是"段号到底跳没跳、为什么跳"永远查不出来
        #   （用户 2026-10-14 报「从到达最后 route 的终点，会跳跃到后面几个 route」，
        #   而日志里连一条切段记录都没有）。提到 INFO：一次切段就一行，不刷屏，
        #   且带上 from→to、触发原因（踩到 goal / 临近终点）与当时全局坐标。
        logger.info(
            f"[切段] 第 {_old_route + 1}/{len(self.img_routes)} 段 → "
            f"第 {self.idx_routes + 1}/{len(self.img_routes)} 段"
            f"（{'踩到 goal 像素' if self.cmd_action == 'goal' else '离本段终点够近'}；"
            f"全局 {tuple(self.loc_player_global) if self.loc_player_global else None}）")

    def run_once(self):
        '''
        Process one game window frame
        '''
        # Check if need viz window
        self.is_show_debug_window = self.is_need_show_debug_window
        if not self.is_show_debug_window:
            self.img_frame_debug = None
            self.img_route_debug = None

        ###########################
        ### Image Preprocessing ###
        ###########################
        # Get game window frame
        _perf_t0 = time.perf_counter()
        img_frame = self.get_img_frame()
        self._perf("抓帧", _perf_t0)
        if img_frame is None:
            activate_game_window(self.capture.window_title)
            return -1 # Wait for game window to be ready
        else:
            self.img_frame = img_frame

        # Grayscale game window
        self.img_frame_gray = cv2.cvtColor(self.img_frame, cv2.COLOR_BGR2GRAY)

        # Image for debug viz - use the cropped (client area) frame
        if self.is_show_debug_window:
            self.img_frame_debug = self.img_frame.copy()

        # Get current route image
        if self.cfg["bot"]["mode"] == "normal" and self.img_routes:
            # 回正线优先：走丢 / 掉出安全点时临时改用 route_home.png，
            # 走完回正线（goal）或回到安全点后自动切回主线（见 update_cmd_by_*）。
            if self.is_using_home_route and self.img_route_home is not None:
                self.img_route = self.img_route_home
            else:
                # ⚠️ 这里**绝不能**写裸下标 —— 2026-10-12 与 2026-10-14 两次真机停摆
                #    都是同一个 IndexError（按下标取 route 时越界）。
                #    界面按【开始】会在同进程里重跑 load_config（AutoBotController.py:109），
                #    那次调用把 `img_routes` **换成了新列表**，而 `idx_routes` 可能还停在
                #    上一轮的值（重录成更少段 / 换图 / 有段被判废）。
                #    上面那道 `and self.img_routes` 只判"空不空" ⇒ 挡不住这个坑；
                #    崩溃点又排在"路线推进"之前 ⇒ 段号永远回不来 ⇒ 每帧都炸。
                #    所以这里先取一次快照（避免列表对象被换掉的空窗），再带边界取。
                _routes = self.img_routes
                # 下面的判据**按下标取之前先判边界**（自检 `tools/verify_route_index_bounds.py`
                # 【2】① 钉的就是这个字面量形态）；取图时用快照 `_routes`，避免列表对象
                # 正好在两次读之间被 load_config 换掉。
                if 0 <= self.idx_routes < len(self.img_routes):
                    self.img_route = _routes[self.idx_routes]
                else:
                    logger.warning(
                        f"[路线] 段号 {self.idx_routes} 越界（共 {len(_routes)} 段），"
                        f"本帧改用第 1 段并把段号重置（load_config 里也会钳，这里再兜一次）。")
                    self.idx_routes = 0
                    if _routes:
                        self.img_route = _routes[self.idx_routes]
            if self.is_show_debug_window:
                self.img_route_debug = cv2.cvtColor(self.img_route, cv2.COLOR_RGB2BGR)


        ###################
        ### Get Minimap ###
        ###################
        # Get minimap coordinate and size on game window
        _perf_t0 = time.perf_counter()
        minimap_result = get_minimap_loc_size(self.img_frame)
        self._perf("小地图定位", _perf_t0)
        if minimap_result is None:
            if time.time() - self.t_last_minimap_update > 30:
                # Unable to get minimap for 30 seconds -> assume it's login screen
                loc_login_button = self.get_login_button_location()
                if loc_login_button:
                    logger.info("Found login button on screen. Proceed to login.")
                    click_in_game_window(self.capture.window_title,
                                         loc_login_button)
                    time.sleep(3)
                    click_in_game_window(self.capture.window_title,
                                         self.cfg["ui_coords"]["select_character"])
                    time.sleep(2)
        else:
            x, y, w, h = minimap_result
            # Shrink minimap boardary by one pixel to avoid pixel leaking to minimap
            x += 1
            y += 1
            w -= 2
            h -= 2
            # update minimap image
            self.loc_minimap = (x, y)
            self.img_minimap = self.img_frame[y:y+h, x:x+w]
            self.t_last_minimap_update = time.time()


        #################################
        ### Player Location Detection ###
        #################################
        # Get player location in game window
        # 国服无法用组队血条定位，角色名标签（nametag）是画面内的唯一定位方式
        _perf_t0 = time.perf_counter()
        loc_player = self.get_player_location_by_nametag()
        self._perf("名字定位", _perf_t0)

        # 定位健康度告警（2026-09-10 加）
        # 匹配失败时 get_player_location_by_nametag() 仍会返回坐标，只是那个坐标由
        # **上一帧的** self.loc_nametag 算出来 → 位置被「冻住」。分两种情况，严重程度完全不同：
        #   ① 命中过、最近失败 —— 基本无害。同一张地图里角色的**屏幕位置几乎是死的**
        #      （实测：地图A 连续 5 帧角色中心恒为 (711,382)，跨图也只有 y 差 39px），
        #      所以沿用上次命中的位置照样能用。最常见原因：地图拥挤、名牌被别的玩家盖住。
        #   ② **开局至今一次都没命中** —— 严重。位置停在初始值，攻击框落到画面左上角，
        #      症状是「只走路、不打怪」而且**不报错**。必须让用户看见。
        # 每 30 帧只在跨过阈值那一帧告警一次，不刷屏。
        _streak = self.nametag_miss_streak
        if _streak >= 30 and _streak % 30 == 0:
            # ★ 2026-10-14 加：与这条告警**同源**，把"当时的画面"存到 debug/。
            #   纯观测：判据、节流、fail-open 都在 _dump_nametag_miss 里。
            self._dump_nametag_miss(_streak)
            if not self.nametag_ever_hit:
                if getattr(self, "loc_nametag_from_cache", False):
                    # 2026-10-14 加：开局用的是**存盘位置**（上次命中时存下的）。
                    # 比"停在 (0,0)、攻击框跑到左上角"好得多，但角色还在走 ——
                    # 位置是死的，偏差累积超过攻击框半高照样打空，所以照样要吼，
                    # 只是严重程度从 ERROR 降为 WARNING（并非"什么都没得用"）。
                    logger.warning(
                        f"[名字定位] 开局至今 {_streak} 帧没认出角色名名牌"
                        f"（阈值 {self.cfg['nametag']['diff_thres']}），"
                        f"正用**上次保存的位置** {self.loc_player} 出招"
                        f"（存盘文件 {self._nametag_pos_path()}）。\n"
                        f"        注意：角色还在走、位置却是死的 —— 偏得多时会打空"
                        f"（攻击框上下只有 "
                        f"{self.cfg['directional_attack']['range_y']}px）。\n"
                        f"        常见原因：① 地图拥挤、名牌被别人盖住 ⇒ 走到人少处"
                        f"② 名字/称号改过 ⇒ 重新标定名字模板"
                        f"（界面上按 F2「标定名字」；或 python -m tools.calibrate_nametag）\n"
                        f"        ③ 换过角色 / 搬过游戏窗口 ⇒ 删掉上面那个 json 文件即可，"
                        f"下次命中会自动重新写一份")
                else:
                    logger.error(
                        f"[名字定位] 开局至今 {_streak} 帧一次都没定位到角色名名牌"
                        f"（阈值 {self.cfg['nametag']['diff_thres']}）—— 位置仍是初始值，"
                        f"攻击框会落在画面左上角，等于打不到怪。\n"
                        f"        （这张图没有可用的「上次位置」记录："
                        f"{self._nametag_pos_path()} 不存在 / 被删 / 读不动；"
                        f"本图第一次成功命中后会自动写一份）\n"
                        f"        排查：① 当前地图玩家是不是太多、自己的名牌被盖住了？"
                        f"先走到人少处或换个位置\n"
                        f"              ② 角色名/称号改过了吗？改过要重新标定模板\n"
                        f"              ③ 点工具箱「保存诊断包」发给 AI，让他看模板本身还认不认得")
            else:
                # ⚠️★ 2026-09-28 改文案（issue #6 排查）：
                #    原来写的是「位置沿用上次命中的结果 —— 同图内角色屏幕位置基本不动，
                #    影响有限」。这句**在攻击判定上是错的**：
                #      角色屏幕位置变化确实不大，但卡住不动的是"引擎以为角色在哪"，
                #      角色本身还在走 —— 偏差累积超过攻击框半高（range_y/2 = 35px）后，
                #      怪就完全落出攻击框 ⇒ **打不到怪**。
                #    用户实测（issue #6）：miss 连续 650 帧（≈32 秒），期间基本不出招。
                #    ⇒ 文案改为**说清这个后果**，并给出可操作的处理办法。
                #    ⚠️ 这里**只改文案、不改行为**：位置过期时是否该停手，
                #      要先用真机实测标定"偏差多大才开始打空"，不能拍脑袋定阈值。
                logger.warning(
                    f"[名字定位] 已连续 {_streak} 帧没匹配到角色名名牌"
                    f"（阈值 {self.cfg['nametag']['diff_thres']}），"
                    f"位置沿用上次命中的结果。\n"
                    f"        注意：**打怪判定用的就是这个位置**，角色却在继续走 ——"
                    f"偏得多时会打空、甚至朝错方向出招（攻击框上下只有 "
                    f"{self.cfg['directional_attack']['range_y']}px）。\n"
                    f"        常见原因：地图拥挤（名牌被别的玩家盖住）/ 名字或称号改过 / "
                    f"模板没标定好。\n"
                    f"        若长时间不恢复：① 先走到人少处 ② 重新标定名字模板"
                    f"（界面上按 F2「标定名字」；或 python -m tools.calibrate_nametag）")
                # ── 匹配分数量级（2026-09-28 加，issue #6 排查用）────────────
                # 为什么必须打：光报"没命中"没法定位病因 —— 差一点点（模板/阈值问题）
                #   和差很多（名牌被挡 / 位置不对）是**完全不同的两件事**，处理办法也不同。
                #   这里给出「本帧分数 / 近段最低分」，一眼区分开。
                # ⚠️ 与上面那条共用同一个 10 秒节流闸，不会刷屏、不影响帧率。
                # ⚠️ 纯观测：不参与任何判定。
                try:
                    _s = float(getattr(self, "nametag_score", -1.0))
                    _h = list(getattr(self, "_nt_score_hist", None) or [])
                    # ⚠️ 没有分数时**不能**拿 -1.0 去比对 —— 那会落进"接近阈值"分支，
                    #    把"压根没数据"误报成"差一点点"，正好把排查带偏（实测踩过）。
                    #    所以分开判：有数据才给结论，没数据就直说。
                    if _h:
                        _best = min(_h)
                        _near = _best < float(self.cfg["nametag"]["diff_thres"]) * 3
                        _verdict = ("**分数差一点点** ⇒ 阈值/模板可能偏紧，"
                                    "**也可能是名牌被宠物名/称号压住**"
                                    "（真机 2026-10-14 实证：被挡时最佳分就落在这段）——"
                                    "看 debug/nt_miss_*.png 一眼分辨"
                                    if _near else
                                    "**离阈值很远** ⇒ 名牌多半**根本没画出来**"
                                    "（被宠物名/称号盖住、或爬梯时不画），或位置/模板不对")
                        _body = f"本帧 {_s:.4f} / 近段最低 {_best:.4f}"
                    else:
                        _verdict = "**取不到匹配分数**（本次没有可用的匹配结果）"
                        _body = f"本帧 {_s:.4f} / 近段最低 无数据"
                    _th = float(self.cfg["nametag"]["diff_thres"])
                    _tpl = self.cfg["nametag"].get("mode", "?")
                    logger.warning(
                        f"[名字定位·分数] {_body}"
                        f"（阈值 {_th}，模式 {_tpl}）—— {_verdict}\n"
                        f"        把这一行发给开发者即可定位。"
                        f"（想看模板长什么样：工具箱「保存诊断包」）")
                except Exception:                            # noqa: BLE001
                    pass          # 观测代码绝不能影响主循环

        # Update player location
        if loc_player is not None:
            # Check if character is on ladder
            #
            # ⚠️ `dx`/`dy` 是**屏幕坐标**的差值（loc_player 来自名字定位）。
            #    下方新增的判据改用**小地图全局坐标**，原因见那段说明。
            dx = abs(loc_player[0] - self.loc_player[0])
            dy = abs(loc_player[1] - self.loc_player[1])
            #
            # ⚠️★ 2026-09-28 加：**用小地图全局 x 判断"是否还在梯子上"**
            #
            #   现场（用户真机 00:37/00:42 + 逐帧日志）：角色爬到第一个梯子顶（y≈93）后，
            #   **引擎仍继续按 up** ⇒ 正好遇到游戏里第二个真梯子 ⇒ 被带上去
            #   （日志 `x=186 y=86 指令(right up none)` —— x 已从 178 走到 186，
            #     **up 却还在**）。
            #
            #   为什么原来的 `dx > 3` 判不出去：这里的 `dx` 是**屏幕坐标**
            #   （`loc_player` 来自名字定位），而**画面会随角色滚动** ——
            #   爬梯时屏幕 x 的漂移与"真的横向走"混在一起，这个阈值抓不准。
            #   实测：用户往右只走 1~2 个**小地图像素**（全局 x 178→180），
            #   屏幕上却不足以让 dx 稳定超过 3。
            #
            #   ⇒ 用**小地图全局坐标**判断（那才是"角色在平台上的真实横向位移"）：
            #     爬到梯子顶、开始沿平台横向移动时，全局 x 会持续变化 ⇒ 判"已下梯"。
            #   实测依据（用户 00:37 那次日志）：爬梯过程中**全局 x 精确停在 178**
            #     （4 次采样全部 178，零抖动）⇒ 用全局 x 判断**不会误触发**。
            #
            #   ⚠️ 两个条件**任一满足**即下梯：
            #     ①（原有）屏幕 dx > 3 —— 保留，兼容既有行为；
            #     ②（新增）**全局 x 变化 ≥ ladder_leave_x**（默认 2）
            #        —— 爬梯时全局 x 恒定，所以 2 像素就能可靠区分。
            #     可调：route.ladder_leave_x（填 0 = 关掉本层，退回只看屏幕 dx）。
            _gx = getattr(self, "loc_player_global", None)
            _gx_prev = getattr(self, "_ladder_gx", None)
            _gx_jump = None
            if _gx and _gx_prev:
                _gx_jump = abs(int(_gx[0]) - int(_gx_prev[0]))
            if self.is_on_ladder:
                try:
                    _gx_tol = int(self.cfg.get("route", {}).get("ladder_leave_x", 2) or 0)
                except (TypeError, ValueError):
                    _gx_tol = 2
                if dx > 3:  # 原有：屏幕横向移动
                    self.is_on_ladder = False
                elif (_gx_tol > 0 and _gx_jump is not None and _gx_jump >= _gx_tol):
                    # 新增：小地图全局 x 变了 ⇒ 已经在平台上横向移动 ⇒ 下梯
                    self.is_on_ladder = False
                    _now = time.time()
                    if _now - getattr(self, "t_ladder_leave_log", 0) > 2:
                        self.t_ladder_leave_log = _now
                        logger.info(
                            f"[梯子] 全局 x 变化 {_gx_jump}px（{_gx_prev[0]} → {_gx[0]}）"
                            f"⇒ 判定已离开梯子（在平台上横向移动），停止按 up。")
            else:
                if dx < 3 and dy != 0:
                    self.is_on_ladder = True
            # 记下本帧全局 x，供下一帧比较（见上）
            if _gx:
                self._ladder_gx = _gx
            # ── 竖向进展戳（2026-10-14 加，用户拍板 A）—— 让路判据的唯一喂点 ──
            # 只认**小地图全局 y 变化**，不用屏幕 y：
            #   · 示范录制 46 个"整段没动"的梯上段共 1004 帧，全局 y 变化次数
            #     **恰好为 0** ⇒ 零误报；
            #   · 屏幕 y 不行：粘滞段仍有 4.9% 的帧 |Δy|≥3、33% 的帧 ≥1（±1~2px
            #     抖动）⇒ 会把窗口不断重置，旗标永远清不掉（探针 _probe_ladder_yield2
            #     实测：真爬段 ≥3 占 32.1% vs 粘滞段 4.9%，单帧阈值分不开）。
            # 真爬段全局 y 变化间隔 p50=8 / p90=10 帧（27.7fps）⇒ 默认 0.7s 窗口够。
            # ⚠️ 本戳与旗标本身**解耦**：不因为"过期"就把 is_on_ladder 清掉 ——
            #   那面旗子还有 5 处消费点（靠边/锁定/掉梯保护），清掉会牵动路线行为。
            #   这里只影响"要不要给梯子让路"，改动面最小（用户拍板 A 的落地口径）。
            _gy_now = int(_gx[1]) if _gx else None
            if (_gy_now is not None and self._ladder_gy is not None
                    and _gy_now != self._ladder_gy):
                self.t_ladder_move = time.time()
            if _gy_now is not None:
                self._ladder_gy = _gy_now
            # logger.info((self.is_on_ladder, dx, dy))
            # Update player location
            self.loc_player = loc_player

        # Draw player center for debugging
        cv2.circle(self.img_frame_debug,
                self.loc_player, radius=3,
                color=(0, 0, 255), thickness=-1)

        # Get player location on minimap
        loc_player_minimap = get_player_location_on_minimap(
                                self.img_minimap,
                                minimap_player_color=self.cfg["minimap"]["player_color"])
        if loc_player_minimap:
            self.loc_player_minimap = loc_player_minimap

        # Get player location on global map
        self.loc_player_global = self.get_player_location_on_global_map()


        #######################
        ### Attack WatchDog ###
        ####################### Check if last attack is timeout
        dt = time.time() - self.t_last_attack
        if self.cfg['bot']['mode'] == 'normal' and \
            dt > self.cfg["watchdog"]["last_attack_timeout"]:
            logger.info(f"[Attack Timeout] Last attack timeout for {round(dt, 2)} seconds")
            # 换频道已移除（国服到处是人，换频道没意义）→ 超时后统一「回城并停止脚本」
            logger.info("[Attack Timeout] Return home!")
            press_key(self.cfg["key"]["return_home"])
            self.is_terminated = True
            self.kb.is_terminated = True


        ######################
        ### State Behavior ###
        ######################
        self.fsm.do_state_stuff()

        # ── 血蓝监控 + 自动喝药（2026-09-27 加）──────────────────────────
        # 放在 do_state_stuff **之后**的原因：
        #   ① 位置紧跟各状态算完指令，与 [运行状态] 日志同一段；
        #   ② 喝药按键与移动/攻击键是**独立**的（药水键不参与方向指令），
        #      放前面还是后面都不影响本帧指令，但放后面能让日志顺序更好读。
        # 用 self.img_frame（本帧的游戏画面）识别，不用 img_frame_debug ——
        # debug 图会被画上圈圈叉叉，颜色会污染 HSV 判据。
        if self.img_frame is not None:
            _perf_t0 = time.perf_counter()
            self._step_bars_and_potions(self.img_frame)
            self._perf("血蓝喝药", _perf_t0)

        self.is_first_frame = False

        # 运行状态日志（每 5 秒一条，见 __init__ 里的说明）。
        # 位置有讲究：放在这里 = 指令刚由 hunting.on_frame 算完、且**还没**走到
        # 「关掉可视化就提前 return」那一步，所以关掉可视化时也照样打。
        self.n_frame += 1
        if time.time() - self.t_last_status_log >= 5:
            self.t_last_status_log = time.time()
            logger.info(
                f"[运行状态] 帧{self.n_frame}"
                f" 小地图{self.loc_player_minimap}"
                f" 全局{self.loc_player_global}"
                f" 段{self.idx_routes}/{len(self.img_routes)}"
                f"{' 回正' if self.is_using_home_route else ''}"
                f" 指令({self.cmd_move_x} {self.cmd_move_y} {self.cmd_action})"
                f" 怪{len(self.monsters)}"
                f" 名字命中{self.nametag_hit}(miss{self.nametag_miss_streak})")
            # 2026-10-14 加：[出招] 心跳（纯观测，紧跟 [运行状态] 的 5 秒节拍）
            self._log_attack_heartbeat()
            # 2026-10-14 加（P4，用户 m10830）：[开火预算] —— 把"看见→出刀"拆成
            #   三段 + 覆盖率 + 没打成的账（同一节拍；跟 detect_log 同一个开关）
            self._log_fire_budget()

        # ── 性能埋点：紧跟 [运行状态]，同样每 5 秒一条 ─────────────────
        # 位置有讲究：必须在「关掉可视化就提前 return」**之前** —— 这次排查
        # 正是要对比"开 / 关画面页签"两种场景，关掉时它也必须打得出来。
        if getattr(self, "perf_on", False) and \
                time.time() - getattr(self, "t_last_perf_log", 0) >= 5:
            try:
                _n = max(1, int(getattr(self, "perf_n", 0)))
                _win = max(1e-6, time.time() - self.t_last_perf_log)
                _acc = getattr(self, "perf_acc", None) or {}
                _cnt = getattr(self, "perf_cnt", None) or {}
                _mx = getattr(self, "perf_max", None) or {}
                logger.info(perf_summary_line({
                    "fps": _n / _win,
                    "n": _n,
                    "viz": bool(getattr(self, "is_show_debug_window", False)),
                    "gap_ms": float(getattr(self, "perf_gap", 0.0)) * 1000.0 / _n,
                    # ★ 分母用**实际执行次数**而不是帧数：降频的阶段（怪检测）
                    #   不能按帧数摊薄，否则日志显示的是假数字（502→251）。
                    "items": [(k, v * 1000.0 / max(1, _cnt.get(k, _n)),
                               _mx.get(k, 0.0) * 1000.0)
                              for k, v in _acc.items()],
                }))
            except Exception:                                # noqa: BLE001
                pass            # 打日志失败绝不能影响主循环
            finally:
                # 清零必须在 finally：就算抛异常也要滚窗口，否则窗口越滚越大
                self.perf_acc = {}
                self.perf_cnt = {}
                self.perf_max = {}
                self.perf_n = 0
                self.perf_gap = 0.0
                self.t_last_perf_log = time.time()


        #####################
        ### Debug Windows ###
        #####################
        # Don't show debug window to save system resource
        if not self.is_show_debug_window:
            return 0 # frame done

        # Print text on debug image
        _perf_t0 = time.perf_counter()
        self.update_info_on_img_frame_debug()
        self._perf("viz画图", _perf_t0)

        # Save debug window to video
        if self.video_writer:
            self.video_writer.write(self.img_frame_debug)

        # Resize img_route_debug for better visualization
        # ⚠️ 必须判空：这张图还没录路线时 img_route_debug 是 None，
        #    cv2.resize(None, ...) 会抛异常 → 主循环崩（2026-09-12 实测抓到）。
        if self.cfg["bot"]["mode"] == "normal" and \
                self.img_route_debug is not None:
            self.img_route_debug = cv2.resize(
                        self.img_route_debug, (0, 0),
                        fx=self.cfg["minimap"]["debug_window_upscale"],
                        fy=self.cfg["minimap"]["debug_window_upscale"],
                        interpolation=cv2.INTER_NEAREST)


        # Update FPS timer
        self.t_last_frame = time.time()

        return 0 # frame done

    def _perf(self, key, t0):
        '''主循环性能埋点：把一个阶段的耗时累加进当前 5 秒窗口（**纯观测**）。

        用法：在要测的代码段前后各加一行 ——
            _t = time.perf_counter()
            ... 要测的代码 ...
            self._perf("阶段名", _t)

        治的是什么（2026-09-14 真机实测）：配置 `fps_limit_main: 30`，实测主循环
        只有 **1.8~2 fps**，而全项目原来**没有任何地方记录主循环实际帧率**
        → "一帧 500ms 花在哪"完全靠猜。这套累加器就是量它用的。

        ⚠️ 为什么全程 `try/except` + `getattr` 兜底（三重保险，别删）：
            ① 它跑在**主循环**里 —— 抛异常 = 整个挂机停摆，
               这比"埋点没数据"严重一万倍；
            ② `tools/verify_navigation.py` 的 Stub **不走 `__init__`**，
               直接 `self.xxx` 会 AttributeError（这已经是第三次踩这个坑）。

        Args:
            key: 阶段名（中文，会直接进日志，如 "怪检测"）
            t0:  该阶段开始时的 `time.perf_counter()` 值
        '''
        try:
            if not getattr(self, "perf_on", False):
                return
            acc = getattr(self, "perf_acc", None)
            if acc is None:
                return
            d = time.perf_counter() - t0
            acc[key] = acc.get(key, 0.0) + d
            # 记**实际执行次数**（降频的阶段会小于帧数，见 perf_cnt 的说明）
            cnt = getattr(self, "perf_cnt", None)
            if cnt is not None:
                cnt[key] = cnt.get(key, 0) + 1
            mx = getattr(self, "perf_max", None)
            if mx is not None and d > mx.get(key, 0.0):
                mx[key] = d
        except Exception:                                   # noqa: BLE001
            pass        # 埋点自身绝不允许把主循环带崩

    def loop(self):
        '''
        Auto Bot main loop
        Only run when call autobot from UI framework and AutoBotController
        '''
        # Activate game window before starting
        activate_game_window(self.capture.window_title)
        time.sleep(0.3)

        while not self.kb.is_terminated:

            t_start = time.time()
            # ── 性能埋点：帧间隔（**含限帧 sleep**）与本窗口帧数 ──────────
            # 必须放这里而不是 run_once 里：run_once 可能提前 return -1（抓帧失败等），
            # 那些帧也得算进来，否则 fps 会虚高、真实卡顿看不出来。
            if getattr(self, "perf_on", False):
                try:
                    _now = time.time()
                    self.perf_gap += max(0.0, _now - self.t_perf_prev)
                    self.t_perf_prev = _now
                    self.perf_n += 1
                except Exception:                            # noqa: BLE001
                    pass

            # Process one game window frame
            self.is_frame_done = False
            # ⚠️ 必须包住（2026-09-12 加）：原来这里没有任何异常处理，run_once 一抛异常
            #    这个线程就**静默死亡** —— 键盘线程还在跑，但指令永远停在 "none none none"，
            #    于是「点开始、角色不动」，而日志里**一个字都没有**（异常堆栈只进 stderr，
            #    不进日志文件）。用户实际就撞上了这个：配置缺 nametag.mode → 第一帧
            #    KeyError → 挂机彻底不动且无从查起。现在异常必须落进日志。
            try:
                _perf_t0 = time.perf_counter()
                ret = self.run_once()
                self._perf("整帧", _perf_t0)
                if self.n_loop_err:
                    # ⚠️ 2026-09-27：这条原来**每次「失败→成功」都打一遍**，
                    #    而"每帧抛异常、偶尔成功一帧"的场景下它同样被刷爆
                    #    （用户日志里出现 13 次，与堆栈一起把有用信息淹掉）。
                    #    恢复本身是好消息、信息量低，只在**累计失败超过 5 帧**时
                    #    报一次即可 —— 偶发单帧抖动不值得留痕。
                    if self.n_loop_err > 5:
                        logger.info(
                            f"[主循环] 已从异常中恢复（此前连续失败 {self.n_loop_err} 帧）")
                    self.n_loop_err = 0
            except Exception as e:
                self.n_loop_err += 1
                now = time.time()
                # ★ 2026-09-27 修「堆栈刷屏」（issue #2 用户日志实证）：
                #   原逻辑是「n_loop_err == 1 就打完整堆栈」，但 n_loop_err 在
                #   **成功一帧后会被清零**（见上面 if self.n_loop_err 那段）。
                #   于是"失败一帧 → 成功一帧 → 再失败"这种交替场景下，
                #   每次都重新从 1 开始 ⇒ **每帧都打一遍十几行完整堆栈**。
                #   用户日志实测：1 秒内好几轮，几十行堆栈把有用信息全埋了。
                #   ⇒ 改成**按时间节流**（与项目其它日志节流同一套做法）：
                #     同一类异常 5 秒内只详细报一次，其余静默计数；
                #     打印时带上累计次数，避免"看起来只错了一次"的误导。
                if now - getattr(self, "t_last_loop_err", 0.0) > 5:
                    self.t_last_loop_err = now
                    # 距上次 ≥5 秒 → 详细报（含完整堆栈）
                    logger.error(
                        f"[主循环] run_once 抛异常，挂机已停止推进"
                        f"（累计失败 {self.n_loop_err} 帧）。完整堆栈如下：\n"
                        + traceback.format_exc())
                ret = -1

            # Only proceed if the frame is valid
            if ret == 0:
                _perf_t0 = time.perf_counter()
                # Draw image on debug window
                if self.is_show_debug_window and self.is_ui:
                    # ⚠️ 必须判空（2026-09-12）：没有路线图时（这张图还没录路线）
                    #    img_route_debug 是 None，原来直接 .copy() 会 AttributeError，
                    #    而 viz 一崩主循环就跟着崩 —— 「打开游戏画面页签反而把挂机搞停」。
                    if self.img_frame_debug is not None:
                        self.image_debug_signal.emit(self.img_frame_debug.copy())
                    if self.img_route_debug is not None:
                        self.route_map_viz_signal.emit(self.img_route_debug.copy())
                self._perf("调试图emit", _perf_t0)
            else:
                pass
                # logger.warning("Skipped debug window update due to invalid frame.")

            self.is_frame_done = True

            # Cap FPS to save system resource
            frame_duration = time.time() - t_start
            target_duration = 1.0 / self.cfg["system"]["fps_limit_main"]
            if frame_duration < target_duration:
                time.sleep(target_duration - frame_duration)

def main(args):
    '''
    This main function works as a fake autoBotController
    This function will only be called when the using terminal to
    run this script
    '''
    #####################
    ### Init Auto Bot ###
    #####################
    try:
        mapleStoryAutoBot = MapleStoryAutoBot(args)
    except Exception as e:
        logger.error(f"MapleStoryAutoBot Init failed: {e}")
        sys.exit(1)
    else:
        logger.info("MapleStoryAutoBot Init Successfully")

    ####################
    ### Apply Config ###
    ####################
    # Load defautl yaml config
    cfg = load_yaml("config/config_default.yaml")
    # Override with user customized config
    cfg = override_cfg(cfg, load_yaml(f"config/config_{args.cfg}.yaml"))
    # Dump config to log for debugging
    logger.debug(yaml.dump(cfg, sort_keys=False,
                 indent=2, default_flow_style=False))
    # autoBot load config
    mapleStoryAutoBot.load_config(cfg)

    #####################
    ### Start AutoBot ###
    #####################
    try:
        mapleStoryAutoBot.start() # Start all threads in autoBot
    except Exception as e:
        logger.error(f"MapleStoryAutoBot start failed: {e}")
        mapleStoryAutoBot.terminate_threads() # Terminate all threads
        sys.exit(1)
    else:
        logger.info("MapleStoryAutoBot Start Successfully")

    # Start record game window for debugging
    if args.record:
        mapleStoryAutoBot.start_record()

    kb_listener = KeyBoardListener(is_autobot=True)
    kb_listener.register_func_key_handler('f1', mapleStoryAutoBot.kb.toggle_enable)
    kb_listener.register_func_key_handler('f2', mapleStoryAutoBot.screenshot_img_frame)
    kb_listener.register_func_key_handler('f12', mapleStoryAutoBot.terminate_threads)

    # While loop
    while not mapleStoryAutoBot.is_terminated:
        # Show debug image on window
        if mapleStoryAutoBot.is_frame_done:
            if mapleStoryAutoBot.img_frame_debug is not None:
                cv2.imshow("Game Window Debug",
                    mapleStoryAutoBot.img_frame_debug[:
                        mapleStoryAutoBot.cfg["ui_coords"]["ui_y_start"], :])

            if mapleStoryAutoBot.img_route_debug is not None:
                cv2.imshow("Route Map Debug", mapleStoryAutoBot.img_route_debug)

        cv2.waitKey(1)

        time.sleep(0.01)

    #########################
    ### Terminate AutoBot ###
    #########################
    mapleStoryAutoBot.terminate_threads() # Terminate all threads

    cv2.destroyAllWindows()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--disable_control',
        action='store_true',
        help='Disable simulated keyboard input'
    )

    parser.add_argument(
        '--cfg',
        type=str,
        default='custom',
        help='Choose customized config yaml file in config/'
    )

    parser.add_argument(
        '--debug',
        action="store_true",
        help="Enable debug logging"
    )

    parser.add_argument(
        '--record',
        action="store_true",
        help="Record debug window"
    )

    parser.add_argument(
        '--disable_viz',
        action="store_true",
        help="Disable viz debug window"
    )

    parser.add_argument(
        '--test_image',
        default="",
        help="Pass in image in test/XXX.png"
    )

    parser.add_argument(
        '--init_state',
        default="",
        help="choose the init_state"
    )

    args = parser.parse_args()
    args.is_ui = False # Always set False for command line

    # Set logger level
    if args.debug:
        logger.set_level(logging.DEBUG)

    main(args)
