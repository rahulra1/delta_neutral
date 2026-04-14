import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from './context/AuthContext';
import Sidebar from './components/Sidebar';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Strategy from './pages/Strategy';
import StrategyDetail from './pages/StrategyDetail';
import OptionChain from './pages/OptionChain';
import StrategyBuilder from './pages/StrategyBuilder';
import Performance from './pages/Performance';
import Profile from './pages/Profile';
import Broker from './pages/Broker';
import BrokerSetup from './pages/BrokerSetup';
import ChartPage from './pages/ChartPage';
import Admin from './pages/Admin';
import StrategyLogs from './pages/StrategyLogs';

function PrivateRoute({ children }) {
  const { user } = useAuth();
  return user ? children : <Navigate to="/login" />;
}

export default function App() {
  const { user } = useAuth();
  return user ? (
    <div className="app-layout">
      <Sidebar />
      <main className="app-main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/strategy/new" element={<Strategy />} />
          <Route path="/strategy/:sid" element={<StrategyDetail />} />
          <Route path="/strategy/:sid/logs" element={<StrategyLogs />} />
          <Route path="/option-chain" element={<OptionChain />} />
          <Route path="/strategy-builder" element={<StrategyBuilder />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/broker" element={<Broker />} />
          <Route path="/broker/setup" element={<BrokerSetup />} />
          <Route path="/chart" element={<ChartPage />} />
          <Route path="/admin" element={<Admin />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </div>
  ) : (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="*" element={<Navigate to="/login" />} />
    </Routes>
  );
}
