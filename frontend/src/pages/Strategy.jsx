import React, { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../api';

export default function Strategy() {
  const [sp] = useSearchParams();
  const asset = sp.get('asset') || 'BTC';
  const [profiles, setProfiles] = useState([]);
  const [sid, setSid] = useState(null);
  const [running, setRunning] = useState(false);
  const [logs, setLogs] = useState([]);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const logRef = useRef(null);
  const esRef = useRef(null);
  const pollRef = useRef(null);

  const [form, setForm] = useState({
    expiry_date: '', lot_size: 10, target_delta: 0.20, delta_tolerance: 0.05,
    premium_threshold: 40, target_pnl: 10, monitoring_interval: 5,
    max_adjustments: 10, profile_id: ''
  });

  useEffect(() => {
    api.get('/profiles').then(r => {
      const p = r.data.profiles || [];
      setProfiles(p);
      if (p.length && !form.profile_id) setForm(f => ({ ...f, profile_id: p[0].id }));
    });
    return () => { esRef.current?.close(); clearInterval(pollRef.current); };
  }, []);

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  const start = async () => {
    setError('');
    try {
      const { data } = await api.post('/start', { ...form, asset, premium_threshold: form.premium_threshold / 100 });
      setSid(data.sid);
      setRunning(true);
      setLogs([]);
      connectStream(data.sid);
      startPoll(data.sid);
    } catch (e) { setError(e.response?.data?.error || 'Failed to start'); }
  };

  const stop = async () => {
    if (!sid) return;
    await api.post('/stop', { sid });
    setRunning(false);
    esRef.current?.close();
    clearInterval(pollRef.current);
  };

  const connectStream = id => {
    esRef.current?.close();
    const token = localStorage.getItem('token');
    const es = new EventSource(`/api/stream/${id}?token=${token}`);
    es.onmessage = e => {
      setLogs(l => [...l, e.data]);
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    };
    esRef.current = es;
  };

  const startPoll = id => {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await api.get(`/status/${id}`);
        setStatus(data);
        if (data.status !== 'running') { setRunning(false); clearInterval(pollRef.current); esRef.current?.close(); }
      } catch {}
    }, 3000);
  };

  const s = status || {};
  const call = s.call_leg || {};
  const put = s.put_leg || {};

  return (
    <div className="container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <div className="page-title" style={{ marginBottom: 0 }}>Delta Neutral — {asset}</div>
        <div style={{ display: 'flex', gap: 8 }}>
          <a href="/strategy/new?asset=BTC" className="btn btn-outline">₿ New BTC Strategy</a>
          <a href="/strategy/new?asset=ETH" className="btn btn-outline">⟠ New ETH Strategy</a>
        </div>
      </div>

      {error && <div className="error-msg">{error}</div>}

      <div className="card">
        <div className="grid-3">
          <div className="field"><label>Expiry Date</label><input value={form.expiry_date} onChange={e => set('expiry_date', e.target.value)} placeholder="DD-MM-YYYY" /></div>
          <div className="field"><label>Lot Size</label><input type="number" value={form.lot_size} onChange={e => set('lot_size', +e.target.value)} /></div>
          <div className="field"><label>Target Delta</label><input type="number" step="0.01" value={form.target_delta} onChange={e => set('target_delta', +e.target.value)} /></div>
          <div className="field"><label>Delta Tolerance</label><input type="number" step="0.01" value={form.delta_tolerance} onChange={e => set('delta_tolerance', +e.target.value)} /></div>
          <div className="field"><label>Premium Threshold (%)</label><input type="number" value={form.premium_threshold} onChange={e => set('premium_threshold', +e.target.value)} /></div>
          <div className="field"><label>Target P&L ($)</label><input type="number" value={form.target_pnl} onChange={e => set('target_pnl', +e.target.value)} /></div>
          <div className="field"><label>Monitor Interval (s)</label><input type="number" value={form.monitoring_interval} onChange={e => set('monitoring_interval', +e.target.value)} /></div>
          <div className="field"><label>Max Adjustments</label><input type="number" value={form.max_adjustments} onChange={e => set('max_adjustments', +e.target.value)} /></div>
          <div className="field"><label>Profile</label>
            <select value={form.profile_id} onChange={e => set('profile_id', e.target.value)}>
              {profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
          <button className="btn btn-green" onClick={start} disabled={running}>▶ Start</button>
          <button className="btn btn-red" onClick={stop} disabled={!running}>■ Stop</button>
          {sid && <span style={{ fontSize: '.8rem', color: 'var(--muted)', alignSelf: 'center' }}>SID: {sid}</span>}
        </div>
      </div>

      {status && (
        <>
          <div className="top-stats" style={{ gridTemplateColumns: 'repeat(4,1fr)' }}>
            <div className="stat-card"><div className="label">Total P&L</div><div className="value" style={{ color: s.total_pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${(s.total_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Realized</div><div className="value">${(s.realized_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Unrealized</div><div className="value">${(s.unrealized_pnl || 0).toFixed(2)}</div></div>
            <div className="stat-card"><div className="label">Adjustments</div><div className="value">{s.adjustment_count || 0}</div></div>
          </div>

          <div className="grid-2">
            {[['CALL', call], ['PUT', put]].map(([label, leg]) => (
              <div className="card" key={label}>
                <div style={{ fontWeight: 700, marginBottom: 10 }}>{label} Leg</div>
                {leg.symbol ? (
                  <table style={{ width: '100%', fontSize: '.85rem' }}>
                    <tbody>
                      {[['Symbol', leg.symbol], ['Strike', leg.strike], ['Entry', `$${(leg.entry_price || 0).toFixed(2)}`], ['Mark', `$${(leg.mark_price || 0).toFixed(2)}`], ['Delta', leg.delta], ['Size', leg.size], ['Payoff', <span style={{ color: (leg.payoff || 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>${(leg.payoff || 0).toFixed(2)}</span>]].map(([k, v]) => (
                        <tr key={k}><td style={{ padding: '4px 0', color: 'var(--muted)' }}>{k}</td><td style={{ padding: '4px 0', fontWeight: 600, textAlign: 'right' }}>{v}</td></tr>
                      ))}
                    </tbody>
                  </table>
                ) : <div style={{ color: 'var(--muted)', fontSize: '.85rem' }}>No position</div>}
              </div>
            ))}
          </div>
        </>
      )}

      <div className="card">
        <div style={{ fontWeight: 700, marginBottom: 10 }}>Live Logs</div>
        <div ref={logRef} style={{ background: '#0f172a', color: '#e2e8f0', padding: 14, borderRadius: 8, height: 260, overflowY: 'auto', fontFamily: 'monospace', fontSize: '.78rem', whiteSpace: 'pre-wrap' }}>
          {logs.length ? logs.map((l, i) => <div key={i}>{l}</div>) : <span style={{ color: '#64748b' }}>Waiting for logs...</span>}
        </div>
      </div>
    </div>
  );
}
