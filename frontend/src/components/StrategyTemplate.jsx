import React, { useState, useEffect, useRef } from 'react';
import api from '../api';

/**
 * Reusable strategy template for any strategy type.
 *
 * <StrategyTemplate
 *   title="Delta Neutral"
 *   icon="⚡"
 *   type="Options"
 *   description="..."
 *   profiles={profiles}
 *   configFields={[{key, label, type, default, options?, hint?}]}
 *   onStart={(config) => Promise}     // returns {sid}
 *   onStop={(sid) => Promise}
 *   statusEndpoint="/status"          // polled for live data
 *   streamEndpoint="/stream"          // SSE for logs
 *   renderStatus={(status) => JSX}    // custom status display
 * />
 *
 * OR for signal-based strategies:
 *
 * <StrategyTemplate
 *   signalMode
 *   signalKey="rsi_div_mss"
 *   title="Div + MSS"
 *   ...
 * />
 */
export default function StrategyTemplate({
  title, icon, type, description, profiles = [],
  // Config mode (Delta Neutral)
  configFields = [], onStart, onStop, statusEndpoint, streamEndpoint, renderStatus,
  // Signal mode (Div+MSS, SMA+Vol)
  signalMode = false, signalKey = '',
  // Common
  defaultAsset = 'BTC', timeframes = ['15m', '1h', '4h', '1d'],
}) {
  const [asset, setAsset] = useState(defaultAsset);
  const [profileId, setProfileId] = useState('');
  const [error, setError] = useState('');

  useEffect(() => { if (profiles.length && !profileId) setProfileId(profiles[0].id); }, [profiles]);

  return (
    <div className="card">
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: '1rem' }}>{icon} {title}</div>
          <div style={{ fontSize: '.78rem', color: 'var(--muted)', marginTop: 2 }}>
            <span style={{ padding: '1px 8px', borderRadius: 4, fontSize: '.68rem', fontWeight: 600, background: type === 'Options' ? '#ede9fe' : '#fef3c7', color: type === 'Options' ? '#6366f1' : '#d97706', marginRight: 6 }}>{type}</span>
            {description}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {/* Asset Toggle */}
          <div style={{ display: 'flex', borderRadius: 8, overflow: 'hidden', border: '1px solid var(--border)' }}>
            {['BTC', 'ETH'].map(a => (
              <button key={a} onClick={() => setAsset(a)} style={{ padding: '6px 16px', border: 'none', fontSize: '.82rem', fontWeight: 700, cursor: 'pointer', background: asset === a ? 'var(--accent)' : 'var(--card)', color: asset === a ? '#fff' : 'var(--muted)', transition: 'all .15s' }}>
                {a === 'BTC' ? '₿ BTC' : '⟠ ETH'}
              </button>
            ))}
          </div>
          {/* Profile */}
          <select value={profileId} onChange={e => setProfileId(e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)' }}>
            <option value="">Default Keys</option>
            {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>
      </div>

      {error && <div className="error-msg">{error}</div>}

      {/* Render mode */}
      {signalMode ? (
        <SignalPanel asset={asset} profileId={profileId} signalKey={signalKey} timeframes={timeframes} profiles={profiles} setError={setError} title={title} />
      ) : (
        <ConfigPanel asset={asset} profileId={profileId} configFields={configFields} onStart={onStart} onStop={onStop} statusEndpoint={statusEndpoint} streamEndpoint={streamEndpoint} renderStatus={renderStatus} profiles={profiles} setError={setError} />
      )}
    </div>
  );
}

// ── Signal-based strategy panel ──

