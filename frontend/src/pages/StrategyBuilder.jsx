import React, { useState, useEffect, useMemo } from 'react';
import api from '../api';

const ASSETS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'];
const CRYPTO = new Set(['BTC', 'ETH']);
const LOT_SIZE = { BTC: 0.001, ETH: 0.01, NIFTY: 75, BANKNIFTY: 30, FINNIFTY: 40, MIDCPNIFTY: 75, SENSEX: 20, BANKEX: 30 };
const STRIKES = ['ATM', ...Array.from({ length: 10 }, (_, i) => `OTM${i + 1}`), ...Array.from({ length: 10 }, (_, i) => `ITM${i + 1}`)];
const PRESETS = {
  'Short Straddle': [{ side: 'sell', type: 'CE', strike: 'ATM', lots: 1 }, { side: 'sell', type: 'PE', strike: 'ATM', lots: 1 }],
  'Short Strangle': [{ side: 'sell', type: 'CE', strike: 'OTM2', lots: 1 }, { side: 'sell', type: 'PE', strike: 'OTM2', lots: 1 }],
  'Iron Condor': [{ side: 'buy', type: 'CE', strike: 'OTM4', lots: 1 }, { side: 'sell', type: 'CE', strike: 'OTM2', lots: 1 }, { side: 'sell', type: 'PE', strike: 'OTM2', lots: 1 }, { side: 'buy', type: 'PE', strike: 'OTM4', lots: 1 }],
  'Iron Butterfly': [{ side: 'buy', type: 'CE', strike: 'OTM3', lots: 1 }, { side: 'sell', type: 'CE', strike: 'ATM', lots: 1 }, { side: 'sell', type: 'PE', strike: 'ATM', lots: 1 }, { side: 'buy', type: 'PE', strike: 'OTM3', lots: 1 }],
  'Bull Call Spread': [{ side: 'buy', type: 'CE', strike: 'ATM', lots: 1 }, { side: 'sell', type: 'CE', strike: 'OTM3', lots: 1 }],
  'Bear Put Spread': [{ side: 'buy', type: 'PE', strike: 'ATM', lots: 1 }, { side: 'sell', type: 'PE', strike: 'OTM3', lots: 1 }],
};
const COND_TYPES = [{ v: 'time', l: '⏰ Time Based' }, { v: 'indicator', l: '📊 Indicator Based' }, { v: 'price', l: '💰 Price Based' }, { v: 'pnl', l: '📈 P&L Based' }, { v: 'premium', l: '🏷 Premium Based' }];

const EXPIRY_TYPES = [
  { v: 'daily', l: 'Daily (nearest)', daily: true },
  { v: 'next_daily', l: 'Next Daily', daily: true },
  { v: 'weekly', l: 'Weekly (nearest)' },
  { v: 'next_weekly', l: 'Next Weekly' },
  { v: 'monthly', l: 'Monthly' },
  { v: 'next_monthly', l: 'Next Monthly' },
];
const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// ISO week number for a Date (matches Python's isocalendar week grouping).
function isoWeekKey(dt) {
  const d = new Date(Date.UTC(dt.getFullYear(), dt.getMonth(), dt.getDate()));
  const day = d.getUTCDay() || 7;            // Mon=1..Sun=7
  d.setUTCDate(d.getUTCDate() + 4 - day);     // nearest Thursday
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
  return `${d.getUTCFullYear()}-${week}`;
}

