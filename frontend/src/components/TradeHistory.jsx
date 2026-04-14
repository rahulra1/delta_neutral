import React, { useState, useMemo } from 'react';

const PAGE_SIZES = [5, 10, 20, 50];

export default function TradeHistory({ trades = [], onSelect }) {
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState('started_at');
  const [sortDir, setSortDir] = useState('desc');
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(10);

  const toggleSort = key => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
    setPage(1);
  };

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return trades.filter(t => {
      if (!q) return true;
      const p = t.params || {};
      return [t.sid, t.status, p.asset, p.expiry_date, p.source, p.name, p.leg_details]
        .some(v => v && String(v).toLowerCase().includes(q));
    });
  }, [trades, search]);

  const sorted = useMemo(() => {
    return [...filtered].sort((a, b) => {
      let va, vb;
      if (sortKey === 'pnl') { va = a.pnl || 0; vb = b.pnl || 0; }
      else if (sortKey === 'status') { va = a.status || ''; vb = b.status || ''; }
      else if (sortKey === 'asset') { va = (a.params || {}).asset || ''; vb = (b.params || {}).asset || ''; }
      else if (sortKey === 'started_at') { va = a.started_at || ''; vb = b.started_at || ''; }
      else { va = a[sortKey] || ''; vb = b[sortKey] || ''; }
      if (typeof va === 'number') return sortDir === 'asc' ? va - vb : vb - va;
      return sortDir === 'asc' ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });
  }, [filtered, sortKey, sortDir]);

  const totalPages = Math.ceil(sorted.length / perPage) || 1;
  const paginated = sorted.slice((page - 1) * perPage, page * perPage);

  const SortIcon = ({ col }) => {
    if (sortKey !== col) return <span style={{ opacity: .3 }}>↕</span>;
    return <span>{sortDir === 'asc' ? '↑' : '↓'}</span>;
  };

  const cols = [
    { key: 'sid', label: 'ID' },
    { key: 'source', label: 'Source' },
    { key: 'status', label: 'Status' },
    { key: 'asset', label: 'Asset' },
    { key: 'expiry', label: 'Expiry' },
    { key: 'lots', label: 'Lots' },
    { key: 'details', label: 'Details' },
    { key: 'pnl', label: 'P&L' },
    { key: 'started_at', label: 'Started' },
  ];

  if (!trades.length) return <div style={{ color: 'var(--muted)', fontSize: '.85rem', padding: 16 }}>No trade history yet.</div>;

  return (
    <div>
      {/* Controls */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
        <input value={search} onChange={e => { setSearch(e.target.value); setPage(1); }} placeholder="🔍 Search trades..." style={{ padding: '8px 14px', border: '1px solid var(--border)', borderRadius: 8, fontSize: '.85rem', background: 'var(--bg)', minWidth: 200, flex: 1, maxWidth: 300 }} />
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: '.82rem', color: 'var(--muted)' }}>
          <span>Show</span>
          <select value={perPage} onChange={e => { setPerPage(+e.target.value); setPage(1); }} style={{ padding: '4px 8px', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg)', fontSize: '.82rem' }}>
            {PAGE_SIZES.map(n => <option key={n} value={n}>{n}</option>)}
          </select>
          <span>of {sorted.length} trades</span>
        </div>
      </div>

      {/* Table */}
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.82rem' }}>
          <thead>
            <tr>
              {cols.map(c => (
                <th key={c.key} onClick={() => toggleSort(c.key)} style={{ textAlign: 'left', padding: '8px 10px', color: 'var(--muted)', fontSize: '.72rem', textTransform: 'uppercase', borderBottom: '2px solid var(--border)', cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' }}>
                  {c.label} <SortIcon col={c.key} />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paginated.map(t => {
              const p = t.params || {};
              const pnl = t.pnl || 0;
              return (
                <tr key={t.sid + t.started_at} onClick={() => onSelect?.(t)} style={{ cursor: onSelect ? 'pointer' : 'default', transition: 'background .1s' }} onMouseOver={e => e.currentTarget.style.background = 'var(--bg)'} onMouseOut={e => e.currentTarget.style.background = ''}>
                  <td style={{ padding: '10px', fontWeight: 600, borderBottom: '1px solid var(--border)' }}>{t.sid}</td>
                  <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}><span style={{ fontSize: '.7rem', padding: '2px 8px', borderRadius: 4, fontWeight: 700, background: '#ede9fe', color: '#6366f1' }}>{p.source || 'DN'}</span></td>
                  <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}><span className={t.status === 'running' ? 'badge badge-yellow' : pnl >= 0 ? 'badge badge-green' : 'badge badge-red'}>{t.status === 'running' ? 'Running' : pnl >= 0 ? 'Profit' : 'Loss'}</span></td>
                  <td style={{ padding: '10px', borderBottom: '1px solid var(--border)', fontWeight: 600 }}>{p.asset || 'BTC'}</td>
                  <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}>{p.expiry_date || '—'}</td>
                  <td style={{ padding: '10px', borderBottom: '1px solid var(--border)' }}>{p.lot_size || p.legs || '—'}</td>
                  <td style={{ padding: '10px', borderBottom: '1px solid var(--border)', fontSize: '.75rem', color: 'var(--muted)', maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.leg_details || (p.target_delta ? `Δ${p.target_delta}` : p.name || '—')}</td>
                  <td style={{ padding: '10px', fontWeight: 700, color: pnl >= 0 ? 'var(--green)' : 'var(--red)', borderBottom: '1px solid var(--border)' }}>${pnl.toFixed(2)}</td>
                  <td style={{ padding: '10px', fontSize: '.75rem', color: 'var(--muted)', borderBottom: '1px solid var(--border)', whiteSpace: 'nowrap' }}>{(t.started_at || '').replace('T', ' ').slice(0, 16)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12, fontSize: '.82rem' }}>
          <span style={{ color: 'var(--muted)' }}>
            Showing {(page - 1) * perPage + 1}–{Math.min(page * perPage, sorted.length)} of {sorted.length}
          </span>
          <div style={{ display: 'flex', gap: 4 }}>
            <PgBtn onClick={() => setPage(1)} disabled={page === 1}>«</PgBtn>
            <PgBtn onClick={() => setPage(p => p - 1)} disabled={page === 1}>‹</PgBtn>
            {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
              const start = Math.max(1, Math.min(page - 2, totalPages - 4));
              const p = start + i;
              if (p > totalPages) return null;
              return <PgBtn key={p} onClick={() => setPage(p)} active={p === page}>{p}</PgBtn>;
            })}
            <PgBtn onClick={() => setPage(p => p + 1)} disabled={page === totalPages}>›</PgBtn>
            <PgBtn onClick={() => setPage(totalPages)} disabled={page === totalPages}>»</PgBtn>
          </div>
        </div>
      )}
    </div>
  );
}

function PgBtn({ children, onClick, disabled, active }) {
  return (
    <button onClick={onClick} disabled={disabled} style={{
      padding: '5px 12px', border: '1px solid var(--border)', borderRadius: 6,
      background: active ? 'var(--accent)' : 'var(--card)', color: active ? '#fff' : 'var(--text)',
      cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? .4 : 1,
      fontSize: '.78rem', fontFamily: 'inherit', fontWeight: active ? 700 : 400,
    }}>{children}</button>
  );
}
