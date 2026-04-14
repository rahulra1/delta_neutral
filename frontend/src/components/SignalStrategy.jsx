import React, { useState, useEffect } from 'react';
import api from '../api';

export default function SignalStrategy({ strategyKey, title, description, icon, profiles }) {
  const [asset, setAsset] = useState('BTC');
  const [tf, setTf] = useState('1h');
  const [lots, setLots] = useState(10);
  const [profileId, setProfileId] = useState('');
  const [signals, setSignals] = useState([]);
  const [backtest, setBacktest] = useState(null);
  const [loading, setLoading] = useState(false);
  const [deployed, setDeployed] = useState(false);
  const [strongOnly, setStrongOnly] = useState(false);

  const lotSize = asset === 'BTC' ? 0.001 : 0.01;

  useEffect(() => { if (profiles.length && !profileId) setProfileId(profiles[0].id); }, [profiles]);

  const fetchSignals = () => {
    setLoading(true); setBacktest(null);
    api.get('/chart-data', { params: { symbol: asset, interval: tf, indicators: strategyKey } })
      .then(r => setSignals((r.data?.indicators?.[strategyKey]?.signals || []).slice(-10).reverse()))
      .catch(() => setSignals([]))
      .finally(() => setLoading(false));
  };

  useEffect(fetchSignals, [asset, tf, strategyKey]);

  const runBacktest = () => {
    setLoading(true);
    api.get('/chart-data', { params: { symbol: asset, interval: tf, indicators: strategyKey } })
      .then(r => {
        const allSigs = r.data?.indicators?.[strategyKey]?.signals || [];
        const sigs = strongOnly && strategyKey === 'sma_vol_breakout' ? allSigs.filter(s => s.strength === 'strong') : allSigs;
        const candles = r.data?.candles || [];
        let totalPnl = 0, wins = 0, losses = 0;
        const trades = sigs.map(s => {
          const isBuy = s.type === 'buy';
          const si = candles.findIndex(c => c.t >= s.time);
          let outcome = 'open', pnl = 0;
          if (si >= 0) for (let i = si + 1; i < candles.length; i++) {
            const c = candles[i];
            if (isBuy) { if (c.l <= s.sl) { outcome='sl'; pnl=-(s.price-s.sl)*lots*lotSize; break; } if (c.h >= s.tp1) { outcome='tp'; pnl=(s.tp1-s.price)*lots*lotSize; break; } }
            else { if (c.h >= s.sl) { outcome='sl'; pnl=-(s.sl-s.price)*lots*lotSize; break; } if (c.l <= s.tp1) { outcome='tp'; pnl=(s.price-s.tp1)*lots*lotSize; break; } }
          }
          pnl = parseFloat(pnl.toFixed(2)); totalPnl += pnl;
          if (outcome === 'tp') wins++; else if (outcome === 'sl') losses++;
          return { ...s, pnl, outcome };
        });
        setBacktest({ totalPnl: parseFloat(totalPnl.toFixed(2)), wins, losses, total: sigs.length, open: sigs.length - wins - losses, trades });
      }).catch(() => {}).finally(() => setLoading(false));
  };

  const deploy = () => {
    const name = `${title} ${asset} ${tf}`;
    if (!window.confirm(`Deploy ${name} with ${lots} lots?`)) return;
    api.post('/strategy-builder/deploy', { name, underlying: asset, strategy_type: strategyKey, timeframe: tf, lots, profile_id: profileId, legs: [], risk: { sl_pct: 0, target_pct: 0 }, execution: { lots } })
      .then(() => { setDeployed(true); setTimeout(() => setDeployed(false), 5000); })
      .catch(e => alert(e.response?.data?.error || 'Deploy failed'));
  };

  const closed = (backtest?.wins || 0) + (backtest?.losses || 0);

  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: '1rem' }}>{icon} {title}</div>
          <div style={{ fontSize: '.78rem', color: 'var(--muted)', marginTop: 2 }}>{description}</div>
        </div>
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
        <div style={{ display: 'flex', borderRadius: 6, overflow: 'hidden', border: '1px solid var(--border)' }}>
          {['BTC', 'ETH'].map(a => <button key={a} onClick={() => setAsset(a)} style={{ padding: '6px 16px', border: 'none', fontSize: '.82rem', fontWeight: 700, cursor: 'pointer', background: asset === a ? 'var(--accent)' : 'var(--card)', color: asset === a ? '#fff' : 'var(--muted)' }}>{a === 'BTC' ? '₿ BTC' : '⟠ ETH'}</button>)}
        </div>
        <select value={tf} onChange={e => setTf(e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)' }}>
          <option value="15m">15m</option><option value="1h">1H</option><option value="4h">4H</option><option value="1d">1D</option>
        </select>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <label style={{ fontSize: '.72rem', color: 'var(--muted)' }}>Lots</label>
          <input type="number" min={1} value={lots} onChange={e => setLots(Math.max(1, +e.target.value))} style={{ width: 65, padding: '6px 8px', border: '1px solid var(--border)', borderRadius: 6, textAlign: 'center', background: 'var(--bg)' }} />
          <span style={{ fontSize: '.65rem', color: 'var(--muted)' }}>= {(lots * lotSize).toFixed(3)} {asset}</span>
        </div>
        <select value={profileId} onChange={e => setProfileId(e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: '.82rem', background: 'var(--bg)' }}>
          <option value="">Default Keys</option>
          {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </div>

      {/* Action buttons */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 14, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="btn" style={{ background: '#f59e0b', color: '#fff' }} onClick={runBacktest} disabled={loading}>{loading ? '⏳...' : '📝 Backtest'}</button>
        <button className="btn btn-green" onClick={deploy} disabled={loading || deployed}>{deployed ? '✅ Deployed!' : '🚀 Deploy Live'}</button>
        {strategyKey === 'sma_vol_breakout' && <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '.8rem', cursor: 'pointer' }}><input type="checkbox" checked={strongOnly} onChange={e => setStrongOnly(e.target.checked)} /> Strong only</label>}
      </div>

      {/* Backtest Results */}
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

      {/* Recent Signals */}
      <div style={{ fontSize: '.78rem', fontWeight: 600, color: 'var(--muted)', marginBottom: 6 }}>Recent Signals</div>
      {loading ? <div style={{ color: 'var(--muted)', padding: 8 }}>Loading...</div> : signals.length === 0 ? <div style={{ color: 'var(--muted)', padding: 8 }}>No signals</div> : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.8rem' }}>
          <thead><tr>{['Signal', 'Price', 'SL', 'TP', 'R:R', ...(strategyKey === 'sma_vol_breakout' ? ['Setup', 'Vol'] : []), 'Risk ($)'].map(h => <th key={h} style={{ textAlign: 'left', padding: '6px 8px', color: 'var(--muted)', fontSize: '.68rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
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
              {strategyKey === 'sma_vol_breakout' && <><td style={{ padding: '6px 8px' }}><span className={`badge ${s.setup === 1 ? 'badge-green' : 'badge-yellow'}`}>{s.setup === 1 ? 'S1' : 'S2'}</span></td><td style={{ padding: '6px 8px' }}>{s.vol_ratio}x</td></>}
              <td style={{ padding: '6px 8px', color: 'var(--red)', fontWeight: 600 }}>${risk.toFixed(2)}</td>
            </tr>);
          })}</tbody>
        </table>
      )}
    </div>
  );
}
