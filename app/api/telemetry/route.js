import { Client } from 'pg';

export const dynamic = 'force-dynamic'; // always hit the DB - never cache/prerender telemetry

async function batchFor(client, logId) {
  const { rows } = await client.query(
    'SELECT * FROM news_items WHERE cron_log_id = $1 ORDER BY created_at DESC',
    [logId]
  );
  return rows;
}

export async function GET() {
  const client = new Client({ connectionString: process.env.DATABASE_URL });
  await client.connect();

  const logs = await client.query('SELECT * FROM cron_logs ORDER BY executed_at DESC LIMIT 10');

  // Show the in-progress run live - a story is claimed+posted the moment
  // main_pipeline.py gets to it, so this shouldn't wait for the whole run
  // (up to ~40min) to finish before it shows up. Fall back to the last
  // completed run's batch only while the new run hasn't posted anything yet.
  let latestLog = logs.rows[0];
  let latestBatch = latestLog ? await batchFor(client, latestLog.id) : [];
  if (latestLog?.status === 'RUNNING' && latestBatch.length === 0) {
    const prevLog = logs.rows.find((r) => r.status !== 'RUNNING');
    if (prevLog) {
      latestLog = prevLog;
      latestBatch = await batchFor(client, prevLog.id);
    }
  }

  const news = await client.query('SELECT * FROM news_items ORDER BY created_at DESC LIMIT 20');
  await client.end();

  return Response.json({ cronLogs: logs.rows, latestBatch, newsItems: news.rows });
}
