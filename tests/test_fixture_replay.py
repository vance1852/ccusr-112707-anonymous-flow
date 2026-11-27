"""基于 data/frames.jsonl 真实联调资料的端到端回放核定。"""
import unittest

from mall_edge.model import AuditReason, IssueStatus, WithholdReason
from mall_edge.replay import run_replay


class FixtureReplayTest(unittest.TestCase):
    def setUp(self):
        self.service = run_replay()

    def test_pairing_dedup_and_independent_passages(self):
        east = self.service.ingest.passages_for("east", 0)
        # sig1 双侧配对 1 次；sig2/sig3 各自独立 2 次；sB 迟到 sig2 被拒
        self.assertEqual(3, len(east))
        self.assertEqual(1, sum(1 for p in east if p.paired))
        self.assertEqual(2, sum(1 for p in east if not p.paired))
        # atrium sig7 配对成 1 次；west 三次独立经过
        self.assertEqual(1, len(self.service.ingest.passages_for("atrium", 0)))
        self.assertEqual(3, len(self.service.ingest.passages_for("west", 0)))

    def test_late_sig2_after_window_closed_is_rejected_not_reimported(self):
        self.assertEqual(
            [AuditReason.TOKEN_EXPIRED],
            [a.reason for a in self.service.ingest.audits],
        )
        # 回放结束无任何配对标记残留
        self.assertEqual(0, self.service.ingest.pending_token_count())

    def test_three_gates_each_demonstrated(self):
        west = self.service._latest_issue("cafe", "west", 0)
        self.assertEqual(IssueStatus.RELEASED, west.status)
        self.assertEqual(3, west.count)  # 达最低人数，正常发布

        atrium = self.service._latest_issue("hall", "atrium", 0)
        self.assertEqual(IssueStatus.WITHHELD, atrium.status)
        self.assertEqual(WithholdReason.PRIVACY, atrium.withhold_reason)
        self.assertIsNone(atrium.count)  # 小样本数值不出服务

        east = self.service._latest_issue("hall", "east", 0)
        self.assertEqual(IssueStatus.WITHHELD, east.status)
        self.assertEqual(WithholdReason.COVERAGE, east.withhold_reason)

    def test_revocation_invalidates_offline_copy_even_within_ttl(self):
        result = self.service.terminal_query("tea", "east", 0, 36)
        self.assertFalse(result.available)
        self.assertTrue(result.cache_invalidated_by_revocation)
        # tea 的 east v1 原为覆盖门槛暂缺（从未发布）；撤回后重新核定为
        # unauthorized，并把收过该窗口副本的终端计入影响面
        unauthorized = next(
            i for i in self.service.engine.issues
            if i.merchant == "tea" and i.withhold_reason == WithholdReason.UNAUTHORIZED
        )
        self.assertIn("tablet-7", unauthorized.affected_terminals)

    def test_other_segments_survive_revocation(self):
        result = self.service.terminal_query("cafe", "west", 0, 36)
        self.assertTrue(result.available)
        self.assertEqual(3, result.count)

    def test_guidance_flow_is_zone_level_only(self):
        self.assertEqual(
            {"segment": "west", "bin": [0, 30], "available": True, "count": 3},
            self.service.guidance_zone_flow("west", 0),
        )
        east = self.service.guidance_zone_flow("east", 0)
        self.assertFalse(east["available"])
        self.assertEqual("coverage", east["reason"])

    def test_trace_only_shows_lawfully_retained_sources(self):
        trace = self.service.trace_window("east", 0)
        self.assertEqual("partial", trace["coverage"]["status"])
        self.assertEqual(3, trace["retained_passages"])
        # 被拒观测只显示依据，不显示标记/载荷
        self.assertEqual(
            [{"arrival": 26, "device": "sB", "captured_s": 11,
              "reason": "token_expired"}],
            trace["rejected_observations"],
        )
        for source in trace["retained_sources"]:
            self.assertNotIn("token", source)

    def test_device_stop_three_determinations_are_separate(self):
        sa = self.service.assess_device_stop("sA")
        self.assertTrue(sa.confirmed_stopped)          # 有 v2 off 回报
        self.assertTrue(sa.old_data_lawful)             # v1 旧经过仍按原授权保留

        sb = self.service.assess_device_stop("sB")
        self.assertFalse(sb.confirmed_stopped)          # 仅 link offline
        self.assertIn("缓冲", sb.stop_detail)
        self.assertTrue(sb.old_data_lawful)


if __name__ == "__main__":
    unittest.main()
