import asyncio
import random
import re
import uuid
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger

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

# 定时任务未指定随机误差时的默认值：入睡与醒来各自 ±20 分钟随机偏移
默认随机误差秒 = 20 * 60

# 睡前提醒内容的默认模板，{n} 为距入睡的分钟数
默认睡前提醒 = (
    "(sleep task) 你还有 {n} 分钟就要睡了。"
    "可以趁现在主动告诉用户你快要去睡觉了，或者陪用户聊最后一会儿；"
    "到点睡觉任务会让你自动入睡。"
)

# 框架/防抖在各处插入的 <system_reminder>...</system_reminder>，
# 暂存消息合并时全部清掉，只保留插件自己附加的最后一条醒来提示
系统提醒模式 = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)


def 解析cron表达式(表达式) -> CronTrigger:
    """解析标准 5 位 cron 表达式（分 时 日 月 周），失败抛 ValueError。"""
    表达式 = str(表达式 or "").strip()
    字段数 = len(表达式.split())
    if 字段数 != 5:
        raise ValueError(
            f"cron 表达式需要 5 个字段（分 时 日 月 周），实际收到 {字段数} 个，"
            '例如 "0 23 * * *" 表示每天 23:00'
        )
    try:
        return CronTrigger.from_crontab(表达式)
    except ValueError as e:
        raise ValueError(f"cron 表达式「{表达式}」无法解析：{e}") from None


def 计算下次触发(表达式: str, 基准: datetime | None = None) -> datetime | None:
    """cron 的下一次触发时间（本地时间，去掉时区信息）。

    Returns:
        下次触发时间；表达式在可预见的未来永远不会触发时返回 None。
    """
    下次 = 解析cron表达式(表达式).get_next_fire_time(None, 基准 or datetime.now())
    if 下次 is None:
        return None
    return 下次.astimezone().replace(tzinfo=None)


