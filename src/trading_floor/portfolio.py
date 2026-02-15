import json
import math
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

@dataclass
class Position:
    symbol: str
    quantity: int
    avg_price: float
    current_price: float = 0.0
    highest_price: float = 0.0 # For trailing stop (Longs)
    lowest_price: float = 0.0  # For trailing stop (Shorts)

    def __post_init__(self):
        if self.highest_price == 0.0: self.highest_price = self.avg_price
        if self.lowest_price == 0.0: self.lowest_price = self.avg_price

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
                    avg_price=pos_data["avg_price"],
                    highest_price=pos_data.get("highest_price", 0.0),
                    lowest_price=pos_data.get("lowest_price", 0.0)
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
                    "avg_price": p.avg_price,
                    "highest_price": p.highest_price,
                    "lowest_price": p.lowest_price
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
                # Update High/Low watermarks for Trailing Stop
                if price > pos.highest_price: pos.highest_price = price
                if price < pos.lowest_price or pos.lowest_price == 0.0: pos.lowest_price = price
            
            pos_value += pos.market_value
        self.state.equity = self.state.cash + pos_value

    def execute(self, symbol: str, side: str, price: float, quantity: int = 0, target_value: float = 0.0) -> float:
        """
        Execute a trade with Short Selling + Execution Realism (Slippage/Comm).
        Returns realized PnL.
        """
        # Load execution config
        exec_cfg = self.cfg.get("execution", {})
        slippage = exec_cfg.get("slippage_bps", 0) * 0.0001
        commission = exec_cfg.get("commission", 0.0)

        # Apply slippage
        if side == "BUY":
            exec_price = price * (1 + slippage)
        else:
            exec_price = price * (1 - slippage)

        # Guard: skip trade if exec_price is invalid
        if not exec_price or exec_price <= 0 or math.isnan(exec_price) or math.isinf(exec_price):
            logger.warning("Skipping trade for %s: invalid exec_price=%s", symbol, exec_price)
            return 0.0

        # Sizing Logic
        if quantity == 0:
            if target_value > 0:
                if math.isnan(target_value) or math.isinf(target_value):
                    logger.warning("Skipping trade for %s: invalid target_value=%s", symbol, target_value)
                    return 0.0
                # Use Volatility-Sized Target Value
                quantity = int(target_value // exec_price)
            else:
                # Fallback to Equal Weight
                max_pos = self.cfg.get("risk", {}).get("max_positions", 2)
                target_alloc = self.state.equity / max_pos
                if not target_alloc or math.isnan(target_alloc) or math.isinf(target_alloc) or target_alloc <= 0:
                    logger.warning("Skipping trade for %s: invalid target_alloc=%s (equity=%s)", symbol, target_alloc, self.state.equity)
                    return 0.0
                quantity = int(target_alloc // exec_price)
            
            if quantity < 1: quantity = 1

        # Calculate commission cost
        comm_cost = float(quantity) * commission
        
        realized_pnl = 0.0
        
        # --- BUY LOGIC ---
        if side == "BUY":
            cost = (exec_price * quantity) + comm_cost # Pay cost + comm
            
            if symbol in self.state.positions and self.state.positions[symbol].quantity < 0:
                # Covering Short
                pos = self.state.positions[symbol]
                qty_to_cover = min(quantity, abs(pos.quantity))
                
                entry_val = pos.avg_price * qty_to_cover
                exit_val = exec_price * qty_to_cover
                
                # Cash outflow: Buyback cost + commission
                self.state.cash -= (exit_val + comm_cost)
                
                # PnL = (ShortEntry - CoverExit) - Comm
                trade_pnl = (entry_val - exit_val) - comm_cost
                realized_pnl += trade_pnl
                
                pos.quantity += qty_to_cover
                
                remaining_qty = quantity - qty_to_cover
                if remaining_qty > 0:
                    # Flip to Long
                    cost_rem = (exec_price * remaining_qty) + (remaining_qty * commission)
                    if self.state.cash >= cost_rem:
                        self.state.cash -= cost_rem
                        if pos.quantity == 0:
                             pos.quantity = remaining_qty
                             pos.avg_price = exec_price # Slippage included in basis
                    else:
                        print(f"Not enough cash to flip long on {symbol}")

                if pos.quantity == 0:
                    del self.state.positions[symbol]
                    
            else:
                # Normal Long Buy
                if self.state.cash >= cost:
                    self.state.cash -= cost # Deduct total cost (inc comm)
                    if symbol in self.state.positions:
                        pos = self.state.positions[symbol]
                        # Avg price logic typically includes basis but excludes comm (comm is expense)
                        # But for simple PnL tracking, let's bake it in or treat as expense.
                        # Standard: Basis = (Qty * Price) + Comm
                        total_cost_basis = (pos.quantity * pos.avg_price) + (exec_price * quantity) + comm_cost
                        pos.quantity += quantity
                        pos.avg_price = total_cost_basis / pos.quantity
                    else:
                        # Initial basis includes commission
                        basis_price = exec_price + (comm_cost / quantity)
                        self.state.positions[symbol] = Position(symbol, quantity, basis_price, price)
                else:
                    print(f"Not enough cash to buy {symbol}")

        # --- SELL LOGIC ---
        elif side == "SELL":
            proceeds = (exec_price * quantity) - comm_cost # Receive proceeds less comm
            
            if symbol in self.state.positions and self.state.positions[symbol].quantity > 0:
                # Closing Long
                pos = self.state.positions[symbol]
                qty_to_sell = min(quantity, pos.quantity)
                
                sale_val = exec_price * qty_to_sell
                # Commission for this portion
                part_comm = qty_to_sell * commission
                net_proceeds = sale_val - part_comm
                
                cost_basis = pos.avg_price * qty_to_sell
                
                self.state.cash += net_proceeds
                realized_pnl += (net_proceeds - cost_basis)
                
                pos.quantity -= qty_to_sell
                
                remaining_qty = quantity - qty_to_sell
                if remaining_qty > 0:
                     # Flip to Short
                     short_proceeds = (exec_price * remaining_qty) - (remaining_qty * commission)
                     self.state.cash += short_proceeds
                     
                     if pos.quantity == 0:
                         pos.quantity = -remaining_qty
                         # Basis for short is Entry Price - (Comm/Qty) usually? 
                         # Or just Entry Price. Net proceeds matters for Cash.
                         # Let's track Entry Price as the Execution Price. Comm is expense.
                         # For PnL calc later: (Entry - Exit) - Comm.
                         # So let's store effective entry price: Price - (Comm/Qty)
                         effective_entry = exec_price - (commission / remaining_qty)
                         pos.avg_price = effective_entry
                         
                if pos.quantity == 0:
                    del self.state.positions[symbol]
            
            else:
                # Opening Short
                if self.state.equity > 0:
                     self.state.cash += proceeds
                     effective_entry = exec_price - (commission / quantity)
                     
                     if symbol in self.state.positions:
                         pos = self.state.positions[symbol]
                         total_val = (abs(pos.quantity) * pos.avg_price) + (effective_entry * quantity)
                         pos.quantity -= quantity
                         pos.avg_price = total_val / abs(pos.quantity)
                     else:
                         self.state.positions[symbol] = Position(symbol, -quantity, effective_entry, price)
                else:
                    print(f"Equity too low to short {symbol}")

        return realized_pnl
