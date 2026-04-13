import React, { useState, useEffect, useRef } from 'react';
import api from '../api';
import PayoffChart from '../components/PayoffChart';

const ASSETS = ['BTC', 'ETH', 'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'];
const CRYPTO = new Set(['BTC', 'ETH']);
const LOT_SIZE = { BTC: 0.001, ETH: 0.01, NIFTY: 75, BANKNIFTY: 30, FINNIFTY: 40, MIDCPNIFTY: 75, SENSEX: 20, BANKEX: 30 };

export default function OptionChain() {
  const [asset, setAsset] = useState('BTC');
  const [profiles, setProfiles] = useState([]);
  const [profileId, setProfileId] = useState('');
  const [mode, setMode] = useState('live');
  const [expiries, setExpiries] = useState([]);
  const [expiry, setExpiry] = useState('');
  const [chain, setChain] = useState([]);
  const [spot, setSpot] = useState(0);
  const [legs, setLegs] = useState([]);
  const [maxProfit, setMaxProfit] = useState(0);
  const [maxLoss, setMaxLoss] = useState(0);
  const [monitorId, setMonitorId] = useState(null);
  const [monitorData, setMonitorData] = useState(null);
  const monitorRef = useRef(null);

  const isCrypto = CRYPTO.has(asset);
  const sym = isCrypto ? '$' : '₹';
  const lot = LOT_SIZE[asset] || 1;

  useEffect(() => {
    api.get('/profiles').then(r => {
      const p = r.data.profiles || [];
      setProfiles(p);
      if (p.length) setProfileId(p[0].id);
    });
  }, []);

  const switchMode = m => {
    setMode(m);
    const target = m === 'live' ? 'delta_exchange' : 'demo';
    const match = profiles.find(p => p.broker === target);
    if (match) setProfileId(match.id);
  };

  useEffect(() => {
    if (!profileId) return;
    api.get('/expiries', { params: { asset, profile_id: profileId } }).then(r => {
      const e = r.data.expiries || [];
      setExpiries(e);
      setExpiry(e[0] || '');
    });
  }, [asset, profileId]);

  useEffect(() => {
    if (!expiry || !profileId) return;
    api.get('/chain', { params: { asset, expiry, profile_id: profileId } }).then(r => {
      setChain(r.data.chain || []);
      setSpot(r.data.spot_price || 0);
    });
  }, [asset, expiry, profileId]);

  useEffect(() => {
    if (!monitorId) return;
    const poll = () => api.get(`/monitor/${monitorId}`).then(r => setMonitorData(r.data)).catch(() => {});
    poll();
    monitorRef.current = setInterval(poll, 5000);
    return () => clearInterval(monitorRef.current);
  }, [monitorId]);

  const atmIdx = chain.length && spot ? chain.reduce((best, row, i, arr) => Math.abs(parseFloat(row.strike) - spot) < Math.abs(parseFloat(arr[best].strike) - spot) ? i : best, 0) : -1;

  const addLeg = (row, type, side) => {
    const opt = type === 'call' ? row.call : row.put;
    if (!opt || !opt.product_id) return;
    const exists = legs.findIndex(l => l.symbol === opt.symbol);
    if (exists >= 0) { setLegs(prev => prev.filter((_, i) => i !== exists)); return; }
    setLegs(prev => [...prev, { side, type, strike: row.strike, symbol: opt.symbol, product_id: opt.product_id, delta: opt.delta || 0, mark: opt.mark_price || 0, iv: opt.iv || 0, size: 1 }]);
  };

  const toggleSide = i => setLegs(prev => prev.map((l, j) => j === i ? { ...l, side: l.side === 'buy' ? 'sell' : 'buy' } : l));
  const removeLeg = i => setLegs(prev => prev.filter((_, j) => j !== i));
  const updateSize = (i, v) => setLegs(prev => prev.map((l, j) => j === i ? { ...l, size: Math.max(1, v) } : l));

  const netCredit = legs.reduce((s, l) => s + l.mark * l.size * lot * (l.side === 'sell' ? 1 : -1), 0);

  const payoffLegs = legs.map(l => ({ ...l, type: l.type }));

  const execute = () => {
    const payload = {
      legs: legs.map(l => ({ product_id: l.product_id, symbol: l.symbol, size: l.size, side: l.side, type: l.type, strike: l.strike, mark: l.mark })),
      max_profit: maxProfit, max_loss: maxLoss, asset, profile_id: profileId
    };
    api.post('/place-legs', payload).then(r => {
      if (r.data.monitor_id) setMonitorId(r.data.monitor_id);
      const ok = (r.data.results || []).filter(r => r.success).length;
      alert(`${ok} order(s) placed`);
      if (ok === legs.length) setLegs([]);
    });
  };

  const f2 = v => typeof v === 'number' ? v.toFixed(2) : '—';
  const f4 = v => typeof v === 'number' ? v.toFixed(4) : '—';

  return (
    <div className="container" style={{ maxWidth: 1440 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10, flexWrap: 'wrap' }}>
        <h1 style={{ fontSize: '1.1rem', fontWeight: 700, marginRight: 12 }}>Options Chain</h1>
        <select value={asset} onChange={e => setAsset(e.target.value)} style={{ padding: '6px 12px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, background: 'var(--card)' }}>
          {ASSETS.map(a => <option key={a}>{a}</option>)}
        </select>
        <div style={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 4, padding: '6px 14px', fontWeight: 700, fontSize: 13 }}>Spot: {sym}{spot ? spot.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—'}</div>
        <div style={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 4, padding: '6px 12px', fontSize: 10, color: 'var(--muted)' }}>{isCrypto ? `1 Lot = ${lot} ${asset}` : `Lot Size = ${lot}`}</div>
        {isCrypto && (
          <div style={{ display: 'flex', background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 4, overflow: 'hidden', fontSize: 11 }}>
            <span onClick={() => switchMode('live')} style={{ padding: '5px 14px', cursor: 'pointer', fontWeight: 600, background: mode === 'live' ? 'var(--green)' : 'transparent', color: mode === 'live' ? '#fff' : 'var(--muted)' }}>🟢 Live</span>
            <span onClick={() => switchMode('demo')} style={{ padding: '5px 14px', cursor: 'pointer', fontWeight: 600, background: mode === 'demo' ? '#f59e0b' : 'transparent', color: mode === 'demo' ? '#fff' : 'var(--muted)' }}>🟡 Demo</span>
          </div>
        )}
        <select value={profileId} onChange={e => setProfileId(e.target.value)} style={{ padding: '6px 12px', border: '1px solid var(--border)', borderRadius: 4, fontSize: 12, background: 'var(--card)' }}>
          <option value="">Default</option>
          {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <div style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--muted)' }}>Click = BUY · Right-click = SELL</div>
      </div>

      <div className="expiry-tabs">
        {expiries.map(e => (
          <div key={e} className={`expiry-tab ${e === expiry ? 'active' : ''}`} onClick={() => setExpiry(e)}>{e}</div>
        ))}
      </div>

      {/* Strategy Builder */}
      {legs.length > 0 && (
        <div style={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 6, padding: 16, marginBottom: 12 }}>
          <h3 style={{ fontSize: '.85rem', marginBottom: 10 }}>📐 Strategy Builder</h3>
          <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse', marginBottom: 10 }}>
            <thead><tr>{['Side', 'Type', 'Strike', 'Symbol', 'Δ', 'Mark', 'IV', 'Lots', 'Value', ''].map(h => <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--muted)', fontWeight: 600 }}>{h}</th>)}</tr></thead>
            <tbody>
              {legs.map((l, i) => (
                <tr key={i}>
                  <td style={{ padding: '4px 8px' }}>
                    <div style={{ display: 'inline-flex', borderRadius: 3, overflow: 'hidden', fontSize: 10, fontWeight: 700, cursor: 'pointer' }} onClick={() => toggleSide(i)}>
                      <span style={{ padding: '3px 10px', background: l.side === 'buy' ? 'var(--green)' : 'rgba(2,192,118,0.15)', color: l.side === 'buy' ? '#fff' : 'var(--green)' }}>BUY</span>
                      <span style={{ padding: '3px 10px', background: l.side === 'sell' ? 'var(--red)' : 'rgba(246,70,93,0.15)', color: l.side === 'sell' ? '#fff' : 'var(--red)' }}>SELL</span>
                    </div>
                  </td>
                  <td style={{ padding: '4px 8px' }}>{l.type.toUpperCase()}</td>
                  <td style={{ padding: '4px 8px' }}>{Number(l.strike).toLocaleString()}</td>
                  <td style={{ padding: '4px 8px', fontSize: 11 }}>{l.symbol}</td>
                  <td style={{ padding: '4px 8px', fontWeight: 600 }}>{f4(l.delta)}</td>
                  <td style={{ padding: '4px 8px' }}>{sym}{f2(l.mark)}</td>
                  <td style={{ padding: '4px 8px', color: 'var(--muted)' }}>{l.iv ? (l.iv * 100).toFixed(1) + '%' : '—'}</td>
                  <td style={{ padding: '4px 8px' }}><input type="number" min={1} value={l.size} onChange={e => updateSize(i, +e.target.value)} style={{ width: 60, padding: '4px 8px', border: '1px solid var(--border)', borderRadius: 3, textAlign: 'center' }} /></td>
                  <td style={{ padding: '4px 8px', color: l.side === 'sell' ? 'var(--green)' : 'var(--red)' }}>{sym}{(l.mark * l.size * lot * (l.side === 'sell' ? 1 : -1)).toFixed(2)}</td>
                  <td style={{ padding: '4px 8px', color: 'var(--red)', cursor: 'pointer', fontSize: 14 }} onClick={() => removeLeg(i)}>✕</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ display: 'flex', gap: 12, margin: '10px 0', flexWrap: 'wrap' }}>
            {[
              [netCredit >= 0 ? 'Net Credit' : 'Net Debit', `${sym}${Math.abs(netCredit).toFixed(2)}`, netCredit >= 0 ? 'var(--green)' : 'var(--red)'],
              ['Legs', `${legs.length}`, 'var(--text)'],
            ].map(([lbl, val, clr]) => (
              <div key={lbl} style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: '10px 14px', textAlign: 'center', minWidth: 90 }}>
                <div style={{ fontSize: '.6rem', color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '.5px' }}>{lbl}</div>
                <div style={{ fontSize: '.95rem', fontWeight: 700, marginTop: 2, color: clr }}>{val}</div>
              </div>
            ))}
          </div>
          {legs.length > 0 && (
            <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: 12, marginTop: 10 }}>
              <PayoffChart legs={payoffLegs} lotSize={lot} spot={spot} sym={sym} />
            </div>
          )}
          <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 12, flexWrap: 'wrap' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}><label style={{ fontSize: 10, color: 'var(--muted)' }}>Max Profit $</label><input type="number" value={maxProfit} onChange={e => setMaxProfit(+e.target.value)} style={{ width: 80, padding: '4px 8px', border: '1px solid var(--border)', borderRadius: 3 }} /></div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}><label style={{ fontSize: 10, color: 'var(--muted)' }}>Max Loss $</label><input type="number" value={maxLoss} onChange={e => setMaxLoss(+e.target.value)} style={{ width: 80, padding: '4px 8px', border: '1px solid var(--border)', borderRadius: 3 }} /></div>
            <button className="btn btn-green" onClick={execute} style={{ fontWeight: 700 }}>Execute & Monitor</button>
            <button className="btn btn-outline" onClick={() => setLegs([])}>Clear All</button>
          </div>
        </div>
      )}

      {/* Monitor */}
      {monitorData && (
        <div style={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 6, padding: 16, marginBottom: 12 }}>
          <h3 style={{ fontSize: '.85rem', marginBottom: 8 }}>👁 Live Strategy Monitor</h3>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: '.8rem' }}>
            <div>PnL: <b style={{ color: (monitorData.current_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>{sym}{(monitorData.current_pnl || 0).toFixed(2)}</b></div>
            <div>Status: <b>{monitorData.running ? '🟢 Live' : '⚫ Stopped'}</b></div>
          </div>
        </div>
      )}

      {/* Chain Table */}
      <div className="chain-wrap">
        <table>
          <thead>
            <tr>
              <th colSpan={7} style={{ textAlign: 'center', fontSize: 11, letterSpacing: 1, borderBottom: '2px solid var(--green)', color: 'var(--green)' }}>CALLS</th>
              <th style={{ background: 'var(--accent)', color: '#fff', fontSize: 11, minWidth: 80, borderLeft: '2px solid var(--accent)', borderRight: '2px solid var(--accent)' }}>STRIKE</th>
              <th colSpan={7} style={{ textAlign: 'center', fontSize: 11, letterSpacing: 1, borderBottom: '2px solid var(--red)', color: 'var(--red)' }}>PUTS</th>
            </tr>
            <tr>
              {['OI', 'Vol', 'IV', 'Delta', 'Bid', 'Ask', 'Mark'].map(h => <th key={'c' + h} style={{ color: 'var(--green)' }}>{h}</th>)}
              <th style={{ background: 'var(--accent)', color: '#fff' }}>Strike</th>
              {['Mark', 'Bid', 'Ask', 'Delta', 'IV', 'Vol', 'OI'].map(h => <th key={'p' + h} style={{ color: 'var(--red)' }}>{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {chain.map((row, i) => {
              const s = parseFloat(row.strike);
              const c = row.call || {}, p = row.put || {};
              const cITM = spot && s < spot, pITM = spot && s > spot;
              const isAtm = i === atmIdx;
              const cOI = isCrypto ? Math.round((parseFloat(c.oi) || 0) / lot) : Math.round(parseFloat(c.oi) || 0);
              const pOI = isCrypto ? Math.round((parseFloat(p.oi) || 0) / lot) : Math.round(parseFloat(p.oi) || 0);
              return (
                <React.Fragment key={row.strike}>
                  {isAtm && <tr><td colSpan={7} style={{ height: 3, padding: 0, background: 'linear-gradient(90deg,transparent,var(--accent),transparent)' }} /><td style={{ position: 'relative', padding: 0 }}><span style={{ position: 'absolute', top: -14, left: '50%', transform: 'translateX(-50%)', background: 'var(--accent)', color: '#fff', fontSize: 9, padding: '2px 10px', borderRadius: 3, fontWeight: 700, whiteSpace: 'nowrap' }}>ATM {spot?.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span></td><td colSpan={7} style={{ height: 3, padding: 0, background: 'linear-gradient(90deg,transparent,var(--accent),transparent)' }} /></tr>}
                  <tr>
                    <td className={`call-side ${cITM ? 'itm' : ''}`}>{cOI || '—'}</td>
                    <td className={`call-side ${cITM ? 'itm' : ''}`}>{c.volume || '—'}</td>
                    <td className={`call-side ${cITM ? 'itm' : ''}`}>{c.iv ? (c.iv * 100).toFixed(1) + '%' : '—'}</td>
                    <td className={`call-side ${cITM ? 'itm' : ''}`} style={{ fontWeight: 600 }}>{f4(c.delta)}</td>
                    <td className={`call-side ${cITM ? 'itm' : ''} selectable`} onClick={() => addLeg(row, 'call', 'buy')} onContextMenu={e => { e.preventDefault(); addLeg(row, 'call', 'sell'); }}>{f2(c.bid)}</td>
                    <td className={`call-side ${cITM ? 'itm' : ''} selectable`} onClick={() => addLeg(row, 'call', 'buy')} onContextMenu={e => { e.preventDefault(); addLeg(row, 'call', 'sell'); }}>{f2(c.ask)}</td>
                    <td className={`call-side ${cITM ? 'itm' : ''} selectable`} onClick={() => addLeg(row, 'call', 'buy')} onContextMenu={e => { e.preventDefault(); addLeg(row, 'call', 'sell'); }} style={{ fontWeight: 700, color: 'var(--green)' }}>{f2(c.mark_price)}</td>
                    <td className="strike-col">{Number(row.strike).toLocaleString()}</td>
                    <td className={`put-side ${pITM ? 'itm' : ''} selectable`} onClick={() => addLeg(row, 'put', 'buy')} onContextMenu={e => { e.preventDefault(); addLeg(row, 'put', 'sell'); }} style={{ fontWeight: 700, color: 'var(--red)' }}>{f2(p.mark_price)}</td>
                    <td className={`put-side ${pITM ? 'itm' : ''} selectable`} onClick={() => addLeg(row, 'put', 'buy')} onContextMenu={e => { e.preventDefault(); addLeg(row, 'put', 'sell'); }}>{f2(p.bid)}</td>
                    <td className={`put-side ${pITM ? 'itm' : ''} selectable`} onClick={() => addLeg(row, 'put', 'buy')} onContextMenu={e => { e.preventDefault(); addLeg(row, 'put', 'sell'); }}>{f2(p.ask)}</td>
                    <td className={`put-side ${pITM ? 'itm' : ''}`} style={{ fontWeight: 600 }}>{f4(p.delta)}</td>
                    <td className={`put-side ${pITM ? 'itm' : ''}`}>{p.iv ? (p.iv * 100).toFixed(1) + '%' : '—'}</td>
                    <td className={`put-side ${pITM ? 'itm' : ''}`}>{p.volume || '—'}</td>
                    <td className={`put-side ${pITM ? 'itm' : ''}`}>{pOI || '—'}</td>
                  </tr>
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
