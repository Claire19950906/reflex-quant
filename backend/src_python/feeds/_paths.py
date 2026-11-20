"""feeds 层路径锚点 —— 已下沉到 common/_paths.py，这里只做转发。

保留文件是为了让 feeds 里的模块继续 `from ._paths import ...` 不用改。
"""
from common._paths import BACKEND_DIR, FEEDS_DIR, REPO_DIR, SRC_DIR

