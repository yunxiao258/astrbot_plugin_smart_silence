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

v1.1.0 优化点：
- 静默判定逻辑抽为模块级纯函数（is_fallback_silent / decide_silence_action），便于单元测试；
- 发送前兜底增强：流式输出或 on_llm_response 漏检时，直接识别静默前缀并处理
  （未 @ 则拦截；被 @ 则剥离前缀后发送），不再依赖单一钩子路径；
- 注入去重：系统提示词已包含隐藏标记时不再重复注入；
- 自定义指令支持 {prefix} 占位符；
- 全局唤醒前缀为空时兜底使用 "/"；
- 纯装饰文本（括号/引号/表情等无实质内容）视为静默。

v1.2.0 优化点：
- 更强的默认克制：未真正 @ 机器人时一律注入严格指令（不再依赖他插件唤醒标记），
  显著减少无关插话；
- 按群策略：白名单群（必回复，静默全失效）与黑名单群（严格静默，未 @ 一律拦截）；
- 拦截统计：按群累计拦截次数并持久化到 stats.json，提供 /静默 状态 命令查询。

v1.3.0 优化点：
- 硬阻止未点名消息：未 @ 机器人且消息未点名机器人（名字在 bot_names 中）时，
  直接 stop_event 阻止 LLM 调用，完全不回复（完全静默，仅记统计）；
- 点名但未 @ 的消息放行：注入严格指令，由 LLM 判断是否确属点名提问；
- 黑名单群未 @ 时同样在请求阶段直接阻止，不再浪费 LLM 调用；
- 新增配置 hard_block_unmentioned（默认开启）与 bot_names（机器人名字列表）。

v1.4.0 优化点：
- 临时暂停：/静默 暂停 [N] 暂停 N 分钟（默认 30），期间不阻止不拦截不注入，
  到期自动恢复；/静默 恢复 立即恢复；暂停状态持久化，重启仍有效；
- 按天趋势：拦截统计增加按天记录，/静默 状态 显示近 7 天趋势。

v1.5.0 优化点：
- 多机器人配置隔离：一个 AstrBot 实例挂多个机器人账号时，
  通过 per_bot_config（JSON）按 self_id 为每个机器人单独覆盖全部配置项，
  未覆盖的项回退到全局配置，避免各机器人配置错乱。

v1.6.0 优化点：
- 其他机器人消息特殊对待：bot_accounts 配置本实例其他机器人账号，
  识别到机器人消息时不再硬阻止，而是注入专属规则（bot_message_rule，可自定义，
  支持 {prefix}/{bot_name} 变量）告知 LLM 这是机器人消息，按规则回复；
- 全部配置项均支持 per_bot_config 按机器人覆盖。
"""

from datetime import date, timedelta
import json
import os
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, AtAll, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_smart_silence"
PLUGIN_AUTHOR = "Administrator"
PLUGIN_DESC = "让 LLM 根据上下文决定是否发言，避免机器人频繁回复刷屏"
PLUGIN_VERSION = "1.6.0"

# 事件 extra 中记录静默标记的键名
EXTRA_SILENCE_KEY = "_smart_silence_marked"
# 记录是否需要剥离前缀的键名
EXTRA_STRIP_KEY = "_smart_silence_strip"
# 注入指令时附带、用于检测是否已注入的隐藏标记
INJECT_MARKER = "\n\n<!-- smart-silence-rule-active -->"

# per_bot_config 中允许覆盖的配置键白名单
PER_BOT_KEYS = (
    "enable",
    "only_group",
    "silence_prefix",
    "force_reply_on_wake",
    "inject_prompt",
    "custom_instruction",
    "reply_always_groups",
    "silence_always_groups",
    "hard_block_unmentioned",
    "bot_names",
    "bot_accounts",
    "bot_message_rule",
)

# 默认机器人消息规则（注入给 LLM，告知消息来自其他机器人）
DEFAULT_BOT_RULE = (
    "\n\n【机器人消息提示】本条消息来自另一个机器人账号（同属本机其他机器人），"
    "并非真人用户。请按以下规则处理：仅在对方明确提出需要你的帮助或信息时回复，"
    "回答保持简短克制；若只是通知、状态同步、任务交接或闲聊，"
    "你的回复必须严格等于以下特殊标记——{prefix}，一个字都不要多。"
)

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


def is_fallback_silent(text: str) -> bool:
    """兜底判断：回复整段是否为静默表达（无实质内容）

    要求：
    - 文本较短（<=60 字）；
    - 静默关键词出现在开头位置（前 15 字内，去掉装饰后）；
    - 不包含数字、代码块、链接等实质内容特征。
    同时：纯装饰文本（无实质内容）也视为静默。
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
        # 整段仅为装饰符号（如“……”、“~”），无实质内容，视为静默
        return True
    # 静默关键词必须出现在开头 15 字内
    head = cleaned[:15]
    return any(p in head for p in FALLBACK_SILENT_PATTERNS)


