'''
KeyBoardListener for routeRecorder.py
'''
# Standard Import
import threading
import time

import pygetwindow as gw
from pynput import keyboard

# Local import
from src.utils.logger import logger

class KeyBoardListener():
    '''
    KeyBoardListener
    '''
    def __init__(self, cfg=None, is_autobot=True):
        self.cfg = cfg
        self.t_last_run = time.time()
        self.is_enable = True
        self.debounce_interval = 1 # second
        self.is_terminated = False
        self.fps = 0
        self.fps_limit = 30
        self.key_pressing = [] # record the key pressed by user
        self.is_pressed_func_key = [False]*12  # 'F1', 'F2', .... 'F12'
        # 「用户请求退出」标志：pynput 回调事件驱动置位、由主循环消费。
        # 不用 cv2.waitKey 采样按键 —— 录制器单帧处理远慢于人按键时长（轻按
        # 不到 0.2 秒），10fps 轮询几乎必丢键（2026-09-12 实测：q 退不出）。
        self.quit_requested = False
        # 跳跃键的"按过"标志（边沿触发，录制器消费一次；见 on_press / consume_jump_press）
        self._jump_pending = False

        # Timer
        self.t_func_key = [0]*12 # 'F1', 'F2', .... 'F12'

        # Keys
        self.movement_keys = {
            keyboard.Key.up: "up",
            keyboard.Key.down: "down",
            keyboard.Key.left: "left",
            keyboard.Key.right: "right",
            keyboard.Key.space: "space"
        }
        # ★ 2026-10-13：**修饰键也要能当跳跃键**（`key.jump: alt` 是很常见的配法）。
        #   根因：on_press 认键只有两条路 —— ① 普通字符键 `key.char.lower()`
        #   ② `self.movement_keys.get(key)`。修饰键**没有 `.char`**（AttributeError），
        #   只能走 ②；而 ② 的表里原来只有 up/down/left/right/space ⇒ k 恒为 None
        #   ⇒ `if k and k == self._jump_key_name()` 恒假 ⇒ `_jump_pending` 永不置位
        #   ⇒ 录制时**一次跳都记不下来**（全程静默、不报错；线里 0 个跳跃像素，
        #   真机表现是「爬不上管子 / 走到该跳的地方不跳」；2026-10-13 用户
        #   key.jump=alt 实测：route1/route2 只有 left/right 线 + 一个 goal 圆点）。
        #   用 getattr 逐个试，平台缺哪个 Key 就跳过（不写死，换平台不会炸）。
        for _names, _norm in (
            (("alt", "alt_l", "alt_r", "alt_gr"), "alt"),
            (("ctrl", "ctrl_l", "ctrl_r"), "ctrl"),
            (("shift", "shift_l", "shift_r"), "shift"),
            (("cmd", "cmd_l", "cmd_r"), "cmd"),
        ):
            for _n in _names:
                _key = getattr(keyboard.Key, _n, None)
                if _key is not None:
                    self.movement_keys[_key] = _norm
        self.func_keys = {
            getattr(keyboard.Key, f"f{i+1}"): i for i in range(12)
        }

        self.func_key_handlers = {
            f"f{i}": self.do_nothing for i in range(1, 13)
        }

        # Start keyboard control thread
        if is_autobot:
            threading.Thread(target=self.run_for_autobot, daemon=True).start()
        else:
            self.cfg = cfg
            self.window_title = cfg["game_window"]["title"]
            threading.Thread(target=self.run_for_route_recorder, daemon=True).start()

        listener = keyboard.Listener(on_press=self.on_press,
                                     on_release=self.on_release)
        listener.start()

    def do_nothing(self):
        pass

    def register_func_key_handler(self, key: str, handler: callable):
        key = key.lower()
        if key in self.func_key_handlers:
            self.func_key_handlers[key] = handler
        else:
            logger.warning(f"[KeyBoardListener] '{key}' is not a supported function key.")

    def on_release(self, key):
        '''
        Handle key release events and update key_pressing list.
        '''
        try:
            # Regular keys (like 'a', '1', etc.)
            k = key.char.lower()
        except AttributeError:
            k = self.movement_keys.get(key, None)

        # Remove the key from key_pressing list if it's in there
        if k in self.key_pressing:
            self.key_pressing.remove(k)

    def on_press(self, key):
        '''
        Handle key press events.
        '''
        try:
            # Regular character keys (e.g., 'a', 'w', '1')
            k = key.char.lower()
        except AttributeError:
            # Handle F1, F2, F3, ... F12
            if key in self.func_keys:
                idx = self.func_keys[key]
                if time.time() - self.t_func_key[idx] > self.debounce_interval:
                    self.is_pressed_func_key[idx] = True # Polling
                    self.func_key_handlers.get(key.name.lower())()
                    self.t_func_key[idx] = time.time()

            k = self.movement_keys.get(key, None)

        if k and k not in self.key_pressing:
            self.key_pressing.append(k)

        if k == 'q':
            self.quit_requested = True

        # ★ 2026-09-17：**跳跃键必须边沿触发**（跟 q 一个道理，见 __init__ 的说明）。
        #    `key_pressing` 是"当前按住"的快照，而录制器只有 **10fps（100ms/帧）**；
        #    一次"跳"是轻按（<100~200ms）⇒ 轮询**大概率整帧都没看到** ⇒ 跳没被记下来。
        #    真机反例：用户跳跃键 = c，"边按右、边跳、边按上"爬管子，重录两遍都是
        #    **0 个跳跃像素**，机器人走到管柱底下只会按 up ⇒ 爬不上去。
        #    ⇒ 按下时置位，由录制器主循环 `consume_jump_press()` 取走（取走即清）。
        if k and k == self._jump_key_name():
            self._jump_pending = True

    def _jump_key_name(self):
        '''config 里配的跳跃键（`key.jump`），归一化成小写；拿不到就回落 "space"。'''
        try:
            return str((self.cfg or {}).get("key", {}).get("jump") or "space").strip().lower() or "space"
        except Exception:                                    # noqa: BLE001
            return "space"

    def consume_jump_press(self):
        '''自上次调用以来，跳跃键**被按过**吗？（边沿触发，取走即清）

        ⚠️ 为什么不能用 `key_pressing` 轮询：录制器 10fps，轻按会整帧漏掉。
           同 `quit_requested` 的教训（2026-09-12 实测 q 退不出）。
        '''
        hit = getattr(self, "_jump_pending", False)
        self._jump_pending = False
        return hit

    def toggle_enable(self):
        '''
        toggle_enable
        '''
        # self.is_enable = not self.is_enable
        logger.info(f"Player pressed F1, is_enable:{self.is_enable}")

    def disable(self):
        '''
        disable keyboard controlller
        '''
        self.is_enable = False

    def enable(self):
        '''
        enable keyboard controlller
        '''
        self.is_enable = True

    def stop(self):
        '''
        Stop keyboard listener thread
        '''
        self.is_terminated = True

    def is_game_window_active(self):
        '''
        Check if the game window is currently the active (foreground) window.

        Returns:
        - True
        - False
        '''
        active_window = gw.getActiveWindow()
        return active_window is not None and self.window_title in active_window.title

    def limit_fps(self):
        '''
        Limit FPS
        '''
        # If the loop finished early, sleep to maintain target FPS
        target_duration = 1.0 / self.fps_limit  # seconds per frame
        frame_duration = time.time() - self.t_last_run
        if frame_duration < target_duration:
            time.sleep(target_duration - frame_duration)

        # Update FPS
        self.fps = round(1.0 / (time.time() - self.t_last_run))
        self.t_last_run = time.time()
        # logger.info(f"FPS = {self.fps}")

    def run_for_route_recorder(self):
        '''
        run
        '''
        while not self.is_terminated:
            # Check if game window is active
            if not self.is_enable or not self.is_game_window_active():
                self.limit_fps()
                continue

            self.limit_fps()

    def run_for_autobot(self):
        '''
        run
        '''
        while not self.is_terminated:
            self.limit_fps()

        logger.info("[KeyBoardListener] Terminated")
