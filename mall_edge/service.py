"""边缘服务门面：接收现场帧并对外提供查询、追查与核定接口。

角色边界：
- 引导人员：只看分区级当前有效客流（无商户维度、无原始片段）；
- 商户/门店端：只看本商户授权范围内的有效汇总，撤回应即时反映；
- 楼层运营（主管）：发布核定、撤回操作；
- 值班人员：追查暂缺原因，只显示仍合法保留的来源与范围依据。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .aggregation import PublicationEngine
from .ingest import IngestLog
from .model import (
    AuditReason,
    CacheEntry,
    CoverageStatement,
    DeviceState,
    Grant,
    IssueStatus,
    ShareRevocation,
    StopAssessment,
    WithholdReason,
)


class ProtocolError(ValueError):
    """现场帧不符合 site.json 声明的线协议。"""


@dataclass
class QueryResult:
    available: bool
    segment: str
    bin_start: int
    bin_end: int
    count: Optional[int]
    reason: Optional[WithholdReason]
    basis: Optional[str]
    watermark: int
    cache_expires_s: Optional[int] = None
    cache_invalidated_by_revocation: bool = False


class EdgeService:
    def __init__(self, profile: dict):
        self.profile = profile
        self.bin_s = profile["publication"]["bin_s"]
        self.cache_ttl = profile["publication"]["client_cache_s"]
        self._expected_width = {
            name: len(spec.split()) for name, spec in profile["wire_fields"].items()
        }
        self._device_segments = {
            dev: seg for seg, devs in profile["segments"].items() for dev in devs
        }
        # (agreement_id, version) -> Grant
        self.grants: dict[tuple[str, int], Grant] = {}
        self.devices: dict[str, DeviceState] = {
            dev: DeviceState(device_id=dev, segment=seg)
            for seg, devs in profile["segments"].items() for dev in devs
        }
        # (segment, bin_start) -> 最新 CoverageStatement
        self.coverage_statements: dict[tuple[str, int], CoverageStatement] = {}
        self.revocations: list[ShareRevocation] = []
        self.terminal_store: dict[str, set[str]] = {}
        self.caches: list[CacheEntry] = []
        self._arrivals_seen: set[int] = set()

        self.ingest = IngestLog(profile, self.grants, self.devices)
        self.engine = PublicationEngine(
            profile, self.grants, self.devices, self.coverage_statements,
            self.revocations, self.ingest, self.terminal_store,
        )

    # ================================================================== #
    # 帧入口
    # ================================================================== #
    def handle_frame(self, frame: dict) -> None:
        ftype = frame["type"]
        if ftype not in self._expected_width:
            raise ProtocolError(f"未知消息类型 {ftype!r}")
        if len(frame["args"]) != self._expected_width[ftype]:
            raise ProtocolError(
                f"{ftype} 载荷宽度 {len(frame['args'])} 与线协议 "
                f"{self._expected_width[ftype]} 不符"
            )
        # arrival 全局唯一且按序，重复接收的帧内容靠各自的幂等逻辑处理
        self._arrivals_seen.add(frame["arrival"])
        handler = getattr(self, f"_on_{ftype}")
        handler(frame)

    def _on_permit(self, frame: dict) -> None:
        segment, agreement_id, version = frame["args"]
        spec = next(a for aid, a in self.profile["agreements"].items()
                    if aid == agreement_id)
        if spec["segment"] != segment:
            raise ProtocolError(f"授权 {agreement_id} 与区段 {segment} 不匹配")
        grant = Grant(
            agreement_id=agreement_id, segment=segment, version=int(version),
            merchants=tuple(spec["merchants"]),
            start_s=spec["start_s"], end_s=spec["end_s"],
        )
        self.grants[(agreement_id, int(version))] = grant

    def _on_apply(self, frame: dict) -> None:
        device_id, agreement_id, version, action = frame["args"]
        state = self.devices[device_id]
        state.record_apply(int(version), action, frame["captured"])

    def _on_fragment(self, frame: dict) -> None:
        self.ingest.handle_fragment(frame)

    def _on_coverage(self, frame: dict) -> None:
        segment, status = frame["args"]
        if status not in ("full", "partial", "down"):
            raise ProtocolError(f"未知覆盖状态 {status!r}")
        bin_start = (frame["captured"] // self.bin_s) * self.bin_s
        self.coverage_statements[(segment, bin_start)] = CoverageStatement(
            segment=segment, status=status,
            received_s=frame["received"], bin_start=bin_start,
        )

    def _on_share(self, frame: dict) -> None:
        segment, merchant, action = frame["args"]
        if action not in ("revoke", "restore"):
            raise ProtocolError(f"未知共享动作 {action!r}")
        self.revocations.append(ShareRevocation(
            segment=segment, merchant=merchant,
            action=action, received_s=frame["received"],
        ))
        if action == "revoke":
            # 撤回传播：同区段其他商户同窗口结果重新核定；其他区段不受影响
            self.engine.propagate_revocation(segment, frame["received"])

    def _on_publish(self, frame: dict) -> None:
        merchant, start_s, end_s = frame["args"]
        # 主管核定该期该不该发：覆盖该商户持有共享的全部区段（含此刻已撤回的，
        # 撤回要给出 unauthorized 结论而不是静默跳过），门槛在 engine 内逐项裁决
        for segment in self._segments_known_to(merchant):
            self.engine.adjudicate(
                merchant, segment, int(start_s), int(end_s), frame["received"],
            )

    def _segments_known_to(self, merchant: str) -> list[str]:
        return sorted({g.segment for g in self.grants.values()
                       if merchant in g.merchants})

    def _on_cache(self, frame: dict) -> None:
        merchant, terminal_id, action = frame["args"]
        if action == "save":
            self.terminal_store.setdefault(merchant, set()).add(terminal_id)
            # 登记当前授权范围内所有发布期号，便于撤回时计入影响面
            for issue in self.engine.issues:
                if issue.merchant != merchant:
                    continue
                self.caches.append(CacheEntry(
                    merchant=merchant, terminal_id=terminal_id,
                    segment=issue.segment, saved_s=frame["received"],
                    expires_s=frame["received"] + self.cache_ttl,
                    issue_version=issue.version,
                    watermark_at_save=issue.watermark,
                ))
            # 若保存发生在撤回之后，回填该终端到该窗口所有已撤回期号的影响范围
            for old in self.engine.issues:
                if (old.merchant == merchant
                        and old.status == IssueStatus.WITHDRAWN):
                    object.__setattr__(
                        old, "affected_terminals",
                        tuple(sorted(set(old.affected_terminals) | {terminal_id})),
                    )
        elif action == "query":
            return  # 查询语义见 terminal_query；帧仅记录一次读取动作
        else:
            raise ProtocolError(f"未知缓存动作 {action!r}")

    def _on_link(self, frame: dict) -> None:
        source = frame["source"]
        state = frame["args"][0]
        if source in self.devices:
            self.devices[source].link_online = (state == "online")
            self.devices[source].link_changed_at = frame["received"]
        # 终端断线状态由 cache 条目的 TTL 与撤回水位共同约束，无需额外登记

    # ================================================================== #
    # 门店端查询（含断线副本语义）
    # ================================================================== #
    def terminal_query(self, merchant: str, segment: str, bin_start: int,
                       now_s: int) -> QueryResult:
        issue = self._latest_issue(merchant, segment, bin_start)
        watermark = self.engine.watermark
        # 终端离线副本：只要该终端在该窗口保存过副本、且商户对该区段的共享已撤回，
        # 无论期号当前迭代到第几版，都显式给出「副本因可知撤回而失效」的结论
        cache = self._any_cache_for(merchant, segment, bin_start)
        if (cache is not None
                and self.engine._is_revoked(segment, merchant, now_s)):
            return QueryResult(
                False, segment,
                issue.bin_start if issue else bin_start,
                issue.bin_end if issue else bin_start + self.bin_s, None,
                WithholdReason.UNAUTHORIZED,
                f"终端 {cache.terminal_id} 的离线副本在授权允许期限内可读，"
                "但对应共享已撤回（撤回可知即失效），副本不得再使用",
                watermark, cache_expires_s=cache.expires_s,
                cache_invalidated_by_revocation=True,
            )
        if issue is None:
            return QueryResult(
                False, segment, bin_start, bin_start + self.bin_s, None,
                WithholdReason.COVERAGE, "该窗口尚无核定结果", watermark,
            )
        if issue.status != IssueStatus.RELEASED:
            reason = (issue.withhold_reason if issue.status == IssueStatus.WITHHELD
                      else WithholdReason.UNAUTHORIZED)
            label = "门槛暂缺" if issue.status == IssueStatus.WITHHELD else "已撤回"
            return QueryResult(
                False, segment, issue.bin_start, issue.bin_end, None, reason,
                f"{label}：{issue.basis}", watermark,
            )

        cache_invalidated = False
        expires = None
        if cache is not None:
            expires = cache.expires_s
            # 已知撤回即使 TTL 未到也立即失效：按副本对应的区段精确判断，
            # 不依赖全局水位数字（其他区段撤回推进的水位不应波及本副本）
            cache_invalidated = self.engine._is_revoked(
                segment, merchant, now_s)
            if cache_invalidated:
                return QueryResult(
                    False, segment, issue.bin_start, issue.bin_end, None,
                    WithholdReason.UNAUTHORIZED,
                    f"终端 {cache.terminal_id} 的离线副本在授权允许期限内可读，"
                    "但对应共享已撤回（服务水位已推进），副本失效",
                    watermark, cache_expires_s=expires,
                    cache_invalidated_by_revocation=True,
                )
            if now_s > cache.expires_s:
                return QueryResult(
                    False, segment, issue.bin_start, issue.bin_end, None,
                    WithholdReason.UNAUTHORIZED,
                    f"离线副本已过授权允许期限（{cache.expires_s}s）", watermark,
                    cache_expires_s=expires,
                )
        return QueryResult(
            True, segment, issue.bin_start, issue.bin_end, issue.count, None,
            issue.basis, watermark, cache_expires_s=expires,
        )

    def _latest_issue(self, merchant: str, segment: str, bin_start: int):
        matches = [i for i in self.engine.issues
                   if i.merchant == merchant and i.segment == segment
                   and i.bin_start == bin_start]
        return matches[-1] if matches else None

    def _cache_for(self, merchant: str, segment: str, issue_version: int):
        matches = [c for c in self.caches
                   if c.merchant == merchant and c.segment == segment
                   and c.issue_version == issue_version]
        return matches[-1] if matches else None

    def _any_cache_for(self, merchant: str, segment: str, bin_start: int):
        """该终端在某窗口保存过的任意版本副本（取最近一次）。"""
        bin_end = bin_start + self.bin_s
        matches = [c for c in self.caches
                   if c.merchant == merchant and c.segment == segment]
        return matches[-1] if matches else None

    # ================================================================== #
    # 引导人员：分区级当前有效客流（跨商户的合法发布，不给原始来源）
    # ================================================================== #
    def guidance_zone_flow(self, segment: str, bin_start: int) -> dict:
        counts = {
            i.merchant: i.count for i in self.engine.issues
            if i.segment == segment and i.bin_start == bin_start
            and i.status == IssueStatus.RELEASED
        }
        if not counts:
            issue = next((i for i in self.engine.issues
                          if i.segment == segment and i.bin_start == bin_start), None)
            return {
                "segment": segment, "bin": [bin_start, bin_start + self.bin_s],
                "available": False,
                "reason": issue.withhold_reason.value if issue else "no_issue",
            }
        # 同一区段各商户看到的是同一批匿名经过，取其一作为分区计数，不叠加
        count = next(iter(counts.values()))
        return {
            "segment": segment, "bin": [bin_start, bin_start + self.bin_s],
            "available": True, "count": count,
        }

    # ================================================================== #
    # 值班人员追查：只显示仍合法保留的来源与范围依据
    # ================================================================== #
    def trace_window(self, segment: str, bin_start: int) -> dict:
        """追查某一区某一窗为何因覆盖、撤回或隐私门槛而暂缺。"""
        coverage = self.engine.evaluate_coverage(segment, bin_start)
        # 追查显示「仍合法保留」的来源：撤回只使撤回方丧失可见性，
        # 采集本身合法的经过对其他授权方与值班追查仍保留，不做物理删除。
        retained = [p for p in self.ingest.passages_for(segment, bin_start)
                    if self.grants.get((p.agreement_id, p.grant_version)) is not None]
        grant_rows = []
        for grant in sorted(
            (g for g in self.grants.values() if g.segment == segment),
            key=lambda g: (g.agreement_id, g.version),
        ):
            merchant_rows = []
            for merchant in grant.merchants:
                revoked = self.engine._is_revoked(segment, merchant, 10**9)
                merchant_rows.append({
                    "merchant": merchant,
                    "sharing": "revoked" if revoked else "active",
                })
            grant_rows.append({
                "agreement": grant.agreement_id, "version": grant.version,
                "window_s": [grant.start_s, grant.end_s], "merchants": merchant_rows,
            })

        # 只列仍合法保留的经过（无权/被拒观测本就不存在），不含标记/身份/原始片段
        retained_sources = [{
            "passage_id": p.passage_id,
            "captured_s": p.captured_s,
            "direction": p.direction,
            "paired": p.paired,
            "source_devices": list(p.source_devices),
            "agreement": p.agreement_id,
            "grant_version": p.grant_version,
        } for p in retained]

        # 处置依据：只给设备/窗口/原因，不含任何观测内容
        rejected = [{
            "arrival": a.arrival, "device": a.device_id,
            "captured_s": a.captured_s, "reason": a.reason.value,
        } for a in self.ingest.audits
            if a.segment == segment and a.bin_start == bin_start]

        latest = next((i for i in reversed(self.engine.issues)
                       if i.segment == segment and i.bin_start == bin_start), None)
        return {
            "segment": segment,
            "bin": [bin_start, bin_start + self.bin_s],
            "coverage": {"status": coverage.status, "covered": coverage.covered,
                         "basis": coverage.basis},
            "retained_passages": len(retained),
            "retained_sources": retained_sources,
            "grants": grant_rows,
            "rejected_observations": rejected,
            "adjudication": None if latest is None else {
                "status": latest.status.value,
                "reason": latest.withhold_reason.value
                if latest.withhold_reason else None,
                "basis": latest.basis,
                "affected_terminals": list(latest.affected_terminals),
            },
        }

    # ================================================================== #
    # 探头停止三项分别核定
    # ================================================================== #
    def assess_device_stop(self, device_id: str) -> StopAssessment:
        """分别回答：探头是否确认停止 / 旧数据能否按原授权用 / 原发布是否仍合规。"""
        state = self.devices[device_id]
        segment = state.segment

        # 1. 停止确认：必须有设备回报的 off；仅 link offline 不等于确认停止
        off_versions = [v for v, action in state.confirms.items() if action == "off"]
        if off_versions:
            confirmed = True
            stop_detail = (f"设备已回报授权版本 {sorted(off_versions)} 停用确认"
                           f"（最近 apply 于 {state.last_apply_at}s）")
        elif not state.link_online:
            confirmed = False
            stop_detail = (f"仅链路于 {state.link_changed_at}s 离线，尚无设备停用确认；"
                           "离线期间可能仍在本地缓冲观测，按未确认处理")
        else:
            confirmed = False
            stop_detail = "设备链路在线且无停用回报"

        # 2. 旧数据：撤回前采集、归属当时授权版本的可确证经过仍按原授权保留使用
        old_passages = [p for p in self.ingest.passages
                        if device_id in p.source_devices]
        if old_passages:
            lawful = True
            grants_used = sorted({(p.agreement_id, p.grant_version)
                                  for p in old_passages})
            old_detail = (
                f"{len(old_passages)} 次可确证经过采集当时归属授权 "
                f"{grants_used}；授权归属以采集当时的区段、启用资格与设备确认版本为准，"
                "撤回不溯及采集本身，仍可在原授权目的与保留期限内使用"
            )
        else:
            lawful = False
            old_detail = "无该设备的合法保留经过（无权片段与过期标记重送均未留存观测）"

        # 3. 原发布合规性：该设备参与过的「已发布」期是否被撤回。
        # 覆盖/隐私门槛导致从未发布的窗口不属于「原发布」，不在此判定内。
        released_or_withdrawn = [i for i in self.engine.issues
                                 if i.segment == segment
                                 and i.status in (IssueStatus.RELEASED,
                                                  IssueStatus.WITHDRAWN)
                                 and any(device_id in p.source_devices
                                         for p in self.ingest.passages_for(
                                             i.segment, i.bin_start))]
        withdrawn = [i for i in released_or_withdrawn
                     if i.status == IssueStatus.WITHDRAWN]
        if withdrawn:
            compliant = False
            ids = ", ".join(i.issue_id for i in withdrawn)
            old_pub_detail = (f"原发布 {ids} 因共享撤回不再合规，已置为撤回并通知影响终端；"
                              "其他区段/楼层的合法窗口不受牵连")
        elif released_or_withdrawn:
            compliant = True
            old_pub_detail = "该设备参与的发布期当前仍满足覆盖与门槛要求，保持合规"
        else:
            compliant = False
            old_pub_detail = ("该设备参与的窗口均未发布（覆盖/隐私门槛暂缺），"
                              "不存在可评的原发布；暂缺不等于发布不合规")

        return StopAssessment(
            device_id=device_id, confirmed_stopped=confirmed, stop_detail=stop_detail,
            old_data_lawful=lawful, old_data_detail=old_detail,
            old_publication_compliant=compliant, old_publication_detail=old_pub_detail,
        )
