CREATE TABLE IF NOT EXISTS news_items (
    id SERIAL PRIMARY KEY,
    url TEXT UNIQUE,
    source TEXT,
    title TEXT,
    location TEXT,
    context TEXT,
    accused_victim TEXT,
    issues TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cron_logs (
    id SERIAL PRIMARY KEY,
    status TEXT,
    fetched_per_provider JSONB,
    total_fetched INT,
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
