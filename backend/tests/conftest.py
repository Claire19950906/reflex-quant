# -*- coding: utf-8 -*-
"""pytest conftest — 全局 fixture（v171+ 时间结构化测试稳定基线）。

自动作用（autouse=True）：
- 重置 clock 模块状态（_last_wall/_last_mono/_jump_warned → 0）
- 重置 TRIDENT_TZ → 'UTC'（避免本地开发者 TZ 影响）
- 重置 logging.NullHandler 状态

非自动作用：
- _fresh_db() → :memory: db + migrate（v171+ 列就绪）
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# ── 生产库写保护（2026-11-19 第三次截断事故后加的硬闸）───────────────────────
# 必须在任何用例 import db / config 之前装：一旦有连接打开生产库，写语句直接抛错。
# 为什么放在 conftest 最前面：conftest 由 pytest 在收集阶段最先加载，早于用例模块。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _prod_db_guard import install as _install_prod_guard

_install_prod_guard()
# 让子进程也继承"禁止写生产库"（17:14 那轮 pytest 就是靠 subprocess 继续写库的）
os.environ.setdefault("TRIDENT_BLOCK_PROD_WRITE", "1")


# ── Phase B 回灌: 把 backend/src_python 注入 sys.path ────────────────────────
# 为什么需要：src_python 下的模块是「同级 import」（`import config` / `import db`
# / `from market_sentiment import fng`），不是包内相对导入。金融版原先靠每个测试
# 文件顶部各写一份 sys.path hack 维持，本目录其余文件未覆盖到，`pytest tests/xxx.py`
# 单跑就会 `ModuleNotFoundError: No module named 'config'`。
#
# 这里在 conftest 做全局注入，与 trident 侧 tests/conftest.py 保持一致；与既有的
# per-file hack 幂等共存（重复 insert 同一条路径无副作用）。
# ── v396 事故防护：pytest 绝不碰生产库 ───────────────────────────────────────
# 2026-11-16 18:51 事故：在 backend/ 目录直接 `pytest` 跑全量，部分用例的 fixture
# 对"默认 DB 路径"执行 DELETE/初始化 → 把生产库 trident_event_bus.db 的
# raw_news（19,214 → 6）和 ai_decisions（478 → 4）截断了。
# 修复：只要没显式指定 TRIDENT_DB_PATH，pytest 一律指向独立临时库。
# 想针对真实库跑检查，请显式 `TRIDENT_DB_PATH=/abs/path pytest ...`（并且自己承担风险）。
if not os.environ.get("TRIDENT_DB_PATH"):
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="trident_pytest_db_")
    os.environ["TRIDENT_DB_PATH"] = os.path.join(_TEST_DB_DIR, "trident_event_bus.db")

_SRC_PYTHON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

# 在任何测试模块 import config 之前，强制把所有测试的 SQLite 隔离到临时库。
# 背景：test_structured_api_v172.py 用 os.environ.setdefault 想隔离真实库，但 pytest
# 按字母序先收集的其它模块会先 import config，把 config.DB_PATH 固化成真实的
# trident_event_bus.db，令 setdefault 失效 → 后续测试顶层 db.migrate() 写真实库，
# 撞上运行中 engine/api 的写锁（"database is locked"），还会污染生产数据。
_ISOLATED_DB_DIR = tempfile.mkdtemp(prefix="trident_tests_")
os.environ["TRIDENT_DB_PATH"] = os.path.join(_ISOLATED_DB_DIR, "test_event_bus.db")

# 本会话的隔离库路径：冻结在这里，供 _guard_config_db_path 每个用例前回填。
# 为什么需要（2026-11-19 全量复跑事故）：test_fix_a1_a2_c1.py 直接
# `os.environ.pop("TRIDENT_DB_PATH")` 没还原，env 一旦缺失，任何之后 reload
# 的 config 都会回落到默认 `BASE_DIR/trident_event_bus.db`（= 生产库），
# 于是后面的模块全被「禁止写生产库」闸门拦成 error —— 单跑绿、全量红。
_SESSION_DB_PATH = os.environ["TRIDENT_DB_PATH"]

# v322: 熔断器状态也必须隔离到临时目录。
# 背景：v322 给熔断器加了「跨进程落盘共享」（backend/runtime/breakers/*.json）。
# 于是测试进程会读到**生产**留下的 state="open" 状态 → 熔断跳过 →
# 被测语义（首次应真实调用一次）全部失真（test_gate_deferred_v320 6 个失败）。
# 更糟的是反向污染：测试里 failure() 会把生产的通道真的熔断掉。
# 所以测试必须用独立目录（TRIDENT_BREAKER_DIR 是 circuit_breaker 的读取入口）。
_ISOLATED_BREAKER_DIR = os.path.join(_ISOLATED_DB_DIR, "breakers")
os.environ["TRIDENT_BREAKER_DIR"] = _ISOLATED_BREAKER_DIR
os.makedirs(_ISOLATED_BREAKER_DIR, exist_ok=True)

# v322: 速率闸门的滑动窗口文件也必须隔离。
# 否则测试里每次 consume_rate_slot() 都会扣**生产**的 RPM 名额
#（文件在 backend/runtime/llm_rpm_slots.json，与 engine/api/review 四个进程共用）
# → 跑一次全量测试就能把生产的速率闸门占满 → 线上"突然抽风"。
_ISOLATED_RATE_DIR = os.path.join(_ISOLATED_DB_DIR, "rate")
os.environ["TRIDENT_RATE_DIR"] = _ISOLATED_RATE_DIR
os.makedirs(_ISOLATED_RATE_DIR, exist_ok=True)


# ── 屏蔽 macOS 复制副本 (v321.3 P2) ───────────────────────────────────────────
# 背景：Finder 复制产生的 "xxx 2.py" 是**逐字节副本**（实测 diff = 0 行），
# pytest 会把同一批用例**收集并跑两遍**。
# 危害有二：
#   1) 全量测试时间近乎翻倍；
#   2) 排查卡死时要在两份完全相同的文件之间来回比对，噪音极大
#      （本轮 71% 卡死排查就在这一步多耗了一轮）。
# 规则：文件名以 " 2.py" / " 3.py" 结尾的副本一律不收集。
# 说明：这里只做**忽略**，不删用户的文件 —— 万一里面有改动也能原样保留。
collect_ignore = [
    _f for _f in os.listdir(os.path.dirname(os.path.abspath(__file__)))
    if _f.endswith(".py") and os.path.splitext(_f)[0].rsplit(" ", 1)[-1].isdigit()
    and " " in os.path.splitext(_f)[0]
]

# ── 手动 E2E 脚本体不进 pytest（2026-11-17）──────────────────────────────────
# `e2e_channel_trace.py` 是**手工跑的全链路追踪脚本**，模块顶层就直接建库、写库、
# 打桩 api_server.evaluate_news，不是单元测试。被 pytest 收集时会在 import 阶段
# 执行整条链路，桩函数签名与 _ingest_external_news_sync 的新调用（published_at=）
# 不匹配 → 一条 TypeError 把整批收集打断（exit 2，后面几十个文件一个都没跑）。
# 这类脚本已有 `python tests/e2e_channel_trace.py` 的手工入口，pytest 里忽略即可。
collect_ignore += ["e2e_channel_trace.py"]


# ── v453 探针触发器：测试库里自动摘掉 ────────────────────────────────────────
# v453 给 ai_decisions / raw_news 加了 BEFORE INSERT 触发器，拒绝
# `reasoning = 'test'` 的「探针行」（防止有人往库里塞假数据试探）。这是生产铁律，
# 必须留着。但测试 fixture 恰恰是**故意**用 reasoning='test' 造合成行，
# 于是全量跑时 9 条策略回归 + nlq + no_lookahead 全部「插入成功但查不到」
#（RAISE(IGNORE) 静默丢弃 → 断言 0 == 4）。单跑也一样红，属于测试与铁律的冲突。
# 处理：在**测试进程**里 patch 一次 db.migrate，migrate 完把这两个探针触发器摘掉；
# 生产代码与 db.py 本身一字不改，测试也不再被它误伤。
try:
    import db as _db_for_tests

    _orig_migrate = _db_for_tests.migrate

    _PROD_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "trident_event_bus.db")

    def _is_prod_conn(conn) -> bool:
        """按 PRAGMA database_list 的真实路径判断，避免把生产库当测试库摘触发器。"""
        try:
            for row in conn.execute("pragma database_list"):
                p = row[2] or ""
                if p and os.path.abspath(p) == os.path.abspath(_PROD_DB):
                    return True
        except Exception:
            pass
        return False

    def _migrate_without_probe_guards(conn, *a, **kw):
        out = _orig_migrate(conn, *a, **kw)
        # v483：这段 patch 以前对任何连接都摘触发器。17:14 那轮 `pytest -q` 里
        # 有用例对生产库调 migrate → 两道探针守卫被摘掉，测试残留直接进库。
        # 现在只对临时库动手，生产库原样保留。
        if _is_prod_conn(conn):
            print("[conftest] 跳过摘触发器：目标连接是生产库", flush=True)
            return out
        for _trg in ("trg_ai_decisions_reject_probe", "trg_raw_news_reject_probe"):
            try:
                conn.execute("DROP TRIGGER IF EXISTS %s" % _trg)
            except Exception:
                pass
        return out

    _db_for_tests.migrate = _migrate_without_probe_guards
except Exception:
    pass


@pytest.fixture(autouse=True)
def _isolated_breakers():
    """每个测试前清空熔断器（内存注册表 + 落盘状态）。

    v322：熔断状态跨进程落盘后，测试之间会互相污染 ——
    某个用例把通道打到 open，后续用例新建同名熔断器时
    _sync_from_shared() 把它读回来 → "首次应真实调用一次"却 0 次调用，
    被测语义完全失真（test_gate_deferred_v320 6 个失败）。
    反向污染更危险：测试里的 failure() 会把**生产**通道真的熔断掉。
    """
    import circuit_breaker

    circuit_breaker.reset()
    yield
    circuit_breaker.reset()


@pytest.fixture(autouse=True)
def _trident_test_baseline(monkeypatch):
    """每个测试前自动重置（clock 状态清零 + 清除 TRIDENT_TZ env）。

    注意：不强制设 TRIDENT_TZ=UTC，因为部分测试依赖默认 Asia/Shanghai。
    TZ 切换通过 set_tz fixture 或 测试内部手动设置。
    """
    # 1) 清除 TRIDENT_TZ env（确保各测试独立，不受外部 shell env 影响）
    monkeypatch.delenv("TRIDENT_TZ", raising=False)

    # 2) clock 状态清零
    try:
        import clock as _clock
        _clock._reset_state_for_tests()
    except Exception:
        pass

    yield

    # 测试结束后再清一次（防止 test 间状态泄漏）
    try:
        import clock as _clock
        _clock._reset_state_for_tests()
    except Exception:
        pass


# ── demo_mode 模块级开关的统一还原（v341）─────────────────────────────
# demo_mode 是一个"重载型全局单例"：`importlib.reload(demo_mode)` 配合
# TRIDENT_MODE=demo 会把 IS_DEMO 变成 True。monkeypatch 会还原 **env**，
# 但**模块属性不会**跟着回来 —— 于是后续用例读到 demo 态：
#   test_demo_policy 的 sanitize() 开始四舍五入 → 断言失败，
#   而报错信息跟它自己的逻辑毫无关系（"单独跑必过、全量跑挂"）。
# 靠每个用例自己收拾不现实（实测就漏了），统一在 conftest 兜住。
_DEMO_GLOBALS = ("IS_FULL", "IS_DEMO", "TRIDENT_MODE",
                 "DEMO_DECIMALS", "DEMO_HISTORY_DAYS", "DEMO_LATENCY_MS")


@pytest.fixture(autouse=True)
def _restore_demo_mode_globals():
    """测试前后对齐 demo_mode 的模块级开关，不让重载污染后续用例。"""
    dm = sys.modules.get("demo_mode")
    if dm is None:
        yield
        return
    snap = {k: getattr(dm, k) for k in _DEMO_GLOBALS if hasattr(dm, k)}
    yield
    cur = sys.modules.get("demo_mode", dm)
    for k, v in snap.items():
        try:
            setattr(cur, k, v)
        except Exception:
            pass


# v321.3 P2: 测试期**全局 socket 超时上限**。
# 背景: 全量跑时**间歇性**卡在某个百分比 (实测 58%)，`sample` 抓栈显示
#   主线程停在 `sock_recv_into → internal_select → poll` —— 在等某个
#   **同步** socket 读。单独跑对应文件却只要几秒就过 → 说明是前面测试
#   启动的 api_server/bridge 后台线程 (fd-guard / quote-net / bridge-*)
#   在外网全 timeout 的环境下堆积, 把主线程的请求拖住。
#
# 处置: 给 socket 设一个**默认超时**。任何漏网的同步外网调用都会在
#   20s 内失败而不是无限等 —— 测试要么快速通过, 要么快速报错,
#   不会整轮挂死。20s 足够正常本地/快速外网, 又不至于掩盖慢路径。
#
# ⚠️ 用 monkeypatch.setattr 而不是直接改 socket.setdefaulttimeout:
#   前者会在每个测试后自动还原, 不污染同进程内的其它代码。
import socket as _socket

_ORIG_SETDEFAULTTIMEOUT = _socket.setdefaulttimeout


@pytest.fixture(autouse=True)
def _global_socket_timeout():
    """每个测试期间给 socket 设默认超时, 结束后还原。"""
    _socket.setdefaulttimeout(20)
    try:
        yield
    finally:
        _socket.setdefaulttimeout(None)


@pytest.fixture
def fresh_db():
    """给需要 :memory: db 的测试一个便捷 fixture。"""
    import sqlite3
    import db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


@pytest.fixture
def set_tz():
    """切换 TRIDENT_TZ 的便捷 fixture（yield 后自动恢复）。"""
    def _set(name: str) -> None:
        os.environ["TRIDENT_TZ"] = name
    yield _set
    os.environ.pop("TRIDENT_TZ", None)

# ── v459：DB 路径守卫 ───────────────────────────────────────────────────────
# 有些用例（模版是 test_cost_model / test_no_lookahead）会把 `config.DB_PATH`
# 指到自己的临时库。只要有人忘了还原，**后面所有模块**都会连到那个（已被删掉的）
# 文件 → sqlite 新开空库 → 一堆"单跑全过、全量必挂"的假失败。
# 这里在每个用例开始前把路径拉回本会话的隔离库，谁忘了还原都不会伤到后来者。
# 用例自己在测试体内改路径仍然有效（fixture 只在用例开始前跑一次）。
@pytest.fixture(autouse=True)
def _guard_config_db_path():
    # v487（2026-11-19 全量复跑）：除了把 config.DB_PATH 拉回来，还要把**环境变量**
    # 一起回填。原因：有用例 `os.environ.pop("TRIDENT_DB_PATH")` 后没还原（失败/早退
    # 路径更常见），env 一旦缺失，后面任何 reload 的 config 都会默认回落到生产库路径，
    # 触发 TRIDENT_BLOCK_PROD_WRITE 闸门 → 整片模块 error。
    if os.environ.get("TRIDENT_DB_PATH") != _SESSION_DB_PATH:
        os.environ["TRIDENT_DB_PATH"] = _SESSION_DB_PATH
    expected = _SESSION_DB_PATH
    if not expected:
        yield
        return
    try:
        import config as _config
    except Exception:
        yield
        return
    if getattr(_config, "DB_PATH", None) != expected:
        _config.DB_PATH = expected
    # 光修 config 还不够：api_server 和 routers/* 在 **import 时**就把
    # `config.DB_PATH` 快照成自己的模块级 `DB_PATH`（如 routers/event_atoms.py:25）。
    # 只要它们是在别的用例把路径改坏的那一刻被首次 import，之后整场测试都读那个库
    # → 典型症状：test_structured_api_v172 单跑绿、全量 6 条全红（count 全是 0）。
    # v475：这些模块已改成 `__getattr__` 懒读 config.DB_PATH，不再 import 时快照。
    # 所以这里只需把**残留的旧快照属性**删掉，让懒读生效（别再把新值写进去，
    # 否则又变成"锁死一个值"）。
    for _name, _mod in list(sys.modules.items()):
        if _name == "api_server" or _name.startswith("routers."):
            # v487 修：只删「懒读模块」的残留快照。判据是模块自己实现了
            # `__getattr__`（v475 那批改成懒读的模块才有）。没实现 __getattr__
            # 的模块（如 routers/divergence.py 直接 `from db_location import DB_PATH`）
            # 删掉就是 AttributeError —— 实测 test_db_path_single_source 全量跑红。
            if "DB_PATH" in getattr(_mod, "__dict__", {}) \
                    and getattr(_mod, "__getattr__", None) is not None:
                del _mod.__dict__["DB_PATH"]
    yield

@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """给"自己建表 + 跑 db.migrate"的用例一个**独立**临时库。

    v459（2026-11-19）：这类用例原来是直接在**会话共享库**上 migrate，全量跑时
    前序用例留下的 api_server / bridge 线程还握着 WAL 写锁，migrate 必然撞
    `sqlite3.OperationalError: database is locked` —— 单跑绿、全量红。
    换成独立库后彻底无关：`db.get_db_path()` 每次现读 config，所以把
    config.DB_PATH 指到 tmp 即可，模块内部再取连接也是这个新库。
    """
    import config as _config
    path = str(tmp_path / "isolated_case.db")
    monkeypatch.setattr(_config, "DB_PATH", path, raising=False)
    for _name, _mod in list(sys.modules.items()):
        if _name == "api_server" or _name.startswith("routers."):
            if getattr(_mod, "DB_PATH", None) is not None:
                _mod.DB_PATH = path
    import db as _db
    conn = _db.get_connection()
    _db.migrate(conn)
    conn.commit()
    yield conn


# ── v452：功能开关类环境变量的用例级复位 ─────────────────────────────────
# 背景：一批用例直接写 `os.environ[...] = ...` 且不还原（或 finally 里漏路径），
# 泄漏到后面所有文件 → 典型症状是「单跑绿、全量红，且每轮红的文件不一样」：
#   · EVIDENCE_CONFIDENCE_GATE=50   → 决策/反思统计口径整体偏移
#   · TRIDENT_DISABLE_COSTS=1       → 成本结算路径被跳过（ADAPTER_SKIPPED 变 RISK_REJECTED）
#   · TRADING_MANAGER_ENABLED=0     → 交易执行被风控短路
#   · DUP_ENTRY_GUARD / AUTO_STOP_* → 开仓守卫行为变化
#   · TRIDENT_ALLOW_ANON_FULL=1     → 鉴权契约变化
#   · HTTPS_PROXY / HTTP_PROXY      → 网络探测走别人的代理
# 这里只复位「开关类」键（不含 DB_PATH / BREAKER_DIR 等会话级隔离项，
# 那些由 conftest 顶部的专项守卫负责）。
_LEAKY_ENV_KEYS = (
    "EVIDENCE_CONFIDENCE_GATE",
    "TRIDENT_DISABLE_COSTS",
    "TRADING_MANAGER_ENABLED",
    "ALLOW_HEDGE",
    "DUP_ENTRY_GUARD",
    "AUTO_STOP_ENABLED",
    "AUTO_STOP_PCT",
    "TRIDENT_ALLOW_ANON_FULL",
    "TRIDENT_TZ",
    "LLM_MAX_RPM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)
_LEAKY_ENV_BASELINE = {k: os.environ.get(k) for k in _LEAKY_ENV_KEYS}


@pytest.fixture(autouse=True)
def _restore_leaky_env():
    """每条用例结束后把开关类环境变量拉回**本用例开始时**的值。

    注意：不能在会话/模块级建基线 —— 有些文件在 import 时就设
    `TRIDENT_ALLOW_ANON_FULL=1`（鉴权豁免），按会话基线恢复会把它们打回 401。
    以"每条用例开始时的快照"为基准，只撤销用例体内的泄漏。
    """
    snap = {_k: os.environ.get(_k) for _k in _LEAKY_ENV_KEYS}
    yield
    for _k, _v in snap.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
