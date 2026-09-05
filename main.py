import asyncio
import re
import uuid
from datetime import datetime, timedelta

from astrbot.api.all import (
    AstrBotConfig,
    At,
    Context,
    Plain,
    Star,
    StarTools,
    filter,
    logger,
)
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType
from astrbot.core.utils.wake_prefix import 获取第一个唤醒词

from .data import Data

时长单位秒 = {
    "min": 60, "m": 60, "分钟": 60, "分": 60,
    "h": 3600, "小时": 3600, "时": 3600,
}

# 「HH:MM」与当前时刻差在该容差内视为"就是现在"，
# 避免 LLM 填了当前时间、当前分钟刚过一两秒就被滚到第二天
整分钟容差秒 = 120

# 框架/防抖在各处插入的 <system_reminder>...</system_reminder>，
# 暂存消息合并时全部清掉，只保留插件自己附加的最后一条醒来提示
系统提醒模式 = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)


class Sleep(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir()
        self.data = Data(self.data_dir)
        休息设置: dict = config["休息设置"]
        self.最长休息秒 = float(休息设置["最长休息小时"]) * 3600
        self.醒来提示模板: str = 休息设置["醒来提示"]
        # 每个会话独立的唤醒定时任务：umo -> asyncio.Task
        self.唤醒任务: dict[str, asyncio.Task] = {}
        self.唤醒锁 = asyncio.Lock()

    async def initialize(self) -> None:
        """可选异步初始化，当插件被激活时会调用这个方法"""
        # 插件重载/重启后恢复：没到点就重新排定时器，已经过了醒来的点就立刻醒来补回复
        for umo, 醒来时间 in self.data.获取全部睡觉安排().items():
            if self.data.是在睡觉(umo) and datetime.now() >= 醒来时间:
                asyncio.create_task(self.补唤醒(umo))
            else:
                self.安排唤醒任务(umo, 醒来时间)
                生效时间 = self.data.获取生效时间(umo)
                logger.info(
                    f"[睡觉] 恢复 {umo} 的睡觉安排："
                    f"{生效时间:%m-%d %H:%M} 生效，{醒来时间:%Y-%m-%d %H:%M} 醒来"
                )

    async def 补唤醒(self, umo: str) -> None:
        """启动时的补唤醒。插件 initialize 早于平台适配器实例化，需先等平台就绪。"""
        平台ID = self.data.获取暂存(umo).get("平台ID", "")
        for _ in range(120):
            if self.context.get_platform_inst(平台ID):
                break
            await asyncio.sleep(0.5)
        await self.执行唤醒(umo)

    async def terminate(self) -> None:
        """可选异步终止，当插件被禁用、重载前会调用这个方法"""
        for umo in list(self.唤醒任务):
            self.取消唤醒任务(umo)

    # ---------- 时间解析 ----------

    @staticmethod
    def 解析时间点(文本: str, 基准: datetime) -> datetime:
        """解析时间点。支持 yyyy-mm-dd HH:MM / yyyy-mm-dd / HH:MM，
        纯 HH:MM 早于基准时自动视为第二天（两分钟容差内视为"现在"）。
        解析失败抛 ValueError。
        """
        文本 = 文本.strip()
        for 格式 in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%H:%M", "%H:%M:%S"):
            try:
                时间 = datetime.strptime(文本, 格式)
            except ValueError:
                continue
            if 格式.startswith("%H"):
                # 只有时刻：默认今天
                时间 = 时间.replace(
                    year=基准.year, month=基准.month, day=基准.day,
                )
                if 时间 <= 基准:
                    if (基准 - 时间).total_seconds() <= 整分钟容差秒:
                        # 刚过去的整分钟（如 LLM 填了当前时间）视为立即
                        时间 = 基准
                    else:
                        时间 += timedelta(days=1)
            return 时间
        raise ValueError("支持的格式：yyyy-mm-dd HH:MM 或 HH:MM")

    @staticmethod
    def 解析时长(文本: str) -> float:
        """解析持续时间，返回秒数。

        支持 8h / 20min / 30（无单位为分钟），也支持多段组合，
        如 8h23min、1小时30分钟、1h30（无单位的段一律按分钟算）。
        """
        提示 = "支持的格式：8h / 20min / 30（无单位为分钟），可组合如 8h23min、1小时30分钟"
        文本 = 文本.strip()
        if not 文本:
            raise ValueError(提示)
        # 单位按长度降序排列：避免「分钟」被「分」截走、「min」被「m」截走
        段模式 = re.compile(r"(\d+(?:\.\d+)?)\s*(小时|分钟|min|分|时|h|m)?", re.IGNORECASE)
        总秒数 = 0.0
        位置 = 0
        while 位置 < len(文本):
            if 文本[位置].isspace():
                位置 += 1
                continue
            匹配 = 段模式.match(文本, 位置)
            if not 匹配:
                raise ValueError(提示)
            数值 = float(匹配.group(1))
            单位 = (匹配.group(2) or "min").lower()
            总秒数 += 数值 * 时长单位秒[单位]
            位置 = 匹配.end()
        return 总秒数

    @staticmethod
    def 格式化时长(秒数: float) -> str:
        秒数 = int(秒数)
        if 秒数 >= 3600:
            小时 = 秒数 / 3600
            return f"{小时:g} 小时"
        if 秒数 >= 60:
            return f"{秒数 // 60} 分钟"
        return f"{秒数} 秒"

    # ---------- 睡觉工具（供 LLM 调用） ----------

    @filter.llm_tool("sleep_tool")
    async def sleep_tool(self, event: AstrMessageEvent, 开始时间=None, 持续时间=None, 结束时间=None, 静默执行=False):
        """
        睡觉（休息）工具，当你想睡觉/休息时，可调用此工具，例如凌晨2点，进行符合System prompt的作息。
        休息期间，这个会话里用户发的消息将暂存起来，等到醒来之后，再进行llm请求回复。
        支持时间格式：yyyy-mm-dd HH:MM，或直接填 HH:MM，会自动解析是否为第二天
        Args:
            开始时间(string): 可选，开始睡觉的时间，默认立即开始，如晚上入睡时间,0:00，午睡时间13:00，若立即睡觉请勿填写此项，否则当前时间迅速过去会被解析到第二天
            持续时间(string): 可选，持续时间，支持单位：min（分钟），h（小时），例如24min，8h，也可组合多段如8h23min、1小时33分钟，不加单位则为分钟
            结束时间(string): 可选，结束时间，当填了持续时间时，则此项无效，用于精确休息结束时间，如起床时间 7:12，午休起床 14:43
            静默执行(boolean): 可选，静默执行此工具，默认false，设成true时，将立即入睡，参数‘开始时间’无效，执行完后工具将不会返回任何内容并直接结束本轮对话，比如用户在深夜发来消息，而按作息你应该已睡觉，则适合静默执行
        """
        umo = event.unified_msg_origin
        此刻 = datetime.now()
        # 静默执行：不惊动用户，忽略开始时间立即入睡，成功后不返回内容、直接结束本轮对话
        静默 = 静默执行 in (True, "true", "True", "1", 1)
        生效时间 = 此刻
        if not 静默 and 开始时间:
            try:
                生效时间 = self.解析时间点(str(开始时间), 此刻)
            except ValueError as e:
                return f"睡觉失败：开始时间「{开始时间}」无法识别。{e}"

        if 持续时间:
            try:
                时长秒 = self.解析时长(str(持续时间))
            except ValueError as e:
                return f"睡觉失败：持续时间「{持续时间}」无法识别。{e}"
            if 时长秒 <= 0:
                return "睡觉失败：持续时间必须大于 0。"
            醒来时间 = 生效时间 + timedelta(seconds=时长秒)
        elif 结束时间:
            try:
                醒来时间 = self.解析时间点(str(结束时间), 生效时间)
            except ValueError as e:
                return f"睡觉失败：结束时间「{结束时间}」无法识别。{e}"
            if 醒来时间 <= 生效时间:
                return f"睡觉失败：结束时间「{结束时间}」不晚于开始睡觉的时间。"
        else:
            return "睡觉失败：需要提供持续时间或结束时间其中之一。"

        # 最短休息兜底（时长从生效时刻起算）；超过上限直接拒绝，不静默截断
        醒来时间 = max(醒来时间, 生效时间 + timedelta(seconds=5))
        if 醒来时间 > 生效时间 + timedelta(seconds=self.最长休息秒):
            请求时长 = self.格式化时长((醒来时间 - 生效时间).total_seconds())
            return (
                f"睡觉失败：请求的休息时长 {请求时长} 超过了上限 "
                f"{self.格式化时长(self.最长休息秒)}，本次睡觉请求已被拒绝。"
                "请缩短持续时间或调整结束时间后重试。"
            )

        # 统一截断到秒：持久化只存到秒，内存里的时间若带微秒，
        # 定时唤醒到点后的相等比对会永远失配、静默不醒
        生效时间 = 生效时间.replace(microsecond=0)
        醒来时间 = 醒来时间.replace(microsecond=0)

        立即入睡 = 生效时间 <= 此刻
        新的一觉 = self.data.开始睡觉(
            umo=umo,
            生效时间=生效时间,
            醒来时间=醒来时间,
            原因=event.message_str or "",
        )
        self.安排唤醒任务(umo, 醒来时间)
        休息时长描述 = self.格式化时长((醒来时间 - 生效时间).total_seconds())
        logger.info(
            f"[睡觉] {'调整安排' if not 新的一觉 else '入睡'}{'（静默）' if 静默 else ''}：{umo} "
            f"{'立即生效' if 立即入睡 else f'{生效时间:%m-%d %H:%M} 生效'}，"
            f"将于 {醒来时间:%Y-%m-%d %H:%M} 醒来"
        )
        if not 立即入睡:
            文案 = (
                f"已安排休息：{生效时间:%Y-%m-%d %H:%M} 开始睡觉，"
                f"{醒来时间:%Y-%m-%d %H:%M} 醒来（休息 {休息时长描述}）。"
                "到点前你仍正常回复消息；开始休息后这个会话里收到的消息会被暂存，醒来后统一回复。"
            )
        elif 新的一觉:
            文案 = (
                f"已入睡，将于 {醒来时间:%Y-%m-%d %H:%M} 醒来"
                f"（休息 {休息时长描述}）。"
                "休息期间这个会话里收到的消息会被暂存，醒来后统一回复。现在可以和用户道晚安或午安了。"
            )
        else:
            文案 = (
                f"原本就有休息安排，已调整为 {生效时间:%m-%d %H:%M} 生效、"
                f"{醒来时间:%Y-%m-%d %H:%M} 醒来。"
                "休息期间这个会话里收到的消息会被暂存，醒来后统一回复。"
            )
        if 静默:
            return None
        return 文案

    @filter.llm_tool("wake_tool")
    async def wake_tool(self, event: AstrMessageEvent):
        """
        取消睡觉（休息）安排工具。当你之前安排了一个还没开始的睡觉/休息（例如计划今晚23:00睡），但现在改变主意不想睡了，可调用此工具取消，取消后你继续正常回复消息。
        注意：正在睡觉中你收不到任何用户消息，因此无法在睡觉中调用此工具；睡觉只能等自然醒来，或由管理员用「/起床」指令叫醒。
        """
        umo = event.unified_msg_origin
        if not self.data.有睡觉安排(umo):
            return "当前没有休息安排。"
        已生效 = self.data.是在睡觉(umo)
        await self.执行唤醒(umo)
        if 已生效:
            # 睡觉中收不到消息，理论上到不了这里，仅兜底
            return "该休息安排已生效，现已将其结束。"
        return "已取消尚未开始的休息安排，你将继续正常回复消息。"

    # ---------- 消息拦截（睡觉期间暂存） ----------

    @filter.on_llm_request()
    async def llm请求前(self, event: AstrMessageEvent, req):
        """llm请求前，检查本会话是否在睡觉，在睡则暂存消息并拦截本次请求"""

        umo = event.unified_msg_origin
        if not self.data.是在睡觉(umo):
            return

        文本 = self.提取请求文本(req)
        self.data.暂存一条消息(
            umo,
            信封=self.构造信封(event),
            消息={
                "发送者ID": event.get_sender_id(),
                "昵称": event.get_sender_name() or "",
                "文本": 文本,
                "时间": self.格式化时间戳(datetime.now()),
            },
        )
        # 拦截本次 LLM 请求：睡觉时不回复，攒到醒来再说
        event.stop_event()
        logger.info(f"[睡觉] 已暂存 {umo} 的消息，等醒来再回复")

        # 到点还没被定时器唤醒（比如机器负载高），由这条消息顺手触发醒来
        醒来时间 = self.data.获取醒来时间(umo)
        if 醒来时间 and datetime.now() >= 醒来时间:
            await self.执行唤醒(umo)

    # ---------- 唤醒 ----------

    def 安排唤醒任务(self, umo: str, 醒来时间: datetime) -> None:
        self.取消唤醒任务(umo)
        任务 = asyncio.create_task(self.定时唤醒(umo, 醒来时间))
        任务.add_done_callback(lambda t: self.唤醒任务.pop(umo, None))
        self.唤醒任务[umo] = 任务

    def 取消唤醒任务(self, umo: str) -> None:
        任务 = self.唤醒任务.pop(umo, None)
        if 任务 and not 任务.done():
            任务.cancel()

    async def 定时唤醒(self, umo: str, 醒来时间: datetime) -> None:
        while True:
            剩余 = (醒来时间 - datetime.now()).total_seconds()
            if 剩余 <= 0:
                break
            await asyncio.sleep(剩余)
            # 到点后复查最新安排：被取消就结束，被推迟就跟着新时间继续等；
            # 不要求精确相等，当前时间过了醒来点就算到点（防阻塞/精度差导致永远不醒）
            当前醒来时间 = self.data.获取醒来时间(umo)
            if 当前醒来时间 is None:
                return
            if 当前醒来时间 > 醒来时间:
                醒来时间 = 当前醒来时间
        await self.执行唤醒(umo)

    async def 执行唤醒(self, umo: str) -> None:
        async with self.唤醒锁:
            if not self.data.有睡觉安排(umo):
                return
            信封 = self.data.取走暂存(umo)
            # 先清状态再入队：醒来事件的 LLM 请求不能再被本插件拦截
        消息列表 = (信封 or {}).get("消息") or []
        if not 消息列表:
            logger.info(f"[睡觉] {umo} 结束睡觉安排，期间没有暂存的消息")
            return
        logger.info(f"[睡觉] {umo} 醒来了，需要回复 {len(消息列表)} 条暂存消息")

        try:
            平台 = self.context.get_platform_inst(信封.get("平台ID", ""))
            if 平台 is None:
                raise RuntimeError(f"平台 {信封.get('平台ID')} 不存在")
            abm = self.构造醒来消息对象(信封, 消息列表)
            self.context.get_event_queue().put_nowait(平台.create_event(abm))
            logger.info(f"[睡觉] 已向 {umo} 投递醒来汇总消息（{len(消息列表)} 条）")
        except Exception as e:
            logger.error(f"[睡觉] 唤醒会话 {umo} 失败：{e}")

    def 构造信封(self, event: AstrMessageEvent) -> dict:
        """记录会话的基础信息，醒来后凭它重建消息事件。"""
        信封 = {
            "平台ID": event.get_platform_id(),
            "会话类型": "私聊" if event.is_private_chat() else "群聊",
            "会话ID": event.message_obj.session_id,
            "自身ID": event.get_self_id(),
        }
        if 信封["会话类型"] == "群聊" and event.message_obj.group:
            信封["群名"] = event.message_obj.group.group_name or ""
        return 信封

    # ---------- 暂存内容的提取与格式化 ----------

    @staticmethod
    def 提取请求文本(req) -> str:
        """用框架处理后的 LLM 请求内容作为暂存文本：
        prompt + 附加内容（附件标注、引用消息、图片转述等），
        多媒体与附件跟随框架的处理结果，而不是原始消息的占位符。
        """
        部分 = []
        提示 = (getattr(req, "prompt", None) or "").strip()
        if 提示:
            部分.append(提示)
        for 部件 in getattr(req, "extra_user_content_parts", None) or []:
            文本 = getattr(部件, "text", None)
            if 文本 and 文本.strip():
                部分.append(文本.strip())
        return "\n".join(部分).strip()

    @staticmethod
    def 清理系统提醒(文本: str) -> str:
        """去掉框架/防抖插入的 <system_reminder>，压掉清理后留下的多余空行。"""
        清理后 = 系统提醒模式.sub("", 文本 or "")
        return re.sub(r"\n{3,}", "\n\n", 清理后).strip()

    @staticmethod
    def 格式化时间戳(时间: datetime) -> str:
        """消息时间戳格式：2026-9-5 15:38:57（日期不补零，时分秒补零）。"""
        return f"{时间.year}-{时间.month}-{时间.day} {时间:%H:%M:%S}"

    def 合并暂存文本(self, 信封: dict, 消息列表: list[dict]) -> str:
        """把暂存消息合并成一条文本。群聊带昵称区分发言人，私聊不带。"""
        群聊 = 信封.get("会话类型") == "群聊"
        有效条目 = []
        for 条目 in 消息列表:
            文本 = self.清理系统提醒(条目.get("文本", ""))
            if 文本:
                有效条目.append((条目, 文本))
        行列表 = []
        for 序号, (条目, 文本) in enumerate(有效条目, 1):
            前缀 = f"[Message {序号}; {条目.get('时间', '')}]"
            if 群聊:
                发送者 = 条目.get("昵称") or 条目.get("发送者ID") or "未知用户"
                行列表.append(f"{前缀} {发送者}: {文本}")
            else:
                行列表.append(f"{前缀} {文本}")
        数量 = len(行列表)
        提醒正文 = self.醒来提示模板.replace("{数量}", str(数量))
        # 配置里只写正文；旧配置若自带 <system_reminder> 包装则不重复添加
        if "<system_reminder>" not in 提醒正文:
            提醒正文 = f"<system_reminder>{提醒正文}</system_reminder>"
        return "\n".join(行列表) + "\n" + 提醒正文

    def 构造醒来消息对象(self, 信封: dict, 消息列表: list[dict]) -> AstrBotMessage:
        """用暂存的消息构造一条新的会话消息，走完整 pipeline 请求 LLM 并回复。"""
        群聊 = 信封.get("会话类型") == "群聊"
        自身ID = 信封.get("自身ID", "")
        文本 = self.合并暂存文本(信封, 消息列表)

        abm = AstrBotMessage()
        abm.type = MessageType.GROUP_MESSAGE if 群聊 else MessageType.FRIEND_MESSAGE
        abm.self_id = 自身ID
        第一条 = 消息列表[0]
        abm.sender = MessageMember(
            user_id=str(第一条.get("发送者ID", "")),
            nickname=第一条.get("昵称") or None,
        )
        abm.session_id = 信封.get("会话ID", "")
        if 群聊:
            abm.group = Group(group_id=信封.get("会话ID", ""))
            abm.group.group_name = 信封.get("群名") or "N/A"
            # 群聊带 At 自己的消息段保证唤醒；文本里不含 At，prompt 是干净的
            abm.message = [At(qq=自身ID), Plain(text=文本)]
        else:
            abm.message = [Plain(text=文本)]
        abm.message_str = 获取第一个唤醒词() + 文本
        abm.message_id = uuid.uuid4().hex
        abm.timestamp = int(datetime.now().timestamp())
        abm.raw_message = None
        return abm

    # ---------- 管理指令 ----------

    @filter.command("睡觉状态", alias={"睡了吗"})
    async def 睡觉状态(self, event: AstrMessageEvent):
        """查看各会话的睡觉安排与暂存消息数。指令：/睡觉状态"""
        安排 = self.data.获取全部睡觉安排()
        if not 安排:
            yield event.plain_result("现在所有会话都是清醒状态～")
            return
        行列表 = ["😴 睡觉安排："]
        本会话 = event.unified_msg_origin
        for umo, 醒来时间 in 安排.items():
            标记 = "（本会话）" if umo == 本会话 else ""
            暂存数 = len(self.data.获取暂存(umo).get("消息", []))
            if self.data.是在睡觉(umo):
                行列表.append(
                    f"· {umo}{标记}：😴 睡觉中，{醒来时间:%m-%d %H:%M} 醒来，暂存 {暂存数} 条"
                )
            else:
                生效时间 = self.data.获取生效时间(umo)
                行列表.append(
                    f"· {umo}{标记}：⏰ {生效时间:%m-%d %H:%M} 开始休息，{醒来时间:%m-%d %H:%M} 醒来"
                )
        yield event.plain_result("\n".join(行列表))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("起床", alias={"取消睡觉"})
    async def 起床(self, event: AstrMessageEvent, 范围: str = ""):
        """管理员手动叫醒/取消睡觉安排，暂存的消息会立刻汇总回复。指令：/起床 [全部]"""
        if 范围.strip() == "全部":
            安排 = list(self.data.获取全部睡觉安排())
            if not 安排:
                yield event.plain_result("所有会话都没在睡～")
                return
            for umo in 安排:
                await self.执行唤醒(umo)
            yield event.plain_result(f"已处理 {len(安排)} 个会话的睡觉安排。")
            return
        umo = event.unified_msg_origin
        if not self.data.有睡觉安排(umo):
            yield event.plain_result("本会话没有睡觉安排～")
            return
        if not self.data.是在睡觉(umo):
            yield event.plain_result(
                f"本会话还没开始睡（{self.data.获取生效时间(umo):%m-%d %H:%M} 生效），已帮你取消。"
            )
            await self.执行唤醒(umo)
            return
        yield event.plain_result("好，马上起床处理暂存的消息！")
        await self.执行唤醒(umo)
