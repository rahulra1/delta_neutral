import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from './context/AuthContext';
import Navbar from './components/Navbar';
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

function PrivateRoute({ children }) {
  const { user } = useAuth();
  return user ? children : <Navigate to="/login" />;
}

export default function App() {
  const { user } = useAuth();
  return (
    <>
      {user && <Navbar />}
      <Routes>
        <Route path="/login" element={user ? <Navigate to="/" /> : <Login />} />
        <Route path="/" element={<PrivateRoute><Dashboard /></PrivateRoute>} />
        <Route path="/strategy/new" element={<PrivateRoute><Strategy /></PrivateRoute>} />
        <Route path="/strategy/:sid" element={<PrivateRoute><StrategyDetail /></PrivateRoute>} />
        <Route path="/option-chain" element={<PrivateRoute><OptionChain /></PrivateRoute>} />
        <Route path="/strategy-builder" element={<PrivateRoute><StrategyBuilder /></PrivateRoute>} />
        <Route path="/performance" element={<PrivateRoute><Performance /></PrivateRoute>} />
        <Route path="/profile" element={<PrivateRoute><Profile /></PrivateRoute>} />
        <Route path="/broker" element={<PrivateRoute><Broker /></PrivateRoute>} />
        <Route path="/broker/setup" element={<PrivateRoute><BrokerSetup /></PrivateRoute>} />
        <Route path="/chart" element={<PrivateRoute><ChartPage /></PrivateRoute>} />
        <Route path="/admin" element={<PrivateRoute><Admin /></PrivateRoute>} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </>
  );
}
