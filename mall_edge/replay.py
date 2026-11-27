"""按接收顺序回放联调帧，驱动边缘服务。"""
from __future__ import annotations

from pathlib import Path

from device_replay import ROOT, load_profile, read_frames
from mall_edge.service import EdgeService


def build_service(root: Path = ROOT) -> EdgeService:
    return EdgeService(load_profile(root))


def run_replay(root: Path = ROOT) -> EdgeService:
    service = build_service(root)
    for frame in read_frames(root):
        service.handle_frame(frame)
    return service
