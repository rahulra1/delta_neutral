import React, { useEffect, useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import api from '../api';

const NAV_ITEMS = [
  { to: '/', icon: '📊', label: 'Dashboard' },
  { to: '/strategy/new', icon: '⚡', label: 'Strategy Center' },
  { to: '/option-chain', icon: '⛓', label: 'Trade Terminal' },
  { to: '/performance', icon: '📈', label: 'Performance' },
  { to: '/strategy-builder', icon: '🛠', label: 'Strategy Builder' },
  { to: '/chart', icon: '📉', label: 'Chart' },
  { to: '/broker', icon: '🔗', label: 'Broker' },
  { to: '/profile', icon: '👤', label: 'Settings' },
];

export default function Sidebar() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const [credits, setCredits] = useState(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => { api.get('/credits').then(r => setCredits(r.data.credits_remaining)).catch(() => {}); }, []);

  const items = [...NAV_ITEMS];
  if (user?.is_admin) items.push({ to: '/admin', icon: '🛡', label: 'Admin' });

  return (
    <div className="sidebar" style={{ width: collapsed ? 60 : 200 }}>
      {/* Brand */}
      <div className="sidebar-brand" onClick={() => nav('/')}>
        <span className="sidebar-logo">⚡</span>
        {!collapsed && <span className="sidebar-title">AlgoX</span>}
      </div>

      {/* Nav */}
      <nav className="sidebar-nav">
        {items.map(item => (
          <NavLink key={item.to} to={item.to} end={item.to === '/'} className={({ isActive }) => `sidebar-link ${isActive ? 'active' : ''}`}>
            <span className="sidebar-icon">{item.icon}</span>
            {!collapsed && <span className="sidebar-label">{item.label}</span>}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="sidebar-footer">
        {!collapsed && credits !== null && (
          <div className="sidebar-credits" onClick={() => nav('/profile')}>🪙 {credits} credits</div>
        )}
        <div className="sidebar-user">
          <div className="sidebar-avatar">{user?.username?.[0]?.toUpperCase()}</div>
          {!collapsed && (
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: '.82rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{user?.username}</div>
              <div style={{ fontSize: '.68rem', color: 'var(--muted)', cursor: 'pointer' }} onClick={e => { e.stopPropagation(); logout(); nav('/login'); }}>Logout</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
