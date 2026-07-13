import threading
import queue
import uuid
import os
import sys
import time
import logging
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
import config as default_config
from flask import Flask, request, jsonify, Response, session, g, send_from_directory
from functools import wraps
from auth import check_api_connection
from strategy import DeltaNeutralStrategy
from trade_history import record_start, record_end, get_history
from models import init_db, create_user, verify_user, get_user, update_api_keys, get_profiles, get_profile, create_profile, update_profile, delete_profile, get_user_credits, deduct_credits, add_credits, set_user_plan, get_credit_history, is_admin, set_admin, get_all_users, get_all_plans, CREDIT_COSTS, save_strategy, update_strategy_db, get_live_strategies, delete_strategy_db, get_db, save_pnl_snapshot, get_pnl_snapshots
from strategy.tracker import TrackedStrategy, registry
from api.position_tracker import position_tracker

logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder=None)
_secret = os.environ.get('FLASK_SECRET_KEY')
if not _secret:
    import secrets as _secrets
    _secret = _secrets.token_hex(32)
    logger.warning("⚠ FLASK_SECRET_KEY not set — using random key (sessions won't survive restarts)")
app.secret_key = _secret
JWT_SECRET = _secret

init_db()

# Lock protecting strategies, all_tracked, and active_monitors
_state_lock = threading.Lock()

# {sid: {thread, strategy, log_queue, running, params, user_id}}
strategies = {}

# Unified tracker for all strategies from any source
# {sid: {source, name, status, user_id, pnl, started_at, details, ...}}
all_tracked = {}

# {monitor_id: {monitor, user_id, profile_id}}
active_monitors = {}

# Forward declarations for strategy dicts (fully defined later in their sections)
iv_crush_strategies = {}
call_ratio_strategies = {}
oi_strategies = {}
strangle_strategies = {}
portfolio_strangle_strategies = {}
hybrid_strategies = {}
weekly_dn_strategies = {}
ema_spread_strategies = {}
pivot_st_strategies = {}
_futures_traders = {}

