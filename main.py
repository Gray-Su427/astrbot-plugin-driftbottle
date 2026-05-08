import asyncio
import json
from datetime import datetime
from pathlib import Path
from random import choice
from typing import Any
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot-plugin-driftbottle"
KV_STATE_KEY = "driftbottle_state"
BOTTLES_FILE_NAME = "bottles.json"


# ----------------------------
# 工具函数（复用自盲盒插件）
# ----------------------------


def _resolve_data_root() -> Path:
    """解析插件数据存储根目录。"""
    current_dir = Path(__file__).resolve().parent
    for ancestor in [current_dir, *current_dir.parents]:
        data_dir = ancestor / "data"
        if data_dir.is_dir():
            return data_dir / "plugins" / PLUGIN_NAME
    return current_dir / "data" / "plugins" / PLUGIN_NAME


DATA_ROOT_DIR = _resolve_data_root()


def _safe_json_dump(path: Path, data: object) -> None:
    """安全地将数据写入 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_json_load(path: Path, default: object) -> object:
    """安全地从 JSON 文件读取数据。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _get_sender_id(event: AstrMessageEvent) -> str:
    """获取发送者 ID（兼容多平台适配器）。"""
    sender = getattr(getattr(event, "message_obj", None), "sender", None)
    for attr in ("user_id", "qq", "id", "uid"):
        value = getattr(sender, attr, None)
        if value is not None:
            return str(value)
    raise ValueError("无法获取发送者 ID。")


# ----------------------------
# AstrBot 插件主体
# ----------------------------


