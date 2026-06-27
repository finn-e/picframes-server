import { Env } from '../types';
import { verifyToken } from '../auth';

export async function handleChangeOrientation(request: Request, env: Env): Promise<Response> {
  const frame = await verifyToken(request, env);
  if (!frame) return new Response(JSON.stringify({ error: 'Unauthorized' }), { status: 401 });

  const body: any = await request.json();
  const orientation: string = body.orientation;
  if (!['landscape', 'portrait'].includes(orientation))
    return new Response(JSON.stringify({ error: 'orientation must be landscape or portrait' }), { status: 400 });

  await env.DB.prepare('UPDATE frames SET orientation = ? WHERE id = ?').bind(orientation, frame.id).run();
  return new Response(JSON.stringify({ ok: true, orientation }), { status: 200, headers: { 'Content-Type': 'application/json' } });
}
