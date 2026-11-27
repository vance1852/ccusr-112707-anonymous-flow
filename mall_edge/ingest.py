"""片段摄取、有限窗配对与重放幂等。

隐私边界：
- 配对标记（token）只存在于内存的待配对表，按「边缘首次接收 + TTL」到期清扫；
  配对解析后立即删除，到期后任何重送一律拒绝，不重新导入；
- 不保存标记原文或其派生索引。重放幂等键只使用非识别性的观测元数据
  （设备、现场时刻、方向、授权版本），它无法跨设备重建个人路线；
- 无权片段不落任何观测数据，只写一条不含载荷的处置审计。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import AuditEvent, AuditReason, Passage


@dataclass
class _PendingPair:
    """待配对标记的内存态（含标记原文，仅在 TTL 内存在）。"""

    token: str
    segment: str
    agreement_id: str
    version: int
    bin_start: int
    first_received_s: int
    expires_s: int
    # device -> (captured_s, direction, received_s, arrival)
    members: dict[str, tuple[int, str, int, int]] = field(default_factory=dict)


class IngestLog:
    """片段摄取状态机：授权核验 → 窗内配对 → 独立经过落账。"""

    def __init__(self, profile: dict, grants: dict, devices: dict):
        self._bin_s = profile["publication"]["bin_s"]
        self._ttl = profile["publication"]["join_signal_ttl_s"]
        self._grants = grants          # (agreement_id, version) -> Grant
        self._devices = devices        # device_id -> DeviceState
        self._device_segments = {
            dev: seg for seg, devs in profile["segments"].items() for dev in devs
        }
        self._pending: dict[str, _PendingPair] = {}
        # 标记墓碑：配对窗关闭后，标记本身不得再被重新导入。
        # 只在内存保留标记到窗口依据的引用，不写盘、不构成持久索引；
        # 迟到的另一侧片段据此被拒绝，无权参与去重。
        self._token_tombstones: dict[str, tuple[str, int, str]] = {}
        # 重放幂等：(设备, 现场时刻, 方向, 授权, 版本) -> 处置去向
        self._ingested: dict[tuple, str] = {}
        self.passages: list[Passage] = []
        self.audits: list[AuditEvent] = []
        self._passage_seq = 0

    # ------------------------------------------------------------------ #
    # 配对窗清扫
    # ------------------------------------------------------------------ #
    def sweep(self, now_s: int) -> None:
        """到期标记：先落成各自独立经过，再彻底删除标记。"""
        expired = [token for token, p in self._pending.items() if p.expires_s <= now_s]
        for token in expired:
            pending = self._pending.pop(token)
            self._materialize_independent(pending)

    def _materialize_independent(self, pending: _PendingPair) -> None:
        """窗内未形成跨探头确证的标记 → 各自独立经过（不得相互去重）。"""
        ordered = sorted(pending.members.items(), key=lambda kv: (kv[1][0], kv[0]))
        for device_id, (captured, direction, _received, arrival) in ordered:
            self._new_passage(
                pending, device_id, captured, direction,
                source_devices=(device_id,), paired=False, arrival=arrival,
            )
        self._token_tombstones[pending.token] = (
            pending.agreement_id, pending.version, pending.segment,
        )

    def _new_passage(self, pending: _PendingPair, device_id: str, captured: int,
                     direction: str, source_devices: tuple[str, ...],
                     paired: bool, arrival: int) -> Passage:
        self._passage_seq += 1
        passage = Passage(
            passage_id=f"p-{pending.segment}-{pending.bin_start}-{self._passage_seq}",
            segment=pending.segment,
            agreement_id=pending.agreement_id,
            grant_version=pending.version,
            bin_start=pending.bin_start,
            captured_s=captured,
            direction=direction,
            source_devices=tuple(sorted(source_devices)),
            paired=paired,
        )
        self.passages.append(passage)
        self._ingested[self._dedup_key(
            device_id, captured, direction, pending.agreement_id, pending.version
        )] = passage.passage_id
        return passage

    # ------------------------------------------------------------------ #
    # 授权核验
    # ------------------------------------------------------------------ #
    def _eligible(self, frame: dict, device_id: str, agreement_id: str,
                  version: int, captured: int) -> Optional[AuditReason]:
        grant = self._grants.get((agreement_id, version))
        if grant is None or not (grant.start_s <= captured < grant.end_s):
            return AuditReason.GRANT_INACTIVE
        if self._device_segments.get(device_id) != grant.segment:
            return AuditReason.GRANT_INACTIVE
        state = self._devices.get(device_id)
        if state is None:
            return AuditReason.VERSION_UNCONFIRMED
        confirmation = state.confirmation(version, captured)
        if confirmation is None:
            return AuditReason.VERSION_UNCONFIRMED
        if confirmation == "off":
            return AuditReason.VERSION_CONFIRMED_OFF
        return None

    @staticmethod
    def _dedup_key(device_id, captured, direction, agreement_id, version):
        return device_id, captured, direction, agreement_id, version

    def _audit(self, frame, device_id, agreement_id, version, captured, reason):
        bin_start = (captured // self._bin_s) * self._bin_s
        self.audits.append(AuditEvent(
            arrival=frame["arrival"], received_s=frame["received"],
            captured_s=captured, device_id=device_id,
            segment=self._device_segments.get(device_id, "?"),
            agreement_id=agreement_id, grant_version=version,
            bin_start=bin_start, reason=reason,
        ))

    # ------------------------------------------------------------------ #
    # 片段入口
    # ------------------------------------------------------------------ #
    def handle_fragment(self, frame: dict) -> None:
        device_id, agreement_id, version, token, direction = frame["args"]
        received, captured = frame["received"], frame["captured"]
        bin_start = (captured // self._bin_s) * self._bin_s

        self.sweep(received)  # 先处理到期，过期标记不允许再被新帧「续命」

        key = self._dedup_key(device_id, captured, direction, agreement_id, version)
        if key in self._ingested:
            # 探头重启后重送同一观测：幂等忽略，绝不重复计数
            self._audit(frame, device_id, agreement_id, version, captured,
                        AuditReason.DUPLICATE_FRAME)
            return

        if token in self._token_tombstones:
            # 配对窗已关闭：标记不得重新导入，迟到片段无权参与去重
            self._audit(frame, device_id, agreement_id, version, captured,
                        AuditReason.TOKEN_EXPIRED)
            return

        reason = self._eligible(frame, device_id, agreement_id, version, captured)
        if reason is not None:
            # 无权片段：不解析标记、不留观测，仅登记依据
            self._audit(frame, device_id, agreement_id, version, captured, reason)
            return

        pending = self._pending.get(token)
        if pending is None:
            self._pending[token] = _PendingPair(
                token=token, segment=self._device_segments[device_id],
                agreement_id=agreement_id, version=version, bin_start=bin_start,
                first_received_s=received, expires_s=received + self._ttl,
                members={device_id: (captured, direction, received, frame["arrival"])},
            )
            return

        # 标记已在窗内：校验同区段同授权，防止跨范围拼合
        if (pending.segment != self._device_segments[device_id]
                or pending.agreement_id != agreement_id
                or pending.version != version):
            self._audit(frame, device_id, agreement_id, version, captured,
                        AuditReason.TOKEN_SCOPE_MISMATCH)
            return
        if device_id in pending.members:
            self._audit(frame, device_id, agreement_id, version, captured,
                        AuditReason.DUPLICATE_FRAME)
            return

        # 相邻探头在有限窗内对上同一标记：确证为同一次经过，合并后立即销毁标记
        pending.members[device_id] = (captured, direction, received, frame["arrival"])
        first_device, (first_captured, first_dir, _r, arrival) = sorted(
            pending.members.items(), key=lambda kv: (kv[1][0], kv[0])
        )[0]
        self._pending.pop(token)
        self._new_passage(
            pending, first_device, first_captured, first_dir,
            source_devices=tuple(pending.members.keys()), paired=True, arrival=arrival,
        )

    # ------------------------------------------------------------------ #
    # 对外只暴露无标记的结果
    # ------------------------------------------------------------------ #
    def passages_for(self, segment: str, bin_start: int) -> list[Passage]:
        self.sweep(10**12)  # 核定前确保所有待配对标记已到期解析并清除
        return [p for p in self.passages
                if p.segment == segment and p.bin_start == bin_start]

    def pending_token_count(self) -> int:
        """供测试/自检：仍在窗内的标记数（正常运行时仅短暂非零）。"""
        return len(self._pending)