@register(PLUGIN_NAME, "Gray-Su", "匿名情绪漂流瓶——私聊投递，群内捞取", "1.0.0")
class DriftBottlePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._lock = asyncio.Lock()
        self._bottles: list[dict[str, Any]] = []
        self._bottles_loaded = False

    async def initialize(self):
        """插件初始化，加载漂流瓶数据。"""
        logger.info("driftbottle plugin initialized")
        await self._load_bottles()

    # ----------------------------
    # 数据读写
    # ----------------------------

    def _bottles_file_path(self) -> Path:
        return DATA_ROOT_DIR / BOTTLES_FILE_NAME

    async def _load_bottles(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载漂流瓶数据。"""
        async with self._lock:
            raw = _safe_json_load(self._bottles_file_path(), [])
            if not isinstance(raw, list):
                raw = []
            self._bottles = [
                b for b in raw if isinstance(b, dict) and b.get("id") and b.get("content")
            ]
            self._bottles_loaded = True
            _safe_json_dump(self._bottles_file_path(), self._bottles)
            return self._bottles

    async def _save_bottles(self) -> None:
        """将漂流瓶数据写入 JSON 文件。"""
        async with self._lock:
            _safe_json_dump(self._bottles_file_path(), self._bottles)

    async def _get_bottles(self) -> list[dict[str, Any]]:
        """获取漂流瓶列表（懒加载）。"""
        if not self._bottles_loaded:
            return await self._load_bottles()
        return self._bottles

    def _get_floating_bottles(self, exclude_sender_id: str = "") -> list[dict[str, Any]]:
        """获取漂流中的瓶子，可排除指定发送者。"""
        return [
            b
            for b in self._bottles
            if b.get("status") == "floating" and b.get("sender_id") != exclude_sender_id
        ]

    # ----------------------------
    # 指令：投瓶（私聊）
    # ----------------------------

    @filter.command("投瓶")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def throw_bottle(self, event: AstrMessageEvent):
        """私聊投入一张匿名小纸条。用法：/投瓶 <内容>"""
        content = event.message_str.strip()
        # 去掉可能的指令前缀残留
        for prefix in ("/投瓶", "/投瓶 "):
            if content.startswith(prefix):
                content = content[len(prefix):].strip()
                break

        if not content:
            yield event.plain_result("请输入纸条内容～\n用法：/投瓶 <你想说的话>")
            return

        if len(content) > 500:
            yield event.plain_result("纸条内容太长啦，请控制在 500 字以内～")
            return

        try:
            sender_id = _get_sender_id(event)
        except ValueError:
            yield event.plain_result("无法识别你的身份，请稍后再试。")
            return

        bottle = {
            "id": uuid4().hex[:8],
            "sender_id": sender_id,
            "content": content,
            "created_at": _timestamp(),
            "status": "floating",
            "read_at": None,
        }

        bottles = await self._get_bottles()
        bottles.append(bottle)
        await self._save_bottles()

        yield event.plain_result(
            "🫧 你的小纸条已投入瓶中，\n"
            "它会漂向远方，被温柔地拾起。\n\n"
            f"纸条编号：{bottle['id']}\n"
            "（请记住编号，以便后续管理）"
        )

    # ----------------------------
    # 指令：捞瓶（群聊）
    # ----------------------------

    @filter.command("捞瓶")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def pick_bottle(self, event: AstrMessageEvent):
        """随机捞出一张匿名小纸条。"""
        try:
            sender_id = _get_sender_id(event)
        except ValueError:
            yield event.plain_result("无法识别你的身份，请稍后再试。")
            return

        floating = self._get_floating_bottles(exclude_sender_id=sender_id)
        if not floating:
            yield event.plain_result("🫧 瓶海空空如也，暂时没有可以捞的纸条～\n去私聊机器人 /投瓶 投一张吧！")
            return

        bottle = choice(floating)
        bottle["status"] = "read"
        bottle["read_at"] = _timestamp()
        await self._save_bottles()

        yield event.plain_result(
            "🫧 捞到了一张小纸条：\n\n"
            f"「{bottle['content']}」\n\n"
            "——来自某位同学"
        )

    # ----------------------------
    # 指令：开箱（群聊）
    # ----------------------------

    @filter.command("开箱")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def open_box(self, event: AstrMessageEvent):
        """一次性捞出所有漂流中的纸条，逐条展示（适合每周活动使用）。"""
        floating = self._get_floating_bottles()
        if not floating:
            yield event.plain_result("📦 箱子里空空的，没有纸条～\n去私聊机器人 /投瓶 投一张吧！")
            return

        now = _timestamp()
        lines = [f"📦 打开了漂流瓶箱，共 {len(floating)} 张小纸条：\n"]

        for i, bottle in enumerate(floating, 1):
            bottle["status"] = "read"
            bottle["read_at"] = now
            lines.append(f"{i}.「{bottle['content']}」")
            lines.append("  ——来自某位同学")
            lines.append("")

        await self._save_bottles()

        lines.append("所有纸条已读出，箱子里又空了～")
        lines.append("下周继续投递吧！私聊机器人 /投瓶 即可投递。")

        yield event.plain_result("\n".join(lines))

    # ----------------------------
    # 指令：瓶海（群聊）
    # ----------------------------

    @filter.command("瓶海")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def bottle_sea(self, event: AstrMessageEvent):
        """查看当前漂流瓶数量统计。"""
        bottles = await self._get_bottles()
        floating = sum(1 for b in bottles if b.get("status") == "floating")
        read = sum(1 for b in bottles if b.get("status") == "read")
        total = len(bottles)

        yield event.plain_result(
            f"🫧 瓶海统计：\n"
            f"  漂流中：{floating} 张\n"
            f"  已读出：{read} 张\n"
            f"  总计：{total} 张\n\n"
            f"私聊机器人 /投瓶 可以投递纸条哦～"
        )

    # ----------------------------
    # 指令：我的瓶子（私聊）
    # ----------------------------

    @filter.command("我的瓶子")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def my_bottles(self, event: AstrMessageEvent):
        """查看自己投入且仍在漂流中的瓶子。"""
        try:
            sender_id = _get_sender_id(event)
        except ValueError:
            yield event.plain_result("无法识别你的身份，请稍后再试。")
            return

        bottles = await self._get_bottles()
        my_floating = [
            b for b in bottles if b.get("sender_id") == sender_id and b.get("status") == "floating"
        ]

        if not my_floating:
            yield event.plain_result("🫧 你目前没有漂流中的纸条～\n私聊机器人 /投瓶 可以投递纸条哦！")
            return

        lines = [f"🫧 你有 {len(my_floating)} 张纸条在漂流中：\n"]
        for bottle in my_floating:
            preview = bottle["content"][:30] + "..." if len(bottle["content"]) > 30 else bottle["content"]
            lines.append(f"  编号 {bottle['id']}：{preview}")
            lines.append(f"  投入时间：{bottle['created_at']}")
            lines.append("")

        yield event.plain_result("\n".join(lines).strip())

    # ----------------------------
    # 指令：收回（私聊）
    # ----------------------------

    @filter.command("收回")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def recall_bottle(self, event: AstrMessageEvent):
        """收回自己仍在漂流中的纸条。用法：/收回 <编号>"""
        bottle_id = event.message_str.strip()
        for prefix in ("/收回", "/收回 "):
            if bottle_id.startswith(prefix):
                bottle_id = bottle_id[len(prefix):].strip()
                break

        if not bottle_id:
            yield event.plain_result("请输入要收回的纸条编号～\n用法：/收回 <编号>\n可用 /我的瓶子 查看编号")
            return

        try:
            sender_id = _get_sender_id(event)
        except ValueError:
            yield event.plain_result("无法识别你的身份，请稍后再试。")
            return

        bottles = await self._get_bottles()
        target = None
        for bottle in bottles:
            if (
                bottle.get("id") == bottle_id
                and bottle.get("sender_id") == sender_id
                and bottle.get("status") == "floating"
            ):
                target = bottle
                break

        if not target:
            yield event.plain_result(
                f"找不到编号为 {bottle_id} 且属于你的漂流中纸条。\n"
                "请检查编号是否正确，或用 /我的瓶子 查看你的纸条。"
            )
            return

        target["status"] = "recalled"
        await self._save_bottles()

        yield event.plain_result(
            f"🫧 已收回编号 {bottle_id} 的纸条。\n"
            "这张纸条不会被任何人看到。"
        )

    async def terminate(self):
        """插件销毁时的清理工作。"""
        if self._bottles_loaded:
            await self._save_bottles()