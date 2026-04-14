import React from 'react';

/**
 * Reusable position card component.
 * 
 * Usage:
 *   <PositionCard position={pos} sym="$" onClose={() => handleClose(pos)} />
 *   <PositionCard position={pos} sym="₹" compact />
 *   <PositionCard position={pos} sym="$" /> // no close button
 * 
 * position shape: { symbol, side, type, strike, size, entry_price, current_mark, current_pnl, delta, source, opened_at, strategy_sid }
 * All fields optional except symbol and side.
 */
export default function PositionCard({ position: p, sym = '$', onClose, compact = false }) {
  const side = (p.side || '').toLowerCase();
  const entry = p.entry_price || p.entry || 0;
  const mark = p.current_mark || p.mark_price || p.mark || entry;
  const pnl = p.current_pnl || p.pnl || p.payoff || 0;
  const chg = entry ? ((mark - entry) / entry * 100) : 0;

  if (compact) {
    return (
      <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: '8px 12px', fontSize: 11, minWidth: 150 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 2 }}>
          <span>
            <SideBadge side={side} />
            <span style={{ fontWeight: 700, marginLeft: 4, fontSize: 11 }}>{p.symbol}</span>
          </span>
          {onClose && <CloseBtn onClick={onClose} />}
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--muted)' }}>{sym}{mark.toFixed(2)}</span>
          <span style={{ fontWeight: 700, color: pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>{sym}{pnl.toFixed(2)}</span>
        </div>
      </div>
    );
  }

  return (
    <div style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: '12px 14px', minWidth: 190, fontSize: 11, transition: 'border-color .15s' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <span>
          <SideBadge side={side} />
          <span style={{ fontWeight: 700, fontSize: 12, marginLeft: 4 }}>{p.symbol}</span>
        </span>
        {onClose && <CloseBtn onClick={onClose} />}
      </div>

      {/* Details */}
      <Row label="Type" value={(p.type || '').toUpperCase()} />
      <Row label="Strike" value={p.strike ? Number(p.strike).toLocaleString() : '—'} />
      <Row label="Size" value={`${p.size || 0} lots`} />
      <Row label="Entry" value={`${sym}${entry.toFixed(2)}`} />
      <Row label="Mark" value={
        <span style={{ fontWeight: 700 }}>
          {sym}{mark.toFixed(2)}
          <span style={{ fontSize: 9, marginLeft: 4, color: chg >= 0 ? 'var(--red)' : 'var(--green)' }}>
            ({chg >= 0 ? '+' : ''}{chg.toFixed(2)}%)
          </span>
        </span>
      } />
      {p.delta !== undefined && p.delta !== 0 && <Row label="Delta" value={Number(p.delta).toFixed(4)} />}

      {/* P&L */}
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, paddingTop: 6, borderTop: '1px solid var(--border)', fontSize: 12 }}>
        <span style={{ color: 'var(--muted)' }}>P&L</span>
        <span style={{ fontWeight: 800, color: pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>{sym}{pnl.toFixed(2)}</span>
      </div>

      {/* Footer */}
      {(p.source || p.opened_at) && (
        <div style={{ fontSize: 9, color: 'var(--muted)', marginTop: 4 }}>
          {p.source && <span>{p.source}</span>}
          {p.opened_at && <span> · {p.opened_at.slice(11, 19)}</span>}
        </div>
      )}
    </div>
  );
}

/** Grid of position cards with total P&L header */
export function PositionGrid({ positions = [], sym = '$', onClose, onRefresh, title = '📋 Open Positions' }) {
  const totalPnl = positions.reduce((s, p) => s + (p.current_pnl || p.pnl || p.payoff || 0), 0);

  if (!positions.length) return null;

  return (
    <div style={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 8, padding: 16, marginBottom: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ fontWeight: 700, fontSize: '.85rem' }}>{title} ({positions.length})</div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{ fontSize: '.85rem', fontWeight: 700, color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
            Total: {sym}{totalPnl.toFixed(2)}
          </span>
          {onRefresh && <button onClick={onRefresh} style={{ padding: '4px 10px', border: '1px solid var(--border)', borderRadius: 4, background: 'var(--card)', cursor: 'pointer', fontSize: 11 }}>🔄</button>}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {positions.map((p, i) => (
          <PositionCard key={p.product_id || i} position={p} sym={sym} onClose={onClose ? () => onClose(p) : undefined} />
        ))}
      </div>
    </div>
  );
}

