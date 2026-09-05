"""睡觉插件的持久化数据：各会话的睡觉状态 + 暂存消息。

数据按会话（unified_msg_origin，umo）独立：每个会话有各自的人格作息，
A 会话在睡觉不影响 B 会话正常回复。暂存消息同样按会话分组，
醒来后逐会话汇总回复。

落盘格式顶层以 umo 为键，一个会话的全部数据都收在自己的条目下：

{
  "umo": {
    "睡觉状态": {"生效时间": iso, "入睡时间": iso, "醒来时间": iso, "原因": str},
    "暂存消息": {"平台ID", "会话类型", "会话ID", "自身ID", "群名", "消息": [{发送者ID,昵称,文本,时间}]}
  }
}

睡觉状态带「生效时间」：开始时间在未来的休息安排会提前落盘，
但生效时间到点前不算在睡觉（消息照常回复），到点后自动视为在睡。
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
        # umo -> {"睡觉状态": {...}, "暂存消息": {...}}
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

    def 结束睡觉(self, umo: str) -> None:
        会话 = self.会话数据.get(umo)
        if 会话 is None:
            return
        会话.pop("睡觉状态", None)
        if not 会话:
            # 睡觉状态和暂存消息都没了，整个会话条目也一并清掉
            self.会话数据.pop(umo, None)
        self.保存()

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
