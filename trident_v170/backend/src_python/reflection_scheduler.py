#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reflection system -> Scheduler (L3.1)

每 10 分钟 (configurable) 自动跑:
  1. sample_quality_filter.audit_trades()        (质检)
  2. check_sample_integrity → decision_errors    (E020)
  3. L2.1 verifier_feedback                       (E020 → review_insights)
  4. L2.2 analysis_layer_feedback                 (E020 → decision_layer_signals)
  5. L2.3 kg_feedback                             (E020 累计 → KG proposal)
  6. L3.2 reflection_alert                        (E020 累计 → alert_log)

不自动跑 LLM 反思 (避免重复 token 消耗)
不自动跑 verifier 主流程 (避免撞 verifier 主逻辑)

运行模式:
  --once    : 跑一次 (cron 模式, 适合 crontab / launchd 每 10 分钟触发)
  (无参数)  : daemon 模式 (while True + sleep, 适合后台常驻)

默认 NOT auto-install scheduler (不写 launchd/crontab), 由用户手工决定.

Usage:
    python3 src_python/reflection_scheduler.py --once
    python3 src_python/reflection_scheduler.py          # daemon
"""
from __future__ import annotations
import sys
import time
import sqlite3
from datetime import datetime
from typing import Any, Dict

import db
import reflection_checkers as rc
import reflection_feedback_config as rfc
import sample_quality_filter as sqf
import verifier_feedback as vfb
import analysis_layer_feedback as alf
import kg_feedback as kgf
import reflection_alert as ral
import insight_verifier as ivr
import apply_verification as apv


DEFAULT_INTERVAL_SECONDS = 600  # 10 分钟
DEFAULT_ALERT_THRESHOLD = 5
DEFAULT_ALERT_WINDOW_HOURS = 1
# R5 路径 A：K 线事后验证需等 post-hoc 窗口充分成熟，6 小时验证一次（持久判据见 _verification_due）
VERIFICATION_INTERVAL_SECONDS = 6 * 3600


def _collect_e020_from_settled_trades(conn) -> int:
    """对每个已结算 trade 的 decision 跑 check_sample_integrity, 写 E020 到 decision_errors.

    Returns: 新写入的 decision_errors 行数
    """
    rows = conn.execute(
        "SELECT DISTINCT decision_id FROM quick_sim_trades WHERE settled = 1 AND decision_id IS NOT NULL"
    ).fetchall()
    settled_ids = [r[0] for r in rows]
    written = 0
    for did in settled_ids:
        errors = rc.check_sample_integrity({"id": did}, conn=conn)
        written += rc.save_errors(errors, conn=conn)
    return written


def _verification_due(conn) -> bool:
    """上一轮 verification 距今是否已过 6 小时。持久判据：读 review_insights 最新 verified_at。"""
    try:
        row = conn.execute(
            "SELECT MAX(verified_at) FROM review_insights WHERE verified_at != ''"
        ).fetchone()
        last = row[0] if (row and row[0]) else None
        if not last:
            return True
        last_epoch = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
        return time.time() - last_epoch >= VERIFICATION_INTERVAL_SECONDS
    except Exception:
        return False  # 无法判断时跳过，避免每次 --once 重复全量验证


def _run_insight_verification(conn) -> Dict[str, Any]:
    """R5 路径 A：事后因果验证 review_insights → 写 verification_* 客观记录。

    只写客观验证记录（deactivate_failed=False），不停用任何 insight ——
    XAU placeholder / 0 trades 会让 failed 判定置信度不足，自动停用需等数据地基牢固。
    """
    try:
        apv.ensure_verification_columns()
    except Exception:
        pass
    try:
        results = ivr.verify_all_insights(active_only=True, conn=conn)
    except Exception as exc:
        return {"ran": False, "error": f"verify_all_insights: {type(exc).__name__}: {exc}"}
    try:
        stats = apv.write_verification_results(
            results, dry_run=False, deactivate_failed=False, conn=conn)
    except Exception as exc:
        return {"ran": False, "error": f"write_verification_results: {type(exc).__name__}: {exc}"}
    return {"ran": True, "insights": len(results), **stats}


def run_one_cycle(alert_threshold: int = DEFAULT_ALERT_THRESHOLD,
                  alert_window: int = DEFAULT_ALERT_WINDOW_HOURS,
                  conn=None) -> Dict[str, Any]:
    """跑一个完整周期 (质检 → E020 → L2.1 → L2.2 → L2.3 → L3.2)

    Returns: 全周期结果汇总
    """
    if conn is None:
        conn = db.get_connection()
    conn.row_factory = sqlite3.Row

    started = datetime.now().isoformat(timespec='seconds') + 'Z'
    enabled_codes = rfc.get_enabled_codes()
    result = {
        'started': started,
        'version': 'v231',
        'enabled_codes': enabled_codes,
        'step0_audit': {},
        'step0_5_e020': {},
        'step1_verifier_by_code': {},
        'step2_analysis_layer_by_code': {},
        'step3_kg_by_code': {},
        'step4_alert_by_code': {},
        # backward compat: E020 单值镜像 (老脚本读 result['step3_kg'] 等)
        'step1_verifier': {},
        'step2_analysis_layer': {},
        'step3_kg': {},
        'step4_alert': {},
        'step5_verification': {},
        'step6_logic_analysis': {},  # v231+ E028-E033 形式逻辑反思
    }

    # 0) 质检
    audit = sqf.audit_trades(conn=conn)
    result['step0_audit'] = {
        'total': audit['total'],
        'valid': audit['valid'],
        'invalid': audit['invalid'],
        'by_reason': audit['by_reason'],
    }

    # 0.5) E020 写入 decision_errors (从已结算 trade 的 decision 扫一遍)
    written = _collect_e020_from_settled_trades(conn)
    result['step0_5_e020'] = {'e020_written': written}

    # 1-4) 遍历所有启用错误码 (v204+ 泛化)
    # 每个错误码: verifier → analysis_layer → kg → alert
    # E020 走默认阈值 (policy.kg_threshold=5, alert_threshold=10, window=1h)
    for code in enabled_codes:
        try:
            result['step1_verifier_by_code'][code] = vfb.apply_error_code_to_insights(
                code, limit=500, conn=conn)
        except Exception as exc:
            result['step1_verifier_by_code'][code] = {'error': '{}: {}'.format(type(exc).__name__, exc)}
        try:
            result['step2_analysis_layer_by_code'][code] = alf.record_error_code_to_layer_signals(
                code, limit=500, conn=conn)
        except Exception as exc:
            result['step2_analysis_layer_by_code'][code] = {'error': '{}: {}'.format(type(exc).__name__, exc)}
        try:
            result['step3_kg_by_code'][code] = kgf.check_and_submit_error_code_proposal(
                code, conn=conn)
        except Exception as exc:
            result['step3_kg_by_code'][code] = {'error': '{}: {}'.format(type(exc).__name__, exc)}
        try:
            result['step4_alert_by_code'][code] = ral.check_and_alert_error_code(
                code, conn=conn)
        except Exception as exc:
            result['step4_alert_by_code'][code] = {'error': '{}: {}'.format(type(exc).__name__, exc)}

    # backward compat 镜像 (老脚本读 result['step3_kg'] 之类)
    if 'E020' in enabled_codes:
        result['step1_verifier'] = result['step1_verifier_by_code'].get('E020', {})
        result['step2_analysis_layer'] = result['step2_analysis_layer_by_code'].get('E020', {})
        result['step3_kg'] = result['step3_kg_by_code'].get('E020', {})
        result['step4_alert'] = result['step4_alert_by_code'].get('E020', {})

    # 5) R5 路径 A 事后因果验证（6 小时节流）
    if _verification_due(conn):
        result['step5_verification'] = _run_insight_verification(conn=conn)
    else:
        result['step5_verification'] = {'ran': False, 'reason': 'cooldown'}

    # 6) v231+ logic_analysis: 扫未跑过的 trade_reflections_v2, 跑 6 个 detector (E028-E033).
    #    写到 decision_errors (复用 save_errors) + trade_reflections_v2.{logic_integrity_score, logic_findings_json, logic_checked_at}.
    #    失败不阻塞主循环.
    try:
        import reflection_logic_analysis as rla
        result['step6_logic_analysis'] = rla.enrich_pending_logic(
            batch_size=50, max_per_run=100, conn=conn)
    except Exception as exc:
        result['step6_logic_analysis'] = {'ran': False, 'error': f'{type(exc).__name__}: {exc}'}

    result['finished'] = datetime.now().isoformat(timespec='seconds') + 'Z'
    return result


def print_cycle_report(result: Dict[str, Any], cycle: int = 0) -> None:
    """打印单周期报告 (v204+ 多错误码摘要)."""
    print()
    print('=' * 80)
    print('[L3.1 reflection_scheduler] cycle #{} version={}'.format(
        cycle, result.get('version', 'v202')))
    print('started:', result['started'])
    print('enabled_codes:', result.get('enabled_codes', []))
    print('-' * 80)
    a = result['step0_audit']
    print('step0  audit: total={} valid={} invalid={}'.format(
        a.get('total'), a.get('valid'), a.get('invalid')))
    e = result['step0_5_e020']
    print('step0.5 E020: written={}'.format(e.get('e020_written')))

    # 多错误码 detail (v204+)
    codes = result.get('enabled_codes', [])
    if codes:
        print('-' * 80)
        print('feedback loop ({} codes):'.format(len(codes)))
        print(' {:<6} {:<10} {:<10} {:<10} {:<10}'.format(
            'code', 'verifier', 'analysis', 'kg_prop', 'alert'))
        for code in codes:
            v = result['step1_verifier_by_code'].get(code, {})
            al = result['step2_analysis_layer_by_code'].get(code, {})
            k = result['step3_kg_by_code'].get(code, {})
            al2 = result['step4_alert_by_code'].get(code, {})
            v_str = '{}'.format(v.get('insights_marked', '-'))
            al_str = '{}'.format(al.get('signals_upserted', '-'))
            k_str = '{}'.format(k.get('submitted', '-'))
            al2_str = '{}'.format(al2.get('alerted', '-'))
            print(' {:<6} {:<10} {:<10} {:<10} {:<10}'.format(
                code, v_str, al_str, k_str, al2_str))

    v5 = result.get('step5_verification') or {}
    print('step5  verification: ran={} written={} failed={}'.format(
        v5.get('ran'), v5.get('n_written'), v5.get('n_event_failed')))
    # v231+ step6: logic_analysis
    v6 = result.get('step6_logic_analysis') or {}
    if v6:
        if 'error' in v6:
            print('step6  logic_analysis: error={}'.format(v6['error']))
        else:
            print('step6  logic_analysis: scanned={} enriched={} skipped={} errors={}'.format(
                v6.get('scanned'), v6.get('enriched'), v6.get('skipped'), v6.get('errors')))
    print('finished:', result['finished'])
    print('=' * 80)


def daemon(interval: int = DEFAULT_INTERVAL_SECONDS,
           alert_threshold: int = DEFAULT_ALERT_THRESHOLD,
           alert_window: int = DEFAULT_ALERT_WINDOW_HOURS) -> None:
    """daemon 模式: while True + sleep"""
    cycle = 0
    print('[L3.1 scheduler] daemon started, interval={}s'.format(interval))
    while True:
        cycle += 1
        conn = None
        try:
            conn = db.get_connection()
            result = run_one_cycle(alert_threshold=alert_threshold,
                                   alert_window=alert_window, conn=conn)
            print_cycle_report(result, cycle=cycle)
        except KeyboardInterrupt:
            print('[L3.1 scheduler] interrupted, exit')
            raise
        except Exception as e:
            print('[L3.1 scheduler] cycle #{} error: {}'.format(cycle, e))
        finally:
            # v189-8: 每周期自建连接必须自闭, 否则 daemon 无限循环会按周期
            # 持续泄漏连接 (曾观测到 API 进程累积数十个未关闭连接)。
            if conn is not None:
                conn.close()
        time.sleep(interval)


def main():
    once = '--once' in sys.argv
    interval = DEFAULT_INTERVAL_SECONDS
    if '--interval' in sys.argv:
        try:
            i = sys.argv.index('--interval')
            interval = int(sys.argv[i + 1])
        except (IndexError, ValueError):
            pass
    if once:
        conn = db.get_connection()
        result = run_one_cycle(conn=conn)
        print_cycle_report(result, cycle=1)
    else:
        daemon(interval=interval)


if __name__ == "__main__":
    main()