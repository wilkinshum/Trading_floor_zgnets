# Sector/Industry mapping for the trading universe
# Used by sector news filter to check sector-level sentiment before entries

SECTOR_MAP = {
    # --- Tech / Software ---
    "MSFT":  {"sector": "Technology", "industry": "Software", "sector_etf": "XLK"},
    "GOOGL": {"sector": "Technology", "industry": "Internet Services", "sector_etf": "XLK"},
    "META":  {"sector": "Technology", "industry": "Social Media", "sector_etf": "XLK"},
    "AMZN":  {"sector": "Technology", "industry": "E-Commerce/Cloud", "sector_etf": "XLK"},
    "ORCL":  {"sector": "Technology", "industry": "Enterprise Software", "sector_etf": "XLK"},
    "CRWD":  {"sector": "Technology", "industry": "Cybersecurity", "sector_etf": "CIBR"},
    "NFLX":  {"sector": "Technology", "industry": "Streaming", "sector_etf": "XLK"},

    # --- Semiconductors ---
    "NVDA":  {"sector": "Semiconductors", "industry": "AI Chips", "sector_etf": "SMH"},
    "AMD":   {"sector": "Semiconductors", "industry": "Chips", "sector_etf": "SMH"},
    "TSM":   {"sector": "Semiconductors", "industry": "Foundry", "sector_etf": "SMH"},
    "ASML":  {"sector": "Semiconductors", "industry": "Lithography", "sector_etf": "SMH"},
    "QCOM":  {"sector": "Semiconductors", "industry": "Mobile Chips", "sector_etf": "SMH"},
    "VRT":   {"sector": "Semiconductors", "industry": "AI Infrastructure", "sector_etf": "SMH"},
    "ANET":  {"sector": "Semiconductors", "industry": "Networking", "sector_etf": "SMH"},

    # --- Quantum Computing ---
    "IONQ":  {"sector": "Quantum Computing", "industry": "Quantum Hardware", "sector_etf": "QTUM"},
    "RGTI":  {"sector": "Quantum Computing", "industry": "Quantum Hardware", "sector_etf": "QTUM"},
    "QBTS":  {"sector": "Quantum Computing", "industry": "Quantum Software", "sector_etf": "QTUM"},

    # --- Crypto Mining / AI Data Centers ---
    "IREN":  {"sector": "Crypto/AI Infra", "industry": "Mining/Data Centers", "sector_etf": "BITQ"},
    "HUT":   {"sector": "Crypto/AI Infra", "industry": "Mining", "sector_etf": "BITQ"},
    "MARA":  {"sector": "Crypto/AI Infra", "industry": "Mining", "sector_etf": "BITQ"},
    "RIOT":  {"sector": "Crypto/AI Infra", "industry": "Mining", "sector_etf": "BITQ"},
    "CORZ":  {"sector": "Crypto/AI Infra", "industry": "Mining/Data Centers", "sector_etf": "BITQ"},
    "BITF":  {"sector": "Crypto/AI Infra", "industry": "Mining", "sector_etf": "BITQ"},

    # --- Space / Aerospace & Defense ---
    "RKLB":  {"sector": "Space/Defense", "industry": "Launch Services", "sector_etf": "ITA"},
    "ASTS":  {"sector": "Space/Defense", "industry": "Satellite", "sector_etf": "ITA"},
    "LUNR":  {"sector": "Space/Defense", "industry": "Lunar Services", "sector_etf": "ITA"},
    "RDW":   {"sector": "Space/Defense", "industry": "Space Components", "sector_etf": "ITA"},
    "KTOS":  {"sector": "Space/Defense", "industry": "Drones/Defense", "sector_etf": "ITA"},
    "AVAV":  {"sector": "Space/Defense", "industry": "Drones", "sector_etf": "ITA"},

    # --- Energy / Nuclear / Utilities ---
    "GEV":   {"sector": "Energy", "industry": "Gas Turbines", "sector_etf": "XLE"},
    "CEG":   {"sector": "Energy", "industry": "Nuclear Utility", "sector_etf": "XLE"},
    "CCJ":   {"sector": "Energy", "industry": "Uranium", "sector_etf": "URA"},
    "OKLO":  {"sector": "Energy", "industry": "Nuclear/SMR", "sector_etf": "URA"},
    "VST":   {"sector": "Energy", "industry": "Power Generation", "sector_etf": "XLE"},
    "UUUU":  {"sector": "Energy", "industry": "Uranium/REE", "sector_etf": "URA"},

    # --- Clean Energy / Storage ---
    "EOSE":  {"sector": "Clean Energy", "industry": "Energy Storage", "sector_etf": "ICLN"},
    "FLNC":  {"sector": "Clean Energy", "industry": "Energy Storage", "sector_etf": "ICLN"},
    "ONDS":  {"sector": "Clean Energy", "industry": "Smart Grid", "sector_etf": "ICLN"},

    # --- EV / Automotive ---
    "TSLA":  {"sector": "EV/Auto", "industry": "Electric Vehicles", "sector_etf": "IDRV"},

    # --- Healthcare / Biotech ---
    "ISRG":  {"sector": "Healthcare", "industry": "Surgical Robotics", "sector_etf": "XLV"},
    "GH":    {"sector": "Healthcare", "industry": "Genomics", "sector_etf": "XBI"},
    "GRAL":  {"sector": "Healthcare", "industry": "Genomics", "sector_etf": "XBI"},
    "MIRM":  {"sector": "Healthcare", "industry": "Biotech", "sector_etf": "XBI"},
    "UNH":   {"sector": "Healthcare", "industry": "Insurance/Managed Care", "sector_etf": "XLV"},

    # --- Infrastructure / Industrial ---
    "MTZ":   {"sector": "Industrial", "industry": "Infrastructure", "sector_etf": "XLI"},
    "AGX":   {"sector": "Industrial", "industry": "Infrastructure", "sector_etf": "XLI"},
    "POWL":  {"sector": "Industrial", "industry": "Electrical Equipment", "sector_etf": "XLI"},
    "SYM":   {"sector": "Industrial", "industry": "Warehouse Automation", "sector_etf": "XLI"},
    "TE":    {"sector": "Industrial", "industry": "Connectivity", "sector_etf": "XLI"},
    "AMTM":  {"sector": "Industrial", "industry": "Test & Measurement", "sector_etf": "XLI"},

    # --- Meme / Speculative ---
    "GME":   {"sector": "Consumer", "industry": "Retail/Meme", "sector_etf": "XLY"},

    # --- Small Cap Speculative ---
    "NBIS":  {"sector": "Technology", "industry": "AI/Analytics", "sector_etf": "XLK"},
    "CRML":  {"sector": "Technology", "industry": "AI/SaaS", "sector_etf": "XLK"},
    "TMC":   {"sector": "Materials", "industry": "Deep Sea Mining", "sector_etf": "XLB"},
    "TMQ":   {"sector": "Materials", "industry": "Mining", "sector_etf": "XLB"},
    "IDR":   {"sector": "Industrial", "industry": "Remediation", "sector_etf": "XLI"},
    "ELVA":  {"sector": "Healthcare", "industry": "Biotech", "sector_etf": "XBI"},

    # --- ETFs (skip sector filter) ---
    "SPY":   {"sector": "ETF", "industry": "Broad Market", "sector_etf": None},
    "QQQ":   {"sector": "ETF", "industry": "Nasdaq 100", "sector_etf": None},

    # === NEW ADDITIONS (expanding universe) ===

    # --- Financials ---
    "JPM":   {"sector": "Financials", "industry": "Banking", "sector_etf": "XLF"},
    "GS":    {"sector": "Financials", "industry": "Investment Banking", "sector_etf": "XLF"},
    "V":     {"sector": "Financials", "industry": "Payments", "sector_etf": "XLF"},
    "XYZ":   {"sector": "Financials", "industry": "Fintech", "sector_etf": "ARKF"},  # was SQ, Block rebranded
    "COIN":  {"sector": "Financials", "industry": "Crypto Exchange", "sector_etf": "BITQ"},

    # --- Consumer ---
    "COST":  {"sector": "Consumer", "industry": "Retail", "sector_etf": "XLY"},
    "SBUX":  {"sector": "Consumer", "industry": "Restaurants", "sector_etf": "XLY"},
    "NKE":   {"sector": "Consumer", "industry": "Apparel", "sector_etf": "XLY"},

    # --- Materials ---
    "FSLR":  {"sector": "Clean Energy", "industry": "Solar", "sector_etf": "TAN"},
    "MP":    {"sector": "Materials", "industry": "Rare Earth", "sector_etf": "XLB"},

    # --- Robotics / AI ---
    "PATH":  {"sector": "Technology", "industry": "AI/Automation", "sector_etf": "BOTZ"},
    "PLTR":  {"sector": "Technology", "industry": "AI/Analytics", "sector_etf": "XLK"},

    # --- Crypto adjacent ---
    "MSTR":  {"sector": "Crypto/AI Infra", "industry": "Bitcoin Treasury", "sector_etf": "BITQ"},
}

# Unique sectors for news queries
SECTOR_QUERIES = {
    "Technology": "technology stocks sector today",
    "Semiconductors": "semiconductor stocks chip sector today",
    "Quantum Computing": "quantum computing stocks today",
    "Crypto/AI Infra": "crypto mining bitcoin stocks today",
    "Space/Defense": "space defense stocks sector today",
    "Energy": "energy nuclear utility stocks today",
    "Clean Energy": "clean energy solar storage stocks today",
    "EV/Auto": "electric vehicle EV stocks today",
    "Healthcare": "healthcare biotech stocks today",
    "Industrial": "industrial infrastructure stocks today",
    "Consumer": "consumer retail stocks today",
    "Financials": "financial bank fintech stocks today",
    "Materials": "materials mining stocks today",
}

def get_sector(symbol: str) -> dict:
    """Get sector info for a symbol, returns empty dict if unknown."""
    return SECTOR_MAP.get(symbol, {})

def get_all_sectors() -> list[str]:
    """Get list of unique sectors in the universe."""
    return sorted(set(info["sector"] for info in SECTOR_MAP.values() if info["sector"] != "ETF"))
