"""Base strategy interface — all strategy types must implement these methods."""
from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """Common interface for all trading strategies."""

    @abstractmethod
    def initialize(self):
        """Set up the strategy (fetch chain, place orders, start WS). Returns True on success."""

    @abstractmethod
    def monitor(self):
        """Run the monitoring loop (blocking). Exits on target/SL/manual stop."""

    @abstractmethod
    def close_all(self):
        """Close all open positions and stop monitoring."""

    def stop(self):
        """Graceful shutdown — close positions and clean up resources."""
        self.close_all()

    @property
    @abstractmethod
    def pnl(self):
        """Current total P&L as a float."""
