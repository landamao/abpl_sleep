"""睡觉插件的持久化数据：各会话的睡觉状态 + 暂存消息 + 定时睡觉任务。

数据按会话（unified_msg_origin，umo）独立：每个会话有各自的人格作息，
A 会话在睡觉不影响 B 会话正常回复。暂存消息同样按会话分组，
醒来后逐会话汇总回复。

落盘格式顶层以 umo 为键，一个会话的全部数据都收在自己的条目下：

{
  "umo": {
    "睡觉状态": {"生效时间": iso, "入睡时间": iso, "醒来时间": iso, "原因": str},
    "暂存消息": {"平台ID", "会话类型", "会话ID", "自身ID", "群名", "消息": [{发送者ID,昵称,文本,时间}]},
    "睡觉任务": {
      "任务ID": {
        "名称": str,            # 任务名/备注，可空
        "cron": str,            # 标准 5 位 cron 表达式（分 时 日 月 周）
        "时长秒": float,         # 每次触发后的休息时长
        "次数": int,             # 总执行次数，0 表示不限
        "误差秒": float,         # 入睡/醒来的随机偏移幅度，0 表示完全准点
        "已执行": int,
        "下次触发": iso,         # 已含入睡随机偏移，空 string 表示待计算
        "下次提醒": iso,         # 睡前提醒时刻（已含随机偏移），空 string 表示无
        "已提醒": int,           # 本周期睡前提醒是否已发
        "信封": {...},           # 创建任务时的会话信息，用于投递睡前提醒
        "创建时间": iso,
        "原因": str
      }
    }
  }
}

睡觉状态带「生效时间」：开始时间在未来的休息安排会提前落盘，
但生效时间到点前不算在睡觉（消息照常回复），到点后自动视为在睡。
定时睡觉任务按 cron 到点触发一次入睡，「下次触发」落盘以便重启恢复；
离线期间错过的触发点，启动后由调度循环立即补触发。
"""

import json
from datetime import datetime
from pathlib import Path

状态文件名 = "睡觉状态.json"

时间格式 = "%Y-%m-%dT%H:%M:%S"


