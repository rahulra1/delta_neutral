import React, { useState } from 'react';
import { useAuth } from '../context/AuthContext';

export default function Login() {
  const { login, register } = useAuth();
  const [tab, setTab] = useState('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');

  const handleSubmit = async e => {
    e.preventDefault(); setError('');
    try {
      if (tab === 'register') {
        if (password !== confirm) { setError('Passwords do not match'); return; }
        if (password.length < 6) { setError('Password min 6 characters'); return; }
        await register(username, password);
      } else { await login(username, password); }
    } catch (err) { setError(err.response?.data?.error || 'Something went wrong'); }
  };

  const scrollTo = id => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' });

  return (
    <>
      {/* Navbar */}
      <nav style={{ position: 'fixed', top: 0, left: 0, right: 0, height: 60, background: 'rgba(15,23,42,0.8)', backdropFilter: 'blur(16px)', borderBottom: '1px solid rgba(255,255,255,0.06)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 40px', zIndex: 100 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: '1.3rem' }}>⚡</span>
          <span style={{ fontSize: '1.1rem', fontWeight: 800, background: 'linear-gradient(135deg,#818cf8,#a78bfa)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>AlgoX</span>
        </div>
        <div style={{ display: 'flex', gap: 28, alignItems: 'center' }}>
          {[['Features', 'features'], ['Strategies', 'strategies'], ['Pricing', 'pricing']].map(([l, id]) => (
            <span key={id} onClick={() => scrollTo(id)} style={{ color: '#94a3b8', fontSize: '.85rem', fontWeight: 500, cursor: 'pointer' }}>{l}</span>
          ))}
          <span onClick={() => scrollTo('auth')} style={{ background: 'linear-gradient(135deg,#6366f1,#7c3aed)', color: '#fff', padding: '8px 20px', borderRadius: 8, fontSize: '.85rem', fontWeight: 600, cursor: 'pointer' }}>Get Started</span>
        </div>
      </nav>

      {/* Hero */}
      <div className="hero" style={{ paddingTop: 100 }}>
        <div className="hero-content">
          <div className="hero-left">
            <div style={{ display: 'inline-block', background: 'rgba(99,102,241,0.15)', border: '1px solid rgba(99,102,241,0.3)', borderRadius: 20, padding: '4px 16px', fontSize: '.78rem', color: '#a5b4fc', fontWeight: 600, marginBottom: 20 }}>🚀 Automated Options & Futures Trading</div>
            <h1>Trade Smarter<br />with <span className="gradient">AI-Powered</span><br />Strategies</h1>
            <p>Deploy delta-neutral options strategies, RSI divergence signals, and volume breakout systems across BTC, ETH, NIFTY and more. Real-time monitoring with smart adjustments.</p>

            {/* Stats */}
            <div style={{ display: 'flex', gap: 32, marginBottom: 36 }}>
              {[['3+', 'Strategies'], ['8+', 'Indices'], ['24/7', 'Monitoring'], ['<1s', 'Execution']].map(([n, l]) => (
                <div key={l} style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '1.8rem', fontWeight: 800, color: '#fff' }}>{n}</div>
                  <div style={{ fontSize: '.72rem', color: '#64748b', textTransform: 'uppercase', letterSpacing: '.8px' }}>{l}</div>
                </div>
              ))}
            </div>

            {/* Tags */}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {['🔗 Delta Exchange', '📊 NIFTY / BANKNIFTY', '₿ BTC / ETH', '🛡 Risk Management', '📈 Backtesting'].map(t => (
                <span key={t} style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', padding: '6px 14px', borderRadius: 20, fontSize: '.78rem', color: '#94a3b8' }}>{t}</span>
              ))}
            </div>

            {/* Trust */}
            <div style={{ marginTop: 32, display: 'flex', gap: 20, alignItems: 'center' }}>
              <div style={{ display: 'flex' }}>
                {['R', 'A', 'S', 'K'].map((l, i) => <div key={i} style={{ width: 28, height: 28, borderRadius: '50%', background: ['#6366f1', '#8b5cf6', '#a855f7', '#c084fc'][i], color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '.7rem', fontWeight: 700, marginLeft: i ? -8 : 0, border: '2px solid #0f172a' }}>{l}</div>)}
              </div>
              <div style={{ fontSize: '.82rem', color: '#94a3b8' }}><b style={{ color: '#fff' }}>100+</b> traders trust AlgoX</div>
            </div>
          </div>

          {/* Login Card */}
          <div className="login-card" id="auth">
            <h2>Get Started</h2>
            <div className="sub">Start trading in under 2 minutes</div>
            <div className="tabs">
              <button className={`tab ${tab === 'login' ? 'active' : ''}`} onClick={() => setTab('login')}>Login</button>
              <button className={`tab ${tab === 'register' ? 'active' : ''}`} onClick={() => setTab('register')}>Register</button>
            </div>
            {error && <div className="error-msg">{error}</div>}
            <form onSubmit={handleSubmit}>
              <div className="field"><label>Username</label><input value={username} onChange={e => setUsername(e.target.value)} placeholder="Enter username" required /></div>
              <div className="field"><label>Password</label><input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Enter password" required /></div>
              {tab === 'register' && <div className="field"><label>Confirm Password</label><input type="password" value={confirm} onChange={e => setConfirm(e.target.value)} placeholder="Confirm password" required /></div>}
              <button type="submit" className="btn-primary-lg">{tab === 'login' ? 'Login →' : 'Create Account →'}</button>
            </form>
            <div style={{ textAlign: 'center', marginTop: 16, fontSize: '.75rem', color: '#64748b' }}>🪙 Get <b style={{ color: '#a5b4fc' }}>50 free credits</b> on signup</div>
          </div>
        </div>
      </div>

      {/* Strategies Section */}
      <section id="strategies" style={{ padding: '80px 40px', background: '#0f172a' }}>
        <div style={{ maxWidth: 1100, margin: '0 auto', textAlign: 'center' }}>
          <h2 style={{ fontSize: '2rem', fontWeight: 800, color: '#fff', marginBottom: 8 }}>Built-in Strategies</h2>
          <p style={{ color: '#64748b', fontSize: '.95rem', marginBottom: 48 }}>Battle-tested strategies ready to deploy</p>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 20 }}>
            {[
              { icon: '⚡', name: 'Delta Neutral', type: 'Options', desc: 'Short strangle with auto-rebalancing. Sells call + put at matching deltas, monitors premiums, adjusts when one leg spikes.', tags: ['Short Strangle', 'Auto Adjust', 'P&L Target'] },
              { icon: '📊', name: 'RSI Div + MSS', type: 'Futures', desc: 'Detects RSI divergence combined with market structure shifts for high-probability reversal entries with defined risk.', tags: ['RSI Divergence', 'Structure Shift', '2:1 R:R'] },
              { icon: '📈', name: 'SMA + Volume', type: 'Futures', desc: 'Enters on strong SMA50 breakouts confirmed by high increasing volume. Filters weak breakouts prone to reversal.', tags: ['SMA 50', 'Volume Confirm', 'Trend Entry'] },
            ].map(s => (
              <div key={s.name} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 16, padding: 28, textAlign: 'left', transition: 'all .25s' }}>
                <div style={{ fontSize: '1.6rem', marginBottom: 12 }}>{s.icon}</div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
                  <span style={{ fontWeight: 700, fontSize: '1rem', color: '#fff' }}>{s.name}</span>
                  <span style={{ fontSize: '.65rem', padding: '2px 8px', borderRadius: 4, background: s.type === 'Options' ? 'rgba(99,102,241,0.2)' : 'rgba(245,158,11,0.2)', color: s.type === 'Options' ? '#a5b4fc' : '#fbbf24', fontWeight: 600 }}>{s.type}</span>
                </div>
                <p style={{ fontSize: '.85rem', color: '#94a3b8', lineHeight: 1.6, marginBottom: 14 }}>{s.desc}</p>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {s.tags.map(t => <span key={t} style={{ fontSize: '.68rem', padding: '3px 10px', borderRadius: 4, background: 'rgba(255,255,255,0.05)', color: '#64748b', fontWeight: 500 }}>{t}</span>)}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Features */}
      <section id="features" style={{ padding: '80px 40px', background: '#1e293b' }}>
        <div style={{ maxWidth: 1100, margin: '0 auto', textAlign: 'center' }}>
          <h2 style={{ fontSize: '2rem', fontWeight: 800, color: '#fff', marginBottom: 8 }}>Everything You Need</h2>
          <p style={{ color: '#64748b', fontSize: '.95rem', marginBottom: 48 }}>Professional-grade tools for options & futures traders</p>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 20 }}>
            {[
              ['🎯', 'Delta-Neutral', 'Auto sell strangles at target delta, monitor in real-time, rebalance when premiums deviate.'],
              ['📡', 'Live Option Chain', 'Real-time chain with OI, IV, greeks. Click to build strategies. Payoff charts update instantly.'],
              ['📈', 'Payoff Analysis', 'Interactive payoff diagrams with time decay slider and spot price simulation.'],
              ['🔄', 'Smart Adjustments', 'Auto-adjust when premiums spike. Close losing leg, re-enter at matching delta.'],
              ['🛡', 'Risk Management', 'Per-leg SL, overall targets, trailing SL, max adjustments. Auto-exit at P&L targets.'],
              ['📊', 'Backtesting', 'Test strategies on historical data before deploying. See win rate, P&L, and trade details.'],
            ].map(([icon, title, desc]) => (
              <div key={title} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 14, padding: 24, textAlign: 'left', transition: 'all .25s' }}>
                <div style={{ fontSize: '1.4rem', marginBottom: 10 }}>{icon}</div>
                <div style={{ fontWeight: 700, fontSize: '.95rem', color: '#fff', marginBottom: 6 }}>{title}</div>
                <p style={{ fontSize: '.82rem', color: '#94a3b8', lineHeight: 1.6 }}>{desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" style={{ padding: '80px 40px', background: '#0f172a' }}>
        <div style={{ maxWidth: 900, margin: '0 auto', textAlign: 'center' }}>
          <h2 style={{ fontSize: '2rem', fontWeight: 800, color: '#fff', marginBottom: 8 }}>Simple Pricing</h2>
          <p style={{ color: '#64748b', fontSize: '.95rem', marginBottom: 48 }}>Start free, upgrade when you need more</p>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 20 }}>
            {[
              { name: 'Free', price: '₹0', credits: '50', features: ['1 live strategy', '1 broker', 'Option chain', 'Basic backtesting'], cta: 'Get Started', popular: false },
              { name: 'Pro', price: '₹999', credits: '∞', features: ['Unlimited strategies', 'Unlimited brokers', 'All features', 'Priority support', 'Advanced analytics'], cta: 'Go Pro', popular: true },
              { name: 'Basic', price: '₹499', credits: '500', features: ['5 live strategies', '3 brokers', 'All strategies', 'Email support'], cta: 'Start Basic', popular: false },
            ].map(p => (
              <div key={p.name} style={{ background: p.popular ? 'linear-gradient(135deg,rgba(99,102,241,0.15),rgba(139,92,246,0.1))' : 'rgba(255,255,255,0.03)', border: `1px solid ${p.popular ? 'rgba(99,102,241,0.4)' : 'rgba(255,255,255,0.06)'}`, borderRadius: 16, padding: 28, position: 'relative' }}>
                {p.popular && <div style={{ position: 'absolute', top: -12, left: '50%', transform: 'translateX(-50%)', background: 'linear-gradient(135deg,#6366f1,#7c3aed)', color: '#fff', fontSize: '.65rem', fontWeight: 800, padding: '4px 16px', borderRadius: 12, letterSpacing: '1px' }}>POPULAR</div>}
                <div style={{ fontSize: '.82rem', color: '#64748b', textTransform: 'uppercase', letterSpacing: '1px', fontWeight: 700, marginBottom: 8 }}>{p.name}</div>
                <div style={{ fontSize: '2.2rem', fontWeight: 800, color: '#fff', marginBottom: 4 }}>{p.price}<small style={{ fontSize: '.8rem', color: '#64748b', fontWeight: 400 }}>/mo</small></div>
                <div style={{ fontSize: '.82rem', color: '#a5b4fc', fontWeight: 600, marginBottom: 16 }}>{p.credits} credits/month</div>
                <ul style={{ listStyle: 'none', textAlign: 'left', marginBottom: 20, padding: 0 }}>
                  {p.features.map(f => <li key={f} style={{ fontSize: '.82rem', color: '#94a3b8', padding: '5px 0', borderBottom: '1px solid rgba(255,255,255,0.04)' }}>✓ {f}</li>)}
                </ul>
                <span onClick={() => scrollTo('auth')} style={{ display: 'block', padding: 12, borderRadius: 10, fontSize: '.88rem', fontWeight: 700, textAlign: 'center', cursor: 'pointer', background: p.popular ? 'linear-gradient(135deg,#6366f1,#7c3aed)' : 'transparent', color: p.popular ? '#fff' : '#94a3b8', border: p.popular ? 'none' : '1px solid rgba(255,255,255,0.1)', boxShadow: p.popular ? '0 4px 16px rgba(99,102,241,.3)' : 'none' }}>{p.cta}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Footer */}
      <div style={{ padding: 40, textAlign: 'center', color: '#64748b', fontSize: '.78rem', background: '#0f172a', borderTop: '1px solid rgba(255,255,255,0.06)' }}>© 2026 AlgoX. Built for traders, by traders. Use at your own risk.</div>
    </>
  );
}
