import sqlite3
from pathlib import Path
from datetime import datetime
import json

class Database:
    def __init__(self, db_path="trading.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # Trades Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                side TEXT,
                quantity INTEGER,
                price REAL,
                pnl REAL,
                score REAL,
                strategy_data TEXT
            )
        """)
        
        # Signals Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                score_mom REAL,
                score_mean REAL,
                score_break REAL,
                score_news REAL,
                weight_mom REAL,
                weight_mean REAL,
                weight_break REAL,
                weight_news REAL,
                final_score REAL
            )
        """)
        
        # Events Table (General Logs)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                message TEXT,
                metadata TEXT
            )
        """)
        
        # Agent Memory Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                symbol TEXT,
                signal_type TEXT,
                signal_value REAL,
                outcome TEXT,
                pnl REAL DEFAULT 0,
                regime_spy TEXT,
                regime_vix TEXT,
                regime_label TEXT,
                confidence REAL,
                memory_influenced BOOLEAN DEFAULT 0,
                timestamp TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_memory_agent ON agent_memory(agent_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_memory_regime ON agent_memory(regime_label)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_memory_timestamp ON agent_memory(timestamp)")

        # Shadow Predictions Table (Kalman + HMM shadow mode)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shadow_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT,
                kalman_signal REAL,
                kalman_level REAL,
                kalman_trend REAL,
                kalman_uncertainty REAL,
                existing_signal REAL,
                hmm_state TEXT,
                hmm_bull_prob REAL,
                hmm_bear_prob REAL,
                hmm_transition_prob REAL,
                hmm_transition_risk REAL,
                existing_regime TEXT,
                actual_return_1h REAL,
                actual_return_1d REAL,
                outcome_filled BOOLEAN DEFAULT 0
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_shadow_timestamp ON shadow_predictions(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_shadow_symbol ON shadow_predictions(symbol)")

        # ── V4 Tables ────────────────────────────────────────────

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS position_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL CHECK(strategy IN ('intraday', 'swing')),
                side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                entry_order_id TEXT,
                entry_price REAL,
                entry_time TIMESTAMP,
                entry_qty REAL,
                exit_order_id TEXT,
                exit_price REAL,
                exit_time TIMESTAMP,
                stop_price REAL,
                tp_price REAL,
                max_hold_days INTEGER,
                signals_json TEXT,
                market_regime TEXT,
                sector TEXT,
                exit_reason TEXT CHECK(exit_reason IN ('tp', 'sl', 'trail', 'time', 'kill_switch', 'manual', NULL)),
                pnl REAL,
                pnl_pct REAL,
                status TEXT DEFAULT 'open' CHECK(status IN ('open', 'pending', 'closed', 'cancelled')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alpaca_order_id TEXT UNIQUE,
                client_order_id TEXT UNIQUE,
                position_meta_id INTEGER REFERENCES position_meta(id),
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                qty REAL NOT NULL,
                filled_qty REAL DEFAULT 0,
                limit_price REAL,
                stop_price REAL,
                avg_fill_price REAL,
                status TEXT DEFAULT 'pending',
                submitted_at TIMESTAMP,
                filled_at TIMESTAMP,
                cancelled_at TIMESTAMP,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER REFERENCES orders(id),
                alpaca_order_id TEXT,
                fill_price REAL NOT NULL,
                fill_qty REAL NOT NULL,
                fill_time TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS budget_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                reserved_amount REAL NOT NULL,
                order_id INTEGER REFERENCES orders(id),
                status TEXT DEFAULT 'reserved' CHECK(status IN ('reserved', 'filled', 'released')),
                created_at TIMESTAMP,
                released_at TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signal_accuracy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_meta_id INTEGER REFERENCES position_meta(id),
                strategy TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_score REAL,
                price_direction REAL,
                sector_return REAL,
                market_regime TEXT,
                was_correct BOOLEAN,
                adjusted_correct BOOLEAN,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_date DATE NOT NULL,
                strategy TEXT,
                trades_analyzed INTEGER,
                pnl REAL,
                win_rate REAL,
                signal_accuracy_json TEXT,
                recommendations_json TEXT,
                adjustments_applied_json TEXT,
                report_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changed_by TEXT NOT NULL,
                strategy TEXT,
                field_path TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                reverted BOOLEAN DEFAULT FALSE,
                reverted_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # V4 Indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_position_meta_strategy_status ON position_meta(strategy, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_position_meta_symbol ON position_meta(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol, strategy)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signal_accuracy_type ON signal_accuracy(signal_type, strategy)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_budget_reservations_strategy ON budget_reservations(strategy, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_config_history_field ON config_history(field_path, created_at)")

        conn.commit()
        conn.close()

    def log_trade(self, trade: dict):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trades (timestamp, symbol, side, quantity, price, pnl, score, strategy_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get("timestamp"),
            trade.get("symbol"),
            trade.get("side"),
            trade.get("quantity", 0),
            trade.get("price", 0.0),
            trade.get("pnl", 0.0),
            trade.get("score", 0.0),
            json.dumps(trade.get("metadata", {}))
        ))
        conn.commit()
        conn.close()

    def log_signal(self, signal: dict):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO signals (
                timestamp, symbol, 
                score_mom, score_mean, score_break, score_news,
                weight_mom, weight_mean, weight_break, weight_news,
                final_score
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.get("timestamp"),
            signal.get("symbol"),
            signal.get("components", {}).get("momentum", 0.0),
            signal.get("components", {}).get("meanrev", 0.0),
            signal.get("components", {}).get("breakout", 0.0),
            signal.get("components", {}).get("news", 0.0),
            signal.get("weights_used", signal.get("weights", {})).get("momentum", 0.0),
            signal.get("weights_used", signal.get("weights", {})).get("meanrev", 0.0),
            signal.get("weights_used", signal.get("weights", {})).get("breakout", 0.0),
            signal.get("weights_used", signal.get("weights", {})).get("news", 0.0),
            signal.get("final_score", 0.0)
        ))
        conn.commit()
        conn.close()
        
    def log_event(self, event: dict):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO events (timestamp, level, message, metadata)
            VALUES (?, ?, ?, ?)
        """, (
            event.get("timestamp", datetime.utcnow().isoformat()),
            event.get("level", "INFO"),
            event.get("message", ""),
            json.dumps(event.get("metadata", {}))
        ))
        conn.commit()
        conn.close()
