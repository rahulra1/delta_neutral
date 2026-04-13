import React, { useEffect, useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import api from '../api';

export default function Navbar() {
  const { user, logout } = useAuth();
  const nav = useNavigate();
  const [credits, setCredits] = useState(null);

  useEffect(() => { api.get('/credits').then(r => setCredits(r.data.credits_remaining)).catch(() => {}); }, []);

  const handleLogout = () => { logout(); nav('/login'); };

  return (
    <div className="nav">
      <NavLink to="/" className="logo" style={{ textDecoration: 'none' }}>⚡ AlgoX</NavLink>
      <NavLink to="/" end>Dashboard</NavLink>
      <NavLink to="/option-chain">Option Chain</NavLink>
      <NavLink to="/strategy/new?asset=BTC">Strategies</NavLink>
      <NavLink to="/strategy-builder">Builder</NavLink>
      <NavLink to="/performance">Performance</NavLink>
      <NavLink to="/chart">Chart</NavLink>
      <NavLink to="/profile">Profile</NavLink>
      <NavLink to="/broker">Broker</NavLink>
      <div className="spacer" />
      {credits !== null && (
        <div onClick={() => nav('/profile')} style={{ background: '#fef3c7', color: '#92400e', padding: '4px 12px', borderRadius: 4, fontSize: '.75rem', fontWeight: 700, cursor: 'pointer' }}>
          🪙 {credits} credits
        </div>
      )}
      <div className="user">
        <div className="avatar">{user?.username?.[0]?.toUpperCase()}</div>
        <span>{user?.username}</span>
        <a href="#" onClick={e => { e.preventDefault(); handleLogout(); }} style={{ border: 'none', padding: 0, fontSize: '.75rem' }}>Logout</a>
      </div>
    </div>
  );
}
