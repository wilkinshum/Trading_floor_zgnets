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
            signal.get("weights", {}).get("momentum", 0.0),
            signal.get("weights", {}).get("meanrev", 0.0),
            signal.get("weights", {}).get("breakout", 0.0),
            signal.get("weights", {}).get("news", 0.0),
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