# Resume strategies from DB on startup
def _resume_db_strategies():
    from models import get_db
    from strategy.monitor import StrategyMonitor
    import json as _json
    conn = get_db()
    rows = conn.execute("SELECT * FROM live_strategies WHERE status IN ('running', 'open (no monitor)')").fetchall()
    conn.close()
    for r in rows:
        d = dict(r)
        sid = d['sid']
        legs = _json.loads(d['legs']) if d['legs'] else []
        # Handle double-encoded legs (string inside a string)
        if isinstance(legs, str):
            try:
                legs = _json.loads(legs)
            except (ValueError, TypeError):
                legs = []
        if not isinstance(legs, list):
            legs = []
        details = _json.loads(d['details']) if d['details'] else {}
        if isinstance(details, str):
            try:
                details = _json.loads(details)
            except (ValueError, TypeError):
                details = {}
        user_id = d['user_id']
        source = d['source']
        max_profit = d.get('max_profit', 0) or 0
        max_loss = d.get('max_loss', 0) or 0
        asset = d.get('asset', 'BTC')
        lot_size = d.get('lot_size', 0.001) or 0.001
        profile_id = d.get('profile_id')

        # 1. Restore all_tracked
        all_tracked[sid] = {
            'sid': sid, 'source': source, 'name': d['name'],
            'user_id': user_id, 'status': d['status'],
            'started_at': d['started_at'], 'pnl': d['pnl'] or 0,
            'details': details,
        }

        # 2. Restore position_tracker
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            position_tracker.open(user_id, leg.get('product_id'), leg.get('symbol') or '',
                type=leg.get('type') or '', strike=leg.get('strike') or '',
                side=leg.get('side') or '', size=int(leg.get('size') or 0),
                entry_price=float(leg.get('entry_price') or 0),
                asset=asset, source=source)

        # 3. Futures Signal — resume scanning (doesn't require legs)
        if source == 'Futures Signal':
            from strategy.futures_signal_trader import FuturesSignalTrader
            trader = FuturesSignalTrader(
                signal_key=details.get('signal_key', ''),
                asset=details.get('asset', asset),
                timeframe=details.get('timeframe', '15m'),
                lots=int(details.get('lots', 1)),
                scan_interval=int(details.get('scan_interval', 60)),
                max_trades_per_day=int(details.get('max_trades_per_day', 3)),
                api_key='', api_secret='', broker=details.get('broker'),
                profile_id=profile_id,
            )
            trader.sid = sid
            trader.legs = legs  # restore previously filled legs
            trader.last_signal_time = details.get('last_signal_time', 0) or 0
            trader._pending_signal = details.get('pending_signal')
            trader.trades_today = details.get('trades_today', 0) or 0
            if profile_id:
                try:
                    p = get_profile(int(profile_id), user_id)
                    if p:
                        trader._api_key = p['api_key']
                        trader._api_secret = p['api_secret']
                        trader._broker = p.get('broker')
                except Exception:
                    pass
            trader.start()
            _futures_traders[sid] = {'trader': trader, 'user_id': user_id}
            logger.info(f"[resume] Resumed Futures Signal {sid} — {d['name']}")
            continue

        if not legs:
            # Daily-recurring strategies don't need legs to resume — they open trades on schedule
            if source not in ('EMA Spread', 'OI Strategy', 'Daily Strangle', 'Weekly DN',
                              'Portfolio Strangle', 'Hybrid Switch'):
                continue

        # Skip strategies where all legs have no product_id (invalid/empty data)
        valid_legs = [l for l in legs if l.get('product_id')]
        if not valid_legs and source not in ('EMA Spread', 'OI Strategy', 'Daily Strangle',
                                              'Weekly DN', 'Portfolio Strangle', 'Hybrid Switch'):
            logger.warning(f"[resume] Skipping {sid} — no valid legs (missing product_id)")
            all_tracked[sid]['status'] = 'closed'
            try:
                update_strategy_db(sid, status='closed', exit_reason='invalid_legs')
            except Exception:
                pass
            continue

        if source in ('Option Chain', 'Strategy Builder') and max_profit > 0 and max_loss > 0:
            mon = StrategyMonitor(
                legs=legs, max_profit=max_profit, max_loss=max_loss,
                asset=asset, lot_size=lot_size,
            )
            mon.current_pnl = d['pnl'] or 0
            mon.user_id = user_id
            mon.sid = sid
            mon.profile_id = profile_id
            active_monitors[sid] = {'monitor': mon, 'user_id': user_id, 'profile_id': profile_id}
            mon.on_complete = lambda pnl, reason, s=sid: (update_tracked(s, status='completed', pnl=round(pnl, 2)), record_end(s, pnl, 0))
            mon._log("🔄 Resumed after restart")
            mon.start()
            logger.info(f"[resume] Resumed monitor {sid} — {d['name']}")
        # 4. Restore AlgoX DN with full DeltaNeutralStrategy (adjustments enabled)
        elif source == 'AlgoX DN':
            call_leg = next((l for l in legs if l.get('type') == 'call'), None)
            put_leg = next((l for l in legs if l.get('type') == 'put'), None)
            if not call_leg or not put_leg:
                # Can't restore without both legs — fall back to monitor-only
                strat = TrackedStrategy(
                    sid=sid, source=source, name=d['name'],
                    user_id=user_id, legs=legs, asset=asset,
                    lot_size=lot_size, max_profit=max_profit, max_loss=max_loss,
                    profile_id=profile_id, interval=d.get('interval', 10),
                    details=details,
                )
                strat.started_at = d['started_at']
                strat.current_pnl = d['pnl'] or 0
                registry.register(strat)
                strat.start_monitoring()
                logger.info(f"[resume] Resumed DN {sid} as monitor-only (incomplete legs)")
                continue

            # Launch full DN strategy in a background thread (same as /start)
            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False,
                     'params': details, 'user_id': user_id, 'profile_id': profile_id}
            strategies[sid] = entry

            def _resume_dn(sid=sid, entry=entry, details=details, call_leg=call_leg,
                           put_leg=put_leg, asset=asset, lot_size=lot_size,
                           profile_id=profile_id, user_id=user_id, d=d):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] DN {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] DN {sid} — setup failed after 5 attempts")
                    return
                try:
                    s = DeltaNeutralStrategy(
                        asset=details.get('asset', asset),
                        expiry_date=details.get('expiry_date', ''),
                        target_delta=float(details.get('target_delta', 0.20)),
                        delta_tolerance=float(details.get('delta_tolerance', 0.05)),
                        lot_size=int(details.get('lot_size', lot_size)),
                        premium_threshold=float(details.get('premium_threshold', 40)) / 100,
                        target_pnl=float(details.get('target_pnl', 25)),
                        max_adjustments=int(details.get('max_adjustments', 5)),
                        monitoring_interval=int(details.get('monitoring_interval', 5)),
                    )
                    # Restore positions from DB legs (skip order placement)
                    s.call_position = {'product_id': call_leg['product_id'],
                                       'symbol': call_leg['symbol'],
                                       'strike_price': call_leg.get('strike', ''),
                                       'mark_price': call_leg['entry_price']}
                    s.put_position = {'product_id': put_leg['product_id'],
                                      'symbol': put_leg['symbol'],
                                      'strike_price': put_leg.get('strike', ''),
                                      'mark_price': put_leg['entry_price']}
                    s.call_entry_price = call_leg['entry_price']
                    s.put_entry_price = put_leg['entry_price']
                    s.call_actual_entry_price = call_leg['entry_price']
                    s.put_actual_entry_price = put_leg['entry_price']
                    s.adjustment_count = d.get('adjustment_count', 0) or 0
                    s.cumulative_realized_pnl = d.get('pnl', 0) or 0

                    entry['strategy'] = s
                    entry['running'] = True

                    # Start WebSocket for live prices + adjustment triggers
                    s.ws_manager.start()
                    import time as _t; _t.sleep(2)
                    symbols = [s.call_position['symbol'], s.put_position['symbol']]
                    s.ws_manager.subscribe(symbols)

                    _save_dn_legs(sid, s)
                    _orig_adjust = s.adjust_position
                    def _hooked_adjust(*a, **kw):
                        _orig_adjust(*a, **kw)
                        _save_dn_legs(sid, s)
                    s.adjust_position = _hooked_adjust

                    s.monitor_and_adjust()
                except Exception as e:
                    logger.error(f"[resume] DN {sid} error: {e}")
                    if entry.get('strategy'):
                        entry['strategy'].close_all_positions()
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = strategy.cumulative_realized_pnl if strategy else 0
                        adj = strategy.adjustment_count if strategy else 0
                        _save_dn_legs(sid, strategy)
                        record_end(sid, pnl, adj)
                        update_tracked(sid, status='completed', pnl=round(pnl, 2),
                                       exit_reason='intentional_close')
                        if strategy:
                            strategy.ws_manager.stop()
                    elif not strategy:
                        logger.warning(f"[resume] DN {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        logger.warning(f"[resume] DN {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                        if strategy:
                            _save_dn_legs(sid, strategy)
                            strategy.ws_manager.stop()
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_dn, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed DN strategy {sid} with full adjustments")

        # 5. Restore IV Crush with full IVCrushStrategy
        elif source == 'IV Crush':
            call_leg = next((l for l in legs if l.get('type') == 'call'), None)
            put_leg = next((l for l in legs if l.get('type') == 'put'), None)
            if not call_leg or not put_leg:
                strat = TrackedStrategy(sid=sid, source=source, name=d['name'],
                    user_id=user_id, legs=legs, asset=asset, lot_size=lot_size,
                    max_profit=max_profit, max_loss=max_loss, profile_id=profile_id,
                    interval=d.get('interval', 10), details=details)
                strat.started_at = d['started_at']
                strat.current_pnl = d['pnl'] or 0
                registry.register(strat)
                strat.start_monitoring()
                continue

            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            iv_crush_strategies[sid] = entry

            def _resume_iv(sid=sid, entry=entry, details=details, call_leg=call_leg,
                           put_leg=put_leg, asset=asset, user_id=user_id, d=d):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] IV Crush {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] IV Crush {sid} — setup failed after 5 attempts")
                    return
                try:
                    from strategy.iv_crush import IVCrushStrategy
                    s = IVCrushStrategy(
                        asset=details.get('asset', asset),
                        expiry_date=details.get('expiry_date', ''),
                        lot_size=int(details.get('lot_size', 10)),
                        iv_rv_threshold=float(details.get('iv_rv_threshold', 1.3)),
                        max_loss_pct=float(details.get('max_loss_pct', 50)),
                        target_profit_pct=float(details.get('target_profit_pct', 30)),
                        monitoring_interval=int(details.get('monitoring_interval', 10)),
                    )
                    s.call_position = {'product_id': call_leg['product_id'],
                                       'symbol': call_leg['symbol'],
                                       'strike_price': call_leg.get('strike', '')}
                    s.put_position = {'product_id': put_leg['product_id'],
                                      'symbol': put_leg['symbol'],
                                      'strike_price': put_leg.get('strike', '')}
                    s.call_entry_price = call_leg['entry_price']
                    s.put_entry_price = put_leg['entry_price']
                    s.call_contract_value = call_leg.get('contract_value', 0.001)
                    s.put_contract_value = put_leg.get('contract_value', 0.001)
                    entry['strategy'] = s
                    entry['running'] = True
                    s.ws_manager.start()
                    import time as _t; _t.sleep(2)
                    s.ws_manager.subscribe([call_leg['symbol'], put_leg['symbol']])
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] IV Crush {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, 'total_pnl', 0), 2)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=pnl,
                                       exit_reason='intentional_close')
                        if strategy:
                            strategy.ws_manager.stop()
                    elif not strategy:
                        logger.warning(f"[resume] IV Crush {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        logger.warning(f"[resume] IV Crush {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                        if strategy:
                            strategy.ws_manager.stop()
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_iv, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed IV Crush {sid} with full monitoring")

        # 6. Restore Call Ratio with full CallRatioStrategy
        elif source == 'Call Ratio':
            if len(legs) < 2:
                strat = TrackedStrategy(sid=sid, source=source, name=d['name'],
                    user_id=user_id, legs=legs, asset=asset, lot_size=lot_size,
                    max_profit=max_profit, max_loss=max_loss, profile_id=profile_id,
                    interval=d.get('interval', 10), details=details)
                strat.started_at = d['started_at']
                strat.current_pnl = d['pnl'] or 0
                registry.register(strat)
                strat.start_monitoring()
                continue

            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            call_ratio_strategies[sid] = entry

            def _resume_cr(sid=sid, entry=entry, details=details, legs=legs,
                           asset=asset, user_id=user_id, d=d):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] Call Ratio {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] Call Ratio {sid} — setup failed after 5 attempts")
                    return
                try:
                    from strategy.call_ratio import CallRatioStrategy
                    s = CallRatioStrategy(
                        asset=details.get('asset', asset),
                        expiry_date=details.get('expiry_date', ''),
                        lot_size=int(details.get('lot_size', 10)),
                        buy_offset_pct=float(details.get('buy_offset_pct', 2)),
                        sell_offset_pct=float(details.get('sell_offset_pct', 4)),
                        hedge_offset_pct=float(details.get('hedge_offset_pct', 7)),
                        target_pct=float(details.get('target_pct', 5)),
                        sl_pct=float(details.get('sl_pct', 8)),
                        monitoring_interval=int(details.get('monitoring_interval', 30)),
                    )
                    s.legs = legs
                    entry['strategy'] = s
                    entry['running'] = True
                    s.ws_manager.start()
                    import time as _t; _t.sleep(2)
                    s.ws_manager.subscribe([l['symbol'] for l in legs if l.get('symbol')])
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] Call Ratio {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, 'total_pnl', 0), 2)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=pnl,
                                       exit_reason='intentional_close')
                        if strategy:
                            strategy.ws_manager.stop()
                    elif not strategy:
                        logger.warning(f"[resume] Call Ratio {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        logger.warning(f"[resume] Call Ratio {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                        if strategy:
                            strategy.ws_manager.stop()
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_cr, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed Call Ratio {sid} with full monitoring")

        # 7. Restore OI Strategy
        elif source == 'OI Strategy':
            if not legs:
                continue
            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            oi_strategies[sid] = entry

            def _resume_oi(sid=sid, entry=entry, details=details, legs=legs,
                           asset=asset, user_id=user_id, d=d):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] OI Strategy {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] OI Strategy {sid} — setup failed after 5 attempts")
                    return
                try:
                    from strategy.oi_strategy import OIStrategy
                    s = OIStrategy(
                        asset=details.get('asset', asset),
                        lot_size=int(details.get('lot_size', 100)),
                        target_pct=float(details.get('target_pct', 50)) / 100,
                        stop_loss_pct=float(details.get('stop_loss_pct', 50)) / 100,
                        monitor_interval=int(details.get('monitoring_interval', 30)),
                        entry_hour=int(details.get('entry_hour', 18)),
                        entry_minute=int(details.get('entry_minute', 30)),
                    )
                    s.legs = legs
                    from config import get_contract_value
                    cv = get_contract_value(asset)
                    s.max_premium = sum(l['entry_price'] * l['size'] * cv for l in legs)
                    s.cumulative_pnl = float(details.get('cumulative_pnl', 0))
                    s.total_days_traded = int(details.get('total_days_traded', 0))
                    s.trade_log = details.get('trade_log', [])
                    s._running = True
                    entry['strategy'] = s
                    s._log_queue = entry['log_queue']
                    s._log_history = entry['log_history']
                    import config as _cfg
                    s._api_key = _cfg.get_api_key()
                    s._api_secret = _cfg.get_api_secret()
                    s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
                    entry['running'] = True
                    # Resume monitoring for any open legs
                    if legs:
                        import threading as _thr
                        premium = sum(l['entry_price'] * l['size'] * cv for l in legs)
                        day_num = s.total_days_traded or 1
                        _thr.Thread(target=s._monitor_day_trade,
                                    args=(legs, premium, day_num), daemon=True).start()
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] OI Strategy {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, '_pnl', 0), 2)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=pnl,
                                       exit_reason='intentional_close')
                    elif not strategy:
                        logger.warning(f"[resume] OI Strategy {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        logger.warning(f"[resume] OI Strategy {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_oi, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed OI Strategy {sid}")

        # 8. Restore Weekly Delta Neutral
        elif source == 'Weekly DN':
            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            weekly_dn_strategies[sid] = entry

            def _resume_wdn(sid=sid, entry=entry, details=details, user_id=user_id):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] Weekly DN {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] Weekly DN {sid} — setup failed after 5 attempts")
                    return
                try:
                    from strategy.weekly_delta_neutral import WeeklyDeltaNeutral
                    s = WeeklyDeltaNeutral(
                        asset=details.get('asset', 'BTC'),
                        target_delta=float(details.get('target_delta', 0.20)),
                        delta_tolerance=float(details.get('delta_tolerance', 0.05)),
                        lot_size=int(details.get('lot_size', 100)),
                        premium_threshold=float(details.get('premium_threshold', 40)) / 100,
                        target_pnl=float(details.get('target_pnl', 25)),
                        max_adjustments=int(details.get('max_adjustments', 5)),
                        monitoring_interval=int(details.get('monitoring_interval', 5)),
                        entry_hour=int(details.get('entry_hour', 21)),
                        entry_minute=int(details.get('entry_minute', 0)),
                    )
                    entry['strategy'] = s
                    entry['running'] = True
                    s.cumulative_pnl = float(details.get('cumulative_pnl', 0))
                    s.weeks_traded = int(details.get('weeks_traded', 0))
                    s.trade_log = details.get('trade_log', [])
                    s.initialize()
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] Weekly DN {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, 'cumulative_pnl', 0), 2)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=pnl,
                                       exit_reason='intentional_close')
                    elif not strategy:
                        logger.warning(f"[resume] Weekly DN {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        logger.warning(f"[resume] Weekly DN {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_wdn, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed Weekly DN {sid}")

        # 9. Restore EMA Credit Spread
        elif source == 'EMA Spread':
            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            ema_spread_strategies[sid] = entry

            def _resume_ecs(sid=sid, entry=entry, details=details, user_id=user_id, legs=legs):
                # Retry setup up to 5 times with backoff — handles transient API failures on restart
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] EMA Spread {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] EMA Spread {sid} — setup failed after 5 attempts, will retry on next restart")
                    return
                try:
                    from strategy.ema_credit_spread import EMACreditSpread
                    s = EMACreditSpread(
                        asset=details.get('asset', 'BTC'),
                        lot_size=int(details.get('lot_size', 100)),
                        sell_delta=float(details.get('sell_delta', 0.20)),
                        buy_delta=float(details.get('buy_delta', 0.10)),
                        ema_period=int(details.get('ema_period', 14)),
                        tp_pct=float(details.get('tp_pct', 90)) / 100,
                        sl_pct=float(details.get('sl_pct', 100)) / 100,
                        monitor_interval=int(details.get('monitoring_interval', 30)),
                        entry_hour=int(details.get('entry_hour', 18)),
                        entry_minute=int(details.get('entry_minute', 30)),
                        min_expiry_days=int(details.get('min_expiry_days', 8)),
                    )
                    entry['strategy'] = s
                    s._log_queue = entry['log_queue']
                    s._log_history = entry['log_history']
                    s._sid = sid
                    import config as _cfg
                    s._api_key = _cfg.get_api_key()
                    s._api_secret = _cfg.get_api_secret()
                    s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
                    entry['running'] = True
                    s.cumulative_pnl = float(details.get('cumulative_pnl', 0))
                    s.total_days_traded = int(details.get('total_days_traded', 0))
                    s.trade_log = details.get('trade_log', [])
                    s.legs = legs or []
                    s.initialize()
                    # Log restored trade history
                    if s.trade_log:
                        for t in s.trade_log:
                            print(f"[EMA Day{t.get('day',0)}] {t.get('date','')} | {t.get('direction','')} | {t.get('exit_reason','')} | PnL: ${t.get('pnl',0):+.4f}")
                        print(f"[EMA Spread] Restored {len(s.trade_log)} days | Cum PnL: ${s.cumulative_pnl:+.4f}")
                    # Resume monitoring for open legs
                    if legs:
                        import threading as _thr
                        from config import get_contract_value
                        cv = get_contract_value(details.get('asset', 'BTC'))
                        sell_legs = [l for l in legs if l.get('side') == 'sell']
                        buy_legs = [l for l in legs if l.get('side') == 'buy']
                        premium = sum((l['entry_price'] for l in sell_legs), 0) - sum((l['entry_price'] for l in buy_legs), 0)
                        premium *= int(details.get('lot_size', 100)) * cv
                        day_num = s.total_days_traded or 1
                        direction = 'bear_call' if any(l.get('type') == 'call' for l in legs) else 'bull_put'
                        _thr.Thread(target=s._monitor_day_trade,
                                    args=(legs, premium, day_num, direction), daemon=True).start()
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] EMA Spread {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    # Only mark as completed if the strategy intentionally stopped
                    # (i.e., _running was set to False by close_all or user action).
                    # If it crashed or never ran, leave status as 'running' so it
                    # gets resumed on next restart.
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, 'cumulative_pnl', 0), 4)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=round(pnl, 2),
                                       exit_reason='intentional_close')
                    elif not strategy:
                        # Strategy object never created — setup failed, don't close
                        logger.warning(f"[resume] EMA Spread {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        # Strategy crashed — log but keep status as running for next restart
                        logger.warning(f"[resume] EMA Spread {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_ecs, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed EMA Spread {sid}")

        # 10. Resume Daily Strangle
        elif source == 'Daily Strangle':
            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            strangle_strategies[sid] = entry

            def _resume_strangle(sid=sid, entry=entry, details=details, user_id=user_id, legs=legs):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] Daily Strangle {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] Daily Strangle {sid} — setup failed after 5 attempts")
                    return
                try:
                    from strategy.daily_strangle import DailyStrangle
                    s = DailyStrangle(
                        asset=details.get('asset', 'BTC'),
                        lot_size=int(details.get('lot_size', 100)),
                        target_premium=float(details.get('target_premium', 100)),
                        sl_pct=float(details.get('sl_pct', 105)) / 100,
                        entry_hour=int(details.get('entry_hour', 9)),
                        entry_minute=int(details.get('entry_minute', 0)),
                        exit_hour=int(details.get('exit_hour', 17)),
                        exit_minute=int(details.get('exit_minute', 15)),
                        monitor_interval=int(details.get('monitoring_interval', 10)),
                    )
                    entry['strategy'] = s
                    s._log_queue = entry['log_queue']
                    s._log_history = entry['log_history']
                    import config as _cfg
                    s._api_key = _cfg.get_api_key()
                    s._api_secret = _cfg.get_api_secret()
                    s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
                    entry['running'] = True
                    s.cumulative_pnl = float(details.get('cumulative_pnl', 0))
                    s.total_days_traded = int(details.get('total_days_traded', 0))
                    s.trade_log = details.get('trade_log', [])
                    s.legs = legs or []
                    s._sid = sid
                    s.initialize()
                    # Log restored trade history
                    if s.trade_log:
                        for t in s.trade_log:
                            print(f"[Strangle Day{t.get('day',0)}] {t.get('date','')} | {t.get('exit_reason','')} | PnL: ${t.get('pnl',0):+.2f}")
                        print(f"[Strangle] Restored {len(s.trade_log)} days | Cum PnL: ${s.cumulative_pnl:+.2f}")
                    if legs:
                        import threading as _thr
                        day_num = s.total_days_traded or 1
                        _thr.Thread(target=s._monitor_day,
                                    args=(legs, day_num), daemon=True).start()
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] Daily Strangle {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, 'cumulative_pnl', 0), 2)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=pnl,
                                       exit_reason='intentional_close')
                    elif not strategy:
                        logger.warning(f"[resume] Daily Strangle {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        logger.warning(f"[resume] Daily Strangle {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_strangle, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed Daily Strangle {sid}")

        # 10b. Resume Pivot SuperTrend
        elif source == 'Pivot SuperTrend':
            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            pivot_st_strategies[sid] = entry

            def _resume_pivot_st(sid=sid, entry=entry, details=details, user_id=user_id, legs=legs):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] PivotST {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] PivotST {sid} — setup failed after 5 attempts")
                    return
                try:
                    from strategy.pivot_supertrend import PivotSuperTrend
                    s = PivotSuperTrend(
                        asset=details.get('asset', 'BTC'),
                        lot_size=int(details.get('lot_size', 100)),
                        target_delta=float(details.get('target_delta', 0.50)),
                        delta_tolerance=float(details.get('delta_tolerance', 0.15)),
                        st_period=int(details.get('st_period', 7)),
                        st_multiplier=int(details.get('st_multiplier', 3)),
                        max_trades=int(details.get('max_trades', 3)),
                        monitor_interval=int(details.get('monitoring_interval', 10)),
                        entry_hour=int(details.get('entry_hour', 9)),
                        entry_minute=int(details.get('entry_minute', 20)),
                        exit_hour=int(details.get('exit_hour', 17)),
                        exit_minute=int(details.get('exit_minute', 0)),
                    )
                    entry['strategy'] = s
                    s._log_queue = entry['log_queue']
                    s._log_history = entry['log_history']
                    import config as _cfg
                    s._api_key = _cfg.get_api_key()
                    s._api_secret = _cfg.get_api_secret()
                    s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
                    entry['running'] = True
                    s.cumulative_pnl = float(details.get('cumulative_pnl', 0))
                    s.total_days_traded = int(details.get('total_days_traded', 0))
                    s.trade_log = details.get('trade_log', [])
                    s.legs = legs or []
                    s._sid = sid
                    s.initialize()
                    if s.trade_log:
                        print(f"[PivotST] Restored {len(s.trade_log)} days | Cum PnL: ${s.cumulative_pnl:+.4f}")
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] PivotST {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, 'cumulative_pnl', 0), 4)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=pnl,
                                       exit_reason='intentional_close')
                    elif not strategy:
                        logger.warning(f"[resume] PivotST {sid} — thread exited without strategy object")
                    else:
                        logger.warning(f"[resume] PivotST {sid} — thread exited unexpectedly, keeping status 'running'")
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_pivot_st, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed Pivot SuperTrend {sid}")

        # 11. Resume Hybrid Switch
        elif source == 'Hybrid Switch':
            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            hybrid_strategies[sid] = entry

            def _resume_hybrid(sid=sid, entry=entry, details=details, user_id=user_id):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] Hybrid Switch {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] Hybrid Switch {sid} — setup failed after 5 attempts")
                    return
                try:
                    from strategy.hybrid_switch import HybridSwitch
                    s = HybridSwitch(
                        asset=details.get('asset', 'BTC'),
                        lot_size=int(details.get('lot_size', 1)),
                        buy_multiplier=int(details.get('buy_multiplier', 10)),
                        sell_sl_pct=float(details.get('sell_sl_pct', 200)) / 100,
                        buy_sl_pct=float(details.get('buy_sl_pct', 50)) / 100,
                        trail_points=float(details.get('trail_points', 10)),
                        otm_index=int(details.get('otm_index', 5)),
                        entry_hour=int(details.get('entry_hour', 19)),
                        entry_minute=int(details.get('entry_minute', 15)),
                        exit_hour=int(details.get('exit_hour', 17)),
                        exit_minute=int(details.get('exit_minute', 15)),
                        monitor_interval=int(details.get('monitoring_interval', 10)),
                    )
                    entry['strategy'] = s
                    s._log_queue = entry['log_queue']
                    s._log_history = entry['log_history']
                    import config as _cfg
                    s._api_key = _cfg.get_api_key()
                    s._api_secret = _cfg.get_api_secret()
                    s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
                    entry['running'] = True
                    s.cumulative_pnl = float(details.get('cumulative_pnl', 0))
                    s.total_days_traded = int(details.get('total_days_traded', 0))
                    s.trade_log = details.get('trade_log', [])
                    s._sid = sid
                    # Restore open legs from DB
                    s.legs = legs or []
                    s.initialize()
                    # Log restored trade history
                    if s.trade_log:
                        for t in s.trade_log:
                            print(f"[Hybrid Day{t.get('day',0)}] {t.get('date','')} | {t.get('direction','')} | {t.get('exit_reason','')} | PnL: ${t.get('pnl',0):+.2f}")
                        print(f"[Hybrid] Restored {len(s.trade_log)} days | Cum PnL: ${s.cumulative_pnl:+.2f}")
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] Hybrid Switch {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, 'cumulative_pnl', 0), 2)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=pnl,
                                       exit_reason='intentional_close')
                    elif not strategy:
                        logger.warning(f"[resume] Hybrid Switch {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        logger.warning(f"[resume] Hybrid Switch {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_hybrid, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed Hybrid Switch {sid}")

        # 11b. Resume Portfolio Strangle
        elif source == 'Portfolio Strangle':
            entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500),
                     'log_history': [], 'running': False, 'params': details,
                     'user_id': user_id, 'profile_id': profile_id}
            portfolio_strangle_strategies[sid] = entry

            def _resume_portfolio(sid=sid, entry=entry, details=details, user_id=user_id, legs=legs):
                for attempt in range(5):
                    if _setup_strategy_thread(entry):
                        break
                    logger.warning(f"[resume] Portfolio Strangle {sid} — setup failed (attempt {attempt+1}/5), retrying in 30s")
                    entry['running'] = False
                    time.sleep(30)
                else:
                    logger.error(f"[resume] Portfolio Strangle {sid} — setup failed after 5 attempts")
                    return
                try:
                    from strategy.portfolio_strangle import PortfolioStrangle
                    # Parse entry_times
                    entry_times_raw = details.get('entry_times', ['9:15', '10:20', '11:15'])
                    entry_times = []
                    for t in entry_times_raw:
                        parts = t.split(':')
                        entry_times.append((int(parts[0]), int(parts[1])))
                    skip_days_raw = details.get('skip_weekdays', [4, 6])
                    skip_days = [int(d) for d in skip_days_raw]

                    s = PortfolioStrangle(
                        asset=details.get('asset', 'BTC'),
                        lot_size=int(details.get('lot_size', 30)),
                        sl_pct=float(details.get('sl_pct', 300)) / 100,
                        recost_entries=int(details.get('recost_entries', 1)),
                        otm_index=int(details.get('otm_index', 5)),
                        entry_times=entry_times,
                        exit_hour=int(details.get('exit_hour', 17)),
                        exit_minute=int(details.get('exit_minute', 29)),
                        monitor_interval=int(details.get('monitoring_interval', 10)),
                        skip_weekdays=skip_days,
                    )
                    entry['strategy'] = s
                    s._log_queue = entry['log_queue']
                    s._log_history = entry['log_history']
                    s._sid = sid
                    import config as _cfg
                    s._api_key = _cfg.get_api_key()
                    s._api_secret = _cfg.get_api_secret()
                    s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
                    entry['running'] = True
                    s.cumulative_pnl = float(details.get('cumulative_pnl', 0))
                    s.total_days_traded = int(details.get('total_days_traded', 0))
                    s.trade_log = details.get('trade_log', [])
                    s.legs = legs or []
                    s.initialize()
                    # Log restored trade history
                    if s.trade_log:
                        for t in s.trade_log:
                            print(f"[Portfolio D{t.get('day',0)}] {t.get('date','')} | {t.get('exit_reason','')} | PnL: ${t.get('pnl',0):+.4f}")
                        print(f"[Portfolio] Restored {len(s.trade_log)} days | Cum PnL: ${s.cumulative_pnl:+.4f}")
                    # If there are active (non-stopped) legs from before restart,
                    # monitor them in a background thread while main loop waits for next entry
                    active_legs = [l for l in s.legs if not l.get('stopped', False)]
                    if active_legs:
                        import threading as _thr
                        day_num = s.total_days_traded or 1
                        day_legs_all = [{
                            'legs': active_legs,
                            'slot': 1,
                            'entry_time': 'restored',
                            'recost_used': {l['type']: True for l in active_legs},  # no recost on restored legs
                            'sl_hit': {l['type']: False for l in active_legs},
                        }]
                        _thr.Thread(target=s._monitor_all_slots,
                                    args=(f"[Portfolio D{day_num}]", day_legs_all, day_num),
                                    daemon=True).start()
                        print(f"[Portfolio] Resumed monitoring {len(active_legs)} active legs")
                    s.monitor()
                except Exception as e:
                    logger.error(f"[resume] Portfolio Strangle {sid} error: {e}")
                finally:
                    strategy = entry.get('strategy')
                    if strategy and not strategy._running:
                        pnl = round(getattr(strategy, 'cumulative_pnl', 0), 4)
                        record_end(sid, pnl, 0)
                        update_tracked(sid, status='completed', pnl=round(pnl, 4),
                                       exit_reason='intentional_close')
                    elif not strategy:
                        logger.warning(f"[resume] Portfolio Strangle {sid} — thread exited without strategy object, keeping status 'running'")
                    else:
                        logger.warning(f"[resume] Portfolio Strangle {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
                    _teardown_strategy_thread(entry)

            t = threading.Thread(target=_resume_portfolio, daemon=True)
            entry['thread'] = t
            t.start()
            logger.info(f"[resume] Resumed Portfolio Strangle {sid}")

        # 12. Everything else — use TrackedStrategy
        else:
            strat = TrackedStrategy(
                sid=sid, source=source, name=d['name'],
                user_id=user_id, legs=legs, asset=asset,
                lot_size=lot_size, max_profit=max_profit, max_loss=max_loss,
                profile_id=profile_id, interval=d.get('interval', 10),
                details=details,
            )
            strat.started_at = d['started_at']
            strat.current_pnl = d['pnl'] or 0
            strat.adjustment_count = d.get('adjustment_count', 0)
            strat.log("🔄 Resumed after restart")
            registry.register(strat)
            strat.start_monitoring()
            logger.info(f"[resume] Resumed strategy {sid} — {d['name']}")

_db_resumed = False

@app.before_request
def _resume_once():
    global _db_resumed
    if not _db_resumed:
        _db_resumed = True
        _resume_db_strategies()

def track_strategy(sid, source, name, user_id, details=None):
    """Register a strategy in the unified tracker."""
    with _state_lock:
        all_tracked[sid] = {
            'sid': sid, 'source': source, 'name': name,
            'user_id': user_id, 'status': 'running',
            'started_at': datetime.now().isoformat(),
            'pnl': 0, 'details': details or {},
        }
        started_at = all_tracked[sid]['started_at']
    try:
        legs = (details or {}).get('legs', [])
        save_strategy(sid, user_id, source, name, 'running',
                      started_at, details=details, legs=legs,
                      max_profit=(details or {}).get('max_profit', 0),
                      max_loss=(details or {}).get('max_loss', 0),
                      profile_id=(details or {}).get('profile_id'),
                      asset=(details or {}).get('asset', 'BTC'))
    except Exception:
        pass

def update_tracked(sid, **kwargs):
    with _state_lock:
        if sid in all_tracked:
            all_tracked[sid].update(kwargs)
    try:
        update_strategy_db(sid, **{k: v for k, v in kwargs.items()
                                   if k in ('status', 'pnl', 'details', 'legs', 'exit_reason', 'adjustment_count')})
    except Exception:
        pass


def _get_jwt_user_id():
    """Extract user_id from JWT Bearer token or query param."""
    token = None
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
    if not token:
        token = request.args.get('token')
    if token:
        try:
            payload = pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            return payload.get('user_id')
        except pyjwt.ExpiredSignatureError:
            return None
        except pyjwt.InvalidTokenError:
            return None
    return None


def current_user_id():
    uid = _get_jwt_user_id()
    if uid:
        return uid
    return session.get('user_id')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = current_user_id()
        if not uid:
            if request.path.startswith('/api/'):
                return jsonify(error='Unauthorized'), 401
            return jsonify(error='Unauthorized'), 401
        g.user_id = uid
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = current_user_id()
        if not uid or not is_admin(uid):
            return jsonify(error='Admin access required'), 403
        return f(*args, **kwargs)
    return decorated


def credits_required(action):
    """Decorator that checks and deducts credits before running the endpoint."""
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            uid = current_user_id()
            ok, cost = deduct_credits(uid, action, f.__name__)
            if not ok:
                creds = get_user_credits(uid)
                return jsonify(error=f'Insufficient credits. Need {cost}, have {creds["credits_remaining"] if creds else 0}. Upgrade your plan.'), 402
            return f(*args, **kwargs)
        return decorated
    return wrapper


def _make_token(user_id):
    return pyjwt.encode({'user_id': user_id, 'exp': datetime.now(tz=timezone.utc) + timedelta(days=7)}, JWT_SECRET, algorithm='HS256')


# ── JWT Auth API ──

@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    d = request.json or {}
    user = verify_user(d.get('username', ''), d.get('password', ''))
    if not user:
        return jsonify(error='Invalid credentials'), 401
    return jsonify(token=_make_token(user['id']), user={'id': user['id'], 'username': user['username'], 'is_admin': bool(user.get('is_admin'))})


@app.route('/api/auth/register', methods=['POST'])
def api_auth_register():
    d = request.json or {}
    username = (d.get('username') or '').strip()
    password = d.get('password', '')
    if not username or len(password) < 6:
        return jsonify(error='Username required, password min 6 chars'), 400
    if not create_user(username, password):
        return jsonify(error='Username already taken'), 400
    user = verify_user(username, password)
    return jsonify(token=_make_token(user['id']), user={'id': user['id'], 'username': user['username'], 'is_admin': False})


# ── Serve React frontend ──

# React catch-all moved to end of file


class LogCapture:
    """Thread-aware stdout that routes print() to the correct strategy's log queue.
    Kept as fallback for any remaining print() calls or third-party library output."""
    _local = threading.local()

    def __init__(self, original):
        self.original = original

    def write(self, text):
        self.original.write(text)
        q = getattr(LogCapture._local, 'log_queue', None)
        if q and text.strip():
            try:
                q.put_nowait(text.strip())
            except queue.Full:
                # Discard oldest message to make room
                try:
                    q.get_nowait()
                    q.put_nowait(text.strip())
                except (queue.Empty, queue.Full):
                    pass
        h = getattr(LogCapture._local, 'log_history', None)
        if h is not None and text.strip():
            h.append(text.strip())
            if len(h) > 200:
                del h[:len(h)-200]

    def flush(self):
        self.original.flush()


class _StrategyQueueHandler(logging.Handler):
    """Logging handler that routes log records to the thread-local strategy queue."""
    def emit(self, record):
        msg = self.format(record)
        q = getattr(LogCapture._local, 'log_queue', None)
        if q and msg.strip():
            try:
                q.put_nowait(msg.strip())
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(msg.strip())
                except (queue.Empty, queue.Full):
                    pass
        h = getattr(LogCapture._local, 'log_history', None)
        if h is not None and msg.strip():
            h.append(msg.strip())
            if len(h) > 200:
                del h[:len(h)-200]


def _setup_logging():
    """Configure root logger with both console and strategy-queue handlers."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        console = logging.StreamHandler(sys.__stderr__)
        console.setFormatter(logging.Formatter('%(message)s'))
        root.addHandler(console)
        queue_handler = _StrategyQueueHandler()
        queue_handler.setFormatter(logging.Formatter('%(message)s'))
        root.addHandler(queue_handler)


_setup_logging()


def _save_dn_legs(sid, s):
    """Extract call/put legs from a DeltaNeutralStrategy and persist to DB."""
    legs = []
    for leg_name in ('call', 'put'):
        pos = getattr(s, f'{leg_name}_position', None)
        if pos:
            legs.append({
                'product_id': pos.get('product_id'),
                'symbol': pos.get('symbol', ''),
                'type': leg_name,
                'strike': pos.get('strike_price', ''),
                'side': 'sell',
                'size': s.lot_size,
                'entry_price': round(getattr(s, f'{leg_name}_actual_entry_price', 0), 2),
            })
    if legs:
        try:
            update_strategy_db(sid, legs=legs, adjustment_count=s.adjustment_count)
        except Exception:
            pass


# Install once at import time — all threads share this, but each routes to its own queue
import sys
sys.stdout = LogCapture(sys.stdout)


def _setup_strategy_thread(entry):
    """Common setup for strategy runner threads: log routing + credential resolution.
    Returns True if setup succeeded, False if it failed (error already logged to queue)."""
    LogCapture._local.log_queue = entry['log_queue']
    LogCapture._local.log_history = entry['log_history']
    from config import set_thread_credentials
    profile_id = entry.get('profile_id')
    if not profile_id:
        entry['log_queue'].put("❌ No profile selected. Please select an API profile.")
        entry['running'] = False
        return False
    p = get_profile(int(profile_id), entry['user_id'])
    if not p:
        entry['log_queue'].put("❌ Profile not found.")
        entry['running'] = False
        return False
    set_thread_credentials(p['api_key'], p['api_secret'], p.get('broker'))
    if not check_api_connection():
        entry['log_queue'].put("❌ Cannot connect to API")
        entry['running'] = False
        return False
    return True


def _teardown_strategy_thread(entry):
    """Common cleanup for strategy runner threads."""
    LogCapture._local.log_queue = None
    LogCapture._local.log_history = None
    entry['running'] = False
    entry['log_queue'].put("__STOPPED__")


def run_strategy(sid, params):
    entry = strategies[sid]
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        s = DeltaNeutralStrategy(
            asset=params.get('asset', 'BTC'),
            expiry_date=params['expiry_date'],
            target_delta=float(params['target_delta']),
            delta_tolerance=float(params['delta_tolerance']),
            lot_size=int(params['lot_size']),
            premium_threshold=float(params['premium_threshold']) / 100,
            target_pnl=float(params['target_pnl']),
            max_adjustments=int(params['max_adjustments']),
            monitoring_interval=int(params['monitoring_interval']),
        )

        entry['strategy'] = s
        entry['running'] = True

        if not s.initialize():
            entry['log_queue'].put("✗ Strategy initialization failed")
            s.ws_manager.stop()
            entry['running'] = False
            return

        # Save legs to DB so they survive restart
        _save_dn_legs(sid, s)

        # Hook: save legs after each adjustment
        _orig_adjust = s.adjust_position
        def _hooked_adjust(*a, **kw):
            _orig_adjust(*a, **kw)
            _save_dn_legs(sid, s)
        s.adjust_position = _hooked_adjust

        s.monitor_and_adjust()
    except Exception as e:
        entry['log_queue'].put(f"✗ Error: {e}")
        if entry.get('strategy'):
            entry['strategy'].close_all_positions()
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = strategy.cumulative_realized_pnl
            adj = strategy.adjustment_count
            _save_dn_legs(sid, strategy)
            record_end(sid, pnl, adj)
            update_tracked(sid, status='completed', pnl=round(pnl, 2),
                           exit_reason='intentional_close')
            strategy.ws_manager.stop()
        elif not strategy:
            logger.warning(f"[deploy] DN {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] DN {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
            _save_dn_legs(sid, strategy)
            strategy.ws_manager.stop()
        _teardown_strategy_thread(entry)


# ── Old template routes removed — React frontend serves all pages ──


# ── Profile API ──

def get_profile_creds(profile_id):
    """Get API credentials + broker from a profile, or fall back to user's default keys."""
    uid = current_user_id()
    if profile_id:
        p = get_profile(int(profile_id), uid)
        if p:
            return p['api_key'], p['api_secret'], p['name'], p.get('broker', 'demo')
    user = get_user(uid)
    if user and user.get('api_key') and user.get('api_secret'):
        return user['api_key'], user['api_secret'], 'Default', 'demo'
    return None, None, None, None


@app.route('/api/profiles')
@login_required
def api_profiles():
    return jsonify(profiles=get_profiles(current_user_id()))


@app.route('/api/brokers')
@login_required
def api_brokers():
    from config import BROKERS
    return jsonify(brokers=[
        {'id': k, 'name': mod.BROKER_NAME}
        for k, mod in BROKERS.items()
    ])


@app.route('/api/profiles', methods=['POST'])
@login_required
def api_create_profile():
    d = request.json
    name = (d.get('name') or '').strip()
    api_key = (d.get('api_key') or '').strip()
    api_secret = (d.get('api_secret') or '').strip()
    broker = (d.get('broker') or 'demo').strip()
    if not name or not api_key or not api_secret:
        return jsonify(error="Name, API key, and secret are required"), 400
    create_profile(current_user_id(), name, api_key, api_secret, broker)
    return jsonify(status="created")


@app.route('/api/profiles/<int:pid>', methods=['PUT'])
@login_required
def api_update_profile(pid):
    d = request.json
    update_profile(pid, current_user_id(), d.get('name',''), d.get('api_key',''), d.get('api_secret',''), d.get('broker', 'demo'))
    return jsonify(status="updated")


@app.route('/api/profiles/<int:pid>', methods=['DELETE'])
@login_required
def api_delete_profile(pid):
    delete_profile(pid, current_user_id())
    return jsonify(status="deleted")


@app.route('/api/test-connection')
@login_required
def api_test_connection():
    """Test if an API profile can connect to Delta Exchange."""
    from config import set_thread_credentials
    api_key, api_secret, _, broker = get_profile_creds(request.args.get('profile_id'))
    if not api_key:
        return jsonify(success=False, error="No keys")
    set_thread_credentials(api_key, api_secret, broker)
    try:
        ok = check_api_connection()
        return jsonify(success=ok)
    except Exception:
        return jsonify(success=False)


# ── Strategy Routes (per-user isolated) ──

@app.route('/api/dashboard')
@login_required
def api_dashboard():
    """Compute dashboard stats from trade history + DB."""
    uid = current_user_id()
    all_history = get_history()
    with _state_lock:
        user_sids = {sid for sid, e in strategies.items() if e.get('user_id') == uid}
        user_sids.update(sid for sid, t in all_tracked.items() if t.get('user_id') == uid)
    user_sids.update(s.sid for s in registry.get_user_strategies(uid))
    trades = [t for t in all_history if t.get('sid') in user_sids or t.get('user_id') == uid]

    # Include DB-tracked strategies not in trade history
    trade_sids = {t.get('sid') for t in trades}
    with _state_lock:
        for sid, t in all_tracked.items():
            if t.get('user_id') != uid or sid in trade_sids:
                continue
            trades.append({
                'sid': sid, 'user_id': uid, 'status': t.get('status', 'running'),
                'started_at': t.get('started_at', ''), 'ended_at': None,
                'pnl': t.get('pnl', 0), 'params': t.get('details', {}),
                'adjustments': 0,
            })

    # Also include completed/closed strategies from DB not yet in trades
    try:
        import json as _json
        conn = get_db()
        db_rows = conn.execute('SELECT * FROM live_strategies WHERE user_id=?', (uid,)).fetchall()
        conn.close()
        for r in db_rows:
            d = dict(r)
            if d['sid'] in trade_sids or d['sid'] in {t.get('sid') for t in trades}:
                continue
            trades.append({
                'sid': d['sid'], 'user_id': uid, 'status': d['status'],
                'started_at': d['started_at'], 'ended_at': None,
                'pnl': d.get('pnl', 0) or 0,
                'params': _json.loads(d.get('details') or '{}'),
                'adjustments': d.get('adjustment_count', 0),
            })
    except Exception:
        pass

    # Inject live PnL for running strategies into trade list
    with _state_lock:
        for t in trades:
            sid = t.get('sid')
            if t.get('status') == 'running':
                # Check active monitors (Option Chain / Strategy Builder)
                if sid in active_monitors and active_monitors[sid].get('user_id') == uid:
                    mon = active_monitors[sid]['monitor']
                    t['pnl'] = round(mon.current_pnl, 2)
                    if not mon.running:
                        t['status'] = 'completed'
                # Check old strategies dict (Delta Neutral)
                elif sid in strategies and strategies[sid].get('strategy'):
                    t['pnl'] = round(strategies[sid]['strategy'].total_pnl, 2)
                # Check IV Crush
                elif sid in iv_crush_strategies and iv_crush_strategies[sid].get('strategy'):
                    t['pnl'] = round(iv_crush_strategies[sid]['strategy'].total_pnl, 2)
                # Check Call Ratio
                elif sid in call_ratio_strategies and call_ratio_strategies[sid].get('strategy'):
                    t['pnl'] = round(call_ratio_strategies[sid]['strategy'].total_pnl, 2)
                # Check OI Strategy
                elif sid in oi_strategies and oi_strategies[sid].get('strategy'):
                    s = oi_strategies[sid]['strategy']
                    t['pnl'] = round(s.pnl, 2)
                    t['cumulative_pnl'] = round(s.cumulative_pnl, 2)
                # Check Weekly DN
                elif sid in weekly_dn_strategies and weekly_dn_strategies[sid].get('strategy'):
                    s = weekly_dn_strategies[sid]['strategy']
                    t['pnl'] = round(s.pnl, 2)
                    t['cumulative_pnl'] = round(s.cumulative_pnl, 2)
                # Check EMA Spread
                elif sid in ema_spread_strategies and ema_spread_strategies[sid].get('strategy'):
                    s = ema_spread_strategies[sid]['strategy']
                    t['pnl'] = round(s.pnl, 4)
                    t['cumulative_pnl'] = round(s.cumulative_pnl, 4)
                # Check Daily Strangle
                elif sid in strangle_strategies and strangle_strategies[sid].get('strategy'):
                    s = strangle_strategies[sid]['strategy']
                    t['pnl'] = round(s.pnl, 2)
                    t['cumulative_pnl'] = round(s.cumulative_pnl, 2)
                # Check Pivot SuperTrend
                elif sid in pivot_st_strategies and pivot_st_strategies[sid].get('strategy'):
                    s = pivot_st_strategies[sid]['strategy']
                    t['pnl'] = round(s.pnl, 4)
                    t['cumulative_pnl'] = round(s.cumulative_pnl, 4)
                # Check Portfolio Strangle
                elif sid in portfolio_strangle_strategies and portfolio_strangle_strategies[sid].get('strategy'):
                    s = portfolio_strangle_strategies[sid]['strategy']
                    t['pnl'] = round(s.pnl, 4)
                    t['cumulative_pnl'] = round(s.cumulative_pnl, 4)
                # Check Hybrid Switch
                elif sid in hybrid_strategies and hybrid_strategies[sid].get('strategy'):
                    s = hybrid_strategies[sid]['strategy']
                    t['pnl'] = round(s.pnl, 2)
                    t['cumulative_pnl'] = round(s.cumulative_pnl, 2)
                # Check unified tracker
                rs = registry.get(sid)
                if rs and rs.running:
                    t['pnl'] = rs.current_pnl

        completed = [t for t in trades if t.get('status') == 'completed']
        running_count = sum(1 for sid, e in strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in active_monitors.items() if e.get('user_id') == uid and e['monitor'].running)
        running_count += sum(1 for sid, e in iv_crush_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in call_ratio_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in oi_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in weekly_dn_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in ema_spread_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in strangle_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in pivot_st_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in portfolio_strangle_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in hybrid_strategies.items() if e.get('user_id') == uid and e.get('running'))
        running_count += sum(1 for sid, e in _futures_traders.items() if e.get('user_id') == uid and e['trader'].running)
    running_count += len(registry.get_running(uid))
    pnls = [t.get('pnl', 0) for t in completed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)

    # P&L over time for chart — sorted by end date, deduplicated per day
    completed_sorted = sorted(completed, key=lambda t: t.get('ended_at') or t.get('started_at', ''))
    pnl_by_date = {}
    cumulative = 0
    for t in completed_sorted:
        cumulative += t.get('pnl', 0)
        date_key = (t.get('ended_at') or t.get('started_at', ''))[:10]
        pnl_by_date[date_key] = round(cumulative, 2)
    pnl_series = [{'date': d, 'pnl': p} for d, p in pnl_by_date.items()]

    # Asset allocation from params
    asset_counts = {}
    for t in trades:
        asset = t.get('params', {}).get('asset', 'BTC')
        asset_counts[asset] = asset_counts.get(asset, 0) + 1

    return jsonify(
        total_pnl=round(total_pnl, 2),
        open_positions=running_count,
        total_trades=len(completed),
        win_rate=round(len(wins)/len(completed)*100, 2) if completed else 0,
        avg_gain=round(sum(wins)/len(wins), 2) if wins else 0,
        avg_loss=round(sum(losses)/len(losses), 2) if losses else 0,
        big_win=round(max(wins), 2) if wins else 0,
        big_loss=round(min(losses), 2) if losses else 0,
        max_drawdown=round(min(pnls), 2) if pnls else 0,
        profitable_trades=len(wins),
        losing_trades=len(losses),
        pnl_series=pnl_series,
        asset_allocation=asset_counts,
        recent_trades=trades[-20:][::-1],
    )


PEER_PORT = os.environ.get('ALGOX_PEER_PORT', '')  # Set by deploy script to old instance port


def _fetch_peer_strategies(uid, token):
    """Fetch running strategies from the peer (old) instance."""
    if not PEER_PORT:
        return []
    try:
        import requests as req
        r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/strategies',
                     headers={'Authorization': f'Bearer {token}'}, timeout=3)
        if r.ok:
            return r.json().get('strategies', [])
    except Exception:
        pass
    return []


