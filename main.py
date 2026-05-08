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
BLINDBOX_PLUGIN_NAME = "astrbot_plugin_blindbox"


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


def _default_data() -> dict[str, Any]:
    """返回默认的数据结构。"""
    return {"public": [], "groups": {}}


# ----------------------------
# AstrBot 插件主体
# ----------------------------


@register(PLUGIN_NAME, "Gray-Su", "匿名情绪漂流瓶——私聊投递，群内捞取", "2.0.0")
class DriftBottlePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = _default_data()
        self._data_loaded = False
        self._member_to_group: dict[str, str] = {}  # sender_id -> group_name 缓存

    async def initialize(self):
        """插件初始化，加载漂流瓶数据和小组映射。"""
        logger.info("driftbottle plugin initialized")
        await self._load_data()
        await self._load_member_to_group()

    # ----------------------------
    # 数据读写
    # ----------------------------

    def _bottles_file_path(self) -> Path:
        return DATA_ROOT_DIR / BOTTLES_FILE_NAME

    async def _load_data(self) -> dict[str, Any]:
        """从 JSON 文件加载漂流瓶数据（双池结构）。"""
        async with self._lock:
            raw = _safe_json_load(self._bottles_file_path(), None)
            if isinstance(raw, dict) and "public" in raw:
                # 新格式：{"public": [...], "groups": {"组名": [...]}}
                self._data = raw
            elif isinstance(raw, list):
                # 兼容旧格式（单列表），迁移到新格式
                self._data = {"public": raw, "groups": {}}
                _safe_json_dump(self._bottles_file_path(), self._data)
            else:
                self._data = _default_data()
            self._data_loaded = True
            _safe_json_dump(self._bottles_file_path(), self._data)
            return self._data

    async def _save_data(self) -> None:
        """将漂流瓶数据写入 JSON 文件。"""
        async with self._lock:
            _safe_json_dump(self._bottles_file_path(), self._data)

    async def _get_data(self) -> dict[str, Any]:
        """获取数据（懒加载）。"""
        if not self._data_loaded:
            return await self._load_data()
        return self._data

    # ----------------------------
    # 小组映射管理
    # ----------------------------

    async def _load_member_to_group(self) -> None:
        """加载用户→小组映射：优先从盲盒插件读取，备用从 KV 缓存读取。"""
        # 1. 尝试从盲盒插件读取
        blindbox_meta = self.context.get_registered_star(BLINDBOX_PLUGIN_NAME)
        if blindbox_meta:
            try:
                # 盲盒插件的状态存储在 KV 中，key 为 "blindbox_state"
                blindbox_state = await self.get_kv_data("blindbox_state", None)
                if isinstance(blindbox_state, dict):
                    member_to_group = blindbox_state.get("member_to_group", {})
                    if isinstance(member_to_group, dict):
                        self._member_to_group.update(member_to_group)
                        logger.info(f"从盲盒插件加载了 {len(member_to_group)} 条小组映射")
            except Exception as e:
                logger.warning(f"从盲盒插件读取小组映射失败: {e}")

        # 2. 从自己的 KV 缓存读取（手动设置的覆盖盲盒的）
        cached = await self.get_kv_data(KV_STATE_KEY, None)
        if isinstance(cached, dict):
            manual_mapping = cached.get("member_to_group", {})
            if isinstance(manual_mapping, dict):
                self._member_to_group.update(manual_mapping)
                logger.info(f"从本地缓存加载了 {len(manual_mapping)} 条小组映射")

    async def _save_member_to_group(self) -> None:
        """保存手动设置的小组映射到 KV。"""
        cached = await self.get_kv_data(KV_STATE_KEY, None)
        if not isinstance(cached, dict):
            cached = {}
        cached["member_to_group"] = self._member_to_group
        await self.put_kv_data(KV_STATE_KEY, cached)

    def _get_user_group(self, sender_id: str) -> str | None:
        """根据发送者 ID 获取所属小组名。"""
        return self._member_to_group.get(sender_id)

    # ----------------------------
    # 瓶子池操作
    # ----------------------------

    def _get_pool(self, pool_name: str) -> list[dict[str, Any]]:
        """获取指定池子中的瓶子列表。"""
        if pool_name == "public":
            return self._data.get("public", [])
        groups = self._data.get("groups", {})
        if pool_name not in groups:
            groups[pool_name] = []
        return groups[pool_name]

    def _get_floating_from_pool(
        self, pool_name: str, exclude_sender_id: str = ""
    ) -> list[dict[str, Any]]:
        """获取指定池子中漂流中的瓶子，可排除指定发送者。"""
        pool = self._get_pool(pool_name)
        return [
            b
            for b in pool
            if b.get("status") == "floating" and b.get("sender_id") != exclude_sender_id
        ]

    def _add_bottle_to_pools(self, bottle: dict[str, Any], pools: list[str]) -> None:
        """将瓶子添加到指定池子中。"""
        for pool_name in pools:
            pool = self._get_pool(pool_name)
            pool.append(bottle)
        bottle["pools"] = pools

    def _remove_bottle_from_all_pools(self, bottle_id: str) -> bool:
        """从所有池子中移除指定瓶子（用于收回）。"""
        removed = False
        # 大群池
        public = self._data.get("public", [])
        new_public = [b for b in public if b.get("id") != bottle_id]
        if len(new_public) < len(public):
            removed = True
        self._data["public"] = new_public
        # 小组池
        groups = self._data.get("groups", {})
        for group_name, pool in groups.items():
            new_pool = [b for b in pool if b.get("id") != bottle_id]
            if len(new_pool) < len(pool):
                removed = True
            groups[group_name] = new_pool
        return removed

    def _find_bottle_in_all_pools(
        self, bottle_id: str, sender_id: str = "", status: str = "floating"
    ) -> dict[str, Any] | None:
        """在所有池子中查找指定瓶子。"""
        all_pools = [self._data.get("public", [])]
        all_pools.extend(self._data.get("groups", {}).values())
        for pool in all_pools:
            for bottle in pool:
                if bottle.get("id") == bottle_id:
                    if sender_id and bottle.get("sender_id") != sender_id:
                        continue
                    if status and bottle.get("status") != status:
                        continue
                    return bottle
        return None

    def _get_all_user_bottles(
        self, sender_id: str, status: str = "floating"
    ) -> list[dict[str, Any]]:
        """获取用户在所有池子中的瓶子（去重，按 id）。"""
        seen_ids: set[str] = set()
        result: list[dict[str, Any]] = []
        all_pools = [self._data.get("public", [])]
        all_pools.extend(self._data.get("groups", {}).values())
        for pool in all_pools:
            for bottle in pool:
                if (
                    bottle.get("sender_id") == sender_id
                    and bottle.get("status") == status
                    and bottle.get("id") not in seen_ids
                ):
                    seen_ids.add(bottle["id"])
                    result.append(bottle)
        return result

    # ----------------------------
    # 指令：投瓶（私聊）
    # ----------------------------

    @filter.command("投瓶")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def throw_bottle(self, event: AstrMessageEvent):
        """私聊投入一张匿名小纸条。用法：/投瓶 <内容>"""
        content = event.message_str.strip()
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

        sender_name = event.get_sender_name() or "某位同学"

        await self._get_data()

        # 确定瓶子放入哪些池子
        pools = ["public"]
        group_name = self._get_user_group(sender_id)
        if group_name:
            pools.append(group_name)

        bottle = {
            "id": uuid4().hex[:8],
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": content,
            "created_at": _timestamp(),
            "status": "floating",
            "read_at": None,
            "pools": pools,
        }

        self._add_bottle_to_pools(bottle, pools)
        await self._save_data()

        group_hint = f"\n已同时投入【{group_name}】的私有瓶海。" if group_name else ""
        yield event.plain_result(
            "🫧 你的小纸条已投入瓶中，\n"
            "它会漂向远方，被温柔地拾起。\n\n"
            f"纸条编号：{bottle['id']}\n"
            f"（请记住编号，以便后续管理）"
            f"{group_hint}"
        )

    # ----------------------------
    # 指令：捞瓶（群聊，从大群池）
    # ----------------------------

    @filter.command("捞瓶")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def pick_bottle(self, event: AstrMessageEvent):
        """从大群瓶海随机捞出一张匿名小纸条。"""
        try:
            sender_id = _get_sender_id(event)
        except ValueError:
            yield event.plain_result("无法识别你的身份，请稍后再试。")
            return

        await self._get_data()
        floating = self._get_floating_from_pool("public", exclude_sender_id=sender_id)
        if not floating:
            yield event.plain_result("🫧 瓶海空空如也，暂时没有可以捞的纸条～\n去私聊机器人 /投瓶 投一张吧！")
            return

        bottle = choice(floating)

        yield event.plain_result(
            "🫧 捞到了一张小纸条：\n\n"
            f"「{bottle['content']}」\n\n"
            "——来自某位同学"
        )

    # ----------------------------
    # 指令：自家鱼塘（群聊，从小组池）
    # ----------------------------

    @filter.command("自家鱼塘")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def pick_from_group(self, event: AstrMessageEvent):
        """从自己所属小组的私有瓶海随机捞一张纸条。"""
        try:
            sender_id = _get_sender_id(event)
        except ValueError:
            yield event.plain_result("无法识别你的身份，请稍后再试。")
            return

        await self._get_data()
        group_name = self._get_user_group(sender_id)
        if not group_name:
            yield event.plain_result(
                "🫧 还不知道你属于哪个小组呢～\n"
                "请私聊机器人发送 /设置小组 <组名> 来设置，\n"
                "或者等管理员在盲盒插件中配置好小组信息。"
            )
            return

        floating = self._get_floating_from_pool(group_name, exclude_sender_id=sender_id)
        if not floating:
            yield event.plain_result(
                f"🫧 【{group_name}】的鱼塘空空如也～\n"
                "去私聊机器人 /投瓶 投一张吧！"
            )
            return

        bottle = choice(floating)
        sender_name = bottle.get("sender_name", "某位同学")

        yield event.plain_result(
            f"🫧 从【{group_name}】的鱼塘捞到了一张小纸条：\n\n"
            f"「{bottle['content']}」\n\n"
            f"——来自 {sender_name}"
        )

    # ----------------------------
    # 指令：开箱（群聊，从小组池）
    # ----------------------------

    @filter.command("开箱")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def open_box(self, event: AstrMessageEvent):
        """打开自己所属小组的漂流瓶箱，展示所有纸条（适合每周活动）。"""
        try:
            sender_id = _get_sender_id(event)
        except ValueError:
            yield event.plain_result("无法识别你的身份，请稍后再试。")
            return

        await self._get_data()
        group_name = self._get_user_group(sender_id)
        if not group_name:
            yield event.plain_result(
                "📦 还不知道你属于哪个小组呢～\n"
                "请私聊机器人发送 /设置小组 <组名> 来设置，\n"
                "或者等管理员在盲盒插件中配置好小组信息。"
            )
            return

        floating = self._get_floating_from_pool(group_name)
        if not floating:
            yield event.plain_result(
                f"📦 【{group_name}】的箱子里空空的，没有纸条～\n"
                "去私聊机器人 /投瓶 投一张吧！"
            )
            return

        now = _timestamp()
        lines = [f"📦 打开了【{group_name}】的漂流瓶箱，共 {len(floating)} 张小纸条：\n"]

        for i, bottle in enumerate(floating, 1):
            bottle["status"] = "read"
            bottle["read_at"] = now
            sender_name = bottle.get("sender_name", "某位同学")
            lines.append(f"{i}.「{bottle['content']}」")
            lines.append(f"  ——来自 {sender_name}")
            lines.append("")

        await self._save_data()

        lines.append("所有纸条已读出，箱子里又空了～")
        lines.append("下周继续投递吧！私聊机器人 /投瓶 即可投递。")

        yield event.plain_result("\n".join(lines))

    # ----------------------------
    # 指令：瓶海（群聊）
    # ----------------------------

    @filter.command("瓶海")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def bottle_sea(self, event: AstrMessageEvent):
        """查看大群瓶海和所属小组瓶海的数量统计。"""
        await self._get_data()

        # 大群统计
        public = self._data.get("public", [])
        pub_floating = sum(1 for b in public if b.get("status") == "floating")
        pub_read = sum(1 for b in public if b.get("status") == "read")

        lines = [
            "🫧 瓶海统计：\n",
            f"  【大群瓶海】漂流中 {pub_floating} 张 | 已读出 {pub_read} 张",
        ]

        # 小组统计
        try:
            sender_id = _get_sender_id(event)
            group_name = self._get_user_group(sender_id)
        except ValueError:
            group_name = None

        if group_name:
            group_pool = self._get_pool(group_name)
            grp_floating = sum(1 for b in group_pool if b.get("status") == "floating")
            grp_read = sum(1 for b in group_pool if b.get("status") == "read")
            lines.append(f"  【{group_name}瓶海】漂流中 {grp_floating} 张 | 已读出 {grp_read} 张")
        else:
            lines.append("  【小组瓶海】未设置小组，无法显示")

        lines.append(f"\n私聊机器人 /投瓶 可以投递纸条哦～")

        yield event.plain_result("\n".join(lines))

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

        await self._get_data()
        my_floating = self._get_all_user_bottles(sender_id, status="floating")

        if not my_floating:
            yield event.plain_result("🫧 你目前没有漂流中的纸条～\n私聊机器人 /投瓶 可以投递纸条哦！")
            return

        lines = [f"🫧 你有 {len(my_floating)} 张纸条在漂流中：\n"]
        for bottle in my_floating:
            preview = bottle["content"][:30] + "..." if len(bottle["content"]) > 30 else bottle["content"]
            pools = bottle.get("pools", [])
            pool_hint = "、".join(pools) if pools else "未知"
            lines.append(f"  编号 {bottle['id']}：{preview}")
            lines.append(f"  所在池子：{pool_hint}")
            lines.append(f"  投入时间：{bottle['created_at']}")
            lines.append("")

        yield event.plain_result("\n".join(lines).strip())

    # ----------------------------
    # 指令：收回（私聊）
    # ----------------------------

    @filter.command("收回")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def recall_bottle(self, event: AstrMessageEvent):
        """收回自己仍在漂流中的纸条（从所有池子移除）。用法：/收回 <编号>"""
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

        await self._get_data()
        target = self._find_bottle_in_all_pools(bottle_id, sender_id=sender_id, status="floating")

        if not target:
            yield event.plain_result(
                f"找不到编号为 {bottle_id} 且属于你的漂流中纸条。\n"
                "请检查编号是否正确，或用 /我的瓶子 查看你的纸条。"
            )
            return

        self._remove_bottle_from_all_pools(bottle_id)
        await self._save_data()

        yield event.plain_result(
            f"🫧 已收回编号 {bottle_id} 的纸条。\n"
            "这张纸条已从所有瓶海中移除，不会被任何人看到。"
        )

    # ----------------------------
    # 指令：设置小组（私聊）
    # ----------------------------

    @filter.command("设置小组")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def set_group(self, event: AstrMessageEvent):
        """手动设置自己所属的小组。用法：/设置小组 <组名>"""
        group_name = event.message_str.strip()
        for prefix in ("/设置小组", "/设置小组 "):
            if group_name.startswith(prefix):
                group_name = group_name[len(prefix):].strip()
                break

        if not group_name:
            yield event.plain_result("请输入小组名称～\n用法：/设置小组 <组名>\n例如：/设置小组 第1组")
            return

        try:
            sender_id = _get_sender_id(event)
        except ValueError:
            yield event.plain_result("无法识别你的身份，请稍后再试。")
            return

        old_group = self._member_to_group.get(sender_id)
        self._member_to_group[sender_id] = group_name
        await self._save_member_to_group()

        if old_group:
            yield event.plain_result(
                f"🫧 小组已更新：{old_group} → {group_name}\n"
                "之后投递的纸条会同时进入大群瓶海和该小组的私有瓶海。"
            )
        else:
            yield event.plain_result(
                f"🫧 已设置小组为【{group_name}】\n"
                "之后投递的纸条会同时进入大群瓶海和该小组的私有瓶海。"
            )

    async def terminate(self):
        """插件销毁时的清理工作。"""
        if self._data_loaded:
            await self._save_data()