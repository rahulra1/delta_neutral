import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../api';

// Option-chain replay on REAL Groww historical 1-minute candles (no pricing model).
const OPEN = 9 * 60 + 15;
const CLOSE = 15 * 60 + 30;
const POS_KEY = 'optionReplayPositions';
const STATE_KEY = 'optionReplayState';
const STEPS = [{ m: 5, l: '5m' }, { m: 15, l: '15m' }, { m: 30, l: '30m' }, { m: 60, l: '1h' }];

const hhmm = m => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
const toMin = s => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };
const money = v => (v == null ? '–' : `${v >= 0 ? '+' : '−'}₹${Math.abs(Math.round(v)).toLocaleString('en-IN')}`);
const px = v => (v == null ? '–' : Number(v).toFixed(2));
const big = v => (v == null ? '–' : v >= 1e7 ? `${(v / 1e7).toFixed(2)}Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : String(Math.round(v)));
const col = v => ((v || 0) >= 0 ? 'var(--green)' : 'var(--red)');
const stamp = (d, m) => `${d} ${hhmm(m)}`;

// Groww F&O option charges for one round trip (same formula as strategy/nse_sector_options.estimate_charges)
function charges(buyValue, sellValue) {
  const brokerage = 40;
  const txn = 0.0003503 * (buyValue + sellValue);
  const sebi = 0.000001 * (buyValue + sellValue);
  return brokerage + txn + sebi + 0.001 * sellValue + 0.00003 * buyValue + 0.18 * (brokerage + txn + sebi);
}

function lastWeekday() {
  const d = new Date(Date.now() + 5.5 * 3600e3);
  do { d.setUTCDate(d.getUTCDate() - 1); } while ([0, 6].includes(d.getUTCDay()));
  return d.toISOString().slice(0, 10);
}

function load(key, dflt) {
  try { return JSON.parse(localStorage.getItem(key)) ?? dflt; } catch { return dflt; }
}

function legPnl(l, price) {
  if (price == null) return null;
  const sign = l.side === 'BUY' ? 1 : -1;
  return (price - l.entry.price) * l.qty * sign;
}

function legCharges(l, exitPrice) {
  if (exitPrice == null) return 0;
  const a = l.entry.price * l.qty, b = exitPrice * l.qty;
  return l.side === 'BUY' ? charges(a, b) : charges(b, a);
}

export default function OptionReplay() {
  const saved = load(STATE_KEY, {});
  const [meta, setMeta] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const [profileId, setProfileId] = useState(saved.profileId || '');
  const [symbol, setSymbol] = useState(saved.symbol || 'NIFTY');
  const [symInput, setSymInput] = useState(saved.symbol || 'NIFTY');
  const [date, setDate] = useState(saved.date || lastWeekday());
  const [minute, setMinute] = useState(saved.minute || OPEN + 5);
  const [expiry, setExpiry] = useState(saved.expiry || '');
  const [width, setWidth] = useState(saved.width || 10);
  const [lots, setLots] = useState(1);
  const [lotOverride, setLotOverride] = useState('');
  const [chain, setChain] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [moving, setMoving] = useState(false);
  const [positions, setPositions] = useState(() => load(POS_KEY, []));
  const [quotes, setQuotes] = useState({});
  const reqId = useRef(0);

  useEffect(() => {
    api.get('/option-replay/meta').then(r => setMeta(r.data)).catch(() => {});
    api.get('/profiles').then(r => setProfiles((r.data.profiles || []).filter(p => p.broker === 'groww'))).catch(() => {});
  }, []);
  useEffect(() => { localStorage.setItem(POS_KEY, JSON.stringify(positions)); }, [positions]);
  useEffect(() => {
    localStorage.setItem(STATE_KEY, JSON.stringify({ profileId, symbol, date, minute, expiry, width }));
  }, [profileId, symbol, date, minute, expiry, width]);

  const params = useMemo(() => (profileId ? { profile_id: profileId } : {}), [profileId]);
  const errText = e => e.response?.data?.error || e.message;

  // ---- chain
  useEffect(() => {
    const id = ++reqId.current;
    setLoading(true);
    api.get('/option-replay/chain', { params: { ...params, symbol, date, time: hhmm(minute), expiry: expiry || undefined, width } })
      .then(r => {
        if (id !== reqId.current) return;
        setChain(r.data); setErr(null);
        if (r.data.expiry !== expiry) setExpiry(r.data.expiry);
      })
      .catch(e => { if (id === reqId.current) { setErr({ text: errText(e), code: e.response?.data?.code }); setChain(null); } })
      .finally(() => { if (id === reqId.current) setLoading(false); });
  }, [params, symbol, date, minute, expiry, width]);

  // ---- position quotes at the current moment
  const openLegs = useMemo(() => positions.filter(p => !p.exit), [positions]);
  useEffect(() => {
    if (!openLegs.length) { setQuotes({}); return; }
    api.post('/option-replay/quote', { ...params, date, time: hhmm(minute),
      legs: openLegs.map(l => ({ symbol: l.symbol, gsym: l.gsym, strike: l.strike, type: l.type, expiry: l.expiry })) })
      .then(r => setQuotes(Object.fromEntries(openLegs.map((l, i) => [l.id, r.data.quotes[i]]))))
      .catch(() => setQuotes({}));
  }, [params, date, minute, openLegs]);

  // ---- time navigation (continuous session time: past 15:30 rolls into the next trading day's 09:15)
  const nextDay = useCallback(async (d, dir) => {
    const r = await api.get('/option-replay/day', { params: { ...params, symbol, date: d, dir } });
    return r.data.date;
  }, [params, symbol]);

  const move = async mins => {
    setMoving(true);
    try {
      let d = date, m = minute + mins;
      while (m > CLOSE) {
        if (minute < CLOSE && d === date) { m = CLOSE; break; }       // stop at the close first
        const nd = await nextDay(d, 1);
        if (!nd) { m = CLOSE; break; }
        d = nd; m = OPEN + (m - CLOSE);
      }
      while (m < OPEN) {
        if (minute > OPEN && d === date) { m = OPEN; break; }
        const pd = await nextDay(d, -1);
        if (!pd) { m = OPEN; break; }
        d = pd; m = CLOSE - (OPEN - m);
      }
      setDate(d); setMinute(m);
    } catch (e) { setErr({ text: errText(e), code: e.response?.data?.code }); }
    setMoving(false);
  };
  const moveDay = async dir => {
    setMoving(true);
    try { const d = await nextDay(date, dir); if (d) setDate(d); } catch (e) { setErr({ text: errText(e), code: e.response?.data?.code }); }
    setMoving(false);
  };

  // ---- orders
  const lotSize = Number(lotOverride) || chain?.lot_size || 0;
  const now = stamp(date, minute);
  const place = (row, type, side) => {
    const leg = row[type.toLowerCase()];
    if (!leg || leg.ltp == null || !lotSize) return;
    setPositions(ps => [...ps, {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, symbol: chain.symbol, gsym: leg.gsym,
      strike: row.strike, type, expiry: chain.expiry, side, lots: Number(lots) || 1, lotSize,
      qty: (Number(lots) || 1) * lotSize, entry: { date, time: hhmm(minute), price: leg.ltp, at: now }, exit: null,
    }]);
  };
  const closeLeg = l => {
    const q = quotes[l.id];
    if (!q || q.price == null) return;
    setPositions(ps => ps.map(p => (p.id === l.id ? { ...p, exit: { date, time: hhmm(minute), price: q.price, at: now, status: q.status } } : p)));
  };
  const closeAll = () => openLegs.forEach(closeLeg);

  // ---- totals
  const rows = positions.map(l => {
    const notYet = now < l.entry.at;
    const price = l.exit ? l.exit.price : quotes[l.id]?.price;
    const pnl = notYet ? null : legPnl(l, price);
    return { l, notYet, price, pnl, fees: l.exit ? legCharges(l, l.exit.price) : 0 };
  });
  const realized = rows.filter(r => r.l.exit).reduce((s, r) => s + (r.pnl || 0), 0);
  const fees = rows.reduce((s, r) => s + r.fees, 0);
  const openMtm = rows.filter(r => !r.l.exit && r.pnl != null).reduce((s, r) => s + r.pnl, 0);

  const th = { padding: '6px 6px', fontSize: '.64rem', color: 'var(--muted)', fontWeight: 700, borderBottom: '1px solid var(--border)', position: 'sticky', top: 0, background: 'var(--card)', whiteSpace: 'nowrap' };
  const td = { padding: '4px 6px', fontSize: '.76rem', borderBottom: '1px solid var(--border)', whiteSpace: 'nowrap' };
  const btn = { padding: '4px 9px', fontSize: '.72rem', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', cursor: 'pointer', fontFamily: 'inherit', color: 'var(--text)' };
  const bs = (kind) => ({ ...btn, padding: '1px 6px', fontSize: '.62rem', fontWeight: 700, color: '#fff', border: 'none', background: kind === 'BUY' ? 'var(--green)' : 'var(--red)' });
  const input = { padding: '6px 8px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', fontFamily: 'inherit', fontSize: '.82rem', color: 'var(--text)' };
  const label = { display: 'flex', flexDirection: 'column', gap: 2, fontSize: '.64rem', color: 'var(--muted)', fontWeight: 600 };

  const stale = leg => leg?.last_trade && toMin(leg.last_trade) < minute - 5;
  const cell = (row, type, extra) => {
    const leg = row[type];
    if (!leg) return <td style={{ ...td, ...extra, color: 'var(--muted)', textAlign: 'center' }}>–</td>;
    return (
      <td style={{ ...td, ...extra, textAlign: 'right', fontWeight: 700 }} title={leg.ltp == null ? 'No trade yet today' : `Last trade ${leg.last_trade} · day O ${px(leg.open)} H ${px(leg.high)} L ${px(leg.low)}`}>
        {leg.ltp == null ? <span style={{ color: 'var(--muted)', fontWeight: 400 }}>no trade</span> : px(leg.ltp)}
        {stale(leg) && <div style={{ fontSize: '.56rem', color: 'var(--muted)', fontWeight: 400 }}>@{leg.last_trade}</div>}
      </td>
    );
  };
  const buttons = (row, type) => {
    const leg = row[type.toLowerCase()];
    const ok = leg && leg.ltp != null && lotSize;
    return (
      <td style={{ ...td, textAlign: 'center' }}>
        <button type="button" style={{ ...bs('BUY'), opacity: ok ? 1 : 0.3 }} disabled={!ok} onClick={() => place(row, type, 'BUY')} aria-label={`Buy ${row.strike} ${type}`}>B</button>{' '}
        <button type="button" style={{ ...bs('SELL'), opacity: ok ? 1 : 0.3 }} disabled={!ok} onClick={() => place(row, type, 'SELL')} aria-label={`Sell ${row.strike} ${type}`}>S</button>
      </td>
    );
  };

  return (
    <div className="container" style={{ maxWidth: 1500 }}>
      <h1 className="page-title" style={{ marginBottom: 4 }}>⏪ Option Replay</h1>
      <div style={{ fontSize: '.76rem', color: 'var(--muted)', marginBottom: 14 }}>
        Go back to any session, pick an expiry, see the option chain as it traded at that minute, place paper trades and step time forward to watch the P&L.
        Every price is a REAL traded price from {meta?.source || 'Groww historical 1-minute candles'} — the close of the last completed 1-minute candle. No pricing model.
      </div>

      {/* ---- controls */}
      <div className="card" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        {profiles.length > 1 && (
          <label style={label}>Groww profile
            <select style={input} value={profileId} onChange={e => setProfileId(e.target.value)}>
              <option value="">First Groww profile</option>
              {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
        )}
        <label style={label}>Underlying
          <input style={{ ...input, width: 130 }} list="replay-symbols" value={symInput}
            onChange={e => setSymInput(e.target.value.toUpperCase())}
            onKeyDown={e => { if (e.key === 'Enter' && symInput) { setSymbol(symInput); setExpiry(''); } }}
            onBlur={() => { if (symInput && symInput !== symbol) { setSymbol(symInput); setExpiry(''); } }}
            title="Index (NIFTY, BANKNIFTY, SENSEX...) or any F&O stock (RELIANCE...). Press Enter." />
          <datalist id="replay-symbols">{(meta?.indices || []).map(s => <option key={s} value={s} />)}</datalist>
        </label>
        <label style={label}>Date
          <input type="date" style={input} value={date} min="2020-01-01" max={new Date().toISOString().slice(0, 10)}
            onChange={e => e.target.value && setDate(e.target.value)} />
        </label>
        <label style={label}>Time (IST)
          <input type="time" style={input} value={hhmm(minute)} min="09:15" max="15:30" step="60"
            onChange={e => { if (!e.target.value) return; const m = toMin(e.target.value); setMinute(Math.min(CLOSE, Math.max(OPEN, m))); }} />
        </label>
        <label style={label}>Expiry
          <select style={input} value={expiry} onChange={e => setExpiry(e.target.value)}>
            {(chain?.expiries || (expiry ? [expiry] : [])).map(x => <option key={x} value={x}>{x}</option>)}
          </select>
        </label>
        <label style={label}>Strikes ± ATM
          <select style={input} value={width} onChange={e => setWidth(Number(e.target.value))}>
            {[5, 10, 15, 20].map(w => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
        <label style={label}>Lots / order
          <input type="number" min="1" style={{ ...input, width: 70 }} value={lots} onChange={e => setLots(Math.max(1, Number(e.target.value) || 1))} />
        </label>
        <label style={label} title="Official lot size from the NSE/BSE bhavcopy of that day. Type a value if it is not available.">Lot size
          <input type="number" min="1" style={{ ...input, width: 80 }} placeholder={chain?.lot_size ? String(chain.lot_size) : 'enter'} value={lotOverride}
            onChange={e => setLotOverride(e.target.value)} />
        </label>
      </div>

      {/* ---- time stepper */}
      <div className="card" style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', justifyContent: 'center' }}>
        <button type="button" style={btn} disabled={moving} onClick={() => moveDay(-1)}>« 1 day</button>
        {[...STEPS].reverse().map(s => <button key={`b${s.m}`} type="button" style={btn} disabled={moving} onClick={() => move(-s.m)}>− {s.l}</button>)}
        <div style={{ minWidth: 190, textAlign: 'center' }}>
          <div style={{ fontWeight: 800, fontSize: '1.05rem' }}>{date} · {hhmm(minute)}</div>
          <div style={{ fontSize: '.66rem', color: 'var(--muted)' }}>{loading || moving ? 'loading…' : chain ? `${chain.symbol} ${px(chain.spot)}${chain.spot_open ? ` (open ${px(chain.spot_open)})` : ''}` : '—'}</div>
        </div>
        {STEPS.map(s => <button key={`f${s.m}`} type="button" style={btn} disabled={moving} onClick={() => move(s.m)}>+ {s.l}</button>)}
        <button type="button" style={btn} disabled={moving} onClick={() => moveDay(1)}>1 day »</button>
        <span style={{ width: 12 }} />
        <button type="button" style={btn} disabled={moving} onClick={() => setMinute(OPEN + 1)}>Open</button>
        <button type="button" style={btn} disabled={moving} onClick={() => setMinute(CLOSE)}>Close</button>
      </div>

      {err && (
        <div className="card" style={{ color: 'var(--red)' }} role="alert">
          {err.text}
          {(err.code === 'groww_auth' || err.code === 'no_groww_profile') && <> — <Link to="/broker">open the Broker page</Link></>}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 3fr) minmax(320px, 2fr)', gap: 14, alignItems: 'start' }}>
        {/* ---- option chain */}
        <div className="card" style={{ marginBottom: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8, flexWrap: 'wrap', gap: 6 }}>
            <div style={{ fontWeight: 700 }}>
              Option chain {chain && <span style={{ color: 'var(--muted)', fontWeight: 500, fontSize: '.78rem' }}>· {chain.symbol} {chain.expiry} · {chain.dte} days to expiry · lot {lotSize || '?'} · ATM {chain.atm}</span>}
            </div>
            <div style={{ fontSize: '.64rem', color: 'var(--muted)' }}>LTP = last trade up to {hhmm(minute)} · OI / volume from the 1-minute candles</div>
          </div>
          {chain && (
            <div style={{ overflowX: 'auto', maxHeight: 640 }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th scope="col" style={th}>CE OI</th><th scope="col" style={th}>CE Vol</th><th scope="col" style={th}>CE LTP</th><th scope="col" style={th}>Trade</th>
                    <th scope="col" style={{ ...th, textAlign: 'center' }}>Strike</th>
                    <th scope="col" style={th}>Trade</th><th scope="col" style={th}>PE LTP</th><th scope="col" style={th}>PE Vol</th><th scope="col" style={th}>PE OI</th>
                  </tr>
                </thead>
                <tbody>
                  {chain.rows.map(r => {
                    const atm = r.strike === chain.atm;
                    const ceItm = chain.spot != null && r.strike < chain.spot;
                    const peItm = chain.spot != null && r.strike > chain.spot;
                    const ce = { background: ceItm ? 'var(--light-green)' : undefined };
                    const pe = { background: peItm ? 'var(--light-red)' : undefined };
                    return (
                      <tr key={r.strike} style={atm ? { outline: '2px solid var(--purple)' } : undefined}>
                        <td style={{ ...td, ...ce, textAlign: 'right' }}>{big(r.ce?.oi)}</td>
                        <td style={{ ...td, ...ce, textAlign: 'right' }}>{big(r.ce?.volume)}</td>
                        {cell(r, 'ce', ce)}
                        {buttons(r, 'CE')}
                        <td style={{ ...td, textAlign: 'center', fontWeight: 800, background: 'var(--bg)' }}>{r.strike}</td>
                        {buttons(r, 'PE')}
                        {cell(r, 'pe', pe)}
                        <td style={{ ...td, ...pe, textAlign: 'right' }}>{big(r.pe?.volume)}</td>
                        <td style={{ ...td, ...pe, textAlign: 'right' }}>{big(r.pe?.oi)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {!chain && !err && <div style={{ color: 'var(--muted)', fontSize: '.82rem' }}>{loading ? 'Downloading the chain from Groww (first load of a day can take a few seconds)…' : '—'}</div>}
        </div>

        {/* ---- positions */}
        <div className="card" style={{ marginBottom: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <div style={{ fontWeight: 700 }}>Paper positions</div>
            <div style={{ display: 'flex', gap: 6 }}>
              <button type="button" style={btn} disabled={!openLegs.length} onClick={closeAll}>Square off all</button>
              <button type="button" style={btn} disabled={!positions.length} onClick={() => window.confirm('Remove all positions?') && setPositions([])}>Clear</button>
            </div>
          </div>
          <div className="top-stats" style={{ gridTemplateColumns: 'repeat(3,1fr)', marginBottom: 10 }}>
            <div className="stat-card"><div className="label">Open MTM</div><div className="value" style={{ color: col(openMtm), fontSize: '1rem' }}>{money(openMtm)}</div></div>
            <div className="stat-card"><div className="label">Realized (gross)</div><div className="value" style={{ color: col(realized), fontSize: '1rem' }}>{money(realized)}</div></div>
            <div className="stat-card"><div className="label">Net after est. charges</div><div className="value" style={{ color: col(realized + openMtm - fees), fontSize: '1rem' }}>{money(realized + openMtm - fees)}</div><div className="sub">charges ₹{Math.round(fees).toLocaleString('en-IN')} (closed legs)</div></div>
          </div>
          {!positions.length && <div style={{ color: 'var(--muted)', fontSize: '.8rem' }}>Click B (buy) or S (sell) next to a price in the chain to open a position at that minute's real price.</div>}
          {positions.length > 0 && (
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr>{['Contract', 'Side', 'Qty', 'Entry', 'Now / Exit', 'P&L', ''].map(h => <th key={h} scope="col" style={{ ...th, textAlign: 'left' }}>{h}</th>)}</tr></thead>
                <tbody>
                  {rows.map(({ l, notYet, price, pnl }) => {
                    const q = quotes[l.id];
                    return (
                      <tr key={l.id} style={{ opacity: l.exit ? 0.65 : 1 }}>
                        <td style={td}><b>{l.symbol} {l.strike} {l.type}</b><div style={{ fontSize: '.6rem', color: 'var(--muted)' }}>exp {l.expiry}</div></td>
                        <td style={{ ...td, color: l.side === 'BUY' ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>{l.side}</td>
                        <td style={td}>{l.qty}<div style={{ fontSize: '.6rem', color: 'var(--muted)' }}>{l.lots} × {l.lotSize}</div></td>
                        <td style={td}>{px(l.entry.price)}<div style={{ fontSize: '.6rem', color: 'var(--muted)' }}>{l.entry.date} {l.entry.time}</div></td>
                        <td style={td}>
                          {notYet ? <span style={{ color: 'var(--muted)' }}>before entry</span> : px(price)}
                          <div style={{ fontSize: '.6rem', color: 'var(--muted)' }}>{l.exit ? `closed ${l.exit.date} ${l.exit.time}${l.exit.status === 'expired' ? ' (expiry)' : ''}` : (q?.note || '')}</div>
                        </td>
                        <td style={{ ...td, fontWeight: 700, color: col(pnl) }}>{pnl == null ? '–' : money(pnl)}</td>
                        <td style={td}>
                          {!l.exit && <button type="button" style={btn} disabled={notYet || !q || q.price == null} onClick={() => closeLeg(l)}>Exit</button>}
                          {' '}<button type="button" style={{ ...btn, padding: '4px 7px' }} aria-label="Remove position" onClick={() => setPositions(ps => ps.filter(p => p.id !== l.id))}>✕</button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <div style={{ fontSize: '.62rem', color: 'var(--muted)', marginTop: 8 }}>
            Fills at the last traded price (no bid-ask spread). Positions held past expiry settle at intrinsic value vs the underlying's last price on expiry day.
            Stepping back before a position's entry shows it as "before entry". Positions are saved in this browser.
          </div>
        </div>
      </div>
    </div>
  );
}