@app.route('/api/strategies')
@login_required
def api_all_strategies():
    """Return all tracked strategies for the current user with live PnL."""
    uid = current_user_id()
    from api.live_pnl import compute_live_legs
    result = []
    with _state_lock:
        for sid, t in list(all_tracked.items()):
            if t['user_id'] != uid:
                continue
            entry = dict(t)
            if entry['status'] in ('completed', 'closed'):
                continue  # Don't show finished strategies on main page
            if entry['status'] in ('running', 'open (no monitor)'):
                if sid in strategies and strategies[sid].get('strategy'):
                    s = strategies[sid]['strategy']
                    entry['pnl'] = round(getattr(s, 'total_pnl', 0), 2)
                elif sid in active_monitors:
                    mon = active_monitors[sid]['monitor']
                    entry['pnl'] = round(mon.current_pnl, 2)
                    if not mon.running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(mon.current_pnl, 2))
                        continue  # skip — just completed
                elif sid in iv_crush_strategies and iv_crush_strategies[sid].get('strategy'):
                    s = iv_crush_strategies[sid]['strategy']
                    entry['pnl'] = round(s.total_pnl, 2)
                    if not s.running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.total_pnl, 2))
                        continue
                elif sid in call_ratio_strategies and call_ratio_strategies[sid].get('strategy'):
                    s = call_ratio_strategies[sid]['strategy']
                    entry['pnl'] = round(s.total_pnl, 2)
                    if not s.running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.total_pnl, 2))
                        continue
                elif sid in oi_strategies and oi_strategies[sid].get('strategy'):
                    s = oi_strategies[sid]['strategy']
                    entry['pnl'] = round(s.pnl, 2)
                    if not s._running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.pnl, 2))
                        continue
                elif sid in weekly_dn_strategies and weekly_dn_strategies[sid].get('strategy'):
                    s = weekly_dn_strategies[sid]['strategy']
                    entry['pnl'] = round(s.pnl, 2)
                    if not s._running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.pnl, 2))
                        continue
                elif sid in ema_spread_strategies and ema_spread_strategies[sid].get('strategy'):
                    s = ema_spread_strategies[sid]['strategy']
                    entry['pnl'] = round(s.pnl, 4)
                    if not s._running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.pnl, 4))
                        continue
                elif sid in strangle_strategies and strangle_strategies[sid].get('strategy'):
                    s = strangle_strategies[sid]['strategy']
                    entry['pnl'] = round(s.pnl, 2)
                    if not s._running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.pnl, 2))
                        continue
                    # Include live legs for dashboard display
                    with s._legs_lock:
                        entry['legs'] = [
                            {'symbol': l.get('symbol', ''), 'strike': l.get('strike', ''),
                             'type': l.get('type', ''), 'side': l.get('side', ''),
                             'size': l.get('size', 0), 'entry_price': round(l.get('entry_price', 0), 4),
                             'current_mark': round(l.get('current_mark', l.get('entry_price', 0)), 4),
                             'current_pnl': round(l.get('current_pnl', 0), 4),
                             'stopped': l.get('stopped', False),
                             'product_id': l.get('product_id')}
                            for l in s.legs
                        ]
                elif sid in pivot_st_strategies and pivot_st_strategies[sid].get('strategy'):
                    s = pivot_st_strategies[sid]['strategy']
                    entry['pnl'] = round(s.pnl, 4)
                    if not s._running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.pnl, 4))
                        continue
                    with s._legs_lock:
                        entry['legs'] = [
                            {'symbol': l.get('symbol', ''), 'strike': l.get('strike', ''),
                             'type': l.get('type', ''), 'side': l.get('side', ''),
                             'size': l.get('size', 0), 'entry_price': round(l.get('entry_price', 0), 4),
                             'signal': l.get('signal', ''),
                             'product_id': l.get('product_id')}
                            for l in s.legs
                        ]
                elif sid in portfolio_strangle_strategies and portfolio_strangle_strategies[sid].get('strategy'):
                    s = portfolio_strangle_strategies[sid]['strategy']
                    entry['pnl'] = round(s.pnl, 4)
                    if not s._running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.pnl, 4))
                        continue
                    # Include live legs for dashboard display
                    with s._legs_lock:
                        entry['legs'] = [
                            {'symbol': l.get('symbol', ''), 'strike': l.get('strike', ''),
                             'type': l.get('type', ''), 'side': l.get('side', ''),
                             'size': l.get('size', 0), 'entry_price': round(l.get('entry_price', 0), 4),
                             'current_mark': round(l.get('current_mark', l.get('entry_price', 0)), 4),
                             'current_pnl': round(l.get('current_pnl', 0), 4),
                             'stopped': l.get('stopped', False),
                             'product_id': l.get('product_id')}
                            for l in s.legs
                        ]
                elif sid in hybrid_strategies and hybrid_strategies[sid].get('strategy'):
                    s = hybrid_strategies[sid]['strategy']
                    entry['pnl'] = round(s.pnl, 2)
                    if not s._running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed', pnl=round(s.pnl, 2))
                        continue
                    # Include live legs for dashboard display
                    with s._legs_lock:
                        entry['legs'] = [
                            {'symbol': l.get('symbol', ''), 'strike': l.get('strike', ''),
                             'type': l.get('type', ''), 'side': l.get('side', ''),
                             'size': l.get('size', 0), 'entry_price': round(l.get('entry_price', 0), 4),
                             'current_mark': round(l.get('current_mark', l.get('entry_price', 0)), 4),
                             'current_pnl': round(l.get('current_pnl', 0), 4),
                             'active': l.get('active', True),
                             'product_id': l.get('product_id')}
                            for l in s.legs
                        ]
                elif sid in _futures_traders:
                    trader = _futures_traders[sid]['trader']
                    if not trader.running:
                        entry['status'] = 'completed'
                        update_tracked(sid, status='completed')
                        continue
                    entry['running'] = True
                    # Compute live P&L
                    from api.pricing import get_futures_price
                    from strategy.futures_signal_trader import FUTURES_PRODUCTS as FP
                    from config import get_contract_value
                    sym = FP.get(trader.asset)
                    pd = get_futures_price(sym) if sym else None
                    mark = pd['mark_price'] if pd else 0
                    cv = get_contract_value(trader.asset)
                    total_pnl = 0
                    legs_out = []
                    for l in trader.legs:
                        lpnl = ((mark - l['entry_price']) if l['side'] == 'buy' else (l['entry_price'] - mark)) * l['size'] * cv if mark else 0
                        total_pnl += lpnl
                        legs_out.append({'symbol': l['symbol'], 'side': l['side'], 'size': l['size'], 'entry_price': l['entry_price'], 'current_mark': round(mark, 2), 'current_pnl': round(lpnl, 2)})
                    entry['pnl'] = round(total_pnl, 2)
                    entry['legs'] = legs_out
                else:
                    # No monitor — compute live P&L from legs
                    raw_legs = entry.get('details', {}).get('legs', [])
                    asset = entry.get('details', {}).get('asset', 'BTC')
                    if raw_legs:
                        _, pnl = compute_live_legs(raw_legs, asset)
                        entry['pnl'] = pnl
            result.append(entry)

    # Merge strategies from unified tracker (only running)
    for ts in registry.get_user_strategies(uid):
        if ts.sid not in {s['sid'] for s in result}:
            if not ts.running and ts.status in ('completed', 'closed'):
                continue
            st = ts.get_status()
            st.pop('logs', None)
            result.append(st)

    # Merge running strategies from peer (old) instance
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    peer = _fetch_peer_strategies(uid, token)
    local_sids = {s['sid'] for s in result}
    for ps in peer:
        if ps['sid'] not in local_sids:
            ps['_peer'] = True
            result.append(ps)

    return jsonify(strategies=result)


