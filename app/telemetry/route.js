import { Client } from 'pg';

export async function GET() {
  const client = new Client({ connectionString: process.env.DATABASE_URL });
  await client.connect();

  const logs = await client.query('SELECT * FROM cron_logs ORDER BY executed_at DESC LIMIT 10');
  const news = await client.query('SELECT * FROM news_items ORDER BY created_at DESC LIMIT 20');
  await client.end();

  return Response.json({ cronLogs: logs.rows, newsItems: news.rows });
}
