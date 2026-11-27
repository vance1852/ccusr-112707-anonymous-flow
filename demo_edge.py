"""演示：回放联调资料并输出隐私合规视角的处置报告。

运行：python3 demo_edge.py（隔离模式 python3 -I demo_edge.py 同样可用）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mall_edge.replay import run_replay


def line(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    service = run_replay()

    line("配对窗与去重（标记到期即销毁，服务内不留标记）")
    for passage in service.ingest.passages:
        kind = "双侧配对" if passage.paired else "单侧独立"
        print(f"  {passage.passage_id}: {passage.segment} t={passage.captured_s}s "
              f"{passage.direction} {kind} 来源={list(passage.source_devices)} "
              f"授权={passage.agreement_id}v{passage.grant_version}")
    print(f"  回放结束后内存中残留配对标记：{service.ingest.pending_token_count()} 个")

    line("被拒绝/幂等忽略的观测（审计只留依据，不留载荷与标记）")
    for event in service.ingest.audits:
        print(f"  arrival={event.arrival} 设备={event.device_id} "
              f"采集于{event.captured_s}s 原因={event.reason.value}")

    line("发布核定流水（主管视角）")
    for issue in service.engine.issues:
        count = "—" if issue.count is None else issue.count
        reason = issue.withhold_reason.value if issue.withhold_reason else "-"
        print(f"  {issue.issue_id}: {issue.merchant} / {issue.segment} "
              f"[{issue.bin_start},{issue.bin_end}) 状态={issue.status.value} "
              f"人数={count} 门槛={reason} 影响终端={list(issue.affected_terminals)}")
        print(f"      依据：{issue.basis}")

    line("门店端查询（断线副本 + 撤回水位）")
    for label, kwargs in [
        ("tea 平板在 t=36 查 east（副本 TTL 内但撤回已知）",
         dict(merchant="tea", segment="east", bin_start=0, now_s=36)),
        ("cafe 终端在 t=36 查 west（其他区段不受牵连）",
         dict(merchant="cafe", segment="west", bin_start=0, now_s=36)),
        ("hall 在 t=40 查 east（撤回后重新核定）",
         dict(merchant="hall", segment="east", bin_start=0, now_s=40)),
        ("hall 在 t=40 查 atrium",
         dict(merchant="hall", segment="atrium", bin_start=0, now_s=40)),
    ]:
        result = service.terminal_query(**kwargs)
        shown = "暂缺" if not result.available else f"{result.count} 人"
        print(f"  {label}: {shown}（{result.reason.value if result.reason else '有效'}）")
        if result.cache_invalidated_by_revocation:
            print("      离线副本因可知撤回而失效")
        print(f"      {result.basis}")

    line("引导人员分区客流（只有分区级，无来源明细）")
    for segment in ("west", "atrium", "east"):
        print(f"  {service.guidance_zone_flow(segment, 0)}")

    line("值班人员追查 east [0,30)（仅显示合法保留的来源与范围依据）")
    trace = service.trace_window("east", 0)
    print(f"  覆盖：{trace['coverage']['status']} covered={trace['coverage']['covered']}")
    print(f"  合法保留经过：{trace['retained_passages']} 次")
    for source in trace["retained_sources"]:
        print(f"    - {source}")
    print(f"  被拒观测：{trace['rejected_observations']}")
    print(f"  核定：{trace['adjudication']}")

    line("探头停止三项分别核定")
    for device_id in ("sA", "sB"):
        assessment = service.assess_device_stop(device_id)
        print(f"  {device_id}:")
        print(f"    1) 确认停止={assessment.confirmed_stopped}：{assessment.stop_detail}")
        print(f"    2) 旧数据可按原授权使用={assessment.old_data_lawful}：{assessment.old_data_detail}")
        print(f"    3) 原发布仍合规={assessment.old_publication_compliant}：{assessment.old_publication_detail}")


if __name__ == "__main__":
    main()