@app.route('/api/strategies/<sid>/close', methods=['POST'])
@login_required
def api_close_strategy(sid):
    """Close a single strategy by sid."""
    uid = current_user_id()

    # If not local, proxy to peer
    with _state_lock:
        found = sid in all_tracked
    if not found and PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.post(f'http://127.0.0.1:{PEER_PORT}/api/strategies/{sid}/close',
                         headers={'Authorization': f'Bearer {token}'}, timeout=10)
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify(error="Not found"), 404

    with _state_lock:
        if sid not in all_tracked or all_tracked[sid]['user_id'] != uid:
            return jsonify(error="Not found"), 404

    from config import set_thread_credentials
    from api.orders import place_order

    # Resolve profile_id from whichever source has it
    with _state_lock:
        profile_id = None
        if sid in strategies:
            profile_id = strategies[sid].get('profile_id')
        elif sid in active_monitors:
            profile_id = active_monitors[sid].get('profile_id')
        elif sid in iv_crush_strategies:
            profile_id = iv_crush_strategies[sid].get('profile_id')
        elif sid in call_ratio_strategies:
            profile_id = call_ratio_strategies[sid].get('profile_id')
        elif sid in oi_strategies:
            profile_id = oi_strategies[sid].get('profile_id')
        elif sid in weekly_dn_strategies:
            profile_id = weekly_dn_strategies[sid].get('profile_id')
        elif sid in ema_spread_strategies:
            profile_id = ema_spread_strategies[sid].get('profile_id')
        elif sid in strangle_strategies:
            profile_id = strangle_strategies[sid].get('profile_id')
        elif sid in pivot_st_strategies:
            profile_id = pivot_st_strategies[sid].get('profile_id')
        elif sid in portfolio_strangle_strategies:
            profile_id = portfolio_strangle_strategies[sid].get('profile_id')
        elif sid in hybrid_strategies:
            profile_id = hybrid_strategies[sid].get('profile_id')
        if not profile_id:
            rs = registry.get(sid)
            if rs:
                profile_id = rs.profile_id
        if not profile_id and all_tracked.get(sid, {}).get('details', {}).get('profile_id'):
            profile_id = all_tracked[sid]['details']['profile_id']

    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    logger.info(f"[close] sid={sid} profile_id={profile_id} broker={broker}")
    if api_key:
        set_thread_credentials(api_key, api_secret, broker)

    closed = False
    # Delta Neutral strategy
    if sid in strategies:
        e = strategies[sid]
        if e.get('strategy'):
            e['strategy'].running = False
            e['strategy'].close_all_positions()
            closed = True
    # IV Crush strategy
    if not closed and sid in iv_crush_strategies:
        ic = iv_crush_strategies[sid]
        if ic.get('strategy'):
            ic['strategy'].running = False
            ic['strategy'].close_all()
            closed = True
    # Call Ratio strategy
    if not closed and sid in call_ratio_strategies:
        cr = call_ratio_strategies[sid]
        if cr.get('strategy'):
            cr['strategy'].running = False
            cr['strategy'].close_all()
            closed = True
    # OI Strategy
    if not closed and sid in oi_strategies:
        oi = oi_strategies[sid]
        if oi.get('strategy'):
            oi['strategy'].close_all()
        closed = True
    # Weekly DN
    if not closed and sid in weekly_dn_strategies:
        wdn = weekly_dn_strategies[sid]
        if wdn.get('strategy'):
            wdn['strategy'].close_all()
        closed = True
    # EMA Spread
    if not closed and sid in ema_spread_strategies:
        ecs = ema_spread_strategies[sid]
        if ecs.get('strategy'):
            try:
                ecs['strategy'].close_all()
            except Exception as e:
                logger.error(f"[close] EMA Spread {sid} close_all error: {e}")
        closed = True
    # Daily Strangle
    if not closed and sid in strangle_strategies:
        st = strangle_strategies[sid]
        if st.get('strategy'):
            try:
                st['strategy'].close_all()
            except Exception as e:
                logger.error(f"[close] Strangle {sid} close_all error: {e}")
        closed = True
    # Pivot SuperTrend
    if not closed and sid in pivot_st_strategies:
        pst = pivot_st_strategies[sid]
        if pst.get('strategy'):
            try:
                pst['strategy'].close_all()
            except Exception as e:
                logger.error(f"[close] PivotST {sid} close_all error: {e}")
        closed = True
    # Portfolio Strangle
    if not closed and sid in portfolio_strangle_strategies:
        ps = portfolio_strangle_strategies[sid]
        if ps.get('strategy'):
            try:
                ps['strategy'].close_all()
            except Exception as e:
                logger.error(f"[close] Portfolio Strangle {sid} close_all error: {e}")
        closed = True
    # Hybrid Switch
    if not closed and sid in hybrid_strategies:
        hs = hybrid_strategies[sid]
        if hs.get('strategy'):
            try:
                hs['strategy'].close_all()
            except Exception as e:
                logger.error(f"[close] Hybrid {sid} close_all error: {e}")
        closed = True
    # Option Chain monitor
    if not closed and sid in active_monitors:
        active_monitors[sid]['monitor'].stop()
        closed = True
    # TrackedStrategy (registry)
    if not closed:
        rs = registry.get(sid)
        if rs and rs.user_id == uid:
            rs.close()
            closed = True
    # Fallback — close positions by reversing each leg
    if not closed and api_key:
        details = all_tracked[sid].get('details', {})
        placed_legs = details.get('legs', [])
        if isinstance(placed_legs, list) and placed_legs:
            failed = []
            for leg in placed_legs:
                try:
                    close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                    result = place_order(leg['product_id'], leg['symbol'], int(leg['size']), close_side)
                    if result is None:
                        failed.append(leg.get('symbol', 'unknown'))
                except Exception as e:
                    failed.append(f"{leg.get('symbol')}: {e}")
            if failed:
                return jsonify(success=False, error=f"Failed to close: {', '.join(failed)}"), 500
            closed = True
    # Futures Signal trader
    if not closed and sid in _futures_traders:
        _futures_traders[sid]['trader'].stop()
        closed = True

    if not closed:
        return jsonify(success=False, error="Failed to close strategy"), 500

    update_tracked(sid, status='closed', exit_reason='user_closed')
    # Clean up position tracker
    try:
        tracked_details = all_tracked.get(sid, {}).get('details', {})
        if isinstance(tracked_details, str):
            import json as _j
            tracked_details = _j.loads(tracked_details)
        tracked_legs = tracked_details.get('legs', []) if isinstance(tracked_details, dict) else []
        if isinstance(tracked_legs, str):
            import json as _j
            tracked_legs = _j.loads(tracked_legs)
        if isinstance(tracked_legs, list):
            for leg in tracked_legs:
                if isinstance(leg, dict) and leg.get('product_id'):
                    position_tracker.close(uid, leg['product_id'])
    except Exception as e:
        logger.warning(f"[close] position_tracker cleanup error for {sid}: {e}")
    return jsonify(success=True, status='closed')


@app.route('/api/strategies/close-all', methods=['POST'])
@login_required
def api_close_all_strategies():
    """Close all running strategies for the current user."""
    uid = current_user_id()
    from config import set_thread_credentials
    closed_count = 0

    with _state_lock:
        items_to_close = [(sid, dict(t)) for sid, t in all_tracked.items()
                          if t['user_id'] == uid and t['status'] in ('running', 'open (no monitor)')]

    for sid, t in items_to_close:

        # Resolve profile_id from all sources
        with _state_lock:
            profile_id = None
            if sid in strategies:
                profile_id = strategies[sid].get('profile_id')
            elif sid in active_monitors:
                profile_id = active_monitors[sid].get('profile_id')
            elif sid in iv_crush_strategies:
                profile_id = iv_crush_strategies[sid].get('profile_id')
            elif sid in call_ratio_strategies:
                profile_id = call_ratio_strategies[sid].get('profile_id')
            elif sid in oi_strategies:
                profile_id = oi_strategies[sid].get('profile_id')
            elif sid in weekly_dn_strategies:
                profile_id = weekly_dn_strategies[sid].get('profile_id')
            elif sid in ema_spread_strategies:
                profile_id = ema_spread_strategies[sid].get('profile_id')
            elif sid in ema_spread_strategies:
                profile_id = ema_spread_strategies[sid].get('profile_id')
            elif sid in strangle_strategies:
                profile_id = strangle_strategies[sid].get('profile_id')
            elif sid in pivot_st_strategies:
                profile_id = pivot_st_strategies[sid].get('profile_id')
            elif sid in portfolio_strangle_strategies:
                profile_id = portfolio_strangle_strategies[sid].get('profile_id')
            elif sid in hybrid_strategies:
                profile_id = hybrid_strategies[sid].get('profile_id')
            if not profile_id:
                rs = registry.get(sid)
                if rs:
                    profile_id = rs.profile_id
            if not profile_id and t.get('details', {}).get('profile_id'):
                profile_id = t['details']['profile_id']

        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)

        closed = False
        if sid in strategies and strategies[sid].get('strategy'):
            strategies[sid]['strategy'].running = False
            strategies[sid]['strategy'].close_all_positions()
            closed = True
        if not closed and sid in iv_crush_strategies and iv_crush_strategies[sid].get('strategy'):
            iv_crush_strategies[sid]['strategy'].running = False
            iv_crush_strategies[sid]['strategy'].close_all()
            closed = True
        if not closed and sid in call_ratio_strategies and call_ratio_strategies[sid].get('strategy'):
            call_ratio_strategies[sid]['strategy'].running = False
            call_ratio_strategies[sid]['strategy'].close_all()
            closed = True
        if not closed and sid in oi_strategies:
            oi = oi_strategies[sid]
            if oi.get('strategy'):
                oi['strategy'].close_all()
            closed = True
        if not closed and sid in weekly_dn_strategies:
            wdn = weekly_dn_strategies[sid]
            if wdn.get('strategy'):
                wdn['strategy'].close_all()
            closed = True
        if not closed and sid in ema_spread_strategies:
            ecs = ema_spread_strategies[sid]
            if ecs.get('strategy'):
                ecs['strategy'].close_all()
            closed = True
        if not closed and sid in strangle_strategies:
            st = strangle_strategies[sid]
            if st.get('strategy'):
                st['strategy'].close_all()
            closed = True
        if not closed and sid in pivot_st_strategies:
            pst = pivot_st_strategies[sid]
            if pst.get('strategy'):
                pst['strategy'].close_all()
            closed = True
        if not closed and sid in portfolio_strangle_strategies:
            ps = portfolio_strangle_strategies[sid]
            if ps.get('strategy'):
                ps['strategy'].close_all()
            closed = True
        if not closed and sid in hybrid_strategies:
            hs = hybrid_strategies[sid]
            if hs.get('strategy'):
                hs['strategy'].close_all()
            closed = True
        if not closed and sid in active_monitors:
            active_monitors[sid]['monitor'].stop()
            closed = True
        if not closed:
            rs = registry.get(sid)
            if rs:
                rs.close()
                closed = True
        # Fallback — close by reversing legs
        if not closed and api_key:
            from api.orders import place_order
            placed_legs = t.get('details', {}).get('legs', [])
            if isinstance(placed_legs, list):
                for leg in placed_legs:
                    close_side = 'buy' if leg['side'] == 'sell' else 'sell'
                    place_order(leg['product_id'], leg['symbol'], int(leg['size']), close_side)

        update_tracked(sid, status='closed', exit_reason='user_closed')
        closed_count += 1

    return jsonify(closed=closed_count)


@app.route('/api/strategy-detail/<sid>')
@login_required
def api_strategy_detail(sid):
    uid = current_user_id()
    from api.live_pnl import compute_live_legs

    # Check in-memory tracked strategies
    with _state_lock:
        t = all_tracked.get(sid)
        if t:
            t = dict(t)
    if t and t['user_id'] == uid:
        entry = dict(t)
        asset = entry.get('details', {}).get('asset', 'BTC')
        live_legs = []
        logs = []
        pnl_history = []

        if sid in strategies and strategies[sid].get('strategy'):
            strat = strategies[sid]['strategy']
            entry['pnl'] = round(strat.total_pnl, 2)
            entry['realized_pnl'] = round(getattr(strat, 'realized_pnl', 0), 2)
            entry['unrealized_pnl'] = round(getattr(strat, 'unrealized_pnl', 0), 2)
            entry['adjustment_count'] = getattr(strat, 'adjustment_count', 0)
            entry['adjustment_history'] = getattr(strat, 'adjustment_history', [])
            entry['running'] = strategies[sid].get('running', False)
            logs = strategies[sid].get('log_history', [])
            for leg_name in ['call', 'put']:
                info = _leg_info(strat, leg_name)
                if info:
                    live_legs.append({
                        'symbol': info['symbol'], 'type': leg_name, 'strike': info['strike'],
                        'side': 'sell', 'size': info['size'], 'product_id': None,
                        'entry_price': info['entry'], 'current_mark': info['mark'],
                        'current_pnl': info['payoff'], 'delta': info['delta'],
                    })
        elif sid in iv_crush_strategies and iv_crush_strategies[sid].get('strategy'):
            ic = iv_crush_strategies[sid]
            strat = ic['strategy']
            entry['pnl'] = round(strat.total_pnl, 2)
            entry['realized_pnl'] = round(getattr(strat, 'realized_pnl', 0), 2)
            entry['unrealized_pnl'] = round(getattr(strat, 'unrealized_pnl', 0), 2)
            entry['running'] = ic.get('running', False)
            logs = ic.get('log_history', [])
            live_legs = _iv_crush_legs(strat)
            # Enrich with live marks
            for leg in live_legs:
                ws = strat.ws_manager.get_latest_price(leg['symbol'])
                mark = ws['mark_price'] if ws else leg['entry_price']
                d = -1  # short
                leg['current_mark'] = round(mark, 2)
                leg['current_pnl'] = round(d * (mark - leg['entry_price']) * leg['size'] * leg.get('contract_value', 0.001), 2)
        elif sid in call_ratio_strategies and call_ratio_strategies[sid].get('strategy'):
            cr = call_ratio_strategies[sid]
            strat = cr['strategy']
            entry['pnl'] = round(strat.total_pnl, 2)
            entry['running'] = cr.get('running', False)
            logs = cr.get('log_history', [])
            for leg in strat.legs:
                live_legs.append({
                    'product_id': leg.get('product_id'), 'symbol': leg['symbol'],
                    'type': 'call', 'strike': leg.get('strike', ''),
                    'side': leg['side'], 'size': leg['size'],
                    'entry_price': round(leg['entry_price'], 2),
                    'current_mark': round(leg.get('current_mark', leg['entry_price']), 2),
                    'current_pnl': leg.get('current_pnl', 0),
                })
        elif sid in oi_strategies and oi_strategies[sid].get('strategy'):
            oi = oi_strategies[sid]
            strat = oi['strategy']
            entry['pnl'] = round(strat.pnl, 2)
            entry['running'] = oi.get('running', False)
            logs = oi.get('log_history', [])
            for leg in strat.legs:
                live_legs.append({'symbol': leg['symbol'], 'type': leg['type'], 'strike': leg['strike'],
                    'side': 'sell', 'size': leg['size'], 'entry_price': round(leg['entry_price'], 4),
                    'current_mark': 0, 'current_pnl': 0})
        elif sid in weekly_dn_strategies and weekly_dn_strategies[sid].get('strategy'):
            wdn = weekly_dn_strategies[sid]
            strat = wdn['strategy']
            entry['pnl'] = round(strat.pnl, 2)
            entry['running'] = wdn.get('running', False)
            logs = wdn.get('log_history', [])
        elif sid in ema_spread_strategies and ema_spread_strategies[sid].get('strategy'):
            ecs = ema_spread_strategies[sid]
            strat = ecs['strategy']
            entry['pnl'] = round(strat.pnl, 4)
            entry['running'] = ecs.get('running', False)
            logs = ecs.get('log_history', [])
            entry['trade_log'] = strat.trade_log[-20:]
            entry['days_traded'] = strat.total_days_traded
            entry['cumulative_pnl'] = round(strat.cumulative_pnl, 4)
            for leg in strat.legs:
                live_legs.append({'symbol': leg['symbol'], 'type': leg['type'], 'strike': leg['strike'],
                    'side': leg['side'], 'size': leg['size'], 'entry_price': round(leg['entry_price'], 4),
                    'current_mark': round(leg.get('current_mark', 0), 4),
                    'current_pnl': round(leg.get('current_pnl', 0), 4),
                    'delta': leg.get('delta', 0)})
            # Use in-memory pnl_history for the chart
            if hasattr(strat, '_pnl_history') and strat._pnl_history:
                pnl_history = list(strat._pnl_history[-500:])
        elif sid in strangle_strategies and strangle_strategies[sid].get('strategy'):
            st = strangle_strategies[sid]
            strat = st['strategy']
            entry['pnl'] = round(strat.pnl, 2)
            entry['cumulative_pnl'] = round(strat.cumulative_pnl, 2)
            entry['running'] = st.get('running', False)
            logs = st.get('log_history', [])
            entry['trade_log'] = strat.trade_log[-20:]
            entry['days_traded'] = strat.total_days_traded
            for leg in strat.legs:
                live_legs.append({'symbol': leg['symbol'], 'type': leg['type'], 'strike': leg['strike'],
                    'side': 'sell', 'size': leg['size'], 'entry_price': round(leg['entry_price'], 2),
                    'current_mark': round(leg.get('current_mark', 0), 2),
                    'current_pnl': round(leg.get('current_pnl', 0), 2),
                    'stopped': leg.get('stopped', False)})
            if hasattr(strat, '_pnl_history') and strat._pnl_history:
                pnl_history = list(strat._pnl_history[-500:])
        elif sid in pivot_st_strategies and pivot_st_strategies[sid].get('strategy'):
            pst = pivot_st_strategies[sid]
            strat = pst['strategy']
            entry['pnl'] = round(strat.pnl, 4)
            entry['cumulative_pnl'] = round(strat.cumulative_pnl, 4)
            entry['running'] = pst.get('running', False)
            logs = pst.get('log_history', [])
            entry['trade_log'] = strat.trade_log[-20:]
            entry['days_traded'] = strat.total_days_traded
            entry['today_trades'] = strat.today_trade_count
            entry['pivot'] = round(strat._pivot, 0) if strat._pivot else None
            entry['r1'] = round(strat._r1, 0) if strat._r1 else None
            entry['s1'] = round(strat._s1, 0) if strat._s1 else None
            entry['st_direction'] = 'bullish' if strat._st_direction == 1 else 'bearish' if strat._st_direction == -1 else None
            for leg in strat.legs:
                live_legs.append({'symbol': leg.get('symbol', ''), 'type': leg.get('type', ''),
                    'strike': leg.get('strike', ''), 'side': 'sell', 'size': leg.get('size', 0),
                    'entry_price': round(leg.get('entry_price', 0), 4),
                    'signal': leg.get('signal', '')})
            if hasattr(strat, '_pnl_history') and strat._pnl_history:
                pnl_history = list(strat._pnl_history[-500:])
        elif sid in portfolio_strangle_strategies and portfolio_strangle_strategies[sid].get('strategy'):
            ps = portfolio_strangle_strategies[sid]
            strat = ps['strategy']
            entry['pnl'] = round(strat.pnl, 4)
            entry['cumulative_pnl'] = round(strat.cumulative_pnl, 4)
            entry['running'] = ps.get('running', False)
            logs = ps.get('log_history', [])
            entry['trade_log'] = strat.trade_log[-20:]
            entry['days_traded'] = strat.total_days_traded
            for leg in strat.legs:
                live_legs.append({'symbol': leg['symbol'], 'type': leg['type'], 'strike': leg['strike'],
                    'side': leg['side'], 'size': leg['size'], 'entry_price': round(leg['entry_price'], 4),
                    'current_mark': round(leg.get('current_mark', 0), 4),
                    'current_pnl': round(leg.get('current_pnl', 0), 4),
                    'stopped': leg.get('stopped', False)})
            if hasattr(strat, '_pnl_history') and strat._pnl_history:
                pnl_history = list(strat._pnl_history[-500:])
        elif sid in hybrid_strategies and hybrid_strategies[sid].get('strategy'):
            hs = hybrid_strategies[sid]
            strat = hs['strategy']
            entry['pnl'] = round(strat.pnl, 2)
            entry['cumulative_pnl'] = round(strat.cumulative_pnl, 2)
            entry['running'] = hs.get('running', False)
            logs = hs.get('log_history', [])
            entry['trade_log'] = strat.trade_log[-20:]
            entry['days_traded'] = strat.total_days_traded
            for leg in strat.legs:
                live_legs.append({'symbol': leg['symbol'], 'type': leg['type'], 'strike': leg['strike'],
                    'side': leg['side'], 'size': leg['size'], 'entry_price': round(leg['entry_price'], 2),
                    'current_mark': round(leg.get('current_mark', 0), 2),
                    'current_pnl': round(leg.get('current_pnl', 0), 2),
                    'role': leg.get('role', ''), 'active': leg.get('active', False)})
            if hasattr(strat, '_pnl_history') and strat._pnl_history:
                pnl_history = list(strat._pnl_history[-500:])
        elif sid in _futures_traders:
            trader = _futures_traders[sid]['trader']
            entry['running'] = trader.running
            logs = [f"[{t['time']}] {t['side'].upper()} @ {t['price']} | SL: {t['sl']} | TP: {t['tp']} | {'✓ Filled' if t['success'] else '✗ Failed'}" for t in trader.trade_log]
            logs = [f"[FST] {trader.signal_key} {trader.asset} {trader.timeframe} | Scans: {trader._scan_count} | Trades: {trader.trades_today}/{trader.max_trades_per_day}"] + logs
            total_pnl = 0
            from api.pricing import get_futures_price
            from strategy.futures_signal_trader import FUTURES_PRODUCTS as FP
            from config import get_contract_value
            symbol = FP.get(trader.asset)
            price_data = get_futures_price(symbol) if symbol else None
            mark = price_data['mark_price'] if price_data else 0
            cv = get_contract_value(trader.asset)
            for leg in trader.legs:
                leg_pnl = 0
                if mark > 0:
                    if leg['side'] == 'buy':
                        leg_pnl = (mark - leg['entry_price']) * leg['size'] * cv
                    else:
                        leg_pnl = (leg['entry_price'] - mark) * leg['size'] * cv
                total_pnl += leg_pnl
                live_legs.append({
                    'symbol': leg['symbol'],
                    'side': leg['side'],
                    'size': leg['size'],
                    'entry_price': leg['entry_price'],
                    'current_mark': round(mark, 2),
                    'current_pnl': round(leg_pnl, 2),
                    'sl': leg.get('sl'),
                    'tp': leg.get('tp'),
                    'type': 'futures',
                })
            entry['pnl'] = round(total_pnl, 2)
        else:
            # Option Chain / Strategy Builder / Tracker — use common P&L calculator
            raw_legs = []
            if sid in active_monitors:
                mon = active_monitors[sid]['monitor']
                raw_legs = mon.legs
                logs = mon.get_status().get('logs', [])
                entry['running'] = mon.running
                if not mon.running:
                    entry['status'] = 'completed'
            rs = registry.get(sid)
            if rs:
                raw_legs = rs.legs
                logs = rs.get_logs(200)
                entry['running'] = rs.running
            if not raw_legs:
                raw_legs = entry.get('details', {}).get('legs', [])

            if raw_legs:
                live_legs, total_pnl = compute_live_legs(raw_legs, asset)
                entry['pnl'] = total_pnl

        entry['legs'] = live_legs
        entry['logs'] = logs
        # Include pnl_history from in-memory or DB snapshots
        if not pnl_history:
            if sid in active_monitors:
                pnl_history = active_monitors[sid]['monitor'].pnl_history[-500:]
            rs = registry.get(sid)
            if rs and rs._pnl_history:
                pnl_history = rs._pnl_history[-500:]
        if not pnl_history:
            pnl_history = [(s['ts'], s['pnl']) for s in get_pnl_snapshots(uid, sid=sid)]
        entry['pnl_history'] = pnl_history
        return jsonify(**entry)
    # Try peer instance
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/strategy-detail/{sid}',
                        headers={'Authorization': f'Bearer {token}'}, timeout=3)
            if r.ok:
                return jsonify(r.json())
        except Exception:
            pass
    # Fallback: trade_history.json
    for h in get_history():
        if h.get('sid') == sid:
            return jsonify(
                sid=sid, source='Trade History', name=h.get('params', {}).get('asset', 'BTC') + ' ' + h.get('params', {}).get('expiry_date', ''),
                status=h.get('status', 'completed'), pnl=h.get('pnl', 0),
                started_at=h.get('started_at', ''), details=h.get('params', {}),
                adjustments=h.get('adjustments', 0), ended_at=h.get('ended_at', ''),
                user_id=uid,
            )
    # Fallback: DB live_strategies
    db = get_db()
    row = db.execute('SELECT * FROM live_strategies WHERE sid=? AND user_id=?', (sid, uid)).fetchone()
    db.close()
    if row:
        import json as _j
        d = dict(row)
        d['details'] = _j.loads(d.get('details') or '{}')
        d['legs'] = _j.loads(d.get('legs') or '[]')
        d['logs'] = _j.loads(d.get('logs') or '[]')
        d['running'] = d.get('status') == 'running'
        d['pnl'] = d.get('pnl', 0)
        return jsonify(**d)
    return jsonify(error='Not found'), 404


