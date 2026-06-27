import { Env } from './types';

const B62 = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';

export function generateBase62Token(length = 32): string {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return Array.from(bytes).map(b => B62[b % 62]).join('');
}

export async function verifyToken(request: Request, env: Env): Promise<any | null> {
  const mac   = request.headers.get('X-Device-Mac')?.toLowerCase();
  const token = request.headers.get('X-Device-Token');
  if (!mac || !token) return null;
  const row = await env.DB.prepare(
    `SELECT f.* FROM frames f JOIN tokens t ON t.frame_id = f.id WHERE f.mac = ? AND t.token = ?`
  ).bind(mac, token).first();
  return row || null;
}

export async function hashPassword(password: string): Promise<string> {
  const data = new TextEncoder().encode(password);
  const hash = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2,'0')).join('');
}

export async function verifyPassword(password: string, hash: string): Promise<boolean> {
  return (await hashPassword(password)) === hash;
}
