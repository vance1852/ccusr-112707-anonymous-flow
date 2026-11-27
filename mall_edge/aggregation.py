"""覆盖评估、发布门槛核定与撤回重算。

核定顺序（任一不通过即暂缺，数值不出服务）：
  时间粒度 → 请求方共享资格 → 覆盖门槛 → 最低汇总人数 → 撤回后的可相减防护。

低覆盖区段只保留可确证经过并标注依据，绝不按「无人经过」发布零值。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import (
    AuditReason,
    IssueStatus,
    Passage,
    PublicationIssue,
    WithholdReason,
)


@dataclass
class CoverageVerdict:
    covered: bool
    status: str                 # full / partial / down
    basis: str


class PublicationEngine:
    def __init__(self, profile, grants, devices, coverage_statements,
                 revocations, ingest, terminal_store):
        self.profile = profile
        self.bin_s = profile["publication"]["bin_s"]
        self.min_size = profile["publication"]["min_size"]
        self.grants = grants                  # (agreement, version) -> Grant
        self.devices = devices
        self.coverage_statements = coverage_statements  # (segment, bin) -> 最新 statement
        self.revocations = revocations        # list[ShareRevocation]
        self.ingest = ingest
        self.terminal_store = terminal_store  # merchant -> set(terminal_id)
        self.issues: list[PublicationIssue] = []
        self.watermark = 0

    # ------------------------------------------------------------------ #
    # 共享资格
    # ------------------------------------------------------------------ #
    def _active_grant_for(self, segment: str, merchant: str, at_s: int):
        """at_s 时刻商户对区段仍有效的授权（授权时间窗内且未撤回共享）。"""
        for grant in sorted(
            (g for g in self.grants.values() if g.segment == segment),
            key=lambda g: g.version,
        ):
            if merchant not in grant.merchants:
                continue
            if not (grant.start_s <= at_s < grant.end_s):
                continue
            if self._is_revoked(segment, merchant, at_s):
                return None
            return grant
        return None

    def _is_revoked(self, segment: str, merchant: str, at_s: int) -> bool:
        state = None  # None=从未操作；True=已撤回；False=已恢复
        for rev in self.revocations:
            if rev.segment != segment or rev.merchant != merchant:
                continue
            if rev.received_s > at_s:
                break
            state = (rev.action == "revoke")
        return state is True

    # ------------------------------------------------------------------ #
    # 覆盖评估
    # ------------------------------------------------------------------ #
    def evaluate_coverage(self, segment: str, bin_start: int) -> CoverageVerdict:
        statement = self.coverage_statements.get((segment, bin_start))
        if statement is not None:
            covered = statement.status == "full"
            return CoverageVerdict(
                covered=covered, status=statement.status,
                basis=f"运营声明覆盖={statement.status}（接收于 {statement.received_s}s）",
            )
        # 无声明时按设备在整个窗口的确认状态推断
        devices = self.profile["segments"][segment]
        active = [d for d in devices
                  if self.devices[d].confirmed_on_throughout(
                      self._versions_for(segment), bin_start, bin_start + self.bin_s)]
        if not active:
            return CoverageVerdict(False, "down", "窗口内无已确认启用的探头")
        if len(active) == len(devices):
            return CoverageVerdict(True, "full", "全部探头窗口内确认启用")
        return CoverageVerdict(
            False, "partial",
            f"仅 {len(active)}/{len(devices)} 个探头确认启用：{','.join(sorted(active))}",
        )

    def _versions_for(self, segment: str) -> list[int]:
        return sorted({g.version for g in self.grants.values() if g.segment == segment})

    # ------------------------------------------------------------------ #
    # 经过计数（剔除已被撤回共享的来源）
    # ------------------------------------------------------------------ #
    def _live_passages_for(self, segment: str, bin_start: int,
                           viewer: str, at_s: int) -> list[Passage]:
        """面向某查看方的有效经过。

        - 查看方自己撤回 → 其授权下的来源对其不可见（unauthorized 在更早门槛拦截）；
        - 同区段其他商户的授权被撤回 → 该授权下采集的来源在重算中剔除，
          查看方自己授权下的来源不受连坐；
        - 共享授权（多商户同一 agreement）中某商户撤回 → 仅撤回方丧失可见性，
          其他商户仍可使用该授权的来源。
        """
        revoked = self._revoked_merchants(segment, at_s)
        passages = self.ingest.passages_for(segment, bin_start)
        live = []
        for passage in passages:
            grant = self.grants.get((passage.agreement_id, passage.grant_version))
            if grant is None:
                continue
            grant_merchants = set(grant.merchants)
            if viewer in grant_merchants and self._is_revoked(
                    segment, viewer, at_s):
                continue
            # 独立授权（查看方不在内）的商户已撤回：其来源不得再进入任何人的汇总
            if viewer not in grant_merchants and grant_merchants & revoked:
                continue
            live.append(passage)
        return live

    def _live_passages(self, segment: str, bin_start: int,
                       at_s: int) -> list[Passage]:
        """无特定查看方时的有效经过：剔除任何已撤回商户所属授权的来源。"""
        revoked = self._revoked_merchants(segment, at_s)
        passages = self.ingest.passages_for(segment, bin_start)
        live = []
        for passage in passages:
            grant = self.grants.get((passage.agreement_id, passage.grant_version))
            if grant is None:
                continue
            if any(m in revoked for m in grant.merchants):
                continue
            live.append(passage)
        return live

    def _revoked_merchants(self, segment: str, at_s: int) -> set[str]:
        revoked = set()
        for rev in self.revocations:
            if rev.segment != segment or rev.received_s > at_s:
                continue
            if rev.action == "revoke":
                revoked.add(rev.merchant)
            else:
                revoked.discard(rev.merchant)
        return revoked

    # ------------------------------------------------------------------ #
    # 发布核定
    # ------------------------------------------------------------------ #
    def adjudicate(self, merchant: str, segment: str, start_s: int,
                   end_s: int, at_s: int) -> PublicationIssue:
        prior = self._prior_issue(merchant, segment, start_s)
        issue_version = (prior.version + 1) if prior else 1

        def withhold(reason: WithholdReason, basis: str) -> PublicationIssue:
            return self._record(merchant, segment, start_s, end_s, at_s,
                                IssueStatus.WITHHELD, None, reason, basis, issue_version)

        # 1. 时间粒度：必须与发布粒度对齐且恰好一个窗
        if end_s - start_s != self.bin_s or start_s % self.bin_s != 0:
            return withhold(WithholdReason.GRANULARITY,
                            f"请求窗 [{start_s},{end_s}) 不符合 {self.bin_s}s 发布粒度")

        # 2. 请求方共享资格
        grant = self._active_grant_for(segment, merchant, at_s)
        if grant is None:
            return withhold(WithholdReason.UNAUTHORIZED,
                            f"商户 {merchant} 在 {at_s}s 对区段 {segment} 无有效共享"
                            "（未授权或已撤回）")

        # 3. 覆盖门槛
        coverage = self.evaluate_coverage(segment, start_s)
        if not coverage.covered:
            # 低覆盖不得按零发布；可确证独立经过仍保留供追查
            retained = self.ingest.passages_for(segment, start_s)
            return withhold(
                WithholdReason.COVERAGE,
                f"{coverage.basis}；已合法保留 {len(retained)} 次可确证经过，"
                "低覆盖不等于无人经过，不发布计数",
            )

        # 4. 最低汇总人数（按查看方的有效可见来源计数）
        live = self._live_passages_for(segment, start_s, merchant, at_s)
        count = len(live)
        if count < self.min_size:
            return withhold(
                WithholdReason.PRIVACY,
                f"有效经过 {count} 人低于最低汇总人数 {self.min_size}，"
                "为防止小样本反推而暂缺",
            )

        # 5. 撤回后重算：曾看过旧汇总的对象不得拿到可与旧数相减的新数。
        # 基准是该查看方最近一版「已发布」的计数，与本次有效来源计数比较。
        prior_released = next(
            (i for i in reversed(self.issues)
             if i.merchant == merchant and i.segment == segment
             and i.bin_start == start_s and i.status == IssueStatus.RELEASED),
            None,
        )
        prior_count = prior_released.count if prior_released else None
        if prior_count is not None and count != prior_count:
            affected = self._terminals_for(merchant)
            return withhold(
                WithholdReason.SUBTRACTION_GUARD,
                f"撤回后重算为 {count}（原为 {prior_count}）；已向终端 "
                f"{','.join(sorted(affected)) or '无'} 发放旧版，"
                "提供可相减的新数会反推出被撤通行，本版不予发布",
            )

        basis = (f"覆盖={coverage.status}；独立经过 {count} 次（配对去重后）"
                 f"；授权 {grant.agreement_id} v{grant.version}")
        return self._record(merchant, segment, start_s, end_s, at_s,
                            IssueStatus.RELEASED, count, None, basis, issue_version)

    def _record(self, merchant, segment, start_s, end_s, at_s, status, count,
                reason, basis, issue_version) -> PublicationIssue:
        # 旧版本同一期：若已发布，标记撤回并把收过旧版的终端计入影响范围
        affected: set[str] = set(self._terminals_for(merchant))
        for old in self.issues:
            if (old.merchant == merchant and old.segment == segment
                    and old.bin_start == start_s
                    and old.status == IssueStatus.RELEASED):
                merged = set(old.affected_terminals) | affected
                object.__setattr__(old, "affected_terminals",
                                   tuple(sorted(merged)))
                object.__setattr__(old, "status", IssueStatus.WITHDRAWN)
                object.__setattr__(old, "revoked_at", at_s)
                affected = merged
        issue = PublicationIssue(
            issue_id=f"iss-{merchant}-{segment}-{start_s}-v{issue_version}",
            merchant=merchant, segment=segment, bin_start=start_s, bin_end=end_s,
            status=status, count=count, withhold_reason=reason, basis=basis,
            created_s=at_s, version=issue_version, watermark=self.watermark,
            affected_terminals=tuple(sorted(affected)),
        )
        self.issues.append(issue)
        return issue

    def _prior_issue(self, merchant: str, segment: str, start_s: int):
        matches = [i for i in self.issues
                   if i.merchant == merchant and i.segment == segment
                   and i.bin_start == start_s]
        return matches[-1] if matches else None

    def _terminals_for(self, merchant: str) -> set[str]:
        return set(self.terminal_store.get(merchant, ()))

    # ------------------------------------------------------------------ #
    # 撤回影响传播：同区段其他商户同窗口的已发布结果也要重新核定
    # ------------------------------------------------------------------ #
    def propagate_revocation(self, segment: str, at_s: int) -> list[PublicationIssue]:
        self.watermark += 1
        re_adjudicated: list[PublicationIssue] = []
        seen: set[tuple[str, int]] = set()
        # 只对该窗口内曾经产生过期号的（商户, 窗口）重新核定；
        # 去重保证同一 (商户, 窗口) 只重算一次，避免重复期号把防护基线冲掉
        for issue in list(self.issues):
            if issue.segment != segment:
                continue   # 其他楼层/区段的合法窗口保持发布，不一并停掉
            key = (issue.merchant, issue.bin_start)
            if key in seen:
                continue
            seen.add(key)
            re_adjudicated.append(self.adjudicate(
                issue.merchant, segment, issue.bin_start, issue.bin_end, at_s))
        return re_adjudicated