/** Positions as a table row format */
export function PositionTable({ positions = [], sym = '$', onClose }) {
  if (!positions.length) return null;
  const totalPnl = positions.reduce((s, p) => s + (p.current_pnl || p.pnl || p.payoff || 0), 0);

  return (
    <div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.82rem' }}>
        <thead><tr>{['Side', 'Symbol', 'Type', 'Strike', 'Size', 'Entry', 'Mark', 'Chg%', 'P&L', ''].map(h =>
          <th key={h} style={{ textAlign: 'left', padding: '6px 8px', color: 'var(--muted)', fontSize: '.72rem', textTransform: 'uppercase', borderBottom: '2px solid var(--border)' }}>{h}</th>
        )}</tr></thead>
        <tbody>
          {positions.map((p, i) => {
            const entry = p.entry_price || p.entry || 0;
            const mark = p.current_mark || p.mark_price || p.mark || entry;
            const pnl = p.current_pnl || p.pnl || p.payoff || 0;
            const chg = entry ? ((mark - entry) / entry * 100) : 0;
            return (
              <tr key={p.product_id || i} style={{ borderBottom: '1px solid var(--border)' }}>
                <td style={{ padding: '6px 8px' }}><SideBadge side={(p.side || '').toLowerCase()} /></td>
                <td style={{ padding: '6px 8px', fontWeight: 600, fontSize: '.8rem' }}>{p.symbol}</td>
                <td style={{ padding: '6px 8px' }}>{(p.type || '').toUpperCase()}</td>
                <td style={{ padding: '6px 8px' }}>{p.strike ? Number(p.strike).toLocaleString() : '—'}</td>
                <td style={{ padding: '6px 8px' }}>{p.size || 0}</td>
                <td style={{ padding: '6px 8px' }}>{sym}{entry.toFixed(2)}</td>
                <td style={{ padding: '6px 8px', fontWeight: 700 }}>{sym}{mark.toFixed(2)}</td>
                <td style={{ padding: '6px 8px', color: chg >= 0 ? 'var(--red)' : 'var(--green)', fontWeight: 600, fontSize: '.75rem' }}>{chg >= 0 ? '+' : ''}{chg.toFixed(2)}%</td>
                <td style={{ padding: '6px 8px', fontWeight: 700, color: pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>{sym}{pnl.toFixed(2)}</td>
                <td style={{ padding: '6px 8px' }}>{onClose && <button className="btn btn-red" onClick={() => onClose(p)} style={{ padding: '2px 10px', fontSize: '.72rem' }}>Close</button>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div style={{ textAlign: 'right', padding: '8px', fontWeight: 800, fontSize: '.9rem' }}>
        Total P&L: <span style={{ color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)' }}>{sym}{totalPnl.toFixed(2)}</span>
      </div>
    </div>
  );
}

// ── Shared sub-components ──

function SideBadge({ side }) {
  const isSell = side === 'sell';
  return (
    <span style={{ display: 'inline-block', padding: '1px 8px', borderRadius: 3, fontSize: 9, fontWeight: 700,
      background: isSell ? 'rgba(239,68,68,0.15)' : 'rgba(34,197,94,0.15)',
      color: isSell ? 'var(--red)' : 'var(--green)' }}>
      {side.toUpperCase()}
    </span>
  );
}

function CloseBtn({ onClick }) {
  return <button onClick={onClick} style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 14, padding: 0, lineHeight: 1 }}>✕</button>;
}

function Row({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--muted)', fontSize: 10, padding: '1px 0' }}>
      <span>{label}</span>
      <span style={{ fontWeight: 600, color: 'var(--text)' }}>{value}</span>
    </div>
  );
}
