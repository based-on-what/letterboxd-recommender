// app.js — helpers shared between index.html and results.html.
// Page-specific logic stays inline in each page.

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

// Poll the async job result endpoint until completion (max ~3 min).
async function pollForResult(requestId) {
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 2000));
    const res = await fetch(`/api/result?request_id=${encodeURIComponent(requestId)}`);
    if (res.status === 202) continue;
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || 'Unknown error');
    return body;
  }
  throw new Error('Timed out waiting for recommendations. Please try again.');
}
