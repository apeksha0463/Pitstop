/* Pitstop route explorer: draws the metro map and wires up the filters, list and
   detail panel. No dependencies.

   Stops come from Supabase (site/config.js, publishable read-only key) in one request:
   restaurants with their washroom_summary embedded. Review snippets load only when a
   stop's panel opens. If Supabase can't be reached, everything comes from data.js
   (window.ROUTE_DATA, which also holds the route and landmarks) and a note says so. */
(async function () {
  "use strict";

  const OFFLINE = window.ROUTE_DATA;
  const CFG = window.PITSTOP_CONFIG;
  const TIMEOUT_MS = 6000;
  const VEG_RE = /(?<!non-)\bveg\b/i;          // same rule as build_route_map.py

  async function api(path) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(`${CFG.supabaseUrl}/rest/v1/${path}`, { headers: { apikey: CFG.supabasePublishableKey }, signal: ctrl.signal });
      if (!res.ok) throw new Error(`Supabase ${res.status}`);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  const STATION_SELECT = "data_id,name,google_rating,google_review_count,cuisine,route_km,side,detour_km,maps_link,position_note,"
    + "washroom_summary(washroom_reviews,positive,neutral,negative,mean_sentiment_score,low_data,grade)";

  function toStation(r, i) {
    const w = (Array.isArray(r.washroom_summary) ? r.washroom_summary[0] : r.washroom_summary) || {};
    return {
      id: i + 1, name: r.name, data_id: r.data_id, km: r.route_km, side: r.side,
      detour: r.detour_km != null, detour_km: r.detour_km,
      rating: r.google_rating, google_reviews: r.google_review_count || 0, cuisine: r.cuisine || "",
      veg: VEG_RE.test(`${r.name} ${r.cuisine || ""}`),
      washroom: { reviews: w.washroom_reviews || 0, positive: w.positive || 0, neutral: w.neutral || 0,
                  negative: w.negative || 0, mean: w.mean_sentiment_score ?? null },
      grade: w.grade || "none", low_data: w.low_data ?? true,
      snippets: null,                      // loaded when the panel opens
      maps_url: r.maps_link, position_check: !!r.position_note, position_note: r.position_note || "",
    };
  }

  async function loadData() {
    if (CFG && navigator.onLine !== false) {
      try {
        const rows = await api(`restaurants?select=${encodeURIComponent(STATION_SELECT)}&order=route_km.asc,name.asc`);
        if (rows.length) return { ...OFFLINE, stations: rows.map(toStation), source: "supabase" };
      } catch (e) { /* fall through to the offline copy */ }
    }
    return { ...OFFLINE, source: "offline" };
  }

  // The panel's snippets: the most confident, then the most confident of another sentiment,
  // then the next most confident (pick_snippets in build_route_map.py).
  function pickSnippets(rows, limit = 3) {
    rows = rows.slice().sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    const picked = rows.slice(0, 1).concat(rows.slice(1).filter((r) => r.sentiment !== rows[0].sentiment).slice(0, 1));
    picked.push(...rows.filter((r) => !picked.includes(r)).slice(0, Math.max(0, limit - picked.length)));
    return picked.slice(0, limit).map((r) => ({
      text: r.washroom_snippet.length > 240 ? r.washroom_snippet.slice(0, 240) + "…" : r.washroom_snippet,
      sentiment: r.sentiment, date: (r.iso_date || "").slice(0, 10),
    }));
  }

  async function loadSnippets(st) {
    try {
      const rows = await api(`washroom_mentions?select=washroom_snippet,sentiment,iso_date,confidence&data_id=eq.${encodeURIComponent(st.data_id)}`);
      st.snippets = pickSnippets(rows);
    } catch (e) {
      const saved = OFFLINE.stations.find((s) => s.data_id === st.data_id);
      st.snippets = saved ? saved.snippets : [];
    }
  }

  const DATA = await loadData();
  document.documentElement.dataset.source = DATA.source;
  document.getElementById("data-note").hidden = DATA.source !== "offline";
  const ROUTE_KM = DATA.route_km;
  const GRADES = {
    good: { label: "Good", sym: "✓" },
    moderate: { label: "Moderate", sym: "~" },
    bad: { label: "Bad", sym: "✕" },
    none: { label: "No data", sym: "–" },
  };
  const GRADE_ORDER = ["good", "moderate", "bad", "none"];
  const ZOOMS = [6, 9, 13, 18, 26, 36, 50, 70, 100];    // px per km
  const DEFAULT_ZOOM = { h: 4, v: 2 };
  const DIAG = Math.SQRT1_2;
  const MAX_FAN = 16, FAN_NEAR = 40, STEP = 2, MIN_GAP = 3;

  const $ = (sel) => document.querySelector(sel);
  const mqMobile = matchMedia("(max-width: 767px)");
  const mqReduce = matchMedia("(prefers-reduced-motion: reduce)");
  const stations = DATA.stations.slice().sort((a, b) => a.km - b.km || a.id - b.id);
  const byId = new Map(stations.map((s) => [s.id, s]));
  const landmarks = DATA.landmarks;

  const el = {
    scroller: $("#map-scroll"), minimap: $("#minimap"), list: $("#list"), empty: $("#empty"),
    count: $("#result-count"), panel: $("#panel"), panelBody: $("#panel-body"), tip: $("#tip"),
    filters: $("#filters"), filtersOpen: $("#filters-open"), filtersCount: $("#filters-count"),
    scrim: $("#scrim"), search: $("#search"), minRating: $("#min-rating"), veg: $("#veg-only"),
    mySide: $("#my-side"), sideHint: $("#side-hint"), listOrder: $("#list-order"),
  };

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const fmtKm = (km) => km.toFixed(1);
  const smooth = () => (mqReduce.matches ? "auto" : "smooth");

  /* ------------------------------------------------------------------
     Layout. Every station is a set of capsules (a1, o1, a2, o2, radius)
     in side-local coordinates: a along the route, o away from the road
     edge. A station starts at its real km and moves along the route only
     as far as needed to clear everything already placed on its side; a
     detour next to another spur first fans its end out a little.
     ------------------------------------------------------------------ */
  function modeParams(vertical) {
    return vertical
      ? { vertical, dotR: 8, edgeGap: 3, font: 13, spurFont: 10.5, labelGap: 10, labelR: 22, maxLabelW: Infinity,
          spurPerKm: 22, spurMin: 30, spurMax: 60, underSpur: 20, spurLabelGap: 10, lane: 15, start: 70 }
      : { vertical, dotR: 8, edgeGap: 3, font: 12, spurFont: 10.5, labelGap: 7, labelR: 8, maxLabelW: 150,
          spurPerKm: 40, spurMin: 46, spurMax: 104, spurLabelGap: 9, lane: 16, start: 200 };
  }

  // Real rendered text widths, measured in a hidden SVG. Everything a render needs
  // is measured in one batch (one layout pass); truncation then reads prefix widths
  // with getSubStringLength, which needs no further layout. Station labels are
  // measured bold, the hover state, so highlighting never makes them collide.
  const FONT = getComputedStyle(document.documentElement).getPropertyValue("--font");
  const measureSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  measureSvg.setAttribute("aria-hidden", "true");
  measureSvg.style.cssText = "position:absolute;left:-9999px;top:0;width:10px;height:10px;visibility:hidden";
  document.body.appendChild(measureSvg);
  const measured = new Map();
  const mkey = (text, size, weight) => `${weight}|${size}|${text}`;

  function measure(list) {
    const fresh = [];
    for (const [text, size, weight = 400] of list) {
      const k = mkey(text, size, weight);
      if (measured.has(k)) continue;
      const t = document.createElementNS(measureSvg.namespaceURI, "text");
      t.style.font = `${weight} ${size}px ${FONT}`;
      t.textContent = text;
      measureSvg.appendChild(t);
      const m = { el: t, w: 0 };
      measured.set(k, m);
      fresh.push(m);
    }
    for (const m of fresh) m.w = m.el.getComputedTextLength();
  }

  function textW(text, size, weight = 400) {
    const k = mkey(text, size, weight);
    if (!measured.has(k)) measure([[text, size, weight]]);
    return measured.get(k).w;
  }

  function fitText(text, maxW, size, weight) {
    const full = textW(text, size, weight);
    if (full <= maxW) return { text, w: full };
    const el = measured.get(mkey(text, size, weight)).el, ell = textW("…", size, weight);
    let n = text.length - 1;
    while (n > 1 && el.getSubStringLength(0, n) + ell > maxW) n--;
    const kept = text.slice(0, n).trimEnd();
    return { text: kept + "…", w: el.getSubStringLength(0, kept.length) + ell + 1 };
  }

  function ptSeg(px, py, x1, y1, x2, y2) {
    const vx = x2 - x1, vy = y2 - y1, l2 = vx * vx + vy * vy;
    const t = l2 ? clamp(((px - x1) * vx + (py - y1) * vy) / l2, 0, 1) : 0;
    return Math.hypot(px - x1 - t * vx, py - y1 - t * vy);
  }

  function segDist(a, b) {
    // which side of segment s the point (x, y) lies on
    const side = (s, x, y) => (s[2] - s[0]) * (y - s[1]) - (s[3] - s[1]) * (x - s[0]);
    if (side(a, b[0], b[1]) * side(a, b[2], b[3]) < 0 && side(b, a[0], a[1]) * side(b, a[2], a[3]) < 0) return 0;
    return Math.min(ptSeg(a[0], a[1], b[0], b[1], b[2], b[3]), ptSeg(a[2], a[3], b[0], b[1], b[2], b[3]),
                    ptSeg(b[0], b[1], a[0], a[1], a[2], a[3]), ptSeg(b[2], b[3], a[0], a[1], a[2], a[3]));
  }

  function collides(shapes, placed) {
    for (const s of shapes) {
      for (const p of placed) {
        const pad = s[4] + p[4];
        if (Math.max(s[0], s[2]) + pad < Math.min(p[0], p[2]) || Math.max(p[0], p[2]) + pad < Math.min(s[0], s[2])) continue;
        if (segDist(s, p) < pad) return true;
      }
    }
    return false;
  }

  function geometry(item, a, fan, M) {
    const h = item.spurPx;
    const dotA = a + fan, dotO = M.edgeGap + M.dotR + h;
    const shapes = [[dotA, dotO, dotA, dotO, M.dotR + 4]];
    const g = { a, fan, dot: [dotA, dotO], shapes, spurLabel: null };
    if (h) {
      const half = textW(item.spurText, M.spurFont) / 2;
      let ca, co, ua, uo;
      if (M.vertical) {          // phones: distance just past the dot, reading outwards
        [ua, uo] = [0, 1];
        [ca, co] = [dotA, dotO + M.dotR + 7 + half];
      } else {                   // alongside the spur, on its left
        const len = Math.hypot(fan, dotO);
        [ua, uo] = [fan / len, dotO / len];
        [ca, co] = [a + fan / 2 - uo * M.spurLabelGap, dotO / 2 + ua * M.spurLabelGap];
      }
      shapes.push([a, 0, dotA, dotO, 2.5],
                  [ca - ua * half, co - uo * half, ca + ua * half, co + uo * half, M.spurFont / 2 + 1.5]);
      g.spurLabel = { a: ca, o: co, ua, uo };
    }
    const w = item.labelW;
    let la, lo, da, dob;
    if (M.vertical) {
      [la, lo] = h ? [a + M.underSpur, M.edgeGap + 4] : [dotA, dotO + M.dotR + M.labelGap];
      [da, dob] = [0, 1];
    } else {
      const off = M.dotR + M.labelGap;
      [la, lo, da, dob] = [dotA + off * DIAG, dotO + off * DIAG, DIAG, DIAG];
    }
    g.label = { a: la, o: lo, w };
    shapes.push([la, lo, la + da * w, lo + dob * w, M.labelR]);
    return g;
  }

  function layout(ppk, M, sideW) {
    M.maxReach = (M.vertical ? 0 : M.maxLabelW * DIAG) + MAX_FAN + 60;
    const placed = { west: [], east: [] };
    const lastSpur = { west: -Infinity, east: -Infinity };
    const items = [];
    let last = -Infinity;
    measure(stations.flatMap((st) => [[st.name, M.font, 750], ...(st.detour ? [[fmtKm(st.detour_km) + " km", M.spurFont]] : [])]));
    for (const st of stations) {
      const spurPx = st.detour ? clamp(st.detour_km * M.spurPerKm, M.spurMin, M.spurMax) : 0;
      const lo = spurPx ? M.edgeGap + 4 : M.edgeGap + 2 * M.dotR + M.labelGap;
      const maxW = M.vertical ? sideW - lo - 6 : M.maxLabelW;
      const fit = fitText(st.name, maxW, M.font, 750);
      const item = { st, spurPx, label: fit.text, labelW: fit.w, spurText: spurPx ? fmtKm(st.detour_km) + " km" : "" };
      let a = Math.max(M.start + st.km * ppk, last + MIN_GAP);
      // phones give every station its own 44 px row, so spurs there never need fanning
      const fans = spurPx && !M.vertical && a - lastSpur[st.side] < FAN_NEAR ? [0, 4, 8, 12, MAX_FAN] : [0];
      // only shapes ending within reach of this station can collide with it
      const reach = a - 2 * M.maxReach;
      const near = placed[st.side].filter((p) => Math.max(p[0], p[2]) + p[4] > reach);
      let geo = null;
      while (!geo) {
        for (const fan of fans) {
          const g = geometry(item, a, fan, M);
          if (!collides(g.shapes, near)) { geo = g; break; }
        }
        if (!geo) a += STEP;
      }
      item.a = a;
      item.geo = geo;
      placed[st.side].push(...geo.shapes);
      if (spurPx) lastSpur[st.side] = a;
      last = a;
      items.push(item);
    }
    const ext = (side) => Math.max(0, ...placed[side].map((s) => Math.max(s[1], s[3]) + (M.vertical ? 4 : s[4])));
    const maxA = Math.max(...items.flatMap((i) => i.geo.shapes.map((s) => Math.max(s[0], s[2]) + s[4])));
    const endA = Math.max(M.start + ROUTE_KM * ppk, last + (M.vertical ? 40 : 30));
    const anchors = [[0, M.start], ...items.map((i) => [i.st.km, i.a]), [ROUTE_KM, endA]];
    return { items, ppk, M, endA, maxA, anchors, ext: { west: ext("west"), east: ext("east") } };
  }

  function kmToA(L, km) {
    const an = L.anchors;
    for (let i = 1; i < an.length; i++) {
      const [k0, a0] = an[i - 1], [k1, a1] = an[i];
      if (km <= k1) return k1 === k0 ? a0 : a0 + (a1 - a0) * (clamp(km, k0, k1) - k0) / (k1 - k0);
    }
    return an[an.length - 1][1];
  }

  function aToKm(L, a) {
    const an = L.anchors;
    if (a <= an[0][1]) return 0;
    for (let i = 1; i < an.length; i++) {
      const [k0, a0] = an[i - 1], [k1, a1] = an[i];
      if (a <= a1) return a1 === a0 ? k0 : k0 + (k1 - k0) * (a - a0) / (a1 - a0);
    }
    return ROUTE_KM;
  }

  /* ------------------------------------------------------------------
     Rendering
     ------------------------------------------------------------------ */
  let L = null;            // current layout
  let view = null;         // screen geometry of the current render
  let zoom = { h: null, v: null };

  function viewLen() { return view.vertical ? el.scroller.clientHeight : el.scroller.clientWidth; }
  function scrollPos() { return view.vertical ? el.scroller.scrollTop : el.scroller.scrollLeft; }
  function centerKm() { return view ? aToKm(L, scrollPos() + viewLen() / 2) : 0; }

  function extent(layoutResult, vertical) {
    return vertical ? layoutResult.endA + 70 : Math.max(layoutResult.endA + 130, layoutResult.maxA + 16);
  }

  function fitIndex(vertical, M, sideW) {
    const avail = vertical ? el.scroller.clientHeight : el.scroller.clientWidth;
    for (let i = ZOOMS.length - 1; i >= 0; i--) {
      if (extent(layout(ZOOMS[i], M, sideW), vertical) <= avail) return i;
    }
    return 0;
  }

  function render(keepKm) {
    const vertical = mqMobile.matches;
    const M = modeParams(vertical);
    const key = vertical ? "v" : "h";
    const width = el.scroller.clientWidth;
    const sideW = vertical ? (width - 4 * M.lane) / 2 : 0;
    if (zoom[key] == null) zoom[key] = DEFAULT_ZOOM[key];
    L = layout(ZOOMS[zoom[key]], M, sideW);
    view = buildSvg(L, M, vertical, width);
    el.scroller.innerHTML = view.svg;
    view.svgEl = el.scroller.firstElementChild;
    drawMinimap();
    applyFilters({ keepHash: true });
    if (selectedId != null) markSelected(selectedId);
    if (keepKm != null) scrollToKm(keepKm, false);
    // read the scroll geometry in the next frame, not here: reading it now would
    // force a synchronous layout of the whole page before the first paint
    if (!mmFrame) mmFrame = requestAnimationFrame(updateMinimapWindow);
  }

  function buildSvg(L, M, vertical, width) {
    const lane = M.lane;
    let W, H, toScreen, roadMidAt;
    let roadTop, roadBottom, axisY, cx;
    if (vertical) {
      W = width;
      H = extent(L, true);
      cx = Math.round(W / 2);
      const left = cx - lane, right = cx + lane;
      toScreen = (side, a, o) => [side === "west" ? left - o : right + o, a];
      roadMidAt = (a) => [cx, a];
    } else {
      roadTop = 16 + L.ext.west;
      roadBottom = roadTop + 2 * lane;
      axisY = roadBottom + L.ext.east + 22;
      W = extent(L, false);
      H = axisY + 58;
      toScreen = (side, a, o) => [a, side === "west" ? roadTop - o : roadBottom + o];
      roadMidAt = (a) => [a, roadTop + lane];
    }
    const out = [];
    const n = stations.length;
    out.push(`<svg class="map-svg" xmlns="http://www.w3.org/2000/svg" width="${W}" height="${Math.ceil(H)}" viewBox="0 0 ${W} ${Math.ceil(H)}" role="group" aria-label="Route map: ${n} stops from Phoenix Mall of Asia to Big Bay. West side ${vertical ? "on the left" : "above the road"}, east side ${vertical ? "on the right" : "below"}.">`);

    measure([...landmarks.flatMap((lm) => [[lm.name, 10.5, 750], [lm.name, 13, 750]]), ["◀ Southbound · to Bengaluru", 10, 800]]);
    const lmA = landmarks.map((lm) => ({ ...lm, a: lm.km >= ROUTE_KM ? L.endA : kmToA(L, lm.km) }));
    const inner = lmA.filter((lm) => !lm.terminal);
    const occupied = [[M.start - 16, M.start + 16], [L.endA - 16, L.endA + 16]];
    inner.forEach((lm) => occupied.push([lm.a - 12, lm.a + 12]));
    L.items.filter((it) => it.spurPx).forEach((it) => occupied.push([it.a - 10, it.a + it.geo.fan + 10]));

    // road decorations and km scale (behind the stations)
    out.push('<g aria-hidden="true">');
    if (vertical) {
      out.push(`<rect class="road" x="${cx - lane}" y="${M.start}" width="${2 * lane}" height="${L.endA - M.start}"/>`,
               `<line class="edge" x1="${cx - lane + 1.5}" y1="${M.start}" x2="${cx - lane + 1.5}" y2="${L.endA}"/>`,
               `<line class="edge" x1="${cx + lane - 1.5}" y1="${M.start}" x2="${cx + lane - 1.5}" y2="${L.endA}"/>`,
               `<line class="median" x1="${cx}" y1="${M.start}" x2="${cx}" y2="${L.endA}"/>`);
      // landmark names and km ticks read along the road, on a sign that masks the median
      for (const lm of inner) {
        const w = textW(lm.name, 10.5, 750) + 10, y0 = lm.a + 14;
        out.push(`<rect class="sign-bg" x="${cx - 7}" y="${y0}" width="14" height="${w}"/>`,
                 `<text class="road-sign" transform="translate(${cx} ${y0 + w / 2}) rotate(90)">${esc(lm.name)}</text>`);
        occupied.push([lm.a - 12, y0 + w + 2]);
      }
      for (let km = 10; km < ROUTE_KM; km += 10) {
        const a = kmToA(L, km), w = 34;
        if (occupied.some(([lo, hi]) => a + w / 2 > lo - 4 && a - w / 2 < hi + 4)) continue;
        out.push(`<rect class="sign-bg" x="${cx - 6}" y="${a - w / 2}" width="12" height="${w}"/>`,
                 `<text class="road-tick" transform="translate(${cx} ${a}) rotate(90)">${km} km</text>`);
        occupied.push([a - w / 2, a + w / 2]);
      }
    } else {
      for (const lm of inner) out.push(`<line class="guide" x1="${lm.a}" y1="${roadBottom}" x2="${lm.a}" y2="${axisY}"/>`);
      out.push(`<line class="axis" x1="${M.start}" y1="${axisY}" x2="${L.endA}" y2="${axisY}"/>`);
      for (let km = 0; km < ROUTE_KM; km += 10) {
        const a = kmToA(L, km);
        out.push(`<line class="axis" x1="${a}" y1="${axisY}" x2="${a}" y2="${axisY + 6}"/>`,
                 `<text class="tick" x="${a}" y="${axisY + 19}">${km} km</text>`);
      }
      let lastRight = -Infinity, row = 0;
      for (const lm of inner) {
        const half = textW(lm.name, 13, 750) / 2 + 2;
        row = lm.a - half < lastRight + 8 ? 1 - row : 0;
        lastRight = lm.a + half;
        out.push(`<text class="lm-name" x="${lm.a}" y="${axisY + 38 + row * 16}">${esc(lm.name)}</text>`);
      }
      out.push(`<rect class="road" x="${M.start}" y="${roadTop}" width="${L.endA - M.start}" height="${2 * lane}"/>`,
               `<line class="edge" x1="${M.start}" y1="${roadTop + 1.5}" x2="${L.endA}" y2="${roadTop + 1.5}"/>`,
               `<line class="edge" x1="${M.start}" y1="${roadBottom - 1.5}" x2="${L.endA}" y2="${roadBottom - 1.5}"/>`,
               `<line class="median" x1="${M.start}" y1="${roadTop + lane}" x2="${L.endA}" y2="${roadTop + lane}"/>`);
    }
    // lane names in the widest free stretches of road. Keep left: the west lane runs to Big Bay.
    const laneTexts = [["west", "Northbound · to Big Bay ▶"], ["east", "◀ Southbound · to Bengaluru"]];
    const need = textW(laneTexts[1][1], 10, 800) + 30;
    occupied.sort((p, q) => p[0] - q[0]);
    const gaps = [];
    for (let i = 1; i < occupied.length; i++) {
      const lo = occupied[i - 1][1], hi = occupied[i][0];
      if (hi - lo > need) gaps.push([lo, hi]);
    }
    gaps.sort((p, q) => (q[1] - q[0]) - (p[1] - p[0]));
    for (const [lo, hi] of gaps.slice(0, 3)) {
      const mid = (lo + hi) / 2;
      for (const [side, text] of laneTexts) {
        const off = side === "west" ? -lane / 2 : lane / 2;
        const t = vertical ? `transform="translate(${cx + off} ${mid}) rotate(90)"` : `x="${mid}" y="${roadTop + lane + off}"`;
        out.push(`<text class="lane-text" data-lane="${side}" ${t}>${text}</text>`);
      }
    }
    // terminals and interchanges
    const [sx, sy] = roadMidAt(M.start), [ex, ey] = roadMidAt(L.endA);
    out.push(`<circle class="terminal" cx="${sx}" cy="${sy}" r="14"/>`, `<circle class="terminal" cx="${ex}" cy="${ey}" r="14"/>`);
    if (vertical) {
      out.push(`<text class="terminal-name" x="${cx}" y="${sy - 32}" text-anchor="middle">Phoenix Mall of Asia</text>`,
               `<text class="terminal-name" x="${cx}" y="${ey + 30}" text-anchor="middle">Big Bay</text>`,
               `<text class="terminal-sub" x="${cx}" y="${ey + 48}" text-anchor="middle">${fmtKm(ROUTE_KM)} km</text>`);
    } else {
      out.push(`<text class="terminal-name" x="${sx - 24}" y="${sy - 8}" text-anchor="end">Phoenix</text>`,
               `<text class="terminal-name" x="${sx - 24}" y="${sy + 10}" text-anchor="end">Mall of Asia</text>`,
               `<text class="terminal-name" x="${ex + 24}" y="${ey - 8}">Big Bay</text>`,
               `<text class="terminal-sub" x="${ex + 24}" y="${ey + 10}">${fmtKm(ROUTE_KM)} km</text>`);
    }
    for (const lm of inner) {
      const [x, y] = roadMidAt(lm.a);
      out.push(`<circle class="interchange" cx="${x}" cy="${y}" r="10"><title>${esc(lm.name)}, km ${fmtKm(lm.km)}</title></circle>`);
    }
    out.push("</g>");

    // stations
    for (const it of L.items) out.push(stationSvg(it, M, vertical, toScreen));
    out.push("</svg>");
    return { svg: out.join(""), vertical, W, H, landmarks: lmA };
  }

  function stationSvg(it, M, vertical, toScreen) {
    const { st, geo } = it;
    const side = st.side, west = side === "west";
    const [dx, dy] = toScreen(side, geo.dot[0], geo.dot[1]);
    const g = [`<g class="station g-${st.grade}" data-id="${st.id}" tabindex="0" role="button" aria-label="${esc(ariaLabel(st))}">`];
    // hit area: a 44 px tall row on phones, a disc round the dot elsewhere
    if (vertical) {
      const [x0] = toScreen(side, 0, 0);
      const rowA = geo.label.a, wRow = Math.max(geo.label.o + geo.label.w + 6, geo.dot[1] + M.dotR + 4);
      g.push(`<rect class="hit" x="${west ? x0 - wRow : x0}" y="${rowA - 22}" width="${wRow}" height="44" rx="10"/>`);
    }
    if (it.spurPx) {
      const [x1, y1] = toScreen(side, geo.a, -1);
      const sl = geo.spurLabel, [lx, ly] = toScreen(side, sl.a, sl.o);
      const vx = vertical ? (west ? -sl.uo : sl.uo) : sl.ua, vy = vertical ? sl.ua : (west ? -sl.uo : sl.uo);
      let ang = Math.atan2(vy, vx) * 180 / Math.PI;
      if (ang > 90) ang -= 180;
      if (ang <= -90) ang += 180;
      g.push(`<line class="spur" x1="${x1}" y1="${y1}" x2="${dx}" y2="${dy}"/>`,
             `<text class="spur-label" transform="translate(${lx.toFixed(1)} ${ly.toFixed(1)}) rotate(${ang.toFixed(1)})">${esc(it.spurText)}</text>`);
    }
    if (!vertical) g.push(`<circle class="hit" cx="${dx}" cy="${dy}" r="14"/>`);
    if (st.low_data && st.washroom.reviews > 0) g.push(`<circle class="low-ring" cx="${dx}" cy="${dy}" r="${M.dotR + 5}"/>`);
    g.push(`<circle class="dot" cx="${dx}" cy="${dy}" r="${M.dotR}"/>`,
           `<text class="dot-sym" x="${dx}" y="${dy + 0.5}">${GRADES[st.grade].sym}</text>`);
    const [tx, ty] = toScreen(side, geo.label.a, geo.label.o);
    const tr = vertical ? `x="${tx.toFixed(1)}" y="${ty.toFixed(1)}" text-anchor="${west ? "end" : "start"}"`
                        : `transform="translate(${tx.toFixed(1)} ${ty.toFixed(1)}) rotate(${west ? -45 : 45})"`;
    g.push(`<text class="st-label" ${tr}>${esc(it.label)}</text>`, "</g>");
    return g.join("");
  }

  function ariaLabel(st) {
    const where = st.detour ? `detour ${fmtKm(st.detour_km)} km from the road` : "on the highway";
    const low = st.low_data && st.washroom.reviews ? ", low data" : "";
    const rating = st.rating ? `, rated ${st.rating}` : "";
    return `${st.name}, km ${fmtKm(st.km)}, ${st.side} side, ${where}. Washroom ${GRADES[st.grade].label.toLowerCase()}${low}${rating}.`;
  }

  /* ------------------------------------------------------------------
     Mini-map
     ------------------------------------------------------------------ */
  function drawMinimap() {
    const x = (km) => (km / ROUTE_KM * 1000).toFixed(1);
    const parts = ['<svg viewBox="0 0 1000 40" preserveAspectRatio="none" aria-hidden="true" focusable="false">',
                   '<line class="mm-road" x1="0" y1="20" x2="1000" y2="20" vector-effect="non-scaling-stroke"/>'];
    for (const st of stations) {
      const [y1, y2] = st.side === "west" ? [5, 15] : [25, 35];
      parts.push(`<line class="mm-st g-${st.grade}" data-id="${st.id}" x1="${x(st.km)}" y1="${y1}" x2="${x(st.km)}" y2="${y2}" stroke="var(--g)" vector-effect="non-scaling-stroke"/>`);
    }
    for (const lm of landmarks) parts.push(`<line x1="${x(lm.km)}" y1="12" x2="${x(lm.km)}" y2="28" stroke="var(--ink)" stroke-width="3" vector-effect="non-scaling-stroke"/>`);
    parts.push('<rect class="mm-window" id="mm-window" x="0" y="1" width="100" height="38" vector-effect="non-scaling-stroke"/></svg>');
    el.minimap.innerHTML = parts.join("");
    el.minimap.setAttribute("aria-valuemax", fmtKm(ROUTE_KM));
  }

  let mmFrame = 0;
  function updateMinimapWindow() {
    mmFrame = 0;
    if (!view) return;
    const k0 = aToKm(L, scrollPos()), k1 = aToKm(L, scrollPos() + viewLen());
    const win = $("#mm-window");
    win.setAttribute("x", (k0 / ROUTE_KM * 1000).toFixed(1));
    win.setAttribute("width", Math.max(6, (k1 - k0) / ROUTE_KM * 1000).toFixed(1));
    // the km under the centre of the view (not the mean of the edges: clusters stretch the scale)
    el.minimap.setAttribute("aria-valuenow", fmtKm(centerKm()));
    el.minimap.setAttribute("aria-valuetext", `Showing km ${fmtKm(k0)} to ${fmtKm(k1)}`);
  }

  function scrollToKm(km, animate = true) {
    const target = kmToA(L, clamp(km, 0, ROUTE_KM)) - viewLen() / 2;
    const opts = { behavior: animate ? smooth() : "auto" };
    opts[view.vertical ? "top" : "left"] = Math.max(0, target);
    el.scroller.scrollTo(opts);
  }

  function minimapKm(evt) {
    const r = el.minimap.getBoundingClientRect();
    return clamp((evt.clientX - r.left) / r.width, 0, 1) * ROUTE_KM;
  }

  /* ------------------------------------------------------------------
     Filters and URL hash
     ------------------------------------------------------------------ */
  const DEFAULTS = { dir: "bigbay", grades: GRADE_ORDER.slice(), pos: "all", min: 0, veg: false, side: false, q: "" };
  let state = readHash();

  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    const s = { ...DEFAULTS, grades: DEFAULTS.grades.slice() };
    if (p.get("dir") === "blr") s.dir = "blr";
    if (p.has("grades")) s.grades = p.get("grades").split(",").filter((g) => g in GRADES);
    if (["highway", "detour"].includes(p.get("pos"))) s.pos = p.get("pos");
    const min = parseFloat(p.get("min"));
    if ([3.5, 4, 4.5].includes(min)) s.min = min;
    s.veg = p.get("veg") === "1";
    s.side = p.get("side") === "1";
    s.q = (p.get("q") || "").slice(0, 60);
    return s;
  }

  function writeHash() {
    const p = new URLSearchParams();
    if (state.dir !== DEFAULTS.dir) p.set("dir", state.dir);
    if (state.grades.length !== GRADE_ORDER.length) p.set("grades", state.grades.join(","));
    if (state.pos !== "all") p.set("pos", state.pos);
    if (state.min) p.set("min", String(state.min));
    if (state.veg) p.set("veg", "1");
    if (state.side) p.set("side", "1");
    if (state.q) p.set("q", state.q);
    const hash = p.toString();
    const url = location.href.split("#")[0] + (hash ? "#" + hash : "");
    if (url !== location.href) {
      try { history.replaceState(null, "", url); } catch (e) { location.replace(url); }
    }
  }

  const mySide = () => (state.dir === "bigbay" ? "west" : "east");

  function matches(st) {
    const q = state.q.trim().toLowerCase();
    return state.grades.includes(st.grade)
      && (state.pos === "all" || (state.pos === "detour") === st.detour)
      && (st.rating || 0) >= state.min
      && (!state.veg || st.veg)
      && (!state.side || st.side === mySide())
      && (!q || `${st.name} ${st.cuisine}`.toLowerCase().includes(q));
  }

  function activeFilterCount() {
    return (state.grades.length !== GRADE_ORDER.length) + (state.pos !== "all") + (state.min > 0)
      + state.veg + state.side + (state.q.trim() !== "");
  }

  function syncControls() {
    document.querySelectorAll('input[name="dir"]').forEach((i) => { i.checked = i.value === state.dir; });
    document.querySelectorAll('input[name="pos"]').forEach((i) => { i.checked = i.value === state.pos; });
    document.querySelectorAll("[data-grade]").forEach((b) => b.setAttribute("aria-pressed", String(state.grades.includes(b.dataset.grade))));
    el.minRating.value = String(state.min);
    el.veg.checked = state.veg;
    el.mySide.checked = state.side;
    if (el.search.value !== state.q) el.search.value = state.q;
  }

  function applyFilters({ keepHash = false } = {}) {
    const side = mySide();
    const shown = [];
    for (const st of stations) {
      const ok = matches(st);
      if (ok) shown.push(st);
      const g = view && view.svgEl.querySelector(`.station[data-id="${st.id}"]`);
      if (g) {
        const off = state.side && st.side !== side;
        g.classList.toggle("off-side", off);
        g.classList.toggle("dim", !ok && !off);
        g.setAttribute("tabindex", ok ? "0" : "-1");
        g.setAttribute("aria-hidden", ok ? "false" : "true");
      }
      const tick = el.minimap.querySelector(`.mm-st[data-id="${st.id}"]`);
      if (tick) tick.style.opacity = ok ? "1" : "0.2";
    }
    if (view) view.svgEl.querySelectorAll(".lane-text").forEach((t) => t.classList.toggle("mine", t.dataset.lane === side));
    if (state.dir === "blr") shown.reverse();
    renderList(shown);
    el.count.textContent = `${shown.length} of ${stations.length} stops`;
    const n = activeFilterCount();
    el.filtersCount.textContent = String(n);
    el.filtersCount.hidden = n === 0;
    el.listOrder.textContent = state.dir === "bigbay" ? "· Mall of Asia → Big Bay" : "· Big Bay → Bengaluru";
    const where = mqMobile.matches ? (side === "west" ? "left of the line" : "right of the line")
                                   : (side === "west" ? "above the line" : "below the line");
    el.sideHint.textContent = `Keep left: ${side} stops (${where}) are on your side; the others need a U-turn.`;
    if (!keepHash) writeHash();
  }

  function renderList(shown) {
    const side = mySide();
    el.list.innerHTML = shown.map((st) => {
      const g = GRADES[st.grade];
      const sideText = `${st.side === "west" ? "West" : "East"}, ${st.side === side ? '<span class="side-mine">your side</span>' : "across the median"}`;
      const where = st.detour ? ` · detour ${fmtKm(st.detour_km)} km` : "";
      return `<li><button type="button" class="card${st.id === selectedId ? " selected" : ""}" data-id="${st.id}" aria-label="${esc(ariaLabel(st))}">
        <span class="name">${esc(st.name)}</span>
        <span class="meta">km ${fmtKm(st.km)} · ${sideText}${where}</span>
        <span class="right"><span class="badge g-${st.grade}"><span class="sym" aria-hidden="true">${g.sym}</span>${g.label}</span>
        <span class="rating">${st.rating ? "★ " + st.rating.toFixed(1) : "No rating"}</span></span>
      </button></li>`;
    }).join("");
    el.empty.hidden = shown.length > 0;
  }

  /* ------------------------------------------------------------------
     Detail panel, tooltip, selection
     ------------------------------------------------------------------ */
  let selectedId = null;
  let returnFocus = null;

  function markSelected(id) {
    if (!view) return;
    view.svgEl.querySelectorAll(".station.selected").forEach((g) => g.classList.remove("selected"));
    el.list.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
    if (id == null) return;
    const g = view.svgEl.querySelector(`.station[data-id="${id}"]`);
    if (g) g.classList.add("selected");
    const c = el.list.querySelector(`.card[data-id="${id}"]`);
    if (c) c.classList.add("selected");
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso + "T00:00:00Z");
    return isNaN(d) ? iso : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  }

  function safeMapsUrl(url) {
    return /^https:\/\/(www\.)?google\.[a-z.]+\/maps[/?]/i.test(url || "") ? url : null;
  }

  function panelHtml(st) {
    const g = GRADES[st.grade], w = st.washroom, total = w.positive + w.neutral + w.negative;
    const side = st.side === mySide() ? "your side" : "across the median";
    const pct = (v) => (total ? (v / total * 100).toFixed(1) : 0);
    const mean = w.mean == null ? "" : ` · mean score ${w.mean > 0 ? "+" : ""}${w.mean.toFixed(2)}`;
    const parts = [
      `<div class="panel-top"><h2 id="panel-title">${esc(st.name)}</h2>
       <button type="button" class="btn icon close" id="panel-close" aria-label="Close details">✕</button></div>`,
      `<p class="sub">${st.rating ? `★ ${st.rating.toFixed(1)} on Google (${st.google_reviews.toLocaleString("en-IN")} reviews)` : "No Google rating"} · ${esc(st.cuisine)}${st.veg ? " · Veg" : ""}</p>`,
      `<dl class="facts">
        <dt>Route</dt><dd>km ${fmtKm(st.km)} from Mall of Asia</dd>
        <dt>Side</dt><dd>${st.side === "west" ? "West" : "East"}, ${side}</dd>
        <dt>Access</dt><dd>${st.detour ? `Detour: ${fmtKm(st.detour_km)} km from the road` : "On the highway"}</dd>
      </dl>`,
      `<h3>Washroom</h3>
       <div class="grade-line g-${st.grade}"><span class="sym" aria-hidden="true">${g.sym}</span>${g.label}${mean}</div>`,
    ];
    if (total) {
      parts.push(`<div class="bar" role="img" aria-label="${w.positive} positive, ${w.neutral} neutral, ${w.negative} negative washroom reviews">
          ${w.positive ? `<span class="pos" style="width:${pct(w.positive)}%"></span>` : ""}
          ${w.neutral ? `<span class="neu" style="width:${pct(w.neutral)}%"></span>` : ""}
          ${w.negative ? `<span class="neg" style="width:${pct(w.negative)}%"></span>` : ""}
        </div>
        <ul class="bar-legend" aria-hidden="true">
          <li><span class="sw" style="background:var(--good)"></span>${w.positive} positive</li>
          <li><span class="sw" style="background:var(--none)"></span>${w.neutral} neutral</li>
          <li><span class="sw" style="background:var(--bad)"></span>${w.negative} negative</li>
        </ul>`);
    }
    if (st.low_data) {
      parts.push(`<p class="note"><strong>Low data</strong>${w.reviews ? `Only ${w.reviews} washroom review${w.reviews === 1 ? "" : "s"}, so treat the grade as tentative.` : "No reviews mention the washroom yet."}</p>`);
    }
    if (st.position_check) {
      parts.push(`<p class="note"><strong>Position not verified</strong>${esc(st.position_note.charAt(0).toUpperCase() + st.position_note.slice(1))}.</p>`);
    }
    parts.push(`<div id="panel-quotes" aria-live="polite">${quotesHtml(st)}</div>`);
    const url = safeMapsUrl(st.maps_url);
    if (url) parts.push(`<div class="actions"><a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open in Google Maps<span class="sr-only"> (opens in a new tab)</span></a></div>`);
    return parts.join("");
  }

  function quotesHtml(st) {
    if (st.snippets == null) return st.washroom.reviews ? '<p class="muted">Loading what reviewers said…</p>' : "";
    if (!st.snippets.length) return "";
    return `<h3>What reviewers said</h3><ul class="quotes">${st.snippets.map((s) =>
      `<li><q>${esc(s.text)}</q><span class="qmeta">${esc(fmtDate(s.date))}${s.date ? " · " : ""}${esc(s.sentiment)}</span></li>`).join("")}</ul>`;
  }

  function openPanel(id, opener) {
    const st = byId.get(id);
    if (!st) return;
    if (!el.panel.classList.contains("open")) returnFocus = opener;
    selectedId = id;
    el.panelBody.innerHTML = panelHtml(st);
    el.panel.classList.add("open");
    markSelected(id);
    hideTip();
    $("#panel-close").addEventListener("click", closePanel);
    el.panel.focus({ preventScroll: true });
    if (st.snippets == null && !st.washroom.reviews) st.snippets = [];
    if (st.snippets == null) {
      // fill only the quotes, so focus inside the panel is kept; skip if another stop is open by then
      loadSnippets(st).then(() => { if (selectedId === id) $("#panel-quotes").innerHTML = quotesHtml(st); });
    }
  }

  function closePanel() {
    if (!el.panel.classList.contains("open")) return;
    el.panel.classList.remove("open");
    markSelected(null);
    selectedId = null;
    const r = returnFocus;
    returnFocus = null;
    if (!r) return;
    const target = r.type === "card" ? el.list.querySelector(`.card[data-id="${r.id}"]`)
                 : r.type === "station" ? view.svgEl.querySelector(`.station[data-id="${r.id}"]`) : r.el;
    if (target) target.focus({ preventScroll: r.type === "station" });
  }

  function showTip(g) {
    const st = byId.get(Number(g.dataset.id));
    const gr = GRADES[st.grade];
    el.tip.innerHTML = `<strong>${esc(st.name)}</strong>${gr.sym} ${gr.label} washroom · km ${fmtKm(st.km)}${st.rating ? ` · ★ ${st.rating.toFixed(1)}` : ""}`;
    el.tip.hidden = false;
    const r = g.querySelector(".dot").getBoundingClientRect();
    const tw = el.tip.offsetWidth, th = el.tip.offsetHeight;
    let x = r.left + r.width / 2 - tw / 2, y = r.top - th - 10;
    if (y < 8) y = r.bottom + 10;
    x = clamp(x, 8, innerWidth - tw - 8);
    el.tip.style.left = x + "px";
    el.tip.style.top = y + "px";
  }
  function hideTip() { el.tip.hidden = true; }

  function focusStationOnMap(id) {
    const it = L.items.find((i) => i.st.id === id);
    if (it) scrollToKm(aToKm(L, it.a));
  }

  /* ------------------------------------------------------------------
     Events
     ------------------------------------------------------------------ */
  function wire() {
    // map: delegated station events
    let hoverG = null, drag = null, dragged = false;
    el.scroller.addEventListener("mouseover", (e) => {
      const g = e.target.closest(".station");
      if (g === hoverG) return;
      if (hoverG) hoverG.classList.remove("hover");
      hoverG = g;
      if (g) { g.classList.add("hover"); showTip(g); } else hideTip();
    });
    el.scroller.addEventListener("mouseleave", () => { if (hoverG) hoverG.classList.remove("hover"); hoverG = null; hideTip(); });
    el.scroller.addEventListener("click", (e) => {
      if (dragged) { dragged = false; return; }
      const g = e.target.closest(".station");
      if (g) openPanel(Number(g.dataset.id), { type: "station", id: Number(g.dataset.id) });
    });
    el.scroller.addEventListener("keydown", (e) => {
      const g = e.target.closest && e.target.closest(".station");
      if (g && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        openPanel(Number(g.dataset.id), { type: "station", id: Number(g.dataset.id) });
      }
    });
    el.scroller.addEventListener("focusin", (e) => {
      const g = e.target.closest(".station");
      if (g) showTip(g);
    });
    el.scroller.addEventListener("focusout", hideTip);
    // drag to pan with a mouse
    el.scroller.addEventListener("pointerdown", (e) => {
      // e.target is the scroller itself only on its scrollbar
      if (e.pointerType !== "mouse" || e.button !== 0 || e.target === el.scroller) return;
      drag = { x: e.clientX, y: e.clientY, l: el.scroller.scrollLeft, t: el.scroller.scrollTop };
      dragged = false;
    });
    addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!dragged && Math.hypot(dx, dy) < 5) return;
      dragged = true;
      el.scroller.classList.add("dragging");
      if (view.vertical) el.scroller.scrollTop = drag.t - dy; else el.scroller.scrollLeft = drag.l - dx;
    });
    addEventListener("pointerup", () => { drag = null; el.scroller.classList.remove("dragging"); });
    el.scroller.addEventListener("scroll", () => {
      hideTip();
      if (!mmFrame) mmFrame = requestAnimationFrame(updateMinimapWindow);
    }, { passive: true });

    // mini-map: click or drag to jump, arrows to step
    let mmDrag = false;
    el.minimap.addEventListener("pointerdown", (e) => {
      mmDrag = true;
      el.minimap.setPointerCapture(e.pointerId);
      scrollToKm(minimapKm(e));
    });
    el.minimap.addEventListener("pointermove", (e) => { if (mmDrag && e.buttons) scrollToKm(minimapKm(e), false); });
    el.minimap.addEventListener("pointerup", () => { mmDrag = false; });
    el.minimap.addEventListener("keydown", (e) => {
      const step = { ArrowLeft: -2, ArrowDown: -2, ArrowRight: 2, ArrowUp: 2, PageDown: 10, PageUp: -10 }[e.key];
      if (step) { e.preventDefault(); scrollToKm(centerKm() + step); }
      else if (e.key === "Home") { e.preventDefault(); scrollToKm(0); }
      else if (e.key === "End") { e.preventDefault(); scrollToKm(ROUTE_KM); }
    });

    // zoom
    const setZoom = (idx) => {
      const key = view.vertical ? "v" : "h", km = centerKm();
      zoom[key] = clamp(idx, 0, ZOOMS.length - 1);
      render(km);
    };
    $("#zoom-in").addEventListener("click", () => setZoom(zoom[view.vertical ? "v" : "h"] + 1));
    $("#zoom-out").addEventListener("click", () => setZoom(zoom[view.vertical ? "v" : "h"] - 1));
    $("#zoom-fit").addEventListener("click", () => {
      const M = modeParams(view.vertical);
      setZoom(fitIndex(view.vertical, M, view.vertical ? (el.scroller.clientWidth - 4 * M.lane) / 2 : 0));
    });

    // landmark jumps
    const jumps = $("#jumps");
    jumps.innerHTML = landmarks.filter((lm) => lm.km > 0).map((lm) =>
      `<button type="button" class="btn" data-km="${lm.km}">${esc(lm.name)}</button>`).join("");
    jumps.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) scrollToKm(Number(b.dataset.km));
    });

    // list
    el.list.addEventListener("click", (e) => {
      const c = e.target.closest(".card");
      if (!c) return;
      const id = Number(c.dataset.id);
      focusStationOnMap(id);
      openPanel(id, { type: "card", id });
    });

    // filters
    document.querySelectorAll('input[name="dir"]').forEach((i) => i.addEventListener("change", () => { state.dir = i.value; applyFilters(); }));
    document.querySelectorAll('input[name="pos"]').forEach((i) => i.addEventListener("change", () => { state.pos = i.value; applyFilters(); }));
    document.querySelectorAll("[data-grade]").forEach((b) => b.addEventListener("click", () => {
      const g = b.dataset.grade;
      state.grades = state.grades.includes(g) ? state.grades.filter((x) => x !== g) : GRADE_ORDER.filter((x) => x === g || state.grades.includes(x));
      b.setAttribute("aria-pressed", String(state.grades.includes(g)));
      applyFilters();
    }));
    el.minRating.addEventListener("change", () => { state.min = Number(el.minRating.value); applyFilters(); });
    el.veg.addEventListener("change", () => { state.veg = el.veg.checked; applyFilters(); });
    el.mySide.addEventListener("change", () => { state.side = el.mySide.checked; applyFilters(); });
    el.search.addEventListener("input", () => { state.q = el.search.value.slice(0, 60); applyFilters(); });
    $("#clear-filters").addEventListener("click", () => {
      state = { ...DEFAULTS, grades: DEFAULTS.grades.slice(), dir: state.dir };
      syncControls();
      applyFilters();
    });
    addEventListener("hashchange", () => { state = readHash(); syncControls(); applyFilters({ keepHash: true }); });

    // filter sheet (phones)
    const openSheet = () => {
      el.filters.classList.add("open");
      el.scrim.hidden = false;
      el.filtersOpen.setAttribute("aria-expanded", "true");
      el.filters.querySelector("[data-grade]").focus();
    };
    const closeSheet = () => {
      if (!el.filters.classList.contains("open")) return false;
      el.filters.classList.remove("open");
      el.scrim.hidden = true;
      el.filtersOpen.setAttribute("aria-expanded", "false");
      el.filtersOpen.focus();
      return true;
    };
    el.filtersOpen.addEventListener("click", openSheet);
    $("#filters-done").addEventListener("click", closeSheet);
    el.scrim.addEventListener("click", closeSheet);

    addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (closeSheet()) return;
      closePanel();
    });

    // re-layout when the mode or width changes, keeping the same part of the route in view
    let lastW = el.scroller.clientWidth, resizeTimer = 0;
    const relayout = () => { const km = centerKm(); lastW = el.scroller.clientWidth; render(km); };
    mqMobile.addEventListener("change", () => { closeSheet(); relayout(); });
    addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { if (el.scroller.clientWidth !== lastW) relayout(); }, 150);
    });
  }

  function header() {
    $("#route-km").textContent = String(Math.round(ROUTE_KM));
    const counts = Object.fromEntries(GRADE_ORDER.map((g) => [g, stations.filter((s) => s.grade === g).length]));
    $("#chips").innerHTML = `<li class="chip total"><b>${stations.length}</b>stops</li>` + GRADE_ORDER.map((g) =>
      `<li class="chip g-${g}"><span class="sym" aria-hidden="true">${GRADES[g].sym}</span><b>${counts[g]}</b>${g === "none" ? "no data" : GRADES[g].label.toLowerCase()}</li>`).join("");
  }

  header();
  syncControls();
  wire();
  render();
})();
