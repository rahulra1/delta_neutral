import React, { useState, useEffect, useMemo } from 'react';
import api from '../api';
import PayoffChart from '../components/PayoffChart';

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

export default function StrategyBuilder() {
  const [profiles, setProfiles] = useState([]);
  const [expiries, setExpiries] = useState([]);
  const [name, setName] = useState('Untitled Strategy');
  const [asset, setAsset] = useState('BTC');
  const [expiry, setExpiry] = useState('');
  const [profileId, setProfileId] = useState('');
  const [legs, setLegs] = useState([{ side: 'sell', type: 'CE', strike: 'ATM', lots: 1 }]);
  const [entry, setEntry] = useState([{ type: 'time', value: '09:20' }]);
  const [exit, setExit] = useState([{ type: 'time', value: '15:15' }]);
  const [risk, setRisk] = useState({ sl: 30, target: 50, legSl: 0, trail: 0, maxLoss: 0, reentry: 'none' });
  const [exec, setExec] = useState({ orderType: 'market', product: 'overnight', lots: 1, sqoff: '15:15', days: 'all' });
  const [adj, setAdj] = useState({ trigger: 'none', value: 40, action: 'shift', maxAdj: 3 });
  const [toast, setToast] = useState('');
  const [chainData, setChainData] = useState([]);
  const [spot, setSpot] = useState(0);

  useEffect(() => { api.get('/profiles').then(r => { const p = r.data.profiles || []; setProfiles(p); if (p.length) setProfileId(p[0].id); }); }, []);
  useEffect(() => { if (!profileId) return; api.get('/expiries', { params: { asset, profile_id: profileId } }).then(r => { const e = r.data.expiries || []; setExpiries(e); setExpiry(e[0] || ''); }); }, [asset, profileId]);
  useEffect(() => { if (!expiry || !profileId) return; api.get('/chain', { params: { asset, expiry, profile_id: profileId } }).then(r => { setChainData(r.data.chain || []); setSpot(r.data.spot_price || 0); }).catch(() => {}); }, [asset, expiry, profileId]);

  // Resolve symbolic strikes to real strikes for payoff
  const isCrypto = CRYPTO.has(asset);
  const lotSize = LOT_SIZE[asset] || 1;
  const symCur = isCrypto ? '$' : '₹';
  const resolvedLegs = useMemo(() => {
    if (!chainData.length || !spot) return [];
    const strikes = chainData.map(r => parseFloat(r.strike));
    const atmIdx = strikes.reduce((best, s, i) => Math.abs(s - spot) < Math.abs(strikes[best] - spot) ? i : best, 0);
    return legs.map(l => {
      const m = l.strike.match(/^(ATM|OTM|ITM)(\d*)$/);
      let offset = 0;
      if (m) {
        offset = parseInt(m[2]) || 0;
        const isCall = l.type === 'CE';
        if (m[1] === 'OTM') offset = isCall ? offset : -offset;
        else if (m[1] === 'ITM') offset = isCall ? -offset : offset;
      }
      const idx = Math.max(0, Math.min(atmIdx + offset, chainData.length - 1));
      const row = chainData[idx];
      const opt = l.type === 'CE' ? row?.call : row?.put;
      return { side: l.side, type: l.type === 'CE' ? 'call' : 'put', strike: parseFloat(row?.strike || 0), mark: opt?.mark_price || 0, size: l.lots };
    });
  }, [legs, chainData, spot]);

  const updateLeg = (i, k, v) => setLegs(prev => prev.map((l, j) => j === i ? { ...l, [k]: v } : l));
  const showToast = msg => { setToast(msg); setTimeout(() => setToast(''), 3000); };

  const submit = endpoint => {
    const payload = { name, underlying: asset, expiry, profile_id: profileId, legs, entry_conditions: entry, exit_conditions: exit, risk: { sl_pct: risk.sl, target_pct: risk.target, per_leg_sl: risk.legSl, trailing_sl: risk.trail, max_loss: risk.maxLoss, reentry: risk.reentry }, execution: { order_type: exec.orderType, product: exec.product, lots: exec.lots, square_off: exec.sqoff, days: exec.days }, adjustments: adj };
    api.post(`/strategy-builder/${endpoint}`, payload).then(r => {
      showToast(endpoint === 'deploy' ? '🚀 Strategy deployed!' : endpoint === 'paper-trade' ? '📝 Paper trade started!' : '💾 Saved!');
    }).catch(() => showToast('Error'));
  };

  return (
    <div className="sb-container">
      <div className="sb-header">
        <div>
          <h1 style={{ fontSize: '1.3rem', fontWeight: 800 }}>🛠 Strategy Builder</h1>
          <p className="sb-subtitle" style={{ fontSize: '.85rem', color: 'var(--muted)' }}>Build, test and deploy automated options strategies</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-outline" onClick={() => submit('save')}>💾 Save</button>
          <button className="btn" style={{ background: '#f59e0b', color: '#fff' }} onClick={() => submit('paper-trade')}>📝 Paper Trade</button>
          <button className="btn btn-green" onClick={() => submit('deploy')}>🚀 Deploy Live</button>
        </div>
      </div>

      <div className="sb-grid">
        {/* LEFT COLUMN */}
        <div>
          {/* Basic Info */}
          <div className="sb-card">
            <div className="sb-card-title">📋 Basic Info</div>
            <div className="sb-row">
              <div className="sb-field"><label>Strategy Name</label><input value={name} onChange={e => setName(e.target.value)} /></div>
              <div className="sb-field"><label>Underlying</label><select value={asset} onChange={e => setAsset(e.target.value)}>{ASSETS.map(a => <option key={a}>{a}</option>)}</select></div>
              <div className="sb-field"><label>Expiry</label><select value={expiry} onChange={e => setExpiry(e.target.value)}>{expiries.map(e => <option key={e}>{e}</option>)}</select></div>
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
                <select value={l.strike} onChange={e => updateLeg(i, 'strike', e.target.value)} style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12 }}>{STRIKES.map(s => <option key={s}>{s}</option>)}</select>
                <input type="number" min={1} value={l.lots} onChange={e => updateLeg(i, 'lots', +e.target.value || 1)} style={{ width: 60, padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, textAlign: 'center' }} />
                <button style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 16 }} onClick={() => setLegs(prev => prev.filter((_, j) => j !== i))}>✕</button>
              </div>
            ))}
            <div style={{ display: 'flex', gap: 16, marginTop: 12, fontSize: '.8rem', flexWrap: 'wrap' }}>
              <span style={{ color: 'var(--muted)' }}>Total Legs: <b>{legs.length}</b></span>
            </div>
          </div>

          {/* Payoff Chart */}
          {resolvedLegs.length > 0 && resolvedLegs[0].strike > 0 && (
            <div className="sb-card">
              <div className="sb-card-title">📈 Payoff at Expiry</div>
              <PayoffChart legs={resolvedLegs} lotSize={lotSize} spot={spot} sym={symCur} />
            </div>
          )}

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
            <div className="sb-field"><label>Square Off Time</label><input type="time" value={exec.sqoff} onChange={e => setExec(p => ({ ...p, sqoff: e.target.value }))} /></div>
            <div className="sb-field"><label>Days to Run</label><select value={exec.days} onChange={e => setExec(p => ({ ...p, days: e.target.value }))}><option value="all">All Days</option><option value="mon">Monday</option><option value="tue">Tuesday</option><option value="wed">Wednesday</option><option value="thu">Thursday</option><option value="fri">Friday</option><option value="expiry">Expiry Day Only</option><option value="non_expiry">Non-Expiry Days</option></select></div>
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
