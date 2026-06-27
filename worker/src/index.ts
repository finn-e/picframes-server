/**
 * PicFrames Server - Cloudflare Worker
 * Routes all device API endpoints for the PicFrames e-ink photo frame ecosystem.
 */
import { Env } from './types';
import { handleRegister } from './routes/register';
import { handleUpdate } from './routes/update';
import { handleDailyConfig } from './routes/daily-config';
import { handleDailyZip } from './routes/daily-zip';
import { handleRefresh } from './routes/refresh';
import { handleChangeOrientation } from './routes/change-orientation';
import { handleAdminRoutes } from './routes/admin';

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, X-Device-Mac, X-Device-Token, Authorization',
};

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    if (method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });

    const withCors = (r: Response) => { Object.entries(CORS).forEach(([k,v]) => r.headers.set(k,v)); return r; };

    try {
      if (path === '/register'            && method === 'POST') return withCors(await handleRegister(request, env));
      if (path === '/update'              && method === 'GET')  return withCors(await handleUpdate(request, env));
      if (path === '/daily-config'        && method === 'GET')  return withCors(await handleDailyConfig(request, env));
      if (path === '/daily-zip'           && method === 'GET')  return withCors(await handleDailyZip(request, env));
      if (path === '/refresh'             && method === 'POST') return withCors(await handleRefresh(request, env));
      if (path === '/change-orientation'  && method === 'POST') return withCors(await handleChangeOrientation(request, env));
      if (path.startsWith('/admin'))                            return withCors(await handleAdminRoutes(request, env));
      return withCors(new Response('Not Found', { status: 404 }));
    } catch (e: any) {
      console.error('Worker error:', e);
      return withCors(new Response(JSON.stringify({ error: 'Internal server error', detail: e?.message }), {
        status: 500, headers: { 'Content-Type': 'application/json' },
      }));
    }
  },
};
