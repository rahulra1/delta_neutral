import React, { useState } from 'react';
import { useAuth } from '../context/AuthContext';

const navStyle = {
  position: 'fixed', top: 0, left: 0, right: 0, height: 60, background: 'rgba(255,255,255,0.85)',
  backdropFilter: 'blur(12px)', borderBottom: '1px solid #e2e8f0', display: 'flex',
  alignItems: 'center', justifyContent: 'space-between', padding: '0 40px', zIndex: 100
};
const navLinks = { display: 'flex', alignItems: 'center', gap: 32 };
const navLink = { color: '#64748b', textDecoration: 'none', fontSize: '.88rem', fontWeight: 500, cursor: 'pointer' };
const navBtn = {
  padding: '8px 20px', background: 'linear-gradient(135deg,#6366f1,#7c3aed)', color: '#fff',
  border: 'none', borderRadius: 8, fontSize: '.85rem', fontWeight: 600, cursor: 'pointer'
};

const statsRow = { display: 'flex', gap: 32, marginBottom: 28 };
const statNum = { fontSize: '1.8rem', fontWeight: 700, color: '#0f172a' };
const statLbl = { fontSize: '.72rem', textTransform: 'uppercase', color: '#94a3b8', letterSpacing: '.5px' };
const tagsRow = { display: 'flex', flexWrap: 'wrap', gap: 8 };
const tag = {
  background: '#fff', border: '1px solid #e2e8f0', borderRadius: 20,
  padding: '6px 16px', fontSize: '.78rem', color: '#475569', fontWeight: 500
};

const sectionStyle = { padding: '80px 40px', maxWidth: 1200, margin: '0 auto' };
const sectionTitle = { fontSize: '2rem', fontWeight: 800, textAlign: 'center', marginBottom: 12, color: '#0f172a' };
const sectionSub = { textAlign: 'center', color: '#64748b', marginBottom: 48, fontSize: '1rem' };
const gridThree = { display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 20 };

const featureCard = {
  background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 28,
  transition: 'transform .2s, box-shadow .2s', cursor: 'default'
};
const featureIcon = { fontSize: '2rem', marginBottom: 12 };
const featureH3 = { fontSize: '1rem', fontWeight: 700, marginBottom: 8, color: '#0f172a' };
const featureP = { fontSize: '.85rem', color: '#64748b', lineHeight: 1.6 };

const pricingCard = {
  background: '#fff', border: '1px solid #e2e8f0', borderRadius: 16, padding: 32, textAlign: 'center',
  transition: 'transform .2s, box-shadow .2s', position: 'relative'
};
const popularCard = {
  ...pricingCard, background: 'linear-gradient(135deg,#6366f1,#7c3aed)', color: '#fff',
  border: '1px solid #6366f1', transform: 'scale(1.04)'
};
const popularBadge = {
  position: 'absolute', top: -12, left: '50%', transform: 'translateX(-50%)',
  background: '#fbbf24', color: '#0f172a', padding: '4px 16px', borderRadius: 20,
  fontSize: '.72rem', fontWeight: 700, letterSpacing: '.5px'
};
const planName = { fontSize: '1.1rem', fontWeight: 700, marginBottom: 4 };
const planPrice = { fontSize: '2.4rem', fontWeight: 800, margin: '12px 0 4px' };
const planPer = { fontSize: '.82rem', opacity: .7, marginBottom: 20 };
const planFeature = { fontSize: '.85rem', padding: '6px 0', borderBottom: '1px solid rgba(0,0,0,.06)' };
const planFeatureWhite = { ...planFeature, borderBottomColor: 'rgba(255,255,255,.15)' };
const planBtn = {
  marginTop: 20, width: '100%', padding: '10px 0', borderRadius: 8, border: '1px solid #e2e8f0',
  background: '#fff', color: '#0f172a', fontSize: '.88rem', fontWeight: 600, cursor: 'pointer'
};
const planBtnPopular = {
  ...planBtn, background: '#fff', color: '#6366f1', border: 'none',
  boxShadow: '0 2px 10px rgba(0,0,0,.15)'
};

const footerStyle = {
  textAlign: 'center', padding: '32px 40px', borderTop: '1px solid #e2e8f0',
  color: '#94a3b8', fontSize: '.82rem'
};

const features = [
  { icon: '⚖️', title: 'Delta-Neutral Strategies', desc: 'Automatically sell strangles at matching deltas with real-time monitoring and smart adjustments.' },
  { icon: '🎨', title: 'Visual Strategy Builder', desc: 'Drag-and-drop interface to build multi-leg option strategies with live payoff visualization.' },
  { icon: '📊', title: 'Live Option Chain', desc: 'Real-time option chain with greeks, IV, and one-click trading directly from the chain.' },
  { icon: '📈', title: 'Payoff Analysis', desc: 'Interactive payoff diagrams with breakeven points, max profit/loss, and probability analysis.' },
  { icon: '🔄', title: 'Smart Adjustments', desc: 'Auto-adjust positions when premium deviates beyond thresholds. Never miss a rebalance.' },
  { icon: '🛡️', title: 'Risk Management', desc: 'Built-in P&L targets, position sizing, and portfolio-level risk controls.' }
];

const plans = [
  { name: 'Free', price: '₹0', per: 'forever', items: ['50 credits', '1 strategy slot', '1 broker connection', 'Basic analytics'], popular: false },
  { name: 'Basic', price: '₹499', per: '/month', items: ['500 credits', '5 strategy slots', '3 broker connections', 'Advanced analytics'], popular: true },
  { name: 'Pro', price: '₹999', per: '/month', items: ['Unlimited credits', 'Unlimited strategies', 'Unlimited brokers', 'Priority support'], popular: false }
];

