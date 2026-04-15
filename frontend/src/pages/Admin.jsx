import React, { useState, useEffect } from 'react';
import api from '../api';

export default function Admin() {
  const [stats, setStats] = useState(null);
  const [users, setUsers] = useState([]);
  const [plans, setPlans] = useState([]);
  const [costs, setCosts] = useState(null);
  const [selectedUser, setSelectedUser] = useState(null);
  const [history, setHistory] = useState([]);
  const [credits, setCredits] = useState('');
  const [planId, setPlanId] = useState('');

  const load = () => {
    api.get('/admin/stats').then(r => setStats(r.data));
    api.get('/admin/users').then(r => setUsers(r.data.users || r.data));
    api.get('/admin/plans').then(r => setPlans(r.data.plans || r.data));
    api.get('/credits/costs').then(r => setCosts(r.data.costs || r.data));
  };

  useEffect(load, []);

  const selectUser = u => {
    setSelectedUser(u);
    api.get(`/admin/user-history/${u.id}`).then(r => setHistory(r.data));
  };

  const addCredits = () => api.post('/admin/add-credits', { user_id: selectedUser.id, amount: +credits }).then(() => { setCredits(''); load(); });
  const setPlan = () => api.post('/admin/set-plan', { user_id: selectedUser.id, plan_id: planId }).then(load);
  const toggleAdmin = () => api.post('/admin/set-admin', { user_id: selectedUser.id, is_admin: !selectedUser.is_admin }).then(load);

  return (
    <div className="container">
      <h1 className="page-title">Admin</h1>

      {stats && (
        <div className="top-stats" style={{ marginBottom: 24 }}>
          {[['Users', stats.total_users], ['Credits Available', stats.credits_available], ['Credits Used', stats.credits_used], ['Plans', stats.plans_count]].map(([l, v]) => (
            <div className="stat-card" key={l}><div style={{ fontSize: 12, color: '#888' }}>{l}</div><div style={{ fontSize: 22, fontWeight: 700 }}>{v}</div></div>
          ))}
        </div>
      )}

      <div className="grid-2" style={{ marginBottom: 24 }}>
        {/* Users */}
        <div className="card">
          <h3 style={{ marginBottom: 8 }}>Users</h3>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
              <thead><tr>{['ID', 'Username', 'Plan', 'Credits', 'Used', 'Role', ''].map(h => <th key={h} style={{ textAlign: 'left', padding: 4 }}>{h}</th>)}</tr></thead>
              <tbody>{users.map(u => (
                <tr key={u.id} style={{ background: selectedUser?.id === u.id ? 'rgba(41,98,255,0.15)' : 'transparent' }}>
                  <td style={{ padding: 4 }}>{u.id}</td><td>{u.username}</td><td>{u.plan_name || '—'}</td>
                  <td>{u.credits_remaining ?? '—'}</td><td>{u.credits_used ?? '—'}</td>
                  <td><span className={`badge ${u.is_admin ? 'badge-red' : 'badge-green'}`}>{u.is_admin ? 'admin' : 'user'}</span></td>
                  <td><button className="btn btn-outline" style={{ fontSize: 11, padding: '2px 8px' }} onClick={() => selectUser(u)}>Select</button></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>

        {/* Plans */}
        <div className="card">
          <h3 style={{ marginBottom: 8 }}>Plans</h3>
          <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
            <thead><tr>{['ID', 'Name', 'Price', 'Credits/mo', 'Max Strategies', 'Max Brokers'].map(h => <th key={h} style={{ textAlign: 'left', padding: 4 }}>{h}</th>)}</tr></thead>
            <tbody>{plans.map(p => (
              <tr key={p.id}><td style={{ padding: 4 }}>{p.id}</td><td>{p.name}</td><td>{p.price}</td><td>{p.credits_per_month}</td><td>{p.max_strategies}</td><td>{p.max_brokers}</td></tr>
            ))}</tbody>
          </table>
        </div>
      </div>

      {/* Quick Actions */}
      {selectedUser && (
        <div className="card" style={{ marginBottom: 24 }}>
          <h3 style={{ marginBottom: 8 }}>Actions — {selectedUser.username} (ID: {selectedUser.id})</h3>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
            <input className="field" type="number" placeholder="Credits" value={credits} onChange={e => setCredits(e.target.value)} style={{ width: 100 }} />
            <button className="btn btn-green" onClick={addCredits}>Add Credits</button>
            <select className="field" value={planId} onChange={e => setPlanId(e.target.value)}>
              <option value="">Select plan</option>
              {plans.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            <button className="btn btn-primary" onClick={setPlan}>Set Plan</button>
            <button className="btn btn-red" onClick={toggleAdmin}>{selectedUser.is_admin ? 'Remove Admin' : 'Make Admin'}</button>
          </div>

          {history.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <h4 style={{ marginBottom: 4 }}>Credit History</h4>
              <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
                <thead><tr>{['Date', 'Type', 'Amount', 'Balance', 'Note'].map(h => <th key={h} style={{ textAlign: 'left', padding: 4 }}>{h}</th>)}</tr></thead>
                <tbody>{history.map((h, i) => (
                  <tr key={i}><td style={{ padding: 4 }}>{h.date}</td><td><span className={`badge ${h.amount > 0 ? 'badge-green' : 'badge-red'}`}>{h.type}</span></td><td className={h.amount > 0 ? 'val-green' : 'val-red'}>{h.amount}</td><td>{h.balance}</td><td>{h.note}</td></tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Credit Costs */}
      {costs && (
        <div className="card">
          <h3 style={{ marginBottom: 8 }}>Credit Costs</h3>
          <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
            <thead><tr>{['Action', 'Cost'].map(h => <th key={h} style={{ textAlign: 'left', padding: 4 }}>{h}</th>)}</tr></thead>
            <tbody>{Object.entries(costs).map(([k, v]) => (
              <tr key={k}><td style={{ padding: 4 }}>{k}</td><td>{v}</td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </div>
  );
}
