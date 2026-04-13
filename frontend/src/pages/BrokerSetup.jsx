import React, { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import api from '../api';

const STEPS = [
  { title: 'Create an Account', desc: 'Sign up at Delta Exchange India if you haven\'t already.', note: 'Use your real identity — KYC is required for trading.' },
  { title: 'Navigate to API Settings', desc: 'Go to Settings → API Management in your Delta Exchange dashboard.' },
  { title: 'Create a New API Key', desc: 'Click "Create API Key" and enable trading permissions.', note: 'Do NOT enable withdrawal permissions for safety.' },
  { title: 'Whitelist Your IP', desc: 'Add your server\'s IP address to the API key whitelist for security.' },
  { title: 'Copy Your Credentials', desc: 'Copy the API Key and API Secret. The secret is only shown once.' },
  { title: 'Save & Test Below', desc: 'Paste your credentials in the form below and test the connection.' },
];

export default function BrokerSetup() {
  const nav = useNavigate();
  const [brokers, setBrokers] = useState([]);
  const [form, setForm] = useState({ name: '', broker: '', api_key: '', api_secret: '' });
  const [error, setError] = useState('');
  const [testResult, setTestResult] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.get('/brokers').then(r => {
      const b = r.data.brokers || r.data || [];
      setBrokers(b);
      if (b.length) setForm(f => ({ ...f, broker: b[0].id || b[0].name || b[0] }));
    });
  }, []);

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  const save = async () => {
    setError(''); setTestResult(null); setSaving(true);
    try {
      await api.post('/profiles', form);
      nav('/broker');
    } catch (e) { setError(e.response?.data?.error || 'Failed to save'); }
    setSaving(false);
  };

  const testConnection = async () => {
    setError(''); setTestResult(null); setSaving(true);
    try {
      const { data: profile } = await api.post('/profiles', form);
      const pid = profile.id || profile.profile?.id;
      try {
        const { data: res } = await api.get('/test-connection', { params: { profile_id: pid } });
        if (res.success) {
          setTestResult({ success: true, msg: '✓ Connection successful!' });
          setSaving(false);
          return;
        }
        setTestResult({ success: false, msg: res.error || '✗ Connection failed' });
      } catch {
        setTestResult({ success: false, msg: '✗ Connection test failed' });
      }
      await api.delete(`/profiles/${pid}`);
    } catch (e) { setError(e.response?.data?.error || 'Failed to create profile'); }
    setSaving(false);
  };

  return (
    <div className="container" style={{ maxWidth: 700 }}>
      <Link to="/broker" style={{ fontSize: '.85rem', color: 'var(--muted)', textDecoration: 'none', display: 'inline-block', marginBottom: 16 }}>← Back to Broker Setup</Link>

      {/* Broker Header */}
      <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 16 }}>
        <div style={{ width: 56, height: 56, borderRadius: 12, background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '1.8rem', fontWeight: 700, flexShrink: 0 }}>Δ</div>
        <div>
          <div style={{ fontWeight: 700, fontSize: '1.1rem' }}>Connect Delta Exchange</div>
          <div style={{ fontSize: '.85rem', color: 'var(--muted)' }}>Set up your API connection to start trading</div>
        </div>
      </div>

      {/* Info Box */}
      <div style={{ background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 8, padding: '12px 16px', fontSize: '.85rem', color: '#1e40af', marginBottom: 16 }}>
        ℹ️ You'll need a Delta Exchange India account with API access enabled and your server IP whitelisted.
      </div>

      {/* Steps */}
      {STEPS.map((s, i) => (
        <div key={i} className="step">
          <div className="step-num">{i + 1}</div>
          <div className="step-title">{s.title}</div>
          <div className="step-desc">{s.desc}</div>
          {s.note && <div className="step-note">⚠️ {s.note}</div>}
        </div>
      ))}

      {/* Connection Form */}
      <div className="card" style={{ marginTop: 16 }}>
        <h2>Connection Details</h2>
        {error && <div className="error-msg">{error}</div>}
        <div className="grid-2">
          <div className="field"><label>Connection Name</label><input value={form.name} onChange={e => set('name', e.target.value)} placeholder="My Delta Account" /></div>
          <div className="field"><label>Broker</label>
            <select value={form.broker} onChange={e => set('broker', e.target.value)}>
              {brokers.map(b => <option key={b.id || b.name || b} value={b.id || b.name || b}>{b.name || b}</option>)}
            </select>
          </div>
        </div>
        <div className="field"><label>API Key</label><input value={form.api_key} onChange={e => set('api_key', e.target.value)} placeholder="Paste your API key" /></div>
        <div className="field"><label>API Secret</label><input type="password" value={form.api_secret} onChange={e => set('api_secret', e.target.value)} placeholder="Paste your API secret" /></div>

        {testResult && (
          <div style={{ padding: '8px 14px', borderRadius: 8, marginBottom: 12, fontSize: '.85rem', fontWeight: 600, background: testResult.success ? 'var(--light-green)' : 'var(--light-red)', color: testResult.success ? 'var(--green)' : 'var(--red)' }}>
            {testResult.msg}
          </div>
        )}

        <div style={{ display: 'flex', gap: 10 }}>
          <button className="btn btn-primary" onClick={save} disabled={saving}>Save</button>
          <button className="btn btn-green" onClick={testConnection} disabled={saving}>Test Connection</button>
          <button className="btn btn-outline" onClick={() => nav('/broker')}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
