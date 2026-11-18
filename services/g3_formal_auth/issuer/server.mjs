// Disposable independent OIDC issuer for GM-02AS integration acceptance.
import { scryptSync, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:https';
import { Provider } from 'oidc-provider';

const cfg = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const accounts = new Map(cfg.accounts.map(account => [account.name, account]));
const ids = new Set(cfg.accounts.map(account => account.subject));

const provider = new Provider(cfg.issuer, {
  clients: [{
    client_id: 'gm02as-test-rp',
    client_secret: cfg.clientSecret,
    token_endpoint_auth_method: 'client_secret_basic',
    redirect_uris: [cfg.callback],
    response_types: ['code'],
    grant_types: ['authorization_code'],
  }],
  jwks: { keys: [cfg.signingKey] },
  cookies: { keys: [cfg.cookieKey] },
  claims: { openid: ['sub'] },
  ttl: { IdToken: 180, AuthorizationCode: 60, Interaction: 120, Session: 900, Grant: 900 },
  pkce: { required: () => true },
  features: { devInteractions: { enabled: false } },
  findAccount: async (_ctx, id) => ids.has(id)
    ? { accountId: id, claims: async () => ({ sub: id }) }
    : undefined,
  interactions: { url: (_ctx, interaction) => `/login/${interaction.uid}` },
});

const handler = provider.callback();

function respond(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(body);
}

async function parseForm(req) {
  let body = '';
  for await (const part of req) {
    body += part;
    if (Buffer.byteLength(body) > 2048) throw new Error('invalid form');
  }
  return new URLSearchParams(body);
}

const server = createServer({
  cert: readFileSync(cfg.cert), key: readFileSync(cfg.tlsKey), minVersion: 'TLSv1.2',
}, async (req, res) => {
  const match = /^\/login\/([A-Za-z0-9_-]+)$/.exec(req.url);
  if (!match) return handler(req, res);
  try {
    const details = await provider.interactionDetails(req, res);
    if (details.uid !== match[1]) return respond(res, 403, 'Denied');
    if (details.prompt.name === 'consent' && req.method === 'GET') {
      // Only this pre-approved test RP and its openid identity scope are allowed.
      if (details.params.client_id !== 'gm02as-test-rp' || details.params.scope !== 'openid') {
        return respond(res, 403, 'Denied');
      }
      const grant = new provider.Grant({ accountId: details.session.accountId, clientId: details.params.client_id });
      grant.addOIDCScope('openid');
      return await provider.interactionFinished(req, res, { consent: { grantId: await grant.save() } });
    }
    if (details.prompt.name !== 'login') return respond(res, 403, 'Denied');
    if (req.method === 'GET') {
      return respond(res, 200, '<form method="post"><input name="username"><input name="password" type="password"><button>Sign in</button></form>');
    }
    if (req.method !== 'POST' || req.headers.origin !== cfg.issuer
        || req.headers['content-type'] !== 'application/x-www-form-urlencoded') {
      return respond(res, 403, 'Denied');
    }
    const form = await parseForm(req);
    const account = accounts.get(form.get('username'));
    const expected = Buffer.from(account?.passwordDigest ?? cfg.dummyDigest, 'hex');
    const derived = scryptSync(form.get('password') ?? '', account?.salt ?? cfg.dummySalt, 32);
    if (!timingSafeEqual(expected, derived) || !account) return respond(res, 401, 'Denied');
    return await provider.interactionFinished(req, res, { login: { accountId: account.subject } }, { mergeWithLastSubmission: false });
  } catch {
    if (!res.headersSent) respond(res, 403, 'Denied');
    else res.end();
  }
});
const origin = new URL(cfg.issuer);
if (origin.hostname !== '127.0.0.2') throw new Error('test issuer must use its own loopback host');
server.listen(origin.port, origin.hostname);
