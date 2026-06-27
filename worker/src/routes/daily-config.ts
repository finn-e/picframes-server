import { Env } from '../types';
import { verifyToken } from '../auth';

export async function handleDailyConfig(request: Request, env: Env): Promise<Response> {
  const frame = await verifyToken(request, env);
  if (!frame) return new Response(JSON.stringify({ error: 'Unauthorized' }), { status: 401 });

  const rows = await env.DB.prepare(
    `SELECT i.basename FROM frame_images fi
     JOIN images i ON i.id = fi.image_id
     WHERE fi.frame_id = ? AND fi.enabled = 1
     ORDER BY fi.sort_order ASC`
  ).bind(frame.id).all();

  const images: string[] = (rows.results || []).map((r: any) => r.basename);

  const config = {
    orientation:       frame.orientation,
    sleep_interval:    frame.sleep_interval,
    image_index:       frame.image_index,
    images,
    landscape_flipped: frame.landscape_flipped === 1,
    portrait_flipped:  frame.portrait_flipped === 1,
    daily_zip_version: frame.daily_zip_version,
  };

  return new Response(JSON.stringify(config), { status: 200, headers: { 'Content-Type': 'application/json' } });
}
