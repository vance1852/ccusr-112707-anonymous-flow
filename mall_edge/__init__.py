"""商场边缘匿名客流服务。

只处理匿名通行片段与区段级汇总，不接收、不持久化任何可识别人身身份，
也不保留可用于重建个人路线的配对标记（配对标记仅在有限窗内驻留内存）。
"""

from .service import EdgeService
from .model import IssueStatus, WithholdReason, AuditReason

__all__ = ["EdgeService", "IssueStatus", "WithholdReason", "AuditReason"]
