// Triggers the GitHub Actions pipeline on demand. The dashboard runs on
// Vercel and has no Playwright/social-posting infra of its own, so "refresh"
// means asking GitHub Actions to run main_pipeline.py right now via
// workflow_dispatch, same job the daily cron uses.
export async function POST() {
  const repo = process.env.GITHUB_REPO; // "owner/repo"
  const token = process.env.GITHUB_PAT; // classic PAT with `repo` scope (or fine-grained: Actions: write)
  const ref = process.env.GITHUB_REF || 'master';

  if (!repo || !token) {
    return Response.json(
      { error: 'GITHUB_REPO / GITHUB_PAT are not configured on the Vercel project.' },
      { status: 500 }
    );
  }

  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/pipeline.yml/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref }),
    }
  );

  if (!res.ok) {
    const text = await res.text();
    return Response.json({ error: text || `GitHub API returned ${res.status}` }, { status: res.status });
  }

  return Response.json({ triggered: true });
}
