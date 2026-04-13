import React, { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../api';

export default function StrategyLogs() {
  const { sid } = useParams();
  const nav = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const logRef = useRef(null);
  const autoScroll = useRef(true);

  const load = () => {
    api.get(`/tracker/${sid}/logs`, { params: { last: 500 } })
      .then(r => { setData(r.data); setError(null); })
      .catch(e => setError(e.response?.data?.error || 'Failed to load'));
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [sid]);

  useEffect(() => {
    if (autoScroll.current && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [data?.logs]);

  const handleClose = () => {
    if (window.confirm('Close this strategy and all positions?')) {
      api.post(`/tracker/${sid}/close`).then(load);
    }
  };

  if (error) return <div className="container"><div className="error-msg">{error}</div><button className="btn btn-outline" onClick={() => nav(-1)}>← Back</button></div>;
  if (!data) return <div className="container">Loading...</div>;

  const pnlColor = data.pnl >= 0 ? 'var(--green)' : 'var(--red)';

  return (
    <div className="container" style={{ maxWidth: 1000 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <button className="btn btn-outline" onClick={() => nav(-1)} style={{ padding: '4px 12px', fontSize: '.8rem', marginRight: 12 }}>← Back</button>
          <span style={{ fontSize: '1.2rem', fontWeight: 800 }}>Strategy Logs</span>
          <span style={{ fontSize: '.82rem', color: 'var(--muted)', marginLeft: 8 }}>#{sid}</span>
        </div>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: '.7rem', color: 'var(--muted)', textTransform: 'uppercase' }}>P&L</div>
            <div style={{ fontSize: '1.3rem', fontWeight: 800, color: pnlColor }}>${data.pnl.toFixed(2)}</div>
          </div>
          <span className={`badge ${data.running ? 'badge-green' : data.status === 'completed' ? 'badge-yellow' : 'badge-red'}`} style={{ fontSize: '.8rem', padding: '4px 14px' }}>
            {data.running ? '● Live' : data.status}
          </span>
          {data.running && (
            <button className="btn btn-red" onClick={handleClose} style={{ padding: '6px 16px' }}>✕ Close</button>
          )}
        </div>
      </div>

      {/* Log output */}
      <div
        ref={logRef}
        onScroll={() => {
          const el = logRef.current;
          autoScroll.current = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
        }}
        className="log-box"
        style={{ height: 'calc(100vh - 200px)', minHeight: 400 }}
      >
        {data.logs.map((line, i) => {
          const cls = line.includes('✗') || line.includes('🛑') || line.includes('⚠') ? 'err'
            : line.includes('✓') || line.includes('✅') || line.includes('🎯') ? 'ok'
            : line.includes('📊') ? 'warn' : '';
          return <div key={i} className={`line ${cls}`}>{line}</div>;
        })}
        {data.logs.length === 0 && <div style={{ color: 'var(--muted)', padding: 10 }}>No logs yet...</div>}
      </div>
    </div>
  );
}
