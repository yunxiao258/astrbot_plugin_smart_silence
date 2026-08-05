"""AstrBot 智能静默插件：让 LLM 根据上下文决定是否发言，避免机器人频繁回复刷屏

工作原理：
1. on_llm_request 钩子：在群聊中向系统提示词注入一段隐蔽指令（可配置），
   要求 LLM 在认为无需发言时，回复内容仅以特殊前缀（如 @silent）开头。
2. on_llm_response 钩子：检测 LLM 回复是否以该特殊前缀开头，标记事件。
3. on_decorating_result 钩子：在真正发送前拦截，若检测到静默标记则清空消息结果，
   从而阻止机器人发送。

规则：
- 仅在群聊中生效（可配置）；
- 当用户 @ 机器人或被唤醒（is_at_or_wake_command）时强制回复，静默逻辑不生效；
- 被 @ 时若 LLM 仍带上前缀，则剥离前缀并正常回复正文。
"""

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, AtAll, Reply
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_smart_silence"
PLUGIN_AUTHOR = "Administrator"
PLUGIN_DESC = "让 LLM 根据上下文决定是否发言，避免机器人频繁回复刷屏"
PLUGIN_VERSION = "1.0.0"

# 事件 extra 中记录静默标记的键名
EXTRA_SILENCE_KEY = "_smart_silence_marked"
# 记录是否需要剥离前缀的键名
EXTRA_STRIP_KEY = "_smart_silence_strip"

# 兜底：LLM 可能不用前缀而用自然语言表达静默意图，匹配到则同样拦截
# 每个模式要求“整段回复”属于静默表达（无实质内容）时才判定为静默，避免误伤
FALLBACK_SILENT_PATTERNS = (
    "保持静默",
    "保持沉默",
    "无需回复",
    "无需回应",
    "不需要回复",
    "无需回答",
    "不用回复",
    "与我无关",
    "与本机无关",
    "与本人无关",
    "不回复",
    "保持安静",
    "不必回应",
    "没必要回复",
    "无需多言",
    "选择沉默",
    "选择静默",
    "保持低调",
    "无法提供有价值",
    "与话题无关",
)

