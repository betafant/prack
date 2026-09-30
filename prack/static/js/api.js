// Thin wrapper around the prack HTTP API.

async function request(url, options = {}) {
  const res = await fetch(url, { headers: { Accept: 'application/json' }, ...options });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json();
}

export const api = {
  config: () => request('/api/config'),
  status: () => request('/api/status'),
  live: () => request('/api/live?trail=1'),
  flights: (date, region) => request(`/api/flights?date=${date}&region=${region}`),
  tracks: (date, region) => request(`/api/tracks?date=${date}&region=${region}`),
  flight: (id) => request(`/api/flights/${id}`),
  track: (id) => request(`/api/flights/${id}/track`),
  patchFlight: (id, body) => request(`/api/flights/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  }),
  days: (start, end, region) => request(`/api/days?start=${start}&end=${end}&region=${region}`),
  weather: (date, region) => request(`/api/weather/${date}?region=${region}`),
  fetchWeather: (date, region) => request(`/api/weather/${date}/fetch?region=${region}`, { method: 'POST' }),
  exportUrl: (id, fmt) => `/api/flights/${id}/export.${fmt}`,
  dayZipUrl: (date, region) => `/api/days/${date}/export.zip?region=${region}`,
  stream: () => new EventSource('/api/live/stream'),
};
