import asyncio
import os
import re
import sqlite3
import time
from typing import List, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Node, Nodes, Plain
from astrbot.api.star import Context, Star, StarTools, register


class AtRecord:
    def __init__(
        self,
        at_user_id: str,
        sender_id: str,
        sender_name: str,
        group_id: str,
        message_id: str,
        timestamp: int,
    ):
        self.at_user_id = at_user_id
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.group_id = group_id
        self.message_id = message_id
        self.timestamp = timestamp


def _parse_whitelist_groups(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").split(",")]
        return [p for p in parts if p]
    return []


@register(
    "astrbot_plugin_at_check",
    "谁艾特我",
    "基于 Napcat 的谁艾特我插件，只存 msgid",
    "1.0.0",
)
class AtRecorderNapcat(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.loop = asyncio.get_running_loop()

        data_dir = StarTools.get_data_dir("astrbot_plugin_at_check")
        os.makedirs(data_dir, exist_ok=True)
        self.db_file = os.path.join(data_dir, "at_records.db")

        self.whitelist_groups = set(_parse_whitelist_groups(self.config.get("whitelist_groups")))
        self.record_expire_seconds = int(self.config.get("record_expire_seconds", 86400))
        if self.record_expire_seconds <= 0:
            self.record_expire_seconds = 86400

        self.max_records_per_query = 99

        self._setup_database()
        logger.info(
            "插件 'astrbot_plugin_at_check' 已加载, 白名单群=%s, 记录过期秒数=%s, 单次查询上限=%s",
            sorted(self.whitelist_groups) if self.whitelist_groups else [],
            self.record_expire_seconds,
            self.max_records_per_query,
        )

    def _setup_database(self):
        os.makedirs(os.path.dirname(self.db_file), exist_ok=True)
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS at_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at_user_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    timestamp INTEGER NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_at_user_group_time ON at_records (at_user_id, group_id, timestamp)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON at_records (timestamp)"
            )

    def _db_cleanup_records(self):
        try:
            cutoff_timestamp = int(time.time()) - self.record_expire_seconds
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM at_records WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
                conn.commit()
        except Exception as e:
            logger.error("插件 'astrbot_plugin_at_check' 清理旧@记录时出错: %s", e)

    def _db_write_records(self, records_to_insert: List[AtRecord]):
        if not records_to_insert:
            return
        try:
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.executemany(
                    """
                    INSERT INTO at_records (
                        at_user_id,
                        sender_id,
                        sender_name,
                        group_id,
                        message_id,
                        timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r.at_user_id,
                            r.sender_id,
                            r.sender_name,
                            r.group_id,
                            r.message_id,
                            r.timestamp,
                        )
                        for r in records_to_insert
                    ],
                )
                conn.commit()
        except Exception as e:
            logger.error("插件 'astrbot_plugin_at_check' 写入@记录到数据库时出错: %s", e)

    def _db_fetch_records(self, user_id: str, group_id: str) -> List[Tuple[str, str, str, int]]:
        now = int(time.time())
        min_timestamp = now - self.record_expire_seconds
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT sender_id, sender_name, message_id, timestamp
                FROM at_records
                WHERE at_user_id = ?
                  AND group_id = ?
                  AND timestamp >= ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (user_id, group_id, min_timestamp, self.max_records_per_query),
            )
            rows = cursor.fetchall()
        return rows

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def record_at_message(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            return
        group_id_str = str(group_id)

        if self.whitelist_groups and group_id_str not in self.whitelist_groups:
            logger.info(
                "插件 'astrbot_plugin_at_check' 忽略群消息, group_id=%s 不在白名单中",
                group_id_str,
            )
            return

        if re.fullmatch(r"^(谁艾特我|谁@我|谁@我了)[?？]?$", event.message_str):
            logger.info(
                "插件 'astrbot_plugin_at_check' 收到查询指令消息, 不记录为@记录, group_id=%s, sender_id=%s",
                group_id_str,
                event.get_sender_id(),
            )
            return

        logger.info(
            "插件 'astrbot_plugin_at_check' 处理群消息记录@, group_id=%s, sender_id=%s, message_str=%s",
            group_id_str,
            event.get_sender_id(),
            event.message_str,
        )

        await self.loop.run_in_executor(None, self._db_cleanup_records)

        components_to_check = list(event.message_obj.message)
        for component in event.message_obj.message:
            if hasattr(component, "chain"):
                chain = getattr(component, "chain", None)
                if isinstance(chain, list):
                    components_to_check.extend(chain)

        self_id = str(getattr(event.message_obj, "self_id", "")) or None

        logger.info(
            "插件 'astrbot_plugin_at_check' 解析消息链完成, group_id=%s, self_id=%s, 组件总数=%s",
            group_id_str,
            self_id,
            len(components_to_check),
        )

        at_user_ids = []
        for comp in components_to_check:
            if isinstance(comp, At):
                at_id = str(comp.qq)
                if self_id and at_id == self_id:
                    continue
                at_user_ids.append(at_id)

        if not at_user_ids:
            logger.info(
                "插件 'astrbot_plugin_at_check' 当前消息未发现有效@记录, group_id=%s",
                group_id_str,
            )
            return

        message_id = getattr(event.message_obj, "message_id", None)
        if message_id is None:
            logger.info(
                "插件 'astrbot_plugin_at_check' 当前消息缺少 message_id, 不记录, group_id=%s",
                group_id_str,
            )
            return

        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name()
        timestamp = int(getattr(event.message_obj, "timestamp", int(time.time())))

        records = [
            AtRecord(
                at_user_id=at_id,
                sender_id=sender_id,
                sender_name=sender_name,
                group_id=group_id_str,
                message_id=str(message_id),
                timestamp=timestamp,
            )
            for at_id in at_user_ids
        ]

        await self.loop.run_in_executor(None, self._db_write_records, records)
        logger.info(
            "插件 'astrbot_plugin_at_check' 已记录@消息, group_id=%s, sender_id=%s, at_user_ids=%s, message_id=%s, timestamp=%s",
            group_id_str,
            sender_id,
            at_user_ids,
            message_id,
            timestamp,
        )

    @filter.regex(r"^(谁艾特我|谁@我|谁@我了)[?？]?$")
    async def who_at_me(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            return
        group_id_str = str(group_id)

        if self.whitelist_groups and group_id_str not in self.whitelist_groups:
            logger.info(
                "插件 'astrbot_plugin_at_check' 忽略查询消息, group_id=%s 不在白名单中",
                group_id_str,
            )
            return

        client = getattr(event, "bot", None)
        if client is None or not hasattr(client, "api"):
            yield event.plain_result("当前平台不支持查看谁艾特我")
            return

        logger.info(
            "插件 'astrbot_plugin_at_check' 处理查询请求, group_id=%s, sender_id=%s",
            group_id_str,
            event.get_sender_id(),
        )

        await self.loop.run_in_executor(None, self._db_cleanup_records)

        user_id = str(event.get_sender_id())
        try:
            records = await self.loop.run_in_executor(
                None, self._db_fetch_records, user_id, group_id_str
            )
        except Exception as e:
            logger.error(
                "插件 'astrbot_plugin_at_check' 查询@记录时出错: %s",
                e,
            )
            yield event.plain_result("处理你的请求时发生了一个内部错误")
            return

        if not records:
            logger.info(
                "插件 'astrbot_plugin_at_check' 查询结果为空, group_id=%s, user_id=%s",
                group_id_str,
                user_id,
            )
            yield event.plain_result("在设定时间范围内在这个群里没有人@你哦")
            return

        forward_nodes: List[Node] = []

        for sender_id, sender_name, message_id, ts in reversed(records):
            try:
                msg = await client.api.call_action("get_msg", message_id=message_id)
            except Exception as e:
                logger.warning(
                    "插件 'astrbot_plugin_at_check' 调用 get_msg 失败，message_id=%s: %s",
                    message_id,
                    e,
                )
                continue

            logger.info(
                "插件 'astrbot_plugin_at_check' 成功调用 get_msg, group_id=%s, message_id=%s, ts=%s",
                group_id_str,
                message_id,
                ts,
            )

            text = ""
            if isinstance(msg, dict):
                raw_message = msg.get("raw_message")
                if isinstance(raw_message, str) and raw_message.strip():
                    text = raw_message.strip()
                else:
                    segments = msg.get("message")
                    if isinstance(segments, list):
                        parts = []
                        for seg in segments:
                            if not isinstance(seg, dict):
                                continue
                            seg_type = seg.get("type")
                            data = seg.get("data", {})
                            if seg_type == "text":
                                parts.append(str(data.get("text", "")))
                            elif seg_type == "at":
                                qq = data.get("qq")
                                if qq:
                                    parts.append(f"@{qq}")
                            elif seg_type == "image":
                                parts.append("[图片]")
                            elif seg_type == "face":
                                parts.append("[表情]")
                            elif seg_type == "forward":
                                parts.append("[合并转发]")
                        text = "".join(parts).strip()

            if not text:
                continue

            logger.info(
                "插件 'astrbot_plugin_at_check' 生成转发节点文本, group_id=%s, sender_id=%s, text=%s",
                group_id_str,
                sender_id,
                text,
            )

            node = Node(
                uin=str(sender_id),
                name=sender_name,
                content=[Plain(text)],
            )
            forward_nodes.append(node)

        if not forward_nodes:
            yield event.plain_result(
                "在设定时间范围内在这个群里没有人@你哦（可能消息已失效）"
            )
            return

        yield event.chain_result([Nodes(nodes=forward_nodes)])

    async def terminate(self):
        logger.info("插件 'astrbot_plugin_at_check' 已卸载")
