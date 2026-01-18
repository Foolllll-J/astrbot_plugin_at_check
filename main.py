import asyncio
import json
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Face, Node, Nodes, Plain
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
        context_msg_ids: Optional[str] = None,
    ):
        self.at_user_id = at_user_id
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.group_id = group_id
        self.message_id = message_id
        self.timestamp = timestamp
        self.context_msg_ids = context_msg_ids


@register(
    "astrbot_plugin_at_check",
    "谁艾特我",
    "记录群聊中最近@你的消息，支持单独@的上下文获取。",
    "1.0",
)
class AtRecorderNapcat(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.loop = asyncio.get_running_loop()
        self._single_at_watchers: Dict[Tuple[str, str, str], Dict] = {}

        data_dir = StarTools.get_data_dir("astrbot_plugin_at_check")
        os.makedirs(data_dir, exist_ok=True)
        self.db_file = os.path.join(data_dir, "at_records.db")

        self.group_whitelist = set(str(gid).strip() for gid in self.config.get("group_whitelist", []))
        self.record_expire_seconds = int(self.config.get("record_expire_seconds", 86400))
        if self.record_expire_seconds <= 0:
            self.record_expire_seconds = 86400

        self.max_records_per_query = 99
        self.single_at_context_count = int(self.config.get("single_at_context_count", 0) or 0)
        self.after_context_timeout = int(self.config.get("after_context_timeout", 60) or 0)

        self._setup_database()
        groups = sorted(self.group_whitelist) if self.group_whitelist else []
        logger.info(
            f"[AtCheck] 插件已加载，白名单群={groups}，记录保留秒数={self.record_expire_seconds}，单次查询上限={self.max_records_per_query}，单独@上下文条数={self.single_at_context_count}，后续监控时间={self.after_context_timeout}s"
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
                    timestamp INTEGER NOT NULL,
                    context_msg_ids TEXT
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_at_user_group_time ON at_records (at_user_id, group_id, timestamp)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON at_records (timestamp)"
            )
            cursor.execute("PRAGMA table_info(at_records)")
            columns = [row[1] for row in cursor.fetchall()]
            if "context_msg_ids" not in columns:
                cursor.execute("ALTER TABLE at_records ADD COLUMN context_msg_ids TEXT")
            conn.commit()

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
            logger.error(f"[AtCheck] 清理旧@记录时出错: {e}")

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
                        timestamp,
                        context_msg_ids
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r.at_user_id,
                            r.sender_id,
                            r.sender_name,
                            r.group_id,
                            r.message_id,
                            r.timestamp,
                            r.context_msg_ids,
                        )
                        for r in records_to_insert
                    ],
                )
                conn.commit()
        except Exception as e:
            logger.error(f"[AtCheck] 写入@记录到数据库时出错: {e}")

    def _db_fetch_records(self, user_id: str, group_id: str) -> List[Tuple[str, str, str, int, Optional[str]]]:
        now = int(time.time())
        min_timestamp = now - self.record_expire_seconds
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT sender_id, sender_name, message_id, timestamp, context_msg_ids
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

    def _db_update_context_ids(self, group_id: str, at_message_id: str, context_ids: List[str]):
        if not context_ids:
            return
        try:
            payload = json.dumps(context_ids, ensure_ascii=False)
            with sqlite3.connect(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE at_records
                    SET context_msg_ids = ?
                    WHERE group_id = ? AND message_id = ?
                    """,
                    (payload, group_id, at_message_id),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"[AtCheck] 更新上下文消息ID时出错: {e}")

    def _is_single_at_message(self, event: AstrMessageEvent) -> bool:
        components = getattr(event.message_obj, "message", None)
        if not isinstance(components, list):
            return False
        has_at = False
        for comp in components:
            if isinstance(comp, At):
                has_at = True
                continue
            if isinstance(comp, Plain):
                text = getattr(comp, "text", "")
                s = text.strip()
                s = s.strip("，,。.!！?？~… 　\t\r\n")
                if s:
                    return False
                continue
            return False
        return has_at

    async def _fetch_before_message_ids(
        self,
        client,
        group_id_str: str,
        sender_id_str: str,
        at_timestamp: int,
    ) -> List[str]:
        max_count = self.single_at_context_count
        if max_count <= 0:
            return []
        try:
            count = max_count * 5 + 1
            ret = await client.api.call_action(
                "get_group_msg_history",
                group_id=int(group_id_str),
                count=count,
            )
        except Exception as e:
            logger.error(f"[AtCheck] 获取群历史消息失败: {e}")
            return []
        messages = []
        if isinstance(ret, dict):
            data = ret.get("data") or {}
            messages = data.get("messages") or ret.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return []
        candidates: List[Tuple[int, str]] = []
        total = len(messages)
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender", {}) or {}
            msg_sender_id = str(sender.get("user_id") or msg.get("user_id") or "")
            if msg_sender_id != sender_id_str:
                continue
            msg_time = msg.get("time")
            try:
                msg_time_int = int(msg_time)
            except (TypeError, ValueError):
                msg_time_int = at_timestamp - (total - idx)
            if msg_time_int >= at_timestamp:
                continue
            msg_id = msg.get("message_id")
            if msg_id is None:
                continue
            candidates.append((msg_time_int, str(msg_id)))
        if not candidates:
            return []
        candidates.sort(key=lambda x: x[0])
        before_ids = [mid for _, mid in candidates]
        if len(before_ids) > max_count:
            before_ids = before_ids[-max_count:]
        return before_ids

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def record_at_message(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            return
        group_id_str = str(group_id)
        sender_id = str(event.get_sender_id())

        if self.single_at_context_count > 0 and self._single_at_watchers:
            now_ts = int(getattr(event.message_obj, "timestamp", int(time.time())))
            to_remove = []
            for key, watcher in list(self._single_at_watchers.items()):
                if watcher.get("group_id") != group_id_str:
                    continue
                if watcher.get("sender_id") != sender_id:
                    continue
                start_ts = watcher.get("start_ts", 0)
                if now_ts <= start_ts:
                    continue
                expire_ts = watcher.get("expire_ts", 0)
                if expire_ts and now_ts > expire_ts:
                    context_ids = list(watcher.get("before_ids", []))
                    at_mid = watcher.get("at_message_id")
                    if at_mid:
                        context_ids.append(str(at_mid))
                    context_ids.extend(watcher.get("after_ids", []))
                    if context_ids:
                        await self.loop.run_in_executor(
                            None,
                            self._db_update_context_ids,
                            watcher.get("group_id"),
                            str(at_mid),
                            context_ids,
                        )
                    to_remove.append(key)
                    continue
                max_after = watcher.get("max_context", 0)
                after_ids = watcher.get("after_ids", [])
                if max_after and len(after_ids) >= max_after:
                    continue
                msg_id = getattr(event.message_obj, "message_id", None)
                if msg_id is None:
                    continue
                msg_id_str = str(msg_id)
                if msg_id_str in after_ids:
                    continue
                after_ids.append(msg_id_str)
                watcher["after_ids"] = after_ids
                if max_after and len(after_ids) >= max_after:
                    context_ids = list(watcher.get("before_ids", []))
                    at_mid = watcher.get("at_message_id")
                    if at_mid:
                        context_ids.append(str(at_mid))
                    context_ids.extend(after_ids)
                    if context_ids:
                        await self.loop.run_in_executor(
                            None,
                            self._db_update_context_ids,
                            watcher.get("group_id"),
                            str(at_mid),
                            context_ids,
                        )
                    to_remove.append(key)
            for key in to_remove:
                self._single_at_watchers.pop(key, None)

        if self.group_whitelist and group_id_str not in self.group_whitelist:
            return

        if re.fullmatch(r"^(谁艾特我|谁@我|谁@我了)[?？]?$", event.message_str):
            logger.debug(
                f"[AtCheck] 收到查询指令消息，不记录为@记录，group_id={group_id_str}, sender_id={event.get_sender_id()}"
            )
            return

        logger.debug(
            f"[AtCheck] 处理群消息记录@，group_id={group_id_str}, sender_id={event.get_sender_id()}, message_str={event.message_str}"
        )

        await self.loop.run_in_executor(None, self._db_cleanup_records)

        components_to_check = list(event.message_obj.message)
        for component in event.message_obj.message:
            if hasattr(component, "chain"):
                chain = getattr(component, "chain", None)
                if isinstance(chain, list):
                    components_to_check.extend(chain)

        self_id = str(getattr(event.message_obj, "self_id", "")) or None

        logger.debug(
            f"[AtCheck] 解析消息链完成，group_id={group_id_str}, self_id={self_id}, 组件总数={len(components_to_check)}"
        )

        at_user_ids = []
        for comp in components_to_check:
            if isinstance(comp, At):
                at_id = str(comp.qq)
                if self_id and at_id == self_id:
                    continue
                at_user_ids.append(at_id)

        if not at_user_ids:
            return

        message_id = getattr(event.message_obj, "message_id", None)
        if message_id is None:
            logger.debug(
                f"[AtCheck] 当前消息缺少 message_id，不记录，group_id={group_id_str}"
            )
            return

        sender_name = event.get_sender_name()
        timestamp = int(getattr(event.message_obj, "timestamp", int(time.time())))

        client = getattr(event, "bot", None)
        is_single_at = False
        before_ids: List[str] = []
        if self.single_at_context_count > 0:
            is_single_at = self._is_single_at_message(event)
            if is_single_at and client is not None and hasattr(client, "api"):
                before_ids = await self._fetch_before_message_ids(
                    client,
                    group_id_str,
                    sender_id,
                    timestamp,
                )
                logger.debug(
                    f"[AtCheck] 单独@消息前置上下文获取完成，group_id={group_id_str}, sender_id={sender_id}, 条数={len(before_ids)}"
                )

        context_value = None
        if is_single_at and self.single_at_context_count > 0:
            context_ids = list(before_ids)
            context_ids.append(str(message_id))
            context_value = json.dumps(context_ids, ensure_ascii=False)
            key = (group_id_str, sender_id, str(message_id))
            expire_ts = timestamp + self.after_context_timeout
            self._single_at_watchers[key] = {
                "group_id": group_id_str,
                "sender_id": sender_id,
                "at_message_id": str(message_id),
                "start_ts": timestamp,
                "expire_ts": expire_ts,
                "before_ids": list(before_ids),
                "after_ids": [],
                "max_context": self.single_at_context_count,
            }
            logger.debug(
                f"[AtCheck] 创建单独@上下文监控，group_id={group_id_str}, sender_id={sender_id}, at_message_id={message_id}, before_count={len(before_ids)}, max_after={self.single_at_context_count}"
            )

        records = []
        for at_id in at_user_ids:
            records.append(
                AtRecord(
                    at_user_id=at_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    group_id=group_id_str,
                    message_id=str(message_id),
                    timestamp=timestamp,
                    context_msg_ids=context_value,
                )
            )

        await self.loop.run_in_executor(None, self._db_write_records, records)
        logger.debug(
            f"[AtCheck] 已记录@消息，group_id={group_id_str}, sender_id={sender_id}, at_user_ids={at_user_ids}, message_id={message_id}, timestamp={timestamp}"
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.regex(r"^(谁艾特我|谁@我|谁@我了)[?？]?$")
    async def who_at_me(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            return
        group_id_str = str(group_id)

        if self.group_whitelist and group_id_str not in self.group_whitelist:
            logger.debug(
                f"[AtCheck] 忽略查询消息，group_id={group_id_str} 不在白名单中"
            )
            return

        client = getattr(event, "bot", None)
        if client is None or not hasattr(client, "api"):
            yield event.plain_result("当前平台不支持查看谁艾特我")
            return

        logger.debug(
            f"[AtCheck] 处理查询请求，group_id={group_id_str}, sender_id={event.get_sender_id()}"
        )

        await self.loop.run_in_executor(None, self._db_cleanup_records)

        user_id = str(event.get_sender_id())
        try:
            records = await self.loop.run_in_executor(
                None, self._db_fetch_records, user_id, group_id_str
            )
        except Exception as e:
            logger.error(
                f"[AtCheck] 查询@记录时出错: {e}"
            )
            yield event.plain_result("处理你的请求时发生了一个内部错误")
            return

        if not records:
            logger.info(
                f"[AtCheck] 查询结果为空，group_id={group_id_str}, user_id={user_id}"
            )
            yield event.plain_result("在设定时间范围内在这个群里没有人@你哦")
            return

        if self.single_at_context_count <= 0:
            id_nodes = [
                {
                    "type": "node",
                    "data": {
                        "id": str(row[2]),
                    },
                }
                for row in reversed(records)
            ]
        else:
            all_ids: List[str] = []
            seen = set()
            context_id_set = set()
            for row in records:
                context_raw = row[4]
                if not context_raw:
                    continue
                try:
                    parsed = json.loads(context_raw)
                    if isinstance(parsed, list):
                        for x in parsed:
                            if x is None:
                                continue
                            context_id_set.add(str(x))
                except Exception as e:
                    logger.error(f"[AtCheck] 预解析 context_msg_ids 失败: {e}")

            for row in reversed(records):
                message_id = str(row[2])
                context_raw = row[4]
                ids_for_row: List[str] = []
                if context_raw:
                    try:
                        parsed = json.loads(context_raw)
                        if isinstance(parsed, list):
                            ids_for_row = [str(x) for x in parsed if x is not None]
                    except Exception as e:
                        logger.error(f"[AtCheck] 解析 context_msg_ids 失败: {e}")
                if not ids_for_row:
                    if message_id in context_id_set:
                        continue
                    ids_for_row = [message_id]
                for mid in ids_for_row:
                    if mid in seen:
                        continue
                    seen.add(mid)
                    all_ids.append(mid)
            id_nodes = [
                {
                    "type": "node",
                    "data": {
                        "id": mid,
                    },
                }
                for mid in all_ids
            ]

        if id_nodes:
            try:
                await client.api.call_action(
                    "send_group_forward_msg",
                    group_id=group_id_str,
                    messages=id_nodes,
                )
                logger.debug(
                    f"[AtCheck] 已通过 send_group_forward_msg 使用节点id发送 Napcat 合并转发，group_id={group_id_str}, 节点数={len(id_nodes)}"
                )
                return
            except Exception as e:
                logger.error(
                    f"[AtCheck] 使用节点id发送 Napcat 合并转发失败: {e}"
                )
                yield event.plain_result("处理你的请求时发生了一个内部错误")

    async def terminate(self):
        logger.info("[AtCheck] 插件已卸载")
