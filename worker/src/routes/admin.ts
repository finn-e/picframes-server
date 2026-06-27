import { Env, User } from '../types';
import { hashPassword, verifyPassword, generateBase62Token } from '../auth';

async function authenticateAdmin(request: Request, env: Env): Promise<User | null> {
  const auth = request.headers.get('Authorization');
  if (!auth?.startsWith('Basic ')) return null;
  const [u, p] = atob(auth.slice(6)).split(':', 2);
  const user = await env.DB.prepare('SELECT * FROM users WHERE username = ? AND is_admin = 1').bind(u).first<User>();
  if (!user) return null;
  return (await verifyPassword(p, user.password_hash)) ? user : null;
}

const json = (data: any, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
});

export async function handleAdminRoutes(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;

  // Bootstrap (no auth needed, only if no users exist)
  if (path === '/admin/bootstrap' && method === 'POST') {
    const count = await env.DB.prepare('SELECT COUNT(*) as c FROM users').first<{ c: number }>();
    if (count && count.c > 0) return json({ error: 'Already bootstrapped' }, 409);
    const body: any = await request.json();
    const id = crypto.randomUUID();
    const hash = await hashPassword(body.password || 'changeme');
    await env.DB.prepare('INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?, ?, ?, 1, ?)')
      .bind(id, body.username || 'admin', hash, Math.floor(Date.now() / 1000)).run();
    return json({ ok: true });
  }

  const admin = await authenticateAdmin(request, env);
  if (!admin) return json({ error: 'Unauthorized' }, 401);

  // List users
  if (path === '/admin/users' && method === 'GET') {
    const rows = await env.DB.prepare('SELECT id, username, is_admin, created_at FROM users ORDER BY created_at DESC').all();
    return json(rows.results);
  }
  // Create user
  if (path === '/admin/users' && method === 'POST') {
    const body: any = await request.json();
    const id = crypto.randomUUID();
    const hash = await hashPassword(body.password);
    await env.DB.prepare('INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)')
      .bind(id, body.username, hash, body.is_admin ? 1 : 0, Math.floor(Date.now() / 1000)).run();
    return json({ ok: true, id });
  }
  // Delete user
  const delUserMatch = path.match(/^\/admin\/users\/([^/]+)$/);
  if (delUserMatch && method === 'DELETE') {
    await env.DB.prepare('DELETE FROM users WHERE id = ?').bind(delUserMatch[1]).run();
    return json({ ok: true });
  }
  // Reset password
  const pwMatch = path.match(/^\/admin\/users\/([^/]+)\/password$/);
  if (pwMatch && method === 'POST') {
    const body: any = await request.json();
    const hash = await hashPassword(body.password);
    await env.DB.prepare('UPDATE users SET password_hash = ? WHERE id = ?').bind(hash, pwMatch[1]).run();
    return json({ ok: true });
  }
  // List frames
  if (path === '/admin/frames' && method === 'GET') {
    const rows = await env.DB.prepare(
      'SELECT f.*, u.username FROM frames f JOIN users u ON u.id = f.user_id ORDER BY f.created_at DESC'
    ).all();
    return json(rows.results);
  }
  // Delete frame
  const delFrameMatch = path.match(/^\/admin\/frames\/([^/]+)$/);
  if (delFrameMatch && method === 'DELETE') {
    await env.DB.prepare('DELETE FROM frames WHERE id = ?').bind(delFrameMatch[1]).run();
    return json({ ok: true });
  }

  return json({ error: 'Not found' }, 404);
}
