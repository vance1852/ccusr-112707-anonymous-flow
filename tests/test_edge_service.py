"""合成场景测试：覆盖资料中未直接出现但需求明确要求的规则。

所有输入均为匿名合成帧，不含人身身份。
"""
import unittest

from mall_edge.model import AuditReason, IssueStatus, WithholdReason
from mall_edge.service import EdgeService, ProtocolError


def base_profile():
    return {
        "segments": {"z1": ["d1", "d2"], "z2": ["d3"]},
        "agreements": {
            "a1": {"segment": "z1", "merchants": ["m1", "m2"],
                   "start_s": 0, "end_s": 120},
            "a2": {"segment": "z2", "merchants": ["m3"],
                   "start_s": 0, "end_s": 120},
        },
        "publication": {"bin_s": 30, "min_size": 3,
                        "join_signal_ttl_s": 12, "client_cache_s": 8},
        "wire_fields": {
            "permit": "区段 授权号 版本",
            "apply": "设备 授权号 版本 动作",
            "fragment": "设备 授权号 版本 标记 方向",
            "coverage": "区段 状态",
            "share": "区段 商户 动作",
            "publish": "商户 起秒 止秒",
            "cache": "商户 终端 动作",
            "link": "连接态",
        },
    }


_arrival = {"n": 0}


def frame(ftype, source, args, received=None, captured=None):
    _arrival["n"] += 1
    t = _arrival["n"]
    return {"arrival": t, "received": received if received is not None else t,
            "captured": captured if captured is not None else t,
            "source": source, "type": ftype, "args": args}


def build(profile=None):
    _arrival["n"] = 0
    return EdgeService(profile or base_profile())


def permit_all(service):
    service.handle_frame(frame("permit", "ops", ["z1", "a1", 1], 0, 0))
    service.handle_frame(frame("permit", "ops", ["z2", "a2", 1], 0, 0))
    for dev, agreement in [("d1", "a1"), ("d2", "a1"), ("d3", "a2")]:
        service.handle_frame(frame("apply", "ingress", [dev, agreement, 1, "on"], 1, 1))


class PairingWindowTest(unittest.TestCase):
    def test_inside_window_pairs_once_and_token_is_destroyed(self):
        service = build()
        permit_all(service)
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "k", "in"], 5, 5))
        service.handle_frame(frame("fragment", "d2", ["d2", "a1", 1, "k", "in"], 6, 6))
        passages = service.ingest.passages_for("z1", 0)
        self.assertEqual(1, len(passages))
        self.assertTrue(passages[0].paired)
        self.assertEqual(("d1", "d2"), passages[0].source_devices)
        self.assertEqual(0, service.ingest.pending_token_count())

    def test_expired_token_cannot_be_reimported_and_late_side_excluded(self):
        service = build()
        permit_all(service)
        # d1 在 t=5 收到标记，TTL 到 17；d2 在 t=20 才送达，窗口已关闭
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "k", "in"], 5, 5))
        service.handle_frame(frame("fragment", "d2", ["d2", "a1", 1, "k", "in"], 20, 11))
        passages = service.ingest.passages_for("z1", 0)
        self.assertEqual(1, len(passages))
        self.assertFalse(passages[0].paired)
        self.assertEqual(("d1",), passages[0].source_devices)
        reason = [a.reason for a in service.ingest.audits]
        self.assertEqual([AuditReason.TOKEN_EXPIRED], reason)

    def test_redelivery_after_restart_is_idempotent(self):
        service = build()
        permit_all(service)
        common = ["d1", "a1", 1, "k", "in"]
        service.handle_frame(frame("fragment", "d1", common, 5, 5))
        # 同观测重送（设备重启）：同设备同现场时刻同方向
        service.handle_frame(frame("fragment", "d1", common, 30, 5))
        service.ingest.sweep(40)
        self.assertEqual(1, len(service.ingest.passages_for("z1", 0)))
        self.assertEqual([AuditReason.DUPLICATE_FRAME],
                         [a.reason for a in service.ingest.audits])


