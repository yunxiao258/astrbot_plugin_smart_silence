"""astrbot_plugin_smart_silence 单元测试

运行方式（在插件目录下运行）：
    python -m unittest test_smart_silence -v
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import (  # noqa: E402
    EXTRA_SILENCE_KEY,
    EXTRA_STRIP_KEY,
    INJECT_MARKER,
    SmartSilencePlugin,
    decide_silence_action,
    is_fallback_silent,
)

PREFIX = "@silent"


def run(coro):
    """同步运行异步钩子方法"""
    return asyncio.run(coro)


class FakeComp:
    def __init__(self, text=""):
        self.text = text


class FakeResult:
    def __init__(self, chain):
        self.chain = chain


class FakeEvent:
    """最小化 AstrMessageEvent 替身，用于钩子方法测试"""

    def __init__(
        self,
        chain=None,
        private=False,
        messages=None,
        message_str="",
        self_id="12345",
        at_o_wake=False,
        group_id="",
        is_admin=True,
        sender_id="",
        message_obj=None,
    ):
        self._private = private
        self.messages = messages or []
        self.message_str = message_str
        self.self_id = self_id
        self.is_at_or_wake_command = at_o_wake
        self.group_id = group_id
        self._is_admin = is_admin
        self.sender_id = sender_id
        self.extra = {}
        self.message_obj = message_obj
        self.result = FakeResult(chain) if chain is not None else None
        self.result_chain = None
        self.cleared = False
        self.stopped = False

    def is_private_chat(self):
        return self._private

    def get_extra(self, default=None):
        return self.extra if self.extra is not None else default

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_result(self):
        return self.result

    def get_self_id(self):
        return self.self_id

    def get_messages(self):
        return self.messages

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id

    def clear_result(self):
        self.cleared = True

    def stop_event(self):
        self.stopped = True

    def is_admin(self):
        return self._is_admin

    def set_result(self, result):
        # 模拟真实框架：str 会被转换为 MessageEventResult（含 .chain）
        if isinstance(result, str):
            result = FakeResult([FakeComp(result)])
        self.result_chain = result


class FakeResponse:
    def __init__(self, text=""):
        self.completion_text = text


CONF = {
    "enable": True,
    "only_group": True,
    "silence_prefix": PREFIX,
    "force_reply_on_wake": True,
    "inject_prompt": True,
    "custom_instruction": "",
}


def make_plugin(**overrides):
    cfg = dict(CONF)
    cfg.update(overrides)
    plugin = SmartSilencePlugin(context=None, config=cfg)
    # 隔离统计：重置为全新状态，避免读取到真实运行产生的 stats.json
    plugin.stats = {"total": 0, "today": 0, "date": "", "days": {}, "groups": {}}
    plugin.pause_until = None
    # 使用临时统计文件，避免污染真实 stats.json
    plugin.stats_path = os.path.join(
        tempfile.gettempdir(), "test_smart_silence_stats.json"
    )
    if os.path.exists(plugin.stats_path):
        os.remove(plugin.stats_path)
    return plugin


class TestIsFallbackSilent(unittest.TestCase):
    def test_empty(self):
        self.assertFalse(is_fallback_silent(""))
        self.assertFalse(is_fallback_silent("   "))

    def test_natural_silent_expressions(self):
        for text in ("保持静默", "无需回复", "这个话题与我无关", "好的，保持沉默",
                     "不必回应", "没必要回复", "选择静默", "无法提供有价值的信息"):
            self.assertTrue(is_fallback_silent(text), text)

    def test_pure_decoration_is_silent(self):
        for text in ("……", "～", "。。。", "~"):
            self.assertTrue(is_fallback_silent(text), text)

    def test_too_long_is_content(self):
        self.assertFalse(is_fallback_silent("无需回复" * 30))  # 超过 60 字

    def test_contains_numbers_is_content(self):
        self.assertFalse(is_fallback_silent("保持静默 1234"))

    def test_contains_link_is_content(self):
        self.assertFalse(is_fallback_silent("保持静默 https://example.com"))

    def test_contains_at_is_content(self):
        self.assertFalse(is_fallback_silent("保持静默 @user"))

    def test_normal_reply_not_silent(self):
        for text in ("好的", "了解，谢谢", "我去查一下", "没问题", "这题答案是 42"):
            self.assertFalse(is_fallback_silent(text), text)


class TestDecideSilenceAction(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(decide_silence_action("", PREFIX, False, True)[0], "silent")

    def test_no_prefix_configured(self):
        action, rest = decide_silence_action("你好", "", False, True)
        self.assertEqual((action, rest), ("send", "你好"))

    def test_normal_reply(self):
        action, rest = decide_silence_action("正常回复内容", PREFIX, False, True)
        self.assertEqual((action, rest), ("send", "正常回复内容"))

    def test_silent_not_woken(self):
        action, _ = decide_silence_action(PREFIX, PREFIX, False, True)
        self.assertEqual(action, "silent")

    def test_silent_with_content_still_blocked_when_not_woken(self):
        action, _ = decide_silence_action(f"{PREFIX} 好的", PREFIX, False, True)
        self.assertEqual(action, "silent")

    def test_strip_when_woken(self):
        action, rest = decide_silence_action(f"{PREFIX} 好的", PREFIX, True, True)
        self.assertEqual((action, rest), ("strip", "好的"))

    def test_prefix_only_when_woken_is_silent(self):
        action, rest = decide_silence_action(PREFIX, PREFIX, True, True)
        self.assertEqual((action, rest), ("silent", ""))

    def test_fallback_silent_when_woken_is_silent(self):
        action, _ = decide_silence_action("保持静默", PREFIX, True, True)
        self.assertEqual(action, "silent")

    def test_force_off_blocks_even_when_woken(self):
        action, _ = decide_silence_action(f"{PREFIX} 好的", PREFIX, True, False)
        self.assertEqual(action, "silent")

    def test_prefix_not_at_start_is_normal(self):
        action, rest = decide_silence_action("好的@silent", PREFIX, False, True)
        self.assertEqual((action, rest), ("send", "好的@silent"))


class TestDetectSilence(unittest.TestCase):
    def test_mark_silent(self):
        plugin = make_plugin()
        event = FakeEvent()
        run(plugin.detect_silence(event, FakeResponse(PREFIX)))
        self.assertTrue(event.extra.get(EXTRA_SILENCE_KEY))

    def test_mark_fallback_silent(self):
        plugin = make_plugin()
        event = FakeEvent()
        run(plugin.detect_silence(event, FakeResponse("保持静默")))
        self.assertTrue(event.extra.get(EXTRA_SILENCE_KEY))

    def test_strip_when_woken(self):
        plugin = make_plugin()
        from astrbot.api.message_components import At

        event = FakeEvent(messages=[At(qq="12345"), FakeComp("你好")])
        resp = FakeResponse(f"{PREFIX} 好的")
        run(plugin.detect_silence(event, resp))
        self.assertEqual(resp.completion_text, "好的")
        self.assertTrue(event.extra.get(EXTRA_STRIP_KEY))
        self.assertFalse(event.extra.get(EXTRA_SILENCE_KEY))

    def test_silent_when_woken_but_prefix_only(self):
        plugin = make_plugin()
        event = FakeEvent()
        run(plugin.detect_silence(event, FakeResponse(PREFIX)))
        self.assertTrue(event.extra.get(EXTRA_SILENCE_KEY))

    def test_normal_reply_unaffected(self):
        plugin = make_plugin()
        event = FakeEvent()
        run(plugin.detect_silence(event, FakeResponse("正常回复")))
        self.assertFalse(event.extra.get(EXTRA_SILENCE_KEY))


class TestBlockSilent(unittest.TestCase):
    def test_block_prefix_in_group(self):
        plugin = make_plugin()
        event = FakeEvent(chain=[FakeComp(PREFIX)])
        run(plugin.block_silent(event))
        self.assertTrue(event.cleared)

    def test_block_fallback_in_group(self):
        plugin = make_plugin()
        event = FakeEvent(chain=[FakeComp("保持静默")])
        run(plugin.block_silent(event))
        self.assertTrue(event.cleared)

    def test_not_block_private_chat(self):
        plugin = make_plugin()
        event = FakeEvent(chain=[FakeComp(PREFIX)], private=True)
        run(plugin.block_silent(event))
        self.assertFalse(event.cleared)

    def test_strip_prefix_in_chain_when_woken(self):
        plugin = make_plugin()
        from astrbot.api.message_components import At

        event = FakeEvent(
            chain=[At(qq="12345"), FakeComp(f"{PREFIX} 好的")],
            messages=[At(qq="12345")],
        )
        run(plugin.block_silent(event))
        self.assertFalse(event.cleared)
        comps = [c for c in event.result.chain if hasattr(c, "text")]
        self.assertEqual(comps[-1].text, "好的")

    def test_block_when_woken_but_silent_expession(self):
        plugin = make_plugin()
        from astrbot.api.message_components import At

        event = FakeEvent(
            chain=[FakeComp("保持静默")],
            messages=[At(qq="12345")],
        )
        run(plugin.block_silent(event))
        self.assertTrue(event.cleared)

    def test_extra_silence_mark_respected(self):
        plugin = make_plugin()
        event = FakeEvent(chain=[FakeComp("任何内容")])
        event.extra[EXTRA_SILENCE_KEY] = True
        run(plugin.block_silent(event))
        self.assertTrue(event.cleared)


class TestInjectInstruction(unittest.TestCase):
    class FakeReq:
        def __init__(self):
            self.system_prompt = "原始提示词"

    def test_inject_appends_marker(self):
        plugin = make_plugin(bot_names="云晓")
        req = self.FakeReq()
        event = FakeEvent(message_str="云晓，早上好")
        run(plugin.inject_instruction(event, req))
        self.assertIn(INJECT_MARKER, req.system_prompt)
        self.assertIn(PREFIX, req.system_prompt)

    def test_no_double_inject(self):
        plugin = make_plugin(bot_names="云晓")
        req = self.FakeReq()
        event = FakeEvent(message_str="云晓，早上好")
        run(plugin.inject_instruction(event, req))
        first = req.system_prompt
        run(plugin.inject_instruction(event, req))
        self.assertEqual(req.system_prompt, first)
        self.assertEqual(req.system_prompt.count(INJECT_MARKER), 1)

    def test_wake_no_inject(self):
        plugin = make_plugin()
        from astrbot.api.message_components import At

        req = self.FakeReq()
        event = FakeEvent(messages=[At(qq="12345")])
        run(plugin.inject_instruction(event, req))
        self.assertEqual(req.system_prompt, "原始提示词")

    def test_slash_command_stripped_still_real_wake(self):
        """waking_check 剥离 / 前缀后（message_str="天气 北京"），
        message_obj.message_str 保留原始 "/天气 北京"，仍应判定为真唤醒"""
        from types import SimpleNamespace

        plugin = make_plugin()
        event = FakeEvent(
            message_str="天气 北京",
            message_obj=SimpleNamespace(message_str="/天气 北京"),
        )
        self.assertTrue(plugin._is_real_wake(event))

    def test_slash_command_no_inject(self):
        """带 / 前缀的真实指令：不注入静默指令（修复 /指令 被误屏蔽）"""
        from types import SimpleNamespace

        plugin = make_plugin()
        req = self.FakeReq()
        event = FakeEvent(
            message_str="天气 北京",
            message_obj=SimpleNamespace(message_str="/天气 北京"),
        )
        run(plugin.inject_instruction(event, req))
        self.assertEqual(req.system_prompt, "原始提示词")
        self.assertNotIn(INJECT_MARKER, req.system_prompt)

    def test_custom_instruction_prefix_placeholder(self):
        plugin = make_plugin(
            bot_names="云晓",
            custom_instruction="静默时输出 {prefix} 只有这一个标记",
        )
        req = self.FakeReq()
        event = FakeEvent(message_str="云晓，早上好")
        run(plugin.inject_instruction(event, req))
        self.assertIn(PREFIX, req.system_prompt)
        self.assertNotIn("{prefix}", req.system_prompt)
        self.assertIn(INJECT_MARKER, req.system_prompt)

    def test_force_reply_off_still_injects_when_woken(self):
        plugin = make_plugin(force_reply_on_wake=False)
        from astrbot.api.message_components import At

        req = self.FakeReq()
        event = FakeEvent(messages=[At(qq="12345")])
        run(plugin.inject_instruction(event, req))
        self.assertIn(INJECT_MARKER, req.system_prompt)

    def test_strict_injected_when_not_really_woken(self):
        """伪唤醒（is_at_or_wake_command=True 但未真 @）且点名时注入严格指令"""
        plugin = make_plugin(bot_names="云晓")
        req = self.FakeReq()
        event = FakeEvent(at_o_wake=True, message_str="云晓，早上好")
        run(plugin.inject_instruction(event, req))
        self.assertIn(INJECT_MARKER, req.system_prompt)
        self.assertIn("特别注意", req.system_prompt)

    def test_no_inject_in_always_reply_group(self):
        """白名单群：不注入静默指令"""
        plugin = make_plugin(reply_always_groups="999")
        req = self.FakeReq()
        event = FakeEvent(group_id="999")
        run(plugin.inject_instruction(event, req))
        self.assertEqual(req.system_prompt, "原始提示词")


class TestGroupPolicy(unittest.TestCase):
    def test_always_reply_group_not_blocked(self):
        plugin = make_plugin(reply_always_groups="999")
        event = FakeEvent(chain=[FakeComp(PREFIX)], group_id="999")
        run(plugin.block_silent(event))
        self.assertFalse(event.cleared)

    def test_always_reply_group_not_detected(self):
        plugin = make_plugin(reply_always_groups="999")
        event = FakeEvent(group_id="999")
        run(plugin.detect_silence(event, FakeResponse(PREFIX)))
        self.assertFalse(event.extra.get(EXTRA_SILENCE_KEY))

    def test_always_silent_group_blocked_without_at(self):
        plugin = make_plugin(silence_always_groups="777")
        for text in ("正常回复", PREFIX, "保持静默", "普通内容"):
            event = FakeEvent(chain=[FakeComp(text)], group_id="777")
            run(plugin.block_silent(event))
            self.assertTrue(event.cleared, text)

    def test_always_silent_group_replies_when_at(self):
        plugin = make_plugin(silence_always_groups="777")
        from astrbot.api.message_components import At

        event = FakeEvent(
            chain=[FakeComp("正常回复")],
            group_id="777",
            messages=[At(qq="12345")],
        )
        run(plugin.block_silent(event))
        self.assertFalse(event.cleared)

    def test_blacklist_over_whitelist_preference(self):
        """同一群同时配置黑白名单时，白名单（必回复）优先"""
        plugin = make_plugin(
            reply_always_groups="999", silence_always_groups="999"
        )
        event = FakeEvent(chain=[FakeComp(PREFIX)], group_id="999")
        run(plugin.block_silent(event))
        self.assertFalse(event.cleared)


class TestStats(unittest.TestCase):
    def test_record_block_counts(self):
        plugin = make_plugin()
        event = FakeEvent(chain=[FakeComp(PREFIX)], group_id="101")
        run(plugin.block_silent(event))
        event2 = FakeEvent(chain=[FakeComp(PREFIX)], group_id="101")
        run(plugin.block_silent(event2))
        self.assertEqual(plugin.stats["total"], 2)
        self.assertEqual(plugin.stats["groups"].get("101"), 2)
        self.assertTrue(os.path.exists(plugin.stats_path))

    def test_stats_command_output(self):
        plugin = make_plugin(silence_always_groups="777")
        event = FakeEvent(message_str="/静默 状态", group_id="101")
        run(plugin.block_silent(FakeEvent(
            chain=[FakeComp(PREFIX)], group_id="101"
        )))
        run(plugin.silence_cmd(event))
        self.assertIsNotNone(event.result_chain)
        text = ""
        for comp in event.result_chain.chain:
            text += getattr(comp, "text", "")
        self.assertIn("累计拦截: 1 次", text)
        self.assertIn("黑名单群(严格静默): 1 个", text)
        self.assertIn("拦截Top群", text)
        self.assertIn("近7天:", text)

    def test_daily_trend_recorded(self):
        plugin = make_plugin()
        run(plugin.block_silent(FakeEvent(chain=[FakeComp(PREFIX)], group_id="101")))
        from datetime import date

        days = plugin.stats["days"]
        self.assertEqual(days.get(date.today().isoformat()), 1)

    def test_pause_blocks_nothing(self):
        """暂停期间：不阻止、不注入、不拦截"""
        plugin = make_plugin(bot_names="云晓")
        plugin._pause(30)

        class FakeReq:
            def __init__(self):
                self.system_prompt = "原始提示词"

        req = FakeReq()
        event = FakeEvent(message_str="今天天气怎么样", group_id="101")
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)
        self.assertEqual(req.system_prompt, "原始提示词")
        event2 = FakeEvent(chain=[FakeComp(PREFIX)], group_id="101")
        run(plugin.block_silent(event2))
        self.assertFalse(event2.cleared)

    def test_pause_auto_resume_after_expiry(self):
        """暂停到期后自动恢复"""
        import time

        plugin = make_plugin(bot_names="云晓")
        plugin.pause_until = time.time() - 1  # 直接置为已过期
        self.assertFalse(plugin._is_paused())
        # 恢复后未点名消息再次被阻止
        req = FakeEvent()
        event = FakeEvent(message_str="今天天气怎么样", group_id="101")
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)

    def test_pause_persisted(self):
        """暂停状态写入 stats 持久化"""
        plugin = make_plugin()
        plugin._pause(60)
        self.assertIn("pause_until", plugin.stats)
        self.assertTrue(os.path.exists(plugin.stats_path))

    def test_pause_command_requires_admin(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="/静默 暂停 30", is_admin=False)
        run(plugin.silence_cmd(event))
        text = ""
        for comp in event.result_chain.chain:
            text += getattr(comp, "text", "")
        self.assertIn("仅管理员", text)
        self.assertIsNone(plugin.pause_until)

    def test_pause_command(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="/静默 暂停 30", is_admin=True)
        run(plugin.silence_cmd(event))
        self.assertIsNotNone(plugin.pause_until)
        text = ""
        for comp in event.result_chain.chain:
            text += getattr(comp, "text", "")
        self.assertIn("已暂停 30 分钟", text)

    def test_resume_command(self):
        plugin = make_plugin()
        plugin._pause(30)
        event = FakeEvent(message_str="/静默 恢复", is_admin=True)
        run(plugin.silence_cmd(event))
        self.assertIsNone(plugin.pause_until)
        self.assertNotIn("pause_until", plugin.stats)

    def test_status_shows_pause(self):
        plugin = make_plugin()
        plugin._pause(60)
        event = FakeEvent(message_str="/静默 状态")
        run(plugin.silence_cmd(event))
        text = ""
        for comp in event.result_chain.chain:
            text += getattr(comp, "text", "")
        self.assertIn("暂停: 是", text)


class TestHardBlock(unittest.TestCase):
    class FakeReq:
        def __init__(self):
            self.system_prompt = "原始提示词"

    def test_stop_when_unmentioned(self):
        """未@且未点名：阻止 LLM 调用，不注入"""
        plugin = make_plugin(bot_names="云晓")
        req = self.FakeReq()
        event = FakeEvent(message_str="今天天气怎么样", group_id="101")
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)
        self.assertEqual(req.system_prompt, "原始提示词")

    def test_no_stop_when_named(self):
        """点名但未@：放行，注入严格指令"""
        plugin = make_plugin(bot_names="云晓")
        req = self.FakeReq()
        event = FakeEvent(message_str="云晓，帮我查一下", group_id="101")
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)
        self.assertIn(INJECT_MARKER, req.system_prompt)
        self.assertIn("特别注意", req.system_prompt)

    def test_no_stop_when_at(self):
        """被@：正常放行，不注入"""
        plugin = make_plugin(bot_names="云晓")
        from astrbot.api.message_components import At

        req = self.FakeReq()
        event = FakeEvent(
            message_str="帮我查一下",
            group_id="101",
            messages=[At(qq="12345")],
        )
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)
        self.assertEqual(req.system_prompt, "原始提示词")

    def test_no_stop_in_always_reply_group(self):
        """白名单群：必回复，不阻止"""
        plugin = make_plugin(bot_names="云晓", reply_always_groups="999")
        req = self.FakeReq()
        event = FakeEvent(message_str="今天天气怎么样", group_id="999")
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)

    def test_stop_in_blacklist_even_when_named(self):
        """黑名单群：未@一律阻止（即使点名）"""
        plugin = make_plugin(bot_names="云晓", silence_always_groups="777")
        req = self.FakeReq()
        event = FakeEvent(message_str="云晓，帮我查一下", group_id="777")
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)

    def test_no_stop_when_disabled(self):
        """hard_block_unmentioned=False：回到提示词模式"""
        plugin = make_plugin(bot_names="云晓", hard_block_unmentioned=False)
        req = self.FakeReq()
        event = FakeEvent(message_str="今天天气怎么样", group_id="101")
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)
        self.assertIn(INJECT_MARKER, req.system_prompt)

    def test_stop_without_bot_names(self):
        """未配置名字：任何未@消息都视为未点名"""
        plugin = make_plugin(bot_names="")
        req = self.FakeReq()
        event = FakeEvent(message_str="今天天气怎么样", group_id="101")
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)

    def test_stop_recorded_in_stats(self):
        """阻止计入拦截统计"""
        plugin = make_plugin(bot_names="云晓")
        req = self.FakeReq()
        event = FakeEvent(message_str="今天天气怎么样", group_id="101")
        run(plugin.inject_instruction(event, req))
        self.assertEqual(plugin.stats["total"], 1)
        self.assertEqual(plugin.stats["groups"].get("101"), 1)

    def test_pseudo_wake_not_stopped(self):
        """被 llm_enhancement 等标记为唤醒（伪唤醒）的消息放行并注入 strict"""
        plugin = make_plugin()
        req = self.FakeReq()
        event = FakeEvent(
            message_str="帮我总结一下", group_id="101", at_o_wake=True
        )
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)
        self.assertIn(INJECT_MARKER, req.system_prompt)
        self.assertIn("特别注意", req.system_prompt)

    def test_pseudo_wake_still_stopped_in_blacklist(self):
        """黑名单群：伪唤醒消息也一律阻止"""
        plugin = make_plugin(silence_always_groups="777")
        req = self.FakeReq()
        event = FakeEvent(
            message_str="帮我总结一下", group_id="777", at_o_wake=True
        )
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)

    def test_pseudo_wake_not_stopped_when_hard_block_off(self):
        """hard_block 关闭时伪唤醒消息放行注入"""
        plugin = make_plugin(hard_block_unmentioned=False)
        req = self.FakeReq()
        event = FakeEvent(
            message_str="帮我总结一下", group_id="101", at_o_wake=True
        )
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)
        self.assertIn(INJECT_MARKER, req.system_prompt)


class TestBotMessage(unittest.TestCase):
    class FakeReq:
        def __init__(self):
            self.system_prompt = "原始提示词"

    def _plugin(self, **overrides):
        cfg = {"bot_accounts": "10002"}
        cfg.update(overrides)
        return make_plugin(**cfg)

    def test_bot_message_not_stopped(self):
        """其他机器人消息：不硬阻止，注入机器人规则"""
        plugin = self._plugin()
        req = self.FakeReq()
        event = FakeEvent(
            message_str="任务完成，同步一下", group_id="101", sender_id="10002"
        )
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)
        self.assertIn("另一个机器人", req.system_prompt)
        self.assertIn(INJECT_MARKER, req.system_prompt)

    def test_user_message_still_stopped(self):
        """普通用户消息：硬阻止仍生效"""
        plugin = self._plugin()
        req = self.FakeReq()
        event = FakeEvent(message_str="今天天气怎么样", group_id="101", sender_id="123")
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)
        self.assertEqual(req.system_prompt, "原始提示词")

    def test_bot_message_in_blacklist_stopped(self):
        """黑名单群：机器人消息同样阻止"""
        plugin = self._plugin(silence_always_groups="777")
        req = self.FakeReq()
        event = FakeEvent(
            message_str="任务完成，同步一下",
            group_id="777",
            sender_id="10002",
        )
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)

    def test_bot_message_in_whitelist_no_inject(self):
        """白名单群：机器人消息不注入规则"""
        plugin = self._plugin(reply_always_groups="999")
        req = self.FakeReq()
        event = FakeEvent(
            message_str="任务完成，同步一下",
            group_id="999",
            sender_id="10002",
        )
        run(plugin.inject_instruction(event, req))
        self.assertEqual(req.system_prompt, "原始提示词")

    def test_custom_rule_with_variables(self):
        """自定义规则支持 {prefix}/{bot_name} 变量"""
        plugin = self._plugin(
            bot_names="云晓",
            bot_message_rule="这是 {bot_name} 的消息，静默时输出 {prefix}",
        )
        req = self.FakeReq()
        event = FakeEvent(
            message_str="任务完成", group_id="101", sender_id="10002"
        )
        run(plugin.inject_instruction(event, req))
        self.assertIn("这是 云晓 的消息", req.system_prompt)
        self.assertIn(f"静默时输出 {PREFIX}", req.system_prompt)
        self.assertNotIn("{prefix}", req.system_prompt)

    def test_self_id_not_treated_as_other_bot(self):
        """自己账号不在 bot_accounts 时正常按用户消息处理"""
        plugin = self._plugin()
        req = self.FakeReq()
        event = FakeEvent(
            message_str="今天天气怎么样",
            group_id="101",
            sender_id="10002",
            self_id="10002",
        )
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)

    def test_bot_message_silent_response_still_blocked(self):
        """机器人消息的 LLM 静默前缀仍被拦截（detect/block 正常）"""
        plugin = self._plugin()
        event = FakeEvent(chain=[FakeComp(PREFIX)], group_id="101", sender_id="10002")
        run(plugin.block_silent(event))
        self.assertTrue(event.cleared)


class TestPerBotConfig(unittest.TestCase):
    class FakeReq:
        def __init__(self):
            self.system_prompt = "原始提示词"

    PBC = json.dumps(
        {
            "10001": {"bot_names": "云晓", "enable": True},
            "10002": {"bot_names": "小云", "enable": False},
            "10003": {"silence_always_groups": "777"},
        },
        ensure_ascii=False,
    )

    def _plugin(self, **overrides):
        cfg = {"per_bot_config": self.PBC}
        cfg.update(overrides)
        return make_plugin(**cfg)

    def test_per_bot_names(self):
        """各机器人用各自名字检测点名"""
        plugin = self._plugin()
        # 机器人 10001：名称为“云晓”
        req = self.FakeReq()
        event = FakeEvent(
            message_str="小云，帮我查一下", group_id="101", self_id="10001"
        )
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)  # 未点名“云晓”，阻止
        # 机器人 10002：名称为“小云”，但 enable=False
        req2 = self.FakeReq()
        event2 = FakeEvent(
            message_str="小云，帮我查一下", group_id="101", self_id="10002"
        )
        run(plugin.inject_instruction(event2, req2))
        self.assertFalse(event2.stopped)
        self.assertEqual(req2.system_prompt, "原始提示词")  # enable=False 不动作

    def test_per_bot_blacklist(self):
        """机器人 10003 黑名单 777，其他机器人不受影响"""
        plugin = self._plugin()
        req = self.FakeReq()
        event = FakeEvent(message_str="普通内容", group_id="777", self_id="10003")
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)
        # 全局视角（10001）：777 不是黑名单，未点名 → 也阻止（hard block）
        req2 = self.FakeReq()
        event2 = FakeEvent(message_str="普通内容", group_id="777", self_id="10001")
        run(plugin.inject_instruction(event2, req2))
        self.assertTrue(event2.stopped)

    def test_global_fallback_when_no_override(self):
        """未覆盖的键回退全局配置"""
        plugin = self._plugin(bot_names="全局名")
        req = self.FakeReq()
        event = FakeEvent(
            message_str="全局名，帮我查一下", group_id="101", self_id="10001"
        )
        run(plugin.inject_instruction(event, req))
        self.assertTrue(event.stopped)  # 10001 覆盖了 bot_names=云晓，不匹配全局名
        req2 = self.FakeReq()
        event2 = FakeEvent(
            message_str="全局名，帮我查一下", group_id="101", self_id="10004"
        )
        run(plugin.inject_instruction(event2, req2))
        self.assertFalse(event2.stopped)  # 10004 无覆盖 → 全局名生效

    def test_per_bot_bot_accounts(self):
        """per-bot 覆盖 bot_accounts：仅 10001 认为 10002 是机器人"""
        pbc = json.dumps(
            {"10001": {"bot_accounts": "10002"}}, ensure_ascii=False
        )
        plugin = make_plugin(per_bot_config=pbc)
        req = self.FakeReq()
        event = FakeEvent(
            message_str="任务同步",
            group_id="101",
            self_id="10001",
            sender_id="10002",
        )
        run(plugin.inject_instruction(event, req))
        self.assertFalse(event.stopped)
        req2 = self.FakeReq()
        event2 = FakeEvent(
            message_str="任务同步",
            group_id="101",
            self_id="10004",
            sender_id="10002",
        )
        run(plugin.inject_instruction(event2, req2))
        self.assertTrue(event2.stopped)

    def test_invalid_json_ignored(self):
        """非法 per_bot_config JSON 不崩溃，回退全局"""
        plugin = make_plugin(per_bot_config="{invalid json")
        self.assertEqual(plugin._bot_ctx, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)