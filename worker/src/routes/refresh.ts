import { Env } from '../types';
import { verifyToken } from '../auth';

export async function handleRefresh(request: Request, env: Env): Promise<Response> {
  const frame = await verifyToken(request, env);
  if (!frame) return new Response(JSON.stringify({ error: 'Unauthorized' }), { status: 401 });

  const body: any = await request.json().catch(() => ({}));
  const skip: boolean = body.skip === true;

  const countRow = await env.DB.prepare(
    `SELECT COUNT(*) as cnt FROM frame_images WHERE frame_id = ? AND enabled = 1`
  ).bind(frame.id).first<{ cnt: number }>();
  const total = countRow?.cnt || 0;

  let newIndex: number = frame.image_index;

  if (frame.queued_image) {
    const qRow = await env.DB.prepare(
      `SELECT fi.sort_order FROM frame_images fi
       JOIN images i ON i.id = fi.image_id
       WHERE fi.frame_id = ? AND i.basename = ? AND fi.enabled = 1`
    ).bind(frame.id, frame.queued_image).first<{ sort_order: number }>();
    if (qRow !== null) newIndex = qRow.sort_order;
    await env.DB.prepare('UPDATE frames SET queued_image = NULL WHERE id = ?').bind(frame.id).run();
  } else {
    newIndex = total > 0 ? (frame.image_index + 1) % total : 0;
  }

  await env.DB.prepare('UPDATE frames SET image_index = ? WHERE id = ?').bind(newIndex, frame.id).run();

  return new Response(JSON.stringify({
    image_index:         newIndex,
    current_orientation: frame.orientation,
    sleep_interval:      frame.sleep_interval,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
}
