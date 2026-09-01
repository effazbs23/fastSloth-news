'use client';
import { useEffect, useState } from 'react';

export default function Dashboard() {
  const [data, setData] = useState(null);

  useEffect(() => {
    fetch('/api/telemetry').then(res => res.json()).then(setData);
  }, []);

  if (!data) return <div className="p-8">Loading Telemetry...</div>;

  const lastRun = data.cronLogs[0];

  return (
    <div className="p-8 max-w-4xl mx-auto font-sans space-y-6">
      <h1 className="text-2xl font-bold">📡 Scraper Telemetry & Control Panel</h1>
      
      {/* Metrics Card */}
      <div className="p-4 border rounded bg-slate-50 space-y-2">
        <h2 className="font-semibold text-lg">Last Execution Status</h2>
        <p><strong>Run Time:</strong> {new Date(lastRun?.executed_at).toLocaleString()}</p>
        <p><strong>Status:</strong> <span className="text-green-600 font-bold">{lastRun?.status}</span></p>
        <p><strong>Total Stories Extracted:</strong> {lastRun?.total_fetched}</p>
        
        <div className="mt-2">
          <strong>Per-Provider Extraction Count:</strong>
          <ul className="list-disc ml-5">
            {lastRun && Object.entries(lastRun.fetched_per_provider).map(([provider, count]) => (
              <li key={provider}>{provider}: <strong>{count}</strong></li>
            ))}
          </ul>
        </div>
      </div>

      {/* Extracted News Log */}
      <div className="p-4 border rounded space-y-4">
        <h2 className="font-semibold text-lg">Processed Feed ({data.newsItems.length})</h2>
        {data.newsItems.map(item => (
          <div key={item.id} className="border-b pb-2">
            <span className="text-xs bg-slate-200 px-2 py-1 rounded mr-2 font-bold">{item.source}</span>
            <a href={item.url} target="_blank" className="font-medium hover:underline">{item.title}</a>
            <p className="text-xs text-slate-500 mt-1">📍 {item.location} | 👥 {item.accused_victim}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