class AuthorizationBindingTest(unittest.TestCase):
    def test_eligibility_uses_state_at_capture_time(self):
        service = build()
        service.handle_frame(frame("permit", "ops", ["z1", "a1", 1], 0, 0))
        # d1 从未确认 v1 启用 → 无权片段不留观测
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "k", "in"], 5, 5))
        self.assertEqual(0, len(service.ingest.passages_for("z1", 0)))
        self.assertEqual([AuditReason.VERSION_UNCONFIRMED],
                         [a.reason for a in service.ingest.audits])

    def test_capture_under_old_version_stays_bound_to_old_grant(self):
        service = build()
        permit_all(service)
        # t=5 在 v1 下采集
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "k1", "in"], 5, 5))
        # 之后升级到 v2 并停用 v1
        service.handle_frame(frame("permit", "ops", ["z1", "a1", 2], 6, 6))
        service.handle_frame(frame("apply", "ingress", ["d1", "a1", 2, "on"], 7, 7))
        service.handle_frame(frame("apply", "ingress", ["d1", "a1", 1, "off"], 8, 8))
        # 补送的仍是采集当时的 v1，且属于已留存观测的重放 → 幂等，不改授权归属
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "k1", "in"], 9, 5))
        passages = service.ingest.passages_for("z1", 0)
        self.assertEqual(1, len(passages))
        self.assertEqual(1, passages[0].grant_version)

    def test_fragment_under_confirmed_off_version_is_rejected(self):
        service = build()
        permit_all(service)
        service.handle_frame(frame("apply", "ingress", ["d1", "a1", 1, "off"], 4, 4))
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "k", "in"], 5, 5))
        self.assertEqual([AuditReason.VERSION_CONFIRMED_OFF],
                         [a.reason for a in service.ingest.audits])
        self.assertEqual(0, len(service.ingest.passages_for("z1", 0)))

    def test_capture_outside_grant_window_rejected(self):
        profile = base_profile()
        profile["agreements"]["a1"]["start_s"] = 10
        service = build(profile)
        permit_all(service)
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "k", "in"], 5, 5))
        self.assertEqual([AuditReason.GRANT_INACTIVE],
                         [a.reason for a in service.ingest.audits])


class CoverageAndThresholdTest(unittest.TestCase):
    def _three_passages_z2(self, service):
        for i, t in enumerate((5, 6, 7), start=1):
            service.handle_frame(
                frame("fragment", "d3", ["d3", "a2", 1, f"s{i}", "in"], t, t))

    def test_single_side_allowed_keeps_independent_passages(self):
        service = build()
        permit_all(service)
        self._three_passages_z2(service)
        service.handle_frame(frame("publish", "ops", ["m3", 0, 30], 30, 30))
        issue = service._latest_issue("m3", "z2", 0)
        self.assertEqual(IssueStatus.RELEASED, issue.status)
        self.assertEqual(3, issue.count)

    def test_unauthorized_opposite_side_cannot_join_dedup(self):
        service = build()
        permit_all(service)
        # 该通道只允许 d1 一侧采集：d2 明确回报 off（无权侧）
        service.handle_frame(frame("apply", "ingress", ["d2", "a1", 1, "off"], 4, 4))
        # d1 先报，d2 的同标记片段必须被拒，d1 仍计为独立经过
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "k", "in"], 5, 5))
        service.handle_frame(frame("fragment", "d2", ["d2", "a1", 1, "k", "in"], 6, 6))
        passages = service.ingest.passages_for("z1", 0)
        self.assertEqual(1, len(passages))
        self.assertFalse(passages[0].paired)
        self.assertEqual(("d1",), passages[0].source_devices)
        self.assertEqual([AuditReason.VERSION_CONFIRMED_OFF],
                         [a.reason for a in service.ingest.audits])
        # 有权一侧的独立经过仍可继续使用
        service.handle_frame(frame("coverage", "ops", ["z1", "full"], 10, 10))
        for i, t in enumerate((6, 7), start=2):
            service.handle_frame(
                frame("fragment", "d1", ["d1", "a1", 1, f"e{i}", "in"], t, t))
        service.handle_frame(frame("publish", "ops", ["m1", 0, 30], 30, 30))
        issue = service._latest_issue("m1", "z1", 0)
        self.assertEqual(IssueStatus.RELEASED, issue.status)
        self.assertEqual(3, issue.count)

    def test_low_coverage_is_never_published_as_zero(self):
        service = build()
        permit_all(service)
        self._three_passages_z2(service)
        service.handle_frame(frame("coverage", "ops", ["z2", "partial"], 10, 10))
        service.handle_frame(frame("publish", "ops", ["m3", 0, 30], 30, 30))
        issue = service._latest_issue("m3", "z2", 0)
        self.assertEqual(IssueStatus.WITHHELD, issue.status)
        self.assertEqual(WithholdReason.COVERAGE, issue.withhold_reason)
        self.assertIsNone(issue.count)
        # 可确证经过仍合法保留
        self.assertEqual(3, len(service.ingest.passages_for("z2", 0)))

    def test_privacy_threshold_suppresses_small_counts(self):
        service = build()
        permit_all(service)
        for i, t in enumerate((5, 6), start=1):
            service.handle_frame(
                frame("fragment", "d3", ["d3", "a2", 1, f"s{i}", "in"], t, t))
        service.handle_frame(frame("publish", "ops", ["m3", 0, 30], 30, 30))
        issue = service._latest_issue("m3", "z2", 0)
        self.assertEqual(IssueStatus.WITHHELD, issue.status)
        self.assertEqual(WithholdReason.PRIVACY, issue.withhold_reason)
        self.assertIsNone(issue.count)

    def test_wrong_granularity_rejected(self):
        service = build()
        permit_all(service)
        service.handle_frame(frame("publish", "ops", ["m3", 0, 15], 30, 30))
        issue = service._latest_issue("m3", "z2", 0)
        self.assertEqual(WithholdReason.GRANULARITY, issue.withhold_reason)


