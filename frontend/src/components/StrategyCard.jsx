import React from 'react';

/**
 * Reusable strategy card component.
 *
 * <StrategyCard strategy={s} onClick={() => nav(`/strategy/${s.sid}`)} onClose={() => close(s.sid)} />
 * <StrategyCard strategy={s} compact />
 *
 * strategy shape: { sid, source, name, status, pnl, started_at, legs?, adjustment_count?, running?, details? }
 */
export default function StrategyCard({ strategy: s, onClick, onClose, onLogs, compact = false }) {
  const pnl = s.pnl || 0;
  const isRunning = s.status === 'running' || s.status === 'open (no monitor)';

  if (compact) {
    return (
      <div onClick={onClick} style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: '10px 14px', cursor: onClick ? 'pointer' : 'default', minWidth: 180, transition: 'border-color .15s' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
          <SourceBadge source={s.source} />
          <StatusBadge status={s.status} running={isRunning} />
        </div>
        <div style={{ fontWeight: 700, fontSize: '.88rem', marginBottom: 2 }}>{s.name || s.sid}</div>
        <div style={{ fontWeight: 800, fontSize: '1rem', color: pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${pnl.toFixed(2)}</div>
      </div>
    );
  }

  return (
    <div onClick={onClick} style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 10, padding: '14px 16px', cursor: onClick ? 'pointer' : 'default', transition: 'all .15s', marginBottom: 8 }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 4 }}>
            <SourceBadge source={s.source} />
            <StatusBadge status={s.status} running={isRunning} />
          </div>
          <div style={{ fontWeight: 700, fontSize: '.92rem' }}>{s.name || `Strategy ${s.sid}`}</div>
          <div style={{ fontSize: '.72rem', color: 'var(--muted)', marginTop: 2 }}>
            ID: {s.sid} · {(s.started_at || '').replace('T', ' ').slice(0, 16)}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontWeight: 800, fontSize: '1.1rem', color: pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>${pnl.toFixed(2)}</div>
          {s.cumulative_pnl != null && <div style={{ fontSize: '.72rem', color: 'var(--muted)' }}>Cumulative: ${(s.cumulative_pnl || 0).toFixed(2)}</div>}
          {s.adjustment_count > 0 && <div style={{ fontSize: '.7rem', color: 'var(--muted)' }}>Adj: {s.adjustment_count}</div>}
        </div>
      </div>

      {/* Legs summary */}
      {s.legs?.length > 0 && (
        <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
          {s.legs.slice(0, 4).map((l, i) => (
            <span key={i} style={{ fontSize: '.7rem', padding: '2px 8px', borderRadius: 4, background: (l.side || '').toLowerCase() === 'sell' ? 'rgba(239,68,68,0.1)' : 'rgba(34,197,94,0.1)', color: (l.side || '').toLowerCase() === 'sell' ? 'var(--red)' : 'var(--green)', fontWeight: 600 }}>
              {(l.side || '').toUpperCase()} {(l.type || '').toUpperCase()} {l.strike}
            </span>
          ))}
          {s.legs.length > 4 && <span style={{ fontSize: '.7rem', color: 'var(--muted)' }}>+{s.legs.length - 4} more</span>}
        </div>
      )}

      {/* Details row */}
      {s.details && (
        <div style={{ display: 'flex', gap: 12, marginTop: 8, fontSize: '.72rem', color: 'var(--muted)', flexWrap: 'wrap' }}>
          {s.details.asset && <span>Asset: <b style={{ color: 'var(--text)' }}>{s.details.asset}</b></span>}
          {s.details.expiry_date && <span>Expiry: <b style={{ color: 'var(--text)' }}>{s.details.expiry_date}</b></span>}
          {s.details.lot_size && <span>Lots: <b style={{ color: 'var(--text)' }}>{s.details.lot_size}</b></span>}
          {s.details.target_delta && <span>Δ: <b style={{ color: 'var(--text)' }}>{s.details.target_delta}</b></span>}
          {s.details.timeframe && <span>TF: <b style={{ color: 'var(--text)' }}>{s.details.timeframe}</b></span>}
        </div>
      )}

      {/* Actions */}
      {(onClose || onLogs) && (
        <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
          {onLogs && <button className="btn btn-outline" onClick={e => { e.stopPropagation(); onLogs(); }} style={{ padding: '4px 12px', fontSize: '.75rem' }}>📋 Logs</button>}
          {isRunning && onClose && <button className="btn btn-red" onClick={e => { e.stopPropagation(); onClose(); }} style={{ padding: '4px 12px', fontSize: '.75rem' }}>✕ Close</button>}
        </div>
      )}
    </div>
  );
}

/** Grid of strategy cards */
export function StrategyGrid({ strategies = [], onSelect, onClose, onLogs, title, onRefresh, onCloseAll }) {
  if (!strategies.length && !title) return null;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      {title && (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <div style={{ fontWeight: 700 }}>{title} ({strategies.length})</div>
          <div style={{ display: 'flex', gap: 8 }}>
            {onRefresh && <button className="btn btn-outline" onClick={onRefresh} style={{ padding: '4px 12px', fontSize: '.8rem' }}>🔄 Refresh</button>}
            {onCloseAll && <button className="btn btn-red" onClick={onCloseAll} style={{ padding: '4px 12px', fontSize: '.8rem' }}>✕ Close All</button>}
          </div>
        </div>
      )}
      {strategies.length === 0 ? (
        <div style={{ color: 'var(--muted)', fontSize: '.85rem', padding: 10 }}>No strategies</div>
      ) : (
        strategies.map(s => (
          <StrategyCard key={s.sid} strategy={s}
            onClick={onSelect ? () => onSelect(s) : undefined}
            onClose={onClose ? () => onClose(s.sid) : undefined}
            onLogs={onLogs ? () => onLogs(s.sid) : undefined}
          />
        ))
      )}
    </div>
  );
}

// ── Sub-components ──

function SourceBadge({ source }) {
  const colors = {
    'AlgoX DN': { bg: '#ede9fe', color: '#6366f1' },
    'Option Chain': { bg: '#fef3c7', color: '#d97706' },
    'Strategy Builder': { bg: '#dbeafe', color: '#2563eb' },
    'Div+MSS': { bg: '#fce7f3', color: '#db2777' },
    'SMA+Vol': { bg: '#d1fae5', color: '#059669' },
  };
  const c = colors[source] || { bg: '#f0f0f0', color: 'var(--muted)' };
  return <span style={{ fontSize: '.68rem', padding: '2px 8px', borderRadius: 4, fontWeight: 700, background: c.bg, color: c.color }}>{source || 'Manual'}</span>;
}

function StatusBadge({ status, running }) {
  if (running) return <span className="badge badge-green" style={{ fontSize: '.68rem' }}>● Live</span>;
  if (status === 'completed') return <span className="badge" style={{ fontSize: '.68rem', background: '#f0f0f0', color: 'var(--muted)' }}>Completed</span>;
  if (status === 'closed') return <span className="badge badge-red" style={{ fontSize: '.68rem' }}>Closed</span>;
  return <span className="badge badge-yellow" style={{ fontSize: '.68rem' }}>{status}</span>;
}
