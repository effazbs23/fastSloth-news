import { Client } from 'pg';

export const dynamic = 'force-dynamic'; // always hit the DB - never cache/prerender telemetry

export async function GET() {
  const client = new Client({ connectionString: process.env.DATABASE_URL });
  await client.connect();

  const logs = await client.query('SELECT * FROM cron_logs ORDER BY executed_at DESC LIMIT 10');
  const latestLog = logs.rows.find((r) => r.status !== 'RUNNING');
  const latestBatch = latestLog
    ? (await client.query('SELECT * FROM news_items WHERE cron_log_id = $1 ORDER BY created_at DESC', [latestLog.id])).rows
    : [];
  const news = await client.query('SELECT * FROM news_items ORDER BY created_at DESC LIMIT 20');
  await client.end();

  return Response.json({ cronLogs: logs.rows, latestBatch, newsItems: news.rows });
}