class RevocationTest(unittest.TestCase):
    def _released_z2_with_terminal(self):
        service = build()
        permit_all(service)
        for i, t in enumerate((5, 6, 7), start=1):
            service.handle_frame(
                frame("fragment", "d3", ["d3", "a2", 1, f"s{i}", "in"], t, t))
        service.handle_frame(frame("publish", "ops", ["m3", 0, 30], 30, 30))
        service.handle_frame(frame("cache", "edge", ["m3", "term-1", "save"], 31, 31))
        return service

    def test_revocation_withdraws_issue_and_invalidates_offline_copy(self):
        service = self._released_z2_with_terminal()
        # 终端断线，TTL 到 39；33 撤回，36 查询仍在 TTL 内但撤回已知
        service.handle_frame(frame("link", "term-1", ["offline"], 32, 32))
        service.handle_frame(frame("share", "ops", ["z2", "m3", "revoke"], 33, 33))
        result = service.terminal_query("m3", "z2", 0, 36)
        self.assertFalse(result.available)
        self.assertTrue(result.cache_invalidated_by_revocation)

        old = next(i for i in service.engine.issues
                   if i.status == IssueStatus.WITHDRAWN)
        self.assertIn("term-1", old.affected_terminals)

    def test_cache_expires_on_ttl_even_without_revocation(self):
        service = self._released_z2_with_terminal()
        result = service.terminal_query("m3", "z2", 0, 40)  # 31+8=39 已过期
        self.assertFalse(result.available)
        self.assertFalse(result.cache_invalidated_by_revocation)

    def test_revocation_does_not_touch_other_segments(self):
        service = self._released_z2_with_terminal()
        # z1 也发布一期
        for i, t in enumerate((5, 6, 7), start=1):
            service.handle_frame(
                frame("fragment", "d1", ["d1", "a1", 1, f"e{i}", "in"], t, t))
        service.handle_frame(frame("publish", "ops", ["m1", 0, 30], 30, 30))
        service.handle_frame(frame("share", "ops", ["z2", "m3", "revoke"], 33, 33))
        z1 = service._latest_issue("m1", "z1", 0)
        self.assertEqual(IssueStatus.RELEASED, z1.status)

    def test_subtraction_guard_blocks_releasable_smaller_count(self):
        # 同一区段 z2 上两份授权：a2(m2) 与 a1b(m1)，d3 同时确认两者；
        # 发布 4 人后撤回 m1 的共享，重算为 3 人仍达最低门槛，
        # 但收过旧版（4 人）的终端在场 → 可相减的新数必须暂缺。
        profile = base_profile()
        # a2 是 z2 上 m2 的授权；另挂 a1b 承载 m1 的第 4 条来源
        profile["agreements"]["a2"]["merchants"] = ["m2"]
        profile["agreements"]["a1b"] = {
            "segment": "z2", "merchants": ["m1"], "start_s": 0, "end_s": 120}
        service = build(profile)
        permit_all(service)
        service.handle_frame(frame("permit", "ops", ["z2", "a1b", 1], 0, 0))
        service.handle_frame(frame("apply", "ingress", ["d3", "a1b", 1, "on"], 1, 1))
        # 3 条归属 a2(m2)，1 条归属 a1b(m1)
        for i, t in enumerate((5, 6, 7), start=1):
            service.handle_frame(
                frame("fragment", "d3", ["d3", "a2", 1, f"s{i}", "in"], t, t))
        service.handle_frame(
            frame("fragment", "d3", ["d3", "a1b", 1, "s4", "in"], 8, 8))
        service.handle_frame(frame("publish", "ops", ["m2", 0, 30], 30, 30))
        first = service._latest_issue("m2", "z2", 0)
        self.assertEqual(IssueStatus.RELEASED, first.status)
        self.assertEqual(4, first.count)
        service.handle_frame(frame("cache", "edge", ["m2", "term-1", "save"], 31, 31))

        service.handle_frame(frame("share", "ops", ["z2", "m1", "revoke"], 32, 32))
        re_issued = service._latest_issue("m2", "z2", 0)
        self.assertEqual(IssueStatus.WITHHELD, re_issued.status)
        self.assertEqual(WithholdReason.SUBTRACTION_GUARD,
                         re_issued.withhold_reason)
        self.assertIsNone(re_issued.count)
        self.assertIn("term-1", re_issued.affected_terminals)
        # 旧版已置撤回
        self.assertEqual(IssueStatus.WITHDRAWN, first.status)

    def test_other_merchant_recomputed_under_sharing_change(self):
        # 同通道商户 m1/m2 共享 z1：m1 撤回后，m1 自身无权，m2 的同窗口结果重新核定
        service = build()
        permit_all(service)
        # 双侧探头都有观测，保证 z1 满覆盖
        for i, t in enumerate((5, 6, 7), start=1):
            service.handle_frame(
                frame("fragment", "d1", ["d1", "a1", 1, f"e{i}", "in"], t, t))
        for i, t in enumerate((5, 6, 7), start=1):
            service.handle_frame(
                frame("fragment", "d2", ["d2", "a1", 1, f"f{i}", "out"], t, t))
        service.handle_frame(frame("publish", "ops", ["m1", 0, 30], 30, 30))
        service.handle_frame(frame("publish", "ops", ["m2", 0, 30], 30, 30))
        service.handle_frame(frame("share", "ops", ["z1", "m1", "revoke"], 33, 33))
        m1 = service._latest_issue("m1", "z1", 0)
        self.assertEqual(WithholdReason.UNAUTHORIZED, m1.withhold_reason)
        m2 = service._latest_issue("m2", "z1", 0)
        # m2 未撤回：覆盖完好且其授权下的 6 条来源不受连坐，继续发布
        self.assertEqual(IssueStatus.RELEASED, m2.status)
        self.assertEqual(6, m2.count)


