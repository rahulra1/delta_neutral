import React, { useState, useEffect } from 'react';
import api from '../api';
import { useAuth } from '../context/AuthContext';

export default function Profile() {
  const { user } = useAuth();
  const [profiles, setProfiles] = useState([]);
  const [brokers, setBrokers] = useState([]);
  const [defaultKeys, setDefaultKeys] = useState({ api_key: '', api_secret: '' });
  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState({ name: '', api_key: '', api_secret: '', broker: '' });
  const [error, setError] = useState('');
  const [defaultStatus, setDefaultStatus] = useState('');

  const load = () => api.get('/profiles').then(r => {
    const p = r.data.profiles || [];
    setProfiles(p);
    const def = p.find(pr => pr.is_default);
    if (def) setDefaultKeys({ api_key: def.api_key || '', api_secret: '' });
  });

  useEffect(() => {
    load();
    api.get('/brokers').then(r => {
      const b = r.data.brokers || r.data || [];
      setBrokers(b);
      if (b.length) setForm(f => ({ ...f, broker: b[0].id || b[0].name || b[0] }));
    });
  }, []);

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  const saveDefault = async e => {
    e.preventDefault();
    setDefaultStatus('');
    try {
      await api.post('/profiles', { name: 'Default', ...defaultKeys });
      setDefaultStatus('Saved!');
      load();
    } catch (e) { setDefaultStatus(e.response?.data?.error || 'Failed to save'); }
  };

  const add = async e => {
    e.preventDefault();
    setError('');
    try {
      await api.post('/profiles', form);
      setForm({ name: '', api_key: '', api_secret: '', broker: brokers[0]?.id || brokers[0]?.name || '' });
      setShowAdd(false);
      load();
    } catch (e) { setError(e.response?.data?.error || 'Failed to create profile'); }
  };

  const del = async id => {
    if (!window.confirm('Delete this profile?')) return;
    await api.delete(`/profiles/${id}`);
    load();
  };

  const maskKey = (key) => {
    if (!key || key.length < 12) return key || '—';
    return key.slice(0, 8) + '...' + key.slice(-4);
  };

  return (
    <div className="container">
      <div className="page-title">Profile</div>

      {/* Default API Keys */}
      <div className="card">
        <h2>Default API Keys</h2>
        <div className="field">
          <label>Username</label>
          <input value={user?.username || ''} disabled />
        </div>
        <div style={{ marginBottom: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className={`status-dot ${defaultKeys.api_key ? 'on' : 'off'}`}></span>
          <span style={{ fontSize: '.85rem', color: 'var(--muted)' }}>
            {defaultKeys.api_key ? 'API key configured' : 'No API key configured'}
          </span>
        </div>
        <form onSubmit={saveDefault}>
          <div className="field">
            <label>API Key</label>
            <input value={defaultKeys.api_key} onChange={e => setDefaultKeys(k => ({ ...k, api_key: e.target.value }))} />
          </div>
          <div className="field">
            <label>API Secret</label>
            <input type="password" value={defaultKeys.api_secret} onChange={e => setDefaultKeys(k => ({ ...k, api_secret: e.target.value }))} placeholder="Enter new secret to update" />
          </div>
          {defaultStatus && <div style={{ fontSize: '.85rem', color: defaultStatus === 'Saved!' ? 'var(--green)' : 'var(--red)', marginBottom: 10 }}>{defaultStatus}</div>}
          <button type="submit" className="btn btn-primary">Save Default Keys</button>
        </form>
      </div>

      {/* API Profiles */}
      <div className="card">
        <h2>🔑 API Profiles</h2>
        <p style={{ fontSize: '.85rem', color: 'var(--muted)', marginBottom: 16 }}>
          Manage multiple API connections for different brokers and accounts.
        </p>

        {profiles.map(p => (
          <div key={p.id} className="profile-card">
            <div>
              <div className="pname">{p.name}</div>
              <div className="pkey">{maskKey(p.api_key)}</div>
            </div>
            <button className="btn btn-red" style={{ padding: '4px 14px', fontSize: '.8rem' }} onClick={() => del(p.id)}>Delete</button>
          </div>
        ))}
        {!profiles.length && <div style={{ color: 'var(--muted)', fontSize: '.85rem', marginBottom: 12 }}>No profiles yet.</div>}

        {!showAdd ? (
          <button className="btn btn-outline" style={{ marginTop: 8 }} onClick={() => setShowAdd(true)}>+ Add Profile</button>
        ) : (
          <div style={{ marginTop: 14, padding: 16, background: 'var(--bg)', borderRadius: 8, border: '1px solid var(--border)' }}>
            {error && <div className="error-msg">{error}</div>}
            <form onSubmit={add}>
              <div className="grid-2">
                <div className="field"><label>Name</label><input value={form.name} onChange={e => set('name', e.target.value)} required /></div>
                <div className="field"><label>Broker</label>
                  <select value={form.broker} onChange={e => set('broker', e.target.value)}>
                    {brokers.map(b => <option key={b.id || b.name || b} value={b.id || b.name || b}>{b.name || b}</option>)}
                  </select>
                </div>
                <div className="field"><label>API Key</label><input value={form.api_key} onChange={e => set('api_key', e.target.value)} required /></div>
                <div className="field"><label>API Secret</label><input type="password" value={form.api_secret} onChange={e => set('api_secret', e.target.value)} required /></div>
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button type="submit" className="btn btn-primary">Save</button>
                <button type="button" className="btn btn-outline" onClick={() => { setShowAdd(false); setError(''); }}>Cancel</button>
              </div>
            </form>
          </div>
        )}
      </div>
    </div>
  );
}