class Sleep(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir()
        self.data = Data(self.data_dir)
        休息设置: dict = config["休息设置"]
        self.最长休息秒 = float(休息设置["最长休息小时"]) * 3600
        # 「HH:MM」与当前时刻差在该容差内视为"就是现在"，
        # 避免 LLM 填了当前时间、当前分钟刚过一两秒就被滚到第二天
        self.时间点容差秒 = int(休息设置.get("时间点容差秒", 120))
        self.最短休息秒 = int(休息设置.get("最短休息秒", 5))
        self.暂存消息上限 = int(休息设置.get("暂存消息上限", 100))
        self.醒来提示模板: str = 休息设置["醒来提示"]
        # 睡前提醒：入睡时刻前 N 分钟自动唤醒一次 LLM；分钟数为 0 或提示词留空都视为关闭
        self.睡前提醒秒 = int(休息设置.get("睡前提醒分钟", 30)) * 60
        # 提醒自己的随机误差（与任务的入睡/醒来误差相互独立）
        self.睡前提醒误差秒 = int(休息设置.get("睡前提醒随机误差秒", 120))
        self.睡前提醒模板: str = 休息设置.get("睡前提醒内容", 默认睡前提醒) or ""
        if not self.睡前提醒模板.strip():
            self.睡前提醒秒 = 0
        # 每个会话独立的唤醒定时任务：umo -> asyncio.Task
        self.唤醒任务: dict[str, asyncio.Task] = {}
        self.唤醒锁 = asyncio.Lock()
        # 每个会话独立的定时任务调度循环：umo -> asyncio.Task
        self.调度循环: dict[str, asyncio.Task] = {}

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
        # 恢复定时睡觉任务的调度循环；离线期间错过的触发点会在首轮立即补触发
        for umo in self.data.获取全部任务会话():
            self.安排调度循环(umo)

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
        for umo in list(self.调度循环):
            self.取消调度循环(umo)

    # ---------- 时间解析 ----------

    def 解析时间点(self, 文本: str, 基准: datetime) -> datetime:
        """解析时间点。支持 yyyy-mm-dd HH:MM / yyyy-mm-dd / HH:MM，
        纯 HH:MM 早于基准时自动视为第二天（容差内视为"现在"）。
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
                    if (基准 - 时间).total_seconds() <= self.时间点容差秒:
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

    # ---------- 睡觉工具（供 LLM 调用，工具名与参数名为英文） ----------

    @filter.llm_tool("sleep_now")
    async def 立即入睡(self, event: AstrMessageEvent, duration=None, end_time=None, silent=False):
        """
        立即入睡（开始休息）工具。当你现在就想睡觉/休息时调用此工具，例如到了该睡的时间、该午休了。入睡后这个会话里用户发来的消息将被暂存，到点自动醒来后再统一回复。duration 与 end_time 二选一填写。
        Args:
            duration(string): 可选，休息持续时长，支持单位 min（分钟）、h（小时），如 8h、20min、8h23min、1小时30分钟，不带单位按分钟算；与 end_time 二选一
            end_time(string): 可选，醒来的时间，如 07:12 或 2026-09-07 07:12，只填 HH:MM 且早于当前时刻时自动算作第二天；与 duration 二选一
            silent(boolean): 可选，静默执行，默认 false。设为 true 时本工具不返回任何内容并直接结束本轮对话，适合深夜用户发来消息而按作息你应该已经在睡觉的场景
        """
        umo = event.unified_msg_origin
        此刻 = datetime.now()
        # 静默执行：成功后不返回内容、直接结束本轮对话
        静默 = silent in (True, "true", "True", "1", 1)
        生效时间 = 此刻
        if duration not in (None, ""):
            try:
                时长秒 = self.解析时长(str(duration))
            except ValueError as e:
                return f"睡觉失败：时长「{duration}」无法识别。{e}"
            if 时长秒 <= 0:
                return "睡觉失败：时长必须大于 0。"
            醒来时间 = 生效时间 + timedelta(seconds=时长秒)
        elif end_time not in (None, ""):
            try:
                醒来时间 = self.解析时间点(str(end_time), 生效时间)
            except ValueError as e:
                return f"睡觉失败：结束时间「{end_time}」无法识别。{e}"
            if 醒来时间 <= 生效时间:
                return f"睡觉失败：结束时间「{end_time}」不晚于当前时间。"
        else:
            return "睡觉失败：需要提供 duration（时长）或 end_time（结束时间）其中之一。"

        # 超过上限直接拒绝，不静默截断
        if 醒来时间 > 生效时间 + timedelta(seconds=self.最长休息秒):
            请求时长 = self.格式化时长((醒来时间 - 生效时间).total_seconds())
            return (
                f"睡觉失败：请求的休息时长 {请求时长} 超过了上限 "
                f"{self.格式化时长(self.最长休息秒)}，本次睡觉请求已被拒绝。"
                "请缩短 duration 或调整 end_time 后重试。"
            )

        新的一觉 = self.执行入睡(
            umo, 生效时间=生效时间, 醒来时间=醒来时间, 原因=event.message_str or "",
        )
        休息时长描述 = self.格式化时长((醒来时间 - 生效时间).total_seconds())
        logger.info(
            f"[睡觉] {'调整安排' if not 新的一觉 else '入睡'}{'（静默）' if 静默 else ''}：{umo}，"
            f"将于 {醒来时间:%Y-%m-%d %H:%M} 醒来"
        )
        if 新的一觉:
            文案 = (
                f"已入睡，将于 {醒来时间:%Y-%m-%d %H:%M} 醒来"
                f"（休息 {休息时长描述}）。"
                "休息期间这个会话里收到的消息会被暂存，醒来后统一回复。现在可以和用户道晚安或午安了。"
            )
        else:
            文案 = (
                f"原本就有休息安排，已调整为现在生效、{醒来时间:%Y-%m-%d %H:%M} 醒来"
                f"（休息 {休息时长描述}）。"
                "休息期间这个会话里收到的消息会被暂存，醒来后统一回复。"
            )
        if 静默:
            return None
        return 文案

    @filter.llm_tool("create_sleep_task")
    async def 创建睡觉任务(self, event: AstrMessageEvent, cron=None, duration=None, count=None, jitter=None, name=None):
        """
        创建定时睡觉任务：按 cron 表达式到点自动入睡，适合固定的作息安排（例如每天晚上 23:00 睡到早上 7:00）。到触发时刻会立即按 duration 入睡（若已在睡觉中，则把醒来时间顺延），休息结束自动醒来。入睡与醒来的时刻默认各带 ±20 分钟随机误差，模拟真实作息、避免每天机械准点。
        Args:
            cron(string): 标准 5 位 cron 表达式（分 时 日 月 周），如 "0 23 * * *" 表示每天 23:00，"30 13 * * 1-5" 表示工作日 13:30，"0 23 6 9 *" 表示 9 月 6 日 23:00（指定日期可用来安排今晚的一次性睡觉）
            duration(string): 每次触发后的休息持续时长，格式同 sleep_now 的 duration，如 8h 表示睡 8 小时
            count(number): 可选，总共执行的次数，不填或 0 表示一直重复执行，直到用 cancel_sleep_task 取消
            jitter(string): 可选，随机时间误差：每次触发时入睡时刻与醒来时刻都会各自在 ±该值 内随机偏移（两者相互独立），默认 20min；支持 min（分钟）、h（小时），不带单位按分钟算，填 0 表示完全准点
            name(string): 可选，任务名称或备注，方便之后分辨这个任务是干什么的，如「晚上作息」「午休」
        """
        umo = event.unified_msg_origin
        cron表达式 = str(cron or "").strip()
        if not cron表达式:
            return '创建睡觉任务失败：需要提供 cron 表达式，如 "0 23 * * *" 表示每天 23:00。'
        if duration in (None, ""):
            return "创建睡觉任务失败：需要提供 duration（每次触发后的休息时长），如 8h。"
        try:
            时长秒 = self.解析时长(str(duration))
        except ValueError as e:
            return f"创建睡觉任务失败：时长「{duration}」无法识别。{e}"
        if 时长秒 <= 0:
            return "创建睡觉任务失败：时长必须大于 0。"
        if 时长秒 > self.最长休息秒:
            return (
                f"创建睡觉任务失败：时长 {self.格式化时长(时长秒)} 超过了单次休息上限 "
                f"{self.格式化时长(self.最长休息秒)}，请缩短后重试。"
            )
        try:
            次数 = int(float(count)) if count not in (None, "") else 0
        except (TypeError, ValueError):
            return f"创建睡觉任务失败：次数「{count}」无法识别，请填非负整数，0 或不填表示一直重复。"
        次数 = max(次数, 0)
        名称 = str(name or "").strip()[:50]
        误差秒 = 默认随机误差秒
        if jitter not in (None, ""):
            try:
                误差秒 = self.解析时长(str(jitter))
            except ValueError as e:
                return f"创建睡觉任务失败：随机误差「{jitter}」无法识别。{e}"
            if 误差秒 < 0:
                return "创建睡觉任务失败：随机误差不能为负数。"
            if 误差秒 > self.最长休息秒:
                return (
                    f"创建睡觉任务失败：随机误差 ±{self.格式化时长(误差秒)} 过大，"
                    f"不能超过单次休息上限 {self.格式化时长(self.最长休息秒)}。"
                )
        try:
            下次 = self.计算任务下次触发({"cron": cron表达式, "误差秒": 误差秒})
        except ValueError as e:
            return f"创建睡觉任务失败：{e}"
        if 下次 is None:
            return (
                f"创建睡觉任务失败：cron 表达式「{cron表达式}」在未来永远不会触发，"
                "请检查日期字段是否有效（例如 2 月没有 30 日）。"
            )

        任务ID = uuid.uuid4().hex[:8]
        提醒 = self.计算任务提醒时间(下次)
        self.data.添加任务(
            umo, 任务ID=任务ID, cron=cron表达式, 时长秒=时长秒, 次数=次数,
            误差秒=误差秒, 下次触发=下次, 信封=self.构造信封(event),
            下次提醒=提醒, 名称=名称, 原因=event.message_str or "",
        )
        self.安排调度循环(umo)
        次数描述 = f"共执行 {次数} 次" if 次数 > 0 else "一直重复执行"
        误差描述 = (
            f"入睡与醒来带 ±{self.格式化时长(误差秒)} 随机误差"
            if 误差秒 > 0
            else "入睡与醒来完全准点"
        )
        提醒描述 = f"睡前提醒会在 {提醒:%m-%d %H:%M} 左右自动叫你一声，" if 提醒 else ""
        ID描述 = f"任务ID {任务ID}" + (f"，名称「{名称}」" if 名称 else "")
        logger.info(
            f"[睡觉] 创建定时任务 {任务ID}：{umo} cron={cron表达式}，"
            f"每次睡 {self.格式化时长(时长秒)}，{次数描述}，{误差描述}，下次触发 {下次:%Y-%m-%d %H:%M}"
        )
        return (
            f"睡觉任务创建成功（{ID描述}）：cron「{cron表达式}」，"
            f"每次触发睡 {self.格式化时长(时长秒)}，{次数描述}，{误差描述}。"
            f"{提醒描述}下次触发时间 {下次:%Y-%m-%d %H:%M}，到点会自动入睡，可用 cancel_sleep_task 取消。"
        )

    @filter.llm_tool("cancel_sleep_task")
    async def 取消睡觉任务(self, event: AstrMessageEvent, task_id=None):
        """
        取消定时睡觉任务工具，取消后到点不再自动入睡。不影响正在进行中的休息（睡觉中收不到任何消息、无法调用工具，只能等自然醒来或由管理员处理）。
        Args:
            task_id(string): 可选，要取消的任务 ID（可先用 query_sleep_tasks 查询）；不填则取消本会话全部睡觉任务
        """
        umo = event.unified_msg_origin
        任务列表 = self.data.获取任务列表(umo)
        if not 任务列表:
            return "当前没有睡觉任务。"
        任务ID = str(task_id).strip() if task_id not in (None, "") else None
        if 任务ID and 任务ID not in 任务列表:
            ID列表 = "、".join(任务列表)
            return (
                f"没有找到任务「{任务ID}」，当前的任务 ID：{ID列表}。"
                "可先用 query_sleep_tasks 查询详情。"
            )
        被删 = self.data.删除任务(umo, 任务ID)
        if not self.data.获取任务列表(umo):
            self.取消调度循环(umo)
        描述 = "、".join(f"「{任务.get('cron', '?')}」" for 任务 in 被删)
        logger.info(f"[睡觉] 取消定时任务：{umo}，共 {len(被删)} 个")
        return (
            f"已取消 {len(被删)} 个睡觉任务：{描述}。"
            "到点不再自动入睡（不影响正在进行中的休息）。"
        )

    def 任务提醒描述(self, 任务: dict) -> str:
        """任务的睡前提醒描述片段（带前导逗号），关闭时为空。"""
        if self.睡前提醒秒 <= 0:
            return ""
        if int(任务.get("已提醒", 0)):
            return "，睡前提醒已发"
        提醒 = self.data.解析任务时间(任务, "下次提醒")
        if 提醒:
            return f"，睡前提醒 {提醒:%m-%d %H:%M}"
        return "，睡前提醒待计算"

    @filter.llm_tool("query_sleep_tasks")
    async def 查询睡觉任务(self, event: AstrMessageEvent):
        """
        查询本会话的定时睡觉任务与睡觉状态工具。返回每个任务的 ID、cron 表达式、每次休息时长、已执行次数与总次数、下次触发时间，以及当前是否在睡觉中。取消某个任务前先用它获取任务 ID。
        """
        umo = event.unified_msg_origin
        部分 = []
        if self.data.有睡觉安排(umo):
            醒来时间 = self.data.获取醒来时间(umo)
            if self.data.是在睡觉(umo):
                部分.append(f"当前状态：😴 睡觉中，将于 {醒来时间:%Y-%m-%d %H:%M} 醒来。")
            else:
                生效时间 = self.data.获取生效时间(umo)
                部分.append(
                    f"当前状态：⏰ 已有休息安排，{生效时间:%m-%d %H:%M} 生效，"
                    f"{醒来时间:%m-%d %H:%M} 醒来。"
                )
        else:
            部分.append("当前状态：清醒，没有正在进行的休息。")

        任务列表 = self.data.获取任务列表(umo)
        if not 任务列表:
            部分.append("当前没有定时睡觉任务，可用 create_sleep_task 创建。")
        else:
            部分.append(f"睡觉任务共 {len(任务列表)} 个：")
            for 任务ID, 任务 in 任务列表.items():
                次数 = int(任务.get("次数", 0))
                已执行 = int(任务.get("已执行", 0))
                次数描述 = f"已执行 {已执行}/{次数}" if 次数 > 0 else f"已执行 {已执行} 次，不限次数"
                误差秒 = float(任务.get("误差秒", 默认随机误差秒) or 0)
                误差描述 = f"随机误差 ±{self.格式化时长(误差秒)}" if 误差秒 > 0 else "完全准点"
                下次 = self.data.解析任务时间(任务, "下次触发")
                下次描述 = f"{下次:%Y-%m-%d %H:%M}" if 下次 else "待计算"
                提醒描述 = self.任务提醒描述(任务)
                名称描述 = f"「{任务.get('名称', '')}」" if 任务.get("名称") else ""
                部分.append(
                    f"· [{任务ID}]{名称描述} cron「{任务.get('cron', '')}」，"
                    f"每次睡 {self.格式化时长(float(任务.get('时长秒', 0)))}，"
                    f"{次数描述}，{误差描述}{提醒描述}，下次触发 {下次描述}"
                )
        return "\n".join(部分)

    # ---------- 入睡执行 ----------

    def 执行入睡(self, umo: str, 生效时间: datetime, 醒来时间: datetime, 原因: str = "") -> bool:
        """落盘睡觉安排并排定时唤醒，返回是否为新安排的一觉。"""
        # 最短休息兜底（时长从生效时刻起算）
        醒来时间 = max(醒来时间, 生效时间 + timedelta(seconds=self.最短休息秒))
        # 统一截断到秒：持久化只存到秒，内存里的时间若带微秒，
        # 定时唤醒到点后的相等比对会永远失配、静默不醒
        生效时间 = 生效时间.replace(microsecond=0)
        醒来时间 = 醒来时间.replace(microsecond=0)
        新的一觉 = self.data.开始睡觉(
            umo=umo, 生效时间=生效时间, 醒来时间=醒来时间, 原因=原因,
        )
        self.安排唤醒任务(umo, 醒来时间)
        return 新的一觉

    # ---------- 定时任务调度 ----------

    def 计算任务下次触发(self, 任务: dict) -> datetime | None:
        """cron 下次触发时间 + 随机误差偏移，模拟真实作息。

        入睡偏移在计划下次触发时就掷好并落盘（每天各掷一次）；
        cron 无法解析抛 ValueError，永远不会触发返回 None。
        偏移后落到过去（刚建任务就到点 + 负偏移）按立即触发处理。
        """
        下次 = 计算下次触发(任务.get("cron", ""))
        if 下次 is None:
            return None
        误差秒 = float(任务.get("误差秒", 默认随机误差秒) or 0)
        if 误差秒 > 0:
            下次 += timedelta(seconds=random.uniform(-误差秒, 误差秒))
            now = datetime.now()
            if 下次 < now:
                下次 = now
        return 下次

    def 计算任务提醒时间(self, 下次触发: datetime) -> datetime | None:
        """睡前提醒时刻：入睡时刻（已含入睡随机偏移）前 N 分钟，再叠加提醒自己的随机偏移。

        提醒必须落在入睡之前，已经错过的立即提醒；关闭时返回 None。
        """
        if self.睡前提醒秒 <= 0:
            return None
        now = datetime.now()
        提醒 = 下次触发 - timedelta(seconds=self.睡前提醒秒)
        if self.睡前提醒误差秒 > 0:
            提醒 += timedelta(seconds=random.uniform(-self.睡前提醒误差秒, self.睡前提醒误差秒))
        # 偏移也不能把提醒推到入睡之后
        提醒 = min(提醒, 下次触发 - timedelta(seconds=30))
        if 提醒 < now:
            提醒 = now
        return 提醒

    def 安排调度循环(self, umo: str) -> None:
        """（重新）启动某会话定时任务的调度循环。"""
        self.取消调度循环(umo)
        循环 = asyncio.create_task(self.调度循环主体(umo))
        循环.add_done_callback(lambda t: self.调度循环.pop(umo, None))
        self.调度循环[umo] = 循环

    def 取消调度循环(self, umo: str) -> None:
        循环 = self.调度循环.pop(umo, None)
        if 循环 and not 循环.done():
            循环.cancel()

    async def 调度循环主体(self, umo: str) -> None:
        """等到最近的下次触发/提醒时间，到点处理后继续；任务增删会重启本循环。"""
        while True:
            到期入睡, 到期提醒 = self.整理任务时间(umo)
            for 任务ID in 到期提醒:
                self.发送睡前提醒(umo, 任务ID)
            for 任务ID in 到期入睡:
                self.触发睡觉任务(umo, 任务ID)
            任务列表 = self.data.获取任务列表(umo)
            if not 任务列表:
                return
            下次时间列表 = []
            for 任务 in 任务列表.values():
                if 下次 := self.data.解析任务时间(任务, "下次触发"):
                    下次时间列表.append(下次)
                if not int(任务.get("已提醒", 0)):
                    if 下次 := self.data.解析任务时间(任务, "下次提醒"):
                        下次时间列表.append(下次)
            if not 下次时间列表:
                return
            剩余 = (min(下次时间列表) - datetime.now()).total_seconds()
            if 剩余 > 0:
                await asyncio.sleep(剩余)

    def 整理任务时间(self, umo: str) -> tuple[list[str], list[str]]:
        """校正每个任务的下次触发/提醒时间；cron 失效或永不触发的任务直接移除。

        Returns:
            (到期待入睡的任务 ID 列表（含离线期间错过的）, 到期待提醒的任务 ID 列表)。
        """
        到期入睡 = []
        到期提醒 = []
        now = datetime.now()
        for 任务ID, 任务 in list(self.data.获取任务列表(umo).items()):
            下次触发 = self.data.解析任务时间(任务, "下次触发")
            if 下次触发 is None:
                try:
                    下次触发 = self.计算任务下次触发(任务)
                except ValueError as e:
                    logger.error(f"[睡觉] 移除 cron 失效的定时任务 {任务ID}（{umo}）：{e}")
                    self.data.删除任务(umo, 任务ID)
                    continue
                if 下次触发 is None:
                    logger.error(
                        f"[睡觉] 移除永不触发的定时任务 {任务ID}（{umo}）："
                        f"cron「{任务.get('cron', '')}」"
                    )
                    self.data.删除任务(umo, 任务ID)
                    continue
                self.data.更新任务下次触发(umo, 任务ID, 下次触发)
            if self.睡前提醒秒 <= 0:
                # 提醒关闭：清掉遗留的提醒计划
                if 任务.get("下次提醒"):
                    self.data.更新任务提醒(umo, 任务ID, None, 已提醒=False)
            elif not int(任务.get("已提醒", 0)):
                下次提醒 = self.data.解析任务时间(任务, "下次提醒")
                if 下次提醒 is None:
                    下次提醒 = self.计算任务提醒时间(下次触发)
                    self.data.更新任务提醒(umo, 任务ID, 下次提醒, 已提醒=False)
                # 已经过了入睡点的（如离线补触发）就不必再提醒了
                if 下次提醒 and 下次提醒 <= now and 下次触发 > now:
                    到期提醒.append(任务ID)
            if 下次触发 <= now:
                到期入睡.append(任务ID)
        return 到期入睡, 到期提醒

    def 发送睡前提醒(self, umo: str, 任务ID: str) -> None:
        """到点的睡前提醒：先标记已提醒再异步投递，失败不重试、不阻塞调度循环。"""
        任务 = self.data.获取任务列表(umo).get(任务ID)
        if not 任务:
            return
        if self.data.是在睡觉(umo):
            # 已经在睡（如顺延期间），提醒没有意义
            logger.info(f"[睡觉] 睡前提醒跳过：{umo} 已在睡觉中")
            self.data.更新任务提醒(umo, 任务ID, None, 已提醒=True)
            return
        信封 = 任务.get("信封") or {}
        if not 信封.get("平台ID"):
            # 旧版本任务没存会话信息，无法投递
            logger.warning(f"[睡觉] 睡前提醒跳过：任务 {任务ID}（{umo}）缺少会话信息")
            self.data.更新任务提醒(umo, 任务ID, None, 已提醒=True)
            return
        self.data.更新任务提醒(umo, 任务ID, None, 已提醒=True)
        asyncio.create_task(self.投递睡前提醒(umo, 任务))
        logger.info(f"[睡觉] 触发睡前提醒：{umo}，任务 {任务ID}")

    async def 投递睡前提醒(self, umo: str, 任务: dict) -> None:
        """把睡前提醒构造为合成消息入事件队列，走完整管线请求 LLM。

        插件 initialize 早于平台适配器实例化，先等平台就绪（同补唤醒）。
        """
        信封 = 任务.get("信封") or {}
        for _ in range(120):
            if self.context.get_platform_inst(信封.get("平台ID", "")):
                break
            await asyncio.sleep(0.5)
        try:
            平台 = self.context.get_platform_inst(信封.get("平台ID", ""))
            if 平台 is None:
                raise RuntimeError(f"平台 {信封.get('平台ID')} 不存在")
            下次触发 = self.data.解析任务时间(任务, "下次触发")
            剩余秒 = (下次触发 - datetime.now()).total_seconds() if 下次触发 else 0
            剩余分钟 = max(1, int((剩余秒 + 59) // 60))
            文本 = self.睡前提醒模板.replace("{n}", str(剩余分钟))
            # 配置里只写正文；配置若自带 <system_reminder> 包装则不重复添加
            if "<system_reminder>" not in 文本:
                文本 = f"<system_reminder>{文本}</system_reminder>"
            abm = self.构造合成消息对象(信封, 文本)
            self.context.get_event_queue().put_nowait(平台.create_event(abm))
            logger.info(f"[睡觉] 已向 {umo} 投递睡前提醒（约 {剩余分钟} 分钟后入睡）")
        except Exception as e:
            logger.error(f"[睡觉] 发送睡前提醒失败：{e}")

    def 触发睡觉任务(self, umo: str, 任务ID: str) -> None:
        """执行一次定时任务：立即入睡（睡觉中则顺延醒来时间），并累计次数。"""
        任务 = self.data.获取任务列表(umo).get(任务ID)
        if not 任务:
            return
        now = datetime.now()
        醒来时间 = now + timedelta(seconds=float(任务.get("时长秒") or 0))
        误差秒 = float(任务.get("误差秒", 默认随机误差秒) or 0)
        if 误差秒 > 0:
            # 醒来时刻独立掷一次随机偏移，与入睡时刻的偏移互不影响；
            # 偏移成负数时由 执行入睡 的最短休息兜底，不会睡出负时长
            醒来时间 += timedelta(seconds=random.uniform(-误差秒, 误差秒))
        if self.data.是在睡觉(umo):
            当前醒来时间 = self.data.获取醒来时间(umo)
            if 当前醒来时间 and 当前醒来时间 < 醒来时间:
                self.data.顺延醒来时间(umo, 醒来时间)
                logger.info(
                    f"[睡觉] 定时任务触发：{umo} 睡觉中，醒来时间顺延到 {醒来时间:%Y-%m-%d %H:%M}"
                )
            else:
                logger.info(f"[睡觉] 定时任务触发：{umo} 睡觉中且醒得更晚，本次触发跳过")
        else:
            self.执行入睡(umo, 生效时间=now, 醒来时间=醒来时间, 原因=f"定时任务 {任务.get('cron', '')}")
            logger.info(
                f"[睡觉] 定时任务触发：{umo} 自动入睡，将于 {醒来时间:%Y-%m-%d %H:%M} 醒来"
            )
        self.data.任务触发一次(umo, 任务ID)

        任务 = self.data.获取任务列表(umo).get(任务ID)
        if 任务 is None:
            return
        次数 = int(任务.get("次数", 0))
        if 0 < 次数 <= int(任务.get("已执行", 0)):
            self.data.删除任务(umo, 任务ID)
            logger.info(f"[睡觉] 定时任务 {任务ID}（{umo}）已完成全部 {次数} 次，自动结束")
            return
        try:
            下次 = self.计算任务下次触发(任务)
        except ValueError as e:
            logger.error(f"[睡觉] 定时任务 {任务ID}（{umo}）cron 失效，已移除：{e}")
            self.data.删除任务(umo, 任务ID)
            return
        if 下次 is None:
            logger.error(f"[睡觉] 定时任务 {任务ID}（{umo}）的 cron 已不会再触发，自动结束")
            self.data.删除任务(umo, 任务ID)
            return
        self.data.更新任务下次触发(umo, 任务ID, 下次)
        # 新周期重置睡前提醒
        self.data.更新任务提醒(umo, 任务ID, self.计算任务提醒时间(下次), 已提醒=False)

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
            单会话上限=self.暂存消息上限,
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

    def 构造合成消息对象(
        self, 信封: dict, 文本: str, 发送者ID: str = "", 昵称: str | None = None,
    ) -> AstrBotMessage:
        """用给定文本构造一条新的会话消息，走完整 pipeline 请求 LLM 并回复。

        醒来汇总与睡前提醒共用。
        """
        群聊 = 信封.get("会话类型") == "群聊"
        自身ID = 信封.get("自身ID", "")

        abm = AstrBotMessage()
        abm.type = MessageType.GROUP_MESSAGE if 群聊 else MessageType.FRIEND_MESSAGE
        abm.self_id = 自身ID
        # aiocqhttp 的发送路由：群聊取 get_group_id()（abm.group），私聊取 get_sender_id()。
        # 私聊未指明发送者时必须用会话对端（会话ID 即好友 QQ），否则回复发不出去
        if 发送者ID:
            发送者 = str(发送者ID)
        elif not 群聊:
            发送者 = str(信封.get("会话ID", ""))
        else:
            发送者 = ""
        abm.sender = MessageMember(user_id=发送者, nickname=昵称)
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

    def 构造醒来消息对象(self, 信封: dict, 消息列表: list[dict]) -> AstrBotMessage:
        """把暂存消息合并后构造会话消息。"""
        文本 = self.合并暂存文本(信封, 消息列表)
        第一条 = 消息列表[0]
        return self.构造合成消息对象(
            信封, 文本,
            发送者ID=第一条.get("发送者ID", ""),
            昵称=第一条.get("昵称") or None,
        )

    # ---------- 管理指令 ----------

    @filter.command("睡觉状态", alias={"睡了吗"})
    async def 睡觉状态(self, event: AstrMessageEvent, 会话: str = ""):
        """查看各会话的睡觉安排、定时任务与暂存消息数。指令：/睡觉状态 [会话ID]"""
        安排 = self.data.获取全部睡觉安排()
        全部会话 = sorted(set(安排) | set(self.data.获取全部任务会话()))
        会话 = 会话.strip()
        if 会话:
            全部会话 = [umo for umo in 全部会话 if 会话 == umo or 会话 in umo]
            if not 全部会话:
                yield event.plain_result(
                    f"没有匹配「{会话}」的会话，可填完整会话 ID 或其中一段"
                )
                return
        if not 全部会话:
            yield event.plain_result("现在所有会话都是清醒状态，也没有定时睡觉任务～")
            return
        行列表 = ["😴 睡觉情况："]
        本会话 = event.unified_msg_origin
        for umo in 全部会话:
            标记 = "（本会话）" if umo == 本会话 else ""
            if umo in 安排:
                醒来时间 = 安排[umo]
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
            else:
                行列表.append(f"· {umo}{标记}：清醒")
            for 任务ID, 任务 in self.data.获取任务列表(umo).items():
                次数 = int(任务.get("次数", 0))
                已执行 = int(任务.get("已执行", 0))
                次数描述 = f"{已执行}/{次数}" if 次数 > 0 else f"{已执行}（不限）"
                误差秒 = float(任务.get("误差秒", 默认随机误差秒) or 0)
                误差描述 = f"±{self.格式化时长(误差秒)}" if 误差秒 > 0 else "准点"
                下次 = self.data.解析任务时间(任务, "下次触发")
                下次描述 = f"{下次:%m-%d %H:%M}" if 下次 else "待计算"
                名称描述 = f"「{任务.get('名称', '')}」" if 任务.get("名称") else ""
                行列表.append(
                    f"    ◦ [{任务ID}]{名称描述} {任务.get('cron', '')} 每次睡 "
                    f"{self.格式化时长(float(任务.get('时长秒', 0)))}，误差 {误差描述}，"
                    f"已执行 {次数描述}{self.任务提醒描述(任务)}，下次 {下次描述}"
                )
        yield event.plain_result("\n".join(行列表))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("起床")
    async def 起床(self, event: AstrMessageEvent, 目标: str = ""):
        """管理员立刻叫醒会话，暂存的消息会立刻交给 LLM 汇总回复（定时任务保留）。指令：/起床 [会话ID/全部]"""
        目标 = 目标.strip()
        if 目标 == "全部":
            安排 = list(self.data.获取全部睡觉安排())
            if not 安排:
                yield event.plain_result("所有会话都没在睡～")
                return
            for umo in 安排:
                await self.执行唤醒(umo)
            yield event.plain_result(f"已叫醒 {len(安排)} 个会话。")
            return
        if 目标:
            umos, 错误 = self.解析会话目标(目标)
            if 错误:
                yield event.plain_result(错误)
                return
            for umo in umos:
                await self.执行唤醒(umo)
            yield event.plain_result(
                f"已叫醒 {len(umos)} 个会话（{'；'.join(umos)}）。"
            )
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

    def 解析会话目标(self, 文本: str) -> tuple[list[str], str]:
        """把命令参数解析成目标会话 umo 列表，支持完整 umo 或能唯一匹配的子串。

        Returns:
            (umo 列表, 错误说明)；解析成功时错误说明为空。
        """
        文本 = (文本 or "").strip()
        if not 文本:
            return [], "请提供会话 ID 或任务 ID"
        全部 = sorted(set(self.data.获取全部睡觉安排()) | set(self.data.获取全部任务会话()))
        精确 = [umo for umo in 全部 if umo == 文本]
        if 精确:
            return 精确, ""
        子串 = [umo for umo in 全部 if 文本 in umo]
        if len(子串) == 1:
            return 子串, ""
        if 子串:
            return [], "匹配到多个会话，请用更长的 ID：" + "；".join(子串)
        return [], f"没有找到会话或任务「{文本}」，可用 /睡了吗 查看现有会话"

    def 取消会话睡觉(self, umo: str) -> str:
        """直接取消某会话的睡觉安排与全部定时任务；正在睡觉则直接结束，暂存消息不发给 LLM。"""
        状态文本 = []
        if self.data.有睡觉安排(umo):
            在睡 = self.data.是在睡觉(umo)
            暂存数 = len(self.data.获取暂存(umo).get("消息", []))
            self.data.取走暂存(umo)  # 结束睡觉安排，暂存消息一并丢弃
            self.取消唤醒任务(umo)
            if 在睡:
                状态文本.append(f"已直接结束睡觉（丢弃 {暂存数} 条暂存消息）")
            else:
                状态文本.append("已取消还没生效的睡觉安排")
        被删 = self.data.删除任务(umo)
        if 被删:
            if not self.data.获取任务列表(umo):
                self.取消调度循环(umo)
            状态文本.append(f"已取消 {len(被删)} 个定时任务")
        if not 状态文本:
            return "该会话没有睡觉安排，也没有定时任务。"
        return "，".join(状态文本) + "。"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消睡觉")
    async def 取消睡觉(self, event: AstrMessageEvent, 目标: str = ""):
        """管理员直接取消睡觉安排或睡觉任务（暂存消息不发 LLM；要保留请用 /起床）。指令：/取消睡觉 [会话ID/任务ID]"""
        目标 = 目标.strip()
        if not 目标:
            yield event.plain_result(self.取消会话睡觉(event.unified_msg_origin))
            return
        # 先按任务 ID 找（任务 ID 全局唯一）
        所在会话 = self.data.查找任务会话(目标)
        if 所在会话:
            for umo in 所在会话:
                self.data.删除任务(umo, 目标)
                if not self.data.获取任务列表(umo):
                    self.取消调度循环(umo)
            yield event.plain_result(
                f"已取消任务 {目标}（{'；'.join(所在会话)}）。"
            )
            return
        umos, 错误 = self.解析会话目标(目标)
        if 错误:
            yield event.plain_result(错误)
            return
        yield event.plain_result("\n".join(f"{umo}：{self.取消会话睡觉(umo)}" for umo in umos))
