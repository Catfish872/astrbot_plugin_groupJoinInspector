# -*- coding: utf-8 -*-
"""AstrBot 群入群申请审批检查插件。

仅处理 aiocqhttp/NapCat 的群申请事件：按群号匹配配置的审批答案，查询申请人 QQ 等级，
对 0 级或低等级申请填写拒绝原因并拒绝。
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_NAME = "astrbot_plugin_groupJoinInspector"
PLUGIN_DISPLAY_NAME = "群入群申请检查器"


@register(
    PLUGIN_NAME,
    "天各一方",
    "基于 NapCat/aiocqhttp 的群申请答案与 QQ 等级审批检查插件。",
    "1.0.0",
    "https://github.com/Catfish872/astrbot_plugin_groupJoinInspector",
)
class GroupJoinInspector(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.settings = self._load_settings(self.config)

        self.data_dir = Path(StarTools.get_data_dir()) / PLUGIN_NAME
        self.actions_path = self.data_dir / "actions.jsonl"
        self.recent_requests: Dict[str, float] = {}

    async def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"[{PLUGIN_NAME}] 已启动：审批规则 {len(self.settings['approval_rules'])} 条。"
        )

    async def terminate(self):
        pass

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _load_settings(self, config: Any) -> Dict[str, Any]:
        def get(key: str, default: Any) -> Any:
            try:
                return config.get(key, default)
            except Exception:
                return default

        return {
            "enabled": self._safe_bool(get("enabled", True), True),
            "approval_rules": self._parse_approval_rules(get("approval_rules", [])),
            "answer_trim": self._safe_bool(get("answer_trim", True), True),
            "answer_case_sensitive": self._safe_bool(get("answer_case_sensitive", True), True),
            "answer_mismatch_reject": self._safe_bool(get("answer_mismatch_reject", True), True),
            "answer_mismatch_reason": str(get("answer_mismatch_reason", "验证答案不正确")),
            "reject_level_threshold": max(
                0, self._safe_int(get("reject_level_threshold", 5), 5)
            ),
            "low_level_reject_reason": str(get("low_level_reject_reason", "请换大号加群")),
            "reject_zero_level": self._safe_bool(get("reject_zero_level", True), True),
            "zero_level_reject_reason": str(
                get("zero_level_reject_reason", "请打开个人资料中的QQ等级显示")
            ),
            "reject_unknown_level": self._safe_bool(get("reject_unknown_level", True), True),
            "unknown_level_reject_reason": str(
                get("unknown_level_reject_reason", "暂时无法确认QQ等级，请打开个人资料中的QQ等级显示后重新申请")
            ),
            "auto_approve_passed": self._safe_bool(get("auto_approve_passed", False), False),
            "approve_reason": str(get("approve_reason", "欢迎加入")),
            "duplicate_request_seconds": max(
                1, self._safe_int(get("duplicate_request_seconds", 10), 10)
            ),
        }

    def _parse_approval_rules(self, raw_rules: Any) -> Dict[str, str]:
        rules: Dict[str, str] = {}
        if not isinstance(raw_rules, list):
            return rules
        for item in raw_rules:
            group_id = ""
            answer = ""
            if isinstance(item, dict):
                group_id = self._normalize_digits(
                    item.get("group_id") or item.get("group") or item.get("群号")
                )
                answer = str(item.get("answer") or item.get("答案") or "")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                group_id = self._normalize_digits(item[0])
                answer = str(item[1] or "")
            elif isinstance(item, str):
                parsed = self._parse_rule_text(item)
                if parsed:
                    group_id, answer = parsed
            if group_id and answer:
                rules[group_id] = answer
        return rules

    def _parse_rule_text(self, text: str) -> Optional[tuple]:
        text = str(text or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text.replace("'", '"'))
            if isinstance(data, list) and len(data) >= 2:
                group_id = self._normalize_digits(data[0])
                answer = str(data[1] or "")
                if group_id and answer:
                    return group_id, answer
        except Exception:
            pass
        if ":" in text:
            group, answer = text.split(":", 1)
            group_id = self._normalize_digits(group)
            answer = answer.strip()
            if group_id and answer:
                return group_id, answer
        return None

    @staticmethod
    def _safe_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on", "开启", "启用"}:
            return True
        if text in {"0", "false", "no", "n", "off", "关闭", "禁用"}:
            return False
        return default

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _normalize_digits(value: Any) -> str:
        return "".join(ch for ch in str(value or "") if ch.isdigit())

    @staticmethod
    def _normalize_text(value: Any, trim: bool, case_sensitive: bool) -> str:
        text = str(value or "")
        if trim:
            text = text.strip()
        if not case_sensitive:
            text = text.lower()
        return text

    # ------------------------------------------------------------------
    # 事件监听
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_request(self, event: AstrMessageEvent):
        if not self.settings["enabled"]:
            return
        if event.get_platform_name() != "aiocqhttp":
            return

        raw = self._get_raw_event(event)
        if not raw:
            return
        if self._raw_get(raw, "post_type") != "request":
            return
        if self._raw_get(raw, "request_type") != "group":
            return

        group_id = str(self._raw_get(raw, "group_id") or event.get_group_id() or "")
        user_id = self._normalize_digits(self._raw_get(raw, "user_id"))
        flag = self._raw_get(raw, "flag")
        comment = str(self._raw_get(raw, "comment") or "")
        sub_type = str(self._raw_get(raw, "sub_type") or "")
        if not group_id or not user_id or not flag:
            return

        if not self._mark_request_once(f"{group_id}:{user_id}:{flag}"):
            return

        expected_answer = self.settings["approval_rules"].get(group_id)
        if expected_answer is None:
            return

        if not await self._bot_can_manage_group(event, group_id):
            return

        extracted_answer = self._extract_answer_from_comment(comment)
        if not self._answer_matches(extracted_answer, expected_answer):
            if self.settings["answer_mismatch_reject"]:
                await self._set_group_add_request(
                    event,
                    flag,
                    approve=False,
                    reason=self.settings["answer_mismatch_reason"],
                )
                self._append_action_log(
                    {
                        "action": "reject_answer_mismatch",
                        "group_id": group_id,
                        "user_id": user_id,
                        "sub_type": sub_type,
                        "comment": comment,
                        "extracted_answer": extracted_answer,
                        "expected_answer": expected_answer,
                    }
                )
            return

        level = await self._get_qq_level(event, user_id)
        if level == 0 and self.settings["reject_zero_level"]:
            await self._set_group_add_request(
                event,
                flag,
                approve=False,
                reason=self.settings["zero_level_reject_reason"],
            )
            self._append_action_log(
                {
                    "action": "reject_zero_level",
                    "group_id": group_id,
                    "user_id": user_id,
                    "level": level,
                    "comment": comment,
                }
            )
            return

        if level is None and self.settings["reject_unknown_level"]:
            await self._set_group_add_request(
                event,
                flag,
                approve=False,
                reason=self.settings["unknown_level_reject_reason"],
            )
            self._append_action_log(
                {
                    "action": "reject_unknown_level",
                    "group_id": group_id,
                    "user_id": user_id,
                    "level": level,
                    "comment": comment,
                }
            )
            return

        if (
            level is not None
            and level != 0
            and level <= self.settings["reject_level_threshold"]
        ):
            await self._set_group_add_request(
                event,
                flag,
                approve=False,
                reason=self.settings["low_level_reject_reason"],
            )
            self._append_action_log(
                {
                    "action": "reject_low_level",
                    "group_id": group_id,
                    "user_id": user_id,
                    "level": level,
                    "threshold": self.settings["reject_level_threshold"],
                    "comment": comment,
                }
            )
            return

        if self.settings["auto_approve_passed"]:
            await self._set_group_add_request(
                event,
                flag,
                approve=True,
                reason=self.settings["approve_reason"],
            )
            action = "approve_passed"
        else:
            action = "passed_leave_to_admin"

        self._append_action_log(
            {
                "action": action,
                "group_id": group_id,
                "user_id": user_id,
                "level": level,
                "comment": comment,
            }
        )

    def _extract_answer_from_comment(self, comment: str) -> str:
        """从 QQ/NapCat 验证消息中提取用户填写的答案。

        NapCat 实际传入的 comment 常见格式是：
        问题：xxx\n答案：yyy
        因此优先提取最后一个“答案:”或“答案：”后的内容；如果没有该标记，则回退使用完整 comment。
        """
        text = str(comment or "")
        answer = ""
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            for marker in ("答案：", "答案:", "回答：", "回答:"):
                if marker in line:
                    answer = line.split(marker, 1)[1].strip()
        return answer if answer else text

    def _answer_matches(self, actual: str, expected: str) -> bool:
        trim = self.settings["answer_trim"]
        case_sensitive = self.settings["answer_case_sensitive"]
        return self._normalize_text(actual, trim, case_sensitive) == self._normalize_text(
            expected, trim, case_sensitive
        )

    def _mark_request_once(self, key: str) -> bool:
        now = time.time()
        ttl = self.settings["duplicate_request_seconds"]
        expired = [k for k, t in self.recent_requests.items() if now - t > ttl]
        for k in expired:
            self.recent_requests.pop(k, None)
        if key in self.recent_requests and now - self.recent_requests[key] <= ttl:
            return False
        self.recent_requests[key] = now
        return True

    # ------------------------------------------------------------------
    # OneBot / NapCat API
    # ------------------------------------------------------------------
    async def _get_qq_level(self, event: AstrMessageEvent, user_id: str) -> Optional[int]:
        try:
            data = await self._call_action(
                event,
                "get_stranger_info",
                user_id=int(user_id),
                no_cache=True,
            )
            level = self._extract_level(data, "qqLevel")
            if level is None:
                level = self._extract_level(data, "qq_level")
            return level
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 获取 {user_id} QQ 等级失败：{e}")
            return None

    @staticmethod
    def _extract_level(data: Any, key: str) -> Optional[int]:
        if not isinstance(data, dict):
            return None
        value = data.get(key)
        if value is None or value == "":
            return None
        try:
            level = int(value)
        except Exception:
            return None
        if level < 0:
            return None
        return level

    async def _bot_can_manage_group(self, event: AstrMessageEvent, group_id: str) -> bool:
        self_id = self._normalize_digits(event.get_self_id())
        if not self_id or not group_id:
            return False
        try:
            info = await self._call_action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(self_id),
                no_cache=True,
            )
            role = str(info.get("role", "member")) if isinstance(info, dict) else "member"
            if role in {"owner", "admin"}:
                return True
            logger.info(
                f"[{PLUGIN_NAME}] 跳过群 {group_id} 的申请检查：机器人当前身份是 {role}，不是群主或管理员。"
            )
            return False
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 无法确认机器人在群 {group_id} 的管理权限：{e}")
            return False

    async def _set_group_add_request(
        self, event: AstrMessageEvent, flag: Any, approve: bool, reason: str = ""
    ) -> None:
        await self._call_action(
            event,
            "set_group_add_request",
            flag=str(flag),
            approve=bool(approve),
            reason=reason or " ",
        )

    async def _call_action(self, event: AstrMessageEvent, action: str, **params) -> Any:
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("当前事件没有 bot 实例")
        if hasattr(bot, "call_action"):
            return await bot.call_action(action, **params)
        api = getattr(bot, "api", None)
        if api and hasattr(api, "call_action"):
            return await api.call_action(action, **params)
        raise RuntimeError("当前 bot 不支持 call_action")

    # ------------------------------------------------------------------
    # 原始事件与日志
    # ------------------------------------------------------------------
    def _get_raw_event(self, event: AstrMessageEvent) -> Any:
        try:
            return getattr(event.message_obj, "raw_message", None)
        except Exception:
            return None

    @staticmethod
    def _raw_get(raw: Any, key: str, default: Any = None) -> Any:
        try:
            if isinstance(raw, dict):
                return raw.get(key, default)
            return getattr(raw, key, default)
        except Exception:
            return default

    def _append_action_log(self, record: Dict[str, Any]) -> None:
        try:
            record = {"time": int(time.time()), **record}
            self.actions_path.parent.mkdir(parents=True, exist_ok=True)
            with self.actions_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 写入审批记录失败：{e}")
