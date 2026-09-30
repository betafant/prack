// MapLibre base map (2D / 3D terrain) with deck.gl overlays for traffic and tracks.
import { state, emit, visibleLive, visibleFlights, selectedLive, selectedFlightId, catColor } from './store.js';
import { hexToRgb, escapeHtml, fmtNum, fmtSigned, throttle } from './util.js';
import { atlas } from './icons.js';

const MAGENTA = [255, 62, 201];
const CYAN = [92, 200, 236];

// Dark hypsometric tint for the "Relief" base map (terrain awareness style)
const RELIEF_RAMP = [
  'interpolate', ['linear'], ['elevation'],
  -50, '#081522', 0, '#0f261b', 400, '#14301e', 800, '#1b3a21', 1200, '#2b4425', 1600, '#454b29',
  2000, '#56472c', 2500, '#6a5845', 3000, '#837a71', 3600, '#b6b3b0', 4300, '#eef2f6',
];

const SKY = {
  'sky-color': '#0c1b2c',
  'sky-horizon-blend': 0.55,
  'horizon-color': '#2a4863',
  'horizon-fog-blend': 0.6,
  'fog-color': '#0a0e12',
  'fog-ground-blend': 0.85,
};

const TOOLTIP_STYLE = {
  background: 'rgba(3,5,7,.92)',
  border: '1px solid #2b3542',
  borderRadius: '5px',
  color: '#d5dee8',
  fontFamily: '"B612", monospace',
  fontSize: '11px',
  padding: '6px 9px',
  boxShadow: '0 6px 18px rgba(0,0,0,.5)',
};

const rgbCache = new Map();
function catRgb(cat, alpha = 255) {
  const key = cat + alpha;
  if (!rgbCache.has(key)) rgbCache.set(key, hexToRgb(catColor(cat), alpha));
  return rgbCache.get(key);
}

export class MapView {
  constructor(container) {
    const region = state.region;
    this.ready = false;
    this.memo = {};
    this.map = new maplibregl.Map({
      container,
      style: this.buildStyle(),
      center: region.center,
      zoom: region.zoom,
      maxPitch: 0,
      attributionControl: { compact: true },
      fadeDuration: 0,
      dragRotate: true,
    });
    this.map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
    this.overlay = new deck.MapboxOverlay({
      interleaved: true,
      layers: [],
      pickingRadius: 6,
      getTooltip: (info) => this.tooltip(info),
      onClick: (info) => this.click(info),
      getCursor: ({ isHovering, isDragging }) => (isDragging ? 'grabbing' : isHovering ? 'pointer' : 'grab'),
    });
    this.map.on('load', () => {
      this.map.addControl(this.overlay);
      this.setBasemap(state.basemap);
      this.ready = true;
      this.render();
      emit('map-ready');
    });
    this.map.on('resize', () => this.syncDeckFramebuffer());
    const rerender = throttle(() => this.render(), 120);
    this.map.on('rotate', rerender);
    this.map.on('zoomend', rerender);
    this.map.on('dragstart', () => {
      if (state.follow) emit('follow', false);
    });
    new ResizeObserver(() => this.map.resize()).observe(container);
  }

  /**
   * Workaround for deck.gl 9.4 / luma.gl in interleaved mode: when MapLibre resizes the shared
   * canvas (window resize, details panel opening), luma keeps the old size on its default
   * framebuffer object and deck computes the GL viewport from it, shifting every overlay layer
   * vertically. Resizing a default (handle-less) framebuffer only updates its width/height.
   */
  syncDeckFramebuffer() {
    const context = this.overlay?._deck?.device?.canvasContext;
    const fb = context?._framebuffer;
    const canvas = this.map.getCanvas();
    if (!fb || (fb.width === canvas.width && fb.height === canvas.height)) return;
    context.setDrawingBufferSize?.(canvas.width, canvas.height);
    fb.resize([canvas.width, canvas.height]);
  }

  // ---------------------------------------------------------------- style

