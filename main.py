# -*- coding: utf-8 -*-
"""AstrBot 群入群申请审批检查插件。

仅处理 aiocqhttp/NapCat 的群申请事件：按群号匹配配置的审批答案，查询申请人 QQ 等级，
并按 0 级、低等级或名称威胁情报策略填写拒绝原因并拒绝。
"""

import asyncio
import json
import random
import re
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_NAME = "astrbot_plugin_groupJoinInspector"
PLUGIN_DISPLAY_NAME = "群入群申请检查器"
QZONE_FEED_LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
QZONE_BASE_URL = "https://user.qzone.qq.com"
QZONE_REPORTED_BLOCK_TEXT = "您访问的空间被多名用户举报，暂时无法查看"


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
        self.pending_verifications: Dict[str, Dict[str, Any]] = {}
        self.verification_tasks: Dict[str, asyncio.Task] = {}

    async def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"[{PLUGIN_NAME}] 已启动：审批规则 {len(self.settings['approval_rules'])} 条。"
        )

    async def terminate(self):
        for task in list(self.verification_tasks.values()):
            if task and not task.done():
                task.cancel()
        for task in list(self.verification_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.verification_tasks.clear()
        self.pending_verifications.clear()

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
            "threat_name_keywords": self._parse_string_list(get("threat_name_keywords", ["頟"])),
            "threat_name_case_sensitive": self._safe_bool(
                get("threat_name_case_sensitive", True), True
            ),
            "threat_name_reject_reason": str(
                get("threat_name_reject_reason", "请换大号加群")
            ),
            "auto_approve_passed": self._safe_bool(get("auto_approve_passed", False), False),
            "approve_reason": str(get("approve_reason", "欢迎加入")),
            "duplicate_request_seconds": max(
                1, self._safe_int(get("duplicate_request_seconds", 10), 10)
            ),
            "post_join_verification_enabled": self._safe_bool(
                get("post_join_verification_enabled", True), True
            ),
            "post_join_verification_timeout_seconds": max(
                30, self._safe_int(get("post_join_verification_timeout_seconds", 120), 120)
            ),
            "post_join_profile_min_effective_items": max(
                1, self._safe_int(get("post_join_profile_min_effective_items", 2), 2)
            ),
            "post_join_qzone_access_enabled": self._safe_bool(
                get("post_join_qzone_access_enabled", True), True
            ),
            "post_join_qzone_timeout_seconds": max(
                3, self._safe_int(get("post_join_qzone_timeout_seconds", 8), 8)
            ),
            "post_join_vip_trust_enabled": self._safe_bool(
                get("post_join_vip_trust_enabled", True), True
            ),
            "post_join_qq_level_trust_enabled": self._safe_bool(
                get("post_join_qq_level_trust_enabled", True), True
            ),
            "post_join_qq_level_min": max(
                1, self._safe_int(get("post_join_qq_level_min", 30), 30)
            ),
            "post_join_account_age_trust_enabled": self._safe_bool(
                get("post_join_account_age_trust_enabled", True), True
            ),
            "post_join_account_min_days": max(
                1, self._safe_int(get("post_join_account_min_days", 1825), 1825)
            ),
            "post_join_verification_prompt_template": str(
                get(
                    "post_join_verification_prompt_template",
                    "{at_user} 为确认是真人，请在 {timeout} 秒内直接发送计算结果：{question} = ?",
                )
            ),
            "post_join_verification_success_template": str(
                get("post_join_verification_success_template", "{at_user} 验证通过，欢迎加入。")
            ),
            "post_join_verification_kick_template": str(
                get("post_join_verification_kick_template", "{user_id} 未在规定时间内完成验证，已移出群聊。")
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

    def _parse_string_list(self, value: Any) -> List[str]:
        items: List[str] = []
        if isinstance(value, list):
            candidates = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                candidates = []
            else:
                try:
                    parsed = json.loads(text)
                    candidates = parsed if isinstance(parsed, list) else [text]
                except Exception:
                    candidates = [part for part in text.replace("，", ",").split(",")]
        else:
            candidates = []

        for item in candidates:
            keyword = str(item or "").strip()
            if keyword and keyword not in items:
                items.append(keyword)
        return items

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
    async def on_group_event(self, event: AstrMessageEvent):
        if not self.settings["enabled"]:
            return
        if event.get_platform_name() != "aiocqhttp":
            return

        raw = self._get_raw_event(event)
        if not raw:
            return

        post_type = self._raw_get(raw, "post_type")
        if post_type == "request":
            await self._handle_group_request(event, raw)
        elif post_type == "notice":
            await self._handle_group_notice(event, raw)
        elif post_type == "message":
            await self._handle_group_message(event, raw)

    async def _handle_group_request(self, event: AstrMessageEvent, raw: Any) -> None:
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

        profile = await self._get_stranger_profile(event, user_id)
        level = profile.get("level")
        nickname = str(profile.get("nickname") or "")

        if self._match_threat_name_keyword(nickname):
            await self._set_group_add_request(
                event,
                flag,
                approve=False,
                reason=self.settings["threat_name_reject_reason"],
            )
            self._append_action_log(
                {
                    "action": "reject_threat_name",
                    "group_id": group_id,
                    "user_id": user_id,
                    "nickname": nickname,
                    "comment": comment,
                }
            )
            return

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
                    "nickname": nickname,
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
                    "nickname": nickname,
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
                    "nickname": nickname,
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
                "nickname": nickname,
                "comment": comment,
            }
        )

    async def _handle_group_notice(self, event: AstrMessageEvent, raw: Any) -> None:
        if not self.settings["post_join_verification_enabled"]:
            return
        if self._raw_get(raw, "notice_type") == "group_decrease":
            group_id = str(self._raw_get(raw, "group_id") or "")
            user_id = self._normalize_digits(self._raw_get(raw, "user_id"))
            self._clear_pending_verification(group_id, user_id)
            return
        if self._raw_get(raw, "notice_type") != "group_increase":
            return

        group_id = str(self._raw_get(raw, "group_id") or event.get_group_id() or "")
        user_id = self._normalize_digits(self._raw_get(raw, "user_id"))
        if not group_id or not user_id or user_id == self._normalize_digits(event.get_self_id()):
            return
        if group_id not in self.settings["approval_rules"]:
            return
        if not await self._bot_can_manage_group(event, group_id):
            return

        profile = await self._get_stranger_profile(event, user_id)
        trust = await self._evaluate_post_join_trust(event, user_id, profile)
        if trust["passed"]:
            self._append_action_log(
                {
                    "action": "post_join_trust_pass",
                    "group_id": group_id,
                    "user_id": user_id,
                    "trust_reasons": trust["reasons"],
                    "trust_detail": trust["detail"],
                }
            )
            return

        await self._start_post_join_verification(event, group_id, user_id, trust)

    async def _handle_group_message(self, event: AstrMessageEvent, raw: Any) -> None:
        if self._raw_get(raw, "message_type") != "group":
            return
        group_id = str(self._raw_get(raw, "group_id") or event.get_group_id() or "")
        user_id = self._normalize_digits(self._raw_get(raw, "user_id") or event.get_sender_id())
        key = self._pending_key(group_id, user_id)
        pending = self.pending_verifications.get(key)
        if not pending:
            return

        text = str(getattr(event, "message_str", "") or event.get_message_str() or "").strip()
        answer = self._extract_numeric_answer(text)
        if answer is None:
            return

        pending["attempts"] = int(pending.get("attempts") or 0) + 1
        if answer != str(pending.get("answer")):
            self.pending_verifications[key] = pending
            self._append_action_log(
                {
                    "action": "post_join_verify_wrong_answer",
                    "group_id": group_id,
                    "user_id": user_id,
                    "answer": answer,
                    "attempts": pending["attempts"],
                }
            )
            return

        self._clear_pending_verification(group_id, user_id)
        await self._send_group_message(
            event,
            group_id,
            self._format_template(
                self.settings["post_join_verification_success_template"],
                at_user=f"[CQ:at,qq={user_id}]",
                user_id=user_id,
                group_id=group_id,
            ),
        )
        self._append_action_log(
            {
                "action": "post_join_verify_pass",
                "group_id": group_id,
                "user_id": user_id,
                "attempts": pending.get("attempts", 0),
            }
        )
        event.stop_event()

    async def _start_post_join_verification(
        self, event: AstrMessageEvent, group_id: str, user_id: str, trust: Dict[str, Any]
    ) -> None:
        key = self._pending_key(group_id, user_id)
        old_task = self.verification_tasks.pop(key, None)
        if old_task and not old_task.done():
            old_task.cancel()

        question, answer = self._generate_math_question()
        timeout = self.settings["post_join_verification_timeout_seconds"]
        pending = {
            "group_id": group_id,
            "user_id": user_id,
            "question": question,
            "answer": str(answer),
            "created_at": int(time.time()),
            "expire_at": int(time.time()) + timeout,
            "attempts": 0,
            "trust_detail": trust.get("detail", {}),
            "event": event,
        }
        self.pending_verifications[key] = pending
        task = asyncio.create_task(self._post_join_verification_timeout(key))
        self.verification_tasks[key] = task

        await self._send_group_message(
            event,
            group_id,
            self._format_template(
                self.settings["post_join_verification_prompt_template"],
                at_user=f"[CQ:at,qq={user_id}]",
                user_id=user_id,
                group_id=group_id,
                timeout=timeout,
                question=question,
            ),
        )
        self._append_action_log(
            {
                "action": "post_join_verify_start",
                "group_id": group_id,
                "user_id": user_id,
                "question": question,
                "expire_at": pending["expire_at"],
                "trust_detail": pending["trust_detail"],
            }
        )

    async def _post_join_verification_timeout(self, key: str) -> None:
        try:
            pending = self.pending_verifications.get(key)
            if not pending:
                return
            sleep_seconds = max(0, int(pending.get("expire_at", 0)) - int(time.time()))
            await asyncio.sleep(sleep_seconds)
            pending = self.pending_verifications.get(key)
            if not pending:
                return
            group_id = str(pending.get("group_id") or "")
            user_id = str(pending.get("user_id") or "")
            if not group_id or not user_id:
                self.pending_verifications.pop(key, None)
                return
            event = pending.get("event")
            bot_event = event if isinstance(event, AstrMessageEvent) else None
            if bot_event is None:
                self.pending_verifications.pop(key, None)
                self.verification_tasks.pop(key, None)
                return
            await self._kick_user(bot_event, group_id, user_id, reject=False)
            await self._send_group_message(
                bot_event,
                group_id,
                self._format_template(
                    self.settings["post_join_verification_kick_template"],
                    at_user=f"[CQ:at,qq={user_id}]",
                    user_id=user_id,
                    group_id=group_id,
                ),
            )
            self._append_action_log(
                {
                    "action": "post_join_verify_timeout_kick",
                    "group_id": group_id,
                    "user_id": user_id,
                    "attempts": pending.get("attempts", 0),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 入群后验证超时处理异常：{e}")
        finally:
            pending = self.pending_verifications.pop(key, None)
            self.verification_tasks.pop(key, None)

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
    async def _get_stranger_profile(self, event: AstrMessageEvent, user_id: str) -> Dict[str, Any]:
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
            nickname = str(data.get("nickname") or "") if isinstance(data, dict) else ""
            return {"level": level, "nickname": nickname, "raw": data if isinstance(data, dict) else {}}
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 获取 {user_id} 资料失败：{e}")
            return {"level": None, "nickname": "", "raw": {}}

    async def _evaluate_post_join_trust(
        self, event: AstrMessageEvent, user_id: str, profile: Dict[str, Any]
    ) -> Dict[str, Any]:
        raw = profile.get("raw") if isinstance(profile.get("raw"), dict) else {}
        level = profile.get("level")
        reasons: List[str] = []
        detail: Dict[str, Any] = {}

        if self.settings["post_join_qzone_access_enabled"]:
            qzone = await self._check_qzone_accessible(event, user_id)
            detail["qzone"] = qzone
            if qzone.get("accessible"):
                reasons.append("qzone_accessible")

        profile_items = self._count_effective_profile_items(raw)
        detail["profile_effective_items"] = profile_items
        if profile_items >= self.settings["post_join_profile_min_effective_items"]:
            reasons.append("profile_effective_items")

        vip = self._has_any_vip(raw)
        detail["vip"] = vip
        if self.settings["post_join_vip_trust_enabled"] and vip:
            reasons.append("vip")

        detail["qq_level"] = level
        if (
            self.settings["post_join_qq_level_trust_enabled"]
            and level is not None
            and int(level) >= self.settings["post_join_qq_level_min"]
        ):
            reasons.append("qq_level")

        account_age_days = self._get_account_age_days(raw)
        detail["account_age_days"] = account_age_days
        if (
            self.settings["post_join_account_age_trust_enabled"]
            and account_age_days is not None
            and account_age_days >= self.settings["post_join_account_min_days"]
        ):
            reasons.append("account_age")

        return {"passed": bool(reasons), "reasons": reasons, "detail": detail}

    @staticmethod
    def _is_empty_profile_value(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, bool):
            return not value
        if isinstance(value, (int, float)):
            return value == 0
        if isinstance(value, (list, tuple, set, dict)):
            return len(value) == 0
        text = str(value).strip()
        return text in {"", "0", "0-0-0", "-", "--", "未知", "保密", "未填", "unknown", "None", "null"}

    def _count_effective_profile_items(self, data: Dict[str, Any]) -> int:
        if not isinstance(data, dict):
            return 0
        count = 0
        sex = str(data.get("sex") or "").lower()
        if sex in {"male", "female"}:
            count += 1
        if self._extract_level(data, "age") and self._extract_level(data, "age") > 0:
            count += 1
        if not self._is_empty_profile_value(data.get("birthday_month")) and not self._is_empty_profile_value(data.get("birthday_day")):
            count += 1
        location_values = [data.get("country"), data.get("province"), data.get("city")]
        if any(not self._is_empty_profile_value(value) for value in location_values):
            count += 1
        for key in (
            "long_nick",
            "longNick",
            "labels",
            "phoneNum",
            "eMail",
            "address",
            "college",
            "interest",
            "qid",
        ):
            if not self._is_empty_profile_value(data.get(key)):
                count += 1
        if not self._is_empty_profile_value(data.get("homeTown")):
            count += 1
        if self._extract_level(data, "kBloodType") and self._extract_level(data, "kBloodType") > 0:
            count += 1
        if self._extract_level(data, "makeFriendCareer") and self._extract_level(data, "makeFriendCareer") > 0:
            count += 1
        return count

    def _has_any_vip(self, data: Dict[str, Any]) -> bool:
        if not isinstance(data, dict):
            return False
        if self._safe_bool(data.get("is_vip"), False):
            return True
        if self._safe_bool(data.get("is_years_vip"), False):
            return True
        vip_level = self._extract_level(data, "vip_level")
        return bool(vip_level and vip_level > 0)

    def _get_account_age_days(self, data: Dict[str, Any]) -> Optional[int]:
        if not isinstance(data, dict):
            return None
        reg_time = self._extract_level(data, "reg_time")
        if reg_time is None:
            reg_time = self._extract_level(data, "regTime")
        if reg_time is None or reg_time <= 0:
            return None
        return max(0, int((time.time() - reg_time) // 86400))

    @staticmethod
    def _generate_qzone_gtk(skey: str) -> str:
        hash_val = 5381
        for ch in str(skey or ""):
            hash_val += (hash_val << 5) + ord(ch)
        return str(hash_val & 0x7FFFFFFF)

    @staticmethod
    def _extract_qzone_payload(text: str) -> Optional[Dict[str, Any]]:
        raw = str(text or "")
        match = re.search(r"callback\s*\(\s*([^{]*(\{.*\})[^)]*)\s*\)", raw, re.I | re.S)
        if match:
            json_text = match.group(2)
        else:
            start = raw.find("{")
            end = raw.rfind("}")
            if start < 0 or end <= start:
                return None
            json_text = raw[start : end + 1]
        try:
            payload = json.loads(json_text.strip())
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    async def _check_qzone_accessible(self, event: AstrMessageEvent, user_id: str) -> Dict[str, Any]:
        qq = self._normalize_digits(user_id)
        try:
            cookie_result = await self._call_action(event, "get_cookies", domain="user.qzone.qq.com")
            cookie_str = str(cookie_result.get("cookies") or "") if isinstance(cookie_result, dict) else str(cookie_result or "")
            cookies = {k: v.value for k, v in SimpleCookie(cookie_str).items()}
            skey = cookies.get("p_skey") or cookies.get("skey") or ""
            login_uin = str(cookies.get("uin") or "").lstrip("o")
            if not skey:
                return {"accessible": False, "error": "missing_cookie", "cookie_keys": sorted(cookies.keys())}
            gtk = self._generate_qzone_gtk(skey)
            params = {
                "uin": qq,
                "ftype": 0,
                "sort": 0,
                "pos": 0,
                "num": 10,
                "g_tk": gtk,
                "g_tk_2": gtk,
                "format": "json",
                "qzreferrer": f"{QZONE_BASE_URL}/{login_uin or qq}",
            }
            headers = {
                "referer": f"{QZONE_BASE_URL}/{qq}",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            timeout = aiohttp.ClientTimeout(total=self.settings["post_join_qzone_timeout_seconds"])
            async with aiohttp.ClientSession(timeout=timeout, cookies=cookies, headers=headers) as session:
                async with session.get(QZONE_FEED_LIST_URL, params=params) as resp:
                    http_status = resp.status
                    text = await resp.text(errors="replace")
            payload = self._extract_qzone_payload(text)
            code = payload.get("code") if isinstance(payload, dict) else None
            subcode = payload.get("subcode") if isinstance(payload, dict) else None
            message = ""
            if isinstance(payload, dict):
                message = " ".join(
                    self._brief_value(payload.get(key), 160)
                    for key in ("message", "msg", "errormsg", "submessage")
                    if payload.get(key) is not None
                )
            blocked_text = QZONE_REPORTED_BLOCK_TEXT in "\n".join([text, message])
            accessible = http_status == 200 and code == 0 and subcode == 0 and not blocked_text
            return {
                "accessible": accessible,
                "http_status": http_status,
                "code": code,
                "subcode": subcode,
                "message": message,
                "reported_blocked": blocked_text,
            }
        except Exception as e:
            return {"accessible": False, "error": str(e)}
 
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

    def _match_threat_name_keyword(self, nickname: str) -> Optional[str]:
        text = str(nickname or "")
        if not text:
            return None
        case_sensitive = self.settings["threat_name_case_sensitive"]
        haystack = text if case_sensitive else text.lower()
        for keyword in self.settings["threat_name_keywords"]:
            needle = str(keyword or "")
            if not needle:
                continue
            if (needle if case_sensitive else needle.lower()) in haystack:
                return keyword
        return None

    @staticmethod
    def _brief_value(value: Any, limit: int = 160) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
        text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    @staticmethod
    def _has_effective_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        text = str(value).strip()
        return bool(text and text not in {"0", "-", "--", "未知", "保密", "未填", "unknown", "None", "null"})

    def _format_profile_debug(self, user_id: str, data: Any) -> str:
        if not isinstance(data, dict):
            return f"资料调试：{user_id}\n返回类型：{type(data).__name__}\n原始值：{self._brief_value(data, 600)}"

        sex = data.get("sex")
        birthday_keys = ["birthday_year", "birthday_month", "birthday_day"]
        birthday = "-".join(str(data.get(k) or "") for k in birthday_keys)
        has_birthday_for_constellation = self._has_effective_value(data.get("birthday_month")) and self._has_effective_value(data.get("birthday_day"))
        company_like_keys = [
            key
            for key in data.keys()
            if any(token in str(key).lower() for token in ("company", "corp", "work", "business", "employ", "office"))
            or any(token in str(key) for token in ("公司", "单位", "企业"))
        ]
        interesting_keys = [
            "user_id",
            "nickname",
            "nick",
            "qqLevel",
            "qq_level",
            "sex",
            "age",
            "birthday_year",
            "birthday_month",
            "birthday_day",
            "long_nick",
            "reg_time",
            "phoneNum",
            "eMail",
            "country",
            "province",
            "city",
            "homeTown",
            "address",
            "kBloodType",
            "makeFriendCareer",
            "career",
            "profession",
            "labels",
            "remark",
        ]
        for key in company_like_keys:
            if key not in interesting_keys:
                interesting_keys.append(key)

        lines = [
            f"资料调试：{user_id}",
            f"字段数量：{len(data)}",
            f"性别原始值：{self._brief_value(sex) or '空'}（{'视为有性别' if str(sex).lower() in {'male', 'female'} else '视为无性别'}）",
            f"生日字段：{birthday}（{'可推导星座' if has_birthday_for_constellation else '视为无星座'}）",
            f"疑似公司字段：{', '.join(company_like_keys) if company_like_keys else '未发现'}",
            "关键字段：",
        ]
        for key in interesting_keys:
            if key in data:
                value = data.get(key)
                marker = "有效" if self._has_effective_value(value) else "空/无效"
                lines.append(f"- {key}: {self._brief_value(value) or '空'}（{marker}）")

        other_keys = [str(key) for key in data.keys() if key not in interesting_keys]
        lines.append(f"其他字段名：{', '.join(other_keys[:80]) if other_keys else '无'}")
        if len(other_keys) > 80:
            lines.append(f"其他字段还有 {len(other_keys) - 80} 个未显示。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 管理员调试命令
    # ------------------------------------------------------------------
    @filter.command_group("审批检查", alias={"joininspector", "入群审批"})
    def inspector_admin(self):
        """入群审批检查器管理员调试命令组。"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @inspector_admin.command("资料调试")
    async def cmd_profile_debug(self, event: AstrMessageEvent, user_id: str):
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("当前平台不是 aiocqhttp，无法查询 QQ 资料。")
            return
        qq = self._normalize_digits(user_id)
        if not qq:
            yield event.plain_result("请输入有效 QQ 号。")
            return
        try:
            data = await self._call_action(
                event,
                "get_stranger_info",
                user_id=int(qq),
                no_cache=True,
            )
            self._append_action_log(
                {
                    "action": "profile_debug",
                    "user_id": qq,
                    "keys": list(data.keys()) if isinstance(data, dict) else [],
                    "raw": data,
                }
            )
            yield event.plain_result(self._format_profile_debug(qq, data))
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 资料调试 {qq} 失败：{e}", exc_info=True)
            yield event.plain_result(f"资料调试失败：{e}")
 
    @staticmethod
    def _pending_key(group_id: str, user_id: str) -> str:
        return f"{group_id}:{user_id}"

    def _clear_pending_verification(self, group_id: str, user_id: str) -> None:
        key = self._pending_key(group_id, user_id)
        self.pending_verifications.pop(key, None)
        task = self.verification_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    @staticmethod
    def _extract_numeric_answer(text: str) -> Optional[str]:
        raw = str(text or "").strip()
        if not raw:
            return None
        numbers = re.findall(r"-?\d+", raw)
        if len(numbers) != 1:
            return None
        return numbers[0]

    @staticmethod
    def _generate_math_question() -> tuple:
        op = random.choice(["+", "-", "×"])
        if op == "+":
            a = random.randint(6, 40)
            b = random.randint(3, 30)
            return f"{a} + {b}", a + b
        if op == "-":
            a = random.randint(20, 60)
            b = random.randint(3, min(30, a - 1))
            return f"{a} - {b}", a - b
        a = random.randint(3, 12)
        b = random.randint(3, 12)
        return f"{a} × {b}", a * b

    @staticmethod
    def _format_template(template: str, **kwargs: Any) -> str:
        try:
            return str(template or "").format(**kwargs)
        except Exception:
            return str(template or "")

    async def _send_group_message(self, event: AstrMessageEvent, group_id: str, message: str) -> None:
        if not message:
            return
        await self._call_action(event, "send_group_msg", group_id=int(group_id), message=message)

    async def _kick_user(
        self, event: AstrMessageEvent, group_id: str, user_id: str, reject: bool = False
    ) -> None:
        await self._call_action(
            event,
            "set_group_kick",
            group_id=int(group_id),
            user_id=int(user_id),
            reject_add_request=bool(reject),
        )
 
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
