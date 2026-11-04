"""Crawler estructural: recorre una web en anchura siguiendo <a href> y saca un grafo con priors.

Uso: .venv/bin/python crawler.py <site_name> [--max 25] [--depth 2]
La configuracion de cada web (url inicial, login) esta en sites.json.
"""
import argparse, asyncio, json, re, sys
from collections import deque, Counter
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
SKIP = re.compile(r"logout|log-out|signout|sign-out|delete|remove|\.pdf$|\.zip$|\.png$|\.jpg$|mailto:|tel:|javascript:", re.I)
CLICKS = False  # --clicks: pulsa botones/pestanas ademas de seguir enlaces
PREFIX_CAP = 4  # max paginas hermanas por seccion (/blog/a, /blog/b...)
UTILITY = re.compile(r"setting|config|profile|help|support|account|about|legal|privacy|terms|password|maintenance|cookie|contact|faq|login|signup|register", re.I)

LINKS_JS = """() => [...document.querySelectorAll('a[href]')].map(a => {
  const r = a.getBoundingClientRect();
  return {
    href: a.href,
    text: (a.innerText || a.getAttribute('aria-label') || a.title || '').trim().replace(/\\s+/g,' ').slice(0, 60),
    inNav: !!a.closest('nav,aside,header,[role=navigation],[role=menu],[role=menubar],[role=tablist]'),
    visible: r.width > 0 && r.height > 0,
  };
})"""

RICHNESS_JS = """() => ({
  charts: document.querySelectorAll('canvas, svg.chart, [class*=chart], [class*=graph]').length,
  tables: document.querySelectorAll('table, [role=table], [role=grid], [class*=table]').length,
  images: document.querySelectorAll('img').length,
  inputs: document.querySelectorAll('input, select, textarea').length,
  h1: (document.querySelector('h1, h2, h6, [class*=header-title], [class*=breadcrumb]')?.innerText || '').trim().slice(0, 80),
  headings: [...document.querySelectorAll('h1,h2,h3')].map(h => h.innerText.trim().replace(/\\s+/g,' ')).filter(Boolean).slice(0, 12),
  text: (document.querySelector('main') || document.body).innerText.replace(/\\s+/g,' ').slice(0, 700),
})"""


BTN_JS = """() => {
  const bad = /submit|send|enviar|buy|pay|comprar|pagar|delete|borrar|eliminar|logout|salir|save|guardar|accept|reject|aceptar|rechazar|cookie|close|cerrar/i;
  const seen = new Set();
  return [...document.querySelectorAll('button, [role=tab], [role=button], [role=menuitem]')]
    .filter(e => e.offsetParent && !e.closest('a[href]') && e.type !== 'submit')
    .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' '))
    .filter(t => t.length > 1 && t.length < 30 && !bad.test(t) && !seen.has(t) && seen.add(t))
    .slice(0, 5);
}"""


async def click_explore(page, url, origin):
    """Pulsa botones/pestanas/menus (no destructivos) y devuelve las URLs nuevas que destapan."""
    found = []
    for t in await page.evaluate(BTN_JS):
        try:
            await page.get_by_text(t, exact=True).first.click(timeout=1200)
            await page.wait_for_timeout(900)
            if page.url != url and urlparse(page.url).netloc == origin:
                found.append((page.url, t))
                await goto(page, url, settle=300)
        except Exception:
            try:
                if page.url != url:
                    await goto(page, url, settle=300)
            except Exception:
                pass
    return found


def normalize(url: str) -> str:
    """Una URL -> id de pantalla. /users/123 y /users/456 son la misma pantalla."""
    p = urlparse(url)
    path = re.sub(r"/\d+(?=/|$)", "/:id", p.path)
    path = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27,}(?=/|$)", "/:id", path)
    path = path.rstrip("/") or "/"
    frag = p.fragment if p.fragment.startswith("/") else ""  # rutas hash de SPA (#/x)
    return urlunparse(("", "", path, "", "", frag))


def slug(node_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", node_id).strip("_") or "home"


async def goto(page, url, settle=800):
    """Navegacion robusta: muchas webs nunca llegan a networkidle (analitica, websockets)."""
    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    try:
        await page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass
    await page.wait_for_timeout(settle)
    await dismiss_overlays(page)


COOKIE_RX = re.compile(r"^\s*(accept( all)?|aceptar( todo| todas)?|agree|i agree|allow all|got it|entendido|accept cookies)\s*$", re.I)


async def dismiss_overlays(page):
    """Banners de cookies genericos (incluido shadow DOM): tapan la demo en cualquier web."""
    try:
        btn = page.get_by_role("button", name=COOKIE_RX).first
        if await btn.count():
            await btn.click(timeout=1500)
            await page.wait_for_timeout(300)
    except Exception:
        pass


async def login(page, cfg):
    await page.goto(cfg["url"], wait_until="networkidle")
    for sel, val in cfg["fill"].items():
        await page.fill(sel, val)
    await page.click(cfg["submit"])
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)