function SignalPanel({ asset, profileId, signalKey, timeframes, setError, title }) {
  const [tf, setTf] = useState('1h');
  const [lots, setLots] = useState(10);
  const [signals, setSignals] = useState([]);
  const [backtest, setBacktest] = useState(null);
  const [loading, setLoading] = useState(false);
  const [deployed, setDeployed] = useState(false);
  const [strongOnly, setStrongOnly] = useState(false);
  const [liveSid, setLiveSid] = useState(null);
  const [liveStatus, setLiveStatus] = useState(null);
  const pollRef = React.useRef(null);

  const lotSize = asset === 'BTC' ? 0.001 : 0.01;

  const fetchSignals = () => {
    setLoading(true); setBacktest(null);
    api.get('/chart-data', { params: { symbol: asset, interval: tf, indicators: signalKey } })
      .then(r => setSignals((r.data?.indicators?.[signalKey]?.signals || []).slice(-10).reverse()))
      .catch(() => setSignals([]))
      .finally(() => setLoading(false));
  };

  useEffect(fetchSignals, [asset, tf, signalKey]);

  const runBacktest = () => {
    setLoading(true);
    api.get('/chart-data', { params: { symbol: asset, interval: tf, indicators: signalKey } })
      .then(r => {
        const allSigs = r.data?.indicators?.[signalKey]?.signals || [];
        const sigs = strongOnly && signalKey === 'sma_vol_breakout' ? allSigs.filter(s => s.strength === 'strong') : allSigs;
        const candles = r.data?.candles || [];
        let totalPnl = 0, wins = 0, losses = 0;
        const trades = sigs.map(s => {
          const isBuy = s.type === 'buy';
          const si = candles.findIndex(c => c.t >= s.time);
          let outcome = 'open', pnl = 0;
          if (si >= 0) for (let i = si + 1; i < candles.length; i++) {
            const c = candles[i];
            if (isBuy) { if (c.l <= s.sl) { outcome = 'sl'; pnl = -(s.price - s.sl) * lots * lotSize; break; } if (c.h >= s.tp1) { outcome = 'tp'; pnl = (s.tp1 - s.price) * lots * lotSize; break; } }
            else { if (c.h >= s.sl) { outcome = 'sl'; pnl = -(s.sl - s.price) * lots * lotSize; break; } if (c.l <= s.tp1) { outcome = 'tp'; pnl = (s.price - s.tp1) * lots * lotSize; break; } }
          }
          pnl = parseFloat(pnl.toFixed(2)); totalPnl += pnl;
          if (outcome === 'tp') wins++; else if (outcome === 'sl') losses++;
          return { ...s, pnl, outcome };
        });
        setBacktest({ totalPnl: parseFloat(totalPnl.toFixed(2)), wins, losses, total: sigs.length, open: sigs.length - wins - losses, trades });
      }).catch(() => {}).finally(() => setLoading(false));
  };

  const deploy = () => {
    if (!window.confirm(`Deploy ${title} ${asset} ${tf} with ${lots} lots?`)) return;
    api.post('/futures-signal/start', { signal_key: signalKey, asset, timeframe: tf, lots, scan_interval: 30, max_trades_per_day: 3, profile_id: profileId })
      .then(r => {
        const sid = r.data?.sid;
        setDeployed(true); setLiveSid(sid);
        if (sid) {
          pollRef.current = setInterval(() => {
            api.get(`/futures-signal/logs/${sid}`).then(r => setLiveStatus(r.data)).catch(() => {});
          }, 5000);
        }
      })
      .catch(e => setError(e.response?.data?.error || 'Deploy failed'));
  };

  const stopTrader = () => {
    if (!liveSid) return;
    api.post('/futures-signal/stop', { sid: liveSid }).then(() => {
      setDeployed(false); setLiveSid(null); setLiveStatus(null);
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    });
  };

  React.useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const closed = (backtest?.wins || 0) + (backtest?.losses || 0);

  return (
    <>
      {/* Controls */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
        <div style={{ display: 'flex', borderRadius: 6, overflow: 'hidden', border: '1px solid var(--border)' }}>
          {timeframes.map(t => <button key={t} onClick={() => setTf(t)} style={{ padding: '5px 14px', border: 'none', fontSize: '.78rem', fontWeight: 600, cursor: 'pointer', background: tf === t ? 'var(--accent)' : 'var(--card)', color: tf === t ? '#fff' : 'var(--muted)' }}>{t.toUpperCase()}</button>)}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <label style={{ fontSize: '.72rem', color: 'var(--muted)' }}>Lots</label>
          <input type="number" min={1} value={lots} onChange={e => setLots(Math.max(1, +e.target.value))} style={{ width: 65, padding: '5px 8px', border: '1px solid var(--border)', borderRadius: 6, textAlign: 'center', background: 'var(--bg)' }} />
          <span style={{ fontSize: '.65rem', color: 'var(--muted)' }}>= {(lots * lotSize).toFixed(3)} {asset}</span>
        </div>
      </div>

      {/* Actions */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 14, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="btn" style={{ background: '#f59e0b', color: '#fff' }} onClick={runBacktest} disabled={loading}>{loading ? '⏳...' : '📝 Backtest'}</button>
        <button className="btn btn-green" onClick={deploy} disabled={loading || deployed}>{deployed ? '✅ Running' : '🚀 Deploy Live'}</button>
        {deployed && <button className="btn" style={{ background: '#ef4444', color: '#fff' }} onClick={stopTrader}>⏹ Stop</button>}
        {signalKey === 'sma_vol_breakout' && <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '.8rem', cursor: 'pointer' }}><input type="checkbox" checked={strongOnly} onChange={e => setStrongOnly(e.target.checked)} /> Strong only</label>}
      </div>

      {/* Live Status */}
      {deployed && liveStatus && (
        <div style={{ background: '#0a0a0a', border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 12, fontFamily: 'monospace', fontSize: '.75rem' }}>
          <div style={{ fontWeight: 700, marginBottom: 8, color: '#22c55e' }}>🟢 LIVE — {signalKey} {asset} {tf} | Scans: {liveStatus.scan_count || 0} | Trades: {liveStatus.trades_today || 0}/3</div>
          {(liveStatus.logs || []).length > 0 ? liveStatus.logs.slice(-5).map((t, i) => (
            <div key={i} style={{ padding: '3px 0', borderBottom: '1px solid #222', color: t.success ? '#22c55e' : '#ef4444' }}>
              {t.time} | {t.side?.toUpperCase()} @ {t.price} | SL: {t.sl} | TP: {t.tp} | {t.success ? '✓ Filled' : '✗ Failed'}
            </div>
          )) : <div style={{ color: '#666' }}>Scanning for signals... no trades yet</div>}
        </div>
      )}

      {/* Backtest */}
      {backtest && (
        <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: 14, marginBottom: 12 }}>
          <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 10 }}>📈 Backtest — {asset} {tf.toUpperCase()} · {lots} lots</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 8, marginBottom: 10 }}>
            {[['Total', backtest.total, 'var(--text)'], ['Wins', backtest.wins, 'var(--green)'], ['Losses', backtest.losses, 'var(--red)'], ['Open', backtest.open, '#f59e0b'], ['Win Rate', closed ? `${(backtest.wins / closed * 100).toFixed(0)}%` : '—', 'var(--text)'], ['P&L', `$${backtest.totalPnl.toFixed(2)}`, backtest.totalPnl >= 0 ? 'var(--green)' : 'var(--red)']].map(([k, v, c]) => (
              <div key={k} style={{ textAlign: 'center' }}><div style={{ fontSize: '.62rem', color: 'var(--muted)', textTransform: 'uppercase' }}>{k}</div><div style={{ fontSize: '.95rem', fontWeight: 800, color: c }}>{v}</div></div>
            ))}
          </div>
          <details><summary style={{ cursor: 'pointer', fontSize: '.78rem', color: 'var(--muted)' }}>Show {backtest.total} trades</summary>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.78rem', marginTop: 8 }}>
              <thead><tr>{['Signal', 'Entry', 'SL', 'TP', 'Result', 'P&L'].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
              <tbody>{backtest.trades.map((t, i) => (
                <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '4px 8px' }}><span className={`badge ${t.type === 'buy' ? 'badge-green' : 'badge-red'}`}>{t.type === 'buy' ? '▲' : '▼'}</span></td>
                  <td style={{ padding: '4px 8px' }}>${t.price.toFixed(2)}</td>
                  <td style={{ padding: '4px 8px', color: 'var(--red)' }}>${t.sl.toFixed(2)}</td>
                  <td style={{ padding: '4px 8px', color: 'var(--green)' }}>${t.tp1.toFixed(2)}</td>
                  <td style={{ padding: '4px 8px' }}><span className={`badge ${t.outcome === 'tp' ? 'badge-green' : t.outcome === 'sl' ? 'badge-red' : 'badge-yellow'}`}>{t.outcome === 'tp' ? '✓ TP' : t.outcome === 'sl' ? '✗ SL' : '⏳'}</span></td>
                  <td style={{ padding: '4px 8px', fontWeight: 700, color: t.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${t.pnl.toFixed(2)}</td>
                </tr>))}</tbody>
            </table>
          </details>
        </div>
      )}

      {/* Signals */}
      <div style={{ fontSize: '.78rem', fontWeight: 600, color: 'var(--muted)', marginBottom: 6 }}>Recent Signals</div>
      {loading ? <div style={{ color: 'var(--muted)', padding: 8 }}>Loading...</div> : signals.length === 0 ? <div style={{ color: 'var(--muted)', padding: 8 }}>No signals</div> : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
          <thead><tr>{['Signal', 'Price', 'SL', 'TP', 'R:R', ...(signalKey === 'sma_vol_breakout' ? ['Setup', 'Vol'] : []), 'Risk ($)'].map(h => <th key={h} style={{ textAlign: 'left', padding: '6px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
          <tbody>{signals.map((s, i) => {
            const risk = Math.abs(s.price - s.sl) * lots * lotSize;
            const reward = Math.abs(s.tp1 - s.price) * lots * lotSize;
            const rr = risk > 0 ? (reward / risk).toFixed(2) : '—';
            const dt = new Date(s.time * 1000);
            return (<tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
              <td style={{ padding: '6px 8px' }}><span className={`badge ${s.type === 'buy' ? 'badge-green' : 'badge-red'}`}>{s.type === 'buy' ? '▲ BUY' : '▼ SELL'}</span><div style={{ fontSize: '.65rem', color: 'var(--muted)' }}>{dt.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })} {dt.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}</div></td>
              <td style={{ padding: '6px 8px', fontWeight: 700 }}>${s.price.toFixed(2)}</td>
              <td style={{ padding: '6px 8px', color: 'var(--red)' }}>${s.sl.toFixed(2)}</td>
              <td style={{ padding: '6px 8px', color: 'var(--green)' }}>${s.tp1.toFixed(2)}</td>
              <td style={{ padding: '6px 8px', fontWeight: 600 }}>1:{rr}</td>
              {signalKey === 'sma_vol_breakout' && <><td style={{ padding: '6px 8px' }}><span className={`badge ${s.setup === 1 ? 'badge-green' : 'badge-yellow'}`}>{s.setup === 1 ? 'S1' : 'S2'}</span></td><td style={{ padding: '6px 8px' }}>{s.vol_ratio}x</td></>}
              <td style={{ padding: '6px 8px', color: 'var(--red)', fontWeight: 600 }}>${risk.toFixed(2)}</td>
            </tr>);
          })}</tbody>
        </table>
      )}
    </>
  );
}