// Resolve a logical expiry type to a concrete DD-MM-YYYY date from the list.
// Mirrors api/expiry_resolver.py so the UI preview matches the backend.
function resolveExpiryType(type, expiries) {
  const parse = (s) => { const [d, m, y] = (s || '').split('-').map(Number); return (d && m && y) ? new Date(y, m - 1, d) : null; };
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const future = (expiries || []).map(s => [parse(s), s])
    .filter(([dt]) => dt && dt >= today)
    .sort((a, b) => a[0] - b[0]);
  if (!future.length) return '';
  if (!EXPIRY_TYPES.some(t => t.v === type)) return expiries.includes(type) ? type : '';
  if (type === 'daily') return future[0][1];
  if (type === 'next_daily') return (future[1] || future[future.length - 1])[1];
  // weekly = last expiry of each ISO week
  const lastInWeek = {};
  future.forEach(([dt, s]) => { lastInWeek[isoWeekKey(dt)] = [dt, s]; });
  const weekly = Object.values(lastInWeek).sort((a, b) => a[0] - b[0]).map(x => x[1]);
  if (type === 'weekly') return weekly[0] || future[0][1];
  if (type === 'next_weekly') return weekly[1] || weekly[weekly.length - 1] || future[future.length - 1][1];
  // monthly = last expiry of each calendar month, chronological by (year, month)
  const lastInMonth = {};
  future.forEach(([dt, s]) => { lastInMonth[dt.getFullYear() * 12 + dt.getMonth()] = s; });
  const monthly = Object.keys(lastInMonth).map(Number).sort((a, b) => a - b).map(k => lastInMonth[k]);
  if (!monthly.length) return '';
  if (type === 'monthly') return monthly[0];
  if (type === 'next_monthly') return monthly[1] || monthly[monthly.length - 1];
  return '';
}

