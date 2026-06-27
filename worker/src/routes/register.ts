import { Env } from '../types';
import { hashPassword, generateBase62Token, verifyPassword } from '../auth';

export async function handleRegister(request: Request, env: Env): Promise<Response> {
  const body: any = await request.json();
  const { mac, username, password } = body;
  if (!mac || !username || !password)
    return new Response(JSON.stringify({ error: 'mac, username, and password are required' }), { status: 400 });

  const normMac = mac.toLowerCase();
  const existing = await env.DB.prepare('SELECT id FROM frames WHERE mac = ?').bind(normMac).first();
  if (existing)
    return new Response(JSON.stringify({ error: 'MAC already registered. Delete the existing frame via the admin portal before re-registering.' }), { status: 409 });

  const user = await env.DB.prepare('SELECT id, password_hash FROM users WHERE username = ?')
    .bind(username).first<{ id: string; password_hash: string }>();
  if (!user) return new Response(JSON.stringify({ error: 'Invalid credentials' }), { status: 401 });

  const valid = await verifyPassword(password, user.password_hash);
  if (!valid) return new Response(JSON.stringify({ error: 'Invalid credentials' }), { status: 401 });

  const frameId = crypto.randomUUID();
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare('INSERT INTO frames (id, user_id, mac, name, created_at) VALUES (?, ?, ?, ?, ?)')
    .bind(frameId, user.id, normMac, 'New Frame', now).run();

  const token = generateBase62Token(32);
  await env.DB.prepare('INSERT INTO tokens (token, frame_id, created_at) VALUES (?, ?, ?)')
    .bind(token, frameId, now).run();

  return new Response(JSON.stringify({ token, frame_id: frameId }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}