// ── Config-based strategy panel (Delta Neutral etc.) ──

function ConfigPanel({ asset, profileId, configFields, onStart, onStop, statusEndpoint, streamEndpoint, renderStatus, setError }) {
  const [form, setForm] = useState(() => {
    const f = {};
    configFields.forEach(c => { f[c.key] = c.default ?? ''; });
    return f;
  });
  const [sid, setSid] = useState(null);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState(null);
  const [logs, setLogs] = useState([]);
  const logRef = useRef(null);
  const esRef = useRef(null);
  const pollRef = useRef(null);

  useEffect(() => () => { esRef.current?.close(); clearInterval(pollRef.current); }, []);

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  const start = async () => {
    setError(''); setLogs([]);
    try {
      const config = { ...form, asset, profile_id: profileId };
      const result = await onStart(config);
      if (result?.error) { setError(result.error); return; }
      const id = result?.sid;
      setSid(id); setRunning(true);
      // Stream
      if (streamEndpoint) {
        esRef.current?.close();
        const es = new EventSource(`/api${streamEndpoint}/${id}?token=${localStorage.getItem('token')}`);
        es.onmessage = e => { setLogs(l => [...l, e.data]); if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; };
        esRef.current = es;
      }
      // Poll
      if (statusEndpoint) {
        clearInterval(pollRef.current);
        pollRef.current = setInterval(async () => {
          try { const { data } = await api.get(`${statusEndpoint}/${id}`); setStatus(data); if (!data.running) { setRunning(false); clearInterval(pollRef.current); esRef.current?.close(); } } catch {}
        }, 3000);
      }
    } catch (e) { setError(e.response?.data?.error || 'Failed to start'); }
  };

  const stop = async () => {
    if (!sid) return;
    await onStop(sid);
    setRunning(false); esRef.current?.close(); clearInterval(pollRef.current);
  };

  return (
    <>
      {/* Config Fields */}
      <div className="grid-3" style={{ gap: 10, marginBottom: 12 }}>
        {configFields.map(f => (
          <div className="field" key={f.key}>
            <label>{f.label}</label>
            {f.options ? (
              <select value={form[f.key]} onChange={e => set(f.key, e.target.value)}>
                {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            ) : (
              <input type={f.type || 'text'} step={f.step} value={form[f.key]} onChange={e => set(f.key, f.type === 'number' ? +e.target.value : e.target.value)} placeholder={f.placeholder} />
            )}
            {f.hint && <span className="hint">{f.hint}</span>}
          </div>
        ))}
      </div>

      {/* Actions */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
        <button className="btn btn-green" onClick={start} disabled={running}>▶ Start</button>
        <button className="btn btn-red" onClick={stop} disabled={!running}>■ Stop</button>
        {sid && <span style={{ fontSize: '.8rem', color: 'var(--muted)', alignSelf: 'center' }}>SID: {sid}</span>}
      </div>

      {/* Custom Status */}
      {status && renderStatus?.(status)}

      {/* Logs */}
      <div style={{ fontWeight: 700, fontSize: '.85rem', marginBottom: 6 }}>Live Logs</div>
      <div ref={logRef} style={{ background: '#0f172a', color: '#e2e8f0', padding: 14, borderRadius: 8, height: 220, overflowY: 'auto', fontFamily: 'monospace', fontSize: '.78rem', whiteSpace: 'pre-wrap' }}>
        {logs.length ? logs.map((l, i) => <div key={i}>{l}</div>) : <span style={{ color: '#64748b' }}>Waiting for logs...</span>}
      </div>
    </>
  );
}
