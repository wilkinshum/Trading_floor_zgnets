import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class Position:
    symbol: str
    quantity: int
    avg_price: float
    current_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.quantity

@dataclass
class PortfolioState:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    equity: float = 0.0

class Portfolio:
    def __init__(self, cfg):
        self.cfg = cfg
        self.file_path = Path("portfolio.json")
        self.state = self._load()

    def _load(self) -> PortfolioState:
        if not self.file_path.exists():
            initial_cash = self.cfg.get("risk", {}).get("equity", 5000.0)
            return PortfolioState(cash=initial_cash)
        
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            positions = {}
            for sym, pos_data in data.get("positions", {}).items():
                positions[sym] = Position(
                    symbol=sym,
                    quantity=pos_data["quantity"],
                    avg_price=pos_data["avg_price"]
                )
            return PortfolioState(
                cash=data.get("cash", 0.0),
                positions=positions
            )
        except Exception:
            # Fallback if corrupt
            initial_cash = self.cfg.get("risk", {}).get("equity", 5000.0)
            return PortfolioState(cash=initial_cash)

    def save(self):
        data = {
            "cash": self.state.cash,
            "equity": self.state.equity,
            "positions": {
                sym: {
                    "quantity": p.quantity,
                    "avg_price": p.avg_price
                } for sym, p in self.state.positions.items()
            }
        }
        self.file_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def mark_to_market(self, prices: Dict[str, float]):
        """Update current prices and equity"""
        pos_value = 0.0
        for sym, pos in self.state.positions.items():
            price = prices.get(sym)
            if price:
                pos.current_price = price
            pos_value += pos.market_value
        self.state.equity = self.state.cash + pos_value

    def execute(self, symbol: str, side: str, price: float, quantity: int = 0) -> float:
        """
        Execute a trade. 
        Returns realized PnL for closing trades, 0.0 for opening.
        Adjusts cash and positions.
        """
        # Default sizing if 0: rudimentary "max positions" sizing
        if quantity == 0 and side == "BUY":
            # Simple equal weight sizing: Equity / Max Positions
            max_pos = self.cfg.get("risk", {}).get("max_positions", 2)
            target_alloc = self.state.equity / max_pos
            quantity = int(target_alloc // price)
            if quantity < 1: quantity = 1

        if quantity == 0 and side == "SELL":
            # Assume full exit if quantity 0
            if symbol in self.state.positions:
                quantity = self.state.positions[symbol].quantity

        realized_pnl = 0.0
        
        if side == "BUY":
            cost = price * quantity
            if self.state.cash >= cost:
                self.state.cash -= cost
                if symbol in self.state.positions:
                    # Averaging up/down
                    pos = self.state.positions[symbol]
                    total_cost = (pos.quantity * pos.avg_price) + cost
                    pos.quantity += quantity
                    pos.avg_price = total_cost / pos.quantity
                else:
                    self.state.positions[symbol] = Position(symbol, quantity, price, price)
        
        elif side == "SELL":
            if symbol in self.state.positions:
                pos = self.state.positions[symbol]
                # If selling more than we have, just sell what we have (no shorts yet for simplicity)
                qty_to_sell = min(quantity, pos.quantity)
                
                proceeds = price * qty_to_sell
                cost_basis = pos.avg_price * qty_to_sell
                
                self.state.cash += proceeds
                realized_pnl = proceeds - cost_basis
                
                pos.quantity -= qty_to_sell
                if pos.quantity <= 0:
                    del self.state.positions[symbol]

        return realized_pnl
