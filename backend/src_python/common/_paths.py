"""全仓路径锚点（P2 下沉到 common 层）。

任何子包（feeds / engine / 未来新增的域）都从这里取路径，
不要各自写 parents[N] —— 层级一改就错，而且错得很安静。
"""
from pathlib import Path

COMMON_DIR = Path(__file__).resolve().parent   # src_python/common
SRC_DIR = COMMON_DIR.parent                     # src_python
BACKEND_DIR = SRC_DIR.parent                    # backend
REPO_DIR = BACKEND_DIR.parent                   # 仓库根
FEEDS_DIR = SRC_DIR / "feeds"                     # 数据源域

