"""realtime_validation 测试目录的收集守卫。

本目录下的 test_*.py 是一次性分析脚本 (非单元测试) —— 它们在模块顶层直接
读取 /tmp/btc_10m_3d.json 这个运行时抓取产物。该文件不存在时, 导入即
FileNotFoundError, 会让整轮 pytest 收集失败。

这里在数据缺失时忽略这些脚本, 保证主测试套件可以完整收集与运行。
需要跑这些脚本时, 先用对应采集脚本生成 /tmp/btc_10m_3d.json 再单独执行。
"""
import os

DATA_FILE = "/tmp/btc_10m_3d.json"

# 数据不在就整目录忽略 (inspect_api.py 本来就不匹配 test_*.py)
collect_ignore_glob = [] if os.path.exists(DATA_FILE) else ["test_*.py"]