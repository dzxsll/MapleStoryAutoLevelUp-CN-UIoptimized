'''
Auto generate route map 
'''
# Standard import
import time
import argparse
import sys
import os
import shutil
import threading

# CV import
import numpy as np
import cv2
import yaml

# local import
from src.utils.logger import logger
from src.utils.common import (
    find_pattern_sqdiff, draw_rectangle, screenshot,
    get_minimap_loc_size, get_player_location_on_minimap,
    to_opencv_hsv, load_yaml, override_cfg, load_image, imwrite_unicode,
    crop_frame_to_client, active_config_path, put_text_cn,
    resolve_title_bar_height,
)
from src.utils.paths import resource_path
from src.utils.home_route import color_code_maps, collect_pixels
from src.input.KeyBoardListener import KeyBoardListener
from src.input.GameWindowCapturor import GameWindowCapturor

class RouteRecorder():
    '''
    Route recorder
    '''
    def update_info_on_img_frame_debug(self):
        '''
        update_info_on_img_frame_debug
        '''
        # Print text at bottom left corner
        self.fps = round(1.0 / (time.time() - self.t_last_frame))
        text_y_interval = 23
        text_y_start = 550
        # 2026-09-12：主入口改成界面上的「录制」置顶面板（纯鼠标）。
        # 这里只作辅助说明 —— 录的时候多半在看游戏，这个调试窗是第二眼才看。
        if self._home_recording:
            home_state = "　[回正线录制中：走回主线后按 F6 保存]"
        elif self._home_saved:
            home_state = "　[回正线已存]"
        else:
            home_state = ""
        text_list = [
            f"FPS: {self.fps}    {'[录制中]' if self.is_enable else '[已暂停]'}",
            f"已保存路段: {self.idx_routes} 段{home_state}",
            "主操作：界面上的「录制」面板点按钮（鼠标）",
            "F1 暂停/继续   F3 存这段   F8 折返分段",
            "F2 截图   F6 回正线(两次)   F7 放弃   q 结束",
        ]
        # ⚠️ 中文必须用 PIL 画（put_text_cn）：cv2.putText 的 Hershey 字体没有中文字形，
        #    中文会整串变成 "???" （2026-09-12 修，与引擎调试图同一套做法）。
        for idx, text in enumerate(text_list):
            put_text_cn(
                self.img_frame_debug, text,
                (10, text_y_start + text_y_interval*idx),
                (0, 0, 255), font_size=21, thickness=2
            )

        # Draw minimap rectangle on img debug
        draw_rectangle(
            self.img_frame_debug,
            self.loc_minimap,
            self.img_minimap.shape[:2],
            (0, 0, 255), "minimap",thickness=1
        )

        # Compute crop region with boundary check
        crop_w, crop_h = 80, 80
        x0 = max(0, self.loc_player_global[0] - crop_w // 2)
        y0 = max(0, self.loc_player_global[1] - crop_h // 2)
        x1 = min(self.img_route_debug.shape[1], x0 + crop_w)
        y1 = min(self.img_route_debug.shape[0], y0 + crop_h)

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
        y_paste = 70
        self.img_frame_debug[y_paste:y_paste + h_crop, x_paste:x_paste + w_crop] = mini_map_crop

        # Draw border around minimap
        cv2.rectangle(
            self.img_frame_debug,
            (x_paste, y_paste),
            (x_paste + w_crop, y_paste + h_crop),
            color=(255, 255, 255),   # White border
            thickness=2
        )

    def update_img_frame_debug(self):
        '''
        update_img_frame_debug
        '''
        cv2.imshow("Game Window Debug",
                   self.img_frame_debug[:self.cfg["ui_coords"]["ui_y_start"], :])
        # Update FPS timer
        self.t_last_frame = time.time()

    def get_player_location_on_global_map(self):
        '''
        get_player_location_on_global_map
        '''
        if self.is_first_frame:
            # 地图是 minimap copy + padding 初始化的，玩家位置是**已知的**：
            # minimap 嵌在 img_map 的 (pad, pad) 处，玩家相对 minimap 的偏移已知
            # (self.loc_player_minimap)。走模板匹配 cv2 在小图上经常返回 (0, 0)
            # 或乱值（边界效应）—— 用户实测 "当前 (0,0)" 就是这个症状。
            pad = self.cfg["route_recoder"]["map_padding"]
            self.loc_minimap_global = (pad, pad)
            loc_player_global = (
                pad + self.loc_player_minimap[0],
                pad + self.loc_player_minimap[1],
            )
            score = 1.0
        else:
            # 2026-09-13 修：原写法没传 last_result，每帧在小图上做全图匹配，
            # cv2 在小图上经常返回 (0, 0) 或乱值（line 119 注释也承认了）——
            # 用户实测「当前 (0,0)」就是这个症状。传 last_result 用局部搜索
            # （近 5 帧只能小幅移动）能稳定得多。
            self.loc_minimap_global, score, _ = find_pattern_sqdiff(
                                            self.img_map,
                                            self.img_minimap,
                                            last_result=self.loc_minimap_global)
            # 匹配质量太差就别更新（避免坏值跳回 (0,0)），保持上一帧位置
            if score > self.cfg.get("route_recoder", {}).get(
                    "minimap_match_threshold", 0.4):
                # 复用旧的 loc_minimap_global，只更新 loc_player_global
                loc_player_global = (
                    self.loc_minimap_global[0] + self.loc_player_minimap[0],
                    self.loc_minimap_global[1] + self.loc_player_minimap[1]
                )
            else:
                loc_player_global = (
                    self.loc_minimap_global[0] + self.loc_player_minimap[0],
                    self.loc_minimap_global[1] + self.loc_player_minimap[1]
                )

        # Draw local minimap rectangle
        camera_bottom_right = (
            self.loc_minimap_global[0] + self.img_minimap.shape[1],
            self.loc_minimap_global[1] + self.img_minimap.shape[0]
        )
        cv2.rectangle(self.img_route_debug, self.loc_minimap_global,
                      camera_bottom_right, (0, 255, 255), 1)
        cv2.putText(
            self.img_route_debug,
            f"Minimap,score({round(score, 2)})",
            (self.loc_minimap_global[0], self.loc_minimap_global[1]+15),
            cv2.FONT_HERSHEY_SIMPLEX, 0.4,
            (0, 255, 255), 1
        )

        # Draw player center
        cv2.circle(self.img_route_debug,
                   loc_player_global, radius=2,
                   color=(0, 255, 255), thickness=-1)

        return loc_player_global

    def replace_color_on_map(self, lower_hsv, upper_hsv, replace_color=(0, 0, 0)):
        '''
        Replace pixels in self.img_map that fall within the given HSV range
        and are part of a connected component with area > 15.
        '''
        hsv_map = cv2.cvtColor(self.img_map, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv_map, to_opencv_hsv(lower_hsv), to_opencv_hsv(upper_hsv))

        # Connected components
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)

        for i in range(1, num_labels):  # skip background
            area = stats[i, cv2.CC_STAT_AREA]
            if area > 10:
                component_mask = (labels == i)
                self.img_map[component_mask] = replace_color

    def _win_diag(self):
        """抓帧失败那一刻的窗口状态诊断串（2026-09-15 加）。

        为什么要它：用户反馈"录制时游戏明明是聚焦状态"，而日志里只有一句
        "还没抓到游戏画面" —— 没有任何可判断的信息，只能靠猜
        （我已经猜错两次：残留进程、DPI，都被对照实验否掉了）。
        这里把**失败那一刻**的窗口状态原样记下来，下次复现就能直接看出是哪种：
        最小化 / 不可见 / 尺寸不对 / 被别的窗口挡住 / 句柄失效。
        """
        try:
            import win32gui
            h = getattr(self.capture, "window_hwnd", None)
            if not h:
                return "窗口句柄未知"
            fg = win32gui.GetForegroundWindow()
            fg_title = win32gui.GetWindowText(fg) if fg else ""
            # ⚠️ 2026-09-27：每个查询各自 try —— 原来整串共用一个 try，
            #    只要 GetWindowRect 一次失败（句柄刚失效 / 权限问题），
            #    整条诊断就退化成一句"窗口诊断失败：..."，把**本来拿得到的**
            #    客户区尺寸一起吃掉。而尺寸恰恰是 issue #2 的真因所在。
            #    ⇒ 谁失败谁显示"取不到"，不影响其它字段。
            def _q(fn, default="?"):
                try:
                    return fn()
                except Exception:
                    return default

            rect_txt = _q(lambda: str(win32gui.GetWindowRect(h)), "取不到")
            # 客户区尺寸 vs 配置尺寸（口径说明见下）
            try:
                cl = win32gui.GetClientRect(h)                 # (left, top, right, bottom)
                cw, ch = int(cl[2] - cl[0]), int(cl[3] - cl[1])
                th, tw = (self.cfg.get("game_window", {}).get("size") or [None, None])
                if th and tw:
                    _ok = "✓尺寸相符" if (cw, ch) == (int(tw), int(th)) else \
                          f"✗差了 {cw - int(tw):+d}×{ch - int(th):+d}(宽×高)"
                    size_txt = f"客户区={cw}x{ch} vs 配置={int(tw)}x{int(th)} {_ok}"
                else:
                    size_txt = f"客户区={cw}x{ch}（配置未声明 size）"
            except Exception:
                size_txt = "客户区尺寸取不到"
            return (f"IsWindow={_q(lambda: bool(win32gui.IsWindow(h)))} "
                    f"IsIconic={_q(lambda: bool(win32gui.IsIconic(h)))} "
                    f"IsVisible={_q(lambda: bool(win32gui.IsWindowVisible(h)))} "
                    f"rect={rect_txt} {size_txt} "
                    f"是前台={fg == h}（当前前台={fg_title!r}）")
        except Exception as e:                                # noqa: BLE001
            return f"窗口诊断失败：{e}"

    def _no_frame_reason(self):
        """抓不到画面时，按**真实原因**给一句能直接照做的提示（2026-09-27 加）。

        ⚠️ 为什么必须分段判原因（issue #2 的真实教训）：
          原实现不管什么原因，一律打「等待游戏窗口聚焦。若一直卡在这里：
          游戏窗口是否被挡住/最小化？」—— 而 issue #2 的真因**与前台无关**，
          是**窗口客户区尺寸比 config 声明的小**（配置 1366x768，实测 1282x707）
          ⇒ crop_frame_to_client 每次返回 None ⇒ 10 秒后 sys.exit(2)。
          用户顺着这句提示去查前台、查双屏、换单屏，全是错方向，
          最后把问题描述成"无法识别前台"报了上来。

        Returns:
            (str 原因短语, str 建议操作)
        """
        try:
            import win32gui
            h = getattr(self.capture, "window_hwnd", None)
            if not h or not win32gui.IsWindow(h):
                return ("窗口句柄已失效（游戏可能已经关了）",
                        "重新打开游戏，再按 F4 重来。")
            if win32gui.IsIconic(h):
                return ("游戏窗口被最小化了（抓屏抓不到最小化的窗口）",
                        "点任务栏上的游戏窗口把它还原（别最小化），再按 F4。")
            if not win32gui.IsWindowVisible(h):
                return ("游戏窗口被隐藏了",
                        "在任务栏上把它显示出来，再按 F4。")
            cl = win32gui.GetClientRect(h)
            cw, ch = int(cl[2] - cl[0]), int(cl[3] - cl[1])
            th, tw = (self.cfg.get("game_window", {}).get("size") or [None, None])
            if th and tw and (cw, ch) != (int(tw), int(th)):
                return (
                    f"游戏窗口的客户区尺寸是 {cw}x{ch}（宽x高），"
                    f"而配置声明的是 {int(tw)}x{int(th)} —— 对不上，"
                    f"裁剪这一步每次都失败，所以一帧都拿不到。",
                    f"把游戏窗口调到 **{int(tw)}x{int(th)}**（不含标题栏），"
                    f"或运行 `python -m tools.measure_window --write-config` "
                    f"按你现在的窗口尺寸改配置。\n"
                    f"        注意：这与「前台 / 双屏」无关，不用查那些。")
            frame = getattr(self.capture, "frame", None)
            if frame is None:
                return ("窗口是好的，但抓帧库一帧都没送上来"
                        "（Graphics Capture 与被抓窗口的渲染方式不兼容）",
                        "试试点一下游戏窗口让它重绘；仍不行就把游戏切到窗口模式"
                        "（别用全屏/无边框），再按 F4。")
            return ("抓到了帧，但内容对不上（多半是帧尺寸与预期不符）",
                    "运行 `python -m tools.measure_window` 看实测口径。")
        except Exception as e:                                # noqa: BLE001
            return (f"诊断时出错：{e}", "运行 `python -m tools.measure_window` 看实测口径。")

    def get_img_frame(self):
        '''
        get_img_frame

        与引擎走同一套裁剪（src.utils.common.crop_frame_to_client）：
        抓窗口的帧含标题栏和 1px 窗口边框，先切标题栏、再锚定左上角裁到 config 声明的
        客户区尺寸 —— 保证路线图坐标和引擎跑起来时用的是同一个坐标系。

        原先这里是「切完标题栏后要求尺寸严格相等，否则报错退出」，
        但抓帧本来就比客户区多 2px 宽 + 1px 高，这个检查必然失败、路线根本录不了。
        '''
        # Get window game raw frame
        self.frame = self.capture.get_frame()
        self._frame_ok = False      # 只有真正裁出画面才会在函数末尾置 True
        if self.frame is None:
            # 首帧竞争（2026-09-12 用户实测被这条吓到过）：抓帧线程启动后第一帧
            # 要几秒才到，之前每帧都会走到这里 —— 只提示等待，不是错误。
            # 2026-09-15 改：不再只写"等待中"，而是把**已经等了多久**和**那一刻的
            #   窗口状态**一起记下来（见 _win_diag）—— 否则事后只能靠猜。
            #   每 2 秒最多一条，避免把日志刷爆；超过 5 秒升级成 WARNING。
            now = time.time()
            if now - self._t_last_noframe_log >= 2.0:
                self._t_last_noframe_log = now
                waited = now - self._t_frame_wait_start
                lvl = logger.warning if waited >= 5.0 else logger.info
                lvl(f"[录制] 还没抓到游戏画面 —— 已等 {waited:.1f}s。\n"
                    f"        {self._win_diag()}")
            return

        # ★ issue #14：与引擎/试读共用同一个自适应取值，保证"录制的坐标系"
        #   == "引擎跑起来时的坐标系"，不出现"录制正常但跑起来偏"。
        title_bar = resolve_title_bar_height(self.cfg, tag="路线录制")
        target_size = self.cfg["game_window"]["size"]          # [高, 宽]

        img, msg = crop_frame_to_client(self.frame, target_size, title_bar, tag="路线录制")
        if img is None:
            # ⚠️ 2026-09-27 修（issue #2）：原来这里**没有降频**、也**没有更新
            #    _t_frame_wait_start**，于是裁剪失败时：
            #      ① 每帧都打一条 ERROR（日志被刷爆）；
            #      ② 等待计时器还停在 0 —— 上面那句"已等 N s"继续从 0 数，
            #         看起来像"刚启动就失败"，掩盖了它其实已经失败了很久。
            #    现在与抓不到帧那条路一样降频，并把原因按 _no_frame_reason 说清。
            now = time.time()
            if now - self._t_last_noframe_log >= 2.0:
                self._t_last_noframe_log = now
                _why, _fix = self._no_frame_reason()
                logger.error(
                    f"[路线录制] 画面尺寸不符：{msg}\n"
                    f"        原因：{_why}\n"
                    f"        怎么办：{_fix}")
            return

        if not self._first_frame_logged:
            # 只打一次（2026-09-15 修）：原来这里判的是 _frame_ok，而它在函数开头
            # 就被置成 False 了 → 于是**每一帧都会打一遍**"首帧到位"，
            # 日志被刷爆（实测：28.8 / 28.9 / 29.0 … 一路刷到 31.3）。
            self._first_frame_logged = True
            logger.info(f"[录制] 首帧到位 —— 等了 "
                        f"{time.time() - self._t_frame_wait_start:.1f}s"
                        f"（{self._win_diag()}）")
        self._frame_ok = True
        return img

    def __init__(self, args):
        '''
        Init MapleStoryBot
        '''
        self.args = args # User arguments
        self.idx_routes = 0 # Index of route map
        self.fps = 0 # Frame per second
        self.is_first_frame = True # first frame flag
        self.is_enable = True
        # Coordinate (top-left coordinate)
        self.loc_minimap = (0, 0) # minimap location on game screen
        self.loc_player = (0, 0) # player location on game screen
        self.loc_player_minimap = (0, 0) # player location on minimap
        self.loc_minimap_global = (0, 0) # minimap location on global map
        self.loc_player_global = (0, 0) # player location on global map
        self.loc_player_global_last = None # playeer location on global map last frame
        # 面板实时反馈（2026-09-12 加）：录制是个"盲操作"—— 用户走了一圈，
        # 结果路线图上一个像素都没有（只留下收尾的 goal 点），却完全不知道为什么。
        # 根因可能是角色没动（游戏没焦点）、按键没捕获、或定位卡住，三者症状一样。
        # 把这三样直接显示在面板上，用户一眼就能分清是哪一种。
        self._drawn_cache = 0          # 当前这段已画的路线像素数
        self._t_last_drawn_count = 0.0
        self._ccodes_bgr = None        # 色码表（BGR 顺序），延迟构造一次
        # Images
        self.frame = None # raw image
        self.img_frame = None # game window frame
        self.img_frame_debug = None # game window frame for visualization
        self.img_route = None # route map
        self.img_route_debug = None # route map for visualization
        self.img_minimap = None # minimap on game screen
        self.img_map = None # map
        # Timers
        self.t_last_frame = time.time() # Last frame timer, for fps calculation
        self.t_last_draw_blob = time.time() # Last draw blob timer
        # 已经画上去的"跳跃/传送"像素 {(x,y): color_bgr} —— 移动线不许把它盖掉
        # （见 update 里的"跳跃色保护"，2026-09-17）
        self._blob_pixels = {}

        # ===== 界面「录制」置顶面板的跨进程通道（2026-09-12）=====
        # 面板跑在界面进程里，本录制器是它的 QProcess 子进程：
        #   界面 → 本进程：stdin 写 `@@CMD save_route` 等命令
        #   本进程 → 界面：stdout 写 `@@STATE routes=1 paused=0` 状态行
        # 下面这批标志由 stdin 线程置位、主循环消费（和键盘热键走同一批方法）。
        self.panel_mode = False       # stdin 是管道 → 面板模式（无控制台）
        self._cmd_save_route = False
        self._cmd_save_map = False
        self._cmd_toggle_pause = False
        self._cmd_screenshot = False
        self._cmd_quit = False
        self._cmd_home_route = False
        self._cmd_home_cancel = False
        self._cmd_home_start = False
        self._cmd_home_end = False
        self._cmd_waypoint = False
        self._cmd_arm = False
        # ── 【开始录】闸门（2026-09-15 加，用户要求"手动录制"）───────────────
        # 为什么必须显式开启：起点 = **第一次按下方向键**那一刻的 loc_player_global
        #   （见主循环记 _route1_start 的那处），而它初值是 (0,0)，要等定位成功
        #   才会变成真实坐标。用户在"还没定位到角色"时按了方向键，起点就被钉在
        #   (0,0) —— 第一笔会从 (0,0) 拉到真实位置，起点是垃圾，而且**事后从画面
        #   上看不出来**（面板上那个 (0,0) 也不显眼）。
        # ⇒ 改成：**点了【开始录】才允许画线**；点之前一笔都不画、起点也不记。
        self._armed = False
        # 本帧定位是否有效（供面板判断"现在能不能开始录"）
        self._pos_ok = False
        # 本帧有没有拿到游戏画面（2026-09-15 加）—— 必须和 _pos_ok 分开报：
        # 面板原来只有 posok 一个信号，于是"**一帧都没抓到**"和"抓到了但没定位到角色"
        # 显示成同一句话"还没找到你的角色"，把用户引到错误的方向去查颜色配置。
        # （2026-09-15 实测：用户就是被这句话带偏的 —— 真因是抓帧失败。）
        self._frame_ok = False
        # 抓帧诊断（2026-09-15 加）：用户反馈"录制时游戏明明是聚焦状态"，而日志里
        # 只有一句"还没抓到游戏画面" —— 没有任何可判断的信息，只能靠猜（已猜错两次）。
        # 这两个时间戳用来：① 记录首帧到底等了多久 ② 给失败日志降频（每 2 秒一条）
        self._t_frame_wait_start = time.time()
        self._t_last_noframe_log = 0.0
        self._first_frame_logged = False   # 「首帧到位」只打一次（见 get_img_frame）
        # 本帧要做的动作（键盘/面板统一收进来的意图，见 _collect_intents）
        self._want_save_route = False # 帧末执行：保证 goal 标记画在段末、不被移动线覆盖
        self._want_save_map = False
        self._want_home_route = False # 回正线开关，同样帧末执行（收尾那次要盖 goal）
        self._want_home_end = False   # 面板【已回到主线】：同样是收尾，帧末执行
        self._want_quit = False
        self._last_state_line = None  # 状态行去重，避免每帧刷屏
        # 周期性推状态的限流时间戳（2026-09-16 加，见 push_state）。
        # 为什么必须周期性推：原来只有"收到命令"时才 _emit_state，于是首帧到位后
        # frameok 从 0 变 1 **没人告诉面板** → 面板永远挂着初始化那句
        # 「还没抓到游戏画面」，而【开始录】按钮要 frameok and posok 才显示
        # → 用户卡在第 1 步，什么都点不了（2026-09-16 实测复现并定位）。
        self._t_last_state_push = 0.0
        self._map_saved = False       # map.png 是否已存过（面板据此提示"下一步结束录制"）
        # ── 回正线（route_home.png）───────────────────────────────────────────
        # 回正线 = 偏离主线 / 掉出平台之后，走回主线（或驻守点）的那条路。
        # 录它是**两步**：F6 起头（清空白板）→ 走回主线 → 再按 F6 收尾（存盘）。
        # 为什么不是"按一下就存"：起点必须是**掉落点**，而玩家从主线走到掉落点的那段
        # 也会被画进白板；一步存会把"走出去"那段也存进回正线 —— 引擎按最近路线像素
        # 找方向时可能挑中那段，反而把人往平台外带。分两步就能只保留"回来的路"。
        self._home_recording = False  # True = 正在录回正线（等【已回到主线】收尾）
        self._home_saved = False      # route_home.png 是否已存过
        self._waypoint_count = 0      # 已记下的折返点数量（= 已分了几段）
        # 回正线的**落点**（v0.9 加）：F6 起头那一刻角色站的位置。
        # ⚠️ v0.9 第二版：**不再往图上盖任何东西了** —— 落点锚（127,0,127）整个取消，
        #    判废也不再看"有没有落点标记"。这里只留作**日志/提示**用
        #    （面板显示「起点 = 你现在站的落点」），不写进 PNG。
        self._home_start_pos = None
        # 第一段的**起点**（2026-09-12 加）：闭环容差是拿它当基准的 ——
        # 最后一段走完会循环回第一段，终点离这个点 ≤10px 才接得上（≤40px 靠救援）。
        # 录回程时面板会实时显示「离起点 N px」，省得事后靠日志量。
        self._route1_start = None
        # 严重错误的统一报告（2026-09-12）：最常见的就是「一直抓不到游戏画面」——
        # 之前 run_once 失败时只是 return -1、陷入循环返回，stdin 命令永远不被消费，
        # 用户按按钮「看起来没反应」。现在记一行 error，退出前发给面板。
        self._err_msg = None
        # 抓第一帧的宽限期（秒）：界面切前台→游戏画出来有延迟，头几帧抓不到很正常，
        # 给足时间再判定失败（配合 UI 侧 _activate_game_or_abort 的 5 秒确认）。
        self._first_frame_grace = 10.0
        self._t_record_start = None

        # Load defautl yaml config
        cfg = load_yaml("config/config_default.yaml")
        # Override with user customized config
        # 2026-09-12 配置方案改造后，用户配置跟随主界面「配置方案」（可能不叫
        # config_custom.yaml）；args.cfg 是旧版参数，仅当**显式**传了时才使用。
        #
        # ⚠️★ 2026-09-17 修：--cfg 的 argparse 默认值原来是 'custom'，而
        #    config/config_custom.yaml 必然存在 ⇒ `if args.cfg` 恒为真 ⇒
        #    active_config_path() **永远走不到** ⇒ 录制器永远读 config_custom.yaml，
        #    主界面选中的方案（用户方案 yaml）**一个字节都没生效**。
        #    后果（真机日志实证）：用户方案里 key.jump='c'，录制器却打印
        #    `[录制] 跳跃键 = 'space'` ⇒ 录制时按 c 一次跳都记不下来，回正线 0 跳跃像素。
        #    现在把默认值改成 None，让「没显式传 --cfg」= 跟随主界面方案。
        custom = None
        legacy = f"config/config_{args.cfg}.yaml" if args.cfg else None
        if legacy and os.path.exists(legacy):
            custom = legacy
        else:
            custom = active_config_path()
        if custom:
            self.cfg = override_cfg(cfg, load_yaml(custom))
        else:
            self.cfg = cfg   # 没有任何用户配置：纯默认值
        logger.info(f"[录制] 用户配置 = {custom or '（无，纯 config_default.yaml）'}")

        # 录制模式（2026-09-12：定点 stand 已按用户要求删除，只剩巡逻一套流程）。
        # ⚠️ 不再跟随 cfg：配置里万一还残留 mode: stand，会让录制面板走进已删的流程。
        self.mode = "normal"

        # Parse color_code
        self.color_code = {
            tuple(map(int, k.split(','))): v
            for k, v in cfg["route"]["color_code"].items()
        }
        color_code_up_down = {
            tuple(map(int, k.split(','))): v
            for k, v in cfg["route"]["color_code_up_down"].items()
        }
        self.color_code.update(color_code_up_down) # Combine both dictionaries

        self.fps_limit = self.cfg["system"]["fps_limit_route_recorder"]

        # Check create new map directory
        # 2026-09-12：录制反馈搬进界面后录制器**不再有控制台**，input() 会立即 EOF。
        # 覆盖确认改由主界面弹窗完成（用户选「清掉重录」时才传 --replace）。
        #
        # 追加模式（--append-home，2026-09-12 用户要求）：给**已经录好的图**补回正线，
        # 旧的主线路线（route{N}.png）一条都不动。做法是直接拿已有的 map.png 当底图
        # （坐标系与旧路线一致），录完只写 route_home.png。
        self.append_home = bool(getattr(args, 'append_home', False))
        self._backup_dir = None   # 「清掉重录」时旧路线的备份目录（收尾时决定删还是还原）
        map_dir = resource_path(os.path.join("minimaps", args.new_map))
        if os.path.exists(map_dir):
            if self.append_home:
                map_png = os.path.join(map_dir, "map.png")
                if not os.path.exists(map_png):
                    logger.error(f"[补录] {map_png} 不存在 —— 这张图没有地图底图，"
                                 f"不能只补回正线。请改用【清掉重录】。")
                    sys.exit(2)
                # 非空 → run_once 里不会改动地图，只把路线画在旧底图上
                self.args.map = f"minimaps/{args.new_map}/map.png"
                logger.info(f"[补录] 保留旧路线，用已有地图当底图：{self.args.map}")
            elif getattr(args, 'replace', False):
                # ⚠️ 先改名备份，**不要直接删**（2026-09-12 真实事故）：
                #    用户选了「清掉重录」，但这一次主线一段都没存成（他中途按了 F6 起头
                #    → 之后按 F3 全被拦下），退出时 route{N}.png 全无、旧路线却已经没了，
                #    结果整张图跑不起来。改成备份后，收尾时若发现没录到任何主线路线，
                #    自动把备份还原（见 __main__ 末尾）。
                bak = map_dir + ".bak"
                if os.path.exists(bak):
                    shutil.rmtree(bak, ignore_errors=True)
                os.rename(map_dir, bak)
                self._backup_dir = bak
                logger.info(f"[重录] 旧路线已备份到 {bak}（录完确认没问题才会删；"
                            f"这次要是没录到主线路线会自动还原）")
            else:
                logger.error(f"目录已存在：{map_dir}")
                logger.error("要覆盖重录：从界面按 F4 启动并选「清掉重录」（或加 --replace）")
                sys.exit(2)
        elif self.append_home:
            # 目录都不存在 = 这张图压根没录过，谈不上"补录" → 直接当普通新录
            logger.warning(f"[补录] {map_dir} 不存在 —— 当作新图录制（主线也要录）")
            self.append_home = False
        os.makedirs(map_dir, exist_ok=True)
        if not self.append_home:
            logger.info(f"Created new directory: {map_dir}")

        # Load exist map
        if self.args.map != '':
            self.img_map = load_image(f"{self.args.map}")

        # Start keyboard listener thread
        self.kb = KeyBoardListener(self.cfg, is_autobot=False)

        # ⚠️★ 2026-09-17 加：把"录制器认的跳跃键"**打出来**。
        #    这类 bug 的狠处是**静默** —— 跳跃键不是空格时，录出来一条没有 jump 的线，
        #    全程不报错，只有真机跑起来"爬不上管子"才暴露。现在开局就说清楚。
        _jk = str((self.cfg or {}).get("key", {}).get("jump") or "space").strip().lower()
        logger.info(f"[录制] 跳跃键 = '{_jk}'（来自 config key.jump）；"
                    f"录制时按这个键才会画上跳跃色。"
                    + ("" if _jk == "space"
                       else f"\n        注意：它不是空格 —— 旧版本写死只认 'space'，"
                            f"会导致录制时**一次跳都记不下来**（线里没有 jump 像素）。"))

        # Start game window capturing thread
        logger.info("Waiting for game window to activate, please click on game window")
        self.capture = GameWindowCapturor(self.cfg)

        # 界面面板命令通道（stdin 是管道时才起，见 _start_stdin_reader）
        self._start_stdin_reader()
        # 先把初始状态推给面板：它一显示就能亮出「第 1 步」，不必等第一帧
        self._emit_state()

    def expand_canvas(self, img, expand_top, expand_bottom, expand_left, expand_right):
        '''
        把一张画布按同样的边距扩一圈黑边（内容位置随之整体平移）。

        用于让**路线画布跟着地图画布一起扩容** —— 两者必须始终保持同一坐标系，
        否则按全局坐标画的路线会与 map.png 错位。见 ensure_img_map_capacity 的说明。
        '''
        if img is None:
            return None
        if expand_top == 0 and expand_bottom == 0 and expand_left == 0 and expand_right == 0:
            return img
        h, w = img.shape[:2]
        new = np.zeros((h + expand_top + expand_bottom, w + expand_left + expand_right, 3),
                       dtype=np.uint8)
        new[expand_top:expand_top + h, expand_left:expand_left + w] = img
        return new

    def _sync_saved_route_canvases(self, expand_top, expand_bottom,
                                   expand_left, expand_right):
        '''画布扩容时，把**已经存盘**的路线图一起同步补边/平移（2026-10-14 加）。

        为什么必须有这一步（真机事故 `minimaps/废铁2`）：
          `save_route()` 存的是**保存那一刻**的画布大小，而底图会随小地图滚动
          向右/下扩容 ⇒ 同一场录制里先存的几段天生比 map.png 窄（真机实测：
          route1/2 = 137x268、route3 = 137x314、route4+ = 137x344）。引擎按
          map.png 的坐标系走，窄图会被判「尺寸不一致」**直接跳过不加载** ——
          症状是"头几段凭空消失 + 巡逻闭环从 0px 变 119px + 手绘面板 IndexError"，
          而且**一个错都不报**（只有一句 [路线校验] 会被淹没）。

        扩容那一刻才知道扩了多少（往左/上扩还要整体平移坐标），所以根治点在这里。
          离线补救工具 `tools/route_pad_canvas.py` 只处理得了"只往右/下扩"；
          这里用的就是 `expand_canvas()` 同一套变换，与 `self.img_route` 完全一致，
          往左/上扩时的坐标平移也一并正确。

        ⚠️ 补录回正线模式（--append-home）**不同步**：那种模式下 map.png 是已存在的
           旧底图、这一趟不会被重写，一旦扩容就会让内存画布与磁盘 map.png 错位。
           这种情况只报一句 WARNING 让问题可见 —— 宁可不写，也不要写出一套错坐标。

        Returns:
            bool: 有没有真的写过盘（自检用来断言"不扩容时一个字节都不写"）
        '''
        if not any((expand_top, expand_bottom, expand_left, expand_right)):
            return False
        if getattr(self, "append_home", False):
            logger.warning(
                "[录制] 画布扩容了，但本次是「只补录回正线」—— 磁盘上的 map.png "
                "不会重写，为避免路线与底图错位，**已存盘的路线图没有同步补边**。"
                "补录时请让角色待在已录范围内；已经错位的图用 "
                "python -m tools.route_pad_canvas --map <图名> 体检/补救。")
            return False
        map_dir = resource_path(os.path.join("minimaps", self.args.new_map))
        names = [f"route{i}.png" for i in range(1, int(getattr(self, "idx_routes", 0)) + 1)]
        names.append("route_home.png")
        done, failed = [], []
        for name in names:
            p = os.path.join(map_dir, name)
            if not os.path.exists(p):
                continue
            try:
                img = load_image(p)
                if img is None:
                    failed.append(f"{name}（读不出来）")
                    continue
                new = self.expand_canvas(img, expand_top, expand_bottom,
                                         expand_left, expand_right)
                if new is None or new.shape == img.shape:
                    continue
                if not imwrite_unicode(p, new):
                    failed.append(f"{name}（写出失败）")
                    continue
                done.append(name)
            except Exception as e:                                   # noqa: BLE001
                failed.append(f"{name}（{e}）")
        if done:
            logger.info(
                f"[录制] 画布扩容（左+{expand_left} 上+{expand_top} "
                f"右+{expand_right} 下+{expand_bottom}）⇒ 已存盘的 {len(done)} 个"
                f"路线图同步补边/平移：{', '.join(done)}")
        if failed:
            logger.warning(
                f"[录制] 扩容后同步这些路线图失败：{', '.join(failed)}"
                f" —— 它们还是旧尺寸，引擎会跳过不加载。补救："
                f"python -m tools.route_pad_canvas --map {self.args.new_map}")
        return bool(done)

    def ensure_img_map_capacity(self, x, y, h, w):
        '''
        Ensure that self.img_map is large enough to contain the region defined by (x, y, h, w).
        Always add at least "map_padding" when expanding in any direction.

        ⚠️ 2026-09-10 修：扩容会**整体平移坐标系**，因此所有存着"全局坐标"的东西
        都必须跟着平移，否则同一个坐标系里会混进两套原点：
          · self.img_map        —— 画布本身（下面已扩）
          · self.img_route      —— 路线画布。**原先漏了**：它不扩 → 之后按全局坐标画的线
                                   会与 map.png 错位；而错位量正好是本次的 (expand_left, expand_top)。
          · self.loc_minimap_global / loc_player_global_last —— 全局坐标，同样要平移。
        症状很有迷惑性：因为小地图是"整图视图"的地图上**永远不会扩容**，
        这个坑平时根本不出现，只在大地图（小地图会滚动）上突然"路线整体偏一点"。
        '''
        map_h, map_w = self.img_map.shape[:2]
        pad = self.cfg["route_recoder"]["map_padding"]

        # Compute required expansion margins
        expand_top = pad - y if y < pad else 0
        expand_left = pad - x if x < pad else 0
        expand_bottom = y + h + pad - map_h if y + h + pad > map_h else 0
        expand_right = x + w + pad - map_w if x + w + pad > map_w else 0
        expand_top = max(0, expand_top)
        expand_left = max(0, expand_left)
        expand_bottom = max(0, expand_bottom)
        expand_right = max(0, expand_right)
        # If no expansion needed, return
        if expand_top == 0 and expand_bottom == 0 and expand_left == 0 and expand_right == 0:
            return False

        # Create new canvas and paste old image
        self.img_map = self.expand_canvas(self.img_map, expand_top, expand_bottom,
                                          expand_left, expand_right)
        # 路线画布必须同步扩容（否则路线与地图错位）
        self.img_route = self.expand_canvas(self.img_route, expand_top, expand_bottom,
                                            expand_left, expand_right)

        # Update all global coordinates that depend on the map
        self.loc_minimap_global = (
            self.loc_minimap_global[0] + expand_left,
            self.loc_minimap_global[1] + expand_top
        )
        if self.loc_player_global_last is not None:
            self.loc_player_global_last = (
                self.loc_player_global_last[0] + expand_left,
                self.loc_player_global_last[1] + expand_top
            )

        # ★ 2026-10-14：**已经存盘**的 route{N}.png / route_home.png 必须一起同步。
        #   不做这一步，先存的几段就永远停在旧画布尺寸上 —— 引擎按 map.png 的坐标系
        #   走，会判「尺寸不一致」把它们**整段跳过**（真机事故：闭环从 0px 变 119px，
        #   手绘面板还会 IndexError）。详见 _sync_saved_route_canvases 的说明。
        return self._sync_saved_route_canvases(expand_top, expand_bottom,
                                               expand_left, expand_right)

    def remove_color_code_pixels(self, img):
        """
        Set all pixels in self.img_map to black if they match any color in color_code (assumed RGB).
        """
        for rgb in self.color_code.keys():
            bgr = (rgb[2], rgb[1], rgb[0])  # Convert RGB → BGR
            mask = np.all(img == bgr, axis=2)
            img[mask] = (0, 0, 0)
        return img

    def update_minimap(self):
        '''
        update_minimap
        '''

    # ================== 录制交互：面板命令与键盘热键共用的一套动作 ==================
    # 2026-09-12 用户要求：录制全程用鼠标点，不再有命令行式交互。
    # 界面上的「录制」置顶面板把命令写进本进程 stdin（@@CMD xxx），本进程把状态写回
    # stdout（@@STATE xxx）。键盘 F1~F5 / q 保留作兼容 —— 两条路走的是**同一批方法**，
    # 不会出现"按钮能做的键盘做不到"或反过来。

    def _start_stdin_reader(self):
        """后台线程：读界面面板发来的命令（每行一条 `@@CMD xxx`）。

        只在 stdin 是**管道**（界面用 QProcess 启动）时才起线程 —— 从控制台手跑时
        stdin 是终端，起线程会把用户敲的字符抢走，退出前那个 input() 就再也读不到了。

        stdin 彻底不可用时（理论上 QProcess 会给管道，但别赌）降级到纯键盘，
        并**明确报一句** —— 否则用户会面对一个点了没反应的死面板，还不知道为什么。
        """
        stream = sys.stdin
        if stream is None:
            try:
                stream = open(0, "r", encoding="utf-8", errors="replace")
            except Exception:
                stream = None
        if stream is None:
            logger.warning("[录制] 收不到界面面板的命令（stdin 不可用）—— 请改用键盘："
                           "F1 暂停/继续、F3 保存这一段、F4 保存地图、"
                           "F6 录回正线（按两次）、F7 放弃、F8 折返分段、q 结束")
            # ⚠️ 原来的提示里还有「F5 标定驻守点」—— 定点模式（stand）2026-09-12
            #    已删除，F5 不再有这个功能，2026-09-17 去掉，免得用户按了没反应。
            return
        try:
            if stream.isatty():
                logger.info("[录制] 控制台模式：面板命令通道未启用（键盘 F1~F5 / q 照常可用）")
                return
        except Exception:
            pass
        self.panel_mode = True

        def _loop():
            try:
                for raw in stream:
                    cmd = raw.strip()
                    if not cmd.startswith('@@CMD'):
                        continue          # 非协议行（比如手敲的字符）直接忽略
                    cmd = cmd[len('@@CMD'):].strip().lower()
                    if cmd == 'save_route':
                        self._cmd_save_route = True
                    elif cmd == 'save_map':
                        self._cmd_save_map = True
                    elif cmd == 'toggle_pause':
                        self._cmd_toggle_pause = True
                    elif cmd == 'home_route':
                        self._cmd_home_route = True
                    elif cmd == 'home_start':
                        self._cmd_home_start = True
                    elif cmd == 'home_end':
                        self._cmd_home_end = True
                    elif cmd == 'home_cancel':
                        self._cmd_home_cancel = True
                    elif cmd == 'waypoint':
                        self._cmd_waypoint = True
                    elif cmd == 'arm':
                        self._cmd_arm = True
                    elif cmd == 'screenshot':
                        self._cmd_screenshot = True
                    elif cmd == 'quit':
                        self._cmd_quit = True
            except Exception:
                pass   # stdin 被关掉（界面退出）→ 线程安静结束

        threading.Thread(target=_loop, daemon=True, name="panel-cmd").start()

    def _count_drawn_pixels(self):
        '''当前这段已经画上了多少路线像素（给面板的实时反馈用）。

        降频统计（0.4s 一次）—— 全图 numpy 比对不算便宜，没必要每帧做。

        ⚠️ 色码表要转成 **BGR**：录制器是反着画的（见 _stamp_goal 的 color_rgb[::-1]），
           直接拿 yaml 里的 RGB key 去比对会一个都对不上。
        '''
        now = time.time()
        if now - self._t_last_drawn_count < 0.4:
            return self._drawn_cache
        self._t_last_drawn_count = now
        if self.img_route is None:
            self._drawn_cache = 0
            return 0
        try:
            if self._ccodes_bgr is None:
                self._ccodes_bgr = np.array(
                    [(c[2], c[1], c[0]) for c in self.color_code.keys()],
                    dtype=np.int16)
            arr = self.img_route.reshape(-1, 3).astype(np.int16)
            hit = (arr[:, None, :] == self._ccodes_bgr[None, :, :]).all(axis=2).any(axis=1)
            self._drawn_cache = int(hit.sum())
        except Exception:
            pass
        return self._drawn_cache

    def _emit_state(self):
        """把当前状态以**机器可读的一行**发给界面面板（面板据此更新步骤提示）。

        格式：`@@STATE routes=N paused=0|1 mapsaved=0|1 home=0|1|2 append=0|1 sp=0 wp=N
               loopdist=N mode=...`
          home: 0=还没起头 / 1=已起头（等收尾）/ 2=已存盘
          append: 1=「只补回正线」模式（旧主线路线不动）
          sp: 保留字段（定点已删，恒为 0）；wp=已记下的途经点数；mode 恒为 normal
          loopdist: 当前位置到**第一段起点**的曼哈顿距离；-1 = 还没到该关心它的时候
                    （还在录第 1 段 / 正在录回正线）。走完最后一段会循环回第一段，
                    ≤10px 才接得上、≤40px 靠失联救援，>40px 会卡在终点。
          armed / posok / frameok: 【开始录】闸门与启动诊断（2026-09-15 加）——
                    armed=0 = 用户还没点【开始录】，此时**一笔都不画**（起点不会被钉在 (0,0)）；
                    frameok=0 = 本帧**一帧游戏画面都没抓到**（抓帧失败，见 get_img_frame）；
                    posok=0 = 画面有了但没定位到角色（坐标仍是 0,0）。
                    ⚠️ frameok 与 posok 必须**分开报**：只报一个的话，"没抓到画面"和
                       "没定位到角色"在面板上长得一样，会把用户引去查错的东西。
          pos / keys / drawn: 录制实时反馈（2026-09-12）—— 「用户走了一圈却一个像素
                    都没画上」是最难自查的坑，这三种原因症状完全一样：
                      · pos 不变 + keys 有值 → **角色没动**（多半是游戏窗口没焦点）
                      · keys=none          → 按键没捕获到（权限 / 钩子没装上）
                      · pos 变 + drawn 不涨 → 画线环节坏了（代码问题）
                    面板把它们直接摊开，用户不用再靠猜。
        用独立前缀而不是日志措辞，界面侧不必去猜日志文案；内容不变时不重复发。
        """
        if self._home_recording:
            home = 1
        elif self._home_saved:
            home = 2
        else:
            home = 0
        # 闭环距离：只在**已存过至少一段、且正在录主线**时才有意义
        # ⚠️ loc_player_global 在定位失败时可能是 None（之前实测：first_frame 走 cv2
        #    模板匹配会拿到 (0,0) → 拼字符串时 int(None[0]) 直接 TypeError 把整条
        #    @@STATE 抛掉，面板收不到状态）。所以这里防御 None。
        loopdist = -1
        if self._route1_start is not None and self.idx_routes >= 1 \
                and not self._home_recording and self.loc_player_global:
            loopdist = (abs(int(self.loc_player_global[0]) - self._route1_start[0])
                        + abs(int(self.loc_player_global[1]) - self._route1_start[1]))
        err = self._err_msg if self._err_msg else "none"
        pos = (f"{int(self.loc_player_global[0])},{int(self.loc_player_global[1])}"
               if self.loc_player_global else "?,?")
        line = f"@@STATE routes={self.idx_routes} paused={0 if self.is_enable else 1} " \
               f"err={err} " \
               f"mapsaved={1 if self._map_saved else 0} home={home} " \
               f"append={1 if self.append_home else 0} " \
               f"sp=0 " \
               f"wp={self._waypoint_count} loopdist={loopdist} " \
               f"armed={1 if self._armed else 0} posok={1 if self._pos_ok else 0} " \
               f"frameok={1 if self._frame_ok else 0} " \
               f"pos={pos} " \
               f"keys={','.join(self.kb.key_pressing) if self.kb.key_pressing else 'none'} " \
               f"drawn={self._count_drawn_pixels()} mode={self.mode}"
        if line == self._last_state_line:
            return
        self._last_state_line = line
        try:
            print(line, flush=True)
        except Exception:
            pass   # stdout 已被关掉（界面退出）：不影响录制

    def push_state(self, min_interval=0.2):
        """主循环里周期性把状态推给面板（2026-09-16 加）。

        ⚠️ 为什么必须有这个方法：`_emit_state` 原来只在**初始化**和**收到命令**时被调，
        主循环一次都不调。于是：
          1.0s 初始化推一条 `frameok=0 posok=0`
          1.2s 首帧到位（frameok 真的变成 1 了）
          → 但没人告诉面板 → 面板一直显示「⏳ 还没抓到游戏画面」
          → 【开始录】按钮要 `frameok and posok` 才显示 → **永远不出现** → 用户卡死在第 1 步。
        2026-09-16 实测复现：静默 8 秒只收到 1 条 @@STATE（1.0s 那条），
        首帧在 1.2s 到达，之后 0 条；发命令也 0 条。

        限流 0.2s（5 次/秒）：`_emit_state` 内部按**整行**去重，但行里带
        `pos=` / `keys=` / `drawn=`，走路时每帧都在变 —— 不限流就会按 fps(30) 刷 stdout。
        5 次/秒足够让面板"按钮出现"这种变化在 200ms 内反映出来，肉眼无感。
        """
        now = time.time()
        if now - self._t_last_state_push < min_interval:
            return
        self._t_last_state_push = now
        self._emit_state()

    def arm_recording(self):
        """用户点【开始录】→ 从这里起才允许画线、才记起点（2026-09-15 加）。

        为什么必须由用户显式开启（用户 2026-09-15 原话："原项目默认的是打开的一瞬间
        就录制了起点，但问题是打开的一瞬间，还来不及立即聚焦到游戏窗口，所以通常
        这个起点我都不确定有没有录到，尤其是显示当前位置在一开始永远是 (0,0)"）：

          起点 = **第一次按下方向键**那一刻的 loc_player_global（见主循环）。
          而这个变量初值是 (0,0)，只有定位成功（找到小地图 + 玩家黄点）才会更新。
          所以在"还没定位到角色"时按了方向键，起点就被钉在 (0,0)，第一笔会从
          (0,0) 拉到真实位置 —— **起点是垃圾，而且事后从画面上看不出来**。

        ⇒ 点【开始录】之前：不画线、不记起点；点了之后才开始。
          并且把 loc_player_global_last 清掉 —— 否则第一笔会从"点按钮之前那个位置"
          连一条线过来（那段是准备过程，不属于路线）。

        ⚠️ **定位无效时拒绝开始**（_pos_ok=False，坐标还是 (0,0)）：这时候开始录
           等于明知会录出废起点还照录。宁可不开，让面板去提示用户先解决定位。
        """
        if self._armed:
            return
        if not self._pos_ok:
            logger.warning(
                "[录制] 点了【开始录】，但**还没定位到角色**（坐标仍是 0,0）—— 拒绝开始。\n"
                "        先确认：① 游戏窗口是前台、② 小地图完整可见、\n"
                "        ③ 设置里 minimap.player_color 是你小地图上那个点的真实 BGR 值。")
            self._emit_state()
            return
        self._armed = True
        self.loc_player_global_last = None
        logger.info(f"[录制] 【开始录】已按下 —— 起点基准 = {self.loc_player_global}"
                    f"（从这一笔开始算第一段起点）")
        self._emit_state()

    def toggle_pause(self):
        """开始 / 暂停记录路线（面板【开始·暂停】与键盘 F1 共用）。"""
        self.is_enable = not self.is_enable
        logger.info(f"[录制] {'继续记录' if self.is_enable else '暂停记录'}"
                    f"（已保存 {self.idx_routes} 段）")
        self._emit_state()

    def save_route(self):
        """把当前这一段存成 route{N}.png，并在段末点上 goal 标记。

        ⚠️ 调用点固定在**帧末**（见 run_once 里的 _want_save_route）：goal 圆点必须画在
        本帧所有移动线之后，否则会被同帧画的线盖住 —— 引擎找不到 goal 就会在段末死锁。
        """
        if self.img_route is None:
            logger.warning("[录制] 还没有画面，这一段没存（等游戏画面出来再点）")
            return
        if not self._armed:
            # 2026-09-15 加：还没点【开始录】时白板上一笔都没有，存下去就是一条空路线
            # （还会被登记进地图列表）—— 宁可不存。
            logger.warning("[录制] 还没点【开始录】—— 白板是空的，这一段没存。"
                           "先在面板上点【开始录】，再开始走。")
            return
        if self.append_home:
            # 追加模式只写 route_home.png。这里放行的话会把回正线/白板上的东西
            # 存成 route{N}.png，等于悄悄覆盖了旧主线 —— 与"保留旧路线"的承诺相反。
            logger.warning("[补录] 本次是「只补回正线」，主线路线没动。"
                           "要改主线请重新 F4 并选【清掉重录】。")
            return
        if self._home_recording:
            # ⚠️ 此时白板上画的是**回正线**（F6 起头后还没收尾）。存成 route{N}.png 会
            #    把"回来的路"混进主线巡逻列表 —— 引擎会拿它当普通路线来回走。宁可不存。
            logger.warning("[录制] 回正线正在录（F6 起头了还没收尾），这一段没存。"
                           "要走回主线后按 F6 保存回正线；"
                           "**不想录回正线就按 F7 / 面板【放弃这条】退出，F3 就能用了**。")
            return
        self._stamp_goal()
        out_path = resource_path(f"minimaps/{self.args.new_map}/route{self.idx_routes+1}.png")
        if not imwrite_unicode(out_path, self.img_route):
            logger.error(f"[录制] 路线图写出失败：{out_path}")
        self.idx_routes += 1
        # 重置白板（为什么要清掉"路线颜色"像素、为什么必须清 last，见 _reset_route_canvas）
        self._reset_route_canvas()
        logger.info(f"[录制] 第 {self.idx_routes} 段已保存：{out_path}")
        self._emit_state()

    def _stamp_goal(self):
        """在当前玩家位置点上 goal 标记 —— 引擎靠这个颜色判断"这条路走到头了"。

        必须在**帧末**画（见 run_once 里的 _want_save_route）：同帧画的移动线会盖住它，
        引擎找不到 goal 就会在段末原地死锁。

        ⚠️★ 2026-09-27 撤回一次**错误的修改**，并在此钉死正确口径：
          有人（我）一度把这里改成 `color=tuple(color_rgb)`，理由是"文件直读找不到黄色
          ⇒ goal 没写成功"。**这个推理是错的**，因为它漏了「引擎读取时会再翻转一次」：

            录制器（本函数）：写入前翻一次 RGB→BGR
            引擎（MapleStoryAutoLevelMap.py:676）：读入后再翻一次 BGR→RGB
            ⇒ 两次翻转**互相抵消**，配置里的 (255,255,0) 最终被引擎正确识别为 goal。

          实测证据（用户 minimaps/废都南方工地/route1.png，同一次录制）：
            颜色          文件里的值        引擎 cvtColor(BGR2RGB) 后
            配置 left     (0,0,255) 蓝      → (255,0,0) 红 = **配置里的 left** ✓
            配置 right    (255,0,0) 红      → (0,0,255) 蓝 = **配置里的 right** ✓
            goal（原写法）(0,255,255) 青    → (255,255,0) 黄 = **配置里的 goal** ✓
            goal（我改后）(255,255,0) 黄    → (0,255,255) 青 = **配置里没有** ✗ 判废

          ⇒ 结论：**必须保留这里的翻转**。文件里 goal 是青色是"正确"的，不是 bug。
            验证这条路线的正确方法是用**引擎的口径**看（转一次之后），
            而不是用 cv2 直读文件 —— 直读看到的颜色天生是"反的"。
        """
        if self.img_route is None:
            return
        color_rgb = {v: k for k, v in self.color_code.items()}.get("none none goal")
        if color_rgb is None:
            return
        px, py = (int(v) for v in self.loc_player_global)
        cv2.circle(self.img_route, (px, py), radius=2,
                   color=(color_rgb[2], color_rgb[1], color_rgb[0]), thickness=-1)

    def jump_key(self):
        '''录制器认的**跳跃键**（来自 config `key.jump`，默认 space）。

        ⚠️ 2026-09-17：这里**绝不能写死 "space"**。
           `KeyBoardListener` 只把 `keyboard.Key.space` 映射成字符串 "space"，
           其它普通键按 `key.char.lower()` 原样存（config `key.jump: c` → 存 "c"）。
           原来写死 `"space"`，于是**只要跳跃键不是空格，录制时一次跳都记不下来**
           （全程静默、不报错），真机表现就是「爬不上管子」。
        '''
        try:
            k = str((self.cfg or {}).get("key", {}).get("jump") or "space").strip().lower()
        except Exception:                                    # noqa: BLE001
            k = "space"
        return k or "space"

    def teleport_key(self):
        '''录制器认的**瞬移键**（来自 config `key.teleport`；读不到 / 留空 ⇒ 回落 "e"）。

        ⚠️★ 2026-10-14：这是 `jump_key` 的**同类 bug，当时只修了跳跃那一半**。
           `action_from_keys` 里原来写死 `if "e" in kp:  # Teleport skill`，
           于是 config `key.teleport: space` 的用户录制时按空格 ⇒ 一路落到末尾
           返回 `("", False)`：**一个传送像素都不画，全程静默、不报错**
           （和"跳跃键不是空格就一次跳都记不下来"是同一个病）。

           而引擎侧本来是全支持的：config 里有 4 个 teleport 色码
           （left / right / up / down teleport），`KeyBoardController.run()` 里
           `elif self.cmd_action == "teleport": self._press(cfg["key"]["teleport"])`
           —— 也就是"引擎会走、录不出来"，故障只落在用户身上。

        回落口径与 jump_key 一致：**读不到 / 留空 ⇒ 回落老的写死值 "e"**
        （保证没设过 `key.teleport` 的人行为完全不变）。
        '''
        try:
            k = str((self.cfg or {}).get("key", {}).get("teleport") or "e").strip().lower()
        except Exception:                                    # noqa: BLE001
            k = "e"
        return k or "e"

    def action_from_keys(self, key_press, jump_latched=False):
        '''把"当前按住的键"翻译成路线动作。抽成纯函数是为了**能自检**（见
        `tools/verify_recorder_action.py`）—— 按键判定写死键名这类 bug 全程静默，
        不钉成用例下次还会犯。

        Args:
            key_press: 按住的键名列表（KeyBoardListener.key_pressing）
            jump_latched: **本帧之前按过一次跳跃键**（边沿触发，来自
                `KeyBoardListener.consume_jump_press()`）。
                ⚠️★ 为什么不能只看 key_press：录制器只有 **10fps（100ms/帧）**，
                而一次"跳"是轻按（<100~200ms）⇒ 轮询**大概率整帧都没看到**，
                跳就丢了。真机反例：用户跳跃键 = c，重录两遍都是 0 个跳跃像素。

        Returns:
            (action, is_draw_blob): action 为空串 = 这一帧不画；
            is_draw_blob=True 表示画"团"（跳跃/传送），False 表示画"线"（移动）。
        '''
        kp = set(key_press or ())
        if jump_latched or self.jump_key() in kp:
            if "left" in kp:
                return "left none jump", True
            if "right" in kp:
                return "right none jump", True
            if "down" in kp:
                return "none down jump", True
            # 边跳边按上（爬管子/绳子常见）：色码表里没有"上+跳"的组合，
            # 退成「原地跳」—— 跳起来后下一帧的 up 像素会接手往上爬。
            return "none none jump", True
        # Teleport skill —— 键名**必须**来自 config `key.teleport`（见 teleport_key）：
        # 这里曾写死 "e"，把"瞬移键不是 e"的用户整条传送录制路径**静默吞掉**。
        # ⚠️ 没有方向键时不画（返回空动作）：色码表里只有 left/right/up/down 四种
        #    teleport，没有"原地传送"这一项 —— 硬画会画出引擎认不出的颜色。
        if self.teleport_key() in kp:
            if "left" in kp:
                return "left none teleport", True
            if "right" in kp:
                return "right none teleport", True
            if "down" in kp:
                return "none down teleport", True
            if "up" in kp:
                return "none up teleport", True
            return "", True
        if "up" in kp:
            return "none up none", False
        if "down" in kp:
            return "none down none", False
        if "left" in kp:
            return "left none none", False
        if "right" in kp:
            return "right none none", False
        return "", False

    def read_keys(self):
        '''读本帧的按键状态 → `(key_press, jump_latched)`。

        抽出来是为了自检能覆盖**真实调用点**（只测 `action_from_keys(jump_latched=...)`
        抓不到"忘了把 latch 传进去"这种错，变异测试实测 0 红）。

        ⚠️ `consume_jump_press()` 是**取走即清**，所以一帧只能调一次。
        '''
        key_press = self.kb.key_pressing
        _consume = getattr(self.kb, "consume_jump_press", None)
        return key_press, (bool(_consume()) if callable(_consume) else False)

    def paint_step(self, action, is_draw_blob):
        '''把**一帧的动作**画到 route 白板上（blob=跳跃/传送，line=移动）。

        抽成方法是为了自检能调**真实入口**（`tools/verify_recorder_action.py`）——
        自己复刻一遍"怎么画"就测不到真东西了（项目纪律）。

        ⚠️ 路线图在内存里是 **BGR**（录制器一直"反着画"，见 save_route 的说明），
           所以这里用 color_bgr；自检读回时要记得转。
        '''
        dict_action_to_color = {v: k for k, v in self.color_code.items()}
        color_rgb = dict_action_to_color.get(action, None)
        if color_rgb is None:
            return
        color_bgr = (color_rgb[2], color_rgb[1], color_rgb[0])

        # Draw a line from the last position to the current one (if available)
        px, py = self.loc_player_global
        if is_draw_blob:
            dt = time.time() - self.t_last_draw_blob
            if dt > self.cfg["route_recoder"]["blob_cooldown"]:
                # Draw a small filled circle at current position
                cv2.circle(self.img_route,
                        (px, py),
                        radius=2,
                        color=color_bgr,
                        thickness=-1)  # filled circle
                # ★ 2026-09-17：把跳跃/传送这团像素**记下来**，后面画移动线时
                #   不许把它盖掉（见下面的"跳跃色保护"）。
                _h, _w = self.img_route.shape[:2]
                for _yy in range(py - 2, py + 3):
                    for _xx in range(px - 2, px + 3):
                        if 0 <= _xx < _w and 0 <= _yy < _h \
                                and (_xx - px) ** 2 + (_yy - py) ** 2 <= 4:
                            self._blob_pixels[(_xx, _yy)] = color_bgr
                logger.info(f"[录制] 记下{action} → {color_rgb} 于 ({px},{py})")
                self.t_last_draw_blob = time.time()
                # ⚠️★ 2026-09-27 修：**不再清空位置基准**，改为更新到跳跃点。
                #   旧代码是 `self.loc_player_global_last = None`。
                #   后果（直调 paint_step 实证）：下一帧走的那一笔因为 last 是 None，
                #   退化走 `if self.loc_player_global_last is None` 分支（起点=终点），
                #   **画出来是零长度 → 那一整段移动线丢失**。
                #   实测：走→跳→走，第 40 行 21~49 共 29 个 x 坐标整段空白，
                #   蓝线只在跳跃前后的两点各留一个点 —— 路线看着"断成几截"。
                #   为什么以前要清：担心"跳跃圆点旁边接着画线会把圆点盖回去"。
                #   但现在有**跳跃色保护**（_blob_pixels + 帧末重涂，见本函数末段），
                #   圆点被盖会自动补回 ⇒ 这个清空已经没有存在理由，只剩副作用。
                self.loc_player_global_last = (px, py)
        else:
            if self.loc_player_global_last is None:
                px_last, py_last = self.loc_player_global
                # 一段的**第一笔**从当前位置画起 —— 整个录制过程的第一笔
                # 就是第一段起点，记下来给闭环检查当基准（见 _route1_start）。
                if self._route1_start is None and self.idx_routes == 0 \
                        and not self._home_recording:
                    self._route1_start = (px_last, py_last)
                    logger.info(f"[录制] 记下第一段起点 {self._route1_start}"
                                f"（最后一段要走回这附近，闭环才接得上）")
            else:
                px_last, py_last = self.loc_player_global_last
            cv2.line(self.img_route,
                    (px_last, py_last),
                    (px     , py),
                    color=color_bgr,
                    thickness=1)
            # ── 跳跃色保护（2026-09-17 修）──────────────────────────────
            # 用户"**边跳边按上**"爬管子时，顺序是：
            #   帧 N   ：按 右+跳(+上) → 画跳跃圆点（青/洋红）
            #   帧 N+1 ：还在**同一个像素**上、按着上 → 画一条 1px 的灰线
            #           → 正好把刚画的跳跃圆点**盖成灰**，跳就丢了。
            # 旧版没有这层保护，于是"跳了却线里一个 jump 像素都没有"，
            # 机器人走到管柱底下只会按 up ⇒ 管柱入口偏高就爬不上去。
            _h, _w = self.img_route.shape[:2]
            for (_jx, _jy), _jc in self._blob_pixels.items():
                if 0 <= _jx < _w and 0 <= _jy < _h:
                    self.img_route[_jy, _jx] = _jc
            self.loc_player_global_last = self.loc_player_global

    def _reset_route_canvas(self):
        """清空路线白板，准备画新的一段（主线新段 / 回正线共用）。

        ⚠️ 必须和第一帧一样清掉"路线颜色"像素，不能直接 copy img_map —— 详见 save_route。
        ⚠️ "上一帧位置"必须一起清掉，否则新段第一笔会从旧段终点连过来。
        """
        self.img_route = self.remove_color_code_pixels(self.img_map.copy())
        self.loc_player_global_last = None
        # 新的一段 = 新的白板，上一段的跳跃像素不该再被"保护"回来
        self._blob_pixels = {}

    def _home_route_still_usable(self):
        """route_home.png 存下/还原之后，**按引擎的判废口径**它还站得住吗？

        为什么不自己写判据：判废规则（5 条）是 `home_route.validate_home_line`
        定的，引擎加载时**就用它**。这里若"再抄一遍"，两边口径一飘，
        就会出现"录制器说没事、挂机时不启用"这种最难查的分裂。
        ⇒ 直接调 `route_audit.audit_map`（它内部走的就是同一个函数）。

        Returns:
            (bool, str): (至少有一条可用?, 不可用时的人话原因)
        """
        try:
            from src.utils.route_audit import audit_map
            rep = audit_map(self.args.new_map, self.cfg)
        except Exception as e:                                   # noqa: BLE001
            # 体检本身跑不起来 → **按可用处理**（宁可漏报，也不能因为体检故障
            # 就把用户刚录的东西判死）
            logger.warning(f"[回正线] 体检没跑起来（暂按可用处理）：{e}")
            return True, ""
        if rep.get("home_ok"):
            return True, ""
        bad = [ln.strip() for ln in rep.get("lines", [])
               if "回正线" in ln and "✗" in ln]
        return False, "\n".join(bad) or "体检判为不可用（原因未给出）"

    def _restore_home_route_from_backup(self, bak, map_dir):
        """重录主线时，把备份里的 route_home.png 救回来（**尺寸 + 几何**都得对）。

        ⚠️ 为什么必须救（2026-09-16 用户实测踩到）：
        「重录」只走一遍**主线**，用户基本不会同时再录一次回正线。
        而原来的 `finalize_replace_backup` 一旦看到"录到了主线路线"就
        `rmtree(bak)` —— 把备份里的 route_home.png **一起删了**。
        症状：紧接着挂机时报
        「route_home.png 文件不存在 ——本次不启用回正，角色掉下平台后不会自己走回来」，
        用户完全不知道回正线是怎么没的（2026-09-16 废都南方工地 就是这样丢的）。

        ⚠️★ 2026-09-17 实测补：**光比尺寸是不够的**。实测 `废都南方工地`：
        底图前后两次录制**完全相同**（相位相关 dx=dy=0、模板匹配相似度 0.995、
        74% 像素逐位相等），可主线从 (92~106, 120~124) 换到了 (46~84, 106~110)
        —— 用户重录时**换了个位置巡逻**。于是那份尺寸"刚好一致"的旧回正线
        离新主线 27~44px、终点离 27px ⇒ 判废（覆盖 x 只有 3px）。
        ⇒ 尺寸一致就还原 = 救回一条**废线**，用户以为有回正线，掉下去照样站桩。
        ⇒ 现在多一道**几何校验**（走引擎自己的判废口径）：
           · 过了 → 还原，并说明"这条还能用"；
           · 没过 → **不还原**，把"为什么不能用 + 怎么重画"直接说清楚。

        尺寸校验：回正线是按 map.png 的坐标系画的，底图尺寸一变就会错位 ——
        那种情况下**宁可不要**（只报警告），也不要塞一条错位的线进去。
        """
        src = os.path.join(bak, "route_home.png")
        dst = os.path.join(map_dir, "route_home.png")
        if not os.path.exists(src) or os.path.exists(dst):
            return False
        try:
            img_src = load_image(src)
            img_map = load_image(os.path.join(map_dir, "map.png"))
        except Exception as e:                                   # noqa: BLE001
            logger.warning(f"[重录] 想把旧回正线救回来但读图失败：{e}")
            return False
        if img_src is None or img_map is None or img_src.shape != img_map.shape:
            logger.warning(
                "[重录] 备份里有回正线，但它的尺寸与新底图不一致"
                f"（回正线 {None if img_src is None else img_src.shape} / "
                f"底图 {None if img_map is None else img_map.shape}）——**没有还原**，"
                "免得塞一条错位的线进去。请用【🖊 手绘回正线】或【补录回正线】重做一条。")
            return False
        try:
            if not imwrite_unicode(dst, img_src):
                raise RuntimeError("imwrite_unicode 返回 False")
        except Exception as e:                                   # noqa: BLE001
            logger.warning(f"[重录] 还原旧回正线失败：{e}")
            return False

        # ── 尺寸对上了，再问引擎：**这条线对新主线还成立吗** ──────────────
        ok, why = self._home_route_still_usable()
        if not ok:
            try:
                os.remove(dst)          # 留着也是废线，只会让人以为"回正线还在"
            except OSError:
                pass
            logger.warning(
                "[重录] 备份里的旧回正线**尺寸对得上、但几何已经对不上新主线** —— "
                "**没有还原**（还原了也是废线：挂机时会判废，掉出去照样站桩）。\n"
                "        原因：\n"
                + "\n".join("        " + ln for ln in why.splitlines()) + "\n"
                "        这是正常的：重录主线常常换了巡逻位置，旧回正线的落点/终点"
                "都留在老地方。请用【🖊 手绘回正线】重画一条"
                "（不用开游戏；画完当场校验覆盖率）。")
            return False
        logger.info("[重录] 已把旧备份里的回正线（route_home.png）还原到新目录 "
                    "—— 尺寸与几何都对得上，重录主线不会再把回正线弄丢。")
        return True

    def finalize_replace_backup(self):
        """「清掉重录」的收尾：这次真的录到主线路线了吗？

        - 录到了 → 删掉备份，新路线生效（但**回正线要先救回来**，见下）
        - 没录到 → 把备份**整目录还原**（新录的 map / 回正线一起作废 —— 反正没有主线
          它们也用不了，留着反而让用户以为"我录过了"）。

        为什么必须有这一步（2026-09-12 真实事故）：用户选了「清掉重录」，旧目录被删；
        但他中途按了 F6 起头录回正线，之后按 F3 全被拦下，退出时 route{N}.png 一个都
        没有 —— 整张图空了，挂机直接失效，而旧路线已经没了。

        ⚠️ 2026-09-16 补：录到主线时**不能**无脑 rmtree(bak) —— 备份里的回正线要救回来，
        否则「重录主线」= 静默丢掉回正线。详见 _restore_home_route_from_backup。
        """
        bak = self._backup_dir
        if not bak or not os.path.isdir(bak):
            return
        map_dir = resource_path(os.path.join("minimaps", self.args.new_map))
        saved = [f for f in os.listdir(map_dir)
                 if f.startswith("route") and f.endswith(".png")
                 and f != "route_home.png"]
        if saved:
            self._restore_home_route_from_backup(bak, map_dir)
            shutil.rmtree(bak, ignore_errors=True)
            logger.info(f"[重录] 已录到 {len(saved)} 段主线路线，旧备份已删除。")
            return
        shutil.rmtree(map_dir, ignore_errors=True)
        os.rename(bak, map_dir)
        logger.warning(
            "[重录] 这次**没有录到任何主线路线**（route1/2/3… 一个都没有）——"
            "已自动还原你重录前的旧路线，地图列表不受影响。\n"
            "        常见原因：按了 F6 起头录回正线之后，F3 会被拦下"
            "（避免把回正线存成主线）。要继续录主线，先按 F7 / 面板【放弃这条】"
            "退出回正线状态。")

    def cancel_home_route(self):
        """放弃当前正在录的回正线（面板【放弃这条】与键盘 F7 共用）。

        两步开关原本**没有出口**：误按 F6 起头之后就只能一路走到"保存"，而在这期间
        按 F3 会被拦下（防止把回正线存成主线）—— 结果主线一段都录不上，目录又被
        「清掉重录」提前清空，整张图就空了（2026-09-12 真实事故）。给个取消出口。
        """
        if not self._home_recording:
            logger.info("[回正线] 现在没在录回正线，没什么可放弃的")
            return
        self._home_recording = False
        self._home_start_pos = None
        self._reset_route_canvas()
        logger.info("[回正线] 已放弃这一条，回到主线录制 —— 现在按 F3 可以正常存段了。")
        self._emit_state()

    def _load_home_route_as_canvas(self):
        """把已存的 route_home.png 读回白板 —— 用于**多个落点各录一条**。

        一张图常常不止一处会掉下去（左边一个坑、右边一个坑），落点不同，一条回正线
        接不住。route_home.png 是**一张图**，可以累积多条互不相连的线段：每次 F6 起头
        都把旧的读回来再往上画。引擎按"最近路线像素"接引，掉到哪个点就会接到离它最
        近的那条。

        返回 True = 已叠加（调用方不要再清空白板）；False = 从空白开始（会覆盖旧的）。

        ⚠️ 尺寸必须与当前底图一致（同一个坐标系），否则旧回正线会与新地图错位 ——
        这种情况宁可丢弃重来并提示，也不要画出一条错位的线。
        """
        home_path = resource_path(f"minimaps/{self.args.new_map}/route_home.png")
        if not os.path.exists(home_path):
            return False
        try:
            img = load_image(home_path)          # BGR，与 img_route 同空间
        except Exception as e:
            logger.warning(f"[回正线] 旧的回正线读不回来（{e}）—— 本次从空白开始，会覆盖旧的")
            return False
        if img is None or (self.img_map is not None and img.shape != self.img_map.shape):
            logger.warning(
                f"[回正线] 旧的回正线与当前地图不匹配（回正线 "
                f"{None if img is None else img.shape[:2]} vs 地图 "
                f"{None if self.img_map is None else self.img_map.shape[:2]}）——"
                f"大概是地图重录过。本次从空白开始，会覆盖旧的回正线。")
            return False
        self.img_route = img
        self.loc_player_global_last = None
        return True

    def mark_waypoint(self):
        """巡逻录制：在**中转点（折返点）分段**（面板【在这折返，存下这段】/ 键盘 F8）。

        2026-09-12 改：**真正分段**，不再只是画个白点。

        用户的理解（也是直觉）：A(起点) → B(折返点) 是去程，B → A(终点) 是返程，
        这应该算**两段**。但原实现只在路线图上画个白点、**不存盘** ——
        于是 A→B→A 整圈被存成了**一段**，去程像素(right)和回程像素(left)叠在
        同一张图上。引擎靠"找最近路线像素"导航，同一位置既有 right 又有 left 时
        **分不清该走哪边** → 用户实测「启动后直奔右边一直走」。

        现在点折返点 = 保存当前段 + 自动开新段（从你现在站的位置继续画）。
        依然用白色 (255,255,255) 画个点：它**不在** route.color_code 里，
        引擎不会把它认成移动指令（用了路线色就会被当命令，路线就乱了）。
        """
        if self.img_route is None:
            logger.warning("[录制] 还没有画面，这一段没存（等游戏画面出来再点）")
            return
        px, py = (int(v) for v in self.loc_player_global)
        cv2.circle(self.img_route, (px, py), radius=2,
                   color=(255, 255, 255), thickness=-1)
        self._waypoint_count += 1
        # 存盘走帧末那条路（和 F3 同一条），goal 才会压在最后一笔移动线之上
        self._want_save_route = True
        # ⚠️ 用词：界面按钮叫「在这折返，存下这段」，所以日志也统一叫**折返点**
        #    （旧名叫「中转点」，而录制器里另有一个「只画白点、不分段」的中转点概念，
        #     两个词混着用会让用户以为点错了按钮 —— 2026-09-17 统一）。
        logger.info(f"[录制] 折返点 #{self._waypoint_count}：已存下这一段，"
                    f"接着从你现在站的位置录新的一段")
        self._emit_state()

    def toggle_home_route(self):
        """F6 兼容入口：没起头 → 起头；已起头 → 收尾保存。

        面板走的是**两个独立按钮**（【我在落地点】/【已回到主线】），语义更清楚；
        F6 保留"按两次"的老习惯，按状态自动分发到下面两个方法之一。
        """
        if self._home_recording:
            self.finish_home_route()
        else:
            self.start_home_route()

    def _dist_to_mainline(self):
        '''角色到**已录主线**最近路线像素的曼哈顿距离；一张主线都没录时返回 None。

        回正线的防呆判据（2026-09-12 定点模式删除后改用这个）：起点必须是**落点**。
        若角色此刻就站在主线上，说明他还没真的掉下去 —— 这样录出来的回正线起点会
        落在主线上，等真掉下去时落点离它几十上百 px，引擎
        （search_range=10 / rescue_range=40）接不上 → 干站回不来，而且不报错。

        ⚠️ 颜色口径必须和引擎完全一致：先 imread 再 BGR2RGB，才能对上 color_code
           的 key —— 录制器是**反着画**的（见 _stamp_goal 的 color_rgb[::-1]）。
        '''
        best = None
        cx, cy = self.loc_player_global
        ys_all: list = []
        for i in range(1, 20):
            p = resource_path(f"minimaps/{self.args.new_map}/route{i}.png")
            if not os.path.exists(p):
                break
            try:
                img = cv2.cvtColor(load_image(p), cv2.COLOR_BGR2RGB)
            except Exception:
                continue
            # ⚠️ 2026-09-17 改成 numpy 扫（原来是 Python 双循环逐像素判色，
            #    268×187×3 张图 ≈ 15 万次 tuple 转换 —— 起头那一下会顿）。
            for (x, y) in collect_pixels(img, self.color_code):
                d = abs(x - cx) + abs(y - cy)
                if best is None or d < best:
                    best = d
                ys_all.append(y)
        # 主线像素 y 的众数 = "站立行"（防呆用：判断角色是不是还站在台子上）
        self._main_stand_y_est = None
        if ys_all:
            vals, cnts = np.unique(np.asarray(ys_all, dtype=np.int32),
                                   return_counts=True)
            self._main_stand_y_est = int(vals[int(np.argmax(cnts))])
        return best

    def start_home_route(self):
        """回正线第 1 步：在**落点**起头（面板【我在落地点】/ 键盘 F6 第一次）。

        ⚠️ 起点为什么是落点而不是平台边缘：引擎只在角色周围约 10px（失联救援 40px）
        内找路线像素，掉下去之后离平台边缘往往几十 px —— 起点画在边缘的话，真掉下去
        时根本接不上，回正线等于白录（用户 2026-09-12 指出）。
        """
        if self.img_route is None:
            logger.warning("[回正线] 还没有画面，没开始（等游戏画面出来再点）")
            return
        if self._home_recording:
            logger.info("[回正线] 已经起头了 —— 走回主线后点【已回到主线】保存")
            return
        self._home_recording = True
        # 记下落点（**只作日志/提示用**，第二版不再往图上盖锚点色）
        # ⚠️ 必须在这里记：等收尾时角色已经站在主线上了，那时的位置不是落点。
        self._home_start_pos = (int(self.loc_player_global[0]),
                                int(self.loc_player_global[1]))
        # ⚠️ 防呆（2026-09-12 实测踩坑）：没真的掉下去就按 F6 起头，等于把回正线的
        #    "落点"画在了主线上。等真掉下去时，落点离这条线几十上百 px，引擎
        #    （search_range=10 / rescue_range=40）根本接不上 → 角色既不走也不跳，
        #    干站在下面，而且**不报错**，极难查。
        # ⚠️★ 2026-09-17 收紧：原来只看 `d_main <= 30`，**误报**了真掉下去的落点 ——
        #    真机实测：用户从台子左边掉到 (34,126)，d_main 恰好 = 30（水平差 12 +
        #    垂直差 18）就被判成"还没掉下去"。而那条线其实是好的（165/165 落点接得住）。
        #    正确判据是**两件事**：① 站在路线上（d_main ≤ 10，和 search_range 同量级）；
        #    ② 或者"高度没变"（y 还在主线站立行 ±2）却离得近 —— 那才是站在台子上。
        #    掉下去的人 y 一定比站立行低一大截，所以不会再误报。
        d_main = self._dist_to_mainline()
        _sy = getattr(self, "_main_stand_y_est", None)
        _on_platform_row = (_sy is not None
                            and abs(int(self.loc_player_global[1]) - int(_sy)) <= 2)
        if d_main is not None and (d_main <= 10 or (d_main <= 30 and _on_platform_row)):
            logger.warning(
                f"[回正线] 你现在站的位置离主线只有 {d_main}px"
                + ("、而且高度还在主线的站立行上" if _on_platform_row else "")
                + " —— **你还没真的掉下去**。\n"
                f"        这样录出来的回正线起点就在主线上；等你真掉下去时落点离它"
                f"几十上百 px，引擎接不上，角色会干站着回不来（不报错，很难查）。\n"
                f"        正确做法：先让角色**真的掉下去一次**，站在**落点**上再点"
                f"【我在落地点】起头，然后一路走回主线。")
        elif d_main is not None:
            logger.info(f"[回正线] 起点离主线 {d_main}px"
                        + (f"、比站立行低 {int(self.loc_player_global[1]) - int(_sy)}px"
                           if _sy is not None else "")
                        + " —— 看着是**真落点**，继续。")
        if self._load_home_route_as_canvas():
            logger.info("[回正线] 已有一条回正线，本次会**叠加上去**。\n"
                        "        站到另一个会掉下去的落点上，再走回主线，"
                        "点【已回到主线】保存。")
        else:
            self._reset_route_canvas()
            logger.info("[回正线] 已起头（起点 = 你现在站的落点）。\n"
                        "        接下来一路走回主线，走到之后点【已回到主线】。")
        self._emit_state()

    def finish_home_route(self):
        """回正线第 2 步：走回主线后收尾保存（面板【已回到主线】/ 键盘 F6 第二次）。

        回正线**不占** route{N}.png 的段号 —— 它是另一类路线（只有走丢/掉出去才走）。
        """
        if not self._home_recording:
            logger.info("[回正线] 还没起头 —— 先掉下去、站到落点上点【我在落地点】")
            return
        if self.img_route is None:
            logger.warning("[回正线] 还没有画面，没法保存")
            return

        # ⚠️ v0.9 第二版：只盖 goal，**不再盖落点锚**（锚点已整个取消）。
        #    引擎的进/退判定改成比「角色到回正线」和「角色到主线」两条距离，
        #    不需要任何"触发像素"。
        self._stamp_goal()
        out_path = resource_path(f"minimaps/{self.args.new_map}/route_home.png")
        if not imwrite_unicode(out_path, self.img_route):
            logger.error(f"[回正线] 写出失败：{out_path}")
            self._emit_state()
            return
        self._home_recording = False
        self._home_saved = True
        self._home_start_pos = None
        self._reset_route_canvas()
        logger.info(f"[回正线] 已保存：{out_path}")
        # ── 存完**立刻**按引擎口径体检（2026-09-17 新增）────────────────────
        # 为什么不能等挂机：判废是**引擎加载时**才做的，而界面录制体检的 ok
        # 只看主线（route_audit.audit_map: ok = n_main > 0）—— 回正线判废时
        # 界面照旧是绿的，用户以为存好了，直到下次掉下平台站桩才发现。
        # 实测漏掉的例子：2026-09-16 17:41「终点离主线 79px」。
        ok, why = self._home_route_still_usable()
        if ok:
            logger.info("[回正线] 存完体检：✓ 挂机时**会启用**这条线"
                        "（掉出平台后会沿它走回主线）。")
        else:
            logger.error(
                "[回正线] ⚠️ 刚存的这条**判废**了 —— 挂机时**不会启用**，"
                "掉出去只会站桩打怪（这次录制等于白录）。\n"
                "        原因：\n"
                + "\n".join("        " + ln for ln in why.splitlines()) + "\n"
                "        修法：① 按上面的提示改一版重录；"
                "② 或改用主界面的【🖊 手绘回正线】（不用开游戏，画完当场校验）。")
        logger.info("[回正线] 挂机时角色走丢 / 掉出平台会自动沿这条线走回主线；"
                    "掉出平台时会自动沿它走回主线。")
        logger.info("[回正线] 一条线只接**一个落点**。这张图还有别的地方会掉下去的话，"
                    "点【我在落地点】再补一条（会叠加上去）；也可以之后用 F4 的"
                    "【补录回正线】单独补。")
        self._emit_state()

    def save_map(self):
        """把当前地图存成 map.png（面板【保存地图】与键盘 F4 共用）。"""
        if self.img_map is None:
            logger.warning("[录制] 还没有画面，地图没存（等游戏画面出来再点）")
            return
        out_path = resource_path(f"minimaps/{self.args.new_map}/map.png")
        if not imwrite_unicode(out_path, self.img_map):
            logger.error(f"[录制] 地图写出失败：{out_path}")
            return
        self._map_saved = True
        logger.info(f"[录制] 地图已保存：{out_path}")
        self._emit_state()

    def request_quit(self):
        """请求结束录制（面板【结束录制】与键盘 q 共用），主循环下一圈跳出。

        退出前**自动保存地图**（2026-09-12 用户："不确定自己是不是录好了，也不确定
        什么时候该点哪个"）：map.png 是引擎必须的，以前要用户自己记得点【保存地图】，
        忘了就白录一场。补录模式（append_home）除外 —— 它用的是已有的 map.png，
        不该拿这趟的新画面去覆盖。
        """
        self._want_quit = True
        if not self._map_saved and not self.append_home:
            self._want_save_map = True

    def should_quit(self):
        """主循环的退出判据：面板点了【结束录制】、或键盘按了 q。"""
        return self._want_quit or self.kb.quit_requested

    def _collect_intents(self):
        """把键盘热键与面板命令**统一收成本帧要做的动作**。

        两条路共用同一个出口：按 F3 和点【保存这一段】走的都是 save_route()。
        保存类动作只置标志、不在帧首执行 —— 见 save_route() 的说明。
        """
        kb = self.kb
        # 【开始录】（面板）—— 2026-09-15 加：起点由用户显式开启，见 arm_recording
        if self._cmd_arm:
            self._cmd_arm = False
            self.arm_recording()
        # 保存这一段（F3 / 面板）
        if kb.is_pressed_func_key[2] or self._cmd_save_route:
            kb.is_pressed_func_key[2] = False
            self._cmd_save_route = False
            self._want_save_route = True
        # 开始 / 暂停（F1 / 面板）
        if kb.is_pressed_func_key[0] or self._cmd_toggle_pause:
            kb.is_pressed_func_key[0] = False
            self._cmd_toggle_pause = False
            self.toggle_pause()
        # 保存地图（F4 / 面板）
        if kb.is_pressed_func_key[3] or self._cmd_save_map:
            kb.is_pressed_func_key[3] = False
            self._cmd_save_map = False
            self._want_save_map = True
        # 录回正线（F6 / 面板）：两步开关。收尾那次要盖 goal，所以帧末才执行
        if kb.is_pressed_func_key[5] or self._cmd_home_route:
            kb.is_pressed_func_key[5] = False
            self._cmd_home_route = False
            self._want_home_route = True
        # 放弃正在录的回正线（F7 / 面板）——两步开关的出口，见 cancel_home_route
        if kb.is_pressed_func_key[6] or self._cmd_home_cancel:
            kb.is_pressed_func_key[6] = False
            self._cmd_home_cancel = False
            self.cancel_home_route()
        # 记一个途经点（F8 / 面板【在这折返，存下这段】）
        if kb.is_pressed_func_key[7] or self._cmd_waypoint:
            kb.is_pressed_func_key[7] = False
            self._cmd_waypoint = False
            self.mark_waypoint()
        # 回正线起头（面板【我在落地点】）
        if self._cmd_home_start:
            self._cmd_home_start = False
            self.start_home_route()
        # 回正线收尾（面板【已回到主线】）——帧末执行，goal 要压在最后一笔线上
        if self._cmd_home_end:
            self._cmd_home_end = False
            self._want_home_end = True
        # 截屏（F2 / 面板）
        if kb.is_pressed_func_key[1] or self._cmd_screenshot:
            kb.is_pressed_func_key[1] = False
            self._cmd_screenshot = False
            screenshot(self.img_frame)
        # 结束录制（q / 面板）
        if kb.quit_requested or self._cmd_quit:
            self.request_quit()

    def run_once(self):
        '''
        Process with one game window frame
        '''
        # Get lastest game screen frame buffer
        img_frame = self.get_img_frame()
        if img_frame is None:
            return -1 # Wait for game window to be ready
        else:
            self.img_frame = img_frame

        # Image for debug use
        self.img_frame_debug = self.img_frame.copy()

        # Get minimap from game window
        if self.is_first_frame:
            found = get_minimap_loc_size(self.img_frame)
            if found is None:
                # ⚠️ 不能一失败就退出（2026-09-12）：从"界面切前台到游戏"到
                #    "游戏真的画出来"有延迟，头几帧抓不到是**正常的**。
                #    给一段宽限期重试；真的一直抓不到才报错退出 —— 否则
                #    run_once 会一直 return -1，stdin 命令永远不被消费，
                #    面板按钮"按了没反应"（这才是真死局）。
                if self._t_record_start is None:
                    self._t_record_start = time.time()
                waited = time.time() - self._t_record_start
                if waited < self._first_frame_grace:
                    if int(waited * 2) != int(max(0, waited - 0.5) * 2):
                        # 每 0.5 秒提示一次，别刷屏
                        # ⚠️ 2026-09-27 修（issue #2）：原来这里写死
                        #    "等待游戏窗口聚焦。若一直卡在这里：游戏窗口是否被挡住/最小化？"
                        #    —— 真因往往是**别的**（窗口尺寸不符 / 抓帧库没送帧），
                        #    用户顺着这句去查前台、查双屏、换单屏，全是错方向。
                        #    改成按真实原因给提示（见 _no_frame_reason）。
                        _why, _fix = self._no_frame_reason()
                        logger.warning(
                            f"[录制] 还没抓到游戏画面（已等 {waited:.1f}s）—— {_why}\n"
                            f"        怎么办：{_fix}\n"
                            f"        当前窗口：{self._win_diag()}")
                    return -1
                _why, _fix = self._no_frame_reason()
                msg = (f"一直抓不到游戏画面（等了 "
                       f"{self._first_frame_grace:.0f} 秒）—— 无法建立世界坐标系。\n"
                       f"原因：{_why}\n"
                       f"怎么办：{_fix}\n"
                       f"窗口状态：{self._win_diag()}")
                logger.error("[录制] " + msg)
                self._err_msg = msg.replace("\n", " ")
                self._emit_state()
                sys.exit(2)
                return -1
            # 转成 python int：get_minimap_loc_size 返回的是 numpy 整数，
            # 混进坐标运算/日志里容易出意外，统一成 int。
            x, y, w, h = [int(v) for v in found]
            # Discard 1 pixel boundary of the minimap
            x += 1
            y += 1
            w -= 2
            h -= 2
            self.loc_minimap = (x, y)
            self.img_minimap = self.img_frame[y:y+h, x:x+w]
        else:
            x, y = self.loc_minimap
            h, w = self.img_minimap.shape[:2]
            self.img_minimap = self.img_frame[y:y+h, x:x+w]

        # Replace black pixels (0, 0, 0) with (1, 1, 1)
        black_mask = np.all(self.img_minimap == [0, 0, 0], axis=-1)
        self.img_minimap[black_mask] = [1, 1, 1]

        # Get player location on minimap
        # 显式传配置里的颜色（引擎也是这么调的）。虽然函数默认值恰好等于国服配置，
        # 但写死默认值一旦遇到别的配色就会静默失效，不如统一从配置读。
        loc_player_minimap = get_player_location_on_minimap(
            self.img_minimap,
            minimap_player_color=self.cfg["minimap"]["player_color"])
        if loc_player_minimap:
            self.loc_player_minimap = loc_player_minimap
        else:
            # ⚠️ 找不到就不能默默继续：loc_player_minimap 会停在初始值 (0, 0)，
            #    于是所有线都画在 (pad, pad) —— 长度 0，看起来就是"始终没录到起点"。
            #    （2026-09-12 用户实测：一直在平台上，但录制器画不出任何路径）
            if time.time() - getattr(self, "_t_last_minimap_warn", 0) > 5:
                self._t_last_minimap_warn = time.time()
                # 把**当前裁到的小地图**存成图片（2026-09-14 加）。
                # 光看日志猜不出是"裁错区域"还是"颜色配错"——存一张图，
                # 打开看一眼就明白：图里根本没有小地图 = 裁错区域；
                # 有小地图但找不到黄点 = 颜色配错。
                _dump = ""
                try:
                    # 用 imwrite_unicode：中文路径安全（cv2.imwrite 在中文路径下
                    # 会静默返回 False 且不生成文件，还不抛异常 —— 最坑的一点）
                    _p = resource_path("screenshot/录制_找不到黄点_小地图截图.png")
                    if imwrite_unicode(_p, self.img_minimap):
                        _dump = (f"\n        已把当前裁到的小地图存到：{_p}\n"
                                 f"        打开看一眼：图里**根本没有小地图** = 裁错区域；"
                                 f"有小地图但没黄点 = 颜色配错。")
                    else:
                        _dump = "\n        （想把小地图存成图片但失败了）"
                except Exception as _e:
                    _dump = f"\n        （想把小地图存成图片但失败了：{_e}）"
                logger.warning(
                    f"[录制] 小地图上找不到玩家黄点 —— 位置会卡在 "
                    f"{self.loc_player_minimap}，画不出路径。\n"
                    f"        当前配置 minimap.player_color = "
                    f"{self.cfg['minimap']['player_color']}（BGR）。\n"
                    f"        多半是颜色配错了：用窗口截图工具取一下小地图上"
                    f"你自己那个点的 BGR 值，填进设置里。{_dump}")

        # Update map
        if self.is_first_frame:
            # copy minimap to map
            if self.img_map is None:
                self.img_map = self.img_minimap.copy()
                pad = self.cfg["route_recoder"]["map_padding"]
                self.img_map = cv2.copyMakeBorder(
                    self.img_map,
                    top=pad, bottom=pad, left=pad, right=pad,
                    borderType=cv2.BORDER_CONSTANT,
                    value=(0, 0, 0)  # Black padding
                )

            # Replace player "yellow" dot to black on map
            self.replace_color_on_map(
                (55, 40, 80),
                (60, 100, 100)
            )
            # Replace other player "red" dot to black on map
            self.replace_color_on_map((0, 80, 80),
                                      (5, 100, 100))

            # Update route
            self.img_route = self.remove_color_code_pixels(self.img_map.copy())
            self.img_route_debug = self.img_route.copy()

        else:
            # Create mask where pixels are not black
            mask = np.any(self.img_minimap != [0, 0, 0], axis=2).astype(np.uint8)
            mask = mask * 255

            # Perform template matching to find where the current minimap fits in the global map
            self.loc_minimap_global, score, _ = find_pattern_sqdiff(
                self.img_map,
                self.img_minimap,
                mask=mask
            )
            x, y = self.loc_minimap_global
            h, w = self.img_minimap.shape[:2]
            # Ensure img_map is big enough to fit the newly explored region
            self.ensure_img_map_capacity(x, y, h, w)
            # ⚠️ 必须重新取：扩容会把整个坐标系平移 (expand_left, expand_top)，
            #    上面那对 x, y 是**平移前**的值 —— 继续用它去切 img_map 就会把
            #    小地图内容写到错误位置（在刚扩出来的黑边里贴出一份错位副本）。
            #    同样因为"整图视图"的地图不扩容，这个坑平时不出现。
            x, y = self.loc_minimap_global

            # Don't copy pixel near player
            player_yellow_dot_radius = 5
            px, py = self.loc_player_minimap
            h, w = self.img_minimap.shape[:2]
            x_min = max(0, px - player_yellow_dot_radius)
            x_max = min(w, px + player_yellow_dot_radius)
            y_min = max(0, py - player_yellow_dot_radius)
            y_max = min(h, py + player_yellow_dot_radius)
            # Apply the black color mask to mask player yellow dot
            self.img_minimap[y_min:y_max, x_min:x_max] = (0, 0, 0)

            # Update map
            if self.args.map == '':
                map_slice = self.img_map[y:y+h, x:x+w]
                black_mask = np.all(map_slice == [0, 0, 0], axis=2)
                map_slice[black_mask] = self.img_minimap[black_mask]

            # Replace other player "red" dot to black on map
            self.replace_color_on_map((0, 78, 78),
                                      (5, 100, 100))

        cv2.imshow("Map", self.img_map)
        self.img_route_debug = self.img_route.copy()

        # Get player location on global map
        self.loc_player_global = self.get_player_location_on_global_map()
        # 定位是否有效（2026-09-15）：面板据此判断"现在能不能点【开始录】"。
        # (0,0) 是"还没定位到"的哨兵值 —— 地图有 padding(30)，真实坐标到不了 0。
        try:
            self._pos_ok = (int(self.loc_player_global[0]) != 0
                            or int(self.loc_player_global[1]) != 0)
        except Exception:
            self._pos_ok = False

        # Determine which color code to use based on user input
        key_press, jump_latched = self.read_keys()
        action, is_draw_blob = self.action_from_keys(key_press,
                                                     jump_latched=jump_latched)

        # Check if need to change route
        # 2026-09-12：输入统一收口 —— 界面「录制」面板的按钮和键盘 F1~F5 走同一批方法，
        # 两条路的差异只剩"谁来置位"（见 _collect_intents）。
        self._collect_intents()

        # Update route image
        # ⚠️ 必须 _armed（用户点过【开始录】）才画 —— 见 arm_recording：
        #    否则用户还没准备好时按的方向键会把起点钉在 (0,0)。
        if self.is_enable and self._armed and action != "":
            self.paint_step(action, is_draw_blob)

        # Save route image —— 放在帧末：goal 标记必须压在最后一笔移动线之上
        if self._want_save_route:
            self._want_save_route = False
            self.save_route()

        # 回正线开关（F6 / 面板）—— 同样帧末：收尾时 goal 要压在最后一笔线上
        if self._want_home_route:
            self._want_home_route = False
            self.toggle_home_route()

        # 回正线收尾（面板【已回到主线】）—— 同样帧末
        if self._want_home_end:
            self._want_home_end = False
            self.finish_home_route()

        # Save img_map to map.png
        if self._want_save_map:
            self._want_save_map = False
            self.save_map()

        #####################
        ### Debug Windows ###
        #####################
        # Print text on debug image
        self.update_info_on_img_frame_debug()

        # Show debug image on window
        self.update_img_frame_debug()

        # （截图 F2 已并入 _collect_intents —— 输入统一收口）

        # Resize img_route_debug for better visualization
        self.img_route_debug = cv2.resize(
                    self.img_route_debug, (0, 0),
                    fx=self.cfg["minimap"]["debug_window_upscale"],
                    fy=self.cfg["minimap"]["debug_window_upscale"],
                    interpolation=cv2.INTER_NEAREST)
        cv2.imshow("Route Map Debug", self.img_route_debug)

        # Enable cached location since second frame
        self.is_first_frame = False

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # Argument to specify map name
    parser.add_argument(
        '--new_map',
        type=str,
        default='new_map',
        help='Specify the new map name'
    )

    parser.add_argument(
        '--cfg',
        type=str,
        default=None,
        help='Choose customized config yaml file in config/. '
             '不传时跟随主界面「配置方案」（active_config_path）。'
             '⚠️ 原默认值是 "custom"，它会让 active_config_path() 永远走不到，'
             '参见 RouteRecorder.__init__ 里的 2026-09-17 注释。'
    )

    parser.add_argument(
        '--map',
        type=str,
        default='',
        help='use this map instead of creating a new one'
    )

    parser.add_argument(
        '--replace',
        action='store_true',
        help='地图目录已存在时直接清掉重录（不弹问询；供界面 F4 调用）'
    )

    parser.add_argument(
        '--append-home',
        dest='append_home',
        action='store_true',
        help='只补录回正线：保留已有的 route{N}.png，用已有 map.png 当底图，'
             '录完只写 route_home.png（供界面 F4 的「补录回正线」调用）'
    )

    try:
        routeRecorder = RouteRecorder(parser.parse_args())
    except Exception as e:
        logger.error(f"RouteRecorder Init failed: {e}")
        sys.exit(1)
    else:
        while True:
            t_start = time.time()

            # Process one game window frame
            routeRecorder.run_once()

            # 把状态推给面板（2026-09-16 加）。
            # ⚠️ 不能省：面板的「还没抓到游戏画面 / 没找到角色 / 可以开始录」三态、
            #    以及【开始录】按钮出不出现，全靠这条 @@STATE。原来只在初始化时推过一次，
            #    首帧到位后 frameok 变 1 也推不出去 → 面板卡在"没抓到画面"、按钮永不出现。
            #    限流 5 次/秒，见 push_state 的说明。
            routeRecorder.push_state()

            # Exit if 'q' is pressed / 面板点了【结束录制】
            # cv2.waitKey 只在本圈被调一次，采样间隔 = 单帧处理时间，轻按必被漏掉；
            # quit_requested / _want_quit 由事件驱动置位，永不丢。
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or routeRecorder.should_quit():
                break

            # Cap FPS to save system resource
            frame_duration = time.time() - t_start
            target_duration = 1.0 / routeRecorder.fps_limit
            if frame_duration < target_duration:
                time.sleep(target_duration - frame_duration)

        cv2.destroyAllWindows()

        # 「清掉重录」的收尾：没录到主线就把旧路线还原回来（见方法注释）
        routeRecorder.finalize_replace_backup()

        # 回正线起了头但没收尾（没按第二次 F6）→ 白板上那条直接作废，必须说清楚，
        # 否则用户会以为"我录过了"、挂机掉出去却走不回来。
        if routeRecorder._home_recording:
            logger.warning("[回正线] 起了头但没有保存（没按第二次 F6）—— 这一次录的回正线"
                           "没有存盘。要重来：F4 选【补录回正线】（旧路线不动），"
                           "掉下去站到落点上按 F6 起头，走回主线后再按 F6。")

        # 面板模式：告诉界面"录制循环已停"（后面还有登记收尾，界面可先提示）
        try:
            print("@@STATE finished=1", flush=True)
        except Exception:
            pass

        # 自动登记（2026-09-12 用户需求）：正常退出且路线确实存过盘时，
        # 把这张图登记进 config_data.yaml 的 map_mobs_mapping（怪物列表留空，
        # 之后截怪物模板时会补充）。登记失败不影响已保存的路线。
        try:
            from src.utils.registry import register_map
            map_dir = os.path.join('minimaps', routeRecorder.args.new_map)
            if os.path.exists(os.path.join(map_dir, 'map.png')):
                if register_map(routeRecorder.args.new_map):
                    logger.info(f"[自动登记] 已把「{routeRecorder.args.new_map}」登记进 config_data.yaml"
                                f"（界面地图列表几秒内自动刷新，无需重启）")
                else:
                    logger.info(f"[自动登记] 「{routeRecorder.args.new_map}」此前已登记，跳过")
            else:
                logger.warning(f"[自动登记] 「{routeRecorder.args.new_map}」下没有 map.png —— 没有登记。"
                               f"（录完要先点【保存地图】再【结束录制】）")
        except Exception as e:
            logger.warning(f"[自动登记] 登记失败（不影响已保存的路线）: {e}")
        # 有控制台才等回车 —— 从界面 F4 启动时没有控制台，input() 会立即 EOF
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                input("\n[结束] 按回车关闭窗口 ")
        except Exception:
            pass
