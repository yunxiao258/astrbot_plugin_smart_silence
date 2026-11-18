# -*- coding: utf-8 -*-
"""smart_silence 新功能测试（v1.7.0）：沉默原因分布、发言率报表、/静默 报表子命令。"""

import asyncio
import os
import sys
import tempfile
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PLUGIN_DIR)
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from main import _match_silence_prefix, decide_silence_action  # noqa: E402
from test_smart_silence import FakeEvent, FakeResponse, make_plugin  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class TestReasonDistribution(unittest.TestCase):
    def test_record_block_with_reason(self):
        p = make_plugin()
        p._record_block(FakeEvent(group_id="111"), "hard_block")
        p._record_block(FakeEvent(group_id="111"), "hard_block")
        p._record_block(FakeEvent(group_id="111"), "prefix")
        self.assertEqual(p.stats["reasons"]["hard_block"], 2)
        self.assertEqual(p.stats["reasons"]["prefix"], 1)
        self.assertEqual(p.stats["total"], 3)

    def test_default_reason_other(self):
        p = make_plugin()
        p._record_block(FakeEvent(group_id="111"))
        self.assertEqual(p.stats["reasons"]["other"], 1)

    def test_stop_request_records_hard_block(self):
        p = make_plugin()
        ev = FakeEvent(group_id="222")
        p._stop_request(ev)
        self.assertTrue(ev.stopped)
        self.assertEqual(p.stats["reasons"]["hard_block"], 1)

    def test_stop_request_blacklist_reason(self):
        p = make_plugin()
        p._stop_request(FakeEvent(group_id="222"), "blacklist")
        self.assertEqual(p.stats["reasons"]["blacklist"], 1)


class TestRecordPass(unittest.TestCase):
    def test_pass_counted_per_group(self):
        p = make_plugin()
        p._record_pass(FakeEvent(group_id="a"))
        p._record_pass(FakeEvent(group_id="a"))
        p._record_pass(FakeEvent(group_id="b"))
        self.assertEqual(p.stats["passes"]["a"], 2)
        self.assertEqual(p.stats["passes"]["b"], 1)

    def test_detect_silence_counts_pass(self):
        p = make_plugin()
        ev = FakeEvent(group_id="g1", messages=[], message_str="")
        resp = FakeResponse("普通回复，没有前缀")
        run(p.detect_silence(ev, resp))
        self.assertEqual(p.stats["passes"].get("g1"), 1)

    def test_disabled_no_pass_counted(self):
        p = make_plugin(enable=False)
        ev = FakeEvent(group_id="g2", messages=[])
        run(p.detect_silence(ev, FakeResponse("hi")))
        self.assertIsNone(p.stats.get("passes", {}).get("g2"))


class TestReportCommand(unittest.TestCase):
    def _make_with_data(self):
        p = make_plugin()
        # 群 777：放行 3 次，拦截 1 次（硬阻止）
        for _ in range(3):
            p._record_pass(FakeEvent(group_id="777"))
        p._record_block(FakeEvent(group_id="777"), "hard_block")
        # 其他群数据不应出现
        p._record_block(FakeEvent(group_id="888"), "prefix")
        return p

    def test_report_basic_rates(self):
        p = self._make_with_data()
        text = p._report_text(FakeEvent(group_id="777"))
        self.assertIn("发言率报表", text)
        self.assertIn("进入 LLM 并回复: 3 次", text)
        self.assertIn("被静默拦截: 1 次", text)
        self.assertIn("75.0%", text)

    def test_report_empty_hint(self):
        p = make_plugin()
        text = p._report_text(FakeEvent(group_id="nope"))
        self.assertIn("暂无统计数据", text)

    def test_reason_ranking(self):
        p = make_plugin()
        for _ in range(5):
            p._record_block(FakeEvent(group_id="999"), "hard_block")
        for _ in range(2):
            p._record_block(FakeEvent(group_id="999"), "blacklist")
        p._record_block(FakeEvent(group_id="999"), "prefix")
        text = p._report_text(FakeEvent(group_id="999"))
        hb = text.find("未@未点名硬阻止")
        bl = text.find("黑名单群严格静默")
        pf = text.find("静默前缀拦截")
        self.assertTrue(hb < bl < pf)
        self.assertIn("累计 8 次", text)

    def test_dispatch_report_subcommand(self):
        p = self._make_with_data()
        ev = FakeEvent(group_id="777", message_str="/静默 报表", is_admin=True)
        run(p.silence_cmd(ev))
        self.assertIsNotNone(getattr(ev, "result_chain", None))
        rendered = "".join(
            c.text for c in ev.result_chain.chain if hasattr(c, "text")
        )
        self.assertIn("发言率报表", rendered)

    def test_status_shows_reason_top3(self):
        p = make_plugin()
        for _ in range(3):
            p._record_block(FakeEvent(group_id="555"), "hard_block")
        ev = FakeEvent(group_id="555", message_str="/静默")
        run(p.silence_cmd(ev))
        rendered = "".join(
            c.text for c in ev.result_chain.chain if hasattr(c, "text")
        )
        self.assertIn("沉默原因Top3", rendered)
        self.assertIn("硬阻止 3 次", rendered)


class TestV171FixesAndCommands(unittest.TestCase):
    def test_case_insensitive_and_backtick_prefix(self):
        self.assertEqual(decide_silence_action("@SILENT", "@silent", False, True)[0], "silent")
        self.assertEqual(decide_silence_action("  `@silent`", "@silent", False, True)[0], "silent")
        action, rest = decide_silence_action("`@SILENT` 好的", "@silent", True, True)
        self.assertEqual((action, rest), ("strip", "好的"))
        matched, _ = _match_silence_prefix("`@silent_var` 是变量", "@silent")
        self.assertFalse(matched)

    def test_string_false_config_not_truthy(self):
        p = make_plugin(enable="false", hard_block_unmentioned="0", per_bot_config='{"20002": {"enable": "false"}}')
        self.assertFalse(p.enabled)
        self.assertFalse(p.hard_block_unmentioned)
        ev = FakeEvent(self_id="20002", group_id="101", message_str="hi")
        self.assertFalse(p._cfg_bool(ev, "enable", True))

    def test_pass_persisted_before_report(self):
        p = make_plugin()
        p._record_pass(FakeEvent(group_id="g9"))
        p._record_pass(FakeEvent(group_id="g9"))
        ev = FakeEvent(group_id="g9", message_str="/静默 报表", is_admin=True)
        run(p.silence_cmd(ev))
        rendered = "".join(c.text for c in ev.result_chain.chain if hasattr(c, "text"))
        self.assertIn("进入 LLM 并回复: 2 次", rendered)

    def test_help_clear_and_terminate(self):
        p = make_plugin()
        ev_help = FakeEvent(group_id="g9", message_str="/静默 帮助")
        run(p.silence_cmd(ev_help))
        help_txt = "".join(c.text for c in ev_help.result_chain.chain if hasattr(c, "text"))
        self.assertIn("可用指令", help_txt)
        p._record_block(FakeEvent(group_id="g9"), "hard_block")
        p._pause(10)
        ev_nonadmin = FakeEvent(group_id="g9", message_str="/静默 清空", is_admin=False)
        run(p.silence_cmd(ev_nonadmin))
        self.assertEqual(p.stats["total"], 1)
        ev_clear = FakeEvent(group_id="g9", message_str="/静默 清空", is_admin=True)
        run(p.silence_cmd(ev_clear))
        self.assertEqual(p.stats["total"], 0)
        self.assertIsNotNone(p.pause_until)
        run(p.terminate())


if __name__ == "__main__":
    unittest.main(verbosity=1)
