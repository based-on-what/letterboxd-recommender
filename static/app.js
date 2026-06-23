// app.js — helpers shared between index.html and results.html.
// Page-specific logic stays inline in each page.

// Theme toggle. The initial theme is applied by an inline <head> script on each
// page to avoid a flash; this only handles the click + persistence afterward.
function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  if (next === 'light') document.documentElement.dataset.theme = 'light';
  else delete document.documentElement.dataset.theme;
  try { localStorage.setItem('theme', next); } catch (e) {}
}

function getPlatformStyle(platform) {
  const p = platform.toLowerCase();
  if (p.includes('netflix'))                       return 'background:#E50914;color:#fff';
  if (p.includes('disney'))                        return 'background:#113CCF;color:#fff';
  if (p.includes('prime') || p.includes('amazon')) return 'background:#00A8E0;color:#000';
  if (p.includes('hbo') || p.includes('max'))      return 'background:#5822B4;color:#fff';
  if (p.includes('apple'))                         return 'background:#333;color:#fff';
  if (p.includes('hulu'))                          return 'background:#1CE783;color:#000';
  if (p.includes('paramount'))                     return 'background:#0064FF;color:#fff';
  return 'background:#00e054;color:#000';
}

// Convert an ISO country code into its flag emoji.
function countryCodeToFlag(countryCode) {
  const codePoints = countryCode
    .toUpperCase()
    .split('')
    .map(char => 127397 + char.charCodeAt());
  return String.fromCodePoint(...codePoints);
}

// EventSource wrapper with capped exponential-backoff reconnection.
// onStatusChange receives 'open' | 'reconnecting' | 'failed'.
function createReconnectingStream(url, onMessage, onStatusChange) {
  const MAX_RETRIES = 5;
  let attempts = 0;
  let closedByUser = false;
  let es = null;

  function connect() {
    es = new EventSource(url);
    es.onopen = () => {
      attempts = 0;
      if (onStatusChange) onStatusChange('open');
    };
    es.onmessage = onMessage;
    es.onerror = () => {
      if (closedByUser) return;
      if (es.readyState === EventSource.CLOSED) {
        if (attempts >= MAX_RETRIES) {
          if (onStatusChange) onStatusChange('failed');
          return;
        }
        attempts += 1;
        if (onStatusChange) onStatusChange('reconnecting');
        setTimeout(() => { if (!closedByUser) connect(); }, Math.min(30000, 1000 * 2 ** attempts));
      } else if (onStatusChange) {
        onStatusChange('reconnecting'); // browser-level retry in progress
      }
    };
  }

  connect();
  return {
    close() {
      closedByUser = true;
      if (es) es.close();
    },
  };
}

// fetch() with an overall timeout (default 3 min, matching the server's
// gunicorn timeout) so a hung server cannot leave an infinite spinner.
async function fetchWithTimeout(url, options = {}, timeoutMs = 180000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error('Request timed out after 3 minutes. Please try again.');
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

// Poll the async job result endpoint until the server says it's done (200)
// or the job is gone (404). No client deadline: large profiles take as long
// as they take; the only cap is the server's JOB_RESULT_TTL.
async function pollForResult(requestId) {
  while (true) {
    await new Promise(r => setTimeout(r, 2000));
    const res = await fetch(`/api/result?request_id=${encodeURIComponent(requestId)}`);
    if (res.status === 202) continue; // still pending
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || 'Unknown error');
    return body;
  }
}
