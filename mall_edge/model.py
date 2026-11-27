"""领域模型。

设计约束：
- 任何持久对象都不含配对标记原文、不含可识别人身身份的字段；
- 通行经过是聚合后的最小计数单元，片段级原始载荷在配对解析/拒绝后即丢弃；
- 所有时刻使用与资料一致的整数秒，窗口为 [start, start+bin_s)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class IssueStatus(str, Enum):
    """一期汇总的发布状态。"""

    RELEASED = "released"          # 已发布，授权终端可读取
    WITHHELD = "withheld"          # 未通过门槛，不予发布（数值不出服务）
    WITHDRAWN = "withdrawn"        # 发布后被撤回，终端副本须失效


class WithholdReason(str, Enum):
    """汇总暂缺的门槛原因（可向值班人员展示的依据，不含原始观测）。"""

    COVERAGE = "coverage"          # 覆盖不足（低覆盖区段不得当作无人经过）
    PRIVACY = "privacy"            # 低于最低汇总人数门槛
    GRANULARITY = "granularity"    # 时间粒度不符合发布粒度
    UNAUTHORIZED = "unauthorized"  # 请求方对该区段没有有效共享
    SUBTRACTION_GUARD = "subtraction_guard"  # 撤回差额过小，禁止可相减的新数


class AuditReason(str, Enum):
    """片段级处置审计原因。审计记录只留设备、窗口、依据，不留载荷与标记。"""

    TOKEN_EXPIRED = "token_expired"          # 配对标记到期，拒绝重新导入
    GRANT_INACTIVE = "grant_inactive"        # 采集当时授权未生效/已结束
    VERSION_UNCONFIRMED = "version_unconfirmed"  # 设备未确认该授权版本
    VERSION_CONFIRMED_OFF = "version_confirmed_off"  # 设备确认该版本已停用
    DUPLICATE_FRAME = "duplicate_frame"      # 重启后重送，幂等忽略
    TOKEN_SCOPE_MISMATCH = "token_scope_mismatch"  # 同标记跨区段/授权，按协议异常拒绝


@dataclass(frozen=True)
class Grant:
    """一个区段的一份采集授权（版本化）。"""

    agreement_id: str
    segment: str
    version: int
    merchants: tuple[str, ...]
    start_s: int
    end_s: int
    revoked_at: Optional[int] = None  # 商户级撤回登记在 ShareRevocation，此处保留时间窗


@dataclass
class DeviceState:
    """设备对授权版本的确认状态（来自 apply 回报）。"""

    device_id: str
    segment: str
    # version -> 按时间排列的 [(确认时刻, on/off)]，用于还原采集当时的状态
    apply_history: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    last_apply_at: Optional[int] = None
    link_online: bool = True
    link_changed_at: Optional[int] = None

    def record_apply(self, version: int, action: str, at_s: int) -> None:
        self.apply_history.setdefault(version, []).append((at_s, action))
        self.last_apply_at = at_s

    def confirmation(self, version: int, at_s: int) -> Optional[str]:
        """采集时刻 at_s 设备对该版本最近一次确认的动作。"""
        history = self.apply_history.get(version)
        if not history:
            return None
        latest = None
        for confirmed_at, action in history:
            if confirmed_at <= at_s:
                latest = action
            else:
                break
        return latest

    def confirmed_on(self, version: int, at_s: int) -> bool:
        return self.confirmation(version, at_s) == "on"

    def confirmed_on_throughout(self, versions, start_s: int, end_s: int) -> bool:
        """窗口 [start, end) 内对所列任一版本保持启用：窗口结束前确认过 on，
        且窗口内没有 off 覆盖（on 在窗口开始后才回报也算覆盖该窗）。"""
        for version in versions:
            history = self.apply_history.get(version)
            if not history:
                continue
            on_before_end = any(
                action == "on" and at <= end_s for at, action in history
            )
            had_off_inside = any(
                action == "off" and start_s < at < end_s for at, action in history
            )
            if on_before_end and not had_off_inside:
                return True
        return False

    @property
    def confirms(self) -> dict[int, str]:
        """各版本当前（最近一次）确认动作。"""
        return {v: hist[-1][1] for v, hist in self.apply_history.items() if hist}


@dataclass(frozen=True)
class CoverageStatement:
    """有效区段声明（ops 上报的覆盖状态）。"""

    segment: str
    status: str          # full / partial / down
    received_s: int
    bin_start: int


@dataclass(frozen=True)
class Passage:
    """一次可确证的独立经过（聚合计数单元，无标记、无身份）。"""

    passage_id: str
    segment: str
    agreement_id: str
    grant_version: int
    bin_start: int
    captured_s: int
    direction: str
    source_devices: tuple[str, ...]   # 单侧=单设备；双侧配对=两侧设备
    paired: bool
    grant_revoked: bool = False       # 授权后撤回时置位，影响面向撤回方的可见性


@dataclass(frozen=True)
class AuditEvent:
    """片段处置审计。仅保留依据，不保留原始片段与标记。"""

    arrival: int
    received_s: int
    captured_s: int
    device_id: str
    segment: str
    agreement_id: Optional[str]
    grant_version: Optional[int]
    bin_start: Optional[int]
    reason: AuditReason


@dataclass(frozen=True)
class ShareRevocation:
    """商户对区段共享的撤回/恢复。"""

    segment: str
    merchant: str
    action: str            # revoke / restore
    received_s: int


@dataclass(frozen=True)
class PublicationIssue:
    """一次发布核定结果（某商户、某统计窗、某区段的一期）。"""

    issue_id: str
    merchant: str
    segment: str
    bin_start: int
    bin_end: int
    status: IssueStatus
    count: Optional[int]               # 仅 RELEASED 有值；withhold/withdraw 不带数
    withhold_reason: Optional[WithholdReason]
    basis: str                         # 人读依据，供追查
    created_s: int
    version: int                       # 同一 (merchant,segment,bin) 的版本号
    watermark: int                     # 发布时服务撤回水位
    revoked_at: Optional[int] = None
    affected_terminals: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheEntry:
    """门店端保存的查询副本登记（副本本体在终端，服务只登记可见性）。"""

    merchant: str
    terminal_id: str
    segment: Optional[str]
    saved_s: int
    expires_s: int
    issue_version: int
    watermark_at_save: int


@dataclass
class StopAssessment:
    """「探头停止 / 旧数据 / 原发布」三项分别核定的结果。"""

    device_id: str
    confirmed_stopped: bool
    stop_detail: str
    old_data_lawful: bool
    old_data_detail: str
    old_publication_compliant: bool
    old_publication_detail: str
