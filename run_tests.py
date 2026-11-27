"""隔离模式测试入口。

`python3 -I run_tests.py` 下解释器不读 PYTHONPATH、不注入当前目录，
因此这里显式把项目根加入 sys.path，保证从任意工作目录都能定位 mall_edge 与
device_replay，同时保留隔离模式对不可信资料目录的防护（不自动加载站点模块）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    # 隔离模式下 discover 要求测试目录是包，这里显式按模块加载，
    # 新增测试模块时在下面登记即可
    module_names = [
        "tests.test_archive",
        "tests.test_edge_service",
        "tests.test_fixture_replay",
    ]
    suite = unittest.TestSuite()
    for name in module_names:
        suite.addTests(loader.loadTestsFromName(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