def _validate_strategy_params(params):
    """Validate and clamp strategy parameters. Returns (cleaned_params, error_msg)."""
    try:
        p = {
            'asset': str(params.get('asset', 'BTC')),
            'expiry_date': str(params.get('expiry_date', '')),
            'target_delta': max(0.01, min(0.50, float(params.get('target_delta', 0.20)))),
            'delta_tolerance': max(0.01, min(0.20, float(params.get('delta_tolerance', 0.05)))),
            'lot_size': max(1, min(10000, int(params.get('lot_size', 100)))),
            'premium_threshold': max(5, min(200, float(params.get('premium_threshold', 40)))),
            'target_pnl': max(1, min(100000, float(params.get('target_pnl', 25)))),
            'max_adjustments': max(0, min(50, int(params.get('max_adjustments', 5)))),
            'monitoring_interval': max(2, min(300, int(params.get('monitoring_interval', 5)))),
        }
        if not p['expiry_date']:
            return None, "expiry_date is required"
        if p['asset'] not in ('BTC', 'ETH'):
            return None, f"Unsupported asset: {p['asset']}"
        return p, None
    except (ValueError, TypeError) as e:
        return None, f"Invalid parameter: {e}"


@app.route('/start', methods=['POST'])
@app.route('/api/start', methods=['POST'])
@login_required
@credits_required('deploy_live')
def start():
    params = request.json
    profile_id = params.pop('profile_id', None)

    # Validate credentials from profile or default
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected or keys not configured."), 400

    # Validate strategy parameters
    clean_params, err = _validate_strategy_params(params)
    if err:
        return jsonify(error=err), 400

    sid = params.pop('sid', '') or str(uuid.uuid4())[:8]

    with _state_lock:
        if sid in strategies and strategies[sid]['running']:
            return jsonify(error="Strategy already running"), 400

    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [], 'running': False, 'params': clean_params, 'user_id': current_user_id(), 'profile_id': profile_id}
    with _state_lock:
        strategies[sid] = entry
    record_start(sid, clean_params, user_id=current_user_id())
    track_strategy(sid, 'AlgoX DN', f"{clean_params.get('asset','BTC')} {clean_params.get('expiry_date','')}", current_user_id(), details={**clean_params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_strategy, args=(sid, clean_params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/stop', methods=['POST'])
@app.route('/api/stop', methods=['POST'])
@login_required
def stop():
    sid = request.json.get('sid')
    e = strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if not e['running'] or not e.get('strategy'):
        return jsonify(error="No strategy running"), 400
    e['strategy'].running = False
    e['strategy'].close_all_positions()
    return jsonify(status="stopping")


@app.route('/stream/<sid>')
@app.route('/api/stream/<sid>')
@login_required
def stream(sid):
    e = strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']

    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/status/<sid>')
@app.route('/api/status/<sid>')
@login_required
def status(sid):
    e = strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        # Try peer
        if PEER_PORT:
            try:
                import requests as req
                token = request.headers.get('Authorization', '').replace('Bearer ', '')
                r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/status/{sid}',
                            headers={'Authorization': f'Bearer {token}'}, timeout=3)
                if r.ok: return jsonify(r.json())
            except Exception: pass
        return jsonify(running=False)
    if not e['running'] or not e.get('strategy'):
        return jsonify(running=False)
    s = e['strategy']
    return jsonify(
        running=True,
        adjustment_count=s.adjustment_count,
        adjustment_history=s.adjustment_history,
        total_pnl=round(s.total_pnl, 2),
        realized_pnl=round(s.realized_pnl, 2),
        unrealized_pnl=round(s.unrealized_pnl, 2),
        call=_leg_info(s, 'call'),
        put=_leg_info(s, 'put'),
    )


def _leg_info(s, leg):
    pos = getattr(s, f'{leg}_position')
    if not pos:
        return None
    cv = getattr(s, f'{leg}_contract_value')

    # Try WebSocket first, then REST API for live price
    ws_data = s.ws_manager.get_latest_price(pos['symbol'])
    mark = None
    delta = 0
    if ws_data:
        mark = ws_data['mark_price']
        delta = ws_data.get('delta', 0)
    if not mark:
        try:
            from api.pricing import get_current_price
            rest_data = get_current_price(pos['product_id'], getattr(s, 'asset', 'BTC'))
            if rest_data:
                mark = rest_data.get('mark_price', 0)
                delta = rest_data.get('delta', 0)
        except Exception:
            pass
    if not mark:
        mark = getattr(s, f'{leg}_actual_entry_price')

    from api import get_position_entry_price
    real_entry, real_size = get_position_entry_price(pos['product_id'])
    entry = real_entry if real_entry else getattr(s, f'{leg}_actual_entry_price')
    size = abs(real_size) if real_size else s.lot_size
    payoff = (entry - mark) * size * cv

    return dict(
        symbol=pos['symbol'],
        strike=pos.get('strike_price', ''),
        entry=round(entry, 2),
        mark=round(mark, 2),
        delta=round(delta, 4),
        size=size,
        payoff=round(payoff, 2),
    )


def _enrich_leg(leg, asset='BTC', profile_id=None, user_id=None):
    """Add live mark_price and pnl to a strategy leg dict.
    
    If profile_id/user_id are provided, temporarily sets thread-local credentials
    so the API call uses the correct broker keys.
    """
    from api.pricing import get_current_price
    from config import get_contract_value, set_thread_credentials, get_api_key, get_api_secret
    mark = None
    # Set credentials for this API call if a profile is specified
    _creds_set = False
    if profile_id and user_id:
        try:
            p = get_profile(int(profile_id), int(user_id))
            if p:
                set_thread_credentials(p['api_key'], p['api_secret'], p.get('broker'))
                _creds_set = True
        except Exception:
            pass
    try:
        data = get_current_price(leg['product_id'], asset)
        if data:
            mark = data.get('mark_price')
    except Exception:
        pass
    if not mark:
        mark = leg.get('entry_price', 0)
    cv = get_contract_value(asset)
    size = leg.get('size', 0)
    entry = leg.get('entry_price', 0)
    side = leg.get('side', 'sell')
    if side == 'sell':
        pnl = (entry - mark) * size * cv
    else:
        pnl = (mark - entry) * size * cv
    return round(mark, 4), round(pnl, 4)


@app.route('/api/history')
@login_required
def api_history():
    uid = current_user_id()
    all_history = get_history()
    user_sids = {sid for sid, e in strategies.items() if e.get('user_id') == uid}
    user_sids.update(sid for sid, t in all_tracked.items() if t.get('user_id') == uid)
    user_history = [h for h in all_history if h.get('sid') in user_sids or h.get('user_id') == uid]
    return jsonify(user_history)


@app.route('/api/pnl-series')
@login_required
def api_pnl_series():
    """Return cumulative P&L series for the performance chart with date range filtering."""
    uid = current_user_id()
    since = request.args.get('since')  # ISO date string e.g. '2025-01-01'
    until = request.args.get('until')  # ISO date string

    # Build trades list same as dashboard
    all_history = get_history()
    user_sids = {sid for sid, e in strategies.items() if e.get('user_id') == uid}
    user_sids.update(sid for sid, t in all_tracked.items() if t.get('user_id') == uid)
    user_sids.update(s.sid for s in registry.get_user_strategies(uid))
    trades = [t for t in all_history if t.get('sid') in user_sids or t.get('user_id') == uid]
    trade_sids = {t.get('sid') for t in trades}
    for sid_t, t in all_tracked.items():
        if t.get('user_id') != uid or sid_t in trade_sids:
            continue
        trades.append({'sid': sid_t, 'status': t.get('status', 'running'),
                       'started_at': t.get('started_at', ''), 'ended_at': None,
                       'pnl': t.get('pnl', 0), 'params': t.get('details', {})})
    try:
        import json as _json
        conn = get_db()
        db_rows = conn.execute('SELECT * FROM live_strategies WHERE user_id=?', (uid,)).fetchall()
        conn.close()
        existing_sids = {t.get('sid') for t in trades}
        for r in db_rows:
            d = dict(r)
            if d['sid'] in existing_sids:
                continue
            trades.append({'sid': d['sid'], 'status': d['status'],
                           'started_at': d['started_at'], 'ended_at': None,
                           'pnl': d.get('pnl', 0) or 0, 'params': _json.loads(d.get('details') or '{}')})
    except Exception:
        pass

    completed = [t for t in trades if t.get('status') == 'completed']
    completed.sort(key=lambda t: t.get('ended_at') or t.get('started_at', ''))

    # Apply date filters
    if since:
        completed = [t for t in completed if (t.get('ended_at') or t.get('started_at', ''))[:10] >= since]
    if until:
        completed = [t for t in completed if (t.get('ended_at') or t.get('started_at', ''))[:10] <= until]

    pnl_by_date = {}
    cumulative = 0
    for t in completed:
        cumulative += t.get('pnl', 0)
        date_key = (t.get('ended_at') or t.get('started_at', ''))[:10]
        pnl_by_date[date_key] = round(cumulative, 2)
    series = [{'date': d, 'pnl': p} for d, p in pnl_by_date.items()]

    # Also include DB snapshots for running strategies (intraday granularity)
    snapshots = get_pnl_snapshots(uid, since=since)
    if until:
        snapshots = [s for s in snapshots if s['ts'][:10] <= until]

    return jsonify(pnl_series=series, snapshots=snapshots)


# ── IV Crush Strategy Routes ──


def _iv_crush_legs(s):
    """Extract legs list from IVCrushStrategy's call/put positions."""
    legs = []
    for name, pos, entry_p, cv in [
        ('call', s.call_position, s.call_entry_price, s.call_contract_value),
        ('put', s.put_position, s.put_entry_price, s.put_contract_value),
    ]:
        if pos:
            legs.append({
                'product_id': pos.get('product_id'), 'symbol': pos.get('symbol', ''),
                'type': name, 'strike': pos.get('strike_price', ''), 'side': 'sell',
                'size': s.lot_size, 'entry_price': round(entry_p, 2),
                'contract_value': cv,
            })
    return legs


def run_iv_crush(sid, params):
    entry = iv_crush_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.iv_crush import IVCrushStrategy
        s = IVCrushStrategy(
            asset=params.get('asset', 'BTC'),
            expiry_date=params['expiry_date'],
            lot_size=int(params.get('lot_size', 10)),
            iv_rv_threshold=float(params.get('iv_rv_threshold', 1.3)),
            max_loss_pct=float(params.get('max_loss_pct', 50)),
            target_profit_pct=float(params.get('target_profit_pct', 30)),
            monitoring_interval=int(params.get('monitoring_interval', 10)),
        )
        entry['strategy'] = s
        entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put(f"✗ Init failed: {s.status_msg or 'unknown'}")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return

        # Save legs to DB and register positions
        legs = _iv_crush_legs(s)
        try:
            update_strategy_db(sid, legs=legs)
        except Exception:
            pass
        for leg in legs:
            position_tracker.open(uid, leg['product_id'], leg['symbol'],
                type=leg['type'], strike=leg.get('strike', ''), side='sell',
                size=leg['size'], entry_price=leg['entry_price'],
                asset=params.get('asset', 'BTC'), source='IV Crush')

        # Wrap monitor to inject PnL snapshots
        import strategy.iv_crush as _iv_mod
        _orig_sleep = _iv_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.total_pnl, 2))
                    update_strategy_db(sid, pnl=round(s.total_pnl, 2), legs=_iv_crush_legs(s))
                except Exception:
                    pass
        _iv_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _iv_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(getattr(strategy, 'total_pnl', 0), 2)
            record_end(sid, pnl, 0)
            update_tracked(sid, status='completed', pnl=pnl,
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] IV Crush {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] IV Crush {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
        _teardown_strategy_thread(entry)


@app.route('/api/iv-crush/start', methods=['POST'])
@login_required
def iv_crush_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [],
             'running': False, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    iv_crush_strategies[sid] = entry
    track_strategy(sid, 'IV Crush', f"{params.get('asset','BTC')} IV Crush {params.get('expiry_date','')}", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_iv_crush, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/iv-crush/stop', methods=['POST'])
@login_required
def iv_crush_stop():
    sid = request.json.get('sid')
    e = iv_crush_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        e['strategy'].running = False
        e['strategy'].close_all()
    return jsonify(status="stopping")


@app.route('/api/iv-crush/stream/<sid>')
@login_required
def iv_crush_stream(sid):
    e = iv_crush_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/iv-crush/status/<sid>')
@login_required
def iv_crush_status(sid):
    e = iv_crush_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    s = e.get('strategy')
    if not e['running'] or not s:
        return jsonify(running=False, status_msg=getattr(s, 'status_msg', '') if s else '')
    pnl_pct = (s.total_pnl / s.total_premium * 100) if s.total_premium > 0 else 0
    return jsonify(
        running=True,
        total_pnl=round(s.total_pnl, 2),
        unrealized_pnl=round(s.unrealized_pnl, 2),
        total_premium=round(s.total_premium, 2),
        pnl_pct=round(pnl_pct, 1),
        iv_at_entry=round(s.iv_at_entry, 4),
        current_iv=round(s.current_iv, 4),
        iv_crush_pct=s.iv_crush_pct,
        iv_rv_ratio=round(s.iv_rv_ratio, 2),
        call=_iv_leg(s, 'call'),
        put=_iv_leg(s, 'put'),
    )


def _iv_leg(s, leg):
    pos = getattr(s, f'{leg}_position')
    if not pos:
        return None
    entry = getattr(s, f'{leg}_entry_price')
    ws = s.ws_manager.get_latest_price(pos['symbol'])
    mark = ws['mark_price'] if ws else entry
    return dict(symbol=pos['symbol'], strike=pos.get('strike_price', ''),
                entry=round(entry, 2), mark=round(mark, 2))


# ── Call Ratio Spread Routes ──


def run_call_ratio(sid, params):
    entry = call_ratio_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.call_ratio import CallRatioStrategy
        s = CallRatioStrategy(
            asset=params.get('asset', 'BTC'), expiry_date=params.get('expiry_date', ''),
            lot_size=int(params.get('lot_size', 10)),
            buy_offset_pct=float(params.get('buy_offset_pct', 2)),
            sell_offset_pct=float(params.get('sell_offset_pct', 4)),
            hedge_offset_pct=float(params.get('hedge_offset_pct', 7)),
            target_pct=float(params.get('target_pct', 5)),
            sl_pct=float(params.get('sl_pct', 8)),
            monitoring_interval=int(params.get('monitoring_interval', 30)),
        )
        entry['strategy'] = s; entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put(f"✗ Init failed: {s.status_msg or 'unknown'}")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return

        # Save legs to DB and register positions
        try:
            update_strategy_db(sid, legs=s.legs)
        except Exception:
            pass
        for leg in s.legs:
            position_tracker.open(uid, leg['product_id'], leg['symbol'],
                type='call', strike=leg.get('strike', ''), side=leg['side'],
                size=leg['size'], entry_price=leg['entry_price'],
                asset=params.get('asset', 'BTC'), source='Call Ratio')

        # Wrap monitor to inject PnL snapshots
        import strategy.call_ratio as _cr_mod
        _orig_sleep = _cr_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.total_pnl, 2))
                    update_strategy_db(sid, pnl=round(s.total_pnl, 2), legs=s.legs)
                except Exception:
                    pass
        _cr_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _cr_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(getattr(strategy, 'total_pnl', 0), 2)
            record_end(sid, pnl, 0)
            update_tracked(sid, status='completed', pnl=pnl,
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] Call Ratio {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] Call Ratio {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
        _teardown_strategy_thread(entry)


@app.route('/api/call-ratio/start', methods=['POST'])
@login_required
def call_ratio_start():
    params = request.json; profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key: return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [], 'running': False, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    call_ratio_strategies[sid] = entry
    track_strategy(sid, 'Call Ratio', f"{params.get('asset','BTC')} Call Ratio", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_call_ratio, args=(sid, params), daemon=True); entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/call-ratio/stop', methods=['POST'])
@login_required
def call_ratio_stop():
    sid = request.json.get('sid'); e = call_ratio_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id(): return jsonify(error="Not found"), 404
    if e.get('strategy'): e['strategy'].running = False; e['strategy'].close_all()
    return jsonify(status="stopping")


@app.route('/api/call-ratio/stream/<sid>')
@login_required
def call_ratio_stream(sid):
    e = call_ratio_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id(): return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__": yield f"event: stopped\ndata: done\n\n"; break
                yield f"data: {msg}\n\n"
            except queue.Empty: yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/call-ratio/status/<sid>')
@login_required
def call_ratio_status(sid):
    e = call_ratio_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id(): return jsonify(running=False)
    s = e.get('strategy')
    if not e['running'] or not s: return jsonify(running=False, status_msg=getattr(s, 'status_msg', '') if s else '')
    return jsonify(running=True, total_pnl=s.total_pnl, pnl_pct=s.pnl_pct, deployed_margin=round(s.deployed_margin, 2),
                   legs=[{'symbol': l['symbol'], 'strike': l['strike'], 'side': l['side'], 'size': l['size'],
                          'entry': round(l['entry_price'], 2), 'mark': round(l.get('current_mark', l['entry_price']), 2),
                          'pnl': l.get('current_pnl', 0)} for l in s.legs])


# ── OI Strategy Routes ──


def run_oi_strategy(sid, params):
    entry = oi_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.oi_strategy import OIStrategy
        print(f"[OI] Params received: entry_hour={params.get('entry_hour')}, entry_minute={params.get('entry_minute')}")
        s = OIStrategy(
            asset=params.get('asset', 'BTC'),
            lot_size=int(params.get('lot_size', 100)),
            target_pct=float(params.get('target_pct', 50)) / 100,
            stop_loss_pct=float(params.get('stop_loss_pct', 50)) / 100,
            monitor_interval=int(params.get('monitoring_interval', 30)),
            entry_hour=int(params.get('entry_hour', 18)),
            entry_minute=int(params.get('entry_minute', 30)),
        )
        s._log_queue = entry['log_queue']
        s._log_history = entry['log_history']
        import config as _cfg
        s._api_key = _cfg.get_api_key()
        s._api_secret = _cfg.get_api_secret()
        s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
        entry['strategy'] = s
        entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put("✗ Init failed")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return

        # Save legs to DB and register positions
        try:
            update_strategy_db(sid, legs=s.legs)
        except Exception:
            pass
        for leg in s.legs:
            position_tracker.open(uid, leg['product_id'], leg['symbol'],
                type=leg['type'], strike=leg.get('strike', ''), side='sell',
                size=leg['size'], entry_price=leg['entry_price'],
                asset=params.get('asset', 'BTC'), source='OI Strategy')

        # Wrap monitor to inject PnL snapshots
        import strategy.oi_strategy as _oi_mod
        _orig_sleep = _oi_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.pnl, 2))
                    update_strategy_db(sid, pnl=round(s.pnl, 2), legs=s.legs,
                        details={**params, 'profile_id': entry.get('profile_id'),
                                 'cumulative_pnl': s.cumulative_pnl,
                                 'total_days_traded': s.total_days_traded,
                                 'trade_log': s.trade_log[-50:]})
                except Exception:
                    pass
        _oi_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _oi_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(strategy.cumulative_pnl + strategy._pnl, 2)
            record_end(sid, pnl, getattr(strategy, 'total_days_traded', 0))
            update_tracked(sid, status='completed', pnl=pnl,
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] OI Strategy {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] OI Strategy {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
        _teardown_strategy_thread(entry)


@app.route('/api/oi-strategy/start', methods=['POST'])
@login_required
def oi_strategy_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [],
             'running': False, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    oi_strategies[sid] = entry
    track_strategy(sid, 'OI Strategy', f"{params.get('asset','BTC')} OI Strategy", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_oi_strategy, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/oi-strategy/stop', methods=['POST'])
@login_required
def oi_strategy_stop():
    sid = request.json.get('sid')
    e = oi_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        e['strategy']._running = False
        e['strategy'].close_all()
    return jsonify(status="stopping")


@app.route('/api/oi-strategy/stream/<sid>')
@login_required
def oi_strategy_stream(sid):
    e = oi_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/oi-strategy/status/<sid>')
@login_required
def oi_strategy_status(sid):
    e = oi_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    s = e.get('strategy')
    if not e['running'] or not s:
        return jsonify(running=False)
    pnl_pct = (s._pnl / s.max_premium * 100) if s.max_premium > 0 else 0
    return jsonify(
        running=True,
        total_pnl=round(s.pnl, 2),
        today_pnl=round(s._pnl, 2),
        max_premium=round(s.max_premium, 2),
        pnl_pct=round(pnl_pct, 1),
        cumulative_pnl=round(s.cumulative_pnl, 2),
        days_traded=s.total_days_traded,
        max_call_oi_strike=s.max_call_oi_strike,
        max_put_oi_strike=s.max_put_oi_strike,
        spot_price=s.spot_price,
        expiry=s.expiry,
        entry_hour=s.entry_hour,
        entry_minute=s.entry_minute,
        trade_log=s.trade_log[-10:],
        legs=[{'symbol': l['symbol'], 'strike': l['strike'], 'type': l['type'],
               'side': l['side'], 'size': l['size'], 'entry_price': round(l['entry_price'], 2)}
              for l in s.legs],
    )


# ── Daily Strangle Strategy Routes ──


def run_strangle_strategy(sid, params):
    entry = strangle_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.daily_strangle import DailyStrangle
        s = DailyStrangle(
            asset=params.get('asset', 'BTC'),
            lot_size=int(params.get('lot_size', 100)),
            target_premium=float(params.get('target_premium', 100)),
            sl_pct=float(params.get('sl_pct', 105)) / 100,
            entry_hour=int(params.get('entry_hour', 9)),
            entry_minute=int(params.get('entry_minute', 0)),
            exit_hour=int(params.get('exit_hour', 17)),
            exit_minute=int(params.get('exit_minute', 15)),
            monitor_interval=int(params.get('monitoring_interval', 10)),
        )
        s._log_queue = entry['log_queue']
        s._log_history = entry['log_history']
        import config as _cfg
        s._api_key = _cfg.get_api_key()
        s._api_secret = _cfg.get_api_secret()
        s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
        entry['strategy'] = s
        entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put("✗ Init failed")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return

        import strategy.daily_strangle as _ds_mod
        _orig_sleep = _ds_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.pnl, 2))
                    update_strategy_db(sid, pnl=round(s.pnl, 2), legs=s.legs,
                        details={**params, 'profile_id': entry.get('profile_id'),
                                 'cumulative_pnl': s.cumulative_pnl,
                                 'total_days_traded': s.total_days_traded,
                                 'trade_log': s.trade_log[-50:]})
                except Exception:
                    pass
        _ds_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _ds_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(strategy.cumulative_pnl, 2)
            record_end(sid, pnl, getattr(strategy, 'total_days_traded', 0))
            update_tracked(sid, status='completed', pnl=pnl,
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] Daily Strangle {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] Daily Strangle {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
        _teardown_strategy_thread(entry)


@app.route('/api/strangle/start', methods=['POST'])
@login_required
def strangle_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [],
             'running': True, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    strangle_strategies[sid] = entry
    track_strategy(sid, 'Daily Strangle', f"{params.get('asset','BTC')} 0DTE Strangle", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_strangle_strategy, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/strangle/stop', methods=['POST'])
@login_required
def strangle_stop():
    sid = request.json.get('sid')
    e = strangle_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        try:
            from config import set_thread_credentials
            profile_id = e.get('profile_id')
            if profile_id:
                api_key, api_secret, _, broker = get_profile_creds(profile_id)
                if api_key:
                    set_thread_credentials(api_key, api_secret, broker)
            e['strategy']._running = False
            e['strategy'].close_all()
        except Exception as ex:
            logger.error(f"[strangle_stop] {sid} error: {ex}")
    return jsonify(status="stopping")


@app.route('/api/strangle/stream/<sid>')
@login_required
def strangle_stream(sid):
    e = strangle_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    history = e.get('log_history', [])
    def generate():
        # Send buffered history first
        for msg in list(history):
            yield f"data: {msg}\n\n"
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/strangle/status/<sid>')
@login_required
def strangle_status(sid):
    e = strangle_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    if not e['running']:
        return jsonify(running=False)
    s = e.get('strategy')
    if not s:
        return jsonify(running=True, total_pnl=0, cumulative_pnl=0, days_traded=0, trade_log=[], legs=[])
    profile_id = e.get('profile_id')
    uid = e.get('user_id')
    enriched_legs = []
    for l in s.legs:
        mark, pnl = _enrich_leg(l, getattr(s, 'asset', 'BTC'), profile_id=profile_id, user_id=uid)
        enriched_legs.append({
            'symbol': l['symbol'], 'strike': l['strike'], 'type': l['type'],
            'side': l['side'], 'size': l['size'], 'entry_price': round(l['entry_price'], 2),
            'mark_price': mark, 'pnl': pnl,
            'stopped': l.get('stopped', False),
            'product_id': l.get('product_id'),
        })
    return jsonify(
        running=True,
        total_pnl=round(s.pnl, 2),
        cumulative_pnl=round(s.cumulative_pnl, 2),
        session_pnl=round(s._pnl, 4),
        days_traded=s.total_days_traded,
        trade_log=s.trade_log[-10:],
        legs=enriched_legs,
    )


# ── Pivot + SuperTrend (0DTE) Routes ──


def run_pivot_st_strategy(sid, params):
    entry = pivot_st_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.pivot_supertrend import PivotSuperTrend
        s = PivotSuperTrend(
            asset=params.get('asset', 'BTC'),
            lot_size=int(params.get('lot_size', 100)),
            target_delta=float(params.get('target_delta', 0.50)),
            delta_tolerance=float(params.get('delta_tolerance', 0.15)),
            st_period=int(params.get('st_period', 7)),
            st_multiplier=int(params.get('st_multiplier', 3)),
            max_trades=int(params.get('max_trades', 3)),
            monitor_interval=int(params.get('monitoring_interval', 10)),
            entry_hour=int(params.get('entry_hour', 9)),
            entry_minute=int(params.get('entry_minute', 20)),
            exit_hour=int(params.get('exit_hour', 17)),
            exit_minute=int(params.get('exit_minute', 0)),
        )
        s._log_queue = entry['log_queue']
        s._log_history = entry['log_history']
        import config as _cfg
        s._api_key = _cfg.get_api_key()
        s._api_secret = _cfg.get_api_secret()
        s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
        entry['strategy'] = s
        entry['running'] = True
        s._sid = sid
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put("✗ Init failed")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return
        s.monitor()
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(strategy.cumulative_pnl, 4)
            record_end(sid, pnl, strategy.total_days_traded)
            update_tracked(sid, status='completed', pnl=pnl,
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] PivotST {sid} — thread exited without strategy object")
        else:
            logger.warning(f"[deploy] PivotST {sid} — thread exited unexpectedly, keeping status 'running'")
        _teardown_strategy_thread(entry)


@app.route('/api/pivot-st/start', methods=['POST'])
@login_required
def pivot_st_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [],
             'running': True, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    pivot_st_strategies[sid] = entry
    track_strategy(sid, 'Pivot SuperTrend', f"{params.get('asset','BTC')} Pivot+ST 0DTE", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_pivot_st_strategy, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/pivot-st/stop', methods=['POST'])
@login_required
def pivot_st_stop():
    sid = request.json.get('sid')
    e = pivot_st_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        try:
            from config import set_thread_credentials
            profile_id = e.get('profile_id')
            if profile_id:
                api_key, api_secret, _, broker = get_profile_creds(profile_id)
                if api_key:
                    set_thread_credentials(api_key, api_secret, broker)
            e['strategy']._running = False
            e['strategy'].close_all()
        except Exception as ex:
            logger.error(f"[pivot_st_stop] {sid} error: {ex}")
    return jsonify(status="stopping")


@app.route('/api/pivot-st/stream/<sid>')
@login_required
def pivot_st_stream(sid):
    e = pivot_st_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    history = e.get('log_history', [])
    def generate():
        for msg in list(history):
            yield f"data: {msg}\n\n"
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/pivot-st/status/<sid>')
@login_required
def pivot_st_status(sid):
    e = pivot_st_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    if not e['running']:
        return jsonify(running=False)
    s = e.get('strategy')
    if not s:
        return jsonify(running=True, total_pnl=0, cumulative_pnl=0, days_traded=0, trade_log=[], legs=[])
    profile_id = e.get('profile_id')
    uid = e.get('user_id')
    enriched_legs = []
    for l in s.legs:
        mark, pnl = _enrich_leg(l, getattr(s, 'asset', 'BTC'), profile_id=profile_id, user_id=uid)
        enriched_legs.append({
            'symbol': l.get('symbol', ''), 'strike': l.get('strike', ''), 'type': l.get('type', ''),
            'side': l.get('side', ''), 'size': l.get('size', 0), 'entry_price': round(l.get('entry_price', 0), 4),
            'mark_price': mark, 'pnl': pnl,
            'signal': l.get('signal', ''),
            'product_id': l.get('product_id'),
        })
    return jsonify(
        running=True,
        total_pnl=round(s.pnl, 4),
        cumulative_pnl=round(s.cumulative_pnl, 4),
        days_traded=s.total_days_traded,
        today_trades=s.today_trade_count,
        max_trades=s.max_trades,
        trade_log=s.trade_log[-10:],
        legs=enriched_legs,
        pivot=round(s._pivot, 0) if s._pivot else None,
        r1=round(s._r1, 0) if s._r1 else None,
        s1=round(s._s1, 0) if s._s1 else None,
        st_direction='bullish' if s._st_direction == 1 else 'bearish' if s._st_direction == -1 else None,
    )


# ── Portfolio Strangle (0DTE 3-entry) Routes ──


def run_portfolio_strangle(sid, params):
    entry = portfolio_strangle_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.portfolio_strangle import PortfolioStrangle
        # Parse entry_times from params
        entry_times_raw = params.get('entry_times', ['9:15', '10:20', '11:15'])
        entry_times = []
        for t in entry_times_raw:
            parts = t.split(':')
            entry_times.append((int(parts[0]), int(parts[1])))

        skip_days_raw = params.get('skip_weekdays', [4, 6])
        skip_days = [int(d) for d in skip_days_raw]

        s = PortfolioStrangle(
            asset=params.get('asset', 'BTC'),
            lot_size=int(params.get('lot_size', 30)),
            sl_pct=float(params.get('sl_pct', 300)) / 100,
            recost_entries=int(params.get('recost_entries', 1)),
            otm_index=int(params.get('otm_index', 5)),
            entry_times=entry_times,
            exit_hour=int(params.get('exit_hour', 17)),
            exit_minute=int(params.get('exit_minute', 29)),
            monitor_interval=int(params.get('monitoring_interval', 10)),
            skip_weekdays=skip_days,
        )
        s._log_queue = entry['log_queue']
        s._log_history = entry['log_history']
        s._sid = sid
        import config as _cfg
        s._api_key = _cfg.get_api_key()
        s._api_secret = _cfg.get_api_secret()
        s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
        entry['strategy'] = s
        entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put("✗ Init failed")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return

        import strategy.portfolio_strangle as _ps_mod
        _orig_sleep = _ps_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.pnl, 4))
                except Exception:
                    pass
        _ps_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _ps_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(strategy.cumulative_pnl, 4)
            record_end(sid, pnl, getattr(strategy, 'total_days_traded', 0))
            update_tracked(sid, status='completed', pnl=round(pnl, 4),
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] Portfolio Strangle {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] Portfolio Strangle {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
        _teardown_strategy_thread(entry)


@app.route('/api/portfolio-strangle/start', methods=['POST'])
@login_required
def portfolio_strangle_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [],
             'running': True, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    portfolio_strangle_strategies[sid] = entry
    track_strategy(sid, 'Portfolio Strangle', f"{params.get('asset','BTC')} 0DTE Portfolio",
                   current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_portfolio_strangle, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/portfolio-strangle/stop', methods=['POST'])
@login_required
def portfolio_strangle_stop():
    sid = request.json.get('sid')
    e = portfolio_strangle_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        try:
            from config import set_thread_credentials
            profile_id = e.get('profile_id')
            if profile_id:
                api_key, api_secret, _, broker = get_profile_creds(profile_id)
                if api_key:
                    set_thread_credentials(api_key, api_secret, broker)
            e['strategy']._running = False
            e['strategy'].close_all()
        except Exception as ex:
            logger.error(f"[portfolio_strangle_stop] {sid} error: {ex}")
    return jsonify(status="stopping")


@app.route('/api/portfolio-strangle/stream/<sid>')
@login_required
def portfolio_strangle_stream(sid):
    e = portfolio_strangle_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    history = e.get('log_history', [])
    def generate():
        for msg in list(history):
            yield f"data: {msg}\n\n"
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/portfolio-strangle/status/<sid>')
@login_required
def portfolio_strangle_status(sid):
    e = portfolio_strangle_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    if not e['running']:
        return jsonify(running=False)
    s = e.get('strategy')
    if not s:
        return jsonify(running=True, total_pnl=0, cumulative_pnl=0, days_traded=0, trade_log=[], legs=[])
    profile_id = e.get('profile_id')
    uid = e.get('user_id')
    enriched_legs = []
    for l in s.legs:
        mark, pnl = _enrich_leg(l, getattr(s, 'asset', 'BTC'), profile_id=profile_id, user_id=uid)
        enriched_legs.append({
            'symbol': l['symbol'], 'strike': l['strike'], 'type': l['type'],
            'side': l['side'], 'size': l['size'], 'entry_price': round(l['entry_price'], 4),
            'mark_price': mark, 'pnl': pnl,
            'stopped': l.get('stopped', False),
            'product_id': l.get('product_id'),
        })
    return jsonify(
        running=True,
        total_pnl=round(s.pnl, 4),
        cumulative_pnl=round(s.cumulative_pnl, 4),
        session_pnl=round(s._pnl, 4),
        days_traded=s.total_days_traded,
        trade_log=s.trade_log[-20:],
        legs=enriched_legs,
    )


# ── Hybrid Switch BTST Strategy Routes ──


def run_hybrid_strategy(sid, params):
    entry = hybrid_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return
    try:
        from strategy.hybrid_switch import HybridSwitch
        s = HybridSwitch(
            asset=params.get('asset', 'BTC'),
            lot_size=int(params.get('lot_size', 1)),
            buy_multiplier=int(params.get('buy_multiplier', 10)),
            sell_sl_pct=float(params.get('sell_sl_pct', 200)) / 100,
            buy_sl_pct=float(params.get('buy_sl_pct', 50)) / 100,
            trail_points=float(params.get('trail_points', 10)),
            otm_index=int(params.get('otm_index', 5)),
            entry_hour=int(params.get('entry_hour', 19)),
            entry_minute=int(params.get('entry_minute', 15)),
            exit_hour=int(params.get('exit_hour', 17)),
            exit_minute=int(params.get('exit_minute', 15)),
            monitor_interval=int(params.get('monitoring_interval', 10)),
        )
        s._log_queue = entry['log_queue']
        s._log_history = entry['log_history']
        import config as _cfg
        s._api_key = _cfg.get_api_key()
        s._api_secret = _cfg.get_api_secret()
        s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
        entry['strategy'] = s
        entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put("✗ Init failed")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return
        import strategy.hybrid_switch as _hs_mod
        _orig_sleep = _hs_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.pnl, 2))
                    update_strategy_db(sid, pnl=round(s.pnl, 2), legs=s.legs,
                        details={**params, 'profile_id': entry.get('profile_id'),
                                 'cumulative_pnl': s.cumulative_pnl,
                                 'total_days_traded': s.total_days_traded,
                                 'trade_log': s.trade_log[-50:]})
                except Exception:
                    pass
        _hs_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _hs_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(strategy.cumulative_pnl, 2)
            record_end(sid, pnl, getattr(strategy, 'total_days_traded', 0))
            update_tracked(sid, status='completed', pnl=pnl,
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] Hybrid Switch {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] Hybrid Switch {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
        _teardown_strategy_thread(entry)


@app.route('/api/hybrid/start', methods=['POST'])
@login_required
def hybrid_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [],
             'running': True, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    hybrid_strategies[sid] = entry
    track_strategy(sid, 'Hybrid Switch', f"{params.get('asset','BTC')} Hybrid BTST", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_hybrid_strategy, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/hybrid/stop', methods=['POST'])
@login_required
def hybrid_stop():
    sid = request.json.get('sid')
    e = hybrid_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        try:
            from config import set_thread_credentials
            profile_id = e.get('profile_id')
            if profile_id:
                api_key, api_secret, _, broker = get_profile_creds(profile_id)
                if api_key:
                    set_thread_credentials(api_key, api_secret, broker)
            e['strategy']._running = False
            e['strategy'].close_all()
        except Exception as ex:
            logger.error(f"[hybrid_stop] {sid} error: {ex}")
    return jsonify(status="stopping")


@app.route('/api/hybrid/stream/<sid>')
@login_required
def hybrid_stream(sid):
    e = hybrid_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    history = e.get('log_history', [])
    def generate():
        for msg in list(history):
            yield f"data: {msg}\n\n"
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/hybrid/status/<sid>')
@login_required
def hybrid_status(sid):
    e = hybrid_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    if not e['running']:
        return jsonify(running=False)
    s = e.get('strategy')
    if not s:
        return jsonify(running=True, total_pnl=0, cumulative_pnl=0, days_traded=0, trade_log=[], legs=[])
    profile_id = e.get('profile_id')
    uid = e.get('user_id')
    enriched_legs = []
    for l in s.legs:
        mark, pnl = _enrich_leg(l, getattr(s, 'asset', 'BTC'), profile_id=profile_id, user_id=uid)
        enriched_legs.append({
            'symbol': l['symbol'], 'strike': l['strike'], 'type': l['type'],
            'side': l['side'], 'size': l['size'], 'entry_price': round(l['entry_price'], 2),
            'mark_price': mark, 'pnl': pnl,
            'role': l.get('role', ''), 'active': l.get('active', False),
            'product_id': l.get('product_id'),
        })
    return jsonify(
        running=True,
        total_pnl=round(s.pnl, 2),
        cumulative_pnl=round(s.cumulative_pnl, 2),
        session_pnl=round(s._pnl, 4),
        days_traded=s.total_days_traded,
        trade_log=s.trade_log[-10:],
        legs=enriched_legs,
    )


# ── Weekly Delta Neutral Strategy Routes ──


def run_weekly_dn(sid, params):
    entry = weekly_dn_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.weekly_delta_neutral import WeeklyDeltaNeutral
        s = WeeklyDeltaNeutral(
            asset=params.get('asset', 'BTC'),
            target_delta=float(params.get('target_delta', 0.20)),
            delta_tolerance=float(params.get('delta_tolerance', 0.05)),
            lot_size=int(params.get('lot_size', 100)),
            premium_threshold=float(params.get('premium_threshold', 40)) / 100,
            target_pnl=float(params.get('target_pnl', 25)),
            max_adjustments=int(params.get('max_adjustments', 5)),
            monitoring_interval=int(params.get('monitoring_interval', 5)),
            entry_hour=int(params.get('entry_hour', 21)),
            entry_minute=int(params.get('entry_minute', 0)),
        )
        entry['strategy'] = s
        entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put("✗ Init failed")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return
        # Wrap sleep to persist state periodically
        import strategy.weekly_delta_neutral as _wdn_mod
        _orig_sleep = _wdn_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    update_strategy_db(sid, pnl=round(s.pnl, 2),
                        details={**params, 'profile_id': entry.get('profile_id'),
                                 'cumulative_pnl': s.cumulative_pnl,
                                 'weeks_traded': s.weeks_traded,
                                 'trade_log': s.trade_log[-50:]})
                except Exception:
                    pass
        _wdn_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _wdn_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(strategy.cumulative_pnl, 2)
            weeks = strategy.weeks_traded if strategy else 0
            record_end(sid, pnl, weeks)
            update_tracked(sid, status='completed', pnl=pnl,
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] Weekly DN {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] Weekly DN {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
        _teardown_strategy_thread(entry)


@app.route('/api/weekly-dn/start', methods=['POST'])
@login_required
def weekly_dn_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [],
             'running': False, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    weekly_dn_strategies[sid] = entry
    track_strategy(sid, 'Weekly DN', f"{params.get('asset','BTC')} Weekly Delta Neutral", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_weekly_dn, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/weekly-dn/stop', methods=['POST'])
@login_required
def weekly_dn_stop():
    sid = request.json.get('sid')
    e = weekly_dn_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        e['strategy'].close_all()
    return jsonify(status="stopping")


@app.route('/api/weekly-dn/stream/<sid>')
@login_required
def weekly_dn_stream(sid):
    e = weekly_dn_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/weekly-dn/status/<sid>')
@login_required
def weekly_dn_status(sid):
    e = weekly_dn_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    s = e.get('strategy')
    if not e['running'] or not s:
        return jsonify(running=False)
    return jsonify(
        running=True,
        cumulative_pnl=round(s.cumulative_pnl, 2),
        current_pnl=round(s.pnl, 2),
        weeks_traded=s.weeks_traded,
        entry_time=f"{s.entry_hour}:{s.entry_minute:02d}",
        trade_log=s.trade_log[-10:],
        has_active_trade=s._current_strategy is not None,
    )


# ── EMA Credit Spread Strategy Routes ──


def run_ema_spread(sid, params):
    entry = ema_spread_strategies[sid]
    uid = entry['user_id']
    if not _setup_strategy_thread(entry):
        entry['log_queue'].put("__STOPPED__")
        return

    try:
        from strategy.ema_credit_spread import EMACreditSpread
        print(f"[EMA Spread] Params received: entry_hour={params.get('entry_hour')}, entry_minute={params.get('entry_minute')}")
        s = EMACreditSpread(
            asset=params.get('asset', 'BTC'),
            lot_size=int(params.get('lot_size', 100)),
            sell_delta=float(params.get('sell_delta', 0.20)),
            buy_delta=float(params.get('buy_delta', 0.10)),
            ema_period=int(params.get('ema_period', 14)),
            tp_pct=float(params.get('tp_pct', 90)) / 100,
            sl_pct=float(params.get('sl_pct', 100)) / 100,
            monitor_interval=int(params.get('monitoring_interval', 30)),
            entry_hour=int(params.get('entry_hour', 18)),
            entry_minute=int(params.get('entry_minute', 30)),
            min_expiry_days=int(params.get('min_expiry_days', 8)),
        )
        s._log_queue = entry['log_queue']
        s._log_history = entry['log_history']
        s._sid = sid
        import config as _cfg
        s._api_key = _cfg.get_api_key()
        s._api_secret = _cfg.get_api_secret()
        s._broker = getattr(_cfg._thread_local, 'broker', 'demo')
        entry['strategy'] = s
        entry['running'] = True
        record_start(sid, params, user_id=uid)
        if not s.initialize():
            entry['log_queue'].put("✗ Init failed")
            entry['running'] = False
            entry['log_queue'].put("__STOPPED__")
            return

        # Wrap sleep for PnL snapshots
        import strategy.ema_credit_spread as _ecs_mod
        _orig_sleep = _ecs_mod.time.sleep
        _tick = [0]
        def _snap_sleep(secs):
            _orig_sleep(secs)
            _tick[0] += 1
            if _tick[0] % 6 == 0:
                try:
                    save_pnl_snapshot(uid, sid, round(s.pnl, 4))
                    update_strategy_db(sid, pnl=round(s.pnl, 4), legs=s.legs,
                        details={**params, 'profile_id': entry.get('profile_id'),
                                 'cumulative_pnl': s.cumulative_pnl,
                                 'total_days_traded': s.total_days_traded,
                                 'trade_log': s.trade_log[-50:]})
                except Exception:
                    pass
        _ecs_mod.time.sleep = _snap_sleep
        try:
            s.monitor()
        finally:
            _ecs_mod.time.sleep = _orig_sleep
    except Exception as e:
        entry['log_queue'].put(f"❌ Error: {e}")
    finally:
        strategy = entry.get('strategy')
        if strategy and not strategy._running:
            pnl = round(strategy.cumulative_pnl, 4)
            record_end(sid, pnl, getattr(strategy, 'total_days_traded', 0))
            update_tracked(sid, status='completed', pnl=round(pnl, 2),
                           exit_reason='intentional_close')
        elif not strategy:
            logger.warning(f"[deploy] EMA Spread {sid} — thread exited without strategy object, keeping status 'running'")
        else:
            logger.warning(f"[deploy] EMA Spread {sid} — thread exited unexpectedly, keeping status 'running' for re-resume")
        _teardown_strategy_thread(entry)


@app.route('/api/ema-spread/start', methods=['POST'])
@login_required
def ema_spread_start():
    params = request.json
    profile_id = params.pop('profile_id', None)
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    sid = str(uuid.uuid4())[:8]
    entry = {'thread': None, 'strategy': None, 'log_queue': queue.Queue(maxsize=500), 'log_history': [],
             'running': False, 'params': params, 'user_id': current_user_id(), 'profile_id': profile_id}
    ema_spread_strategies[sid] = entry
    track_strategy(sid, 'EMA Spread', f"{params.get('asset','BTC')} EMA Credit Spread", current_user_id(), details={**params, 'profile_id': profile_id})
    entry['thread'] = threading.Thread(target=run_ema_spread, args=(sid, params), daemon=True)
    entry['thread'].start()
    return jsonify(status="started", sid=sid)


@app.route('/api/ema-spread/stop', methods=['POST'])
@login_required
def ema_spread_stop():
    sid = request.json.get('sid')
    e = ema_spread_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(error="Not found"), 404
    if e.get('strategy'):
        try:
            from config import set_thread_credentials
            profile_id = e.get('profile_id')
            if profile_id:
                api_key, api_secret, _, broker = get_profile_creds(profile_id)
                if api_key:
                    set_thread_credentials(api_key, api_secret, broker)
            e['strategy'].close_all()
        except Exception as ex:
            logger.error(f"[ema_spread_stop] {sid} error: {ex}")
    return jsonify(status="stopping")


@app.route('/api/ema-spread/stream/<sid>')
@login_required
def ema_spread_stream(sid):
    e = ema_spread_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    q = e['log_queue']
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg == "__STOPPED__":
                    yield f"event: stopped\ndata: done\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/ema-spread/status/<sid>')
@login_required
def ema_spread_status(sid):
    e = ema_spread_strategies.get(sid)
    if not e or e.get('user_id') != current_user_id():
        return jsonify(running=False)
    s = e.get('strategy')
    if not e['running'] or not s:
        return jsonify(running=False)
    pnl_pct = (s._pnl / s.net_premium * 100) if s.net_premium > 0 else 0
    profile_id = e.get('profile_id')
    uid = e.get('user_id')
    enriched_legs = []
    for l in s.legs:
        mark, pnl = _enrich_leg(l, getattr(s, 'asset', 'BTC'), profile_id=profile_id, user_id=uid)
        enriched_legs.append({
            'symbol': l['symbol'], 'strike': l['strike'], 'type': l['type'],
            'side': l['side'], 'delta': l['delta'], 'size': l['size'],
            'entry_price': round(l['entry_price'], 4),
            'mark_price': mark, 'pnl': pnl,
            'product_id': l.get('product_id'),
        })
    return jsonify(
        running=True,
        cumulative_pnl=round(s.cumulative_pnl, 4),
        today_pnl=round(s._pnl, 4),
        session_pnl=round(s._pnl, 4),
        net_premium=round(s.net_premium, 4),
        pnl_pct=round(pnl_pct, 1),
        days_traded=s.total_days_traded,
        entry_time=f"{s.entry_hour}:{s.entry_minute:02d}",
        trade_log=s.trade_log[-10:],
        legs=enriched_legs,
    )


# ── Option Chain Routes ──

DELTA_ASSETS = {'BTC', 'ETH'}

@app.route('/api/expiries')
@login_required
def api_expiries():
    asset = request.args.get('asset', 'BTC')
    if asset in DELTA_ASSETS:
        from api.chain import get_expiries
        from config import set_thread_credentials
        profile_id = request.args.get('profile_id')
        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)
        elif not profile_id:
            # No profile and no default keys — still set broker based on what we have
            set_thread_credentials('', '', 'demo')
        return jsonify(expiries=get_expiries(asset))
    from api.nse import get_nse_expiries
    return jsonify(expiries=get_nse_expiries(asset))


@app.route('/api/chain')
@login_required
def api_chain():
    asset = request.args.get('asset', 'BTC')
    expiry = request.args.get('expiry', '')
    if not expiry:
        return jsonify(error="expiry required"), 400
    if asset in DELTA_ASSETS:
        from api.chain import get_option_chain_full
        from config import set_thread_credentials
        profile_id = request.args.get('profile_id')
        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)
        elif not profile_id:
            set_thread_credentials('', '', 'demo')
        chain, spot, exp = get_option_chain_full(expiry, asset)
    else:
        from api.nse import get_nse_chain
        try:
            chain, spot, exp = get_nse_chain(asset, expiry)
        except Exception as e:
            logger.error(f"NSE chain error: {e}")
            return jsonify(error=str(e)), 500
    if chain is None:
        return jsonify(error="No data for this expiry"), 500
    return jsonify(chain=chain, spot_price=spot, expiry=exp)


@app.route('/api/place-legs', methods=['POST'])
@login_required
@credits_required('place_legs')
def api_place_legs():
    """Place multiple option legs and optionally start monitoring."""
    from api.orders import place_order
    from config import set_thread_credentials
    data = request.json
    api_key, api_secret, pname, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected or keys not configured"), 400
    set_thread_credentials(api_key, api_secret, broker)

    legs = data.get('legs', [])
    max_profit = float(data.get('max_profit', 0))
    max_loss = float(data.get('max_loss', 0))

    results = []
    placed_legs = []
    for leg in legs:
        result = place_order(leg['product_id'], leg['symbol'], int(leg['size']), leg['side'])
        ok = result is not None
        results.append({'symbol': leg['symbol'], 'side': leg['side'], 'size': leg['size'], 'success': ok})
        if ok:
            placed_legs.append({
                'product_id': leg['product_id'], 'symbol': leg['symbol'],
                'type': leg.get('type', ''), 'strike': leg.get('strike', ''),
                'side': leg['side'], 'size': int(leg['size']),
                'entry_price': float(leg.get('mark', 0)),
            })
            position_tracker.open(current_user_id(), leg['product_id'], leg['symbol'],
                type=leg.get('type', ''), strike=leg.get('strike', ''),
                side=leg['side'], size=int(leg['size']),
                entry_price=float(leg.get('mark', 0)),
                asset=data.get('asset', 'BTC'), source='Option Chain')

    # Always track the strategy
    asset = data.get('asset', 'BTC')
    sid = str(uuid.uuid4())[:8]
    if placed_legs:
        leg_names = ', '.join(l['symbol'] for l in placed_legs[:3])
        track_strategy(sid, 'Option Chain', f"{asset} {leg_names}", current_user_id(),
                       details={'legs': placed_legs, 'max_profit': max_profit, 'max_loss': max_loss, 'asset': asset, 'profile_id': data.get('profile_id')})
        record_start(sid, {
            'asset': asset, 'source': 'Option Chain',
            'legs': len(placed_legs), 'max_profit': max_profit, 'max_loss': max_loss,
            'expiry_date': data.get('expiry', ''),
            'lot_size': placed_legs[0]['size'] if placed_legs else 0,
            'leg_details': ', '.join(f"{l['side'].upper()} {l.get('type','')} {l.get('strike','')}" for l in placed_legs[:4]),
        }, user_id=current_user_id())

    # Start monitor if targets are set and all orders succeeded
    monitor_id = None
    if max_profit > 0 and max_loss > 0 and placed_legs and all(r['success'] for r in results):
        from strategy.monitor import StrategyMonitor
        from config import get_contract_value
        mon = StrategyMonitor(
            legs=placed_legs, max_profit=max_profit, max_loss=max_loss,
            asset=asset, lot_size=get_contract_value(asset),
        )
        mon.user_id = current_user_id()
        mon.sid = sid
        mon.profile_id = data.get('profile_id')
        monitor_id = sid
        active_monitors[monitor_id] = {'monitor': mon, 'user_id': current_user_id(), 'profile_id': data.get('profile_id')}
        mon.on_complete = lambda pnl, reason: (update_tracked(sid, status='completed', pnl=round(pnl, 2)), record_end(sid, pnl, 0))
        mon.start()
    elif placed_legs and not (max_profit > 0 and max_loss > 0):
        # No monitor — mark as completed immediately (manual trade)
        update_tracked(sid, status='open (no monitor)')

    return jsonify(results=results, monitor_id=monitor_id)


@app.route('/api/positions')
@login_required
def api_positions():
    """Return open option positions with live mark prices."""
    import re
    from api.positions import get_positions
    from config import set_thread_credentials
    api_key, api_secret, _, broker = get_profile_creds(request.args.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    set_thread_credentials(api_key, api_secret, broker)

    positions = get_positions()

    # Fetch live tickers for mark prices
    mark_prices = {}
    try:
        import requests as req
        import config as cfg
        from auth import get_headers
        path = '/v2/tickers'
        qs = '?contract_types=call_options,put_options'
        headers = get_headers('GET', path, qs)
        resp = req.get(f'{cfg.BASE_URL}{path}{qs}', headers=headers, timeout=10)
        if resp.ok:
            for t in resp.json().get('result', []):
                mark_prices[t.get('product_id')] = float(t.get('mark_price', 0))
    except Exception:
        pass

    result = []
    for p in positions:
        size = int(p.get('size', 0))
        if size == 0:
            continue
        sym = p.get('product_symbol', '')
        m = re.match(r'^(C|P)-(\w+)-(\d+)-\d+$', sym)
        opt_type = 'call' if (m and m.group(1) == 'C') else 'put' if m else 'unknown'
        strike = m.group(3) if m else '0'
        side = 'sell' if size < 0 else 'buy'
        pid = p.get('product_id')
        entry = float(p.get('entry_price', 0))
        mark = mark_prices.get(pid, entry)
        # contract_value: BTC options = 0.001, ETH options = 0.01
        asset = m.group(2) if m else 'BTC'
        from config import get_contract_value
        cv = get_contract_value(asset)
        direction = 1 if side == 'buy' else -1
        pnl = direction * (mark - entry) * abs(size) * cv
        result.append({
            'symbol': sym, 'product_id': pid, 'type': opt_type,
            'strike': strike, 'side': side, 'size': abs(size),
            'entry_price': entry, 'mark_price': mark,
            'pnl': round(pnl, 2), 'asset': asset,
        })
    return jsonify(positions=result)



@app.route('/api/close-position', methods=['POST'])
@login_required
def api_close_position():
    """Close a single position leg."""
    from api.orders import place_order
    from config import set_thread_credentials
    data = request.json or {}
    for field in ('side', 'product_id', 'symbol', 'size'):
        if field not in data:
            return jsonify(error=f"Missing required field: {field}"), 400
    try:
        size = int(data['size'])
    except (ValueError, TypeError):
        return jsonify(error="size must be an integer"), 400
    if data['side'] not in ('buy', 'sell'):
        return jsonify(error="side must be 'buy' or 'sell'"), 400
    api_key, api_secret, _, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected"), 400
    set_thread_credentials(api_key, api_secret, broker)
    close_side = 'buy' if data['side'] == 'sell' else 'sell'
    result = place_order(data['product_id'], data['symbol'], size, close_side)
    if result is not None:
        position_tracker.close(current_user_id(), data['product_id'])
        # Also try closing on peer
        if PEER_PORT:
            try:
                import requests as req
                token = request.headers.get('Authorization', '').replace('Bearer ', '')
                req.post(f'http://127.0.0.1:{PEER_PORT}/api/close-position',
                         json=data, headers={'Authorization': f'Bearer {token}'}, timeout=5)
            except Exception:
                pass
    return jsonify(success=result is not None)

@app.route('/api/monitor/<mid>')
@login_required
def api_monitor_status(mid):
    entry = active_monitors.get(mid)
    if entry and entry['user_id'] == current_user_id():
        return jsonify(**entry['monitor'].get_status())
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/monitor/{mid}',
                        headers={'Authorization': f'Bearer {token}'}, timeout=3)
            if r.ok: return jsonify(r.json())
        except Exception: pass
    return jsonify(error="Not found"), 404


@app.route('/api/monitor/<mid>/stop', methods=['POST'])
@login_required
def api_monitor_stop(mid):
    entry = active_monitors.get(mid)
    if not entry or entry['user_id'] != current_user_id():
        if PEER_PORT:
            try:
                import requests as req
                token = request.headers.get('Authorization', '').replace('Bearer ', '')
                r = req.post(f'http://127.0.0.1:{PEER_PORT}/api/monitor/{mid}/stop',
                             headers={'Authorization': f'Bearer {token}'}, timeout=10)
                if r.ok: return jsonify(r.json())
            except Exception: pass
        return jsonify(error="Not found"), 404
    from config import set_thread_credentials
    api_key, api_secret, _, broker = get_profile_creds(entry.get('profile_id'))
    if api_key:
        set_thread_credentials(api_key, api_secret, broker)
    entry['monitor'].stop()
    return jsonify(status="stopped")


# ── Chart Routes ──

@app.route('/api/chart-data')
@login_required
def api_chart_data():
    from api.chart import get_candles, detect_structure, calc_indicators
    symbol = request.args.get('symbol', 'NIFTY')
    interval = request.args.get('interval', '1h')
    indicators = request.args.get('indicators', '').split(',') if request.args.get('indicators') else []
    candles = get_candles(symbol, interval)
    if not candles:
        return jsonify(error='Failed to fetch data'), 500
    structure = detect_structure(candles)
    ind_data = calc_indicators(candles, indicators) if indicators else {}
    return jsonify(candles=candles, indicators=ind_data, **structure)


# ── Strategy Builder Routes ──

@app.route('/api/strategy-builder/save', methods=['POST'])
@login_required
def api_save_strategy_builder():
    data = request.json
    data['user_id'] = current_user_id()
    sid = str(uuid.uuid4())[:8]
    saved = getattr(app, '_saved_strategies', {})
    saved[sid] = data
    app._saved_strategies = saved
    return jsonify(status='saved', sid=sid)


@app.route('/api/strategy-builder/deploy', methods=['POST'])
@login_required
@credits_required('deploy_builder')
def api_deploy_strategy_builder():
    from api.chain import get_option_chain_full, get_expiries
    from api.orders import place_order
    from config import set_thread_credentials
    from strategy.monitor import StrategyMonitor

    data = request.json
    api_key, api_secret, pname, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error="No API profile selected or keys not configured"), 400
    set_thread_credentials(api_key, api_secret, broker)

    asset = data.get('underlying', 'BTC')
    legs_cfg = data.get('legs', [])
    if not legs_cfg:
        return jsonify(error="No legs defined"), 400

    # Resolve expiry
    expiry_key = data.get('expiry', 'current_week')
    expiries = get_expiries(asset)
    if not expiries:
        return jsonify(error="Could not fetch expiries"), 500
    expiry_map = {'current_week': 0, 'next_week': 1, 'current_month': 0, 'next_month': 1}
    expiry = expiries[min(expiry_map.get(expiry_key, 0), len(expiries) - 1)] if expiry_key != 'custom' else expiries[0]

    # Fetch chain
    chain, spot, _ = get_option_chain_full(expiry, asset)
    if not chain or not spot:
        return jsonify(error="Failed to fetch option chain"), 500

    # Build sorted strike list and find ATM index
    strikes = [float(row['strike']) for row in chain]
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))

    # Resolve each leg to a real option
    import re
    results = []
    placed_legs = []
    lots_per_leg = int(data.get('execution', {}).get('lots', 1))
    for leg in legs_cfg:
        opt_type = 'call' if leg['type'] == 'CE' else 'put'
        strike_key = leg.get('strike', 'ATM')
        m = re.match(r'(ATM|OTM|ITM)(\d*)', strike_key)
        offset = 0
        if m:
            offset = int(m.group(2)) if m.group(2) else 0
            if m.group(1) == 'OTM':
                offset = offset if opt_type == 'call' else -offset
            elif m.group(1) == 'ITM':
                offset = -offset if opt_type == 'call' else offset
        idx = max(0, min(atm_idx + offset, len(chain) - 1))
        opt = chain[idx].get(opt_type)
        if not opt or not opt.get('product_id'):
            results.append({'strike': strike_key, 'type': leg['type'], 'success': False, 'error': 'No option found'})
            continue

        size = int(leg.get('lots', 1)) * lots_per_leg
        order = place_order(opt['product_id'], opt['symbol'], size, leg['side'])
        ok = order is not None
        results.append({'symbol': opt['symbol'], 'side': leg['side'], 'size': size, 'success': ok})
        if ok:
            placed_legs.append({
                'product_id': opt['product_id'], 'symbol': opt['symbol'],
                'type': leg['type'], 'strike': opt['strike'],
                'side': leg['side'], 'size': size,
                'entry_price': float(opt.get('mark_price', 0)),
            })
            position_tracker.open(current_user_id(), opt['product_id'], opt['symbol'],
                type=leg['type'], strike=opt['strike'],
                side=leg['side'], size=size,
                entry_price=float(opt.get('mark_price', 0)),
                asset=asset, source='Strategy Builder')

    if not placed_legs:
        return jsonify(error="All orders failed", results=results), 500

    sid = str(uuid.uuid4())[:8]
    data['legs'] = placed_legs  # overwrite abstract config with actual placed legs (with product_id)
    record_start(sid, {
        'asset': asset, 'source': 'Strategy Builder',
        'name': data.get('name', 'Unnamed'), 'legs': len(placed_legs),
        'expiry_date': expiry,
        'lot_size': lots_per_leg,
        'leg_details': ', '.join(f"{l['side'].upper()} {l.get('type','')} {l.get('strike','')}" for l in placed_legs[:4]),
    }, user_id=current_user_id())

    # Start monitor if risk targets are set
    risk = data.get('risk', {})
    sl_pct = float(risk.get('sl_pct', 0))
    tgt_pct = float(risk.get('target_pct', 0))
    total_premium = sum(l['entry_price'] * l['size'] for l in placed_legs if l['side'] == 'sell')
    lot_sizes = {'BTC': 0.001, 'ETH': 0.01}
    lot_size = lot_sizes.get(asset, 0.001)
    max_profit = total_premium * lot_size * tgt_pct / 100 if tgt_pct else 0
    max_loss = total_premium * lot_size * sl_pct / 100 if sl_pct else 0

    data['max_profit'] = max_profit
    data['max_loss'] = max_loss
    data['asset'] = asset
    data['lot_size'] = lot_size
    track_strategy(sid, 'Strategy Builder', data.get('name', 'Unnamed'), current_user_id(), details=data)

    monitor_id = None
    if max_profit > 0 and max_loss > 0:
        mon = StrategyMonitor(
            legs=placed_legs, max_profit=max_profit, max_loss=max_loss,
            asset=asset, lot_size=lot_size,
        )
        mon.user_id = current_user_id()
        mon.sid = sid
        mon.profile_id = data.get('profile_id')
        monitor_id = sid
        active_monitors[monitor_id] = {'monitor': mon, 'user_id': current_user_id(), 'profile_id': data.get('profile_id')}
        mon.on_complete = lambda pnl, reason: (update_tracked(sid, status='completed', pnl=round(pnl, 2)), record_end(sid, pnl, 0))
        mon.start()
        update_tracked(sid, status='running')
    else:
        update_tracked(sid, status='open (no monitor)')

    return jsonify(status='deployed', sid=sid, results=results, monitor_id=monitor_id)