  buildStyle() {
    const terrain = state.config.terrain;
    const dem = {
      type: 'raster-dem',
      tiles: terrain.tiles.map((t) => new URL(t, location.origin).href.replace(/%7B/g, '{').replace(/%7D/g, '}')),
      encoding: terrain.encoding,
      tileSize: 256,
      maxzoom: terrain.max_zoom,
      attribution: 'Terrain: <a href="https://github.com/tilezen/joerd">Mapzen / AWS Terrain Tiles</a>',
    };
    const sources = { 'dem-terrain': { ...dem }, 'dem-shade': { ...dem } };
    const layers = [
      { id: 'background', type: 'background', paint: { 'background-color': '#0a0e12' } },
      {
        id: 'bm-relief', type: 'color-relief', source: 'dem-shade', layout: { visibility: 'none' },
        paint: { 'color-relief-color': RELIEF_RAMP, 'color-relief-opacity': 1 },
      },
    ];
    for (const b of state.region.basemaps) {
      if (b.type !== 'raster') continue;
      sources[`bm-${b.id}`] = {
        type: 'raster', tiles: b.tiles, tileSize: 256, maxzoom: b.max_zoom || 18, attribution: b.attribution,
      };
      layers.push({ id: `bm-${b.id}`, type: 'raster', source: `bm-${b.id}`, layout: { visibility: 'none' }, paint: b.paint || {} });
    }
    layers.push({
      id: 'hillshade', type: 'hillshade', source: 'dem-shade', layout: { visibility: 'none' },
      paint: {
        'hillshade-exaggeration': 0.6,
        'hillshade-shadow-color': '#000000',
        'hillshade-highlight-color': '#d9e4ee',
        'hillshade-accent-color': '#05080b',
        'hillshade-illumination-direction': 315,
      },
    });
    return { version: 8, sources, layers, sky: SKY };
  }

  setBasemap(id) {
    const known = state.region.basemaps.map((b) => b.id);
    if (!known.includes(id)) id = state.region.default_basemap;
    state.basemap = id;
    const style = this.map.getStyle();
    if (!style) return;
    for (const layer of style.layers) {
      if (!layer.id.startsWith('bm-')) continue;
      this.map.setLayoutProperty(layer.id, 'visibility', layer.id === `bm-${id}` ? 'visible' : 'none');
    }
    this.map.setLayoutProperty('hillshade', 'visibility', id === 'relief' ? 'visible' : 'none');
  }

  set3D(on) {
    if (on) {
      this.map.setMaxPitch(85);
      this.map.setTerrain({ source: 'dem-terrain', exaggeration: 1 });
      this.map.easeTo({ pitch: 62, duration: 900 });
    } else {
      this.map.setTerrain(null);
      this.map.setCenterClampedToGround(true);
      this.map.easeTo({ pitch: 0, bearing: 0, elevation: 0, duration: 700 });
      this.map.once('moveend', () => { if (!state.view3d) this.map.setMaxPitch(0); });
    }
    this.render();
  }

  // ---------------------------------------------------------------- camera

  /** Fit a lon/lat box. In 3D the camera centre is lifted to ``alt`` so elevated tracks stay in view. */
  fitBounds(bounds, { alt = null, maxZoom = 14, padding = 70 } = {}) {
    if (!bounds) return;
    let [w, s, e, n] = bounds;
    if (![w, s, e, n].every(Number.isFinite)) return;
    const minSpan = 0.02; // ~2 km, avoids zooming into a single point
    if (n - s < minSpan) { const c = (n + s) / 2; s = c - minSpan / 2; n = c + minSpan / 2; }
    if (e - w < minSpan) { const c = (e + w) / 2; w = c - minSpan / 2; e = c + minSpan / 2; }
    this.map.resize();
    if (!state.view3d) {
      this.map.fitBounds([[w, s], [e, n]], { padding, maxZoom, duration: 900 });
      return;
    }
    const cam = this.map.cameraForBounds([[w, s], [e, n]], { padding, bearing: this.map.getBearing(), pitch: 0 });
    if (!cam) return;
    // zoom <= 12 keeps the camera several km away (and above the Alpine peaks around the track)
    const opts = { center: cam.center, zoom: Math.min(cam.zoom - 0.4, maxZoom - 1, 12), pitch: Math.max(this.map.getPitch(), 55), duration: 1200 };
    if (alt != null) {
      this.map.setCenterClampedToGround(false);
      opts.elevation = alt;
    }
    this.map.easeTo(opts);
  }

