import React, { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import api from '../api';

export default function Broker() {
  const nav = useNavigate();
  const [profiles, setProfiles] = useState([]);
  const [testing, setTesting] = useState(null);
  const [result, setResult] = useState({});

  const load = () => api.get('/profiles').then(r => setProfiles(r.data.profiles || []));
  useEffect(() => { load(); }, []);

  const connectedCount = profiles.filter(p => p.broker === 'delta_exchange' || p.broker === 'Delta Exchange').length;

  const test = async id => {
    setTesting(id);
    setResult(r => ({ ...r, [id]: undefined }));
    try {
      const { data } = await api.get('/test-connection', { params: { profile_id: id } });
      const msg = data.success ? '✓ OK' : '✗ Failed';
      setResult(r => ({ ...r, [id]: msg }));
      setTimeout(() => setResult(r => ({ ...r, [id]: undefined })), 3000);
    } catch (e) {
      setResult(r => ({ ...r, [id]: '✗ Failed' }));
      setTimeout(() => setResult(r => ({ ...r, [id]: undefined })), 3000);
    }
    setTesting(null);
  };

  const del = async id => {
    if (!window.confirm('Remove this connection?')) return;
    await api.delete(`/profiles/${id}`);
    load();
  };

  return (
    <div className="container">
      <div className="page-title">Broker Connections</div>

      {/* Available Brokers */}
      <div className="card">
        <h2>Available Brokers</h2>
        <div className="broker-grid">
          <div className="broker-card active">
            <div className="broker-logo" style={{ background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff', fontSize: '1.8rem' }}>Δ</div>
            <div className="broker-name">Delta Exchange</div>
            <div className="broker-desc">India's leading crypto derivatives exchange</div>
            <span className="badge badge-green">{connectedCount} connected</span>
          </div>
          <div className="broker-card" style={{ opacity: 0.5, cursor: 'default' }}>
            <div className="broker-logo" style={{ background: '#e5e7eb', color: '#9ca3af' }}>+</div>
            <div className="broker-name">More Brokers</div>
            <div className="broker-desc">Coming soon</div>
            <span className="badge badge-yellow">Coming Soon</span>
          </div>
        </div>
      </div>

      {/* My Connections */}
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
          <h2 style={{ marginBottom: 0 }}>My Connections <span className="badge badge-green">{profiles.length}</span></h2>
          <Link to="/broker/setup" className="btn btn-primary" style={{ textDecoration: 'none' }}>+ Add Connection</Link>
        </div>

        {profiles.map(p => (
          <div key={p.id} className="profile-card">
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{ width: 36, height: 36, borderRadius: 8, background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '.9rem', flexShrink: 0 }}>Δ</div>
              <div>
                <div className="pname">{p.name}</div>
                <div className="pkey">
                  <span className="badge badge-green" style={{ marginRight: 6 }}>{p.broker || 'Delta Exchange'}</span>
                  {p.api_key ? p.api_key.slice(0, 8) + '...' + p.api_key.slice(-4) : '—'}
                </div>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {result[p.id] && (
                <span style={{ fontSize: '.8rem', fontWeight: 600, color: result[p.id].includes('✓') ? 'var(--green)' : 'var(--red)' }}>{result[p.id]}</span>
              )}
              <button className="btn btn-outline" style={{ padding: '4px 14px', fontSize: '.8rem' }} onClick={() => test(p.id)} disabled={testing === p.id}>
                {testing === p.id ? 'Testing...' : 'Test'}
              </button>
              <button className="btn btn-red" style={{ padding: '4px 14px', fontSize: '.8rem' }} onClick={() => del(p.id)}>Remove</button>
            </div>
          </div>
        ))}
        {!profiles.length && (
          <div style={{ color: 'var(--muted)', fontSize: '.85rem', padding: '10px 0' }}>
            No connections yet. <Link to="/broker/setup" style={{ color: 'var(--purple)' }}>Set one up →</Link>
          </div>
        )}
      </div>
    </div>
  );
}
