'use client';
import { useEffect, useRef, useState } from 'react';

const STATUS_STYLES = {
  SUCCESS: 'bg-green-100 text-green-700',
  PARTIAL: 'bg-yellow-100 text-yellow-700',
  RUNNING: 'bg-blue-100 text-blue-700',
  ERROR: 'bg-red-100 text-red-700',
};

function StatusPill({ status }) {
  return (
    <span className={`px-2 py-1 rounded text-xs font-bold ${STATUS_STYLES[status] || 'bg-slate-100 text-slate-700'}`}>
      {status}
    </span>
  );
}

function NewsCard({ item }) {
  return (
    <div className="bg-white border rounded p-3 shadow-sm space-y-1">
      <a href={item.url} target="_blank" rel="noreferrer" className="font-medium text-sm hover:underline block">
        {item.title}
      </a>
      <p className="text-xs text-slate-500">📍 {item.location || 'N/A'}</p>
      <p className="text-xs text-slate-700">{item.context}</p>
      <p className="text-xs text-slate-500">👥 {item.accused_victim || 'N/A'}</p>
      <p className="text-xs text-slate-500">⚑ {item.issues || 'N/A'}</p>
    </div>
  );
}

function KanbanBoard({ items }) {
  if (!items.length) {
    return <p className="text-slate-500">No stories in the last fetch yet.</p>;
  }
  const columns = {};
  for (const item of items) {
    (columns[item.source] ??= []).push(item);
  }
  return (
    <div className="flex gap-4 overflow-x-auto pb-2">
      {Object.entries(columns).map(([source, stories]) => (
        <div key={source} className="min-w-[280px] w-[280px] shrink-0 bg-slate-100 rounded p-3 space-y-3">
          <h3 className="font-semibold text-sm flex justify-between">
            <span>{source}</span>
            <span className="text-slate-500">{stories.length}</span>
          </h3>
          {stories.map((item) => (
            <NewsCard key={item.id} item={item} />
          ))}
        </div>
      ))}
    </div>
  );
}

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [tab, setTab] = useState('telemetry');
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState(null);
  const pollRef = useRef(null);

  const loadTelemetry = () => fetch('/api/telemetry').then((res) => res.json());

  useEffect(() => {
    loadTelemetry().then(setData);
    return () => clearInterval(pollRef.current);
  }, []);

  const handleRefresh = async () => {
    setRefreshError(null);
    const before = data?.cronLogs?.[0]?.executed_at;
    const res = await fetch('/api/refresh', { method: 'POST' });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setRefreshError(body.error || 'Failed to trigger refresh.');
      return;
    }
    setRefreshing(true);
    let elapsed = 0;
    pollRef.current = setInterval(async () => {
      elapsed += 5;
      const fresh = await loadTelemetry();
      setData(fresh);
      const latest = fresh.cronLogs?.[0];
      const done = latest && latest.executed_at !== before && latest.status !== 'RUNNING';
      if (done || elapsed >= 180) {
        clearInterval(pollRef.current);
        setRefreshing(false);
        if (done) setTab('kanban');
      }
    }, 5000);
  };

  if (!data) return <div className="p-8">Loading Telemetry...</div>;

  const lastRun = data.cronLogs[0];

  return (
    <div className="p-8 max-w-5xl mx-auto font-sans space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">📡 Scraper Telemetry & Control Panel</h1>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="px-4 py-2 rounded bg-blue-600 text-white font-semibold disabled:opacity-50"
        >
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>
      {refreshError && <p className="text-red-600 text-sm">{refreshError}</p>}
      {refreshing && (
        <p className="text-blue-600 text-sm">
          Pipeline triggered on GitHub Actions — this can take a minute or two. This page updates automatically.
        </p>
      )}

      <div className="flex gap-4 border-b">
        <button
          onClick={() => setTab('telemetry')}
          className={`pb-2 px-1 ${tab === 'telemetry' ? 'border-b-2 border-blue-600 font-semibold' : 'text-slate-500'}`}
        >
          Telemetry
        </button>
        <button
          onClick={() => setTab('kanban')}
          className={`pb-2 px-1 ${tab === 'kanban' ? 'border-b-2 border-blue-600 font-semibold' : 'text-slate-500'}`}
        >
          Last Fetched News
        </button>
      </div>

      {tab === 'telemetry' && (
        <div className="space-y-6">
          <div className="p-4 border rounded bg-slate-50 space-y-2">
            <h2 className="font-semibold text-lg">Last Execution Status</h2>
            <p>
              <strong>Run Time:</strong> {lastRun ? new Date(lastRun.executed_at).toLocaleString() : 'N/A'}
            </p>
            <p>
              <strong>Status:</strong> {lastRun ? <StatusPill status={lastRun.status} /> : 'N/A'}
            </p>
            <p>
              <strong>Total Stories Extracted:</strong> {lastRun?.total_fetched ?? 0}
            </p>
            <div className="mt-2">
              <strong>Per-Provider Extraction Count:</strong>
              <ul className="list-disc ml-5">
                {lastRun &&
                  Object.entries(lastRun.fetched_per_provider || {}).map(([provider, count]) => (
                    <li key={provider}>
                      {provider}: <strong>{count}</strong>
                    </li>
                  ))}
              </ul>
            </div>
          </div>

          <div className="p-4 border rounded space-y-3">
            <h2 className="font-semibold text-lg">Recent Runs</h2>
            {data.cronLogs.map((log) => (
              <div key={log.id} className="flex items-center justify-between border-b pb-2 text-sm">
                <span>{new Date(log.executed_at).toLocaleString()}</span>
                <StatusPill status={log.status} />
                <span>{log.total_fetched} stories</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === 'kanban' && <KanbanBoard items={data.latestBatch} />}
    </div>
  );
}