  fitRegion() {
    this.fitBounds(state.region.bbox, { padding: 20 });
  }

  follow(a) {
    if (!a) return;
    const opts = { center: [a.lon, a.lat], duration: 900, easing: (t) => t };
    if (state.view3d) {
      this.map.setCenterClampedToGround(false);
      opts.elevation = a.alt;
    }
    this.map.easeTo(opts);
  }

  resetNorth() {
    this.map.easeTo({ bearing: 0, pitch: state.view3d ? 62 : 0, duration: 600 });
  }

  // ---------------------------------------------------------------- picking

  click(info) {
    if (!info.object || !info.layer) return;
    const id = info.layer.id;
    if (id === 'aircraft') emit('select', { kind: 'live', id: info.object.id });
    else if (id === 'day-tracks') emit('select', { kind: 'flight', id: info.object.id });
  }

  tooltip({ object, layer }) {
    if (!object || !layer) return null;
    if (layer.id === 'aircraft') {
      const a = object;
      const type = state.config.aircraft_types[a.type] || '';
      return {
        html: `<b>${escapeHtml(a.name)}</b><br>${escapeHtml(type)} · ${escapeHtml(a.src)}<br>`
          + `${fmtNum(a.alt)} m · ${fmtSigned(a.vs)} m/s · ${fmtNum(a.spd)} km/h`,
        style: TOOLTIP_STYLE,
      };
    }
    if (layer.id === 'day-tracks') {
      const f = state.flights.find((x) => x.id === object.id);
      if (!f) return null;
      return {
        html: `<b>${escapeHtml(f.name)}</b><br>${escapeHtml(f.type_name)} · ${fmtNum(f.max_alt)} m max · ${fmtNum(f.distance_km, 1)} km`,
        style: TOOLTIP_STYLE,
      };
    }
    return null;
  }

  // ---------------------------------------------------------------- layers

  cached(key, deps, fn) {
    const m = this.memo[key];
    if (m && m.deps.length === deps.length && m.deps.every((d, i) => d === deps[i])) return m.value;
    const value = fn();
    this.memo[key] = { deps, value };
    return value;
  }