class ProtocolTest(unittest.TestCase):
    def test_wrong_payload_width_rejected(self):
        service = build()
        bad = {"arrival": 1, "received": 0, "captured": 0,
               "source": "ops", "type": "permit", "args": ["z1"]}
        with self.assertRaises(ProtocolError):
            service.handle_frame(bad)

    def test_unknown_message_type_rejected(self):
        service = build()
        bad = {"arrival": 1, "received": 0, "captured": 0,
               "source": "ops", "type": "nope", "args": []}
        with self.assertRaises(ProtocolError):
            service.handle_frame(bad)


class DataMinimizationTest(unittest.TestCase):
    def test_no_token_or_identity_in_retained_objects(self):
        import json
        service = build()
        permit_all(service)
        service.handle_frame(frame("fragment", "d1", ["d1", "a1", 1, "SECRET", "in"], 5, 5))
        service.handle_frame(frame("fragment", "d2", ["d2", "a1", 1, "SECRET", "in"], 6, 6))
        blob = json.dumps({
            "passages": [p.__dict__ for p in service.ingest.passages],
            "audits": [a.__dict__ for a in service.ingest.audits],
            "trace": service.trace_window("z1", 0),
        }, ensure_ascii=False, default=str)
        self.assertNotIn("SECRET", blob)


if __name__ == "__main__":
    unittest.main()