def decide_silence_action(
    text: str,
    prefix: str,
    is_wake: bool,
    force_reply_on_wake: bool,
) -> tuple:
    """统一静默判定，返回 (action, rest)

    action 取值：
    - "send"：正常发送；
    - "strip"：剥离静默前缀后发送（rest 为剥离后的正文）；
    - "silent"：拦截，不发送。

    判定规则：
    - text 为空 → silent（空回复拦截）；
    - prefix 为空（未配置前缀）→ send（禁用前缀静默，仅保留兜底语义）；
    - 不以前缀开头且非自然语言静默表达 → send；
    - 命中静默（前缀开头或整段为静默表达）时：
      - 被唤醒且强制回复：带前缀则 strip（rest 为空则 silent），
        整段静默表达视为空回复 → silent；
      - 否则 → silent。
    """
    if not text:
        return "silent", ""
    if not prefix:
        return "send", text
    starts = text.startswith(prefix)
    fallback_silent = not starts and is_fallback_silent(text)
    if not starts and not fallback_silent:
        return "send", text
    if force_reply_on_wake and is_wake:
        if starts:
            rest = text[len(prefix):].strip()
            return ("strip" if rest else "silent"), rest
        # 被 @ 但整段回复为静默表达（无实质内容），视为空回复拦截
        return "silent", text
    return "silent", text


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
        # 硬阻止：未 @ 且未点名时直接阻止 LLM 调用（完全不回复）
        self.hard_block_unmentioned = bool(
            self.config.get("hard_block_unmentioned", True)
        )
        # 机器人名字（用于点名检测），多个用英文逗号分隔
        self.bot_names = self._parse_group_list(self.config.get("bot_names", ""))
        # 白名单群（必回复，静默全失效）/ 黑名单群（严格静默，未 @ 一律拦截）
        self.reply_always_groups = self._parse_group_list(
            self.config.get("reply_always_groups", "")
        )
        self.silence_always_groups = self._parse_group_list(
            self.config.get("silence_always_groups", "")
        )
        # 本实例其他机器人账号（消息来自这些账号时按机器人规则处理）
        self.bot_accounts = self._parse_group_list(
            self.config.get("bot_accounts", "")
        )
        # 机器人消息规则文案（留空使用默认文案，支持 {prefix}/{bot_name} 变量）
        self.bot_message_rule = str(
            self.config.get("bot_message_rule", "")
        ).strip()

        # 多机器人配置隔离：按 self_id 覆盖配置（未覆盖的键回退到全局配置）
        self._bot_ctx = {}
        raw_pbc = str(self.config.get("per_bot_config", "") or "").strip()
        if raw_pbc:
            try:
                data = json.loads(raw_pbc)
                if isinstance(data, dict):
                    for bid, cfg in data.items():
                        if not isinstance(cfg, dict):
                            continue
                        self._bot_ctx[str(bid).strip()] = {
                            k: v for k, v in cfg.items() if k in PER_BOT_KEYS
                        }
                    logger.info(
                        f"【{PLUGIN_NAME}】已加载 {len(self._bot_ctx)} 个机器人的独立配置"
                    )
            except Exception as e:
                logger.error(f"解析 per_bot_config 失败，已忽略: {e}")

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
            self._wake_prefixes = []
        if not self._wake_prefixes:
            # 全局唤醒前缀为空时兜底使用 "/"，避免指令类消息被误判为无需回复
            self._wake_prefixes = ["/"]

        # 拦截统计（按群/按天累计，持久化到插件目录 stats.json）
        self.stats_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "stats.json"
        )
        self.stats = {"total": 0, "today": 0, "date": "", "days": {}, "groups": {}}
        self._load_stats()
        # 暂停截止时间戳（暂停期间不阻止不拦截不注入），从持久化恢复
        self.pause_until = None
        p = self.stats.get("pause_until")
        if isinstance(p, (int, float)) and p > time.time():
            self.pause_until = p

        logger.info(f"【{PLUGIN_NAME}】智能静默插件初始化完成，前缀={self.silence_prefix}")

    # ========== 工具方法 ==========

    @staticmethod
    def _parse_group_list(raw) -> set:
        """解析逗号分隔的群 ID 列表"""
        if isinstance(raw, (list, tuple, set)):
            return {str(x).strip() for x in raw if str(x).strip()}
        return {x.strip() for x in str(raw or "").split(",") if x.strip()}

    def _v(self, event: AstrMessageEvent, key: str):
        """当前机器人（self_id）对该配置键的覆盖值；未覆盖返回 None"""
        try:
            ctx = self._bot_ctx.get(str(event.get_self_id()))
            if ctx and key in ctx:
                return ctx[key]
        except Exception:
            pass
        return None

    def _cfg(self, event: AstrMessageEvent, key: str, default):
        """按机器人取配置值（优先 per_bot 覆盖，否则全局默认）"""
        v = self._v(event, key)
        return default if v is None else v

    def _bot_accounts_for(self, event: AstrMessageEvent) -> set:
        """当前机器人视角下的“其他机器人账号”列表"""
        return self._parse_group_list(
            self._cfg(event, "bot_accounts", self.bot_accounts)
        )

    def _is_bot_message(self, event: AstrMessageEvent) -> bool:
        """消息发送者是否为配置的其他机器人账号"""
        accounts = self._bot_accounts_for(event)
        if not accounts:
            return False
        try:
            sid = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
        except Exception:
            return False
        return bool(sid) and sid in accounts and sid != self_id

    def _load_stats(self):
        """从磁盘加载统计（失败则忽略，保持内存计数）"""
        try:
            if os.path.exists(self.stats_path):
                with open(self.stats_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.stats.update(data)
                # 跨天时重置“今日”计数
                if self.stats.get("date") != date.today().isoformat():
                    self.stats["today"] = 0
                    self.stats["date"] = date.today().isoformat()
        except Exception as e:
            logger.error(f"加载静默统计失败: {e}")

    def _save_stats(self):
        """保存统计到磁盘（失败则忽略）"""
        try:
            os.makedirs(os.path.dirname(self.stats_path) or ".", exist_ok=True)
            tmp = self.stats_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.stats_path)
        except Exception as e:
            logger.error(f"保存静默统计失败: {e}")

    def _record_block(self, event: AstrMessageEvent):
        """记录一次拦截（按群/按天累计）"""
        self.stats["total"] += 1
        today = date.today().isoformat()
        if self.stats.get("date") != today:
            self.stats["today"] = 0
            self.stats["date"] = today
        self.stats["today"] += 1
        days = self.stats.setdefault("days", {})
        days[today] = days.get(today, 0) + 1
        gid = "private"
        try:
            gid = str(event.get_group_id() or gid)
        except Exception:
            pass
        groups = self.stats.setdefault("groups", {})
        groups[gid] = groups.get(gid, 0) + 1
        self._save_stats()

    def _is_paused(self) -> bool:
        """是否处于暂停状态（到期自动恢复）"""
        if self.pause_until is None:
            return False
        if time.time() >= self.pause_until:
            # 暂停已到期，自动恢复
            self.pause_until = None
            self.stats.pop("pause_until", None)
            self._save_stats()
            logger.info("智能静默：暂停时间已到，自动恢复")
            return False
        return True

    def _pause(self, minutes: float):
        """暂停静默 N 分钟"""
        self.pause_until = time.time() + max(1.0, float(minutes)) * 60
        self.stats["pause_until"] = self.pause_until
        self._save_stats()

    def _resume(self):
        """立即恢复静默"""
        self.pause_until = None
        self.stats.pop("pause_until", None)
        self._save_stats()

    def _group_id(self, event: AstrMessageEvent) -> str:
        """获取群 ID（私聊返回空串）"""
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    def _in_always_reply_group(self, event: AstrMessageEvent) -> bool:
        """是否命中白名单（必回复群，按机器人覆盖）"""
        groups = self._parse_group_list(
            self._cfg(event, "reply_always_groups", self.reply_always_groups)
        )
        return self._group_id(event) in groups

    def _in_always_silent_group(self, event: AstrMessageEvent) -> bool:
        """是否命中黑名单（严格静默群，按机器人覆盖）"""
        groups = self._parse_group_list(
            self._cfg(event, "silence_always_groups", self.silence_always_groups)
        )
        return self._group_id(event) in groups

    def _is_named(self, event: AstrMessageEvent) -> bool:
        """消息文本是否点名机器人（包含配置的机器人名字，按机器人覆盖）"""
        names = self._parse_group_list(
            self._cfg(event, "bot_names", self.bot_names)
        )
        if not names:
            return False
        text = event.message_str or ""
        for name in names:
            if name and name in text:
                return True
        return False

    def _get_instruction(self, event: AstrMessageEvent, strict: bool = False) -> str:
        """构造注入系统提示词的指令文本

        strict=True 表示消息未真正 @ 机器人（普通群消息/伪唤醒），
        此时要求 LLM 更克制：只有确认被直接点名或直接提问
        才允许发言，否则必须输出静默前缀。
        """
        custom = str(
            self._cfg(event, "custom_instruction", self.custom_instruction)
        ).strip()
        prefix = str(
            self._cfg(event, "silence_prefix", self.silence_prefix)
        ).strip()
        if custom:
            # 支持 {prefix} 占位符，替换失败（无占位符）则按原文返回
            return custom.replace("{prefix}", prefix).strip() + INJECT_MARKER
        base = DEFAULT_SILENT_INSTRUCTION.format(prefix=prefix)
        if not strict:
            return base + INJECT_MARKER
        return base + (
            "\n\n【特别注意】当前消息并未直接 @ 你。群聊中的绝大多数消息"
            "都不需要你发言：除非消息明确点名提问的是你，"
            "或你确实能提供对方明确需要且无法从别处获得的高价值信息，"
            "否则一律输出静默前缀"
            f"{prefix}，一个字都不要多，禁止自由发挥插话。"
        ) + INJECT_MARKER

    def _bot_rule_text(self, event: AstrMessageEvent) -> str:
        """构造注入给 LLM 的“机器人消息”规则文本（可自定义，支持变量）"""
        rule = str(
            self._cfg(event, "bot_message_rule", self.bot_message_rule)
        ).strip()
        if not rule:
            rule = DEFAULT_BOT_RULE
        prefix = str(
            self._cfg(event, "silence_prefix", self.silence_prefix)
        ).strip()
        names = self._parse_group_list(
            self._cfg(event, "bot_names", self.bot_names)
        )
        bot_name = next(iter(names), "本机器人")
        return rule.replace("{prefix}", prefix).replace(
            "{bot_name}", bot_name
        ).strip() + INJECT_MARKER

    def _is_group_chat(self, event: AstrMessageEvent) -> bool:
        """判断是否群聊"""
        return not event.is_private_chat()

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
            # 2. 唤醒前缀（如 /help 等指令）。
            #    waking_check 会在剥离前缀后重写 event.message_str，
            #    而 event.message_obj.message_str 保留原始完整消息，
            #    因此优先用原始文本检测，避免 /指令 被误判为未唤醒。
            raw = getattr(event, "message_str", "") or ""
            obj = getattr(event, "message_obj", None)
            if obj is not None:
                obj_raw = getattr(obj, "message_str", None)
                if obj_raw:
                    raw = obj_raw
            if raw:
                for prefix in self._wake_prefixes:
                    if prefix and raw.startswith(prefix):
                        return True
        except Exception as e:
            logger.error(f"真唤醒检测失败: {e}")
            return bool(event.is_at_or_wake_command)
        return False

    # ========== 钩子：LLM 请求前注入隐蔽指令 ==========

    @filter.on_llm_request(priority=200)
    async def inject_instruction(
        self,
        event: AstrMessageEvent,
        req,
        **kwargs,
    ) -> None:
        """当有 LLM 请求时，向系统提示词注入静默判断指令（或直接阻止未点名的请求）"""
        enabled = self._cfg(event, "enable", self.enabled)
        only_group = self._cfg(event, "only_group", self.only_group)
        inject_prompt = self._cfg(event, "inject_prompt", self.inject_prompt)
        force_wake = self._cfg(
            event, "force_reply_on_wake", self.force_reply_on_wake
        )
        hard_block = self._cfg(
            event, "hard_block_unmentioned", self.hard_block_unmentioned
        )
        if not enabled or self._is_paused():
            return
        if only_group and not self._is_group_chat(event):
            # 私聊不生效
            return
        if self._in_always_reply_group(event):
            # 白名单群：必回复，不注入静默指令
            return
        if force_wake and self._is_real_wake(event):
            # 真正被 @ 或唤醒指令触发时，无需注入静默指令
            return

        is_bot_msg = self._is_bot_message(event)

        # 未真正唤醒：判断是否需要硬阻止 LLM 调用
        if not self._is_real_wake(event):
            in_black = self._in_always_silent_group(event)
            if in_black:
                # 黑名单群（严格静默）：未 @ 一律阻止，无论谁发的
                self._stop_request(event)
                return
            # 被其他插件明确标记为唤醒（伪唤醒，如提及/相关/指令触发词）时
            # 不硬阻止，交给 LLM 按 strict 指令克制判断，避免误伤指令类消息
            pseudo_wake = bool(getattr(event, "is_at_or_wake_command", False))
            # 机器人消息跳过“未点名硬阻止”，交给 LLM 按机器人规则判断
            if (
                not is_bot_msg
                and not pseudo_wake
                and hard_block
                and not self._is_named(event)
            ):
                self._stop_request(event)
                return

        if not inject_prompt:
            return

        try:
            if hasattr(req, "system_prompt"):
                prompt = req.system_prompt or ""
                if INJECT_MARKER in prompt:
                    # 已注入过（同一条消息可能多次触发 on_llm_request），避免重复
                    return
                if is_bot_msg:
                    # 其他机器人消息：注入专属规则，告知 LLM 按规则回复
                    req.system_prompt = prompt + self._bot_rule_text(event)
                else:
                    # 点名但未真正 @ 机器人时注入严格指令，
                    # 要求 LLM 确认被直接点名/提问才允许发言，减少无关插话
                    req.system_prompt = prompt + self._get_instruction(
                        event, strict=True
                    )
        except Exception as e:
            logger.error(f"注入静默指令失败: {e}")

    def _stop_request(self, event: AstrMessageEvent):
        """阻止 LLM 调用并记录统计"""
        try:
            event.stop_event()
        except Exception as e:
            logger.error(f"阻止 LLM 调用失败: {e}")
        self._record_block(event)
        logger.info("智能静默：未 @ 且未点名，已阻止 LLM 调用（不回复）")

    # ========== 钩子：LLM 响应后检测特殊前缀 ==========

    @filter.on_llm_response(priority=200)
    async def detect_silence(
        self,
        event: AstrMessageEvent,
        response,
        **kwargs,
    ) -> None:
        """当有 LLM 响应时，检测是否以特殊前缀开头"""
        if not self._cfg(event, "enable", self.enabled) or self._is_paused():
            return
        if self._cfg(event, "only_group", self.only_group) and not self._is_group_chat(
            event
        ):
            return
        if self._in_always_reply_group(event):
            # 白名单群：必回复，不检测静默
            return

        try:
            text = ""
            if hasattr(response, "completion_text"):
                text = response.completion_text or ""
            if not text:
                # 流式响应或空响应，交给 on_decorating_result 兜底判断
                return

            prefix = str(
                self._cfg(event, "silence_prefix", self.silence_prefix)
            ).strip()
            force_wake = self._cfg(
                event, "force_reply_on_wake", self.force_reply_on_wake
            )
            stripped = text.strip()
            # 被 @ 时才需要真唤醒检测（force_reply_on_wake 关闭时无需计算）
            is_wake = self._is_real_wake(event) if force_wake else False
            action, rest = decide_silence_action(
                stripped,
                prefix,
                is_wake,
                force_wake,
            )

            if action == "send":
                return
            if action == "strip":
                # 被 @：剥离前缀，强制回复正文
                event.set_extra(EXTRA_STRIP_KEY, True)
                event.set_extra(EXTRA_SILENCE_KEY, False)
                response.completion_text = rest
                return
            # silent：静默拦截
            event.set_extra(EXTRA_SILENCE_KEY, True)
            if not stripped.startswith(prefix):
                logger.info(
                    f"智能静默：LLM 回复为自然语言静默表达，判定为静默，执行拦截: {stripped[:50]}"
                )
            else:
                logger.debug(f"LLM 回复以 {prefix} 开头，判定为静默，执行拦截")
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

        兜底：无论 LLM 回复来自哪条路径（是否经过 on_llm_response，
        以及流式输出等场景），这里直接检查最终待发送文本本身，
        同时识别静默前缀与自然语言静默表达并处理：
        - 未被唤醒且命中静默 → 拦截；
        - 被 @ 且文本以静默前缀开头 → 剥离前缀后发送正文；
        - 被 @ 但整段为静默表达 → 视为空回复拦截；
        - 白名单群 → 一律放行；黑名单群未 @ → 一律拦截。
        """
        if not self._cfg(event, "enable", self.enabled) or self._is_paused():
            return

        if self._in_always_reply_group(event):
            # 白名单群（必回复）：不拦截任何回复（含前缀，由 LLM 正常输出）
            return

        force_wake = self._cfg(
            event, "force_reply_on_wake", self.force_reply_on_wake
        )
        only_group = self._cfg(event, "only_group", self.only_group)

        if self._in_always_silent_group(event) and not (
            force_wake and self._is_real_wake(event)
        ):
            # 黑名单群（严格静默）：未 @ 一律拦截，无论 LLM 说了什么
            self._record_block(event)
            event.clear_result()
            logger.info("智能静默：黑名单群未 @ 消息，已拦截 LLM 回复")
            return

        extra = event.get_extra(default={})
        is_silent = bool(extra.get(EXTRA_SILENCE_KEY, False))

        if not is_silent and only_group and self._is_group_chat(event):
            # 兜底：直接检查待发送文本（不依赖 on_llm_response 钩子）
            prefix = str(
                self._cfg(event, "silence_prefix", self.silence_prefix)
            ).strip()
            result = event.get_result()
            if result is not None and result.chain:
                texts = [
                    comp.text
                    for comp in result.chain
                    if hasattr(comp, "text") and comp.text
                ]
                joined = "".join(texts).strip()
                if joined:
                    is_wake = self._is_real_wake(event) if force_wake else False
                    action, rest = decide_silence_action(
                        joined,
                        prefix,
                        is_wake,
                        force_wake,
                    )
                    if action == "silent":
                        is_silent = True
                        if joined.startswith(prefix):
                            logger.info(
                                f"智能静默（发送前兜底）：LLM 回复以 {prefix} 开头，执行拦截"
                            )
                        else:
                            logger.info(
                                f"智能静默（发送前兜底）：判定为静默表达，执行拦截: {joined[:50]}"
                            )
                    elif action == "strip":
                        # 被 @：剥离前缀，重写消息链中的文本组件
                        replaced = False
                        for comp in result.chain:
                            if hasattr(comp, "text"):
                                comp.text = rest if not replaced else ""
                                replaced = True
                        logger.info("智能静默（发送前兜底）：已剥离静默前缀，正常回复正文")

        if is_silent:
            # 清空结果，彻底阻止发送
            event.clear_result()
            self._record_block(event)
            logger.info("智能静默：已拦截 LLM 回复，未发送消息")

    # ========== 命令：状态 / 暂停 / 恢复 ==========

    def _command_arg(self, event: AstrMessageEvent) -> str:
        """提取 /静默 后的子命令参数"""
        text = (event.message_str or "").strip()
        trigger = "静默"
        idx = text.find(trigger)
        if idx >= 0:
            text = text[idx + len(trigger):]
        return text.strip()

    def _is_administrator(self, event: AstrMessageEvent) -> bool:
        """是否管理员（基于 event.is_admin()，失败时拒绝放行）"""
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _reply(self, event: AstrMessageEvent, text: str):
        """向事件设置回复内容（使用 event.set_result 的字符串重载，避免裸 MessageChain 导致管道崩溃）"""
        try:
            event.set_result(text)
        except Exception as e:
            logger.error(f"静默命令输出失败: {e}")

    @filter.command("静默")
    async def silence_cmd(
        self,
        event: AstrMessageEvent,
        **kwargs,
    ) -> None:
        """/静默 [状态]：查看状态与统计；/静默 暂停 [N]：暂停 N 分钟（默认 30）；/静默 恢复"""
        arg = self._command_arg(event)

        # 暂停 / 恢复 需要管理员权限
        if arg.startswith("暂停") or arg == "恢复":
            if not self._is_administrator(event):
                self._reply(event, "【智能静默】仅管理员可执行暂停/恢复操作")
                return
            if arg.startswith("暂停"):
                rest = arg[len("暂停"):].strip()
                minutes = 30.0
                if rest:
                    try:
                        minutes = float(rest)
                    except ValueError:
                        minutes = 30.0
                self._pause(minutes)
                self._reply(
                    event,
                    f"【智能静默】已暂停 {minutes:.0f} 分钟，期间不阻止、不拦截、不注入，"
                    "到期自动恢复",
                )
                return
            if arg == "恢复":
                self._resume()
                self._reply(event, "【智能静默】已恢复，静默逻辑正常生效")
                return

        # 默认：输出状态与统计
        self._load_stats()
        groups = self.stats.get("groups", {})
        top = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)[:5]
        top_lines = "、".join(f"群{kv[0]} {kv[1]} 次" for kv in top) or "暂无"
        today = date.today()
        days = self.stats.get("days", {})
        trend = " ".join(
            f"{(today - timedelta(days=i)).strftime('%m-%d')}:"
            f"{days.get((today - timedelta(days=i)).isoformat(), 0)}"
            for i in range(6, -1, -1)
        )
        # 先触发惰性到期恢复（若有暂停且已到期）
        self._is_paused()
        if self.pause_until is not None:
            remain = int((self.pause_until - time.time()) / 60) + 1
            pause_line = f"暂停: 是（剩余约 {remain} 分钟）"
        else:
            pause_line = "暂停: 否"
        # 当前机器人视角的生效配置
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = "?"
        ev = {
            "enable": self._cfg(event, "enable", self.enabled),
            "only_group": self._cfg(event, "only_group", self.only_group),
            "prefix": self._cfg(event, "silence_prefix", self.silence_prefix),
            "force_wake": self._cfg(
                event, "force_reply_on_wake", self.force_reply_on_wake
            ),
            "wlist": len(
                self._parse_group_list(
                    self._cfg(
                        event, "reply_always_groups", self.reply_always_groups
                    )
                )
            ),
            "blist": len(
                self._parse_group_list(
                    self._cfg(
                        event, "silence_always_groups", self.silence_always_groups
                    )
                )
            ),
            "bot_accounts": len(self._bot_accounts_for(event)),
        }
        lines = [
            "【智能静默】运行状态",
            f"机器人(self_id): {self_id}",
            f"启用: {'是' if ev['enable'] else '否'}",
            f"仅群聊生效: {'是' if ev['only_group'] else '否'}",
            f"静默前缀: {ev['prefix'] or '(未配置)'}",
            f"被@强制回复: {'是' if ev['force_wake'] else '否'}",
            pause_line,
            f"白名单群(必回复): {ev['wlist']} 个",
            f"黑名单群(严格静默): {ev['blist']} 个",
            f"其他机器人账号: {ev['bot_accounts']} 个",
            f"累计拦截: {self.stats.get('total', 0)} 次",
            f"今日拦截: {self.stats.get('today', 0)} 次",
            f"近7天: {trend}",
            f"拦截Top群: {top_lines}",
        ]
        self._reply(event, "\n".join(lines))

    async def terminate(self):
        """插件卸载时清理"""
        try:
            logger.info(f"【{PLUGIN_NAME}】插件已卸载")
        except Exception:
            pass