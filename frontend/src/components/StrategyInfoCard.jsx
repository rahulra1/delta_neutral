import React from 'react';

/**
 * Reusable strategy info/selector card.
 *
 * <StrategyInfoCard strategy={s} active={true} onClick={fn} />
 *
 * strategy shape: { key, label, icon, type, desc, features? }
 */
export default function StrategyInfoCard({ strategy: s, active = false, onClick }) {
  const isOptions = s.type === 'Options';
  return (
    <div onClick={onClick} style={{
      flex: 1, minWidth: 220, padding: '18px 20px', borderRadius: 12, cursor: 'pointer', transition: 'all .2s',
      border: active ? '2px solid var(--accent)' : '2px solid var(--border)',
      background: active ? 'var(--accent)' : 'var(--card)',
      color: active ? '#fff' : 'var(--text)',
      boxShadow: active ? '0 4px 16px rgba(26,26,46,0.15)' : 'none',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: '1.4rem' }}>{s.icon}</span>
        <span style={{
          padding: '2px 10px', borderRadius: 4, fontSize: '.68rem', fontWeight: 700,
          background: active ? 'rgba(255,255,255,.2)' : isOptions ? '#ede9fe' : '#fef3c7',
          color: active ? '#fff' : isOptions ? '#6366f1' : '#d97706',
        }}>{s.type}</span>
      </div>
      <div style={{ fontWeight: 800, fontSize: '.95rem', marginBottom: 4 }}>{s.label}</div>
      <div style={{ fontSize: '.75rem', opacity: .8, lineHeight: 1.5, marginBottom: s.features ? 8 : 0 }}>{s.desc}</div>
      {s.features && (
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 6 }}>
          {s.features.map(f => (
            <span key={f} style={{
              fontSize: '.62rem', padding: '2px 8px', borderRadius: 3, fontWeight: 600,
              background: active ? 'rgba(255,255,255,.15)' : 'var(--bg)',
              color: active ? 'rgba(255,255,255,.9)' : 'var(--muted)',
            }}>{f}</span>
          ))}
        </div>
      )}
      {s.rec && (
        <div style={{
          marginTop: 8, padding: '4px 8px', borderRadius: 4, fontSize: '.65rem', fontWeight: 600,
          background: active ? 'rgba(255,255,255,.12)' : '#ecfdf5',
          color: active ? 'rgba(255,255,255,.85)' : '#059669',
          lineHeight: 1.4,
        }}>{s.rec}</div>
      )}
    </div>
  );
}

/** Row of strategy info cards for selection */
export function StrategySelector({ strategies, activeKey, onSelect }) {
  return (
    <div style={{ display: 'flex', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
      {strategies.map(s => (
        <StrategyInfoCard key={s.key} strategy={s} active={activeKey === s.key} onClick={() => onSelect(s.key)} />
      ))}
    </div>
  );
}
