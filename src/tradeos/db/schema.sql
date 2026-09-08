-- TradeOS persistent schema. Every trade must be reproducible from here.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    first_seen_at REAL NOT NULL,
    pair_created_at REAL,
    meta_json TEXT,
    UNIQUE (chain, address)
);

CREATE TABLE IF NOT EXISTS pair_snapshots (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    pair_address TEXT NOT NULL,
    token_address TEXT NOT NULL,
    dex TEXT,
    price_usd REAL,
    liquidity_usd REAL,
    volume_24h REAL,
    volume_1h REAL,
    volume_5m REAL,
    price_change_5m REAL,
    price_change_1h REAL,
    price_change_6h REAL,
    price_change_24h REAL,
    buys_5m INTEGER, sells_5m INTEGER,
    buys_1h INTEGER, sells_1h INTEGER,
    captured_at REAL NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_pair_snap ON pair_snapshots (chain, pair_address, captured_at);

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'trading',      -- trading | treasury | tracked
    strategy TEXT,
    risk_class TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE (chain, address)
);

CREATE TABLE IF NOT EXISTS wallet_scores (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    score REAL NOT NULL,               -- 0..100 smart-money score
    win_rate REAL,
    avg_return_pct REAL,
    trade_count INTEGER,
    classification TEXT,               -- smart_money | whale | neutral | suspicious
    scored_at REAL NOT NULL,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_wallet_scores ON wallet_scores (chain, address, scored_at);

CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,               -- correlation id, e.g. opp_ab12...
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pair_address TEXT,
    symbol TEXT,
    source TEXT NOT NULL,              -- discovery source
    status TEXT NOT NULL,              -- detected|analyzing|approved|rejected|executed|expired
    momentum_score REAL,
    smart_money_score REAL,
    whale_score REAL,
    risk_score REAL,
    overall_score REAL,
    scoring_version TEXT,
    data_snapshot_json TEXT,           -- market data at decision time
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_status ON opportunities (status, created_at);

CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY,
    opportunity_id TEXT,
    agent TEXT NOT NULL,
    verdict TEXT NOT NULL,             -- buy|sell|hold|approve|reject|needs_evidence
    confidence REAL,
    reasoning TEXT,
    evidence_json TEXT,
    model_used TEXT,                   -- model id or 'heuristic'
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_dec ON agent_decisions (opportunity_id);

CREATE TABLE IF NOT EXISTS research_reports (
    id INTEGER PRIMARY KEY,
    opportunity_id TEXT,
    agent TEXT NOT NULL,
    fact_json TEXT,
    inference_json TEXT,
    speculation_json TEXT,
    sources_json TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    opportunity_id TEXT,
    position_id INTEGER,
    wallet_id INTEGER,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    symbol TEXT,
    side TEXT NOT NULL,                -- buy | sell
    mode TEXT NOT NULL,                -- paper | live
    requested_usd REAL NOT NULL,
    filled_usd REAL,
    price_usd REAL,
    quantity REAL,
    slippage_pct REAL,
    gas_usd REAL,
    tx_hash TEXT,
    status TEXT NOT NULL,              -- pending|filled|failed|cancelled
    error TEXT,
    created_at REAL NOT NULL,
    filled_at REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades (created_at);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    opportunity_id TEXT,
    wallet_id INTEGER,
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pair_address TEXT,
    symbol TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,              -- open | closed
    entry_price_usd REAL NOT NULL,
    quantity REAL NOT NULL,
    cost_usd REAL NOT NULL,
    stop_loss_pct REAL,
    take_profit_pct REAL,
    max_hold_hours REAL,
    last_price_usd REAL,
    exit_price_usd REAL,
    exit_reason TEXT,
    realized_pnl_usd REAL,
    opened_at REAL NOT NULL,
    closed_at REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);

CREATE TABLE IF NOT EXISTS portfolio_ledger (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    kind TEXT NOT NULL,                -- deposit|buy|sell|fee|adjustment
    amount_usd REAL NOT NULL,          -- signed cash delta
    ref_trade_id INTEGER,
    note TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY,
    opportunity_id TEXT,
    kind TEXT NOT NULL,                -- approval|rejection|veto|circuit_breaker|kill_switch
    rule TEXT,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS post_trade_reviews (
    id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL,
    opportunity_id TEXT,
    review_json TEXT NOT NULL,
    pnl_usd REAL,
    followed_strategy INTEGER,
    followed_risk_rules INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,              -- proposed|testing|live|retired
    created_at REAL NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,               -- agent name, 'system', 'user'
    action TEXT NOT NULL,
    opportunity_id TEXT,
    detail_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (created_at);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    layer TEXT NOT NULL,               -- user | market | trading | agent
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (layer, key)
);

CREATE TABLE IF NOT EXISTS wallet_swaps (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    wallet TEXT NOT NULL,
    signature TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    direction TEXT NOT NULL,           -- buy | sell (of token_mint, vs SOL)
    token_amount REAL,
    sol_amount REAL,                   -- SOL paid (buy) or received (sell)
    counter_mint TEXT,
    block_time REAL,
    recorded_at REAL NOT NULL,
    UNIQUE (signature, wallet, token_mint)
);
CREATE INDEX IF NOT EXISTS idx_wallet_swaps_token ON wallet_swaps (chain, token_mint, block_time);
CREATE INDEX IF NOT EXISTS idx_wallet_swaps_wallet ON wallet_swaps (chain, wallet, block_time);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    priority TEXT NOT NULL,            -- critical | high | info
    title TEXT NOT NULL,
    body TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