class Data:
    def __init__(self, data_dir):
        self.状态文件 = Path(data_dir) / 状态文件名
        self.状态文件.parent.mkdir(parents=True, exist_ok=True)
        # umo -> {"睡觉状态": {...}, "暂存消息": {...}, "睡觉任务": {...}}
        self.会话数据: dict[str, dict] = {}
        self.加载()

    # ---------- 读写 ----------

    def 加载(self) -> None:
        if not self.状态文件.is_file():
            return
        try:
            内容 = json.loads(self.状态文件.read_text(encoding="utf-8"))
            self.会话数据 = (
                {k: v for k, v in 内容.items() if isinstance(v, dict)}
                if isinstance(内容, dict)
                else {}
            )
        except Exception:
            # 数据坏了宁可当没睡过，也别让插件反复报错
            self.会话数据 = {}

    def 保存(self) -> None:
        try:
            self.状态文件.write_text(
                json.dumps(self.会话数据, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ---------- 单个会话数据的取用 ----------

    def 获取睡觉状态(self, umo: str) -> dict:
        return self.会话数据.get(umo, {}).get("睡觉状态") or {}

    def 获取暂存(self, umo: str) -> dict:
        return self.会话数据.get(umo, {}).get("暂存消息") or {}

    # ---------- 睡觉状态（按会话） ----------

    def 有睡觉安排(self, umo: str) -> bool:
        """该会话存在休息安排（含还没到开始时间的）。"""
        return bool(self.获取睡觉状态(umo))

    def 是在睡觉(self, umo: str) -> bool:
        """该会话的休息已生效、正处于睡觉中（生效时间已到）。"""
        状态 = self.获取睡觉状态(umo)
        if not 状态:
            return False
        try:
            生效时间 = datetime.strptime(状态["生效时间"], 时间格式)
        except Exception:
            return True
        return 生效时间 <= datetime.now()

    def 获取醒来时间(self, umo: str) -> datetime | None:
        状态 = self.获取睡觉状态(umo)
        if not 状态:
            return None
        try:
            return datetime.strptime(状态["醒来时间"], 时间格式)
        except Exception:
            return None

    def 获取生效时间(self, umo: str) -> datetime | None:
        状态 = self.获取睡觉状态(umo)
        if not 状态:
            return None
        try:
            return datetime.strptime(状态["生效时间"], 时间格式)
        except Exception:
            return None

    def 获取全部睡觉安排(self) -> dict[str, datetime]:
        """所有休息安排（含未生效的）的 umo -> 醒来时间。"""
        结果 = {}
        for umo in list(self.会话数据):
            醒来时间 = self.获取醒来时间(umo)
            if 醒来时间:
                结果[umo] = 醒来时间
        return 结果

    def 开始睡觉(
        self, umo: str, 生效时间: datetime, 醒来时间: datetime, 原因: str = ""
    ) -> bool:
        """为某个会话安排休息（立即入睡时生效时间即当前时刻）。

        Returns:
            是否为新安排的一觉（False 表示该会话原本就有安排，这次是调整）。
        """
        新的一觉 = not self.有睡觉安排(umo)
        self.会话数据.setdefault(umo, {})["睡觉状态"] = {
            "生效时间": 生效时间.strftime(时间格式),
            "入睡时间": 生效时间.strftime(时间格式),
            "醒来时间": 醒来时间.strftime(时间格式),
            "原因": (原因 or "")[:200],
        }
        self.保存()
        return 新的一觉

    def 顺延醒来时间(self, umo: str, 新醒来时间: datetime) -> bool:
        """睡觉中把醒来时间往后调整（定时任务触发时使用）。

        注意：返回的是存储里的活引用，直接改它即落盘对象本身。
        """
        状态 = self.会话数据.get(umo, {}).get("睡觉状态")
        if not 状态:
            return False
        状态["醒来时间"] = 新醒来时间.strftime(时间格式)
        self.保存()
        return True

    def 结束睡觉(self, umo: str) -> None:
        会话 = self.会话数据.get(umo)
        if 会话 is None:
            return
        会话.pop("睡觉状态", None)
        if not 会话:
            # 睡觉状态和暂存消息都没了，整个会话条目也一并清掉
            self.会话数据.pop(umo, None)
        self.保存()

    # ---------- 定时睡觉任务（按会话） ----------

    def 获取任务列表(self, umo: str) -> dict[str, dict]:
        """该会话的全部定时睡觉任务：任务ID -> 任务。"""
        return self.会话数据.get(umo, {}).get("睡觉任务") or {}

    def 获取全部任务会话(self) -> list[str]:
        """存在定时睡觉任务的会话 umo 列表。"""
        return [
            umo
            for umo, 会话 in self.会话数据.items()
            if isinstance(会话, dict) and 会话.get("睡觉任务")
        ]

    def 查找任务会话(self, 任务ID: str) -> list[str]:
        """按任务 ID 找所在会话（任务 ID 全局唯一，正常最多一个）。"""
        return [
            umo
            for umo in self.获取全部任务会话()
            if 任务ID in self.获取任务列表(umo)
        ]

    def 添加任务(
        self,
        umo: str,
        任务ID: str,
        cron: str,
        时长秒: float,
        次数: int,
        误差秒: float,
        下次触发: datetime,
        信封: dict | None = None,
        下次提醒: datetime | None = None,
        名称: str = "",
        原因: str = "",
    ) -> None:
        """新增一个定时睡觉任务。

        次数 0 表示不限次数，误差秒 0 表示完全准点；
        信封用于睡前提醒投递，下次提醒为 None 表示不提醒。
        """
        会话 = self.会话数据.setdefault(umo, {})
        任务列表 = 会话.setdefault("睡觉任务", {})
        任务列表[任务ID] = {
            "名称": (名称 or "")[:50],
            "cron": cron,
            "时长秒": float(时长秒),
            "次数": int(次数),
            "误差秒": float(误差秒),
            "已执行": 0,
            "下次触发": 下次触发.strftime(时间格式),
            "下次提醒": 下次提醒.strftime(时间格式) if 下次提醒 else "",
            "已提醒": 0,
            "信封": 信封 or {},
            "创建时间": datetime.now().strftime(时间格式),
            "原因": (原因 or "")[:200],
        }
        self.保存()

    def 删除任务(self, umo: str, 任务ID: str | None = None) -> list[dict]:
        """删除指定任务；任务ID 为 None 时删除该会话全部任务。

        Returns:
            被删除的任务内容列表（用于回复展示），没有删到任何东西时为空。
        """
        会话 = self.会话数据.get(umo)
        if 会话 is None:
            return []
        任务列表 = 会话.get("睡觉任务") or {}
        if 任务ID is None:
            被删 = list(任务列表.values())
            会话.pop("睡觉任务", None)
        else:
            任务 = 任务列表.pop(任务ID, None)
            被删 = [任务] if 任务 else []
            if not 任务列表:
                会话.pop("睡觉任务", None)
        if not 会话:
            self.会话数据.pop(umo, None)
        self.保存()
        return 被删

    def 任务触发一次(self, umo: str, 任务ID: str) -> None:
        """任务被执行过一次，累计已执行次数。"""
        任务 = (self.会话数据.get(umo) or {}).get("睡觉任务", {}).get(任务ID)
        if not 任务:
            return
        任务["已执行"] = int(任务.get("已执行", 0)) + 1
        self.保存()

    def 更新任务下次触发(self, umo: str, 任务ID: str, 下次触发: datetime) -> None:
        任务 = (self.会话数据.get(umo) or {}).get("睡觉任务", {}).get(任务ID)
        if not 任务:
            return
        任务["下次触发"] = 下次触发.strftime(时间格式)
        self.保存()

    def 更新任务提醒(
        self, umo: str, 任务ID: str, 下次提醒: datetime | None, 已提醒: bool,
    ) -> None:
        """更新任务的睡前提醒计划；下次提醒为 None 时清空（关闭/已发）。"""
        任务 = (self.会话数据.get(umo) or {}).get("睡觉任务", {}).get(任务ID)
        if not 任务:
            return
        任务["下次提醒"] = 下次提醒.strftime(时间格式) if 下次提醒 else ""
        任务["已提醒"] = 1 if 已提醒 else 0
        self.保存()

    @staticmethod
    def 解析任务时间(任务: dict, 键: str) -> datetime | None:
        """从任务里解析一个 iso 时间字段，缺失或损坏返回 None。"""
        文本 = (任务 or {}).get(键) or ""
        if not 文本:
            return None
        try:
            return datetime.strptime(文本, 时间格式)
        except ValueError:
            return None

    # ---------- 暂存消息 ----------

    def 暂存一条消息(
        self,
        umo: str,
        信封: dict,
        消息: dict,
        单会话上限: int = 100,
    ) -> None:
        """把一条消息挂到对应会话的暂存列表末尾，超出上限时丢弃最旧的。"""
        会话 = self.会话数据.setdefault(umo, {})
        暂存 = 会话.setdefault("暂存消息", {**信封, "消息": []})
        消息列表: list = 暂存.setdefault("消息", [])
        消息列表.append(消息)
        超出 = len(消息列表) - 单会话上限
        if 超出 > 0:
            del 消息列表[:超出]
        self.保存()

    def 取走暂存(self, umo: str) -> dict | None:
        """结束某会话的睡觉安排并取走其暂存内容，没有暂存时返回 None。"""
        会话 = self.会话数据.get(umo)
        信封 = 会话.pop("暂存消息", None) if 会话 else None
        self.结束睡觉(umo)
        return 信封