export default function Login() {
  const { login, register } = useAuth();
  const [tab, setTab] = useState('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');

  const handleSubmit = async e => {
    e.preventDefault();
    setError('');
    try {
      if (tab === 'register') {
        if (password !== confirm) { setError('Passwords do not match'); return; }
        if (password.length < 6) { setError('Password must be at least 6 characters'); return; }
        await register(username, password);
      } else {
        await login(username, password);
      }
    } catch (err) {
      setError(err.response?.data?.error || 'Something went wrong');
    }
  };

  const scrollTo = id => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' });

  return (
    <div style={{ background: '#fafafa' }}>
      {/* Navbar */}
      <nav style={navStyle}>
        <div style={{ fontSize: '1.2rem', fontWeight: 800, color: '#0f172a' }}>⚡ AlgoX</div>
        <div style={navLinks}>
          <span style={navLink} onClick={() => scrollTo('features')}>Features</span>
          <span style={navLink} onClick={() => scrollTo('pricing')}>Pricing</span>
          <button style={navBtn} onClick={() => scrollTo('login-card')}>Login</button>
        </div>
      </nav>

      {/* Hero */}
      <div className="hero">
        <div className="hero-content">
          <div className="hero-left">
            <h1>Automate Your<br /><span className="gradient">Options Trading</span><br />Strategy</h1>
            <p>Build, backtest and deploy delta-neutral options strategies across BTC, NIFTY, BANKNIFTY and more.</p>
            <div style={statsRow}>
              {[['8+', 'Indices'], ['50+', 'Strategies'], ['24/7', 'Monitoring'], ['0.1s', 'Execution']].map(([n, l]) => (
                <div key={l}>
                  <div style={statNum}>{n}</div>
                  <div style={statLbl}>{l}</div>
                </div>
              ))}
            </div>
            <div style={tagsRow}>
              {['Delta Exchange', 'NIFTY/BANKNIFTY', 'BTC/ETH', 'Risk Management'].map(t => (
                <span key={t} style={tag}>{t}</span>
              ))}
            </div>
          </div>

          <div className="login-card" id="login-card">
            <h2>Get Started</h2>
            <div className="sub">Start trading in under 2 minutes</div>
            <div className="tabs">
              <button className={`tab ${tab === 'login' ? 'active' : ''}`} onClick={() => setTab('login')}>Login</button>
              <button className={`tab ${tab === 'register' ? 'active' : ''}`} onClick={() => setTab('register')}>Register</button>
            </div>
            {error && <div className="error-msg">{error}</div>}
            <form onSubmit={handleSubmit}>
              <div className="field"><label>Username</label><input value={username} onChange={e => setUsername(e.target.value)} required /></div>
              <div className="field"><label>Password</label><input type="password" value={password} onChange={e => setPassword(e.target.value)} required /></div>
              {tab === 'register' && <div className="field"><label>Confirm Password</label><input type="password" value={confirm} onChange={e => setConfirm(e.target.value)} required /></div>}
              <button type="submit" className="btn-primary-lg">{tab === 'login' ? 'Login →' : 'Create Account →'}</button>
            </form>
            <div style={{ textAlign: 'center', marginTop: 14, fontSize: '.72rem', color: '#94a3b8' }}>
              🪙 Get <strong style={{ color: '#6366f1' }}>50 free credits</strong> on signup
            </div>
          </div>
        </div>
      </div>

      {/* Features */}
      <section id="features" style={sectionStyle}>
        <h2 style={sectionTitle}>Everything You Need to Trade Smarter</h2>
        <p style={sectionSub}>Professional-grade tools for options traders of all levels</p>
        <div style={gridThree}>
          {features.map(f => (
            <div key={f.title} style={featureCard}
              onMouseEnter={e => { e.currentTarget.style.transform = 'translateY(-2px)'; e.currentTarget.style.boxShadow = '0 8px 24px rgba(0,0,0,.08)'; }}
              onMouseLeave={e => { e.currentTarget.style.transform = 'none'; e.currentTarget.style.boxShadow = 'none'; }}>
              <div style={featureIcon}>{f.icon}</div>
              <h3 style={featureH3}>{f.title}</h3>
              <p style={featureP}>{f.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" style={{ ...sectionStyle, background: '#f8fafc' }}>
        <h2 style={sectionTitle}>Simple, Transparent Pricing</h2>
        <p style={sectionSub}>Start free. Upgrade when you're ready.</p>
        <div style={{ ...gridThree, alignItems: 'center' }}>
          {plans.map(p => (
            <div key={p.name} style={p.popular ? popularCard : pricingCard}
              onMouseEnter={e => { if (!p.popular) { e.currentTarget.style.transform = 'translateY(-2px)'; e.currentTarget.style.boxShadow = '0 8px 24px rgba(0,0,0,.08)'; }}}
              onMouseLeave={e => { if (!p.popular) { e.currentTarget.style.transform = 'none'; e.currentTarget.style.boxShadow = 'none'; }}}>
              {p.popular && <div style={popularBadge}>POPULAR</div>}
              <div style={planName}>{p.name}</div>
              <div style={planPrice}>{p.price}</div>
              <div style={planPer}>{p.per}</div>
              {p.items.map(item => (
                <div key={item} style={p.popular ? planFeatureWhite : planFeature}>{item}</div>
              ))}
              <button style={p.popular ? planBtnPopular : planBtn} onClick={() => scrollTo('login-card')}>
                {p.name === 'Free' ? 'Get Started' : 'Subscribe'}
              </button>
            </div>
          ))}
        </div>
      </section>

      {/* Footer */}
      <footer style={footerStyle}>© 2026 AlgoX. Built for traders, by traders.</footer>
    </div>
  );
}
