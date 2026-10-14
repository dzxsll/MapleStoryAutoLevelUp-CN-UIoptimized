from src.states.base_state import State

class HuntingState(State):
    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def check_transitions(self):
        # 国服怀旧服无符文系统，打怪状态不再需要转移到其它状态
        return None

    def on_frame(self):
        # 指令来源按挂机方式分派：
        # 2026-09-12：定点模式（stand）已按用户要求删除 —— 站着不动的收益
        # 不如"来回走两步顺便打怪"，统一走巡逻。指令来源只剩路线一条。
        self.bot.update_cmd_by_route()

        # Check if reach goal on route map
        self.bot.check_reach_goal()

        # Get attack commend by detecting mobs near players
        self.bot.update_cmd_by_mob_detection()

        # 卡死处理（2026-09-15 改）
        # 原来固定调 update_cmd_by_random（12 种组合**均匀乱抽**，不知道悬崖在哪）：
        # ① 窄平台上乱抽 = 直接掉下去（实测抽到 right none jump，右边正好是悬崖）；
        # ② 它把真因**盖住**了（随机跳把角色从 (100,122) 踹到 (100,118)，
        #    症状从"在原地蹭"变成"跳到另一层"，日志上看"它动了"，更难查）。
        # 现在按 config 的 watchdog.on_stuck 分派，默认 alert = **不乱动 + 弹置顶提示**。
        if self.bot.is_player_stuck():
            self.bot.handle_stuck()

        # send command to keyboard controller
        self.bot.kb.set_command(self.bot.cmd_move_x + ' ' + \
                                self.bot.cmd_move_y + ' ' + \
                                self.bot.cmd_action)

        # 记录本帧**实际发出**的左右指令（2026-09-12 加）
        # 供下一帧判断「是否已经转向」—— 见 update_cmd_by_mob_detection 的先转身后出招。
        # 必须在 set_command 之后更新，否则记到的是本帧被覆盖前的值。
        self.bot.cmd_move_x_last = self.bot.cmd_move_x

        # ── 手动示范录制：本帧落一行（2026-10-14 加）───────────────────────
        # 放在**最后**：此刻路线/打怪/卡死处理都跑完，`cmd_*` 是本帧最终决策，
        # 录到的"影子决策"才是引擎本来会怎么打。没开录制时这行几乎零成本；
        # 录制器自带 try/except（出错只吼一次并自己关掉，绝不影响挂机）。
        self.bot.record_demo_frame()