@app.route('/api/strategy-builder/paper-trade', methods=['POST'])
@login_required
@credits_required('paper_trade')
def api_paper_trade_strategy_builder():
    data = request.json
    sid = str(uuid.uuid4())[:8]
    track_strategy(sid, 'Strategy Builder (Paper)', data.get('name', 'Unnamed'), current_user_id(), details=data)
    update_tracked(sid, status='paper')
    return jsonify(status='paper', sid=sid)


# ── Credits API ──

@app.route('/api/credits')
@login_required
def api_credits():
    creds = get_user_credits(current_user_id())
    return jsonify(creds or {})


@app.route('/api/credits/history')
@login_required
def api_credits_history():
    return jsonify(history=get_credit_history(current_user_id()))


@app.route('/api/credits/costs')
@login_required
def api_credit_costs():
    return jsonify(costs=CREDIT_COSTS)


# ── Admin Routes ──

@app.route('/api/admin/users')
@login_required
@admin_required
def api_admin_users():
    return jsonify(users=get_all_users())


@app.route('/api/admin/stats')
@login_required
@admin_required
def api_admin_stats():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    creds = conn.execute('SELECT COALESCE(SUM(credits_remaining),0), COALESCE(SUM(credits_used),0) FROM user_credits').fetchone()
    plans_count = conn.execute('SELECT COUNT(*) FROM plans').fetchone()[0]
    conn.close()
    return jsonify(total_users=total, credits_available=creds[0], credits_used=creds[1], plans_count=plans_count)