async def crawl(site: str, max_pages: int, max_depth: int):
    cfg = json.loads((ROOT / "sites.json").read_text())[site]
    out = ROOT / "data" / site
    (out / "shots").mkdir(parents=True, exist_ok=True)
    origin = urlparse(cfg["start"]).netloc

    nodes, edges, seen_edges = {}, [], set()
    links_per_page = {}  # node_id -> set de destinos normalizados enlazados

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        if cfg.get("login"):
            await login(page, cfg["login"])

        queue = deque([(cfg["start"], 0, None, "Inicio", 0, True)])
        queued = {normalize(cfg["start"])}
        per_prefix = Counter()
        while queue and len(nodes) < max_pages:
            url, depth, parent, label, nav_pos, in_nav = queue.popleft()
            try:
                await goto(page, url)
            except Exception as e:
                print("  x", url, e.__class__.__name__)
                continue
            if urlparse(page.url).netloc != origin:
                continue
            nid = normalize(page.url)
            if parent and parent != nid and (parent, nid) not in seen_edges:
                seen_edges.add((parent, nid))
                edges.append({"from": parent, "to": nid, "label": label})
            if nid in nodes:
                continue

            title = (await page.title()).strip()
            rich = await page.evaluate(RICHNESS_JS)
            shot = f"shots/{slug(nid)}.png"
            await page.screenshot(path=str(out / shot))
            nodes[nid] = {
                "id": nid, "url": page.url, "label": label, "title": title, "h1": rich.pop("h1"),
                "headings": rich.pop("headings"), "text": rich.pop("text"),
                "depth": depth, "nav_pos": nav_pos, "in_nav": in_nav, "rich": rich, "shot": shot,
            }
            print(f"  [{len(nodes):2}] d{depth} {nid}  ({label})")

            links = [l for l in await page.evaluate(LINKS_JS)
                     if l["visible"] and urlparse(l["href"]).netloc == origin and not SKIP.search(l["href"] + " " + l["text"])]
            links_per_page[nid] = {normalize(l["href"]) for l in links}
            if depth >= max_depth:
                continue
            if CLICKS:  # navegacion exhaustiva: tambien lo que solo se abre con botones
                for burl, btxt in await click_explore(page, page.url, origin):
                    bid = normalize(burl)
                    if bid not in queued:
                        queued.add(bid)
                        links.append({"href": burl, "text": "▸ " + btxt, "inNav": False, "visible": True})
                        print(f"       + botón «{btxt}» -> {bid}")
                    elif bid in nodes and (nid, bid) not in seen_edges:
                        seen_edges.add((nid, bid))
                        edges.append({"from": nid, "to": bid, "label": "▸ " + btxt})
            nav_i = 0
            for l in links:
                lid = normalize(l["href"])
                pos = nav_i if l["inNav"] else 99
                nav_i += l["inNav"]
                if lid in nodes and nid != lid and (nid, lid) not in seen_edges:
                    seen_edges.add((nid, lid))
                    edges.append({"from": nid, "to": lid, "label": l["text"]})
                if lid in queued and not l["text"].startswith("▸"):
                    continue
                prefix = "/".join(lid.split("/")[:-1]) or "/"
                if prefix != "/" and per_prefix[prefix] >= PREFIX_CAP:
                    continue
                per_prefix[prefix] += 1
                queued.add(lid)
                queue.append((l["href"], depth + 1, nid, l["text"] or lid, pos, l["inNav"]))
        await browser.close()

    # Capa global: destinos enlazados desde la mayoria de paginas (la nav que se repite)
    counts = Counter(t for s in links_per_page.values() for t in s)
    n_pages = max(len(links_per_page), 1)
    for n in nodes.values():
        n["global"] = counts.get(n["id"], 0) >= 0.6 * n_pages and n["depth"] > 0
        n["prior"] = round(prior(n), 3)
        n.setdefault("wow", round(wow_heuristic(n), 3))
        n.setdefault("tags", tags_heuristic(n))
        n.setdefault("desc", n["h1"] or n["title"])

    root = normalize(cfg["start"])
    if not nodes:
        sys.exit("No se pudo cargar ninguna pagina")
    graph = {"site": site, "root": root if root in nodes else next(iter(nodes)),
             "nodes": list(nodes.values()), "edges": [e for e in edges if e["from"] in nodes and e["to"] in nodes]}
    (out / "graph.json").write_text(json.dumps(graph, indent=1, ensure_ascii=False))
    print(f"OK {len(graph['nodes'])} nodos, {len(graph['edges'])} aristas -> {out/'graph.json'}")


def prior(n) -> float:
    """'Lo importante es facil de encontrar': poca profundidad, arriba en la nav, presente en todas partes."""
    depth_s = {0: 1.0, 1: 0.8}.get(n["depth"], 0.45)
    nav_s = max(0.2, 1 - n["nav_pos"] / 12) if n["in_nav"] else 0.3
    p = 0.5 * depth_s + 0.35 * nav_s + (0.15 if n["global"] else 0)
    if UTILITY.search(" ".join([n["id"], n["label"], n["title"]])):
        p *= 0.4
    return min(p, 1.0)


def wow_heuristic(n) -> float:
    r = n["rich"]
    return min(1.0, 0.25 + 0.08 * min(r["charts"], 5) + 0.1 * min(r["tables"], 2) + 0.03 * min(r["images"], 5))


def tags_heuristic(n):
    words = re.findall(r"[a-zA-Z]{4,}", " ".join([n["label"], n["id"]]).lower())
    stop = {"index", "view", "html", "module", "list", "page", "http", "https", "www"}
    return list(dict.fromkeys(w for w in words if w not in stop))[:5]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--max", type=int, default=25)
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--clicks", action="store_true", help="explorar tambien botones, pestanas y menus")
    a = ap.parse_args()
    CLICKS = a.clicks
    asyncio.run(crawl(a.site, a.max, a.depth))
