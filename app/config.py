"""运行配置，全部通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

# SQLite 数据库文件路径（重启后已受理地图从这里恢复）
DB_PATH = Path(os.getenv("DB_PATH", "/workspace/data/weighting.db"))

# 面积小于该阈值（平方米）的交叠视为数值噪声/仅共边，不计入正面积交叠
POSITIVE_AREA_EPS_M2 = float(os.getenv("POSITIVE_AREA_EPS_M2", "1e-6"))

# 后台计算线程池大小
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))

# 输出面积保留小数位（平方米 -> 1e-6 m2，即平方毫米精度）
AREA_ROUND_DIGITS = 6
# 输出比例保留小数位
RATIO_ROUND_DIGITS = 9

# GeoJSON 要素属性中乡镇号字段，按顺序查找
CODE_FIELDS = ("township_code", "code")
