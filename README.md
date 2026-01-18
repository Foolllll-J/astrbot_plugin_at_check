<div align="center">

# 🔔 谁艾特我

<i>🔍 找回那些被淹没在 99+ 中的召唤！</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的群聊 @ 消息记录插件。它可以自动记录群聊中提及你的消息，并支持在消息仅包含 @ 时，自动拉取上下文（前后消息）以补全语义。

---

## 🚀 功能特性

* 💎 **原始消息引用**：直接基于原消息构造合并转发节点，保留被 @ 时的发送时间，并支持图片、表情等各类消息组件的展示。
* 🧩 **智能上下文补全**：当有人仅发送一个 @ 而没有正文时，插件会自动向上追溯历史记录并向下监控后续补充，完整还原对话语义。
* 🛡️ **群聊白名单控制**：支持配置特定群聊白名单，按需开启记录功能，灵活管理。

---

## 📖 使用说明

| 项目               | 描述                                                                                                                       |
| :----------------- | :------------------------------------------------------------------------------------------------------------------------- |
| **支持平台** | 仅支持 **`napcat`** 平台。                                                                                             |
| **查询指令** | 发送 **`谁艾特我`**、**`谁@我`** 或 **`谁@我了`**。                                                                                      |
| **呈现方式** | 以 **合并转发** 的形式展示最近被 @ 的消息。 |
| **上下文说明** | 单独@补全的上下文仅包含文本、表情、图片等常规消息，不包含视频、语音、文件、聊天记录等消息类型。 |

---

## ⚙️ 配置说明

| 配置项 | 类型 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- |
| **`group_whitelist`** | `list` | `[]` | 启用记录的群号白名单。留空则全局启用。 |
| **`record_expire_seconds`** | `int` | `86400` | 记录保留时长（秒）。过期记录自动清理。 |
| **`single_at_context_count`** | `int` | `0` | 当有人“单独@”你时，尝试抓取该发送者前后的消息条数（最大 5）。 |
| **`after_context_timeout`** | `int` | `60` | 单独@后，等待补充后续消息的观察时间（秒）。 |
| **`context_sender_restricted`** | `bool` | `true` | 开启时，上下文仅捕获发起艾特者的消息；关闭时捕获群内所有人的消息。 |

---

## 📅 更新日志

**v1.0**

- 初始版本发布。
- 支持记录群聊 @ 消息。
- 支持单独 @ 的上下文获取。

---

## ❤️ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到问题，欢迎在本仓库提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_at_check/issues)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>