@app.route('/api/admin/plans')
@login_required
@admin_required
def api_admin_plans():
    return jsonify(plans=get_all_plans())


@app.route('/api/admin/add-credits', methods=['POST'])
@login_required
@admin_required
def api_admin_add_credits():
    d = request.json
    uid = d.get('user_id')
    amount = int(d.get('amount', 0))
    desc = d.get('description', 'Admin grant')
    if not uid or amount == 0:
        return jsonify(error='user_id and amount required'), 400
    add_credits(uid, amount, desc)
    return jsonify(status='ok')


@app.route('/api/admin/set-plan', methods=['POST'])
@login_required
@admin_required
def api_admin_set_plan():
    d = request.json
    if not set_user_plan(d.get('user_id'), d.get('plan_id')):
        return jsonify(error='Invalid plan'), 400
    return jsonify(status='ok')


@app.route('/api/admin/set-admin', methods=['POST'])
@login_required
@admin_required
def api_admin_set_admin():
    d = request.json
    set_admin(d.get('user_id'), d.get('is_admin', False))
    return jsonify(status='ok')


@app.route('/api/admin/enabled-strategies')
@login_required
@admin_required
def api_admin_get_enabled():
    from models import get_setting
    import json as _json
    val = get_setting('enabled_strategies')
    return jsonify(enabled=_json.loads(val) if val else None)


