import { Env } from '../types';
import { verifyToken } from '../auth';

export async function handleDailyZip(request: Request, env: Env): Promise<Response> {
  const frame = await verifyToken(request, env);
  if (!frame) return new Response(JSON.stringify({ error: 'Unauthorized' }), { status: 401 });

  const url = new URL(request.url);
  const deviceVersion = url.searchParams.get('version') || '0';

  if (deviceVersion === frame.daily_zip_version && frame.daily_zip_version !== '0') {
    return new Response(null, { status: 304 });
  }

  const zipKey = `zips/${frame.id}/daily.zip`;
  const obj = await env.BUCKET.get(zipKey);
  if (!obj) {
    return new Response(JSON.stringify({ error: 'No zip available yet. Asset processing may still be in progress.' }), { status: 404 });
  }

  return new Response(obj.body, {
    status: 200,
    headers: {
      'Content-Type': 'application/zip',
      'Content-Length': String(obj.size),
      'X-Daily-Zip-Version': frame.daily_zip_version,
    },
  });
}