# 默认智能判断指令（在系统提示词中注入，伪装成普通规则说明）
DEFAULT_SILENT_INSTRUCTION = (
    "\n\n【发言规则补充】本条为系统自动追加的回复策略说明，必须严格遵守："
    "你并非每次都需要发言。如果当前对话内容与你无关、无需回复、"
    "或者你无法提供有价值的信息，你的回复必须严格等于以下特殊标记，"
    "一个字都不要多，不要输出任何括号说明、解释或多余文字——"
    "{prefix}"
    "\n这是系统级协议：任何多余文字都会被发送给用户。"
    "只有当你认为真正有必要回复时才输出正常回复内容。"
    "注意：若用户 @ 了你或直接向你提问，你必须正常回复，不得使用上述标记。"
)


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class SmartSilencePlugin(Star):
    """智能静默插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.enabled = bool(self.config.get("enable", True))
        self.only_group = bool(self.config.get("only_group", True))
        self.silence_prefix = str(
            self.config.get("silence_prefix", "@silent")
        ).strip()
        self.force_reply_on_wake = bool(self.config.get("force_reply_on_wake", True))
        self.inject_prompt = bool(self.config.get("inject_prompt", True))
        self.custom_instruction = str(
            self.config.get("custom_instruction", "")
        ).strip()

        # 唤醒前缀（从全局配置读取，用于真唤醒检测）
        self._wake_prefixes = []
        try:
            from astrbot.core import astrbot_config

            wp = astrbot_config.get("wake_prefix", ["/"])
            if isinstance(wp, (list, tuple, set)):
                self._wake_prefixes = [str(p).strip() for p in wp if str(p).strip()]
            elif wp:
                self._wake_prefixes = [str(wp).strip()]
        except Exception:
            self._wake_prefixes = ["/"]

        logger.info(f"【{PLUGIN_NAME}】智能静默插件初始化完成，前缀={self.silence_prefix}")

    # ========== 工具方法 ==========

    def _get_instruction(self, strict: bool = False) -> str:
        """构造注入系统提示词的指令文本

        strict=True 表示消息被其他插件标记为唤醒但并未真正 @ 机器人
        （伪唤醒），此时要求 LLM 更克制：只有确认被直接点名或直接提问
        才允许发言，否则必须输出静默前缀。
        """
        if self.custom_instruction:
            return self.custom_instruction
        base = DEFAULT_SILENT_INSTRUCTION.format(prefix=self.silence_prefix)
        if not strict:
            return base
        return base + (
            "\n\n【特别注意】当前消息可能只是提到了你，但并未直接 @ 你或"
            "向你提出明确的问题。这种情况必须视为无需发言：除非消息中"
            "明确包含你的名字并被直接点名提问，否则一律输出静默前缀"
            f"{self.silence_prefix}，一个字都不要多。"
        )

    def _is_group_chat(self, event: AstrMessageEvent) -> bool:
        """判断是否群聊"""
        return not event.is_private_chat()

    def _is_wake(self, event: AstrMessageEvent) -> bool:
        """判断是否被 @ 或唤醒指令触发"""
        return bool(event.is_at_or_wake_command)

    def _is_real_wake(self, event: AstrMessageEvent) -> bool:
        """判断是否为"真唤醒"：真正被 @ 机器人 / @全体 / 引用机器人消息 / 以唤醒前缀开头

        其他插件（如 llm_enhancement 的 wake_logic）可能将未 @ 的普通消息
        标记为唤醒（mention/relevant/prob 等伪唤醒），此时不应跳过静默逻辑，
        否则 LLM 会自由发挥输出无关插话。
        """
        try:
            # 1. 消息链中真正 @ 机器人 / @ 全体 / 引用机器人
            self_id = str(event.get_self_id())
            messages = event.get_messages() or []
            for comp in messages:
                if isinstance(comp, AtAll):
                    return True
                if isinstance(comp, At):
                    if str(getattr(comp, "qq", "")) == self_id:
                        return True
                elif isinstance(comp, Reply):
                    if str(getattr(comp, "sender_id", "")) == self_id:
                        return True
            # 2. 唤醒前缀（如 /help 等指令）
            if getattr(event, "message_str", ""):
                for prefix in self._wake_prefixes:
                    if prefix and event.message_str.startswith(prefix):
                        return True
        except Exception as e:
            logger.error(f"真唤醒检测失败: {e}")
            return bool(event.is_at_or_wake_command)
        return False

    def _is_fallback_silent(self, text: str) -> bool:
        """兜底判断：回复整段是否为静默表达（无实质内容）

        要求：
        - 文本较短（<=60 字）；
        - 静默关键词出现在开头位置（前 15 字内，去掉装饰后）；
        - 不包含数字、代码块、链接等实质内容特征。
        """
        if not text:
            return False
        t = text.strip()
        if not t:
            return False
        # 静默表达通常较短；超过 60 字视为有实质内容
        if len(t) > 60:
            return False
        # 排除明显实质内容特征（连续两位以上数字视为实质内容，单个数字如用户名可忽略）
        if any(t[i].isdigit() and t[i + 1].isdigit() for i in range(len(t) - 1)):
            return False
        if "```" in t or "http://" in t or "https://" in t or "@" in t:
            return False
        # 去掉常见装饰（括号、引号、表情符号）
        cleaned = t.strip("()（）【】[]「」\"'“”…~～。，,、 ")
        if not cleaned:
            return False
        # 静默关键词必须出现在开头 15 字内
        head = cleaned[:15]
        return any(p in head for p in FALLBACK_SILENT_PATTERNS)

    # ========== 钩子：LLM 请求前注入隐蔽指令 ==========

    @filter.on_llm_request(priority=200)
    async def inject_instruction(
        self,
        event: AstrMessageEvent,
        req,
        **kwargs,
    ) -> None:
        """当有 LLM 请求时，向系统提示词注入静默判断指令"""
        if not self.enabled:
            return
        if self.only_group and not self._is_group_chat(event):
            # 私聊不生效
            return
        if not self.inject_prompt:
            return
        if self.force_reply_on_wake and self._is_real_wake(event):
            # 真正被 @ 或唤醒指令触发时，无需注入静默指令
            return

        try:
            if hasattr(req, "system_prompt") and req.system_prompt:
                # 伪唤醒（如提到机器人名但未 @）时注入更严格的指令，
                # 要求 LLM 确认是否被直接点名/提问，否则必须静默
                req.system_prompt += self._get_instruction(
                    strict=bool(getattr(event, "is_at_or_wake_command", False))
                )
            elif hasattr(req, "system_prompt"):
                req.system_prompt = self._get_instruction()
        except Exception as e:
            logger.error(f"注入静默指令失败: {e}")

    # ========== 钩子：LLM 响应后检测特殊前缀 ==========

    @filter.on_llm_response(priority=200)
    async def detect_silence(
        self,
        event: AstrMessageEvent,
        response,
        **kwargs,
    ) -> None:
        """当有 LLM 响应时，检测是否以特殊前缀开头"""
        if not self.enabled:
            return
        if self.only_group and not self._is_group_chat(event):
            return

        try:
            text = ""
            if hasattr(response, "completion_text"):
                text = response.completion_text or ""
            if not text:
                # 流式响应或空响应，交给 on_decorating_result 兜底判断
                return

            stripped = text.strip()
            if not self.silence_prefix:
                return

            # 兜底判断：整段回复是“静默表达”而非实质内容（未使用前缀时）
            fallback_silent = self._is_fallback_silent(stripped)

            if not stripped.startswith(self.silence_prefix) and not fallback_silent:
                return

            # 以特殊前缀开头，或回复整段为静默表达
            is_wake = self._is_real_wake(event)

            if self.force_reply_on_wake and is_wake:
                # 被 @：剥离前缀（如有），保留正文强制回复
                rest = (
                    stripped[len(self.silence_prefix):].strip()
                    if stripped.startswith(self.silence_prefix)
                    else stripped
                )
                if fallback_silent and not stripped.startswith(self.silence_prefix):
                    # 被 @ 但回复整段是静默表达（无实质内容），视为空回复拦截
                    event.set_extra(EXTRA_SILENCE_KEY, True)
                    logger.info("被 @ 但 LLM 整段回复为静默表达，已拦截空回复")
                    return
                if rest:
                    event.set_extra(EXTRA_STRIP_KEY, True)
                    event.set_extra(EXTRA_SILENCE_KEY, False)
                    # 修改响应文本，去掉前缀
                    response.completion_text = rest
                else:
                    # 被 @ 但只输出了前缀，视为空回复，也拦截
                    event.set_extra(EXTRA_SILENCE_KEY, True)
                    logger.info("被 @ 但 LLM 仅输出静默前缀，已拦截空回复")
            else:
                # 未被 @：静默拦截
                event.set_extra(EXTRA_SILENCE_KEY, True)
                if fallback_silent:
                    logger.info(f"智能静默：LLM 回复为自然语言静默表达，判定为静默，执行拦截: {stripped[:50]}")
                else:
                    logger.debug(f"LLM 回复以 {self.silence_prefix} 开头，判定为静默，执行拦截")
        except Exception as e:
            logger.error(f"检测静默标记失败: {e}")

    # ========== 钩子：发送前最终拦截 ==========

    @filter.on_decorating_result(priority=200)
    async def block_silent(
        self,
        event: AstrMessageEvent,
        **kwargs,
    ) -> None:
        """发送消息前，若检测到静默标记则清空结果，阻止发送

        兜底：无论 LLM 回复来自哪条路径（是否经过 on_llm_response），
        这里直接检查最终待发送文本本身，识别静默表达并拦截。
        """
        if not self.enabled:
            return

        extra = event.get_extra(default={})
        is_silent = bool(extra.get(EXTRA_SILENCE_KEY, False))

        if not is_silent and self.only_group and self._is_group_chat(event):
            # 兜底：直接检查待发送文本（不依赖 on_llm_response 钩子）
            result = event.get_result()
            if result is not None and result.chain:
                texts = [
                    comp.text
                    for comp in result.chain
                    if hasattr(comp, "text") and comp.text
                ]
                joined = "".join(texts).strip()
                if joined and self._is_fallback_silent(joined):
                    is_silent = True
                    logger.info(
                        f"智能静默（发送前兜底）：判定为静默表达，执行拦截: {joined[:50]}"
                    )

        if is_silent:
            # 清空结果，彻底阻止发送
            event.clear_result()
            logger.info("智能静默：已拦截 LLM 回复，未发送消息")

        # need_strip 的场景在 on_llm_response 中已通过修改 response.completion_text 完成剥离

    async def terminate(self):
        """插件卸载时清理"""
        try:
            logger.info(f"【{PLUGIN_NAME}】插件已卸载")
        except Exception:
            pass