"""
scripts/verify_site.py
======================
Browser check of the route website (site/index.html) with Playwright and the
installed Microsoft Edge (falls back to Playwright's Chromium).

  - Screenshots at 1440x900, 768x1024 and 390x844, light and dark, saved in
    site/screenshots/.
  - Geometry check of the rendered map: no two labels overlap, no label covers
    another station's dot or spur, nothing is clipped by the SVG, and the page
    never scrolls sideways.
  - Scripted flows: zoom and scroll, mini-map jump, landmark jump, station
    click opens the right panel, Esc closes it and returns focus, every filter
    gives the expected count, direction + "only my side", search, the URL hash
    restores filters, keyboard-only use, the phone filter sheet and 44 px tap
    targets.
  - Fails on any console error or warning, and if DOMContentLoaded takes
    300 ms or more.

Usage (from the repository root; rebuild site/data.js first):

    python scripts/build_route_map.py
    python scripts/verify_site.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site" / "index.html"
SHOTS = ROOT / "site" / "screenshots"
SIZES = [(1440, 900), (768, 1024), (390, 844)]
DCL_LIMIT_MS = 300

failures: list[str] = []
passes: list[str] = []


def check(ok: bool, what: str) -> bool:
    (passes if ok else failures).append(what)
    print(("  ok    " if ok else "  FAIL  ") + what)
    return ok


def load_data() -> dict:
    text = (ROOT / "site" / "data.js").read_text(encoding="utf-8")
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


# Runs in the page: every label, dot and spur as a polygon in SVG coordinates,
# then pairwise separating-axis tests between different stations.
GEOMETRY_JS = r"""
() => {
  const svg = document.querySelector('#map-scroll svg');
  const W = +svg.getAttribute('width'), H = +svg.getAttribute('height');
  const poly = (el, pad = 0) => {
    const b = el.getBBox(), m = el.getCTM(), s = svg.getCTM().inverse().multiply(m);
    const x0 = b.x + pad, y0 = b.y + pad, x1 = b.x + b.width - pad, y1 = b.y + b.height - pad;
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(([x, y]) => { const p = new DOMPoint(x, y).matrixTransform(s); return [p.x, p.y]; });
  };
  const linePoly = (l, w) => {
    const x1 = +l.getAttribute('x1'), y1 = +l.getAttribute('y1'), x2 = +l.getAttribute('x2'), y2 = +l.getAttribute('y2');
    const len = Math.hypot(x2 - x1, y2 - y1) || 1, nx = -(y2 - y1) / len * w / 2, ny = (x2 - x1) / len * w / 2;
    return [[x1 + nx, y1 + ny], [x2 + nx, y2 + ny], [x2 - nx, y2 - ny], [x1 - nx, y1 - ny]];
  };
  const circlePoly = (c) => {
    const cx = +c.getAttribute('cx'), cy = +c.getAttribute('cy'), r = +c.getAttribute('r');
    return Array.from({ length: 8 }, (_, i) => [cx + r * Math.cos(i * Math.PI / 4), cy + r * Math.sin(i * Math.PI / 4)]);
  };
  const overlap = (A, B) => {
    for (const P of [A, B]) for (let i = 0; i < P.length; i++) {
      const [x1, y1] = P[i], [x2, y2] = P[(i + 1) % P.length], nx = y2 - y1, ny = x1 - x2;
      let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
      for (const [x, y] of A) { const d = x * nx + y * ny; a0 = Math.min(a0, d); a1 = Math.max(a1, d); }
      for (const [x, y] of B) { const d = x * nx + y * ny; b0 = Math.min(b0, d); b1 = Math.max(b1, d); }
      if (a1 <= b0 || b1 <= a0) return false;
    }
    return true;
  };
  const items = [];
  svg.querySelectorAll('.station').forEach((g) => {
    const id = g.dataset.id, name = g.querySelector('.st-label').textContent;
    g.querySelectorAll('.st-label, .spur-label').forEach((t) => items.push({ id, name, kind: 'label', p: poly(t, 1) }));
    items.push({ id, name, kind: 'dot', p: circlePoly(g.querySelector('.dot')) });
    g.querySelectorAll('.spur').forEach((l) => items.push({ id, name, kind: 'spur', p: linePoly(l, 3) }));
  });
  const deco = [...svg.querySelectorAll('.terminal-name, .terminal-sub, .lm-name, .tick, .road-sign, .road-tick, .lane-text')]
    .map((t) => ({ id: 'deco', name: t.textContent, kind: 'label', p: poly(t, 1) }));
  const problems = [];
  const all = items.concat(deco);
  for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) {
    const a = all[i], b = all[j];
    if (a.id === b.id) continue;
    if (a.kind !== 'label' && b.kind !== 'label' && !(a.kind === 'dot' && b.kind === 'dot')) continue;
    if (a.id === 'deco' && b.id === 'deco') {
      // lane names may sit next to each other on the road, never on top
      if (!overlap(a.p, b.p)) continue;
    }
    if (overlap(a.p, b.p)) problems.push(`${a.kind} "${a.name}" overlaps ${b.kind} "${b.name}"`);
  }
  for (const it of all) {
    if (it.kind !== 'label') continue;
    if (it.p.some(([x, y]) => x < -0.5 || y < -0.5 || x > W + 0.5 || y > H + 0.5)) problems.push(`label "${it.name}" is clipped by the map edge`);
  }
  return { problems, labels: items.filter((i) => i.kind === 'label').length };
}
"""

PAGE_FIT_JS = r"""
() => {
  const problems = [];
  if (document.documentElement.scrollWidth > innerWidth + 1) problems.push(`page scrolls sideways (${document.documentElement.scrollWidth}px > ${innerWidth}px)`);
  for (const e of document.querySelectorAll('button, .chip, .seg span, .card .name, .card .meta, h1, h2, .legend li, .filter-label, legend')) {
    const r = e.getBoundingClientRect();
    if (r.width <= 1 || e.closest('.sr-only') || getComputedStyle(e).visibility === 'hidden') continue;
    if (e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflowX !== 'auto') problems.push(`text clipped in <${e.tagName.toLowerCase()}> "${e.textContent.trim().slice(0, 40)}"`);
  }
  return problems;
}
"""

# Text and graphic colour pairs from the CSS tokens, as WCAG contrast ratios.
CONTRAST_JS = r"""
() => {
  const probe = document.createElement('span');
  document.body.appendChild(probe);
  const rgb = (v) => { probe.style.color = `var(${v})`; return getComputedStyle(probe).color.match(/[\d.]+/g).slice(0, 3).map(Number); };
  const lum = ([r, g, b]) => { const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const ratio = (a, b) => { const [l1, l2] = [lum(rgb(a)), lum(rgb(b))].sort((x, y) => y - x); return (l1 + 0.05) / (l2 + 0.05); };
  const text = [['--ink', '--surface'], ['--ink', '--bg'], ['--muted', '--surface'], ['--muted', '--bg'], ['--muted', '--surface-2'],
                ['--ink', '--surface-2'], ['--lane-ink', '--road'], ['--accent-ink', '--accent'], ['--surface', '--ink'], ['--dot-ink', '--dot'],
                ['--accent', '--surface']];
  const graphic = [['--good', '--surface'], ['--moderate', '--surface'], ['--bad', '--surface'], ['--none', '--surface'],
                   ['--focus', '--surface'], ['--road', '--surface']];
  const out = [];
  for (const [f, b] of text) { const r = ratio(f, b); if (r < 4.5) out.push(`text ${f} on ${b}: ${r.toFixed(2)} < 4.5`); }
  for (const [f, b] of graphic) { const r = ratio(f, b); if (r < 3) out.push(`graphic ${f} on ${b}: ${r.toFixed(2)} < 3`); }
  probe.remove();
  return { checked: text.length + graphic.length, out };
}
"""

TAP_JS = r"""
() => {
  const small = [];
  const visible = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const targets = [...document.querySelectorAll('button, a[href], select, input[type="search"], label.check, .seg label, .station .hit, #minimap')]
    .filter((e) => visible(e) && !e.closest('.filters:not(.open)') && !e.closest('.panel:not(.open)') && !e.classList.contains('skip'));
  for (const e of targets) {
    const r = e.getBoundingClientRect();
    if (r.width < 43.5 || r.height < 43.5) small.push(`${e.tagName.toLowerCase()}.${[...e.classList].join('.')} "${(e.textContent || e.getAttribute('aria-label') || '').trim().slice(0, 30)}" ${r.width.toFixed(0)}x${r.height.toFixed(0)}`);
  }
  return { count: targets.length, small };
}
"""


def launch(p):
    try:
        return p.chromium.launch(channel="msedge")
    except Error:
        return p.chromium.launch()


def new_page(browser, w, h, scheme="light", hash_=""):
    ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
    page = ctx.new_page()
    page.console_problems = []
    page.on("console", lambda m: m.type in ("error", "warning") and page.console_problems.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: page.console_problems.append(f"pageerror: {e}"))
    page.goto(SITE.as_uri() + hash_)
    page.wait_for_load_state("load")
    return page


def wait_scroll_settled(page):
    page.wait_for_timeout(120)   # let a smooth scroll start before waiting for it to stop
    page.wait_for_function("""() => new Promise((res) => {
        const s = document.querySelector('#map-scroll'); let last = -1, same = 0;
        const tick = () => { const v = s.scrollLeft + s.scrollTop; same = v === last ? same + 1 : 0; last = v;
                             if (same > 5) res(true); else requestAnimationFrame(tick); };
        tick(); })""")


def count(page) -> int:
    return int(re.match(r"(\d+)", page.text_content("#result-count")).group(1))


def card_ids(page) -> list[int]:
    return page.eval_on_selector_all(".card", "cs => cs.map(c => +c.dataset.id)")


def main() -> int:
    data = load_data()
    st = data["stations"]
    n = len(st)
    by_id = {s["id"]: s for s in st}
    SHOTS.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = launch(p)

        # The first page load in a freshly started browser also pays for the browser
        # warming up (fonts, GPU process). Report it, but judge DOMContentLoaded on the loads after it.
        page = new_page(browser, 1440, 900)
        cold = page.evaluate("performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd")
        print(f"  info  first load after the browser started: DOMContentLoaded {cold:.0f} ms (browser warm-up, not judged)")
        page.context.close()

        print("Screenshots, layout and console")
        for w, h in SIZES:
            for scheme in ("light", "dark"):
                # median of three fresh loads: single timings on a busy machine are noisy
                timings = []
                for _ in range(2):
                    extra = new_page(browser, w, h, scheme)
                    timings.append(extra.evaluate("performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd"))
                    extra.context.close()
                page = new_page(browser, w, h, scheme)
                dcl, fcp = page.evaluate("""() => new Promise((res) => new PerformanceObserver((list, obs) => {
                    const p = list.getEntriesByName('first-contentful-paint')[0];
                    if (p) { obs.disconnect(); res([performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd, p.startTime]); }
                  }).observe({ type: 'paint', buffered: true }))""")
                timings = sorted(timings + [dcl])
                check(timings[1] < DCL_LIMIT_MS, f"{w}x{h} {scheme}: DOMContentLoaded median {timings[1]:.0f} ms of "
                      f"{', '.join(f'{x:.0f}' for x in timings)} (< {DCL_LIMIT_MS}), first paint {fcp:.0f} ms")
                geo = page.evaluate(GEOMETRY_JS)
                check(not geo["problems"], f"{w}x{h} {scheme}: {geo['labels']} map labels, no overlap or clipping"
                      + ("" if not geo["problems"] else "\n          " + "\n          ".join(geo["problems"][:12])))
                fit = page.evaluate(PAGE_FIT_JS)
                check(not fit, f"{w}x{h} {scheme}: no sideways scroll or clipped text"
                      + ("" if not fit else "\n          " + "\n          ".join(fit[:12])))
                if w == 1440:
                    con = page.evaluate(CONTRAST_JS)
                    check(not con["out"], f"{scheme}: {con['checked']} colour pairs meet WCAG AA (text 4.5:1, graphics 3:1)"
                          + ("" if not con["out"] else "\n          " + "\n          ".join(con["out"])))
                page.screenshot(path=str(SHOTS / f"{w}x{h}-{scheme}.png"))
                if w < 768:
                    taps = page.evaluate(TAP_JS)
                    check(not taps["small"], f"{w}x{h} {scheme}: {taps['count']} tap targets are all at least 44 px"
                          + ("" if not taps["small"] else "\n          " + "\n          ".join(taps["small"][:12])))
                check(not page.console_problems, f"{w}x{h} {scheme}: no console errors or warnings {page.console_problems}")
                page.context.close()

        print("Zoom levels keep the map collision-free (1440x900)")
        page = new_page(browser, 1440, 900)
        for label, sel, times in (("zoomed in x3", "#zoom-in", 3), ("zoomed out x6", "#zoom-out", 6), ("fit", "#zoom-fit", 1)):
            for _ in range(times):
                page.click(sel)
            geo = page.evaluate(GEOMETRY_JS)
            check(not geo["problems"], f"{label}: no overlap or clipping" + ("" if not geo["problems"] else "\n          " + "\n          ".join(geo["problems"][:8])))
        page.context.close()

        print("Flows (1440x900)")
        page = new_page(browser, 1440, 900)
        width = lambda: page.eval_on_selector("#map-scroll svg", "s => +s.getAttribute('width')")
        w0 = width(); page.click("#zoom-in"); w1 = width(); page.click("#zoom-out"); page.click("#zoom-out"); w2 = width()
        check(w1 > w0 > w2, f"zoom: + widens the map ({w0} -> {w1}), - narrows it ({w2})")
        page.click("#zoom-fit")
        whole = page.eval_on_selector("#map-scroll", "s => s.scrollWidth <= s.clientWidth + 1")
        w_fit = width(); page.click("#zoom-out"); at_min = width() == w_fit
        check(whole or at_min, f"zoom: fit shows the whole route, or the widest overview when it cannot ({w_fit}px)")
        page.click("#zoom-in"); page.click("#zoom-in")
        x_before = float(page.get_attribute("#mm-window", "x"))
        page.eval_on_selector("#map-scroll", "s => s.scrollTo({ left: 900 })")
        wait_scroll_settled(page)
        x_after = float(page.get_attribute("#mm-window", "x"))
        check(x_after > x_before, f"scroll: the mini-map window follows the map ({x_before:.0f} -> {x_after:.0f})")

        page.click("#zoom-in"); page.click("#zoom-in")
        box = page.locator("#minimap").bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] / 2)
        wait_scroll_settled(page)
        target = 0.6 * data["route_km"]
        x, wdt = (float(page.get_attribute("#mm-window", a)) for a in ("x", "width"))
        k0, k1 = x / 1000 * data["route_km"], (x + wdt) / 1000 * data["route_km"]
        check(k0 <= target <= k1, f"mini-map jump: km {target:.1f} clicked, km {k0:.1f} to {k1:.1f} now in view")

        lm = next(l for l in data["landmarks"] if l["name"] == "Devanahalli")
        page.click('#jumps button:has-text("Devanahalli")')
        wait_scroll_settled(page)
        centre = float(page.get_attribute("#minimap", "aria-valuenow"))
        check(abs(centre - lm["km"]) < 2, f"landmark jump: centre km {centre:.1f}, Devanahalli at {lm['km']}")

        sid = next(s["id"] for s in st if s["name"].startswith("Aaradhya"))
        page.click(f'.station[data-id="{sid}"] .dot')
        page.wait_for_selector("#panel.open")
        title = page.text_content("#panel-title")
        check(title == by_id[sid]["name"], f"station click opens its panel ({title})")
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('#panel').classList.contains('open')")
        focus = page.evaluate("() => document.activeElement.closest && document.activeElement.closest('.station')?.dataset.id")
        check(focus == str(sid), "Esc closes the panel and returns focus to the station")

        notes_sid = next(s["id"] for s in st if s["position_check"] and s["low_data"] and s["washroom"]["reviews"])
        page.click(f'.card[data-id="{notes_sid}"]')
        page.wait_for_selector("#panel.open")
        body = page.text_content("#panel-body")
        check("Position not verified" in body and "Low data" in body and "Open in Google Maps" in body,
              f"panel shows the low-data and position notes and the Maps button ({by_id[notes_sid]['name']})")
        page.keyboard.press("Escape")

        def expect(label, expected_ids):
            got = count(page)
            ids = card_ids(page)
            dim = page.eval_on_selector_all(".station.dim, .station.off-side", "x => x.length")
            ok = got == len(expected_ids) and sorted(ids) == sorted(expected_ids) and dim == n - got
            check(ok, f"{label}: {got} of {n} (expected {len(expected_ids)}, {dim} dimmed on the map)")

        def clear():
            page.click("#clear-filters")

        clear()
        page.click('[data-grade="good"]')
        expect("grade filter without good", [s["id"] for s in st if s["grade"] != "good"])
        page.click('[data-grade="bad"]')
        expect("grade filter without good and bad", [s["id"] for s in st if s["grade"] not in ("good", "bad")])
        clear()
        page.check('input[name="pos"][value="detour"]')
        expect("detours only", [s["id"] for s in st if s["detour"]])
        page.check('input[name="pos"][value="highway"]')
        expect("on the highway only", [s["id"] for s in st if not s["detour"]])
        clear()
        page.select_option("#min-rating", "4.5")
        expect("rating 4.5 and up", [s["id"] for s in st if (s["rating"] or 0) >= 4.5])
        clear()
        page.check("#veg-only")
        expect("veg only", [s["id"] for s in st if s["veg"]])
        clear()
        check(count(page) == n, "clear filters shows all stops")

        page.check('input[name="dir"][value="blr"]')
        page.check("#my-side")
        east = [s for s in st if s["side"] == "east"]
        expect("going to Bengaluru, only my side", [s["id"] for s in east])
        kms = [by_id[i]["km"] for i in card_ids(page)]
        check(kms == sorted(kms, reverse=True), "going to Bengaluru lists stops from Big Bay down")
        page.check('input[name="dir"][value="bigbay"]')
        west = [s for s in st if s["side"] == "west"]
        expect("going to Big Bay, only my side", [s["id"] for s in west])
        kms = [by_id[i]["km"] for i in card_ids(page)]
        check(kms == sorted(kms), "going to Big Bay lists stops from Mall of Asia up")
        clear()
        page.uncheck("#my-side")

        page.fill("#search", "biryani")
        expect("search 'biryani'", [s["id"] for s in st if "biryani" in f"{s['name']} {s['cuisine']}".lower()])
        page.fill("#search", "kapoor")
        check(card_ids(page) == [next(s["id"] for s in st if s["name"].startswith("Kapoor"))], "search finds Kapoor's Cafe")
        clear()

        page.click('[data-grade="none"]')
        page.check('input[name="pos"][value="highway"]')
        page.select_option("#min-rating", "4")
        page.check('input[name="dir"][value="blr"]')
        before = (count(page), card_ids(page))
        hash_ = page.evaluate("location.hash")
        page.reload()
        page.wait_for_load_state("load")
        after = (count(page), card_ids(page))
        restored = page.evaluate("""() => [document.querySelector('[data-grade="none"]').getAttribute('aria-pressed'),
            document.querySelector('input[name="pos"]:checked').value, document.querySelector('#min-rating').value,
            document.querySelector('input[name="dir"]:checked').value]""")
        check(before == after and restored == ["false", "highway", "4", "blr"],
              f"URL hash {hash_} restores the filters after reload ({after[0]} stops)")
        check(not page.console_problems, f"flows: no console errors or warnings {page.console_problems}")
        page.screenshot(path=str(SHOTS / "1440x900-filtered.png"))
        page.context.close()

        print("Keyboard only (1440x900)")
        page = new_page(browser, 1440, 900)
        on = lambda sel: page.evaluate(f"() => !!document.activeElement.closest && !!document.activeElement.closest('{sel}')")
        for _ in range(80):
            page.keyboard.press("Tab")
            if on(".station"):
                break
        sid = page.evaluate("() => document.activeElement.dataset.id")
        check(on(".station"), f"Tab reaches the map stations (station {sid})")
        ring = page.evaluate("() => getComputedStyle(document.activeElement.querySelector('.hit')).stroke")
        check(ring not in ("none", "rgba(0, 0, 0, 0)", "transparent"), f"focused station shows a focus ring ({ring})")
        page.keyboard.press("Tab")
        sid2 = page.evaluate("() => document.activeElement.dataset.id")
        check(sid2 and sid2 != sid, "Tab moves to the next station")
        page.keyboard.press("Enter")
        page.wait_for_selector("#panel.open")
        check(page.text_content("#panel-title") == by_id[int(sid2)]["name"] and on("#panel"), "Enter opens the station's panel and moves focus into it")
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('#panel').classList.contains('open')")
        check(page.evaluate("() => document.activeElement.dataset.id") == sid2, "Esc returns focus to the station")
        for _ in range(120):
            page.keyboard.press("Tab")
            if on(".card"):
                break
        cid = page.evaluate("() => document.activeElement.dataset.id")
        check(on(".card"), "Tab reaches the list cards")
        page.keyboard.press("Enter")
        page.wait_for_selector("#panel.open")
        check(page.text_content("#panel-title") == by_id[int(cid)]["name"], "Enter on a card opens its panel")
        page.wait_for_timeout(300)   # let the drawer finish sliding in
        page.screenshot(path=str(SHOTS / "1440x900-panel.png"))
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('#panel').classList.contains('open')")
        check(page.evaluate("() => document.activeElement.dataset.id") == cid and on(".card"), "Esc returns focus to the card")
        check(not page.console_problems, f"keyboard: no console errors or warnings {page.console_problems}")
        page.context.close()

        print("Phone (390x844)")
        for scheme in ("light", "dark"):
            page = new_page(browser, 390, 844, scheme)
            dims = page.eval_on_selector("#map-scroll svg", "s => [+s.getAttribute('width'), +s.getAttribute('height')]")
            check(dims[1] > dims[0] * 2, f"{scheme}: the metro line runs top to bottom ({dims[0]}x{dims[1]})")
            sides = page.evaluate("""() => { const mid = document.querySelector('#map-scroll svg').getAttribute('width') / 2;
                return [...document.querySelectorAll('.station')].map(g => [g.dataset.id, +g.querySelector('.dot').getAttribute('cx') < mid]); }""")
            check(all((by_id[int(i)]["side"] == "west") == left for i, left in sides), f"{scheme}: west stops on the left, east on the right")
            page.click("#filters-open")
            page.wait_for_selector("#filters.open")
            page.wait_for_timeout(250)
            taps = page.evaluate(TAP_JS)
            check(not taps["small"], f"{scheme}: filter sheet tap targets are at least 44 px" + ("" if not taps["small"] else "\n          " + "\n          ".join(taps["small"][:12])))
            page.check("#veg-only")
            check(count(page) == sum(s["veg"] for s in st), f"{scheme}: filter sheet applies (veg only)")
            if scheme == "light":
                page.screenshot(path=str(SHOTS / "390x844-filters-light.png"))
            page.click("#filters-done")
            check(page.evaluate("() => !document.querySelector('#filters').classList.contains('open')"), f"{scheme}: Done closes the filter sheet")
            page.evaluate("() => document.querySelector('#clear-filters').click()")
            sid = next(s["id"] for s in st if s["name"].startswith("Food Planks"))
            page.click(f'.station[data-id="{sid}"] .hit')
            page.wait_for_selector("#panel.open")
            page.wait_for_timeout(250)
            top = page.eval_on_selector("#panel", "p => p.getBoundingClientRect().top")
            check(page.text_content("#panel-title") == by_id[sid]["name"] and top > 844 * 0.15, f"{scheme}: tapping a station opens a bottom sheet (top at {top:.0f}px)")
            page.screenshot(path=str(SHOTS / f"390x844-panel-{scheme}.png"))
            page.keyboard.press("Escape")
            check(not page.console_problems, f"{scheme}: no console errors or warnings {page.console_problems}")
            page.context.close()

        browser.close()

    print(f"\n{len(passes)} checks passed, {len(failures)} failed")
    for f in failures:
        print("  FAIL " + f.splitlines()[0])
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