@app.route('/api/admin/enabled-strategies', methods=['POST'])
@login_required
@admin_required
def api_admin_set_enabled():
    from models import set_setting
    import json as _json
    enabled = request.json.get('enabled')
    set_setting('enabled_strategies', _json.dumps(enabled))
    return jsonify(status='ok')


@app.route('/api/enabled-strategies')
def api_public_enabled():
    from models import get_setting
    import json as _json
    val = get_setting('enabled_strategies')
    return jsonify(enabled=_json.loads(val) if val else None)


@app.route('/api/admin/user-history/<int:uid>')
@login_required
@admin_required
def api_admin_user_history(uid):
    return jsonify(history=get_credit_history(uid, 100))


# ── Unified Strategy Tracker API ──

@app.route('/api/tracked-positions')
@login_required
def api_tracked_positions():
    """Return all positions — from broker + position tracker, deduplicated."""
    from api.live_pnl import compute_live_legs
    uid = current_user_id()

    # 1. Get positions from broker API if profile provided (and reconcile)
    broker_positions = []
    profile_id = request.args.get('profile_id', '')
    if profile_id:
        import re
        from api.positions import get_positions
        from config import set_thread_credentials
        api_key, api_secret, _, broker = get_profile_creds(profile_id)
        if api_key:
            set_thread_credentials(api_key, api_secret, broker)
            positions = get_positions()
            # Reconcile: remove tracked positions that broker says are closed
            if positions is not None:
                position_tracker.reconcile_with_broker(uid, positions)
            for p in (positions or []):
                size = int(p.get('size', 0))
                if size == 0:
                    continue
                sym = p.get('product_symbol', '')
                m = re.match(r'^(C|P)-(\w+)-(\d+)-\d+$', sym)
                if m:
                    opt_type = 'call' if m.group(1) == 'C' else 'put'
                    strike = m.group(3)
                    asset = m.group(2)
                else:
                    opt_type = 'futures'
                    strike = '0'
                    asset = 'BTC' if 'BTC' in sym else 'ETH' if 'ETH' in sym else 'BTC'
                side = 'sell' if size < 0 else 'buy'
                pid = p.get('product_id')
                entry = float(p.get('entry_price', 0))
                broker_positions.append({
                    'product_id': pid, 'symbol': sym, 'type': opt_type,
                    'strike': strike, 'side': side, 'size': abs(size),
                    'entry_price': entry, 'asset': asset, 'source': 'Broker',
                })

    # 2. Get positions from tracker (after reconciliation removed stale ones)
    tracked = position_tracker.to_list(uid, refresh=True)

    # 3. Merge: broker positions + tracked (dedup by product_id)
    seen = set()
    merged = []
    for p in tracked:
        if p.get('product_id'):
            seen.add(p['product_id'])
        merged.append(p)
    for p in broker_positions:
        if p.get('product_id') not in seen:
            merged.append(p)

    # 3b. Merge positions from peer (old) instance
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/tracked-positions',
                        headers={'Authorization': f'Bearer {token}'}, params={'profile_id': profile_id}, timeout=3)
            if r.ok:
                for p in r.json().get('positions', []):
                    pid = p.get('product_id')
                    if pid and pid not in seen:
                        seen.add(pid)
                        p['_peer'] = True
                        merged.append(p)
        except Exception:
            pass

    # 4. Compute live prices for all
    if merged:
        from api.pricing import get_current_price
        for p in merged:
            if p.get('current_mark') and p.get('current_pnl') is not None:
                continue  # already has live data from tracker
            pid = p.get('product_id')
            asset = p.get('asset', 'BTC')
            if pid:
                try:
                    data = get_current_price(pid, asset)
                    if data and data.get('mark_price'):
                        mark = float(data['mark_price'])
                        entry = float(p.get('entry_price', 0))
                        lot_size = 0.01 if asset == 'ETH' else 0.001
                        d = 1 if p.get('side') == 'buy' else -1
                        p['current_mark'] = round(mark, 2)
                        p['mark_price'] = round(mark, 2)
                        p['current_pnl'] = round(d * (mark - entry) * int(p.get('size', 0)) * lot_size, 2)
                        p['pnl'] = p['current_pnl']
                except Exception:
                    pass

    total_pnl = sum(p.get('current_pnl') or p.get('pnl') or 0 for p in merged)
    return jsonify(positions=merged, total_pnl=round(total_pnl, 2))

@app.route('/api/tracker/strategies')
@login_required
def api_tracker_list():
    return jsonify(strategies=registry.all_statuses(current_user_id()))

@app.route('/api/tracker/<sid>')
@login_required
def api_tracker_detail(sid):
    s = registry.get(sid)
    if s and s.user_id == current_user_id():
        return jsonify(**s.get_status())
    # Fallback: old strategies dict
    e = strategies.get(sid)
    if e and e.get('user_id') == current_user_id():
        strat = e.get('strategy')
        return jsonify(sid=sid, source='AlgoX DN', name=e.get('params', {}).get('asset', 'BTC'),
            user_id=e['user_id'], status='running' if e.get('running') else 'completed',
            running=e.get('running', False), pnl=round(strat.total_pnl, 2) if strat else 0,
            legs=[], logs=[], details=e.get('params', {}))
    return jsonify(error='Not found'), 404

@app.route('/api/tracker/<sid>/logs')
@login_required
def api_tracker_logs(sid):
    last = int(request.args.get('last', 100))
    # Check unified tracker first
    s = registry.get(sid)
    if s and s.user_id == current_user_id():
        return jsonify(sid=sid, logs=s.get_logs(last), running=s.running, pnl=s.current_pnl, status=s.status)
    # Fallback: check old strategies dict (Delta Neutral strategies)
    e = strategies.get(sid)
    if e and e.get('user_id') == current_user_id():
        logs = list(e.get('log_history', []))
        strat = e.get('strategy')
        pnl = round(strat.total_pnl, 2) if strat else 0
        return jsonify(sid=sid, logs=logs[-last:], running=e.get('running', False), pnl=pnl, status='running' if e.get('running') else 'completed')
    # Check new strategy dicts
    for dct in (iv_crush_strategies, call_ratio_strategies, oi_strategies, weekly_dn_strategies, ema_spread_strategies, strangle_strategies, portfolio_strangle_strategies, hybrid_strategies):
        e = dct.get(sid)
        if e and e.get('user_id') == current_user_id():
            logs = list(e.get('log_history', []))
            return jsonify(sid=sid, logs=logs[-last:], running=e.get('running', False), pnl=0, status='running' if e.get('running') else 'completed')
    # Check active monitors (Option Chain / Strategy Builder)
    m = active_monitors.get(sid)
    if m and m.get('user_id') == current_user_id():
        mon = m['monitor']
        st = mon.get_status()
        return jsonify(sid=sid, logs=st.get('logs', [])[-last:], running=mon.running, pnl=round(mon.current_pnl, 2), status='running' if mon.running else 'completed')
    # Proxy to peer
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.get(f'http://127.0.0.1:{PEER_PORT}/api/tracker/{sid}/logs',
                        headers={'Authorization': f'Bearer {token}'}, params={'last': last}, timeout=3)
            if r.ok:
                return jsonify(r.json())
        except Exception:
            pass
    return jsonify(error='Not found'), 404

@app.route('/api/tracker/<sid>/close', methods=['POST'])
@login_required
def api_tracker_close(sid):
    s = registry.get(sid)
    if s and s.user_id == current_user_id():
        s.close()
        return jsonify(status='closed', pnl=s.current_pnl)
    # Fallback: old strategies dict
    e = strategies.get(sid)
    if e and e.get('user_id') == current_user_id() and e.get('strategy'):
        e['strategy'].running = False
        e['strategy'].close_all_positions()
        return jsonify(status='closed')
    # New strategy dicts
    for dct in (iv_crush_strategies, call_ratio_strategies, oi_strategies, weekly_dn_strategies, ema_spread_strategies, strangle_strategies, portfolio_strangle_strategies, hybrid_strategies):
        e = dct.get(sid)
        if e and e.get('user_id') == current_user_id() and e.get('strategy'):
            e['strategy'].close_all()
            return jsonify(status='closed')
    # Proxy to peer
    if PEER_PORT:
        try:
            import requests as req
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            r = req.post(f'http://127.0.0.1:{PEER_PORT}/api/tracker/{sid}/close',
                         headers={'Authorization': f'Bearer {token}'}, timeout=10)
            if r.ok:
                return jsonify(r.json())
        except Exception:
            pass
    return jsonify(error='Not found'), 404

@app.route('/api/tracker/close-all', methods=['POST'])
@login_required
def api_tracker_close_all():
    count = registry.close_all(current_user_id())
    return jsonify(closed=count)

@app.route('/api/tracker/deploy', methods=['POST'])
@login_required
def api_tracker_deploy():
    """Create and start monitoring a strategy from any source."""
    from config import set_thread_credentials
    data = request.json or {}
    api_key, api_secret, _, broker = get_profile_creds(data.get('profile_id'))
    if not api_key:
        return jsonify(error='No API profile selected'), 400
    set_thread_credentials(api_key, api_secret, broker)

    lot_sizes = {'BTC': 0.001, 'ETH': 0.01}
    asset = data.get('asset', 'BTC')

    strat = TrackedStrategy(
        source=data.get('source', 'Manual'),
        name=data.get('name', f"{asset} Strategy"),
        user_id=current_user_id(),
        legs=data.get('legs', []),
        asset=asset,
        lot_size=lot_sizes.get(asset, 0.001),
        max_profit=float(data.get('max_profit', 0)),
        max_loss=float(data.get('max_loss', 0)),
        profile_id=data.get('profile_id'),
        interval=int(data.get('interval', 10)),
        details=data.get('details', {}),
    )

    def on_done(pnl, reason):
        update_tracked(strat.sid, status='completed', pnl=round(pnl, 2))
        record_end(strat.sid, pnl, strat.adjustment_count)

    strat.on_complete = on_done
    registry.register(strat)
    track_strategy(strat.sid, strat.source, strat.name, current_user_id(), details=strat.details)
    record_start(strat.sid, data, user_id=current_user_id())
    strat.start_monitoring()

    return jsonify(sid=strat.sid, status='running')


# ── Futures Signal Auto-Trade ──



@app.route('/api/futures-signal/start', methods=['POST'])
@login_required
def futures_signal_start():
    from strategy.futures_signal_trader import FuturesSignalTrader

    params = request.json or {}
    profile_id = params.get('profile_id')
    api_key, api_secret, _, broker = get_profile_creds(profile_id)
    if not api_key:
        return jsonify(error="No API profile selected"), 400

    signal_key = params.get('signal_key')
    if not signal_key:
        return jsonify(error="No signal_key provided"), 400

    sid = str(uuid.uuid4())[:8]
    trader = FuturesSignalTrader(
        signal_key=signal_key,
        asset=params.get('asset', 'BTC'),
        timeframe=params.get('timeframe', '15m'),
        lots=int(params.get('lots', 1)),
        scan_interval=int(params.get('scan_interval', 60)),
        max_trades_per_day=int(params.get('max_trades_per_day', 3)),
        api_key=api_key, api_secret=api_secret, broker=broker,
        profile_id=profile_id,
    )
    trader.sid = sid
    trader.start()

    _futures_traders[sid] = {'trader': trader, 'user_id': current_user_id()}
    track_strategy(sid, 'Futures Signal', f"{params.get('asset','BTC')} {signal_key} {params.get('timeframe','15m')}", current_user_id(), details=params)

    return jsonify(status='started', sid=sid)


@app.route('/api/futures-signal/stop', methods=['POST'])
@login_required
def futures_signal_stop():
    sid = (request.json or {}).get('sid')
    entry = _futures_traders.get(sid)
    if not entry or entry['user_id'] != current_user_id():
        return jsonify(error="Not found"), 404
    entry['trader'].stop()
    return jsonify(status='stopped')


@app.route('/api/futures-signal/status')
@login_required
def futures_signal_status():
    user_id = current_user_id()
    active = []
    for sid, entry in _futures_traders.items():
        if entry['user_id'] == user_id:
            active.append({'sid': sid, **entry['trader'].status})
    return jsonify(traders=active)


@app.route('/api/futures-signal/logs/<sid>')
@login_required
def futures_signal_logs(sid):
    entry = _futures_traders.get(sid)
    if not entry or entry['user_id'] != current_user_id():
        return jsonify(logs=[], running=False)
    trader = entry['trader']
    return jsonify(logs=trader.trade_log[-20:], running=trader.running,
                   scan_count=trader._scan_count, trades_today=trader.trades_today)


@app.route('/api/futures-signal/stream/<sid>')
@login_required
def futures_signal_stream(sid):
    import queue as _q
    entry = _futures_traders.get(sid)
    if not entry or entry['user_id'] != current_user_id():
        return Response("data: Not found\n\n", mimetype='text/event-stream')
    trader = entry['trader']

    def generate():
        last_count = 0
        while trader.running:
            sc = trader._scan_count
            if sc > last_count:
                last_count = sc
                msg = f"[Scan #{sc}] {trader.signal_key} {trader.asset} {trader.timeframe} | Trades: {trader.trades_today}/{trader.max_trades_per_day}"
                if trader.trade_log:
                    t = trader.trade_log[-1]
                    msg += f" | Last: {t['side'].upper()} @ {t['price']} {'✓' if t['success'] else '✗'}"
                yield f"data: {msg}\n\n"
            time.sleep(5)
        yield f"event: stopped\ndata: done\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── Serve React Frontend (catch-all — must be last) ──

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_react(path):
    dist = os.path.join(app.root_path, 'frontend', 'dist')
    if path and os.path.exists(os.path.join(dist, path)):
        return send_from_directory(dist, path)
    return send_from_directory(dist, 'index.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
