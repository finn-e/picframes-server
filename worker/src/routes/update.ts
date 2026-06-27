import { Env } from '../types';
import { verifyToken } from '../auth';

export async function handleUpdate(request: Request, env: Env): Promise<Response> {
  const frame = await verifyToken(request, env);
  if (!frame) return new Response(JSON.stringify({ error: 'Unauthorized' }), { status: 401 });

  const url = new URL(request.url);
  const hw = url.searchParams.get('hw') || 'ESP32-S3-PhotoPainter';
  const deviceVersion = url.searchParams.get('version') || '';

  const ghResp = await fetch(
    'https://api.github.com/repos/finn-e/picframe-devices/releases/latest',
    { headers: { 'User-Agent': 'picframes-server/2.0' } }
  );
  if (!ghResp.ok) return new Response('', { status: 200 });

  const release: any = await ghResp.json();
  const latestTag: string = release.tag_name || '';
  if (!latestTag || latestTag === deviceVersion) return new Response('', { status: 200 });

  const asset = (release.assets || []).find((a: any) =>
    a.name.toLowerCase().includes(hw.toLowerCase()) && a.name.endsWith('.zip')
  );
  if (!asset) return new Response('', { status: 200 });

  return new Response(asset.browser_download_url, { status: 200, headers: { 'Content-Type': 'text/plain' } });
}