export default function StrategyBuilder() {
  const [profiles, setProfiles] = useState([]);
  const [expiries, setExpiries] = useState([]);
  const [name, setName] = useState('Untitled Strategy');
  const [asset, setAsset] = useState('BTC');
  const [expiry, setExpiry] = useState('next_weekly');
  const [profileId, setProfileId] = useState('');
  const [legs, setLegs] = useState([{ side: 'sell', type: 'CE', strike: 'ATM', lots: 1 }]);
  const [entry, setEntry] = useState([{ type: 'time', value: '09:20' }]);
  const [exit, setExit] = useState([{ type: 'time', value: '15:15' }]);
  const [risk, setRisk] = useState({ sl: 30, target: 50, legSl: 0, trail: 0, maxLoss: 0, reentry: 'none' });
  const [exec, setExec] = useState({ orderType: 'market', product: 'overnight', lots: 1, sqoff: '15:15', days: [0, 1, 2, 3, 4], daysBeforeExpiryEntry: '', daysBeforeExpiryExit: '' });
  const [adj, setAdj] = useState({ trigger: 'none', value: 40, action: 'shift', maxAdj: 3 });
  const [toast, setToast] = useState('');
  const [chainData, setChainData] = useState([]);
  const [spot, setSpot] = useState(0);
  const [recurring, setRecurring] = useState(false);
  const [expirySource, setExpirySource] = useState(null);
  const [liveSid, setLiveSid] = useState(null);
  const [liveStatus, setLiveStatus] = useState(null);
  const [liveLogs, setLiveLogs] = useState([]);
  const pollRef = React.useRef(null);
  const esRef = React.useRef(null);

  useEffect(() => () => { clearInterval(pollRef.current); esRef.current?.close(); }, []);

  const startMonitoring = (sid) => {
    setLiveSid(sid); setLiveLogs([]);
    clearInterval(pollRef.current);
    pollRef.current = setInterval(() => {
      api.get(`/strategy-builder/status/${sid}`).then(r => {
        setLiveStatus(r.data);
        if (r.data && r.data.running === false) { clearInterval(pollRef.current); esRef.current?.close(); }
      }).catch(() => {});
    }, 4000);
    esRef.current?.close();
    const es = new EventSource(`/api/strategy-builder/stream/${sid}?token=${localStorage.getItem('token')}`);
    es.onmessage = e => setLiveLogs(l => [...l.slice(-300), e.data]);
    esRef.current = es;
  };

  const stopRecurring = () => {
    if (!liveSid) return;
    api.post('/strategy-builder/stop-recurring', { sid: liveSid }).then(() => {
      clearInterval(pollRef.current); esRef.current?.close();
      setLiveStatus(s => s ? { ...s, running: false } : s);
      showToast('⏹ Recurring strategy stopped');
    });
  };

  useEffect(() => { api.get('/profiles').then(r => { const p = r.data.profiles || []; setProfiles(p); if (p.length) setProfileId(p[0].id); }); }, []);
  useEffect(() => { if (!profileId) return; api.get('/expiries', { params: { asset, profile_id: profileId } }).then(r => { const e = r.data.expiries || []; setExpiries(e); setExpirySource({ source: r.data.source, endpoint: r.data.endpoint, live: r.data.live }); }); }, [asset, profileId]);
  // Daily expiry types only apply to crypto; reset if the asset can't do daily.
  useEffect(() => { if (!CRYPTO.has(asset) && (expiry === 'daily' || expiry === 'next_daily')) setExpiry('next_weekly'); }, [asset, expiry]);
  const resolvedExpiry = useMemo(() => resolveExpiryType(expiry, expiries), [expiry, expiries]);
  useEffect(() => { if (!resolvedExpiry || !profileId) return; api.get('/chain', { params: { asset, expiry: resolvedExpiry, profile_id: profileId } }).then(r => { setChainData(r.data.chain || []); setSpot(r.data.spot_price || 0); }).catch(() => {}); }, [asset, resolvedExpiry, profileId]);

  const updateLeg = (i, k, v) => setLegs(prev => prev.map((l, j) => j === i ? { ...l, [k]: v } : l));
  const showToast = msg => { setToast(msg); setTimeout(() => setToast(''), 3000); };

  const submit = endpoint => {
    const payload = { name, underlying: asset, expiry, profile_id: profileId, recurring, legs, entry_conditions: entry, exit_conditions: exit, risk: { sl_pct: risk.sl, target_pct: risk.target, per_leg_sl: risk.legSl, trailing_sl: risk.trail, max_loss: risk.maxLoss, reentry: risk.reentry }, execution: { order_type: exec.orderType, product: exec.product, lots: exec.lots, square_off: exec.sqoff, days: exec.days, days_before_expiry_entry: exec.daysBeforeExpiryEntry, days_before_expiry_exit: exec.daysBeforeExpiryExit }, adjustments: adj };
    api.post(`/strategy-builder/${endpoint}`, payload).then(r => {
      showToast(endpoint === 'deploy' ? '🚀 Strategy deployed!' : endpoint === 'paper-trade' ? '📝 Paper trade started!' : '💾 Saved!');
      // If a recurring strategy was deployed, start the live monitor + log stream.
      if (endpoint === 'deploy' && r.data?.recurring && r.data?.sid) {
        startMonitoring(r.data.sid);
      }
    }).catch(() => showToast('Error'));
  };

  return (
    <div className="sb-container">
      <div className="sb-header">
        <div>
          <h1 style={{ fontSize: '1.3rem', fontWeight: 800 }}>🛠 Strategy Builder</h1>
          <p className="sb-subtitle" style={{ fontSize: '.85rem', color: 'var(--muted)' }}>Build, test and deploy automated options strategies</p>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: '.8rem', cursor: 'pointer', color: 'var(--muted)' }} title="Run on the configured days/time repeatedly instead of placing once now">
            <input type="checkbox" checked={recurring} onChange={e => setRecurring(e.target.checked)} /> Recurring
          </label>
          <button className="btn btn-outline" onClick={() => submit('save')}>💾 Save</button>
          <button className="btn" style={{ background: '#f59e0b', color: '#fff' }} onClick={() => submit('paper-trade')}>📝 Paper Trade</button>
          <button className="btn btn-green" onClick={() => submit('deploy')}>{recurring ? '🔁 Deploy Recurring' : '🚀 Deploy Live'}</button>
        </div>
      </div>

      {/* Live monitoring + logs for a deployed recurring strategy */}
      {liveSid && (
        <div className="sb-card" style={{ marginBottom: 16, borderColor: 'var(--accent)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <div className="sb-card-title" style={{ margin: 0 }}>
              {liveStatus?.running ? '🟢' : '⚪'} Recurring Monitor <span style={{ fontSize: '.7rem', color: 'var(--muted)' }}>· SID {liveSid}</span>
            </div>
            {liveStatus?.running && <button className="btn" style={{ background: '#ef4444', color: '#fff', padding: '5px 14px' }} onClick={stopRecurring}>⏹ Stop</button>}
          </div>
          {liveStatus && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5,1fr)', gap: 8, marginBottom: 10 }}>
              {[['Total P&L', `${liveStatus.total_pnl ?? 0}`, (liveStatus.total_pnl ?? 0) >= 0 ? 'var(--green)' : 'var(--red)'],
                ['Running', `${liveStatus.running_pnl ?? 0}`, (liveStatus.running_pnl ?? 0) >= 0 ? 'var(--green)' : 'var(--red)'],
                ['Realized', `${liveStatus.cumulative_pnl ?? 0}`, (liveStatus.cumulative_pnl ?? 0) >= 0 ? 'var(--green)' : 'var(--red)'],
                ['Entries', liveStatus.total_entries ?? 0, 'var(--text)'],
                ['Open', liveStatus.open_positions ?? 0, 'var(--text)']].map(([k, v, c]) => (
                <div key={k} style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '.6rem', color: 'var(--muted)', textTransform: 'uppercase' }}>{k}</div>
                  <div style={{ fontSize: '.95rem', fontWeight: 800, color: c }}>{v}</div>
                </div>
              ))}
            </div>
          )}
          {liveStatus?.legs?.length > 0 && (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.78rem', marginBottom: 10 }}>
              <thead><tr>{['Side', 'Type', 'Strike', 'Size', 'Entry', 'Expiry'].map(h => <th key={h} style={{ textAlign: 'left', padding: '3px 6px', color: 'var(--muted)', fontSize: '.64rem', borderBottom: '1px solid var(--border)' }}>{h}</th>)}</tr></thead>
              <tbody>{liveStatus.legs.map((l, i) => (
                <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '3px 6px', color: l.side === 'sell' ? 'var(--red)' : 'var(--green)', fontWeight: 700 }}>{(l.side || '').toUpperCase()}</td>
                  <td style={{ padding: '3px 6px' }}>{l.type}</td>
                  <td style={{ padding: '3px 6px' }}>{l.strike}</td>
                  <td style={{ padding: '3px 6px' }}>{l.size}</td>
                  <td style={{ padding: '3px 6px' }}>{(l.entry_price ?? 0).toFixed ? l.entry_price.toFixed(2) : l.entry_price}</td>
                  <td style={{ padding: '3px 6px' }}>{l.expiry}</td>
                </tr>))}</tbody>
            </table>
          )}
          <div style={{ fontWeight: 700, fontSize: '.78rem', marginBottom: 4 }}>Live Logs</div>
          <div style={{ background: '#0f172a', color: '#e2e8f0', padding: 10, borderRadius: 6, height: 160, overflowY: 'auto', fontFamily: 'monospace', fontSize: '.72rem', whiteSpace: 'pre-wrap' }}>
            {liveLogs.length ? liveLogs.map((l, i) => <div key={i}>{l}</div>) : <span style={{ color: '#64748b' }}>Waiting for scheduler logs...</span>}
          </div>
        </div>
      )}

      <div className="sb-grid">
        {/* LEFT COLUMN */}
        <div>
          {/* Basic Info */}
          <div className="sb-card">
            <div className="sb-card-title">📋 Basic Info</div>
            <div className="sb-row">
              <div className="sb-field"><label>Strategy Name</label><input value={name} onChange={e => setName(e.target.value)} /></div>
              <div className="sb-field"><label>Underlying</label><select value={asset} onChange={e => setAsset(e.target.value)}>{ASSETS.map(a => <option key={a}>{a}</option>)}</select></div>
              <div className="sb-field"><label>Expiry</label><select value={expiry} onChange={e => setExpiry(e.target.value)}>{EXPIRY_TYPES.filter(t => !t.daily || CRYPTO.has(asset)).map(t => <option key={t.v} value={t.v}>{t.l}</option>)}</select>
                <span className="hint">
                  {resolvedExpiry ? `→ ${resolvedExpiry}` : 'no expiries'}
                  {expirySource && ` · ${expirySource.source}`}
                  {expirySource && expirySource.live === false && <span style={{ color: 'var(--red)', fontWeight: 700 }}> · TESTNET</span>}
                  {expirySource && expirySource.live === true && expirySource.source === 'Delta Exchange' && <span style={{ color: 'var(--green)', fontWeight: 700 }}> · LIVE</span>}
                </span>
                {expirySource?.endpoint && <span className="hint" style={{ fontSize: '.62rem', opacity: .7 }}>{expirySource.endpoint}</span>}
              </div>
              <div className="sb-field"><label>API Profile</label><select value={profileId} onChange={e => setProfileId(e.target.value)}><option value="">Default Keys</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>
            </div>
          </div>

          {/* Legs */}
          <div className="sb-card">
            <div className="sb-card-header">
              <div className="sb-card-title">🦵 Position Legs</div>
              <button className="btn-sm btn-add" onClick={() => setLegs(prev => [...prev, { side: 'sell', type: 'CE', strike: 'ATM', lots: 1 }])}>+ Add Leg</button>
            </div>
            <div className="sb-presets">
              {Object.keys(PRESETS).map(p => <span key={p} onClick={() => setLegs(PRESETS[p].map(l => ({ ...l })))}>{p}</span>)}
            </div>
            {legs.map((l, i) => (
              <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <select value={l.side} onChange={e => updateLeg(i, 'side', e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, background: l.side === 'sell' ? 'rgba(239,68,68,0.1)' : 'rgba(34,197,94,0.1)', color: l.side === 'sell' ? 'var(--red)' : 'var(--green)', fontWeight: 700 }}><option value="buy">BUY</option><option value="sell">SELL</option></select>
                <select value={l.type} onChange={e => updateLeg(i, 'type', e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12 }}><option>CE</option><option>PE</option></select>
                <select value={l.strike_mode || 'offset'} onChange={e => updateLeg(i, 'strike_mode', e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12 }} title="Strike selection mode"><option value="offset">Offset</option><option value="delta">Delta</option></select>
                {(l.strike_mode || 'offset') === 'delta' ? (
                  <input type="number" step="0.01" min={0.01} max={0.99} value={l.target_delta ?? 0.20} onChange={e => updateLeg(i, 'target_delta', +e.target.value)} title="Target delta" style={{ width: 80, padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, textAlign: 'center' }} />
                ) : (
                  <select value={l.strike} onChange={e => updateLeg(i, 'strike', e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12 }}>{STRIKES.map(s => <option key={s}>{s}</option>)}</select>
                )}
                <input type="number" min={1} value={l.lots} onChange={e => updateLeg(i, 'lots', +e.target.value || 1)} style={{ width: 60, padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, textAlign: 'center' }} />
                <button style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 16 }} onClick={() => setLegs(prev => prev.filter((_, j) => j !== i))}>✕</button>
              </div>
            ))}
            <div style={{ display: 'flex', gap: 16, marginTop: 12, fontSize: '.8rem', flexWrap: 'wrap' }}>
              <span style={{ color: 'var(--muted)' }}>Total Legs: <b>{legs.length}</b></span>
            </div>
          </div>

          {/* Entry Conditions */}
          <div className="sb-card">
            <div className="sb-card-header">
              <div className="sb-card-title">🟢 Entry Conditions</div>
              <button className="btn-sm btn-add" onClick={() => setEntry(prev => [...prev, { type: 'time', value: '' }])}>+ Add Condition</button>
            </div>
            {entry.map((c, i) => (
              <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                <select value={c.type} onChange={e => setEntry(prev => prev.map((x, j) => j === i ? { ...x, type: e.target.value } : x))} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12 }}>{COND_TYPES.map(t => <option key={t.v} value={t.v}>{t.l}</option>)}</select>
                <input value={c.value} onChange={e => setEntry(prev => prev.map((x, j) => j === i ? { ...x, value: e.target.value } : x))} placeholder={c.type === 'time' ? 'HH:MM' : 'Value'} type={c.type === 'time' ? 'time' : 'text'} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, flex: 1 }} />
                <button style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer' }} onClick={() => setEntry(prev => prev.filter((_, j) => j !== i))}>✕</button>
              </div>
            ))}
            <div style={{ fontSize: '.72rem', color: 'var(--muted)', marginTop: 8 }}>All conditions must be TRUE (AND logic) for entry to trigger</div>
            <div style={{ borderTop: '1px solid var(--border)', marginTop: 10, paddingTop: 10 }}>
              <div className="sb-field"><label>Days to Start Strategy</label>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  {DAY_LABELS.map((lbl, d) => {
                    const on = (exec.days || []).includes(d);
                    return <button key={d} type="button" onClick={() => setExec(p => ({ ...p, days: on ? p.days.filter(x => x !== d) : [...p.days, d].sort((a, b) => a - b) }))} style={{ padding: '5px 9px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 11, fontWeight: 700, cursor: 'pointer', background: on ? 'var(--accent)' : 'var(--card)', color: on ? '#fff' : 'var(--muted)' }}>{lbl}</button>;
                  })}
                </div>
                <span className="hint">Weekdays the strategy may enter (recurring mode)</span>
              </div>
              <div className="sb-field"><label>Days Before Expiry to Enter</label><input type="number" value={exec.daysBeforeExpiryEntry} onChange={e => setExec(p => ({ ...p, daysBeforeExpiryEntry: e.target.value }))} placeholder="blank = any day" /><span className="hint">Enter only when ≤ this many days to expiry</span></div>
            </div>
          </div>

          {/* Exit Conditions */}
          <div className="sb-card">
            <div className="sb-card-header">
              <div className="sb-card-title">🔴 Exit Conditions</div>
              <button className="btn-sm btn-add" onClick={() => setExit(prev => [...prev, { type: 'time', value: '' }])}>+ Add Condition</button>
            </div>
            {exit.map((c, i) => (
              <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                <select value={c.type} onChange={e => setExit(prev => prev.map((x, j) => j === i ? { ...x, type: e.target.value } : x))} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12 }}>{COND_TYPES.map(t => <option key={t.v} value={t.v}>{t.l}</option>)}</select>
                <input value={c.value} onChange={e => setExit(prev => prev.map((x, j) => j === i ? { ...x, value: e.target.value } : x))} placeholder={c.type === 'time' ? 'HH:MM' : 'Value'} type={c.type === 'time' ? 'time' : 'text'} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, flex: 1 }} />
                <button style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer' }} onClick={() => setExit(prev => prev.filter((_, j) => j !== i))}>✕</button>
              </div>
            ))}
            <div style={{ fontSize: '.72rem', color: 'var(--muted)', marginTop: 8 }}>ANY condition being TRUE (OR logic) will trigger exit</div>
            <div style={{ borderTop: '1px solid var(--border)', marginTop: 10, paddingTop: 10 }}>
              <div className="sb-field"><label>Square Off Time</label><input type="time" value={exec.sqoff} onChange={e => setExec(p => ({ ...p, sqoff: e.target.value }))} /><span className="hint">Daily time to force-close open legs</span>
                {(() => {
                  const entryTime = (entry.find(c => c.type === 'time' && c.value) || {}).value;
                  if (entryTime && exec.sqoff && exec.sqoff <= entryTime) {
                    return <span className="hint" style={{ color: 'var(--red)', fontWeight: 700 }}>⚠ Square-off ({exec.sqoff}) is at/before entry ({entryTime}) — the position would be held to the NEXT day's {exec.sqoff}. Set a later square-off for same-day exit.</span>;
                  }
                  return null;
                })()}
              </div>
              <div className="sb-field"><label>Days Before Expiry to Exit</label><input type="number" value={exec.daysBeforeExpiryExit} onChange={e => setExec(p => ({ ...p, daysBeforeExpiryExit: e.target.value }))} placeholder="blank = TP/SL only" /><span className="hint">0 = exit on expiry day</span></div>
            </div>
          </div>
        </div>

        {/* RIGHT COLUMN */}
        <div>
          {/* Risk Management */}
          <div className="sb-card">
            <div className="sb-card-title">🛡 Risk Management</div>
            <div className="sb-field"><label>Overall Stop Loss (%)</label><input type="number" value={risk.sl} onChange={e => setRisk(p => ({ ...p, sl: +e.target.value }))} /><span className="hint">% of premium collected</span></div>
            <div className="sb-field"><label>Overall Target (%)</label><input type="number" value={risk.target} onChange={e => setRisk(p => ({ ...p, target: +e.target.value }))} /><span className="hint">% of premium collected</span></div>
            <div className="sb-field"><label>Per-Leg Stop Loss (%)</label><input type="number" value={risk.legSl} onChange={e => setRisk(p => ({ ...p, legSl: +e.target.value }))} /><span className="hint">0 = disabled</span></div>
            <div className="sb-field"><label>Trailing SL (%)</label><input type="number" value={risk.trail} onChange={e => setRisk(p => ({ ...p, trail: +e.target.value }))} /><span className="hint">0 = disabled</span></div>
            <div className="sb-field"><label>Max Loss (absolute $)</label><input type="number" value={risk.maxLoss} onChange={e => setRisk(p => ({ ...p, maxLoss: +e.target.value }))} /><span className="hint">0 = no cap</span></div>
            <div className="sb-field"><label>Re-entry on SL</label><select value={risk.reentry} onChange={e => setRisk(p => ({ ...p, reentry: e.target.value }))}><option value="none">No Re-entry</option><option value="once">Re-enter Once</option><option value="unlimited">Unlimited</option></select></div>
          </div>

          {/* Execution */}
          <div className="sb-card">
            <div className="sb-card-title">⚙️ Execution</div>
            <div className="sb-field"><label>Order Type</label><select value={exec.orderType} onChange={e => setExec(p => ({ ...p, orderType: e.target.value }))}><option value="market">Market</option><option value="limit">Limit</option></select></div>
            <div className="sb-field"><label>Product Type</label><select value={exec.product} onChange={e => setExec(p => ({ ...p, product: e.target.value }))}><option value="intraday">Intraday (MIS)</option><option value="overnight">Overnight (NRML)</option></select></div>
            <div className="sb-field"><label>Lots per Leg</label><input type="number" value={exec.lots} onChange={e => setExec(p => ({ ...p, lots: +e.target.value }))} min={1} /></div>
          </div>

          {/* Adjustments */}
          <div className="sb-card">
            <div className="sb-card-title">🔄 Adjustments</div>
            <div className="sb-field"><label>Adjustment Trigger</label><select value={adj.trigger} onChange={e => setAdj(p => ({ ...p, trigger: e.target.value }))}><option value="none">No Adjustment</option><option value="premium_pct">Premium % Increase</option><option value="delta_breach">Delta Breach</option><option value="mtm_loss">MTM Loss</option></select></div>
            <div className="sb-field"><label>Trigger Value</label><input type="number" value={adj.value} onChange={e => setAdj(p => ({ ...p, value: +e.target.value }))} /><span className="hint">% or absolute based on trigger</span></div>
            <div className="sb-field"><label>Adjustment Action</label><select value={adj.action} onChange={e => setAdj(p => ({ ...p, action: e.target.value }))}><option value="shift">Shift to current ATM</option><option value="close_opposite">Close opposite leg & re-enter</option><option value="add_hedge">Add hedge</option><option value="exit_all">Exit all positions</option></select></div>
            <div className="sb-field"><label>Max Adjustments</label><input type="number" value={adj.maxAdj} onChange={e => setAdj(p => ({ ...p, maxAdj: +e.target.value }))} min={0} /></div>
          </div>

          {/* Summary */}
          <div className="sb-card" style={{ background: 'var(--bg)' }}>
            <div className="sb-card-title">📊 Strategy Summary</div>
            {[['Type', legs.every(l => l.side === 'sell') ? 'Credit' : legs.every(l => l.side === 'buy') ? 'Debit' : 'Mixed'],
              ['Underlying', asset], ['Legs', legs.length],
              ['Expiry', expiry + (resolvedExpiry ? ` (${resolvedExpiry})` : '')],
              ['Start Days', (exec.days || []).length ? exec.days.map(d => DAY_LABELS[d]).join(', ') : '—'],
              ['Entry', entry.map(c => c.type).join(', ') || '—'],
              ['Exit', exit.map(c => c.type).join(', ') || '—'],
              ['SL / Target', `${risk.sl}% / ${risk.target}%`],
            ].map(([k, v]) => (
              <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '5px 0', fontSize: '.82rem', borderBottom: '1px solid var(--border)' }}>
                <span style={{ color: 'var(--muted)' }}>{k}</span><span style={{ fontWeight: 600 }}>{v}</span>
              </div>
            ))}
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '5px 0', fontSize: '.82rem' }}>
              <span style={{ color: 'var(--muted)' }}>Status</span><span className="badge badge-yellow">Draft</span>
            </div>
          </div>
        </div>
      </div>

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
