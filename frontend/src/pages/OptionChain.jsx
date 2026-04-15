import React, { useState, useEffect, useRef } from 'react';
import api from '../api';
import PayoffChart from '../components/PayoffChart';
import OptionChainTable from '../components/OptionChainTable';
import { PositionGrid } from '../components/PositionCard';

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
  const [openPositions, setOpenPositions] = useState([]);
  const [leftTab, setLeftTab] = useState('chain');
  const [rightTab, setRightTab] = useState('payoff');
  const [hoveredRow, setHoveredRow] = useState(null);
  const monitorRef = useRef(null);

  const isCrypto = CRYPTO.has(asset);
  const sym = isCrypto ? '$' : '₹';
  const lot = LOT_SIZE[asset] || 1;

  const loadPositions = () => {
    api.get('/tracked-positions', { params: { profile_id: profileId } }).then(r => {
      setOpenPositions(r.data.positions || []);
    }).catch(() => {});
  };

  useEffect(() => { loadPositions(); const pt = setInterval(loadPositions, 10000); return () => clearInterval(pt); }, []);

  const closePosition = (p) => {
    if (!window.confirm(`Close ${p.side.toUpperCase()} ${p.size} lots of ${p.symbol}?`)) return;
    api.post('/close-position', { product_id: p.product_id, symbol: p.symbol, size: p.size, side: p.side, profile_id: profileId })
      .then(() => loadPositions());
  };

  useEffect(() => {
    api.get('/profiles').then(r => { const p = r.data.profiles || []; setProfiles(p); if (p.length) setProfileId(p[0].id); });
  }, []);

  const switchMode = m => {
    setMode(m);
    const match = profiles.find(p => p.broker === (m === 'live' ? 'delta_exchange' : 'demo'));
    if (match) setProfileId(match.id);
  };

  useEffect(() => {
    if (!profileId) return;
    setChain([]); setExpiry(''); setLegs([]);
    api.get('/expiries', { params: { asset, profile_id: profileId } }).then(r => { const e = r.data.expiries || []; setExpiries(e); setExpiry(e[0] || ''); });
  }, [asset, profileId]);

  useEffect(() => {
    if (!expiry || !profileId) return;
    api.get('/chain', { params: { asset, expiry, profile_id: profileId } }).then(r => { setChain(r.data.chain || []); setSpot(r.data.spot_price || 0); });
  }, [asset, expiry, profileId]);

  useEffect(() => {
    if (!monitorId) return;
    const poll = () => api.get(`/monitor/${monitorId}`).then(r => setMonitorData(r.data)).catch(() => {});
    poll(); monitorRef.current = setInterval(poll, 5000);
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

  const execute = () => {
    api.post('/place-legs', {
      legs: legs.map(l => ({ product_id: l.product_id, symbol: l.symbol, size: l.size, side: l.side, type: l.type, strike: l.strike, mark: l.mark })),
      max_profit: maxProfit, max_loss: maxLoss, asset, profile_id: profileId
    }).then(r => {
      if (r.data.monitor_id) setMonitorId(r.data.monitor_id);
      const ok = (r.data.results || []).filter(x => x.success).length;
      alert(`${ok} order(s) placed`);
      if (ok === legs.length) setLegs([]);
      loadPositions();
    });
  };

  const f2 = v => typeof v === 'number' ? v.toFixed(2) : '—';
  const f4 = v => typeof v === 'number' ? v.toFixed(4) : '—';

  return (
    <div className="at-layout">
      {/* ═══ LEFT PANEL ═══ */}
      <div className="at-left">
        {/* Header row */}
        <div className="at-header">
          <div className="at-header-row">
            <select value={asset} onChange={e => setAsset(e.target.value)} className="at-sel">{ASSETS.map(a => <option key={a}>{a}</option>)}</select>
            <div className="at-spot">{spot ? spot.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—'}</div>
            {isCrypto && (
              <div className="at-mode-toggle">
                <span className={mode === 'live' ? 'active green' : ''} onClick={() => switchMode('live')}>Live</span>
                <span className={mode === 'demo' ? 'active amber' : ''} onClick={() => switchMode('demo')}>Demo</span>
              </div>
            )}
            <select value={profileId} onChange={e => setProfileId(e.target.value)} className="at-sel">
              <option value="">Default</option>
              {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </div>
          {/* Left tabs */}
          <div className="at-tabs">
            <span className={leftTab === 'chain' ? 'active' : ''} onClick={() => setLeftTab('chain')}>Option Chain</span>
            <span className={leftTab === 'positions' ? 'active' : ''} onClick={() => setLeftTab('positions')}>Positions</span>
          </div>
        </div>

        {leftTab === 'chain' ? (
          <>
            {/* Expiry row */}
            <div className="at-expiry-row">
              {expiries.map((e, i) => (
                <span key={e} className={`at-expiry ${e === expiry ? 'active' : ''}`} onClick={() => setExpiry(e)}>{e}</span>
              ))}
              <span className="at-lot-info">Lot Size: {lot}</span>
            </div>

            {/* Chain table */}
            <OptionChainTable chain={chain} spot={spot} sym={sym} lot={lot} isCrypto={isCrypto} legs={legs} onAddLeg={addLeg} />

            {/* Bottom bar */}
            <div className="at-bottom-bar">
              <button className="at-btn outline" onClick={() => setLegs([])}>Clear</button>
              <div style={{ flex: 1 }} />
              <div className="at-bottom-controls">
                <label>TP {sym}<input type="number" value={maxProfit} onChange={e => setMaxProfit(+e.target.value)} /></label>
                <label>SL {sym}<input type="number" value={maxLoss} onChange={e => setMaxLoss(+e.target.value)} /></label>
              </div>
              <button className="at-btn green" onClick={execute} disabled={!legs.length}>
                Live Trade ▾
              </button>
            </div>
          </>
        ) : (
          <div style={{ flex: 1, overflow: 'auto', padding: 16 }}>
            <PositionGrid positions={openPositions} sym={sym} onClose={closePosition} onRefresh={loadPositions} />
          </div>
        )}
      </div>

      {/* ═══ RIGHT PANEL ═══ */}
      <div className="at-right">
        {/* Top bar with ticker */}
        <div className="at-right-header">
          <span style={{ fontWeight: 700 }}>{asset} {spot ? spot.toLocaleString(undefined, { maximumFractionDigits: 2 }) : ''}</span>
          <span style={{ color: 'var(--muted)', fontSize: 11 }}>{new Date().toLocaleTimeString()}</span>
        </div>

        {/* Right tabs */}
        <div className="at-tabs">
          <span className={rightTab === 'payoff' ? 'active' : ''} onClick={() => setRightTab('payoff')}>Payoff</span>
          <span className={rightTab === 'greeks' ? 'active' : ''} onClick={() => setRightTab('greeks')}>Greeks</span>
        </div>

        <div className="at-right-body">
          {rightTab === 'payoff' && legs.length > 0 && (
            <>
              <PayoffChart legs={legs} lotSize={lot} spot={spot} sym={sym} height={160} />

              {monitorData && (
                <div className="at-monitor">
                  <span>👁 Monitor</span>
                  <span style={{ color: (monitorData.current_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>{sym}{(monitorData.current_pnl || 0).toFixed(2)}</span>
                  <span>{monitorData.running ? '🟢' : '⚫'}</span>
                </div>
              )}
            </>
          )}

          {rightTab === 'payoff' && !legs.length && (
            <div className="at-empty">
              <div style={{ fontSize: 48, marginBottom: 12 }}>📊</div>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Select options from the chain</div>
              <div style={{ fontSize: 11 }}>Use B (Buy) and S (Sell) buttons on hover</div>
            </div>
          )}

          {rightTab === 'greeks' && (
            <div style={{ padding: 4 }}>
              {legs.length > 0 ? (
                <table className="at-greeks-table">
                  <thead><tr>{['Type', 'Side', 'Strike', 'Delta', 'IV', 'Mark', 'Lots'].map(h => <th key={h}>{h}</th>)}</tr></thead>
                  <tbody>
                    {legs.map((l, i) => (
                      <tr key={i}>
                        <td>{l.type.toUpperCase()}</td>
                        <td style={{ color: l.side === 'buy' ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>{l.side.toUpperCase()}</td>
                        <td>{Number(l.strike).toLocaleString()}</td>
                        <td>{f4(l.delta)}</td>
                        <td>{l.iv ? (l.iv * 100).toFixed(1) + '%' : '—'}</td>
                        <td>{sym}{f2(l.mark)}</td>
                        <td>
                          <input type="number" min={1} value={l.size} onChange={e => updateSize(i, +e.target.value)}
                            style={{ width: 44, padding: '2px 4px', border: '1px solid var(--border)', borderRadius: 3, textAlign: 'center', fontSize: 11 }} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <div className="at-empty">No legs selected</div>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
