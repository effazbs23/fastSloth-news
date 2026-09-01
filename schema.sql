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

-- Links each story to the run that fetched it, so the dashboard can show
-- "last fetched news" as exactly one run's batch instead of a time window.
-- Safe to re-run against an already-provisioned DB.
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS cron_log_id INTEGER REFERENCES cron_logs(id);
CREATE INDEX IF NOT EXISTS idx_news_items_cron_log_id ON news_items(cron_log_id);