  render() {
    if (!this.ready) return;
    this.syncDeckFramebuffer();
    const z3 = state.view3d;
    const v = state.versions;
    const zoom = this.map.getZoom();
    const bearing = this.map.getBearing();
    const { url: iconAtlas, mapping: iconMapping } = atlas();
    const selFlightId = selectedFlightId();
    const sel = selectedLive();
    const layers = [];

    // -- day overview tracks (history)
    if (state.mode === 'history' && state.trails) {
      const data = this.cached('dayTracks', [v.day, v.filters, selFlightId], () => {
        const visible = new Set(visibleFlights().map((f) => f.id));
        return state.tracks.filter((t) => visible.has(t.id) && t.id !== selFlightId && t.path && t.path.length > 1);
      });
      layers.push(new deck.PathLayer({
        id: 'day-tracks',
        data,
        getPath: z3 ? (t) => t.path : (t) => t.path2,
        getColor: (t) => catRgb(t.cat, t.hidden ? 70 : selFlightId ? 110 : 190),
        getWidth: 2,
        widthUnits: 'pixels',
        widthMinPixels: 1.5,
        jointRounded: true,
        capRounded: true,
        billboard: true,
        pickable: true,
        autoHighlight: true,
        highlightColor: [255, 255, 255, 220],
        updateTriggers: { getPath: z3, getColor: selFlightId },
      }));
    }

    // -- live trails
    const liveData = state.mode === 'live'
      ? this.cached('live', [v.live, v.filters], () => visibleLive())
      : [];
    if (state.mode === 'live' && state.trails) {
      const trails = this.cached('trails', [v.live, v.filters], () => liveData.filter((a) => a.trail && a.trail.length > 1));
      layers.push(new deck.PathLayer({
        id: 'trails',
        data: trails,
        getPath: z3 ? (a) => a.trail : (a) => a.trail2d,
        getColor: (a) => catRgb(a.cat, a.hidden ? 60 : 150),
        getWidth: 2,
        widthUnits: 'pixels',
        jointRounded: true,
        billboard: true,
        updateTriggers: { getPath: z3 },
      }));
    }

    // -- selected flight: track, curtain (3D), cursor
    const tr = state.track;
    if (tr && tr.id === selFlightId) {
      const path = [{ path: z3 ? tr.path3 : tr.path2 }];
      layers.push(new deck.PathLayer({
        id: 'sel-casing', data: path, getPath: (d) => d.path, getColor: [0, 0, 0, 200], getWidth: 6,
        widthUnits: 'pixels', jointRounded: true, capRounded: true, billboard: true,
        parameters: { depthWriteEnabled: false }, // must not hide the magenta line drawn on top
        updateTriggers: { getPath: [z3, v.track] },
      }));
      if (z3 && tr.curtain.length) {
        layers.push(new deck.SolidPolygonLayer({
          id: 'curtain', data: tr.curtain, getPolygon: (q) => q, _full3d: true, extruded: false,
          getFillColor: [255, 62, 201, 46], updateTriggers: { getPolygon: v.track },
        }));
      }
      layers.push(new deck.PathLayer({
        id: 'sel-track', data: path, getPath: (d) => d.path, getColor: MAGENTA, getWidth: 3,
        widthUnits: 'pixels', jointRounded: true, capRounded: true, billboard: true,
        updateTriggers: { getPath: [z3, v.track] },
      }));

      const i = state.cursor;
      const showMarker = i != null || state.mode === 'history';
      if (showMarker && tr.t.length) {
        const idx = i != null ? Math.min(i, tr.t.length - 1) : tr.t.length - 1;
        const p = { lon: tr.lon[idx], lat: tr.lat[idx], alt: tr.alt[idx], gnd: tr.gnd[idx], hdg: tr.hdg[idx], cat: tr.cat };
        const pos = z3 ? [p.lon, p.lat, p.alt] : [p.lon, p.lat];
        if (z3) {
          layers.push(new deck.LineLayer({
            id: 'cursor-drop', data: [p], getSourcePosition: (d) => [d.lon, d.lat, d.gnd ?? 0],
            getTargetPosition: (d) => [d.lon, d.lat, d.alt], getColor: [255, 255, 255, 170], getWidth: 1.5,
          }));
        }
        if (state.mode === 'history' || !sel) {
          layers.push(new deck.IconLayer({
            id: 'cursor-halo', data: [p], iconAtlas, iconMapping, getIcon: (d) => d.cat, getPosition: () => pos,
            getSize: 40, getColor: [0, 0, 0, 210], getAngle: (d) => bearing - (d.hdg ?? 0),
            updateTriggers: { getPosition: [idx, z3], getAngle: [idx, bearing] },
          }));
          layers.push(new deck.IconLayer({
            id: 'cursor-icon', data: [p], iconAtlas, iconMapping, getIcon: (d) => d.cat, getPosition: () => pos,
            getSize: 34, getColor: MAGENTA, getAngle: (d) => bearing - (d.hdg ?? 0),
            updateTriggers: { getPosition: [idx, z3], getAngle: [idx, bearing] },
          }));
        } else if (i != null) {
          layers.push(new deck.ScatterplotLayer({
            id: 'cursor', data: [p], getPosition: () => pos, getRadius: 7, radiusUnits: 'pixels',
            stroked: true, getFillColor: [255, 255, 255], getLineColor: MAGENTA, getLineWidth: 3, lineWidthUnits: 'pixels',
            billboard: true, updateTriggers: { getPosition: [idx, z3] },
          }));
        }
      }
    }

    // -- live aircraft
    if (state.mode === 'live') {
      const pos = z3 ? (a) => [a.lon, a.lat, a.alt] : (a) => [a.lon, a.lat];
      if (z3) {
        layers.push(new deck.LineLayer({
          id: 'ac-drop', data: liveData.filter((a) => a.flying),
          getSourcePosition: (a) => [a.lon, a.lat, a.gnd ?? 0], getTargetPosition: (a) => [a.lon, a.lat, a.alt],
          getColor: (a) => catRgb(a.cat, 110), getWidth: 1,
        }));
      }
      if (sel && liveData.includes(sel)) {
        layers.push(new deck.ScatterplotLayer({
          id: 'sel-ring', data: [sel], getPosition: pos, getRadius: 24, radiusUnits: 'pixels', filled: true, stroked: true,
          getFillColor: [255, 62, 201, 40], getLineColor: MAGENTA, getLineWidth: 2, lineWidthUnits: 'pixels', billboard: true,
          updateTriggers: { getPosition: [v.live, z3] },
        }));
      }
      const angle = (a) => (a.hdg == null ? 0 : bearing - a.hdg);
      layers.push(new deck.IconLayer({
        id: 'ac-halo', data: liveData, iconAtlas, iconMapping, getIcon: (a) => a.cat, getPosition: pos,
        getSize: (a) => (sel === a ? 42 : 34), getColor: [0, 0, 0, 200], getAngle: angle,
        updateTriggers: { getPosition: z3, getAngle: bearing, getSize: sel?.id },
      }));
      layers.push(new deck.IconLayer({
        id: 'aircraft', data: liveData, iconAtlas, iconMapping, getIcon: (a) => a.cat, getPosition: pos,
        getSize: (a) => (sel === a ? 36 : 28),
        getColor: (a) => (a.hidden ? catRgb(a.cat, 90) : a.flying ? catRgb(a.cat) : catRgb(a.cat, 140)),
        getAngle: angle, pickable: true,
        updateTriggers: { getPosition: z3, getAngle: bearing, getSize: sel?.id },
      }));
      if (state.labels) {
        const labelled = zoom >= 8.5 ? liveData : liveData.filter((a) => a === sel);
        layers.push(new deck.TextLayer({
          id: 'labels', data: labelled, getPosition: pos,
          getText: (a) => `${a.name}\n${fmtNum(a.alt)}m ${fmtSigned(a.vs)}`,
          getSize: 11, getColor: (a) => (a === sel ? [255, 255, 255, 255] : [214, 224, 234, 235]),
          getPixelOffset: [0, 30], fontFamily: '"B612", monospace', fontWeight: 700, characterSet: 'auto',
          lineHeight: 1.15, background: true, getBackgroundColor: [3, 5, 7, 185], backgroundPadding: [4, 2, 4, 2],
          getTextAnchor: 'middle', getAlignmentBaseline: 'top',
          updateTriggers: { getPosition: z3, getColor: sel?.id },
        }));
      }
    }

    // -- weather sample points (when the WX drawer is open)
    if (state.wxOpen && state.weather?.points?.length) {
      const pts = state.weather.points.filter((p) => p.summary?.wind_700 || p.summary?.wind_dir != null);
      const wpos = (p) => (z3 ? [p.lon, p.lat, (p.elevation ?? 0) + 2500] : [p.lon, p.lat]);
      const dir = (p) => p.summary.wind_700?.dir ?? p.summary.wind_dir ?? 0;
      layers.push(new deck.IconLayer({
        id: 'wx-arrows', data: pts, iconAtlas, iconMapping, getIcon: () => 'arrow', getPosition: wpos,
        getSize: 30, getColor: [...CYAN, 230], getAngle: (p) => bearing - (dir(p) + 180),
        updateTriggers: { getAngle: bearing, getPosition: z3 },
      }));
      layers.push(new deck.TextLayer({
        id: 'wx-text', data: pts, getPosition: wpos,
        getText: (p) => `${p.name}\nW700 ${fmtNum(p.summary.wind_700?.speed)} km/h`,
        getSize: 10, getColor: [...CYAN, 255], getPixelOffset: [0, 22], fontFamily: '"B612", monospace',
        characterSet: 'auto', background: true, getBackgroundColor: [3, 5, 7, 185], backgroundPadding: [3, 2, 3, 2],
        getAlignmentBaseline: 'top', updateTriggers: { getPosition: z3 },
      }));
    }

    this.overlay.setProps({ layers });
  }
}
